"""本地模拟的 Anthropic 兼容接口，仅用于在没有真实 Key 的情况下跑通整条流水线。

它按系统提示词里的角色返回与 make_sample.py 那张图对应的固定 JSON，并记录每次请求收到了几张图片，
用来验证「图片确实经由 Agent SDK 送到了模型接口」以及「工具调用往返」。
"""
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
LOG = []
STUDENT = {"1": "20", "2": "8", "3": "42", "4": "0.75", "5": "9"}
# 模拟接口看不懂图片，所以按解析请求的次序轮流扮演几位学生，方便演示班级统计
PROFILES = [("张小明", {"1": "20", "2": "8", "3": "42", "4": "0.75", "5": "9"}),
            ("李华", {"1": "20", "2": "9", "3": "48", "4": "3/4", "5": "9"}),
            ("王芳", {"1": "20", "2": "8", "3": "42", "4": "2/6", "5": ""}),
            ("赵磊", {"1": "20", "2": "9", "3": "42", "4": "3/4", "5": "9"})]
KP = {"1": "两位数加法", "2": "两位数减法", "3": "表内乘法", "4": "分数加法", "5": "表内除法"}
STATE = {"n": 0, "cur": STUDENT}


def store_norm(s):
    return str(s).replace(" ", "")


def bbox(i):  # 与 make_sample 的版式对应，0–1000 归一化
    y = 300 + i * 220
    return [round(540 / 1.2), round((y - 10) / 1.6), round(760 / 1.2), round((y + 58) / 1.6)]


