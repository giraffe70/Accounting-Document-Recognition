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
from app import import_api
from app.core import activity, doc_types, model_factory, pipeline, settings_store
from app.db.models import ReviewStatus, VoucherRecord
from app.db.session import init_db
from app.models.schemas import ConfigUpdate, Voucher
from app.services import (accounts, auth, backup, classify, database_view, importdecl, journal,
                          reports, review, sysconfig, taxid, companies, item_account_mapping, entry_workflow, import_jobs)
from app.services import user_management

_LOG_FMT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
# 同時寫檔：辨識失敗的 traceback 只印在主控台，視窗一關就查不到了（滾動 2MB × 5）
_fh = RotatingFileHandler(config.LOG_FILE, maxBytes=2 * 1024 * 1024,
                          backupCount=5, encoding="utf-8")
_fh.setFormatter(logging.Formatter(_LOG_FMT))
logging.getLogger().addHandler(_fh)
logger = logging.getLogger("pdf_scan")

app = FastAPI(title="憑證辨識系統 API")


@app.exception_handler(import_jobs.JobConflict)
async def import_conflict_handler(request, exc):
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)


@app.exception_handler(import_jobs.JobNotFound)
async def import_not_found_handler(request, exc):
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=404)


@app.exception_handler(review.RecognitionProtected)
async def recognition_protected_handler(request, exc):
    return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)


@app.exception_handler(item_account_mapping.MappingConflict)
async def mapping_conflict_handler(request, exc):
    return JSONResponse({"ok": False, "detail": str(exc), "conflicts": exc.conflicts,
                         "latest": exc.latest}, status_code=409)


@app.exception_handler(entry_workflow.EntryConflict)
async def entry_conflict_handler(request, exc):
    return JSONResponse({"ok": False, "detail": str(exc), "latest": exc.latest}, status_code=409)


init_db()
auth.ensure_seed_users()
validation.init_validation()
app.include_router(validation.router)
app.include_router(database_view.router)
app.include_router(import_api.router)
app.include_router(user_management.router)


@app.on_event("startup")
def start_import_worker():
    from app.services import import_jobs
    import_jobs.start()


@app.on_event("shutdown")
def stop_import_worker():
    from app.services import import_jobs
    import_jobs.stop()

_PUBLIC_API = {"/api/login", "/api/logout", "/api/me", "/api/health"}


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """基本資安：/api 需登入；設定、備份與開發者功能限管理員。"""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API:
        user = auth.get_user_by_token(request.cookies.get("session"))
        if not user:
            return JSONResponse({"detail": "未登入"}, status_code=401, headers={"Cache-Control": "no-store"})
        if not user.get("is_admin"):
            if path == "/api/config":
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path == "/api/accounts" and (request.method != "GET" or "company_id" not in request.query_params):
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path.startswith("/api/backup"):
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path.startswith("/api/sysconfig"):
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
            elif path == "/api/feedback":
                return JSONResponse({"detail": "需管理員權限"}, status_code=403)
    response = await call_next(request)
    if path == "/api/users" or path.startswith("/api/users/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ── 登入 / 角色 ──────────────────────────────────────────────────
@app.post("/api/login")
def api_login(body: dict = Body(...)):
    authenticated = auth.authenticate_session(body.get("username", ""), body.get("password", ""))
    if not authenticated:
        return JSONResponse({"detail": "帳號或密碼錯誤"}, status_code=401)
    user, token = authenticated
    resp = JSONResponse(user)
    resp.set_cookie("session", token, httponly=True, samesite="lax")
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
def api_models(local_only: bool = False):
    """主設定可指定僅地端模型；預設保留驗證頁的完整模型清單。"""
    return {
        "ocr": model_factory.list_ocr_providers(),
        "ocr_status": model_factory.ocr_status(),
        "structure": model_factory.list_models(kind="vlm", local_only=local_only),
        "classify": model_factory.list_models(kind="llm", local_only=local_only),
        "text": model_factory.list_models(kind="text", local_only=local_only),
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
            info["model"] = " / ".join(dict.fromkeys(ocr_ok.get("actual_models",{}).values())) or "PaddleOCR（待載入）"
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
                          "has_entries": v.get("has_entries", False), "entry_fields": v["entry_fields"],
                          "is_supporting": doc_types.is_supporting(k)}
                      for k, v in doc_types.DOC_TYPES.items()}}


