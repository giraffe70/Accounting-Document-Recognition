"""FastAPI 入口（參考 research_log_agent）：static 掛載、模型/設定端點、批次 job 輪詢。"""
import logging
import threading
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import json

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app import validation
from app.core import activity, doc_types, model_factory, pipeline, settings_store
from app.db.models import ReviewStatus, VoucherRecord
from app.db.session import init_db
from app.models.schemas import ConfigUpdate, Voucher
from app.services import (accounts, auth, backup, classify, database_view, importdecl, journal,
                          reports, review, sysconfig, taxid)

_LOG_FMT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
# 同時寫檔：辨識失敗的 traceback 只印在主控台，視窗一關就查不到了（滾動 2MB × 5）
_fh = RotatingFileHandler(config.LOG_FILE, maxBytes=2 * 1024 * 1024,
                          backupCount=5, encoding="utf-8")
_fh.setFormatter(logging.Formatter(_LOG_FMT))
logging.getLogger().addHandler(_fh)
logger = logging.getLogger("pdf_scan")

app = FastAPI(title="憑證辨識系統 API")
init_db()
auth.ensure_seed_users()
validation.init_validation()
app.include_router(validation.router)
app.include_router(database_view.router)

_PUBLIC_API = {"/api/login", "/api/logout", "/api/me", "/api/health"}


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """基本資安：/api 需登入；核定/退回限會計師、科目表儲存限管理員。"""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API:
        user = auth.get_user_by_token(request.cookies.get("session"))
        if not user:
            return JSONResponse({"detail": "未登入"}, status_code=401)
        role = user["role"]
        if role != "admin":
            if path.endswith("/approve") or path.endswith("/reject"):
                if role != "accountant":
                    return JSONResponse({"detail": "需會計師權限"}, status_code=403)
            elif path == "/api/accounts" and request.method == "POST":
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path.startswith("/api/backup"):
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path.startswith("/api/sysconfig"):
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path.endswith("/dev-note") or path == "/api/feedback":
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
    return await call_next(request)


