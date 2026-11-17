"""对 Claude Agent SDK 的薄封装。

模型接入方式：Agent SDK 底层是 Claude Code，通过环境变量指向任意 Anthropic 兼容接口
（DeepSeek 为 https://api.deepseek.com/anthropic），因此模型名 / Key / URL 都可以在前端配置。
"""
import asyncio
import json
import os
import re
import time
import uuid
import weakref

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, TextBlock, ToolUseBlock)

from .config import AGENT_TIMEOUT_S, MODEL_EFFORT, WORK_DIR
from .agent_logging import log_event, log_text
from claude_agent_sdk.types import StreamEvent

_MODEL_LIMITS = weakref.WeakKeyDictionary()
# Image-bearing MCP tool results exceed the SDK's default 1 MiB JSON limit.
SDK_MAX_BUFFER_SIZE = 32 * 1024 * 1024


def _model_limit():
    loop = asyncio.get_running_loop()
    if loop not in _MODEL_LIMITS:
        _MODEL_LIMITS[loop] = asyncio.Semaphore(max(1, int(os.environ.get("MAX_PARALLEL_MODEL_CALLS", "3"))))
    return _MODEL_LIMITS[loop]


class AgentError(RuntimeError):
    pass


def _env(cfg: dict, effort=MODEL_EFFORT) -> dict:
    m = cfg["model"]
    return {
        "ANTHROPIC_BASE_URL": cfg["base_url"].rstrip("/"),
        "ANTHROPIC_AUTH_TOKEN": cfg["api_key"],
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m,
        "CLAUDE_CODE_SUBAGENT_MODEL": m,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "ENABLE_TOOL_SEARCH": "false",  # 第三方接口不认识延迟加载工具的占位符
        "API_TIMEOUT_MS": str(AGENT_TIMEOUT_S * 1000),
        "CLAUDE_CODE_EFFORT_LEVEL": effort,
    }