@app.get("/api/companies")
def api_companies():
    return {"companies": taxid.load_companies()}


@app.get("/api/accounts")
def api_get_accounts(company: str = "", company_id: int | None = None, strict: bool = False):
    """該公司的科目表（各公司科目代號不同；未自訂者回退通用表）。"""
    if company_id is not None:
        with review.get_session() as session:
            registered = companies.resolve(session, company_id=company_id)
            if not registered:
                return JSONResponse({"detail": "歸屬公司不存在"}, status_code=422)
            company = registered.account_chart_key
    return {"accounts": accounts.load_accounts(company, strict=strict), "company": company,
            "with_chart": accounts.companies_with_chart()}


@app.post("/api/item-account-mappings/lookup")
def api_lookup_item_accounts(payload: dict = Body(...)):
    with review.get_session() as session:
        company_id = payload.get("company_id")
        if company_id is not None and (not isinstance(company_id, int) or isinstance(company_id, bool) or
                not companies.resolve(session, company_id=company_id)):
            return JSONResponse({"detail": "歸屬公司不存在"}, status_code=422)
        rows = payload.get("items") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return JSONResponse({"detail": "明細格式不合法"}, status_code=422)
        return {"company_id": company_id, "items": item_account_mapping.lookup(session, company_id, rows)}


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
def api_scan_import(request: Request, filenames: list = Body(..., embed=True), company: str = Body("", embed=True), background: bool = Body(False, embed=True), mode: str = Body("automatic", embed=True)):
    """從掃描來源目錄匯入選定檔案（外觀準備，落 prepared）。"""
    d = _scan_dir()
    if d is None:
        return JSONResponse({"detail": "尚未設定或找不到掃描來源目錄"}, status_code=400)
    from app.services import import_jobs
    if len(filenames) != 1:
        return JSONResponse({"ok": False, "detail": "一次只能選擇一個檔案"}, status_code=422)
    paths = [{"path": str(d / Path(str(name)).name), "name": Path(str(name)).name} for name in filenames]
    valid = [p for p in paths if Path(p["path"]).is_file() and Path(p["path"]).suffix.lower() in config.ALLOWED_SUFFIXES]
    if not valid:
        return JSONResponse({"ok": False, "detail": "無有效檔案可匯入"}, status_code=400)
    try:
        created = import_jobs.create_job(valid, import_api.owner(request), company, mode=mode)
    except import_jobs.JobConflict as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    if background:
        return JSONResponse(created, status_code=202)
    return {"ok": True, "job_id": created["id"], "total": len(valid)}


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
        records = review.save_prepared_vouchers(vouchers, company)
        ids = [r.id for r in records if r.record_role != "source_page"] or [r.id for r in records]
        return {"ok": True, "id": ids[0], "ids": ids, "count": len(ids),
                "prepared": True, "source_page_ids": [r.id for r in records if r.record_role == "source_page"],
                "voucher": next(v.model_dump() for v,r in zip(vouchers,records) if r.id == ids[0])}
    except Exception as exc:  # noqa: BLE001
        logger.exception("分析失敗")
        return JSONResponse({"ok": False, "detail": f"分析失敗：{exc}"}, status_code=500)


# ── 批次（背景執行 + 輪詢）──────────────────────────────────────
@app.post("/api/batch")
async def api_batch(request: Request, files: list[UploadFile] = File(...), company: str = Form("")):
    return await import_api.create(request, files, company, mode="automatic")


@app.get("/api/job")
def api_job(request: Request):
    from app.services import import_jobs
    return import_jobs.legacy_status(import_api.owner(request))


def _run_batch(paths: list[str], company: str = ""):
    for p in paths:
        with _job_lock:
            _job["current"] = Path(p).name
        try:
            vouchers = pipeline.prepare_pages(p)
            for voucher, rec in zip(vouchers, review.save_prepared_vouchers(vouchers, company)):
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


