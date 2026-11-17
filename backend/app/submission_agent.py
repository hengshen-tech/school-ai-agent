"""One SDK agent receives all inputs and owns the complete reasoning task."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

from . import imaging, prompts
from .agents import run_json_agent
from .visual_tools import VisualTools
from .paper_library import PaperLibrary
from . import store
from claude_agent_sdk import HookMatcher


async def run_single_agent(cfg, state):
    saved = state.snapshot().get('selected_paper')
    directory = store.sub_dir(state.sid) / 'agent-workdir'
    library = await asyncio.to_thread(PaperLibrary, directory) if saved is None else None
    tools = VisualTools(state.pages, state.references, directory, submission_id=state.sid, library=library)
    selected = None
    if saved:
        selected = {k: saved[k] for k in ('id', 'rev', 'title', 'questions') if k in saved}
        selected['answers'] = [{k: a[k] for k in ('id', 'answer', 'accepted', 'knowledge',
            'review_status', 'solution_method') if k in a} for a in saved.get('answers', [])]
    context = {'mode': state.mode, 'materials': state.manifest, 'teacher_key': state.teacher_key,
        'prefer_saved_answers': bool(state.meta.get('prefer_saved_answers')), 'selected_paper': selected,
        'paper_library': library.context if library else None,
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
    def result_matches(value):
        results = value.get('results')
        if not isinstance(results, list) or (state.mode == 'prepare' and results):
            return False
        if selected is None and not (library and isinstance(value.get('selected_paper_id'), str)
                                     and value['selected_paper_id'] in library.papers):
            return all(isinstance(value.get(key), list) and value[key] for key in ('questions', 'answers'))
        return True
    value = await run_json_agent(cfg, system=prompts.SINGLE_SUBMISSION, blocks=blocks,
        max_turns=max(1, int(os.environ.get('EXAM_AGENT_MAX_TURNS', '200'))), submission_id=state.sid,
        attempts=1, require_success=True, emit=state.emit, result_filter=result_matches,
        builtin_tools=['Read', 'Bash'], allowed_tools=['Read', 'Bash'], work_dir=tools.directory,
        hooks={'PreToolUse': [HookMatcher(hooks=[tools.authorize])],
            'PostToolUse': [HookMatcher(hooks=[tools.tool_finished])],
            'PostToolUseFailure': [HookMatcher(hooks=[tools.tool_finished])]},
        env_overrides={'CLAUDE_CONFIG_DIR': str(tools.directory / 'sdk-config'),
            'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1', 'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'},
        on_tool=lambda name, args: state.emit('process', 'info',
            ('正在查阅试卷库' if str(args.get('file_path', '')).endswith('.json') else '正在读取原图或裁剪细节')
            if name == 'Read' else '正在裁剪放大或核算', kind='activity'),
        stage='process', label='看图、独立解题与批改')
    if library:
        paper = library.resolve_selection(value)
        if paper:
            state.select_library_paper(paper, value['paper_match_reason'])
            # A newly uploaded teacher key can still be compared with the saved key;
            # never ask the Agent to echo or solve the entire library key again.
            value = {**value, 'answers': paper['answers']}
    elif value.get('selected_paper_id') and (value['selected_paper_id'] != saved['id'] or
            type(value.get('selected_paper_rev')) is not int or value['selected_paper_rev'] != saved.get('rev', 0)):
        raise ValueError('不能替换用户已选择的试卷或试卷版本')
    return value
