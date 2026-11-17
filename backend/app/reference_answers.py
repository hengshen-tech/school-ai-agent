"""Validate Agent-provided teacher IDs; never infer or normalize question labels."""
import copy
def validate_agent_references(questions, references, reported_unaligned=None):
    ids = {str(q['id']) for q in questions}
    aligned, unaligned, issues, blocked, warnings = {}, [], {}, set(), []

    def reject(row, reason, affected=()):
        unaligned.append({'reference': copy.deepcopy(row), 'reason': reason})
        if reason not in warnings: warnings.append(reason)
        for ident in affected:
            issues[ident] = reason
            blocked.add(ident)

    if reported_unaligned:
        entries = reported_unaligned if isinstance(reported_unaligned, list) else [reported_unaligned]
        for row in entries:
            detail = row if isinstance(row, dict) else {}
            original_id = detail.get('original_id') or detail.get('id') or '（缺失）'
            reason = f"Agent 未能确认老师答案题号 {original_id} 的对应关系：{detail.get('reason') or '需人工确认'}"
            candidates = detail.get('candidate_ids', [])
            affected = [str(i).strip() for i in candidates if str(i).strip() in ids] if isinstance(candidates, list) else []
            reject(row, reason, affected)
    if not isinstance(references, list):
        reject(references, '老师参考答案格式不正确，已保留待确认；其他题继续批改')
        references = []
    for original in references:
        if not isinstance(original, dict):
            reject(original, '老师参考答案条目缺少有效题号，已保留待确认')
            continue
        row = copy.deepcopy(original)
        raw_id = str(row.get('id') if row.get('id') is not None else '').strip()
        if raw_id not in ids:
            reject(row, f'Agent 返回的老师答案题号 {raw_id or "（缺失）"} 未对应内部题号，已保留待确认；其他题继续批改')
            continue
        ident = raw_id
        row['id'] = ident
        row['answer'] = str(row.get('answer') if row.get('answer') is not None else '')
        if ident in blocked:
            reject(row, issues[ident])
            continue
        previous = aligned.get(ident)
        if previous:
            if previous['answer'].strip() != row['answer'].strip():
                reason = f'老师答案题号 {raw_id} 有多份不同答案，相关题待确认'
                reject(aligned.pop(ident), reason, [ident])
                reject(row, reason)
            elif row.get('confident') is False:
                previous['confident'] = False
            continue
        if not row['answer'].strip() or row.get('confident') is False:
            row['confident'] = False
            issues[ident] = f'老师答案题号 {raw_id} 的内容缺失或无法辨认'
            if issues[ident] not in warnings: warnings.append(issues[ident])
        aligned[ident] = row
    return {'aligned': [row for ident, row in aligned.items() if ident not in blocked],
        'unaligned': unaligned, 'issues': issues, 'warnings': warnings}