# ── 憑證確認（保存修正 → 人工確認完成）──────────────────────────
def _rec_to_dict(rec: VoucherRecord, full: bool = False) -> dict:
    from app.services import recognition_details
    d = {
        "id": rec.id, "source_file": rec.source_file, "company": rec.company,
        "company_id": rec.company_id,
        "record_role": rec.record_role, "source_parent_id": rec.source_parent_id,
        "period": rec.period, "status": rec.status, "confidence": rec.confidence,
        "structure_model": rec.structure_model, "ocr_provider": rec.ocr_provider,
        "correction_note": rec.correction_note,
        "confirmed_at": rec.confirmed_at.isoformat() if rec.confirmed_at else "",
        "confirmed_by_name": getattr(rec, "confirmed_by_name", ""),
        "recognition_report_ready": rec.status == ReviewStatus.confirmed and getattr(rec, "recognition_confirmed_version", 0) == recognition_details.VERSION,
        "manual_entry": bool(rec.manual_entry),
        "entries_status": getattr(rec, "entries_status", "none"),
        "entries_confirmed_at": rec.entries_confirmed_at.isoformat() + "Z" if getattr(rec, "entries_confirmed_at", None) else "",
        "entries_confirmed_by_name": getattr(rec, "entries_confirmed_by_name", ""),
        # 人工是否動過欄位：重新辨識會覆蓋 current_json，前端據此先問過使用者
        "edited": item_account_mapping.strip_accounting(json.loads(rec.ai_json)) !=
                  item_account_mapping.strip_accounting(json.loads(rec.current_json)),
        "created_at": rec.created_at.isoformat() if rec.created_at else "",
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else "",
    }
    d["has_image"] = bool(rec.image_path)
    d["attachments"] = len(json.loads(rec.attachments or "[]"))
    if full:
        d["image_restore_available"] = review.image_restore_available(rec)
        d["ai"] = json.loads(rec.ai_json)
        d["current"] = json.loads(rec.current_json)
        recognition_details.ensure(d["current"], rec.id)
        d["detail_account_warnings"] = review.recognition_account_warnings(rec, d["current"])
        d["entries_checks"] = entry_workflow.checks(d["current"])
        d["entries_proposal"] = json.loads(getattr(rec, "entries_proposal_json", "{}") or "{}")
        d["is_import"] = importdecl.is_import(d["current"])
        # 進口報單鎖定：賣方為國外供應商/海關，統編免核
        d["taxcheck"] = None if d["is_import"] else taxid.check_voucher(d["current"])
        d["datecheck"] = taxid.check_date(d["current"].get("date", ""), d["current"].get("field_evidence"))
        d["amountcheck"] = taxid.check_amounts(d["current"])
    else:
        cur = json.loads(rec.current_json)
        d["origin"] = cur.get("origin", {})
        d["quality"] = cur.get("quality", {})
        d["voucher_id"] = cur.get("voucher_id", "")
        d["date"] = cur.get("date", "")
        d["total_amount"] = cur.get("total_amount")
        d["doc_type"] = cur.get("doc_type", "")            # 清單要顯示表單種類
        d["doc_type_key"] = cur.get("doc_type_key", "")
    return d


@app.get("/api/vouchers")
def api_list(status: str = "", company: str = "", period: str = ""):
    try:
        st = [ReviewStatus(value.strip()) for value in status.split(",")] if status else None
    except ValueError:
        return JSONResponse({"detail": "憑證狀態不合法"}, status_code=422)
    recs = review.list_vouchers(status=st, company=company, period=period, include_sources=True)
    return {"items": [_rec_to_dict(r) for r in recs]}


@app.get("/api/vouchers/{rec_id}")
def api_get(rec_id: int):
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return _rec_to_dict(rec, full=True)


def _delete_busy() -> bool:
    """匯入或批次提取進行中時不刪除，避免與背景寫檔／辨識互相踩踏。"""
    from app.services import import_jobs
    return _job["status"] == "running" or _ejob["status"] == "running" or import_jobs.is_busy()


@app.get("/api/vouchers/{rec_id}/delete-preview")
def api_delete_preview(request: Request, rec_id: int, scope: str = ""):
    """逐頁匯入期間提供 page；其餘沿用 record 與 upload。"""
    if scope and scope not in review.DELETE_SCOPES:
        return JSONResponse({"detail": "刪除範圍不合法"}, status_code=422)
    user = auth.get_user_by_token(request.cookies.get("session")) or {}
    is_admin = bool(user.get("is_admin"))
    busy = _delete_busy()
    try:
        result = review.delete_previews(rec_id, scope, is_admin=is_admin, actor_id=user.get("id"), busy=busy)
    except review.DeleteConflict as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    if result is None:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return {"ok": True, "is_admin": is_admin, "busy": busy, **result}


