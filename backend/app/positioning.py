"""Validate actual OCR answer fragments and repair saved annotations without model calls."""
import json
import math
import re
import threading
import unicodedata

from PIL import Image

from . import imaging, ocr, store

ANNOTATION_VERSION = 4
_REPAIR_LOCK = threading.RLock()
_WIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_NUMBER = re.compile(r'(?:\d+(?:\.\d+)?|\.\d+)\Z')


def _answer_tokens(value):
    value = unicodedata.normalize('NFC', str(value or '')).translate(_WIDTH).lower()
    value = value.replace('−', '-').replace('﹣', '-').replace('×', '*').replace('÷', '/')
    value = re.sub(r'\\(?:text|mathrm|operatorname)\s*\{([^{}]*)\}', r'\1', value)
    value = re.sub(r'\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'(\1)/(\2)', value)
    value = re.sub(r'\\(?:quad|qquad|newline|cr)\b|\n', '|', value)
    value = re.sub(r'\\(?:left|right)\b', '', value)
    value = value.replace(r'\times', '*').replace(r'\cdot', '*').replace(r'\div', '/')
    raw = re.findall(r'\d+(?:\.\d+)?|\.\d+|[a-z]+|[\u3400-\u9fff]|[=<>+*/()|,;:\-]', value)
    tokens, index = [], 0
    while index < len(raw):
        token = raw[index]
        previous = raw[index - 1] if index else None
        if token in ('+', '-') and index + 1 < len(raw) and _NUMBER.fullmatch(raw[index + 1]) and (
            previous is None or previous in ('=', '(', '<', '>', '|', ',', ';', ':', '*', '/')
        ):
            tokens.append(('-' if token == '-' else '') + raw[index + 1])
            index += 2
            continue
        if token not in ('|', ',', ';', ':'):
            tokens.append(token)
        index += 1
    return tokens


def answer_text_matches(student_answer, block_text):
    """An OCR block may be just the final result or one part of a worked solution.

    Match whole tokens in reading order, preserving decimal points and unary minus.
    The Agent still chooses the page/block; never search the sheet for equal numbers.
    """
    source, target = _answer_tokens(student_answer), _answer_tokens(block_text)
    if not source or not target:
        return False
    cursor = iter(source)
    return all(any(actual == expected for actual in cursor) for expected in target)


def _missing_note(question, page):
    if page.get('error'):
        return f"OCR 识别失败（{page['error']}），保留判分；答案位置待确认"
    if not page.get('complete'):
        return '本页 OCR 结果不完整，未获取可靠坐标；保留判分'
    count = len(page.get('blocks') or [])
    if not count:
        return '本页 OCR 未返回可用文字块坐标；保留判分'
    if question.get('answer_state') == 'blank':
        return '本题未作答，没有可标记的学生答案文字块'
    return f'OCR 已识别 {count} 个文字块，尚未匹配到本题学生答案；位置待确认'


def pending_positions(grades):
    return [q for q in grades or [] if not (q.get('bbox_ok') and q.get('bbox_px'))
            and not (q.get('position_source') != 'vision' and q.get('answer_state') == 'blank' and not str(q.get('student_answer', '')).strip()
                     and q.get('grade_confidence', 0) >= .7)]


def apply_visual_fallback(questions, pages, direct=False):
    """Use the same agent's visual box only when OCR supplied no valid position."""
    sizes = {}
    for question in questions:
        if question.get('bbox_ok') and question.get('bbox_px'):
            continue
        if direct:
            if question.get('answer_state') not in ('answered', 'blank', 'unreadable'):
                continue
        elif question.get('answer_state') != 'answered' or not str(question.get('student_answer', '')).strip():
            continue
        confidence = question.get('position_confidence')
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not .9 <= confidence <= 1:
            continue
        box = ocr.valid_box(question.get('answer_bbox'))
        if not box or (box[2] - box[0]) * (box[3] - box[1]) > (800000 if direct else 250000):
            continue
        page = question.get('page')
        if type(page) is not int or not 1 <= page <= len(pages):
            continue
        # A visual guess cannot reuse the confirmed answer area of another question.
        conflict = False
        for other in questions:
            if other is question or other.get('page') != page or not other.get('bbox_ok'):
                continue
            old = ocr.valid_box(other.get('bbox_norm'))
            if old:
                intersection = max(0, min(box[2], old[2]) - max(box[0], old[0])) * max(
                    0, min(box[3], old[3]) - max(box[1], old[1]))
                if intersection > .5 * min((box[2] - box[0]) * (box[3] - box[1]),
                                            (old[2] - old[0]) * (old[3] - old[1])):
                    conflict = True
                    break
        if conflict:
            continue
        if page not in sizes:
            with Image.open(pages[page - 1]) as image:
                sizes[page] = image.size
        question.update(bbox_norm=box, bbox_px=imaging.norm_to_px(box, *sizes[page]), bbox_ok=True,
            position_source='vision' if direct else 'vision_fallback', annotation_anchor='answer', ocr_block_ids=[],
            draw=question.get('status') in (('correct', 'wrong', 'partial', 'review') if direct else ('correct', 'wrong', 'partial')),
            position_note='')


def apply_positions(questions, ocr_pages, pages, rows):
    """Apply valid selections independently; omitted questions keep prior valid positions."""
    if not isinstance(rows, list):
        raise ValueError('positions 必须为数组')
    by_id = {str(q['id']): q for q in questions}
    owners = {(q['page'], block): str(q['id']) for q in questions for block in q.get('ocr_block_ids', [])}
    choices, rejected, seen = {}, [], set()
    for row in rows:
        ident = str(row.get('id', '')).strip() if isinstance(row, dict) else ''
        question = by_id.get(ident)
        try:
            if not question or ident in seen or row.get('page') != question['page']:
                raise ValueError('题号重复、未知或定位页码与学生答案不一致')
            seen.add(ident)
            ids = row.get('block_ids', [])
            if not isinstance(ids, list) or not all(isinstance(v, str) for v in ids) or len(ids) != len(set(ids)):
                raise ValueError('文字块编号无效或重复')
            page = question['page']
            cached = ocr_pages.get(page, {})
            selected = ocr.answer_blocks(ids, cached.get('blocks', [])) if cached.get('complete') and ids else []
            if ids and len(selected) != len(ids):
                raise ValueError('OCR 文字块不存在、不完整或不属于该页')
            if any(owners.get((page, block), ident) != ident for block in ids):
                raise ValueError('同一文字块不能分配给多道题')
            if selected and (question.get('answer_state') != 'answered' or not answer_text_matches(
                question.get('student_answer', ''), '\n'.join(b['text'] for b in selected)
            )):
                raise ValueError('OCR 文字与该题学生答案不匹配，不能使用此位置')
            for key, owner in list(owners.items()):
                if owner == ident:
                    del owners[key]
            owners.update({(page, block): ident for block in ids})
            choices[ident] = selected
        except (ValueError, TypeError) as exc:
            rejected.append({'id': ident, 'reason': str(exc)})
    if rows and not choices and rejected:
        raise ValueError('；'.join(f"第 {r['id']} 题：{r['reason']}" for r in rejected))
    errors = {r['id']: r['reason'] for r in rejected}
    sizes = {}
    for question in questions:
        ident, page = str(question['id']), question['page']
        if ident in choices:
            selected = choices[ident]
            question.update(draw=False, bbox_norm=None, bbox_px=None, bbox_ok=False, ocr_block_ids=[])
            if selected:
                boxes = [b['box'] for b in selected]
                box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
                if page not in sizes:
                    with Image.open(pages[page - 1]) as image:
                        sizes[page] = image.size
                question.update(bbox_norm=box, bbox_px=imaging.norm_to_px(box, *sizes[page]), bbox_ok=True,
                                ocr_block_ids=[b['id'] for b in selected], annotation_anchor='answer', position_source='ocr_blocks')
        valid = bool(question.get('bbox_ok') and question.get('bbox_px'))
        question['draw'] = valid and question.get('status') in ('correct', 'wrong', 'partial')
        if valid:
            question['position_note'] = '' if question['draw'] else '已定位学生答案，人工确认后即可标记'
        else:
            question['position_note'] = errors.get(ident) or _missing_note(question, ocr_pages.get(page, {}))
    return {'positioned': sum(bool(q.get('bbox_ok') and q.get('bbox_px')) for q in questions),
            'drawn': sum(bool(q.get('draw')) for q in questions), 'rejected': rejected,
            'unpositioned': [q['id'] for q in questions if not q.get('bbox_ok') or not q.get('bbox_px')]}


def save_proposal(sid, data, source_version, draft_version):
    path = store.sub_dir(sid) / 'position-proposals.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps({'source_version': source_version, 'draft_version': draft_version,
                                 'positions': data.get('positions')}, ensure_ascii=False) + '\n')