async def run_agent(cfg: dict, *, system: str, blocks: list, mcp_server=None,
                    allowed_tools: list | None = None, max_turns: int = 1, on_tool=None,
                    emit=None, stage="model", label="", effort=MODEL_EFFORT, require_success=False,
                    builtin_tools=None, work_dir=None, hooks=None, env_overrides=None,
                    submission_id=None) -> str:
    """运行一个 Agent 并返回它最后一段文本。blocks 为 Anthropic 格式的内容块（text / image）。"""
    if not cfg.get("api_key"):
        raise AgentError("尚未配置 API Key，请先到「模型设置」填写。")
    opts = ClaudeAgentOptions(
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=system,
        model=cfg["model"],
        env={**_env(cfg, effort), **(env_overrides or {})},
        tools=builtin_tools or [],
        setting_sources=[],       # 不加载任何本机 / 项目设置
        mcp_servers={"grader": mcp_server} if mcp_server else {},
        strict_mcp_config=True,
        allowed_tools=allowed_tools or [],
        max_turns=max_turns,
        cwd=str(work_dir or WORK_DIR),
        hooks=hooks,
        thinking={"type": "adaptive"},
        effort=effort,
        include_partial_messages=True,
    )
    call_id = uuid.uuid4().hex[:12]
    queued_at = time.monotonic()
    started_at, first_output, usage, buffer = None, None, None, []
    last_flush = 0.0

    def console(event, **fields):
        log_event(event, submission_id=submission_id, call_id=call_id, **fields)

    def report(status, message="", **extra):
        if emit:
            emit(stage, status, message, call_id=call_id, label=label, **extra)

    def flush():
        nonlocal last_flush
        if buffer:
            report("output", kind="output", text="".join(buffer))
            buffer.clear()
        last_flush = time.monotonic()

    async def _go():
        nonlocal first_output, usage
        last_text, result, err, result_seen = "", None, None, False
        streamed = False
        thinking_reported = False
        async with ClaudeSDKClient(opts) as client:
            async def gen():
                yield {"type": "user", "message": {"role": "user", "content": blocks},
                       "parent_tool_use_id": None, "session_id": "default"}
            await client.query(gen())
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    delta = event.get("delta", {})
                    now = time.monotonic()
                    if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        first_output = first_output or now
                        streamed = True
                        buffer.append(delta.get("text", ""))
                        if now - last_flush >= .1:
                            flush()
                    elif delta.get("type") == "thinking_delta" and not thinking_reported:
                        thinking_reported = True
                        report("info", "模型正在思考...", kind="activity", activity_type='thinking')
                elif isinstance(msg, AssistantMessage):
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    log_text('agent_output', '\n'.join(texts), submission_id=submission_id, call_id=call_id,
                        incomplete=msg.stop_reason == 'max_tokens')
                    if require_success and getattr(msg, 'stop_reason', None) == 'max_tokens':
                        raise AgentError('模型输出被截断，请减少本次材料后重试')
                    if texts:
                        last_text = "\n".join(texts)
                        if not streamed:
                            first_output = first_output or time.monotonic()
                            buffer.append(last_text)
                            flush()
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            if on_tool:
                                on_tool(b.name, b.input)
                elif isinstance(msg, ResultMessage):
                    result_seen = True
                    result = msg.result
                    usage = msg.usage
                    console('result', subtype=msg.subtype, is_error=msg.is_error, num_turns=msg.num_turns, usage=usage)
                    if result and result != last_text:
                        log_text('agent_result', result, submission_id=submission_id, call_id=call_id)
                    if require_success and (msg.subtype != 'success' or msg.is_error or
                                            getattr(msg, 'stop_reason', None) == 'max_tokens'):
                        err = '模型输出被截断' if getattr(msg, 'stop_reason', None) == 'max_tokens' else msg.result or msg.subtype
                    elif msg.is_error and msg.subtype != "error_max_turns":
                        err = msg.result or msg.subtype
        if require_success and not result_seen:
            raise AgentError('模型未返回完成状态，请重试')
        if err:
            raise AgentError(f"模型调用失败：{str(err)[:400]}")
        return result or last_text

    try:
        console('queued', model=cfg['model'], effort=effort, max_turns=max_turns)
        report("info", f"{label}等待模型调用名额".strip(), kind="activity")
        async with _model_limit():
            started_at = time.monotonic()
            console('started', queue_s=round(started_at - queued_at, 3))
            log_text('system_prompt', system, submission_id=submission_id, call_id=call_id)
            for index, block in enumerate(blocks):
                if block.get('type') == 'text':
                    log_text('input_text', block.get('text', ''), submission_id=submission_id, call_id=call_id, block=index)
                elif block.get('type') == 'image':
                    console('input_image', block=index, media_type=block.get('source', {}).get('media_type'))
            report("info", f"{label}模型开始处理（{effort}）", kind="activity")
            out = await asyncio.wait_for(_go(), AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        console('failed', reason='timeout')
        raise AgentError("模型调用超时")
    except asyncio.CancelledError:
        console('cancelled', reason='task_cancelled')
        raise
    except AgentError as exc:
        console('failed', reason=str(exc))
        raise
    except Exception as e:  # CLI 进程错误等
        console('failed', reason=str(e)[:400])
        raise AgentError(f"Agent 运行失败：{str(e)[:400]}")
    finally:
        flush()
        now = time.monotonic()
        console('finished', elapsed_s=round(now - started_at, 3) if started_at else None, usage=usage)
        report("info", kind="metric", metric={"call_id": call_id, "stage": stage, "label": label,
               "model": cfg["model"], "effort": effort, "usage": usage,
               "queue_s": round((started_at or now) - queued_at, 3),
               "elapsed_s": round(now - started_at, 3) if started_at else 0,
               "first_output_s": round(first_output - started_at, 3) if first_output and started_at else None})
    if not out or not out.strip():
        raise AgentError("模型没有返回内容")
    if re.match(r"\s*API Error", out):
        raise AgentError(out.strip()[:400])
    return out


def extract_json(text: str, *, result_filter=None) -> dict:
    """Find complete top-level documents without joining or mining nested rows."""
    candidates, stack = {}, []
    start, quoted, escaped, parse_error = None, False, False, None
    for index, char in enumerate(text):
        if start is None:
            if char in '{[':
                start, stack = index, [char]
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack or stack[-1] != ('{' if char == '}' else '['):
                raise ValueError('JSON 括号不匹配，不能确认完整结果')
            stack.pop()
            if not stack:
                try:
                    value = json.loads(text[start:index + 1])
                    if isinstance(value, dict) and (result_filter is None or result_filter(value)):
                        fingerprint = json.dumps(value, sort_keys=True, ensure_ascii=False)
                        candidates[fingerprint] = value
                except json.JSONDecodeError as exc:
                    parse_error = exc
                start = None
    if start is not None:
        raise ValueError('JSON 对象或数组未闭合，输出可能被截断')
    if len(candidates) > 1:
        raise ValueError('返回了多份不同的完整 JSON，无法确定唯一结果')
    if candidates:
        return next(iter(candidates.values()))
    if result_filter is not None:
        raise ValueError('未找到符合预期结构的完整 JSON 对象；不能将分段结果直接拼接')
    raise ValueError(f'未找到完整 JSON 对象：{parse_error}' if parse_error else '未找到完整 JSON 对象（不接受数组）')


async def run_json_agent(cfg, *, system, blocks, attempts=2, result_filter=None, **kw) -> dict:
    """运行 Agent 并解析 JSON；解析失败时带着提醒重试一次。"""
    last = None
    for attempt in range(attempts):
        if attempt and kw.get("emit"):
            kw["emit"](kw.get("stage", "model"), "info", "模型输出不是合法 JSON，正在重试一次")
        b = blocks if attempt == 0 else blocks + [
            {"type": "text", "text": "上一次的输出不是合法 JSON。请严格只输出一个 JSON 对象。"}]
        out = await run_agent(cfg, system=system, blocks=b, **kw)
        try:
            obj = extract_json(out, result_filter=result_filter)
            if not isinstance(obj, dict):
                raise ValueError("not a json object")
            return obj
        except Exception as e:
            last = e
            log_event('json_parse_failed', submission_id=kw.get('submission_id'), attempt=attempt + 1, reason=str(e))
    raise AgentError(f"模型输出无法解析为 JSON（{last}）")