@app.delete("/api/vouchers/{rec_id}")
def api_delete(request: Request, rec_id: int, scope: str = "record", expected_updated_at: str = ""):
    """刪除單據、整份上傳檔或目前待確認頁面。"""
    if scope not in review.DELETE_SCOPES:
        return JSONResponse({"detail": "刪除範圍不合法"}, status_code=422)
    if _delete_busy():
        return JSONResponse({"ok": False, "detail": "匯入或提取進行中，請稍後再刪除"}, status_code=409)
    user = auth.get_user_by_token(request.cookies.get("session")) or {}
    try:
        result = review.delete_vouchers(rec_id, scope, is_admin=bool(user.get("is_admin")),
                                        actor=user.get("username", ""), actor_id=user.get("id"),
                                        expected_updated_at=expected_updated_at)
    except review.DeleteForbidden as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=403)
    except review.DeleteConflict as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=422)
    return result or JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.put("/api/vouchers/{rec_id}")
def api_update(request: Request, rec_id: int, voucher: dict = Body(...)):
    from datetime import datetime
    expected = voucher.pop("expected_updated_at", None)
    try:
        expected = datetime.fromisoformat(expected) if expected is not None else None
        rec = review.update_current(rec_id, voucher, expected_updated_at=expected,
            _actor=auth.get_user_by_token(request.cookies.get("session")), _require_account_version=True)
    except ValueError as exc:
        if isinstance(exc, (review.RecognitionProtected, item_account_mapping.MappingConflict, entry_workflow.EntryConflict)):
            raise
        return JSONResponse({"detail": str(exc)}, status_code=422)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return _rec_to_dict(rec, full=True)


@app.post("/api/vouchers/{rec_id}/save")
def api_save_current(request: Request, rec_id: int, payload: dict = Body(...)):
    from sqlalchemy.exc import IntegrityError, OperationalError
    actor = auth.get_user_by_token(request.cookies.get("session"))
    if not actor:
        return JSONResponse({"detail": "未登入"}, status_code=401)
    try:
        rec, skipped = review.save_current(rec_id, payload, actor)
    except ValueError as exc:
        if isinstance(exc, (item_account_mapping.MappingConflict, review.RecognitionProtected, entry_workflow.EntryConflict)):
            raise
        return JSONResponse({"detail": str(exc)}, status_code=422)
    except (IntegrityError, OperationalError) as exc:
        if isinstance(exc, OperationalError) and "locked" not in str(exc).lower():
            raise
        return JSONResponse({"detail": "資料正被更新，請重新查詢後儲存", "conflicts": []}, status_code=409)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    return {**_rec_to_dict(rec, full=True), "mapping_skipped": skipped}