def _saved_proposals(folder):
    journal = folder / 'position-proposals.jsonl'
    paths = [journal] if journal.exists() else sorted((folder / 'sdk-config').rglob('*.jsonl'))
    for path in paths:
        if 'subagents' in path.parts:
            continue
        with path.open(encoding='utf-8') as source:
            for line in source:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if path == journal:
                    if isinstance(item.get('positions'), list):
                        yield item['positions']
                    continue
                if item.get('isSidechain'):
                    continue
                content = item.get('message', {}).get('content', [])
                for block in content if isinstance(content, list) else []:
                    if block.get('type') == 'tool_use' and block.get('name') == 'mcp__workflow__submit_positions':
                        data = block.get('input', {}).get('data', {})
                        if isinstance(data.get('positions'), list):
                            yield data['positions']


def ensure_workflow_annotations(sid, result):
    """Replay the Agent's saved selections against existing OCR, preserving manual verdicts."""
    if result.get('workflow_version') != 2 or result.get('annotation_version', 0) >= ANNOTATION_VERSION:
        return result
    with _REPAIR_LOCK:
        result = store.get_result(sid) or result
        if result.get('annotation_version', 0) >= ANNOTATION_VERSION:
            return result
        folder = store.sub_dir(sid)
        pages = [folder / 'pages' / f'page-{page}.png' for page in range(1, result.get('page_count', 0) + 1)]
        if not pages or not all(p.is_file() for p in pages):
            return result
        backup = folder / 'result-before-position-repair-v4.json'
        if not backup.exists():
            store.save_artifact(sid, backup.name, result)
        ocr_pages = {int(page): value for page, value in (store._read(folder / 'ocr.json', {}) or {}).items()}
        questions = result.get('questions', [])
        for rows in _saved_proposals(folder):
            try:
                apply_positions(questions, ocr_pages, pages, rows)
            except (ValueError, TypeError):
                continue
        apply_positions(questions, ocr_pages, pages, [])
        from .pipeline import render_annotations
        render_annotations(sid, result)
        result['annotation_version'] = ANNOTATION_VERSION
        store.save_result(sid, result)
        return result