# ── 登入 / 角色 ──────────────────────────────────────────────────
@app.post("/api/login")
def api_login(body: dict = Body(...)):
    user = auth.authenticate(body.get("username", ""), body.get("password", ""))
    if not user:
        return JSONResponse({"detail": "帳號或密碼錯誤"}, status_code=401)
    resp = JSONResponse(user)
    resp.set_cookie("session", auth.create_token(user), httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    auth.logout(request.cookies.get("session"))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    user = auth.get_user_by_token(request.cookies.get("session"))
    if not user:
        return JSONResponse({"detail": "未登入"}, status_code=401)
    return user

# 單一批次工作狀態（骨架：一次一件，沿用 research_log_agent 的 job/poll 模式）
_job = {"id": None, "status": "idle", "done": 0, "total": 0, "current": "", "results": [], "errors": []}
_job_lock = threading.Lock()

# 內容提取批次工作（獨立於匯入 job，避免衝突）
_ejob = {"id": None, "status": "idle", "done": 0, "total": 0, "current": "", "results": [], "errors": []}
_ejob_lock = threading.Lock()


# ── 健康檢查與模型清單 ──────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/models")
def api_models():
    """給前端設定 modal：各階段可選模型（地端 Ollama + 已設金鑰的雲端）。"""
    return {
        "ocr": model_factory.list_ocr_providers(),
        "ocr_status": model_factory.ocr_status(),
        "structure": model_factory.list_models(kind="vlm"),
        "classify": model_factory.list_models(kind="llm"),
        "text": model_factory.list_models(kind="text"),
    }


@app.get("/api/engine-status")
def api_engine_status():
    """狀態列：三階段（OCR／VLM／LLM）目前用哪個引擎、是否執行中、地端模型佔用。

    地端模型單張數十秒，使用者需要知道現在卡在哪一段；Ollama 部分沿用
    research_log_agent 的 /api/ps 代理（VRAM、GPU/CPU 佔比、卸載倒數）。
    """
    settings = settings_store.get_settings()
    act = activity.snapshot()
    ps = _ollama_ps()
    ocr_ok = model_factory.ocr_status()

    def _stage(key: str) -> dict:
        setting = settings.get(key, "")
        provider, model = settings_store.parse_setting(setting) if key != "ocr" \
            else settings_store.parse_setting(setting, ("paddle", "pp-ocrv5"))
        info = dict(act.get(key) or {})
        info.update({
            "key": key, "label": activity.STAGE_LABEL[key],
            "setting": setting, "provider": provider, "model": model,
            "local": provider in ("paddle", "ollama"),
        })
        if key == "ocr":
            info["available"] = ocr_ok["available"]
            info["detail"] = ocr_ok["detail"]
        else:
            info["available"] = True
            info["loaded"] = ps["by_name"].get(model)     # None＝未載入（閒置卸載）
            if not ps["ok"]:
                info["available"] = provider != "ollama"
                info["detail"] = ps.get("error", "")
        return info

    return {"stages": [_stage(k) for k in activity.STAGES], "ollama_ok": ps["ok"],
            "ollama_error": ps.get("error", ""), "ollama_stale": ps.get("stale", False)}


_PS_CACHE: dict = {"at": 0.0, "data": None}
_PS_CACHE_TTL = 30.0


def _ollama_ps() -> dict:
    """代理 Ollama /api/ps：目前載入中的模型與 VRAM／GPU 佔比。

    推論進行中時 Ollama 回應會變慢，逾時太短會讓狀態列在「最該顯示執行中」的時候
    誤報未連線；故逾時放寬，且單次失敗先沿用 30 秒內的快取（標記 stale）而非直接報錯。
    """
    import time

    import requests

    base = sysconfig.get("llm", "ollama_base_url") or config.OLLAMA_BASE_URL
    try:
        data = requests.get(f"{base}/api/ps", timeout=8).json()
    except Exception as exc:  # noqa: BLE001
        cached = _PS_CACHE["data"]
        if cached and time.time() - _PS_CACHE["at"] < _PS_CACHE_TTL:
            return {**cached, "stale": True}
        return {"ok": False, "by_name": {}, "base": base,
                "error": f"無法連線 Ollama（{base}）：{exc}"}
    by_name = {}
    for m in data.get("models", []):
        size, vram = m.get("size", 0) or 0, m.get("size_vram", 0) or 0
        if size and vram >= size:
            proc = "GPU 100%"
        elif vram == 0:
            proc = "CPU 100%"
        elif size:
            proc = f"GPU {round(vram / size * 100)}%／CPU {round((1 - vram / size) * 100)}%"
        else:
            proc = "—"
        det = m.get("details") or {}
        by_name[m.get("name", "")] = {
            "size_gb": round(size / 1e9, 2), "vram_gb": round(vram / 1e9, 2),
            "processor": proc, "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"), "expires_at": m.get("expires_at"),
        }
    result = {"ok": True, "by_name": by_name, "base": base}
    _PS_CACHE.update({"at": time.time(), "data": result})
    return result


@app.get("/api/doc-types")
def api_doc_types():
    """各單據類型的欄位契約，供前端動態渲染審核欄位。"""
    return {"types": {k: {"label": v["label"], "fields": v["fields"],
                          "has_items": v.get("has_items", False), "item_fields": v["item_fields"],
                          "has_entries": v.get("has_entries", False), "entry_fields": v["entry_fields"]}
                      for k, v in doc_types.DOC_TYPES.items()}}


@app.get("/api/companies")
def api_companies():
    return {"companies": taxid.load_companies()}


@app.get("/api/accounts")
def api_get_accounts(company: str = ""):
    """該公司的科目表（各公司科目代號不同；未自訂者回退通用表）。"""
    return {"accounts": accounts.load_accounts(company), "company": company,
            "with_chart": accounts.companies_with_chart()}


@app.post("/api/accounts")
def api_save_accounts(payload: dict = Body(...)):
    company = str(payload.get("company", "") or "")
    return {"accounts": accounts.save_accounts(payload.get("accounts", []), company),
            "company": company, "with_chart": accounts.companies_with_chart()}