@app.post("/api/vouchers/{rec_id}/confirm")
def api_confirm_record(request: Request, rec_id: int, expected_updated_at: str = Body(..., embed=True)):
    rec = review.confirm_record(rec_id, expected_updated_at, auth.get_user_by_token(request.cookies.get("session")))
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/company")
def api_set_company(request: Request, rec_id: int, company: str = Body("", embed=True), expected_updated_at: str = Body(..., embed=True)):
    """補填歸屬公司（匯入時漏選者）。"""
    rec = review.set_company(rec_id, company, expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/notes")
def api_notes(rec_id: int, correction_note: str = Body(None, embed=True),
              manual_entry: bool = Body(None, embed=True)):
    """保存修正備註及人工輸入標記，不轉狀態。"""
    patch = {}
    if correction_note is not None:
        patch["correction_note"] = correction_note
    if manual_entry is not None:
        patch["manual_entry"] = manual_entry
    rec = review.set_notes(rec_id, **patch)
    return _rec_to_dict(rec) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/rotate")
def api_rotate(request: Request, rec_id: int, direction: str = Body("cw", embed=True), expected_updated_at: str = Body(..., embed=True)):
    ok = review.rotate_image(rec_id, "ccw" if direction == "ccw" else "cw", expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "無影像可旋轉"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/rotate-fine")
def api_rotate_fine(request: Request, rec_id: int, angle: float = Body(0, embed=True), reset: bool = Body(False, embed=True), expected_updated_at: str = Body(..., embed=True)):
    """angle 為累計角度（正值順時針，±15°）；reset=true 還原微調。"""
    ok = review.rotate_fine(rec_id, angle, reset=reset, expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "無影像可旋轉"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/restore-image")
def api_restore_image(request: Request, rec_id: int, expected_updated_at: str = Body(..., embed=True)):
    """外觀確認：影像復原到剛進入外觀確認時的狀態，不呼叫模型。"""
    ok = review.restore_image(rec_id, expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "無影像可復原"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/crop")
def api_crop(request: Request, rec_id: int, box: list = Body(..., embed=True), expected_updated_at: str = Body(..., embed=True)):
    ok = review.crop_image(rec_id, box, expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return {"ok": bool(ok)} if ok else JSONResponse({"detail": "裁切範圍無效或無影像"}, status_code=400)


@app.post("/api/vouchers/{rec_id}/split")
def api_split(request: Request, rec_id: int, box: list = Body(..., embed=True), expected_updated_at: str = Body(None, embed=True)):
    """一頁多張憑證：裁一塊建立為新憑證紀錄，並把該區從原圖塗白。"""
    try:
        rec = review.split_voucher(rec_id, box, expected_updated_at=expected_updated_at, actor=auth.get_user_by_token(request.cookies.get("session")))
    except (review.RecognitionProtected, entry_workflow.EntryConflict):
        raise
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
def api_re_recognize(request: Request, rec_id: int):
    try:
        rec = review.re_recognize(rec_id, actor=auth.get_user_by_token(request.cookies.get("session")))
    except entry_workflow.EntryConflict:
        raise
    except review.RecognitionProtected as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    except Exception as exc:  # noqa: BLE001
        logger.exception("重新辨識失敗")
        return JSONResponse({"ok": False, "detail": f"重新辨識失敗：{exc}"}, status_code=500)
    return {"ok": True, "id": rec.id} if rec else JSONResponse({"detail": "無影像"}, status_code=404)


@app.post("/api/vouchers/{rec_id}/set-type")
def api_set_type(request: Request, rec_id: int, doc_type_key: str = Body(..., embed=True), expected_updated_at: str = Body(..., embed=True)):
    """外觀確認：修正表單種類。"""
    rec = review.set_type(rec_id, doc_type_key, expected_updated_at=expected_updated_at,
        actor=auth.get_user_by_token(request.cookies.get("session")))
    return {"ok": True, **_rec_to_dict(rec, full=True)} if rec else JSONResponse({"detail": "找不到紀錄"}, status_code=404)


@app.post("/api/vouchers/appearance-batch")
def api_appearance_batch(request: Request, items: list[dict] = Body(..., embed=True), operation: str = Body(..., embed=True)):
    if len(items) != 1:
        return JSONResponse({"detail":"外觀確認一次只能操作一張單據"},status_code=422)
    return review.appearance_batch(items,operation,actor=auth.get_user_by_token(request.cookies.get("session")))


@app.post("/api/vouchers/{rec_id}/confirm-appearance")
def api_confirm_appearance(request: Request, rec_id: int, doc_type_key: str = Body("", embed=True), expected_updated_at: str = Body(None, embed=True), document_mode: str = Body("single", embed=True)):
    """外觀確認完成 → 加入掃描清單（prepared → queued）。可帶 doc_type_key 先修正類型。"""
    from app.services import import_jobs
    try:
        rec = review.confirm_appearance(rec_id, doc_type_key,expected_updated_at=expected_updated_at,
            document_mode=document_mode,actor=auth.get_user_by_token(request.cookies.get("session")))
    except import_jobs.JobNotFound as exc:
        return JSONResponse({"detail":str(exc)},status_code=404)
    except import_jobs.JobConflict as exc:
        return JSONResponse({"detail":str(exc)},status_code=409)
    if not rec:
        return JSONResponse({"detail":"找不到紀錄"},status_code=404)
    pending = json.loads(rec.current_json).get("origin",{}).get("preparation",{}).get("layout_pending",False)
    return JSONResponse({"ok":True,"id":rec.id,"status":rec.status.value,"layout_pending":pending},status_code=202 if pending else 200)


@app.post("/api/vouchers/{rec_id}/recognize")
def api_recognize(request: Request, rec_id: int):
    """依確認的類型在準備好的影像上做內容辨識（queued/prepared → pending 待確認）。"""
    try:
        rec = review.recognize_record(rec_id, actor=auth.get_user_by_token(request.cookies.get("session")))
    except entry_workflow.EntryConflict:
        raise
    except review.RecognitionProtected as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
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


@app.get("/api/vouchers/{rec_id}/recognition-report", response_class=HTMLResponse)
def api_recognition_report(request: Request, rec_id: int, expected_updated_at: str):
    from app.services import recognition_details, recognition_report
    if not auth.get_user_by_token(request.cookies.get("session")):
        return JSONResponse({"detail": "未登入"}, status_code=401)
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    data = json.loads(rec.current_json)
    if (rec.record_role == "source_page" or rec.status != ReviewStatus.confirmed
            or rec.recognition_confirmed_version != recognition_details.VERSION
            or data.get("details_version") != recognition_details.VERSION
            or rec.updated_at.isoformat() != expected_updated_at):
        return JSONResponse({"detail": "內容尚未確認或版本已變更，請重新載入並確認"}, status_code=409)
    return HTMLResponse(recognition_report.render(rec, data, review.recognition_account_warnings(rec, data)),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/vouchers/{rec_id}/image")
def api_voucher_image(rec_id: int, att: int = -1, expected_updated_at: str = ""):
    """att=-1 主頁；att>=0 取第 N 張附頁。"""
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "找不到紀錄"}, status_code=404)
    if expected_updated_at and rec.updated_at.isoformat() != expected_updated_at:
        return JSONResponse({"detail": "影像版本已變更，請重新確認"}, status_code=409)
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
    return FileResponse(str(p), headers={"Cache-Control": "no-store"})


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
def api_classify(request: Request, rec_id: int, expected_updated_at: str = Body(..., embed=True)):
    """Generate an optional proposal without replacing the adopted draft."""
    rec = review.get_voucher(rec_id)
    if not rec:
        return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
    actor = auth.get_user_by_token(request.cookies.get("session"))
    if not actor:
        return JSONResponse({"detail": "未登入"}, status_code=401)
    try:
        review._check_model_write(rec)
        data = json.loads(rec.current_json)
        if rec.status not in (ReviewStatus.pending, ReviewStatus.confirmed) or doc_types.get(data.get("doc_type_key", "")).get("has_entries") or doc_types.is_supporting(data.get("doc_type_key", "")):
            return JSONResponse({"detail": "此類型不產生 AI 分錄建議，請核對原圖分錄"}, status_code=409)
        if rec.updated_at.isoformat() != expected_updated_at:
            raise entry_workflow.EntryConflict("單據已被修改，請重新載入", latest={
                "updated_at": rec.updated_at.isoformat(), "company_id": rec.company_id, "current": data})
        fingerprint = entry_workflow.source_fingerprint(rec)
        voucher = Voucher.model_validate(json.loads(rec.current_json))
        voucher = classify.classify(voucher, company=rec.company)
        saved = review.save_entry_proposal(rec_id, voucher, actor, fingerprint,
            expected_updated_at=expected_updated_at)
        if saved is None:
            return JSONResponse({"detail": "查無此紀錄"}, status_code=404)
        return {"ok": True, **_rec_to_dict(saved, full=True)}
    except entry_workflow.EntryConflict:
        raise
    except review.RecognitionProtected as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    except Exception as exc:  # noqa: BLE001
        logger.exception("科目分類失敗")
        return JSONResponse({"ok": False, "detail": f"分類失敗：{exc}"}, status_code=500)


@app.post("/api/vouchers/{rec_id}/entries/{action}")
def api_manage_entries(request: Request, rec_id: int, action: str, payload: dict = Body(...)):
    actor = auth.get_user_by_token(request.cookies.get("session"))
    if not actor:
        return JSONResponse({"detail": "未登入"}, status_code=401)
    try:
        rec = review.manage_entries(rec_id, action, payload.get("expected_updated_at"), actor,
            proposal_id=payload.get("proposal_id"), replace_existing=payload.get("replace_existing") is True)
    except (review.RecognitionProtected, entry_workflow.EntryConflict):
        raise
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return _rec_to_dict(rec, full=True) if rec else JSONResponse({"detail": "查無此紀錄"}, status_code=404)


@app.get("/api/feedback")
def api_feedback():
    """修正回饋清單（有修正備註、人工輸入或經修正的紀錄；管理員專屬）。"""
    return {"items": review.feedback_log()}


@app.get("/api/alignment")
def api_alignment():
    """已確認紀錄中 AI 原始值 vs 人工修正值的差異（模型對齊訊號）。"""
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
