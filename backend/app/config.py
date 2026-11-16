"""运行配置：数据目录与模型设置（模型名 / Key / URL 由前端填写后持久化）。"""
import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
PAPERS_DIR = DATA_DIR / "papers"
SUBS_DIR = DATA_DIR / "submissions"
WORK_DIR = DATA_DIR / "agent-workdir"
SETTINGS_FILE = DATA_DIR / "settings.json"
for d in (PAPERS_DIR, SUBS_DIR, WORK_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 同一套试卷的判定阈值（题干文本相似度）
PAPER_MATCH_THRESHOLD = float(os.environ.get("PAPER_MATCH_THRESHOLD", "0.85"))
# 发送给模型的整页图片长边上限（像素）
MODEL_IMAGE_MAX_SIDE = int(os.environ.get("MODEL_IMAGE_MAX_SIDE", "2000"))
AGENT_TIMEOUT_S = int(os.environ.get("AGENT_TIMEOUT_S", "1800"))

DEFAULTS = {
    "model": os.environ.get("LLM_MODEL", "deepseek-flash"),  # DeepSeek V4.1 Flash 的官方模型 ID
    "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/anthropic"),
    "api_key": os.environ.get("LLM_API_KEY", ""),
    "agentic_zoom": True,  # 历史视觉复核配置；新工作流通过受限 read_page 工具看图。
    "use_ocr": True,       # 历史配置；新批改统一使用 OCR，单独建库不执行 OCR。
}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    for k in ("model", "base_url", "agentic_zoom", "use_ocr"):
        if k in patch and patch[k] is not None:
            s[k] = patch[k].strip() if isinstance(patch[k], str) else patch[k]
    # Key 留空表示不修改
    if patch.get("api_key"):
        s["api_key"] = patch["api_key"].strip()
    s["base_url"] = s["base_url"].rstrip("/")
    if s["base_url"].endswith("/v1"):  # Claude Code 会自己拼 /v1/messages，带上会变成 /v1/v1/messages
        s["base_url"] = s["base_url"][:-3]
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass
    return s


def public_settings(s: dict) -> dict:
    key = s.get("api_key", "")
    masked = (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("已设置" if key else "")
    from . import ocr
    return {"model": s["model"], "base_url": s["base_url"], "api_key_masked": masked,
            "has_key": bool(key), "agentic_zoom": bool(s.get("agentic_zoom", True)),
            "use_ocr": bool(s.get("use_ocr", True)), "is_deepseek": bool(ocr.endpoint(s)),
            "ocr_active": ocr.enabled(s), "ocr_available": ocr.available(s), "effort": "high"}