@app.post("/api/taxid/check")
def api_taxid_check(voucher: dict = Body(...)):
    from app.core.pipeline import canonicalize_voucher
    voucher = canonicalize_voucher(voucher)
    return taxid.check_voucher(voucher)


@app.get("/api/sysconfig")
def api_get_sysconfig():
    """系統組態（LLM 連線/API Key、掃描來源、雲端部署權限）；密鑰遮罩。"""
    return sysconfig.view()


@app.post("/api/sysconfig")
def api_save_sysconfig(payload: dict = Body(...)):
    section = payload.get("section", "")
    result = sysconfig.save_section(section, payload.get("values", {}))
    if result is None:
        return JSONResponse({"detail": "未知的組態區塊"}, status_code=400)
    return result


@app.get("/api/config")
def api_get_config():
    return settings_store.get_settings()


@app.post("/api/config")
def api_set_config(update: ConfigUpdate):
    new = {k: v for k, v in update.model_dump().items() if v}
    return settings_store.update_settings(new)


# ── 各階段數量（供分頁徽章）────────────────────────────────────
@app.get("/api/stats")
def api_stats():
    return {s.value: len(review.list_vouchers(status=s)) for s in ReviewStatus}


# ── 掃描來源目錄（伺服器端）─────────────────────────────────────
def _scan_dir() -> Path | None:
    """取得已設定且存在的本機掃描來源目錄。"""
    if sysconfig.get("scan_source", "type", "local") != "local":
        return None
    raw = sysconfig.get("scan_source", "path", "")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


@app.get("/api/scan-source/list")
def api_scan_list():
    """列出掃描來源目錄內可匯入的檔案（僅支援的副檔名）。"""
    d = _scan_dir()
    if d is None:
        return JSONResponse({"detail": "尚未設定或找不到掃描來源目錄（系統組態 → 掃描來源）"},
                            status_code=400)
    files = []
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in config.ALLOWED_SUFFIXES:
            try:
                files.append({"name": f.name, "size": f.stat().st_size})
            except OSError:
                pass
    return {"ok": True, "dir": str(d), "files": files}


@app.post("/api/scan-source/import")
def api_scan_import(filenames: list = Body(..., embed=True), company: str = Body("", embed=True)):
    """從掃描來源目錄匯入選定檔案（外觀準備，落 prepared）。"""
    d = _scan_dir()
    if d is None:
        return JSONResponse({"detail": "尚未設定或找不到掃描來源目錄"}, status_code=400)
    with _job_lock:
        if _job["status"] == "running":
            return JSONResponse({"ok": False, "detail": "已有匯入進行中"}, status_code=409)
        paths, errors = [], []
        for name in filenames:
            f = (d / Path(str(name)).name)   # 僅取檔名，防目錄穿越
            if f.is_file() and f.suffix.lower() in config.ALLOWED_SUFFIXES:
                paths.append(str(f))
            else:
                errors.append(f"{name}: 找不到或不支援")
        if not paths:
            return JSONResponse({"ok": False, "detail": "無有效檔案可匯入"}, status_code=400)
        _job.update(id=str(uuid.uuid4()), status="running", done=0,
                    total=len(paths), current="", results=[], errors=errors)
    threading.Thread(target=_run_batch, args=(list(paths), company), daemon=True).start()
    return {"ok": True, "job_id": _job["id"], "total": _job["total"]}


