"""Only explicit job pages and validated state transitions are exposed to the SDK."""
import asyncio
import json
from claude_agent_sdk import create_sdk_mcp_server, tool
from . import imaging

TOOL_NAMES = ['get_state', 'read_page', 'submit_source', 'submit_draft', 'submit_review',
              'submit_grades', 'ocr_page', 'submit_positions']
_STAGES = {
    'submit_source': ('parse', '正在核对材料和完整题目', '材料与题目已整理'),
    'submit_draft': ('solve', '正在保存独立解题草稿', '答案草稿已保存'),
    'submit_review': ('answer_review', '正在检查标准答案审核结果', '标准答案审核完成，争议题待人工确认'),
    'submit_grades': ('grade', '正在保存学生作答批改', '学生作答批改已保存'),
    'ocr_page': ('ocr', '正在读取共享 OCR 结果', '本页 OCR 已读取'),
    'submit_positions': ('locate', '正在检查答案文字块和坐标', '答案定位完成，未定位题保留判分'),
}


async def read_page_image(state, args):
    try:
        if 'reference' in args:
            index = args['reference']
            if type(index) is not int or not 1 <= index <= len(state.references):
                raise ValueError('无效参考页')
            path = state.references[index - 1]['path']
            recognized = state.references[index - 1].get('ocr') or {}
            recognized = {**recognized, 'blocks': [{**b, 'id': f"r{index}:b{i}"}
                for i, b in enumerate(recognized.get('blocks', []), 1)]}
        else:
            index = args.get('page')
            if type(index) is not int or not 1 <= index <= len(state.pages):
                raise ValueError('无效页码')
            path = state.pages[index - 1]
            recognized = state.ocr_pages.get(index) or {}
        block = (await asyncio.to_thread(imaging.page_block, path))['source']
        return {'content': [{'type': 'image', 'data': block['data'], 'mimeType': block['media_type']},
            {'type': 'text', 'text': '本页共享 OCR 结果（可能漏识或误识，原图优先）：\n' +
             json.dumps(recognized, ensure_ascii=False)}]}
    except (ValueError, OSError, KeyError) as exc:
        return {'content': [{'type': 'text', 'text': str(exc)}], 'is_error': True}


def create_tools(state):
    def text(value):
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False, default=str)}]}

    @tool('get_state', 'Read current versioned state, full manifest, shared OCR blocks and coordinates, reviewed answers and archived references.', {})
    async def get_state(args):
        return text(state.snapshot())

    @tool('read_page', 'Read a scoped page as image plus shared OCR text, block IDs and coordinates; or archived selected-paper reference by reference index.',
          {'type': 'object', 'properties': {'page': {'type': 'integer'}, 'reference': {'type': 'integer'}}, 'additionalProperties': False})
    async def read_page(args):
        return await read_page_image(state, args)

    tools = [get_state, read_page]
    for name in TOOL_NAMES[2:]:
        async def handler(args, name=name):
            try:
                stage, starting, finished = _STAGES[name]
                state.emit(stage, 'start', starting)
                method = getattr(state, name)
                result = await method(args['data']) if name == 'ocr_page' else method(args['data'])
                state.emit(stage, 'done', finished)
                return text(result)
            except (ValueError, KeyError, TypeError) as exc:
                return {'content': [{'type': 'text', 'text': f'校验未通过：{exc}'}], 'is_error': True}
        tools.append(tool(name, 'Submit validated workflow transition. Schema is specified in system prompt; payload under data.',
                          {'type': 'object', 'properties': {'data': {'type': 'object'}}, 'required': ['data'], 'additionalProperties': False})(handler))
    return create_sdk_mcp_server(name='workflow', version='2.0.0', tools=tools)
