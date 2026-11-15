"""不依赖模型的单元测试：python -m pytest tests/test_core.py（或直接 python tests/test_core.py）。"""
import os
import sys
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import imaging, ocr, store  # noqa: E402


def Q(i, text):
    return {"id": str(i), "type": "calc", "question": text, "page": 1}


def test_answer_normalization():
    assert store.norm_answer("1.5") != store.norm_answer("15")
    assert store.norm_answer("1,2") != store.norm_answer("12")
    assert store.norm_answer(" ２０。") == store.norm_answer("20")
    assert store.norm_answer("A.") == store.norm_answer("a")


def test_bbox():
    assert imaging.canon_bbox([0.1, 0.2, 0.3, 0.4]) == [100, 200, 300, 400]
    assert imaging.canon_bbox([[10, 20, 30, 40]]) == [10, 20, 30, 40]
    assert imaging.canon_bbox([100, 200, 1500, 400]) is None      # 像素坐标不能硬截断
    assert imaging.canon_bbox(None) is None and imaging.canon_bbox("x") is None
    assert imaging.norm_to_px([500, 500, 600, 600], 2000, 1000) == [1000, 500, 1200, 600]


def test_ocr_parse():
    t = ("<｜ref｜>Title<｜/ref｜><｜box｜>[[52, 40, 948, 96]]<｜/box｜>\n"
         "<|ref|>text<|/ref|><|box|>[[100,200,300,240]]<|/box|>\n1. 12 + 8 =\n")
    b = ocr.parse_blocks(t)
    assert [x["text"] for x in b] == ["Title", "1. 12 + 8 ="] and b[0]["box"] == [52, 40, 948, 96]
    assert ocr.endpoint({"base_url": "https://api.deepseek.com/anthropic"}) == "https://api.deepseek.com/v1/tools/ocr"
    assert ocr.endpoint({"base_url": "https://deepseek.com.evil.io/x"}) is None
    assert ocr.endpoint({"base_url": "https://api.example.com"}) is None
    assert len(ocr.blocks_in(b, [90, 190, 320, 250], min_cover=0.7)) == 1


def test_paper_matching():
    a = [Q(1, "12 + 8 = ?"), Q(2, "15 - 6 = ?"), Q(3, "7 × 6 = ?"), Q(4, "81 ÷ 9 = ?")]
    pid = store.save_paper("单元测验", a, [{"id": q["id"], "answer": "x"} for q in a], "ai")
    # 同一套试卷，模型第二次读出来的写法略有不同 → 命中
    again = [Q(1, "12+8=？"), Q(2, "15 - 6 ="), Q(3, "7 × 6 = ?"), Q(4, "81 ÷ 9 =")]
    assert store.match_paper(again)[0] == pid
    assert set(store.align_answers(again, store.get_paper(pid))) == {"1", "2", "3", "4"}
    # 只改了数字的 B 卷 → 不能命中，否则会套错答案
    b = [Q(1, "13 + 8 = ?"), Q(2, "15 - 7 = ?"), Q(3, "7 × 8 = ?"), Q(4, "72 ÷ 9 = ?")]
    assert store.match_paper(b)[0] is None
    # 题干全空（答题卡）→ 不匹配
    assert store.match_paper([Q(i, "") for i in range(1, 5)])[0] is None
    # 单题数字不同 → 这道题不套用答案
    mixed = [Q(1, "12 + 8 = ?"), Q(2, "15 - 9 = ?")]
    assert set(store.align_answers(mixed, store.get_paper(pid))) == {"1"}


def test_short_numeric_variants_are_distinct_papers():
    a = [Q(1, "请计算：12 + 8 = ? 写出计算结果。")]
    b = [Q(1, "请计算：12 + 9 = ? 写出计算结果。")]
    store.save_paper("短测验", a, [{"id": "1", "answer": "20"}], "ai")
    assert not store.same_paper(a, b)
    assert store.match_paper(b)[0] is None
    assert store.same_paper(a, [Q(1, "请计算：１２＋８＝？ 写出计算结果。")])


def test_long_numeric_variants_do_not_share_answers_for_changed_problem():
    a = [Q(i, f"请计算：{20 + i} + 8 = ? 写出计算结果。") for i in range(12)]
    b = [dict(q) for q in a]
    b[0]["question"] = "请计算：20 + 9 = ? 写出计算结果。"
    store.save_paper("长测验", a, [], "ai")
    assert not store.same_paper(a, b)
    assert store.match_paper(b)[0] is None


def test_numeric_check_also_applies_to_normalized_fingerprint_hits():
    a = [Q(1, "请计算：1.5 + 2 = ? 写出计算结果。")]
    b = [Q(1, "请计算：15 + 2 = ? 写出计算结果。")]
    assert store.fingerprint(a) == store.fingerprint(b)
    store.save_paper("小数练习", a, [], "ai")
    assert store.match_paper(b)[0] is None


def test_answer_edit_only_marks_changed():
    a = [Q(1, "1 + 1 = ?"), Q(2, "2 + 2 = ?"), Q(3, "3 + 3 = ?")]
    pid = store.save_paper("t", a, [{"id": "1", "answer": "2", "confident": False},
                                     {"id": "2", "answer": "5", "confident": False},
                                     {"id": "3", "answer": "6", "confident": True}], "ai")
    rev = store.get_paper(pid)["rev"]
    same = [{"id": "1", "answer": "2"}, {"id": "2", "answer": "5"}, {"id": "3", "answer": "6"}]
    assert store.update_answers(pid, same)["rev"] == rev                 # 没改动不涨版本
    p = store.update_answers(pid, [{"id": "1", "answer": "2"}, {"id": "2", "answer": "4"}, {"id": "zz", "answer": "?"}])
    by = {x["id"]: x for x in p["answers"]}
    assert p["rev"] == rev + 1 and p["answer_source"] == "teacher"
    assert by["2"]["edited"] and not by["1"].get("edited") and by["1"]["confident"] is False and "zz" not in by


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