# ── 單張分析 ────────────────────────────────────────────────────
@app.post("/api/analyze")
async def api_analyze(file: UploadFile = File(...), company: str = Form("")):
    try:
        path = await _save_upload(file)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    try:
        # 匯入只做外觀準備（判類型＋轉正），欄位辨識留待外觀確認後執行
        vouchers = pipeline.prepare_pages(str(path))
        if not vouchers:
            return JSONResponse({"ok": False, "detail": "無法讀取任何頁面"}, status_code=400)
        ids = [review.save_voucher(v, company=company, status=ReviewStatus.prepared).id for v in vouchers]
        return {"ok": True, "id": ids[0], "ids": ids, "count": len(ids),
                "prepared": True, "voucher": vouchers[0].model_dump()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("分析失敗")
        return JSONResponse({"ok": False, "detail": f"分析失敗：{exc}"}, status_code=500)


# ── 批次（背景執行 + 輪詢）──────────────────────────────────────
@app.post("/api/batch")
async def api_batch(files: list[UploadFile] = File(...), company: str = Form("")):
    with _job_lock:
        if _job["status"] == "running":
            return JSONResponse({"ok": False, "detail": "已有批次進行中"}, status_code=409)
        paths = []
        for f in files:
            try:
                paths.append(str(await _save_upload(f)))
            except ValueError as exc:
                _job["errors"].append(f"{f.filename}: {exc}")
        _job.update(id=str(uuid.uuid4()), status="running", done=0,
                    total=len(paths), current="", results=[], errors=list(_job["errors"]))
    threading.Thread(target=_run_batch, args=(list(paths), company), daemon=True).start()
    return {"ok": True, "job_id": _job["id"], "total": _job["total"]}


@app.get("/api/job")
def api_job():
    return _job


def _run_batch(paths: list[str], company: str = ""):
    for p in paths:
        with _job_lock:
            _job["current"] = Path(p).name
        try:
            vouchers = pipeline.prepare_pages(p)
            for voucher in vouchers:
                rec = review.save_voucher(voucher, company=company, status=ReviewStatus.prepared)
                with _job_lock:
                    _job["results"].append({"id": rec.id, **voucher.model_dump()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("批次項目失敗：%s", p)
            with _job_lock:
                _job["errors"].append(f"{Path(p).name}: {exc}")
        finally:
            with _job_lock:
                _job["done"] += 1
    with _job_lock:
        _job["status"] = "completed"
        _job["current"] = ""


# ── 上傳存檔 ────────────────────────────────────────────────────
async def _save_upload(file: UploadFile) -> Path:
    if not file.filename:
        raise ValueError("未提供檔案")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in config.ALLOWED_SUFFIXES:
        raise ValueError(f"不支援的檔案類型：{suffix}")
    content = await file.read()
    if len(content) > config.MAX_FILE_SIZE:
        raise ValueError("檔案超過 5MB 限制")
    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    dest.write_bytes(content)
    return dest


# ── 審核流程（記帳士初步修改 → 會計師核定）──────────────────────
def _rec_to_dict(rec: VoucherRecord, full: bool = False) -> dict:
    d = {
        "id": rec.id, "source_file": rec.source_file, "company": rec.company,
        "period": rec.period, "status": rec.status, "confidence": rec.confidence,
        "structure_model": rec.structure_model, "ocr_provider": rec.ocr_provider,
        "clerk_note": rec.clerk_note, "accountant_note": rec.accountant_note,
        "dev_note": rec.dev_note, "manual_entry": bool(rec.manual_entry),
        # 人工是否動過欄位：重新辨識會覆蓋 current_json，前端據此先問過使用者
        "edited": rec.ai_json != rec.current_json,
        "created_at": rec.created_at.isoformat() if rec.created_at else "",
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else "",
    }
    d["has_image"] = bool(rec.image_path)
    d["attachments"] = len(json.loads(rec.attachments or "[]"))
    if full:
        d["ai"] = json.loads(rec.ai_json)
        d["current"] = json.loads(rec.current_json)
        d["is_import"] = importdecl.is_import(d["current"])
        # 進口報單鎖定：賣方為國外供應商/海關，統編免核
        d["taxcheck"] = None if d["is_import"] else taxid.check_voucher(d["current"])
        d["datecheck"] = taxid.check_date(d["current"].get("date", ""))
        d["amountcheck"] = taxid.check_amounts(d["current"])
    else:
        cur = json.loads(rec.current_json)
        d["voucher_id"] = cur.get("voucher_id", "")
        d["date"] = cur.get("date", "")
        d["total_amount"] = cur.get("total_amount")
        d["doc_type"] = cur.get("doc_type", "")            # 清單要顯示表單種類
        d["doc_type_key"] = cur.get("doc_type_key", "")
    return d


@app.get("/api/vouchers")
def api_list(status: str = "", company: str = "", period: str = ""):
    st = ReviewStatus(status) if status else None
    recs = review.list_vouchers(status=st, company=company, period=period)
    return {"items": [_rec_to_dict(r) for r in recs]}


@app.get("/api/vouchers/{rec_id}")
def api_get(rec_id: int):
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return _rec_to_dict(rec, full=True)


@app.put("/api/vouchers/{rec_id}")
def api_update(rec_id: int, voucher: dict = Body(...)):
    rec = review.update_current(rec_id, voucher)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return _rec_to_dict(rec, full=True)


@app.post("/api/vouchers/{rec_id}/clerk-review")
def api_clerk(rec_id: int, note: str = Body("", embed=True)):
    rec = review.clerk_review(rec_id, note)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/approve")
def api_approve(rec_id: int, note: str = Body("", embed=True)):
    rec = review.accountant_approve(rec_id, note)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/reject")
def api_reject(rec_id: int, note: str = Body("", embed=True)):
    rec = review.reject(rec_id, note)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/company")
def api_set_company(rec_id: int, company: str = Body("", embed=True)):
    """補填歸屬公司（匯入時漏選者）。"""
    rec = review.set_company(rec_id, company)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/notes")
def api_notes(request: Request, rec_id: int, clerk_note: str = Body(None, embed=True),
              accountant_note: str = Body(None, embed=True),
              dev_note: str = Body(None, embed=True),
              manual_entry: bool = Body(None, embed=True)):
    """只存備註、不轉狀態。

    這三欄會被餵給下次辨識當人工修正提示，但先前只有「記帳士確認／核定／退回」
    這些轉狀態的動作才會寫入 → 使用者在待審畫面打了備註按「儲存修正」，備註被
    靜默丟棄，重新辨識自然沒有效果。角色限制比照既有：會計師備註限會計師、
    開發者備註限管理員。"""
    user = auth.get_user_by_token(request.cookies.get("session")) or {}
    role = user.get("role", "")
    patch = {}
    if clerk_note is not None:
        patch["clerk_note"] = clerk_note
    if accountant_note is not None and role in ("accountant", "admin"):
        patch["accountant_note"] = accountant_note
    if dev_note is not None and role == "admin":
        patch["dev_note"] = dev_note
    if manual_entry is not None:
        patch["manual_entry"] = manual_entry
    rec = review.set_notes(rec_id, **patch)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/resubmit")
def api_resubmit(rec_id: int, note: str = Body("", embed=True)):
    """退回 → 待審（重新送審）。不限會計師：退回多半由記帳士修正後送回。"""
    rec = review.resubmit(rec_id, note)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/rotate")
def api_rotate(rec_id: int, direction: str = Body("cw", embed=True)):
    ok = review.rotate_image(rec_id, "ccw" if direction == "ccw" else "cw")
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "無影像可旋轉"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/rotate-fine")
def api_rotate_fine(rec_id: int, angle: float = Body(0, embed=True)):
    ok = review.rotate_fine(rec_id, angle)
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "無影像可旋轉"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/crop")
def api_crop(rec_id: int, box: list = Body(..., embed=True)):
    ok = review.crop_image(rec_id, box)
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "裁切範圍無效或無影像"}, status_code=400)


@app.post("/api/vouchers/{rec_id}/split")
def api_split(rec_id: int, box: list = Body(..., embed=True)):
    """一頁多張憑證：裁一塊建立為新憑證紀錄，並把該區從原圖塗白。"""
    try:
        rec = review.split_voucher(rec_id, box)
    except Exception as exc:  # noqa: BLE001
        logger.exception("分割辨識失敗")
        return JSONResponse({"ok": False, "detail": f"分割失敗：{exc}"}, status_code=500)
    if not rec:
        return JSONResponse({"detail": "裁切範圍無效或無影像"}, status_code=400)
    src = review.get_voucher(rec_id)
    return {"ok": True, "id": rec.id,
            "source_blank": review.is_blank_image(rec_id),
            # 原圖已被塗白：若先前已萃取過欄位，那些值對應的是舊影像，需重辨識
            "source_extracted": bool(src and src.status not in
                                     (ReviewStatus.prepared, ReviewStatus.queued))}


@app.post("/api/vouchers/{rec_id}/re-recognize")
def api_re_recognize(rec_id: int):
    try:
        rec = review.re_recognize(rec_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("重新辨識失敗")
        return JSONResponse({"ok": False, "detail": f"重新辨識失敗：{exc}"}, status_code=500)
    return {"ok": True, "id": rec.id} if rec else JSONResponse({"detail": "無影像"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/set-type")
def api_set_type(rec_id: int, doc_type_key: str = Body(..., embed=True)):
    """外觀確認：修正表單種類。"""
    rec = review.set_type(rec_id, doc_type_key)
    return {"ok": True, "id": rec.id} if rec else JSONResponse({"detail": "找不到紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/confirm")
def api_confirm(rec_id: int, doc_type_key: str = Body("", embed=True)):
    """外觀確認完成 → 加入掃描清單（prepared → queued）。可帶 doc_type_key 先修正類型。"""
    if doc_type_key:
        review.set_type(rec_id, doc_type_key)
    rec = review.confirm_appearance(rec_id)
    return {"ok": True, "id": rec.id} if rec else JSONResponse({"detail": "找不到紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/recognize")
def api_recognize(rec_id: int):
    """依確認的類型在準備好的影像上做內容辨識（queued/prepared → pending 待審）。"""
    try:
        rec = review.recognize_record(rec_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("內容辨識失敗")
        return JSONResponse({"ok": False, "detail": f"辨識失敗：{exc}"}, status_code=500)
    return {"ok": True, "id": rec.id} if rec else JSONResponse({"detail": "無影像"}, status_code=404)


# ── 批次內容提取（掃描清單一鍵提取，輪詢）──────────────────────
@app.post("/api/extract-batch")
def api_extract_batch(ids: list = Body(None, embed=True)):
    """對掃描清單（queued）或指定 ids 逐一內容辨識。背景執行 + 輪詢。"""
    with _ejob_lock:
        if _ejob["status"] == "running":
            return JSONResponse({"ok": False, "detail": "已有提取進行中"}, status_code=409)
        target = list(ids) if ids else review.ids_by_status(ReviewStatus.queued)
        if not target:
            return JSONResponse({"ok": False, "detail": "掃描清單沒有待提取的憑證"}, status_code=400)
        _ejob.update(id=str(uuid.uuid4()), status="running", done=0,
                     total=len(target), current="", results=[], errors=[])
    threading.Thread(target=_run_extract, args=(target,), daemon=True).start()
    return {"ok": True, "job_id": _ejob["id"], "total": _ejob["total"]}


@app.get("/api/extract-job")
def api_extract_job():
    return _ejob


def _run_extract(ids: list):
    for rec_id in ids:
        with _ejob_lock:
            _ejob["current"] = f"#{rec_id}"
        try:
            rec = review.recognize_record(rec_id)
            with _ejob_lock:
                if rec:
                    _ejob["results"].append({"id": rec.id})
                else:
                    _ejob["errors"].append(f"#{rec_id}: 無影像或找不到")
        except Exception as exc:  # noqa: BLE001
            logger.exception("批次提取失敗：#%s", rec_id)
            with _ejob_lock:
                _ejob["errors"].append(f"#{rec_id}: {exc}")
        finally:
            with _ejob_lock:
                _ejob["done"] += 1
    with _ejob_lock:
        _ejob["status"] = "completed"
        _ejob["current"] = ""


@app.get("/api/vouchers/{rec_id}/image")
def api_voucher_image(rec_id: int, att: int = -1):
    """att=-1 主頁；att>=0 取第 N 張附頁。"""
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "找不到紀錄"}, status_code=404)
    if att >= 0:
        atts = json.loads(rec.attachments or "[]")
        rel = atts[att] if att < len(atts) else None
    else:
        rel = rec.image_path
    if not rel:
        return JSONResponse({"detail": "無掃描影像"}, status_code=404)
    p = config.UPLOAD_DIR / rel
    if not p.exists():
        return JSONResponse({"detail": "影像檔不存在"}, status_code=404)
    return FileResponse(str(p))


@app.post("/api/vouchers/merge")
def api_merge(primary_id: int = Body(..., embed=True), ids: list = Body(..., embed=True)):
    """把 ids 中其餘紀錄併入主頁 primary_id（其影像成為主頁附頁，並刪除其餘紀錄）。"""
    others = [i for i in ids if i != primary_id]
    if not others:
        return JSONResponse({"detail": "至少需選兩筆才能合併"}, status_code=400)
    rec = review.merge_vouchers(primary_id, others)
    return {"ok": True, "id": rec.id, "attachments": len(json.loads(rec.attachments or "[]"))} \
        if rec else JSONResponse({"detail": "找不到主頁紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/classify")
def api_classify(rec_id: int):
    """依目前欄位自動產生會計分錄（科目分類），寫回 current。"""
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    try:
        voucher = Voucher.model_validate(json.loads(rec.current_json))
        voucher = classify.classify(voucher, company=rec.company)
        review.update_current(rec_id, voucher.model_dump(), mark_manual=False)
        return {"ok": True, "entries": [e.model_dump() for e in voucher.entries],
                "debit_total": voucher.debit_total, "credit_total": voucher.credit_total,
                "balance_check": voucher.balance_check, "classification_meta": voucher.classification_meta,
                "warnings": voucher.classification_meta.get("warnings", []),
                "validation_warnings": voucher.validation_warnings}
    except Exception as exc:  # noqa: BLE001
        logger.exception("科目分類失敗")
        return JSONResponse({"ok": False, "detail": f"分類失敗：{exc}"}, status_code=500)


@app.post("/api/vouchers/{rec_id}/dev-note")
def api_dev_note(rec_id: int, note: str = Body("", embed=True)):
    """開發者備註（管理員專屬）。"""
    rec = review.set_dev_note(rec_id, note)
    return {"ok": bool(rec)} if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.get("/api/feedback")
def api_feedback():
    """修正回饋清單（有備註或經修正的紀錄；管理員專屬）。"""
    return {"items": review.feedback_log()}


@app.get("/api/alignment")
def api_alignment():
    """已核定紀錄中 AI 原始值 vs 人工修正值的差異（模型對齊訊號）。"""
    return {"pairs": review.alignment_pairs()}


# ── 單月日記帳彙整 ──────────────────────────────────────────────
@app.get("/api/journal/periods")
def api_journal_periods(company: str = ""):
    return {"periods": journal.periods(company=company)}


@app.get("/api/journal")
def api_journal(period: str, company: str = ""):
    return journal.build_journal(period, company=company)


# ── 報表（客製模板兩套）──────────────────────────────────────────
@app.get("/api/report/templates")
def api_report_templates():
    return {"templates": reports.TEMPLATES}


@app.get("/api/report")
def api_report(template: str, period: str, company: str = ""):
    return HTMLResponse(reports.render(template, period, company))


# ── 地端備份（管理員限定）──────────────────────────────────────
@app.get("/api/backups")
def api_backups():
    return {"backups": backup.list_backups()}


@app.post("/api/backup")
def api_backup_create():
    return backup.create_backup()


@app.get("/api/backup/download/{name}")
def api_backup_download(name: str):
    p = backup.backup_path(name)
    if not p:
        return JSONResponse({"detail": "查無此備份"}, status_code=404)
    return FileResponse(str(p), filename=name, media_type="application/zip")


# ── 靜態前端（必須最後掛載，作為 catch-all）────────────────────
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
