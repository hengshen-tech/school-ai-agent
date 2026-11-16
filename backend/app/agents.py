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

from .config import AGENT_TIMEOUT_S, WORK_DIR
from claude_agent_sdk.types import StreamEvent

_MODEL_LIMITS = weakref.WeakKeyDictionary()


def _model_limit():
    loop = asyncio.get_running_loop()
    if loop not in _MODEL_LIMITS:
        _MODEL_LIMITS[loop] = asyncio.Semaphore(max(1, int(os.environ.get("MAX_PARALLEL_MODEL_CALLS", "3"))))
    return _MODEL_LIMITS[loop]


class AgentError(RuntimeError):
    pass


def _env(cfg: dict) -> dict:
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
        "CLAUDE_CODE_EFFORT_LEVEL": "high",
    }


async def run_agent(cfg: dict, *, system: str, blocks: list, mcp_server=None,
                    allowed_tools: list | None = None, max_turns: int = 1, on_tool=None,
                    emit=None, stage="model", label="") -> str:
    """运行一个 Agent 并返回它最后一段文本。blocks 为 Anthropic 格式的内容块（text / image）。"""
    if not cfg.get("api_key"):
        raise AgentError("尚未配置 API Key，请先到「模型设置」填写。")
    opts = ClaudeAgentOptions(
        system_prompt=system,
        model=cfg["model"],
        env=_env(cfg),
        tools=[],                 # 关闭 Claude Code 内置工具（读写文件、Bash 等），Agent 只能用我们给的工具
        setting_sources=[],       # 不加载任何本机 / 项目设置
        mcp_servers={"grader": mcp_server} if mcp_server else {},
        allowed_tools=allowed_tools or [],
        max_turns=max_turns,
        cwd=str(WORK_DIR),
        thinking={"type": "adaptive"},
        effort="high",
        include_partial_messages=True,
    )
    call_id = uuid.uuid4().hex[:12]
    queued_at = time.monotonic()
    started_at, first_output, usage, buffer = None, None, None, []
    last_flush, last_activity = 0.0, 0.0

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
        nonlocal first_output, usage, last_activity
        last_text, result, err = "", None, None
        streamed = False
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
                    elif delta.get("type") == "thinking_delta" and now - last_activity >= 2:
                        last_activity = now
                        report("info", "模型正在思考", kind="activity")
                elif isinstance(msg, AssistantMessage):
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    if texts:
                        last_text = "\n".join(texts)
                        if not streamed:
                            first_output = first_output or time.monotonic()
                            buffer.append(last_text)
                            flush()
                    if on_tool:
                        for b in msg.content:
                            if isinstance(b, ToolUseBlock):
                                on_tool(b.name, b.input)
                elif isinstance(msg, ResultMessage):
                    result = msg.result
                    usage = msg.usage
                    if msg.is_error and msg.subtype != "error_max_turns":
                        err = msg.result or msg.subtype
        if err:
            raise AgentError(f"模型调用失败：{str(err)[:400]}")
        return result or last_text

    try:
        report("info", f"{label}等待模型调用名额".strip(), kind="activity")
        async with _model_limit():
            started_at = time.monotonic()
            report("info", f"{label}模型开始处理（high）", kind="activity")
            out = await asyncio.wait_for(_go(), AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise AgentError("模型调用超时")
    except AgentError:
        raise
    except Exception as e:  # CLI 进程错误等
        raise AgentError(f"Agent 运行失败：{str(e)[:400]}")
    finally:
        flush()
        now = time.monotonic()
        report("info", kind="metric", metric={"call_id": call_id, "stage": stage, "label": label,
               "model": cfg["model"], "effort": "high", "usage": usage,
               "queue_s": round((started_at or now) - queued_at, 3),
               "elapsed_s": round(now - started_at, 3) if started_at else 0,
               "first_output_s": round(first_output - started_at, 3) if first_output and started_at else None})
    if not out or not out.strip():
        raise AgentError("模型没有返回内容")
    if re.match(r"\s*API Error", out):
        raise AgentError(out.strip()[:400])
    return out


def extract_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise ValueError("no json object")
        t = t[i:j + 1]
    return json.loads(t)


async def run_json_agent(cfg, *, system, blocks, **kw) -> dict:
    """运行 Agent 并解析 JSON；解析失败时带着提醒重试一次。"""
    last = None
    for attempt in range(2):
        if attempt and kw.get("emit"):
            kw["emit"](kw.get("stage", "model"), "info", "模型输出不是合法 JSON，正在重试一次")
        b = blocks if attempt == 0 else blocks + [
            {"type": "text", "text": "上一次的输出不是合法 JSON。请严格只输出一个 JSON 对象。"}]
        out = await run_agent(cfg, system=system, blocks=b, **kw)
        try:
            obj = extract_json(out)
            if not isinstance(obj, dict):
                raise ValueError("not a json object")
            return obj
        except Exception as e:
            last = e
    raise AgentError(f"模型输出无法解析为 JSON（{last}）")


def workflow_options(cfg, state):
    """One SDK session, two real native Agent definitions, no shell/filesystem tools."""
    from claude_agent_sdk import AgentDefinition, HookMatcher
    from . import prompts
    from .workflow_tools import TOOL_NAMES, create_tools
    from . import store
    names = [f'mcp__workflow__{name}' for name in TOOL_NAMES]
    common = ['mcp__workflow__get_state', 'mcp__workflow__read_page']
    page_budget = len(state.pages) + len(state.references)
    child_turns = 2 * page_budget + 16
    return ClaudeAgentOptions(
        system_prompt=prompts.WORKFLOW_MAIN, model=cfg['model'], env={**_env(cfg),
            'CLAUDE_CONFIG_DIR': str(store.sub_dir(state.sid) / 'sdk-config'),
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'},
        tools=['Agent'], allowed_tools=['Agent', *names], setting_sources=[],
        mcp_servers={'workflow': create_tools(state)},
        agents={
            'answer-reviewer': AgentDefinition(description='Independent source and standard answer reviewer',
                prompt=prompts.ANSWER_REVIEW_AGENT, background=False, maxTurns=child_turns, tools=[*common, 'mcp__workflow__submit_review']),
            'student-grader': AgentDefinition(description='Read student pages and grade all questions against reviewed key',
                prompt=prompts.STUDENT_GRADER_AGENT, background=False, maxTurns=child_turns, tools=[*common, 'mcp__workflow__submit_grades'])},
        hooks={'PreToolUse': [HookMatcher(hooks=[state.authorize])]},
        cwd=str(WORK_DIR), max_turns=4 * page_budget + 32, include_partial_messages=True,
        thinking={'type': 'adaptive'}, effort='high')


async def run_workflow_agent(cfg, state):
    """Keep one persistent main client; never treat exhausted loops as completion."""
    if not cfg.get('api_key'):
        raise AgentError('尚未配置 API Key，请先到「模型设置」填写。')
    options = workflow_options(cfg, state)
    started = time.monotonic()
    call_id = uuid.uuid4().hex[:12]
    async def run():
        result_seen = False
        streamed_parents = set()
        child_names = {}
        def emit_text(message, text):
            parent = getattr(message, 'parent_tool_use_id', None)
            role = child_names.get(parent, 'main')
            label = {'main': '主 Agent', 'answer-reviewer': '答案审核子 Agent', 'student-grader': '学生批改子 Agent'}.get(role, role)
            state.emit(state.current_stage, 'output', kind='output', text=text,
                       call_id=f'{call_id}:{parent or "main"}', label=label)
        async with ClaudeSDKClient(options) as client:
            await client.query('执行本任务。先调用 get_state，完整阅读材料并通过各阶段工具提交。')
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    parent = getattr(message, 'parent_tool_use_id', None)
                    for block in message.content:
                        if isinstance(block, TextBlock) and parent not in streamed_parents:
                            emit_text(message, block.text)
                        elif isinstance(block, ToolUseBlock):
                            if block.name in ('Agent', 'Task'):
                                child_names[block.id] = block.input.get('subagent_type', 'main')
                            state.emit(state.current_stage, 'info', f'调用 {block.name}', kind='activity')
                    streamed_parents.discard(parent)
                elif isinstance(message, StreamEvent):
                    delta = message.event.get('delta', {})
                    if delta.get('type') == 'text_delta':
                        streamed_parents.add(getattr(message, 'parent_tool_use_id', None))
                        emit_text(message, delta.get('text', ''))
                elif isinstance(message, ResultMessage):
                    result_seen = True
                    state.emit('parse', 'info', kind='metric', metric={'call_id': call_id, 'stage': 'workflow', 'label': '主代理及原生子代理',
                        'model': cfg['model'], 'effort': 'high', 'usage': message.usage, 'elapsed_s': round(time.monotonic() - started, 3), 'queue_s': 0})
                    if message.is_error or message.subtype != 'success':
                        raise AgentError(f'工作流未完成：{message.result or message.subtype}')
        if not result_seen:
            raise AgentError('工作流会话未返回完成状态')
    try:
        async with _model_limit():
            await asyncio.wait_for(run(), AGENT_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise AgentError('工作流模型调用超时，可重试恢复已复核阶段') from exc
