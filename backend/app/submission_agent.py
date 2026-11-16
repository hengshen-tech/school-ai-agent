"""One SDK agent receives all inputs and owns the complete reasoning task."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from . import imaging, prompts
from .agents import run_json_agent
from .visual_tools import VisualTools
from . import store
from claude_agent_sdk import HookMatcher


async def run_single_agent(cfg, state):
    tools = VisualTools(state.pages, state.references, store.sub_dir(state.sid) / 'agent-workdir')
    saved = state.snapshot().get('selected_paper')
    selected = None
    if saved:
        selected = {k: saved[k] for k in ('id', 'rev', 'title', 'questions') if k in saved}
        selected['answers'] = [{k: a[k] for k in ('id', 'answer', 'accepted', 'knowledge',
            'review_status', 'solution_method') if k in a} for a in saved.get('answers', [])]
    context = {'mode': state.mode, 'materials': state.manifest, 'teacher_key': state.teacher_key,
        'prefer_saved_answers': bool(state.meta.get('prefer_saved_answers')), 'selected_paper': selected,
        'image_paths': tools.pages, 'reference_image_paths': tools.references,
        'visual_command_prefix': tools.command_prefix}
    blocks = [{'type': 'text', 'text': json.dumps(context, ensure_ascii=False)}]
    for page, path in enumerate(state.pages, 1):
        blocks.append({'type': 'text', 'text': f'上传第 {page} 页，全分辨率原图：{tools.pages[page - 1]}'})
        blocks.append(await asyncio.to_thread(imaging.page_block, path))
    uploaded = {digest: index for index, digest in enumerate(state.page_hashes, 1)}
    for index, reference in enumerate(state.references, 1):
        path = Path(reference['path'])
        digest = await asyncio.to_thread(lambda p=path: hashlib.sha256(p.read_bytes()).hexdigest())
        label = f'存档参考第 {index} 页（可能含历史学生作答，不能当成标准答案）'
        if digest in uploaded:
            blocks.append({'type': 'text', 'text': f'{label}：图片与上传第 {uploaded[digest]} 页相同。'})
        else:
            blocks.append({'type': 'text', 'text': label})
            blocks.append(await asyncio.to_thread(imaging.page_block, path))
    return await run_json_agent(cfg, system=prompts.SINGLE_SUBMISSION, blocks=blocks,
        max_turns=max(1, int(os.environ.get('EXAM_AGENT_MAX_TURNS', '200'))),
        attempts=1, require_success=True, emit=state.emit,
        builtin_tools=['Read', 'Bash'], allowed_tools=['Read', 'Bash'], work_dir=tools.directory,
        hooks={'PreToolUse': [HookMatcher(hooks=[tools.authorize])]},
        env_overrides={'CLAUDE_CONFIG_DIR': str(tools.directory / 'sdk-config'),
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'},
        on_tool=lambda name, args: state.emit('process', 'info',
            '正在读取原图或裁剪细节' if name == 'Read' else '正在裁剪放大或核算', kind='activity'),
        stage='process', label='看图、独立解题与批改')