def reply_for(body):
    dump = json.dumps(body.get("system", ""), ensure_ascii=False)
    if "你负责识别上传材料的用途" in dump:
        return {"text": json.dumps({"role": "student_work", "confidence": .99})}
    if "你是一位负责整份作业的批改老师" in dump:
        return single_submission_reply(body)
    msgs = body.get("messages", [])
    imgs = sum(1 for m in msgs if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "image")
    tool_imgs = sum(1 for m in msgs if isinstance(m["content"], list) for b in m["content"]
                    if b.get("type") == "tool_result" and isinstance(b.get("content"), list)
                    for c in b["content"] if c.get("type") == "image")
    for m in msgs:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    print("TOOL_RESULT", json.dumps(b, ensure_ascii=False)[:300], flush=True)
    print("TOOLS", [t["name"] for t in body.get("tools", [])], flush=True)
    has_tool_result = any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"])
                          for m in msgs)
    role = next((r for r in ("学情分析", "试卷解析", "标准答案", "批改", "复核", "连通性") if f"角色：{r}" in dump or r in dump[:40]), "?")
    LOG.append({"role": role, "images": imgs, "tool_result_images": tool_imgs, "tools": len(body.get("tools", []))})
    if role == "学情分析":
        if "Agent（班级）" in dump:
            return {"text": json.dumps({"summary": "（模拟）全班整体掌握较好，减法和分数加法是共性薄弱点。",
                "strengths": ["两位数加法全员正确"], "weaknesses": ["第 2 题两位数减法错误率高，典型错误答案为 8"],
                "lecture_plan": [{"focus": "第 2 题", "how": "用数轴演示退位，再做两道变式"}],
                "tier_suggestions": {"提高组": "增加分数混合运算", "巩固组": "减法口算每日 5 题", "基础组": "从 20 以内退位减法补起"},
                "follow_up": ["下次作业加入 3 道退位减法"]}, ensure_ascii=False)}
        return {"text": json.dumps({"comment": "（模拟）基础扎实，计算细心程度有待提高。", "strengths": ["加法、乘法稳定"],
            "weaknesses": ["两位数减法"], "suggestions": ["每天 5 道退位减法口算"],
            "practice": [{"knowledge": "两位数减法", "question": "17 - 9 = ?", "answer": "8"},
                         {"knowledge": "两位数减法", "question": "23 - 7 = ?", "answer": "16"},
                         {"knowledge": "两位数减法", "question": "14 - 6 = ?", "answer": "8"}]}, ensure_ascii=False)}
    if role == "试卷解析":
        name, STATE["cur"] = PROFILES[STATE["n"] % len(PROFILES)]
        STATE["n"] += 1
        STUDENT = STATE["cur"]
        qs = [{"id": k, "type": "calc", "question": q, "student_answer": STUDENT[k],
               "answer_state": "answered" if STUDENT[k] else "blank",
               "answer_bbox": bbox(i), "confidence": 0.95}
              for i, (k, q) in enumerate([("1", "12 + 8 ="), ("2", "15 - 6 ="), ("3", "7 x 6 ="),
                                          ("4", "1/2 + 1/4 ="), ("5", "81 / 9 =")])]
        if "answer_block_ids" in dump:
            # OCR mock emits a title, then a stem and (when answered) an answer
            # block per question. Use the same IDs as the real OCR parser.
            block_number = 2
            for q in qs:
                answered = bool(q["student_answer"])
                q["answer_block_ids"] = [f"b{block_number + 1}"] if answered else []
                q["question_block_ids"] = [f"b{block_number}"]
                q["question_bbox"] = None
                q["answer_bbox"] = None
                block_number += 2 if answered else 1
        result = {"title": "Math Quiz - Unit 3", "student_name": name, "questions": qs}
        if "reference_answers" in dump:
            manifest = next((json.loads(b["text"].split("\n", 1)[1]) for message in msgs
                             if isinstance(message["content"], list) for b in message["content"]
                             if b.get("type") == "text" and b.get("text", "").startswith("这一组文件属于同一次批改。")), [])
            roles = [{"page": m["page"], "role": m["requested_role"] if m["requested_role"] != "auto" else "student_work",
                      "confidence": .99} for m in manifest]
            work = next((m["page"] for m in roles if m["role"] == "student_work"), 1)
            for question in qs:
                question["page"] = work
            result.update(materials=roles, reference_answers=[{"id": k, "answer": a} for k, a in
                          [("1", "20"), ("2", "9"), ("3", "42"), ("4", "3/4"), ("5", "9")]]
                          if any(m["role"] == "answer_key" for m in roles) else [])
        return {"text": json.dumps(result, ensure_ascii=False)}
    if role == "标准答案":
        ans = [("1", "20"), ("2", "9"), ("3", "42"), ("4", "3/4"), ("5", "9")]
        return {"text": json.dumps({"answers": [{"id": k, "answer": v, "accepted": [], "explanation": "",
                                                 "knowledge": KP[k], "confident": True} for k, v in ans]},
                                   ensure_ascii=False)}
    if role == "批改":
        items = next(json.loads(b["text"]) for b in msgs[0]["content"]
                     if b.get("type") == "text" and b["text"].lstrip().startswith("["))
        ok = lambda it: store_norm(it["student_answer"]) in ("0.75", "3/4")
        res = [{"id": it["id"], "verdict": "correct" if ok(it) else "wrong", "error_type": "" if ok(it) else "计算错误",
                "reason": "与标准答案等价" if ok(it) else "计算错误", "confidence": 0.97} for it in items]
        return {"text": "```json\n" + json.dumps({"results": res}, ensure_ascii=False) + "\n```"}
    if role == "复核":
        if body.get("tools") and not has_tool_result:
            name = next(t["name"] for t in body["tools"] if t["name"].startswith("mcp__"))
            return {"tool": (name, {"page": 1, "x1": 400, "y1": 700, "x2": 700, "y2": 800})}
        cur = STATE["cur"]
        bad = lambda k: k == "5" and cur is PROFILES[0][1]  # 只对第一位学生的第 5 题制造一次"复核不一致"
        revs = [{"id": k, "read_answer": "4" if bad(k) else v, "matches": not bad(k), "bbox_ok": True, "note": ""}
                for k, v in cur.items()]
        return {"text": json.dumps({"reviews": revs})}
    return {"text": "OK"}


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/v1/messages")
async def messages(req: Request):
    body = await req.json()
    r = reply_for(body)
    msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model"), "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
    if "tool" in r:
        block = {"type": "tool_use", "id": f"toolu_mock{len(LOG)}", "name": r["tool"][0], "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(r["tool"][1])}
        stop = "tool_use"
    else:
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": r["text"]}
        stop = "end_turn"
    if not body.get("stream"):
        block = dict(block, **({"input": r["tool"][1]} if "tool" in r else {"text": r["text"]}))
        return dict(msg, content=[block], stop_reason=stop)

    def gen():
        yield sse("message_start", {"type": "message_start", "message": msg})
        yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block})
        yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                    "usage": {"output_tokens": 20}})
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/tools/ocr")
async def ocr(req: Request):
    """模拟 DeepSeek OCR：返回示例试卷的文字块与 0–1000 坐标。"""
    form = await req.form()
    purpose = form.get("purpose")
    name, answers = PROFILES[STATE["n"] % len(PROFILES)]  # 紧接着的解析请求会轮到同一位学生
    T = lambda t, b: f"<｜ref｜>text<｜/ref｜><｜box｜>[[{b[0]}, {b[1]}, {b[2]}, {b[3]}]]<｜/box｜>\n{t}\n"
    text = "<｜ref｜>title<｜/ref｜><｜box｜>[[300, 40, 700, 75]]<｜/box｜>\n# Math Quiz - Unit 3\n"
    for i, (k, stem) in enumerate([("1", "12 + 8 ="), ("2", "15 - 6 ="), ("3", "7 x 6 ="), ("4", "1/2 + 1/4 ="), ("5", "81 / 9 =")]):
        y = 300 + i * 220
        text += T(f"{k}. {stem}", [100, round(y / 1.6), 400, round((y + 50) / 1.6)])
        if answers[k]:
            seen = "g" if (name == "李华" and k == "5") else answers[k]  # 故意制造一次 OCR 与抄录不一致
            b = bbox(i)
            text += T(seen, [b[0] + 8, b[1] + 4, b[2] - 60, b[3] - 4])
    LOG.append({"role": "OCR", "purpose": purpose, "bytes": len(await form["file"].read())})
    if os.environ.get('WORKFLOW_MOCK_SINGLE_QUESTION'):
        text = T('2', [300, 300, 400, 350])
    if purpose == "text_recognition":
        text = "1. 20\n2. 9\n3. 42\n4. 3/4\n5. 9"
    return {"id": "ocr-mock", "object": "ocr.result", "created": 0, "purpose": purpose, "page_count": 1,
            "pages": [{"index": 0, "text": text, "finish_reason": "stop"}],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}


