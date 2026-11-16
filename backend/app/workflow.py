"""Versioned, job-scoped agent workflow. Models propose; this module validates."""
import asyncio
import copy
import hashlib
import json
import math
import time
from pathlib import Path
from fractions import Fraction

from PIL import Image
from . import imaging, materials, ocr, store
from .agents import AgentError, run_workflow_agent
from .positioning import ANNOTATION_VERSION


def _clone(value):
    return copy.deepcopy(value)


def _confidence(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and 0 <= n <= 1 else 0
    except (ValueError, TypeError):
        return 0


def _review_agrees(answer, review):
    independent = store.norm_answer(review.get('independent_answer') or '')
    if not independent:
        return False
    accepted = [store.norm_answer(v) for v in [answer['answer'], *(answer.get('accepted') or [])]]
    if independent in accepted:
        return True
    try:
        actual = Fraction(independent)
        expected = Fraction(store.norm_answer(answer['answer']))
        return actual == expected
    except (ValueError, ZeroDivisionError):
        # Subjective answers can express the same rubric in different words.
        return review.get('equivalent_to_draft') is True


class WorkflowState:
    def __init__(self, sid, cfg, meta, pages, manifest, emit, teacher_key=''):
        self.sid, self.cfg, self.meta = sid, cfg, meta
        self.current_stage = 'parse'
        self.pages, self.manifest, self.emit = list(pages), _clone(manifest), emit
        self.teacher_key = teacher_key
        self.mode = meta.get('mode', 'grade')
        if self.mode not in ('grade', 'prepare'):
            raise ValueError('无效工作模式')
        self.source_version = self.draft_version = 0
        self.source = self.review = self.grades = None
        self.answers, self.ocr_pages = [], {}
        self.positions_submitted = False
        self.paper_cache_hit = False
        self.published = False
        self.reviews_count = 0
        self.reads = {}
        self.paper = store.get_paper(meta['paper_id']) if meta.get('paper_id') else None
        if meta.get('paper_id') and not self.paper:
            raise ValueError('所选试卷不存在')
        self.references = (self.paper or {}).get('reference_pages', [])
        self.page_hashes = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in pages]
        self.context_hash = hashlib.sha256(json.dumps({'manifest': manifest, 'teacher': teacher_key,
            'mode': self.mode, 'paper': meta.get('paper_id'), 'reference_revision_bound': True, 'ocr_stage': 'input-v1',
            'config': {k: v for k, v in cfg.items() if k != 'api_key' and not k.startswith('_')},
            'files': self.page_hashes},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def persist(self, name, data):
        store.save_artifact(self.sid, name, _clone(data))

    def snapshot(self):
        paper = _clone(self.paper)
        references = [{'reference': i + 1, **_clone(ref)} for i, ref in enumerate(self.references)]
        for index, reference in enumerate(references, 1):
            if reference.get('ocr'):
                reference['ocr']['blocks'] = [{**block, 'id': f'r{index}:b{i}'}
                    for i, block in enumerate(reference['ocr'].get('blocks', []), 1)]
        if paper:
            paper['reference_pages'] = references
        if paper and self.references:
            for q in paper['questions']:
                old = q.get('source_pages') or ([q['source_page']] if q.get('source_page') else [])
                mapped = [i + 1 for i, r in enumerate(self.references) if r['page'] in old]
                q['reference_pages'] = mapped or q.get('reference_pages') or list(range(1, len(self.references) + 1))
                q['source_pages'] = []
                q.pop('source_page', None)
        return _clone({'mode': self.mode, 'manifest': self.manifest, 'teacher_key': self.teacher_key,
            'prefer_saved_answers': bool(self.meta.get('prefer_saved_answers')),
            'source_version': self.source_version, 'draft_version': self.draft_version,
            'source': self.source, 'answers': self.answers, 'review': self.review,
            'grades': self.grades, 'ocr_pages': self.ocr_pages,
            'positions_submitted': self.positions_submitted, 'selected_paper': paper,
            'reference_pages': references})

    def _ids(self, rows, expected=None):
        if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
            raise ValueError('必须提供完整且非空的题目列表')
        ids = [str(r.get('id', '')).strip() for r in rows]
        if any(not x for x in ids) or len(ids) != len(set(ids)):
            raise ValueError('题号必须非空且唯一')
        if expected is not None and set(ids) != set(expected):
            raise ValueError('必须覆盖全部题目且不能新增题号')
        for row, ident in zip(rows, ids):
            row['id'] = ident
        return ids

    def _version(self, data):
        if data.get('source_version') != self.source_version or data.get('draft_version') != self.draft_version:
            raise ValueError('源题目或答案版本已变更，请重新读取状态')

    def _same_source_images(self, questions):
        """Text alone cannot identify diagram questions; only auto-reuse exact source images."""
        saved = {str(q['id']): q for q in self.paper['questions']}
        digests = {}
        for question in questions:
            pages = question.get('source_pages') or []
            old = saved.get(question['id'], {})
            references = old.get('reference_pages') or []
            if not references:
                source_pages = old.get('source_pages') or [old.get('source_page', old.get('page'))]
                references = [i + 1 for i, ref in enumerate(self.references) if ref['page'] in source_pages]
            if not pages or not references:
                return False
            archived = []
            for index in references:
                if type(index) is not int or not 1 <= index <= len(self.references):
                    return False
                path = str(self.references[index - 1]['path'])
                if path not in digests:
                    try:
                        digests[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    except OSError:
                        return False
                archived.append(digests[path])
            if [self.page_hashes[page - 1] for page in pages] != archived:
                return False
        return True

    def submit_source(self, data):
        data = _clone(data)
        if data.get('multiple_students'):
            raise ValueError('材料包含多位学生，请分别提交每位学生的作答')
        rows = data.get('materials')
        if not isinstance(rows, list) or len(rows) != len(self.manifest):
            raise ValueError('材料用途必须覆盖每页')
        by_page = {r.get('page'): r for r in rows if isinstance(r, dict)}
        if set(by_page) != {m['page'] for m in self.manifest}:
            raise ValueError('材料页码重复或不完整')
        resolved = []
        for m in self.manifest:
            row = by_page[m['page']]
            role = m.get('requested_role', 'auto')
            if role == 'auto':
                role = row.get('role')
                if _confidence(row.get('confidence')) < .7:
                    raise ValueError('无法确认材料用途，请手动指定用途')
            if role not in ('question_paper', 'student_work', 'answer_key'):
                raise ValueError('无法确认材料用途')
            resolved.append({**m, 'role': role})
        work = [r['page'] for r in resolved if r['role'] == 'student_work']
        if self.mode == 'prepare' and work:
            raise ValueError('准备试卷模式不接收学生作答，请移除学生材料或切换为批改模式')
        if self.mode == 'grade' and not work:
            raise ValueError('请添加学生作答材料')
        questions = data.get('questions')
        ids = self._ids(questions)
        valid_pages = set(by_page)
        for q in questions:
            q['id'] = str(q['id']).strip()
            refs = q.get('source_pages') or ([q['source_page']] if type(q.get('source_page')) is int else [])
            if not q.get('question', '').strip() or q.get('type') not in ('choice', 'blank', 'calc', 'judge', 'subjective'):
                raise ValueError('每道题必须包含完整题干和有效类型')
            # Selected papers may supply archived sources using reference indexes.
            if not refs and not (self.paper and q.get('reference_pages')):
                raise ValueError('每道题必须标明题干来源页')
            if any(type(p) is not int or p not in valid_pages for p in refs):
                raise ValueError('题干来源页无效')
            archived = q.get('reference_pages', [])
            if not isinstance(archived, list) or any(type(p) is not int or not 1 <= p <= len(self.references) for p in archived):
                raise ValueError('存档参考页索引无效')
            q['source_pages'] = refs
        if self.paper and self.meta.get('paper_id'):
            saved = self.paper.get('questions', [])
            if {str(q['id']) for q in saved} != set(ids) or any(
                store.norm_answer(q['question']) != store.norm_answer(next(x['question'] for x in saved if str(x['id']) == q['id'])) for q in questions):
                raise ValueError('所选试卷题目必须完整保留，请从试卷库读取完整题目')
        refs = data.get('reference_answers', [])
        if refs and not any(m['role'] == 'answer_key' for m in resolved):
            raise ValueError('只有标准答案材料可提供参考答案')
        if refs:
            ref_ids = self._ids(refs)
            if not set(ref_ids) <= set(ids):
                raise ValueError('老师答案题号未对齐')
        self.source = {**data, 'materials': resolved, 'questions': questions, 'reference_answers': refs}
        self.source_version += 1
        self.draft_version = 0
        self.answers, self.review, self.grades = [], None, None
        self.positions_submitted = False
        self.reviews_count = 0
        self.persist('materials.json', resolved)
        self.persist('layout.json', self.source)
        self.persist(f'source-v{self.source_version}.json', self.source)
        self.published = False
        self.paper_cache_hit = False
        if not self.paper:
            pid, _ = store.match_paper(questions)
            if pid:
                candidate = store.get_paper(pid)
                if candidate and {str(q['id']) for q in candidate['questions']} == set(ids):
                    self.paper = candidate
                    self.references = candidate.get('reference_pages', [])
        cached = (self.paper or {}).get('answers', [])
        has_teacher_material = any(m['role'] == 'answer_key' for m in resolved)
        same_questions = self.paper and {(str(q['id']), store.norm_answer(q['question'])) for q in self.paper['questions']} == {(q['id'], store.norm_answer(q['question'])) for q in questions}
        trusted = same_questions and cached and {str(a['id']) for a in cached} == set(ids) and all(
            bool(str(a.get('answer', '')).strip()) and (a.get('review_status') in ('approved', 'manual_confirmed') or a.get('edited')) for a in cached)
        if self.paper and (not same_questions or (not self.meta.get('paper_id') and not self._same_source_images(questions))):
            self.paper = None
            self.references = []
            trusted = False
        if trusted and (self.meta.get('prefer_saved_answers') or (not self.teacher_key.strip() and not refs and not has_teacher_material)):
            self.answers = _clone(cached)
            for a in self.answers:
                if a.get('edited'): a['review_status'] = 'manual_confirmed'
            self.draft_version = 1
            self.review = {'source_version': self.source_version, 'draft_version': 1,
                           'reviews': [{'id': a['id'], 'status': a['review_status'], 'reason': '复用已复核试卷版本'} for a in self.answers],
                           'cached_paper_id': self.paper['id'], 'cached_paper_rev': self.paper.get('rev', 0)}
            self.paper_cache_hit = self.published = True
            self.emit('paper', 'done', '复用已复核的试卷版本')
            self.emit('solve', 'skip', '复用已复核标准答案')
            self.emit('answer_review', 'skip', '已有当前试卷版本的复核记录')
        return {'source_version': self.source_version, 'cached_review': self.paper_cache_hit, 'next': 'get_state'}

    def submit_draft(self, data):
        if not self.source:
            raise ValueError('先提交完整题目和材料用途')
        if self.draft_version >= 2:
            raise ValueError('只允许一次 AI 修正和重新复核；请保留争议供人工确认')
        answers = _clone(data.get('answers'))
        self._ids(answers, [q['id'] for q in self.source['questions']])
        has_teacher_material = any(m['role'] == 'answer_key' for m in self.source['materials'])
        for a in answers:
            a['id'] = str(a['id'])
            a['answer'] = str(a.get('answer') or '')
            if not a['answer'].strip():
                a['confident'] = False
            a.pop('edited', None)
            a.pop('manual_confirmed', None)
            a['review_status'] = 'review'
            cached = next((old for old in (self.paper or {}).get('answers', []) if str(old['id']) == a['id']), {})
            prefer_saved = self.meta.get('prefer_saved_answers') or (not self.teacher_key.strip() and not self.source.get('reference_answers') and not has_teacher_material)
            if prefer_saved and (cached.get('review_status') == 'manual_confirmed' or cached.get('edited')):
                a.update(_clone(cached))
                a['review_status'] = 'manual_confirmed'
            teacher = next((r for r in self.source.get('reference_answers', []) if str(r['id']) == a['id']), None)
            if teacher:
                a['teacher_answer'] = teacher['answer']
                a['teacher_reference'] = _clone(teacher)
            a['teacher_reference_missing'] = bool(has_teacher_material and not self.source.get('reference_answers')
                and not self.teacher_key.strip() and not self.meta.get('prefer_saved_answers'))
        self.answers = answers
        self.published = self.paper_cache_hit = False
        self.draft_version += 1
        self.review = self.grades = None
        self.positions_submitted = False
        self.persist('answer-draft.json', {'source_version': self.source_version, 'draft_version': self.draft_version, 'answers': answers})
        self.persist(f'answer-draft-v{self.source_version}-{self.draft_version}.json', {'answers': answers})
        return {'draft_version': self.draft_version}

    def submit_review(self, data):
        self._version(data)
        if not self.answers:
            raise ValueError('没有待复核答案')
        reviews = _clone(data.get('reviews'))
        self._ids(reviews, [a['id'] for a in self.answers])
        for r in reviews:
            if r.get('status') not in ('approved', 'review'):
                raise ValueError('复核状态只能是 approved 或 review，模型不能代替老师确认')
        self.review = {'source_version': self.source_version, 'draft_version': self.draft_version, 'reviews': reviews}
        by_id = {str(r['id']): r for r in reviews}
        for a in self.answers:
            r = by_id[a['id']]
            if a.get('review_status') != 'manual_confirmed':
                a['review_status'] = r['status']
                if r['status'] == 'approved' and not _review_agrees(a, r):
                    a['review_status'] = r['status'] = 'review'
                    r['reason'] = str(r.get('reason', '')) + '；缺少独立解答或独立答案与草稿不一致'
                conflict = a.get('teacher_answer') is not None and not _review_agrees(
                    {**a, 'answer': a.get('ai_answer', a['answer'])}, {'independent_answer': a['teacher_answer']})
                missing_reference = a.get('teacher_reference_missing')
                if conflict or missing_reference or a.get('confident') is False or a.get('teacher_reference', {}).get('confident') is False:
                    a['review_status'] = 'review'
                    r['status'] = 'review'
                    r['reason'] = str(r.get('reason', '')) + ('；老师答案与 AI 答案存在冲突' if conflict
                        else '；标准答案图片未提取到参考答案，请人工确认' if missing_reference else '；标准答案把握不足')
            a['review_answer'] = str(r.get('independent_answer', ''))
            a['review_reason'] = str(r.get('reason', ''))
            a['review'] = _clone(r)
        self.grades = None
        self.positions_submitted = False
        self.persist('answer-review.json', self.review)
        self.persist(f'answer-review-v{self.source_version}-{self.draft_version}.json', self.review)
        self.publish_reviewed()
        self.persist('workflow-checkpoint.json', {'context_hash': self.context_hash, 'source': self.source,
            'answers': self.answers, 'review': self.review, 'source_version': self.source_version, 'draft_version': self.draft_version,
            'published_paper_id': self.paper['id'], 'published_paper_rev': self.paper.get('rev', 0)})
        return {'reviewed': True, 'unresolved': [a['id'] for a in self.answers if a['review_status'] == 'review']}

    def submit_grades(self, data):
        self._version(data)
        if self.mode != 'grade' or not self.review:
            raise ValueError('先完成当前版本答案复核，才可批改')
        rows = _clone(data.get('results'))
        self._ids(rows, [q['id'] for q in self.source['questions']])
        work = {m['page'] for m in self.source['materials'] if m['role'] == 'student_work'}
        key = {a['id']: a for a in self.answers}
        questions = {q['id']: q for q in self.source['questions']}
        result = []
        for r in rows:
            ident = str(r['id']); a = key[ident]
            if type(r.get('page')) is not int or r['page'] not in work:
                raise ValueError('学生答案页码必须属于学生作答材料')
            if r.get('answer_state') not in ('answered', 'blank', 'unreadable', 'unaligned'):
                raise ValueError('无效的作答状态')
            if r.get('verdict') not in ('correct', 'wrong', 'partial', None):
                raise ValueError('无效判分')
            reasons = []
            conf = _confidence(r.get('confidence'))
            if a['review_status'] == 'review': reasons.append('标准答案待复核')
            if r.get('key_doubt'): reasons.append('批改发现标准答案争议')
            if r['answer_state'] in ('unreadable', 'unaligned'): reasons.append('学生作答无法辨认或对齐')
            if r['answer_state'] == 'answered' and not str(r.get('student_answer', '')).strip(): reasons.append('未确认空白，不能按未作答判错')
            if r['answer_state'] == 'blank' and str(r.get('student_answer', '')).strip(): reasons.append('空白状态与作答矛盾')
            if conf < .7: reasons.append('批改把握不足')
            verdict = 'wrong' if r['answer_state'] == 'blank' and not reasons else r.get('verdict')
            q = {**questions[ident], **r, 'id': ident, 'standard_answer': a['answer'], 'knowledge': a.get('knowledge', ''),
                'grade_confidence': conf, 'parse_confidence': conf, 'reasons': reasons,
                'verdict': verdict, 'status': 'review' if reasons or not verdict else verdict,
                'draw': False, 'bbox_norm': None, 'bbox_px': None, 'bbox_ok': False,
                'position_source': 'ocr_blocks', 'overridden': False, 'error_type': r.get('error_type', ''),
                'position_note': '未定位到可靠的学生答案文字块，未落笔'}
            result.append(q)
        self.grades = result
        self.positions_submitted = False
        selections = [{'id': q['id'], 'page': q['page'], 'block_ids': q.get('answer_block_ids', [])}
                      for q in result if q.get('answer_block_ids')]
        if selections:
            from .positioning import apply_positions, save_proposal
            save_proposal(self.sid, {'positions': selections}, self.source_version, self.draft_version)
            try:
                apply_positions(self.grades, self.ocr_pages, self.pages, selections)
            except ValueError as exc:
                self.emit('grade', 'info', f'判分已保存，部分 OCR 位置需补充：{exc}', kind='activity')
        self.persist('grading.json', result)
        return {'graded': len(result)}

    async def ocr_page(self, data):
        page = data.get('page')
        if type(page) is not int or not 1 <= page <= len(self.pages):
            raise ValueError('OCR 页码无效')
        if page in self.ocr_pages:
            return _clone(self.ocr_pages[page])
        obj, error = {}, ''
        for attempt in range(3):
            try:
                content = await asyncio.to_thread(imaging.jpeg_bytes, self.pages[page - 1])
                obj = await ocr.recognize(self.cfg, content, f'page-{page}.jpg', purpose='rich_layout_recognition')
                break
            except ocr.OcrError as exc:
                error = str(exc)
                transient = any(x in error for x in ('429', '500', '502', '503', '504', 'Timeout', 'Connect', 'ReadError'))
                if not transient or attempt == 2: break
                await asyncio.sleep(.2 * (attempt + 1))
        self.persist(f'ocr-page-{page}-raw.json', obj or {'error': error})
        rows = obj.get('pages', [])
        complete = len(rows) == 1 and all(r.get('finish_reason') in ('stop', 'end_turn') for r in rows)
        blocks = ocr.parse_blocks(rows[0].get('text', '')) if complete else []
        for block in blocks: block['id'] = f'p{page}:{block["id"]}'
        result = {'page': page, 'blocks': blocks, 'complete': complete, 'error': error}
        self.ocr_pages[page] = result
        self.persist('ocr.json', self.ocr_pages)
        return _clone(result)

    def submit_positions(self, data):
        if not self.grades:
            raise ValueError('先批改再定位')
        work_pages = {m['page'] for m in self.source['materials'] if m['role'] == 'student_work'}
        if not work_pages <= set(self.ocr_pages):
            raise ValueError('必须先对全部学生作答页执行 OCR，再提交定位；OCR 失败可保留空定位')
        from .positioning import apply_positions, save_proposal
        save_proposal(self.sid, data, self.source_version, self.draft_version)
        result = apply_positions(self.grades, self.ocr_pages, self.pages, data.get('positions'))
        self.positions_submitted = True
        self.persist('verified.json', self.grades)
        return result

    async def authorize(self, data, tool_use_id, context):
        name = data.get('tool_name', '')
        role, aid = data.get('agent_type'), data.get('agent_id')
        stage = {'submit_source': 'parse', 'submit_draft': 'solve', 'submit_review': 'answer_review', 'submit_grades': 'grade', 'ocr_page': 'ocr', 'submit_positions': 'locate'}.get(name.removeprefix('mcp__workflow__'))
        if role in ('answer-reviewer', 'student-grader'):
            stage = 'answer_review' if role == 'answer-reviewer' else 'grade'
        if name == 'Agent':
            stage = 'answer_review' if data.get('tool_input', {}).get('subagent_type') == 'answer-reviewer' else 'grade'
        if stage: self.current_stage = stage
        self.emit(self.current_stage, 'info', f"{role or 'main'}: {name}", kind='hook', agent_type=role, agent_id=aid)
        child = bool(aid and role in ('answer-reviewer', 'student-grader'))
        suffix = name.removeprefix('mcp__workflow__')
        def deny(reason):
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}
        required = {'submit_review': 'answer-reviewer', 'submit_grades': 'student-grader'}
        if suffix in required and (not child or role != required[suffix]):
            return deny('此提交只能由指定的原生子代理执行')
        if role and role not in ('main', 'answer-reviewer', 'student-grader'):
            return deny('未知代理身份')
        if child and suffix not in ('get_state', 'read_page', 'submit_review' if role == 'answer-reviewer' else 'submit_grades'):
            return deny('子代理无权修改主代理阶段或调用其他工具')
        if name == 'Agent':
            if child or data.get('tool_input', {}).get('subagent_type') not in ('answer-reviewer', 'student-grader'):
                return deny('只能调用指定工作流子代理')
            return {}
        if not name.startswith('mcp__workflow__'):
            return deny('只允许任务作用域内工具')
        caller = aid if child else 'main'
        if suffix == 'read_page':
            args = data.get('tool_input', {})
            kind = 'reference' if 'reference' in args else 'page'
            index = args.get(kind)
            limit = len(self.references) if kind == 'reference' else len(self.pages)
            if type(index) is not int or not 1 <= index <= limit:
                return deny('页码无效')
            path = self.references[index - 1]['path'] if kind == 'reference' else self.pages[index - 1]
            try:
                with Image.open(path) as image: image.verify()
            except (OSError, ValueError):
                return deny('来源图不存在或无法读取，不能作为已阅读证据')
            if type(index) is int and 1 <= index <= limit:
                self.reads.setdefault(caller, set()).add((kind, index))
        if suffix == 'submit_source':
            needed = {('page', m['page']) for m in self.manifest}
            if not needed <= self.reads.get(caller, set()):
                return deny('主代理必须先读取全部材料页图')
        if self.source and suffix in required:
            if role == 'student-grader':
                needed = {('page', m['page']) for m in self.source['materials'] if m['role'] == 'student_work'}
            else:
                needed = {('page', p) for q in self.source['questions'] for p in q.get('source_pages', [])}
                needed |= {('page', m['page']) for m in self.source['materials'] if m['role'] == 'answer_key'}
                needed |= {('reference', p) for q in self.source['questions'] for p in q.get('reference_pages', [])}
            if not needed <= self.reads.get(caller, set()):
                return deny('子代理必须自己读取对应来源页图片，图片不会继承')
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow'}}

    def restore_checkpoint(self):
        path = store.sub_dir(self.sid) / 'workflow-checkpoint.json'
        if not self.meta.get('retry_of') or not path.exists(): return False
        try:
            checkpoint = json.loads(path.read_text())
            if checkpoint.get('context_hash') != self.context_hash: return False
            published = store.get_paper(checkpoint.get('published_paper_id', ''))
            if not published or published.get('rev', 0) != checkpoint.get('published_paper_rev'): return False
            self.paper = published
            self.references = published.get('reference_pages', [])
            self.submit_source(checkpoint['source'])
            self._ids(checkpoint['answers'], [q['id'] for q in self.source['questions']])
            if checkpoint['answers'] != published['answers']: return False
            self.answers = _clone(checkpoint['answers'])
            self.source_version = checkpoint['source_version']
            self.draft_version = checkpoint['draft_version']
            self.review = _clone(checkpoint['review'])
            self._version(self.review)
            self.published = True
            self.paper_cache_hit = True
            self.persist('answer-review.json', self.review)
            self.emit('answer_review', 'skip', '恢复已验证的复核检查点')
            return True
        except (ValueError, KeyError, TypeError): return False

    @store._serialized
    def publish_reviewed(self):
        if self.published: return
        if not self.review: raise ValueError('尚未复核')
        if self.paper:
            current = store.get_paper(self.paper['id'])
            if current and current.get('rev', 0) != self.paper.get('rev', 0):
                raise ValueError('试卷版本已被老师更新，请重试以读取最新答案')
        # Archive indices belong to the paper, never to a later upload's page numbers.
        references = [{**r, 'page': i + 1} for i, r in enumerate(self.references)]
        source_pages = {p for q in self.source['questions'] for p in q.get('source_pages', [])}
        uploaded_refs = {}
        for material in self.source['materials']:
            page = material['page']
            if material['role'] != 'student_work' or page in source_pages:
                index = len(references) + 1
                uploaded_refs[page] = index
                references.append({**material, 'upload_page': page, 'page': index,
                    'path': self.pages[page - 1], 'ocr': _clone(self.ocr_pages.get(page, {}))})
        canonical = _clone(self.source['questions'])
        for question in canonical:
            question['reference_pages'] = list(dict.fromkeys([
                *question.get('reference_pages', []),
                *(uploaded_refs[p] for p in question.get('source_pages', []))]))
            question['source_pages'] = []
            question.pop('source_page', None)
        pid = store.save_reviewed_paper(self.source.get('title', ''), canonical, self.answers,
            'teacher' if self.teacher_key.strip() or self.source.get('reference_answers') else 'ai',
            pid=(self.paper or {}).get('id'), reference_pages=references or None)
        self.paper = store.get_paper(pid)
        self.references = self.paper.get('reference_pages', [])
        if self.meta.get('paper_id') and self.meta.get('file_hash'):
            self.meta['file_key'] = store.workflow_file_key(self.meta['file_hash'], mode=self.mode,
                paper_id=self.meta['paper_id'], paper_rev=self.paper.get('rev', 0),
                **{k: self.meta.get(k) or '' for k in ('class_id', 'task_id', 'student_id')})
        self.published = True
        self.emit('paper', 'done', '已保存复核后的试卷与来源图')

    def finalize(self):
        from .pipeline import render_annotations, summarize
        if not self.source or not self.answers or not self.review:
            raise AgentError('工作流未完成当前版本的答案复核，模型最终文本不作为完成凭据')
        if self.mode == 'grade' and (self.grades is None or not self.positions_submitted):
            raise AgentError('工作流未完成批改或答案块定位提交')
        self.publish_reviewed()
        paper = self.paper
        pid = paper['id']
        questions = self.grades if self.mode == 'grade' else []
        who = self.source
        student_id = self.meta.get('student_id')
        if not student_id and self.meta.get('class_id'):
            student_id = store.match_student(self.meta['class_id'], who.get('student_name', ''), who.get('student_no', ''))
        result = {**{k: self.meta.get(k) for k in ('class_id', 'task_id', 'filename', 'file_key', 'created_at', 'inputs', 'input_version')},
            'id': self.sid, 'status': 'done', 'mode': self.mode, 'workflow_version': 2,
            'position_method': 'ocr_blocks', 'annotation_version': ANNOTATION_VERSION, 'paper_id': pid,
            'paper_title': paper.get('title', ''), 'paper_rev': paper.get('rev', 0), 'paper_cache_hit': self.paper_cache_hit,
            'paper_similarity': 0, 'answer_source': paper.get('answer_source'), 'model': self.cfg.get('model'),
            'ocr_used': bool(self.ocr_pages), 'ocr_mode': 'deepseek_ocr', 'effort': 'high',
            'student_id': student_id, 'student_match': 'manual' if self.meta.get('student_id') else 'auto' if student_id else None,
            'detected_name': who.get('student_name', ''), 'detected_no': who.get('student_no', ''),
            'materials': self.source['materials'], 'graded_pages': [m['page'] for m in self.source['materials'] if m['role'] == 'student_work'],
            'page_count': len(self.pages), 'questions': questions, 'summary': summarize(questions)}
        if self.mode == 'grade': render_annotations(self.sid, result)
        store.save_result(self.sid, result)
        if self.meta.get('file_key'): store.index_key(self.meta['file_key'], self.sid)
        store.bump_paper(pid)
        if self.meta.get('task_id'):
            roster = store.roster()
            task = next((t for t in roster['tasks'] if t['id'] == self.meta['task_id']), None)
            if task and not task.get('paper_id'):
                task.update(paper_id=pid, paper_title=paper.get('title', ''))
                store.save_roster(roster)
        old = store.get_result(self.meta['replaces']) if self.meta.get('replaces') else None
        if old:
            old['superseded'] = True
            store.save_result(old['id'], old)
        return result


async def run_workflow(sid, cfg, meta, emit, teacher_key=''):
    start = time.monotonic()
    inputs = meta.get('inputs') or [{'original': meta['original'], 'filename': meta['filename'], 'role': 'auto'}]
    emit('preprocess', 'start', '转换全部材料为逐页图片')
    pages, manifest = await asyncio.to_thread(materials.to_pages, inputs, store.sub_dir(sid))
    emit('preprocess', 'done', f'共 {len(pages)} 页', page_count=len(pages))
    state = WorkflowState(sid, cfg, {**meta, 'inputs': inputs}, pages, manifest, emit, teacher_key)
    if ocr.available(cfg):
        emit('ocr', 'start', '先识别全部上传材料，文字块与坐标共享给后续 Agent')
        limit = asyncio.Semaphore(3)
        async def recognize(page):
            async with limit:
                result = await state.ocr_page({'page': page})
                emit('ocr', 'info', f"第 {page} 页：{len(result['blocks'])} 个文字块" +
                     ('' if result['complete'] else '，识别不完整，后续结合原图处理'), kind='activity')
        await asyncio.gather(*(recognize(page) for page in range(1, len(pages) + 1)))
        emit('ocr', 'done', f"已处理 {len(pages)} 页，共 {sum(len(p['blocks']) for p in state.ocr_pages.values())} 个文字块")
    else:
        emit('ocr', 'skip', '当前建库模型未配置 OCR，后续 Agent 读取原图')
    state.restore_checkpoint()
    emit('parse', 'start', '主代理阅读全部材料并提取题目')
    await run_workflow_agent(cfg, state)
    if state.mode == 'prepare':
        for stage in ('grade', 'locate'):
            emit(stage, 'skip', '准备试卷模式无需学生批改与定位')
    emit('annotate', 'start', '保存结果并生成批改图片')
    result = await asyncio.to_thread(state.finalize)
    result['elapsed_s'] = round(time.monotonic() - start, 1)
    store.save_result(sid, result)
    emit('annotate', 'done', '工作流完成，已保存试卷与批改结果')
    return result