@app.get("/log")
def log():
    return LOG


# One-shot submission fixture; every image arrives directly in the user message.
def single_submission_reply(body):
    blocks = body['messages'][0]['content']
    context = json.loads(blocks[0]['text'])
    selected = context.get('selected_paper')
    materials = context['materials']
    work = next((m['page'] for m in materials if m['classified_role'] == 'student_work'), None)
    source = next((m['page'] for m in materials if m['classified_role'] == 'question_paper'), work or 1)
    name, student = PROFILES[STATE['n'] % len(PROFILES)]
    STATE['n'] += 1
    questions = [{'id': str(i), 'type': 'calc', 'question': q, 'source_pages': [source]}
        for i, q in enumerate(('12+8=', '15-6=', '7*6=', '1/2+1/4=', '81/9='), 1)]
    standard = {'1': '20', '2': '9', '3': '42', '4': '.75', '5': '9'}
    if selected:
        questions = selected['questions']
        standard.update({a['id']: a['answer'] for a in selected.get('answers', [])})
    recognized = {}
    for block in blocks:
        text = block.get('text', '')
        if text.startswith(f'上传第 {work} 页；OCR'):
            recognized = json.loads(text.split('\n', 1)[1])
    results = []
    if context['mode'] == 'grade':
        for question in questions:
            ident = question['id']; answer = student.get(ident, '')
            ids = [b['id'] for b in recognized.get('blocks', []) if b.get('text', '').strip() == answer]
            results.append({'id': ident, 'page': work, 'student_answer': answer,
                'answer_state': 'answered' if answer else 'blank',
                'verdict': 'correct' if store_norm(answer) == store_norm(standard[ident]) else 'wrong',
                'confidence': .99, 'answer_block_ids': ids})
    LOG.append({'role': 'single-agent', 'images': sum(b['type'] == 'image' for b in blocks), 'tools': len(body.get('tools', []))})
    return {'text': json.dumps({'title': '模拟试卷', 'student_name': name, 'questions': [] if selected else questions,
        'answers': [] if selected else [{'id': q['id'], 'answer': standard[q['id']], 'confident': True,
                                        'knowledge': KP.get(q['id'], '')} for q in questions],
        'results': results}, ensure_ascii=False)}
