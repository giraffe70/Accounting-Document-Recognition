"""Durable page imports. Model work is outside transactions and the event loop."""
import json
import hashlib
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import update
from sqlmodel import select
from app import config
from app.db.models import ImportJob, ImportFile, ImportPage, utc_now
from app.db.session import get_session
from app.core import model_invocation, ocr_policy, pipeline, settings_store, work_progress
from app.services import review

logger = logging.getLogger("pdf_scan.import_jobs")
_wake = threading.Event()
_stop = threading.Event()
_run_lock = threading.Lock()
_thread = None


class JobNotFound(ValueError):
    pass


class JobConflict(ValueError):
    pass


class SingleFileRequired(ValueError):
    pass


def _sequential(job):
    return json.loads(job.settings_json).get("sequential_review", False)


def _check_available(session, owner_id, *, exclude=None):
    active = session.exec(select(ImportJob).where(
        ImportJob.owner_id == owner_id,
        ImportJob.status.notin_(("completed", "cancelled")),
        ImportJob.id != (exclude or ""))).first()
    if active:
        raise JobConflict("請先完成目前檔案的外觀確認，或取消目前匯入，再上傳下一個檔案")


def _owned(session, job_id, owner_id):
    job = session.get(ImportJob, job_id)
    if job is None or job.owner_id != owner_id:
        raise JobNotFound("匯入工作不存在")
    return job


def create_job(paths, owner_id, company="", *, mode="manual_review", sequential=True):
    from app.core.preparation import MODES
    if mode not in MODES:
        raise ValueError("未知的匯入模式")
    if not paths:
        raise ValueError("無有效檔案可匯入")
    if sequential and len(paths) != 1:
        raise SingleFileRequired("一次只能上傳一個檔案；多頁 PDF 將逐頁進行外觀確認")
    versions = model_invocation.source_versions()
    sources = {}
    for item in paths:
        path = str(item["path"] if isinstance(item, dict) else item)
        sources[path] = _source_hash(path) if Path(path).is_file() else None
    snapshot = {"models": settings_store.get_settings(), "ocr_policy": ocr_policy.get_policy(), "versions": versions, "sources": sources, "mode": mode, "sequential_review": sequential}
    job = ImportJob(id=uuid.uuid4().hex, owner_id=owner_id, company=company,
                    version=versions["execution_version_sha256"], settings_json=json.dumps(snapshot, ensure_ascii=False))
    job_id = job.id
    with get_session() as session:
        # Serialize the check and insert across tabs, threads and server processes.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if sequential:
            _check_available(session, owner_id)
        session.add(job)
        session.flush()
        for position, item in enumerate(paths):
            path, name = (item["path"], item["name"]) if isinstance(item, dict) else (str(item), Path(item).name)
            session.add(ImportFile(id=f"{position:08x}" + uuid.uuid4().hex[:24], job_id=job.id, path=path, name=name, group_id=uuid.uuid4().hex))
        session.commit()
    _wake.set()
    return {"ok": True, "id": job_id, "job_id": job_id, "status": "queued", "total_files": len(paths)}


def _source_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_status(owner_id):
    with get_session() as session:
        job = session.exec(select(ImportJob).where(ImportJob.owner_id == owner_id).order_by(ImportJob.created_at.desc())).first()
        if not job:
            return {"id": "", "status": "idle", "done": 0, "total": 0, "current": "", "results": [], "errors": []}
        view = _view(session, job)
        files = session.exec(select(ImportFile).where(ImportFile.job_id == job.id)).all()
        pages = session.exec(select(ImportPage).where(ImportPage.job_id == job.id)).all()
        done = sum(f.indexed and all(p.status in ("completed", "failed") for p in pages if p.file_id == f.id) for f in files)
        errors = [f.error for f in files if f.error] + [p.error for p in pages if p.error]
        results = []
        for page in pages:
            for record_id in json.loads(page.voucher_ids_json):
                rec = session.get(review.VoucherRecord, record_id)
                if rec:
                    results.append({**json.loads(rec.current_json), "id": rec.id})
        if job.status in ("interrupted", "cancelled"):
            errors.append("匯入工作已中斷或取消，請使用新的匯入工作介面續跑")
        return {"id": job.id, "status": "running" if job.status in ("queued", "running", "cancelling") else "completed",
                "done": done, "total": len(files), "current": (view["current"] or {}).get("file", ""),
                "results": results, "errors": errors}


def _view(session, job):
    files = session.exec(select(ImportFile).where(ImportFile.job_id == job.id)).all()
    names = {file.id: file.name for file in files}
    pages = session.exec(select(ImportPage).where(ImportPage.job_id == job.id).order_by(ImportPage.file_id, ImportPage.page_no)).all()
    data = []
    for page in pages:
        data.append({"id": page.id, "file_name": names[page.file_id], "page_no": page.page_no,
                     "status": page.status, "phase": page.phase, "source_page_id": page.source_page_id,
                     "voucher_ids": json.loads(page.voucher_ids_json), "error": page.error,
                     "preview_url": f"/api/import-jobs/{job.id}/pages/{page.id}/preview" if page.preview_path else None})
    current = next((p for p in data if p["status"] not in ("completed",)), None)
    review_ids = []
    if current and current["status"] == "preview_ready":
        for ident in current["voucher_ids"] or [current["source_page_id"]]:
            rec = session.get(review.VoucherRecord, ident) if ident else None
            if rec and rec.status == review.ReviewStatus.prepared and (rec.record_role != "source_page" or not current["voucher_ids"]):
                review_ids.append(rec.id)
    return {"id": job.id, "job_id": job.id, "status": job.status, "phase": job.phase,
            "total_files": len(files), "total_pages": job.total_pages,
            "done_pages": sum(p.status in ("completed", "failed", "preview_ready") for p in pages),
            "awaiting_review_pages": sum(p.status == "preview_ready" for p in pages),
            "mode": json.loads(job.settings_json).get("mode", "automatic"),
            "sequential_review": _sequential(job), "blocks_upload": job.status not in ("completed", "cancelled"),
            "review_ids": review_ids, "current_page": current["page_no"] if current else None,
            "succeeded_pages": sum(p.status == "completed" for p in pages),
            "failed_pages": sum(p.status == "failed" for p in pages), "pages": data,
            "current": {"file": current["file_name"], "page_no": current["page_no"]} if current else None,
            "error": job.error, "file_errors": [{"name": f.name, "error": f.error} for f in files if f.error],
            "first_result_seconds": job.first_result_seconds, "created_at": job.created_at.isoformat()}


def get_job(job_id, owner_id):
    with get_session() as session:
        return _view(session, _owned(session, job_id, owner_id))


def list_jobs(owner_id):
    with get_session() as session:
        jobs = session.exec(select(ImportJob).where(ImportJob.owner_id == owner_id).order_by(ImportJob.created_at.desc())).all()
        return {"items": [_view(session, job) for job in jobs]}


def is_busy():
    with get_session() as session:
        return session.exec(select(ImportJob.id).where(ImportJob.status.in_(("queued", "running", "cancelling")))).first() is not None


def preview(job_id, page_id, owner_id):
    with get_session() as session:
        _owned(session, job_id, owner_id)
        page = session.get(ImportPage, page_id)
        if page is None or page.job_id != job_id or not page.preview_path:
            raise JobNotFound("預覽尚未就緒")
        path = Path(page.preview_path)
        if not path.is_file():
            raise JobNotFound("預覽不存在")
        return path


def action(job_id, owner_id, action):
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        job = _owned(session, job_id, owner_id)
        if action == "cancel":
            if job.status == "completed":
                raise JobConflict("此工作目前無法取消")
            job.cancel_requested = True
            job.status = "cancelling" if job.status in ("running", "cancelling") else "cancelled"
            job.phase = job.status
        elif action in ("resume", "retry-failed"):
            allowed = ("interrupted", "cancelled") if action == "resume" else ("partial_failed", "failed", "awaiting_review")
            if job.status not in allowed:
                raise JobConflict("此工作目前無法續跑或重試")
            if _sequential(job):
                _check_available(session, owner_id, exclude=job.id)
            snapshot = json.loads(job.settings_json)
            with ocr_policy.isolated_policy(snapshot.get("ocr_policy", {})):
                if job.version != model_invocation.source_versions()["execution_version_sha256"]:
                    raise JobConflict("執行版本已改變，請建立新工作")
            for page in session.exec(select(ImportPage).where(ImportPage.job_id == job.id)).all():
                if page.status == "running" or (action == "retry-failed" and page.status == "failed"):
                    page.status, page.phase, page.error = "queued", "queued", ""
                    session.add(page)
            if action == "retry-failed":
                for file in session.exec(select(ImportFile).where(ImportFile.job_id == job.id)).all():
                    if file.error:
                        file.indexed, file.error = False, ""
                        session.add(file)
            waiting = session.exec(select(ImportPage).where(ImportPage.job_id == job.id, ImportPage.status == "preview_ready")).first()
            state = "awaiting_review" if _sequential(job) and waiting else "queued"
            job.status, job.phase, job.cancel_requested, job.error = state, state, False, ""
        else:
            raise ValueError("未知的工作操作")
        job.updated_at = utc_now()
        session.add(job)
        session.commit()
    _wake.set()
    return get_job(job_id, owner_id)


def recover_interrupted():
    with get_session() as session:
        for job in session.exec(select(ImportJob).where(ImportJob.status.in_(("queued", "running", "cancelling")))).all():
            job.status, job.phase = "interrupted", "interrupted"
            session.add(job)
        for page in session.exec(select(ImportPage).where(ImportPage.status == "running")).all():
            page.status, page.phase = "queued", "queued"
            session.add(page)
        session.commit()


def _phase(job_id, page_id, phase, preview_path=None):
    with get_session() as session:
        job, page = session.get(ImportJob, job_id), session.get(ImportPage, page_id)
        if not job or not page:
            raise JobConflict("工作已移除")
        job.phase, page.phase, job.updated_at = phase, phase, utc_now()
        if preview_path is not None:
            page.preview_path = str(preview_path)
        session.add(job)
        session.add(page)
        session.commit()


def _cancelled(job_id):
    with get_session() as session:
        return session.get(ImportJob, job_id).cancel_requested or _stop.is_set()


def _run(job_id):
    with get_session() as session:
        job = session.get(ImportJob, job_id)
        snapshot = json.loads(job.settings_json)
        sequential = snapshot.get("sequential_review", False)
        company, owner = job.company, job.owner_id
        files = [file.model_dump() for file in session.exec(select(ImportFile).where(ImportFile.job_id == job_id)).all()]
    with settings_store.isolated_settings(snapshot["models"]), ocr_policy.isolated_policy(snapshot["ocr_policy"]):
        if model_invocation.source_versions()["execution_version_sha256"] != job.version:
            with get_session() as session:
                saved = session.get(ImportJob, job_id)
                saved.status, saved.error = "interrupted", "執行版本已改變，請建立新工作"
                session.add(saved); session.commit()
            return
        # Only metadata is expanded first; expensive PDF rendering remains per page.
        for file in files:
            if file["indexed"] or _cancelled(job_id):
                continue
            try:
                expected = snapshot.get("sources", {}).get(file["path"])
                if expected and _source_hash(file["path"]) != expected:
                    raise JobConflict("來源檔案已改變，請建立新工作")
                count = pipeline.page_count(file["path"])
                if count < 1:
                    raise ValueError("檔案沒有頁面")
                with get_session() as session:
                    saved_file = session.get(ImportFile, file["id"])
                    for page_no in range(1, count + 1):
                        existing = session.exec(select(ImportPage).where(ImportPage.file_id == file["id"], ImportPage.page_no == page_no)).first()
                        if not existing:
                            session.add(ImportPage(id=uuid.uuid4().hex, job_id=job_id, file_id=file["id"], page_no=page_no, source_token=uuid.uuid4().hex))
                    saved_file.indexed = True
                    session.add(saved_file); session.commit()
            except Exception as exc:
                with get_session() as session:
                    saved_file = session.get(ImportFile, file["id"])
                    saved_file.indexed, saved_file.error = True, f"{type(exc).__name__}: {exc}"
                    session.add(saved_file); session.commit()
        with get_session() as session:
            saved = session.get(ImportJob, job_id)
            pages = [p.model_dump() for p in session.exec(select(ImportPage).where(ImportPage.job_id == job_id).order_by(ImportPage.file_id, ImportPage.page_no)).all()]
            saved.total_pages = len(pages)
            session.add(saved); session.commit()
        by_file = {file["id"]: file for file in files}
        for page in pages:
            if _cancelled(job_id):
                break
            if sequential and page["status"] in ("failed", "preview_ready"):
                break
            if page["status"] in ("completed", "failed", "preview_ready"):
                continue
            file = by_file[page["file_id"]]
            with get_session() as session:
                saved_page = session.get(ImportPage, page["id"])
                saved_page.status = "running"
                session.add(saved_page); session.commit()
            try:
                expected = snapshot.get("sources", {}).get(file["path"])
                if expected and _source_hash(file["path"]) != expected:
                    raise JobConflict("來源檔案已改變，請建立新工作")
                operation = json.loads(page.get("preparation_json") or "{}")
                if operation.get("operation") == "layout":
                    _run_layout(job_id, page, file, operation, company)
                    if sequential:
                        break
                    continue
                _phase(job_id, page["id"], "rendering")
                source = pipeline.render_page(file["path"], page["page_no"], page["source_token"])
                manual_mode = snapshot.get("mode", "automatic") == "manual_review"
                _phase(job_id, page["id"], "preview" if manual_mode else "orientation", source)
                with work_progress.capture(lambda phase: _phase(job_id, page["id"], phase)):
                    prepare = pipeline.prepare_manual_page if manual_mode else pipeline.prepare_page
                    vouchers = prepare(source, file["path"], page["page_no"], token=page["source_token"], group_id=file["group_id"])
                if sequential and not manual_mode and vouchers and not any(v.origin.get("record_role") != "source_page" for v in vouchers) and not vouchers[0].origin.get("layout", {}).get("auto_split"):
                    # An uncertain automatic layout still needs an editable copy;
                    # the immutable source cannot serve as the manual split remainder.
                    from app.core import preparation
                    working = vouchers[0].model_copy(deep=True)
                    working.origin.pop("record_role", None)
                    working.origin.pop("layout", None)
                    working.origin["preparation"] = preparation.fresh()
                    vouchers.append(working)
                for voucher in vouchers:
                    voucher.source_file = f"{file['name']} p{page['page_no']}"
                    if manual_mode or sequential:
                        voucher.origin.update(import_job_id=job_id, import_page_id=page["id"])
                if expected and _source_hash(file["path"]) != expected:
                    raise JobConflict("來源檔案在處理期間改變，請建立新工作")
                with get_session() as session:
                    saved_page = session.get(ImportPage, page["id"])
                    if saved_page.status == "completed":
                        continue
                    try:
                        records = review.save_prepared_page(vouchers, company, session)
                        saved_page.source_page_id = next((r.id for r in records if r.record_role == "source_page"), None)
                        parent = next((r for r in records if r.record_role == "source_page"), None)
                        if parent and parent.image_path:
                            saved_page.preview_path = str(config.UPLOAD_DIR / parent.image_path)
                        saved_page.voucher_ids_json = json.dumps([r.id for r in records if r.record_role != "source_page"])
                        needs_review = manual_mode or (sequential and (json.loads(saved_page.voucher_ids_json) or not vouchers[0].origin.get("layout", {}).get("auto_split")))
                        saved_page.status = "preview_ready" if needs_review else "completed"
                        saved_page.phase = "awaiting_review" if needs_review else "completed"
                        if needs_review:
                            saved_page.preparation_json = json.dumps({"operation":"review", "preview_ready_at":utc_now().isoformat()})
                        saved = session.get(ImportJob, job_id)
                        if saved.first_result_seconds is None:
                            saved.first_result_seconds = (utc_now() - saved.created_at).total_seconds()
                        session.add(saved_page); session.add(saved); session.commit()
                    except Exception:
                        session.rollback()
                        for image in session.info.get("import_images", []):
                            try:
                                image.unlink(missing_ok=True)
                            except OSError:
                                logger.warning("Uncommitted import image cleanup failed")
                        raise
                if sequential and needs_review:
                    break
            except Exception as exc:
                logger.exception("Import page failed: %s", page["id"])
                with get_session() as session:
                    saved_page = session.get(ImportPage, page["id"])
                    saved_page.status, saved_page.phase, saved_page.error = "failed", "failed", f"{type(exc).__name__}: {exc}"
                    session.add(saved_page); session.commit()
                _clear_layout_pending(page)
                if sequential:
                    break
        with get_session() as session:
            saved = session.get(ImportJob, job_id)
            pages = session.exec(select(ImportPage).where(ImportPage.job_id == job_id)).all()
            file_errors = session.exec(select(ImportFile).where(ImportFile.job_id == job_id, ImportFile.error != "")).all()
            files = session.exec(select(ImportFile).where(ImportFile.job_id == job_id)).all()
            success = sum(p.status == "completed" for p in pages)
            failed = sum(p.status == "failed" for p in pages) + len(file_errors)
            remaining = any(p.status in ("queued", "running") for p in pages) or any(not f.indexed for f in files)
            if (sequential and saved.cancel_requested) or (remaining and (saved.cancel_requested or _stop.is_set())):
                saved.status = "interrupted" if _stop.is_set() else "cancelled"
            elif sequential and failed:
                saved.status = "partial_failed" if success else "failed"
            elif sequential and any(p.status == "preview_ready" for p in pages):
                saved.status = "awaiting_review"
            elif remaining:
                saved.status = "queued"
            else:
                saved.status = ("partial_failed" if success else "failed") if failed else "awaiting_review" if any(p.status == "preview_ready" for p in pages) else "completed"
            saved.phase = saved.status
            saved.error = "；".join(f.error for f in file_errors)
            session.add(saved); session.commit()


def queue_layout(session, rec, actor_id):
    """Called inside the appearance transaction; no model runs in the request."""
    from app.core import preparation
    data = json.loads(rec.current_json)
    origin = data.get("origin") or {}
    page = session.get(ImportPage, origin.get("import_page_id", ""))
    job = _owned(session, origin.get("import_job_id", ""), actor_id)
    if page is None or page.job_id != job.id:
        raise JobConflict("找不到原始匯入頁面")
    if job.status in ("cancelled", "interrupted", "cancelling"):
        raise JobConflict("請先續跑匯入工作")
    snapshot = json.loads(job.settings_json)
    with ocr_policy.isolated_policy(snapshot["ocr_policy"]):
        if model_invocation.source_versions()["execution_version_sha256"] != job.version:
            raise JobConflict("執行版本已改變，請建立新工作")
    page.preparation_json = json.dumps({"operation":"layout","record_id":rec.id,
        "expected_updated_at":rec.updated_at.isoformat(), "image_sha256":preparation.fingerprint(config.UPLOAD_DIR / rec.image_path)})
    page.status, page.phase = "queued", "layout_queued"
    job.status, job.phase = "queued", "layout_queued"
    session.add(page); session.add(job)
    _wake.set()


def check_review_turn(session, rec, actor_id=None):
    """A new sequential import only accepts its current page's working copy."""
    origin = json.loads(rec.current_json).get("origin") or {}
    job = session.get(ImportJob, origin.get("import_job_id", ""))
    if not job or not _sequential(job):
        return
    if actor_id is not None and job.owner_id != actor_id:
        raise JobNotFound("匯入工作不存在")
    if job.cancel_requested or job.status in ("cancelled", "cancelling", "interrupted", "failed", "partial_failed"):
        raise JobConflict("請先續跑或重試目前匯入，再進行外觀確認")
    _check_available(session, job.owner_id, exclude=job.id)
    pages = session.exec(select(ImportPage).where(ImportPage.job_id == job.id).order_by(ImportPage.file_id, ImportPage.page_no)).all()
    current = next((p for p in pages if p.status != "completed"), None)
    if current and current.id != origin.get("import_page_id"):
        raise JobConflict("請先完成目前頁面的外觀確認")


def mark_review_complete(session, rec, *, wake=True):
    origin = json.loads(rec.current_json).get("origin") or {}
    page = session.get(ImportPage, origin.get("import_page_id", ""))
    if page is None or page.status != "preview_ready":
        return
    ids = json.loads(page.voucher_ids_json)
    if all((item := session.get(review.VoucherRecord, ident)) is None or item.status != review.ReviewStatus.prepared or item.record_role == "source_page" for ident in ids):
        page.status, page.phase = "completed", "completed"
        session.add(page)
    job = session.get(ImportJob, page.job_id)
    other = session.exec(select(ImportPage).where(ImportPage.job_id == page.job_id)).all()
    if job and _sequential(job) and not job.cancel_requested:
        if page.status == "completed":
            state = "awaiting_review" if any(p.status == "preview_ready" for p in other) else "partial_failed" if any(p.status == "failed" for p in other) else "queued" if any(p.status == "queued" for p in other) else "completed"
            job.status, job.phase = state, state
            job.updated_at = utc_now()
            session.add(job)
            if wake:
                _wake.set()
    elif job and job.status == "awaiting_review" and all(p.status in ("completed","failed") for p in other):
        job.status, job.phase = "completed", "completed"
        session.add(job)


def _clear_layout_pending(page):
    operation = json.loads(page.get("preparation_json") or "{}")
    if not operation.get("record_id"):
        return
    with get_session() as session:
        rec = session.get(review.VoucherRecord, operation["record_id"])
        if rec:
            data = json.loads(rec.current_json)
            prep = data.get("origin",{}).get("preparation",{})
            prep["layout_pending"] = False
            rec.current_json = json.dumps(data, ensure_ascii=False)
            session.add(rec); session.commit()


def _run_layout(job_id, page, file, operation, company):
    from app.core import preparation
    rec = review.get_voucher(operation["record_id"])
    if not rec or rec.updated_at.isoformat() != operation["expected_updated_at"]:
        raise JobConflict("版面處理前影像版本已改變")
    source = config.UPLOAD_DIR / rec.image_path
    current = json.loads(rec.current_json)
    if not preparation.confirmed(current, source) or preparation.fingerprint(source) != operation["image_sha256"]:
        raise JobConflict("方向確認已失效")
    _phase(job_id, page["id"], "layout")
    with work_progress.capture(lambda phase: _phase(job_id, page["id"], phase)):
        vouchers = pipeline.prepare_page(str(source), file["path"], page["page_no"], token=page["source_token"],
            group_id=file["group_id"], confirmed_preparation=preparation.state(current))
    layout = vouchers[0].origin["layout"]
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        saved = session.get(review.VoucherRecord, rec.id)
        saved_page = session.get(ImportPage, page["id"])
        if saved is None or saved_page is None or saved.updated_at != rec.updated_at or preparation.fingerprint(source) != operation["image_sha256"]:
            raise JobConflict("版面處理期間影像已變更")
        if saved_page.status == "completed":
            return
        data = json.loads(saved.current_json)
        data["origin"]["preparation"]["layout_pending"] = False
        data["origin"]["layout"] = layout
        child_ids = []
        if layout["auto_split"]:
            saved.record_role = "source_page"
            data["origin"]["record_role"] = "source_page"
            for voucher in vouchers[1:]:
                voucher.origin.update(import_job_id=job_id, import_page_id=page["id"])
                voucher.source_file = saved.source_file
                child = review.save_voucher(voucher, company, review.ReviewStatus.prepared, source_parent_id=rec.id, session=session)
                child_ids.append(child.id)
            data["quality"] = vouchers[0].quality
            waiting = _sequential(session.get(ImportJob, job_id)) and bool(child_ids)
            saved_page.status, saved_page.phase = ("preview_ready", "awaiting_review") if waiting else ("completed", "completed")
        else:
            data["quality"] = {"version":1,"state":"needs_review","content_kind":"unknown","issues":layout["issues"]}
            data["origin"]["preparation"]["document_mode"] = "manual_split"
            saved_page.status, saved_page.phase = "preview_ready", "awaiting_review"
            child_ids = [rec.id]
        saved.current_json = json.dumps(data, ensure_ascii=False)
        saved.updated_at = datetime.now()
        saved_page.voucher_ids_json = json.dumps(child_ids)
        saved_page.preparation_json = json.dumps({"operation":"review"})
        session.add(saved); session.add(saved_page)
        try:
            session.commit()
        except Exception:
            session.rollback()
            for image in session.info.get("import_images", []):
                image.unlink(missing_ok=True)
            raise


def run_once():
    if not _run_lock.acquire(blocking=False):
        return False
    try:
        with get_session() as session:
            job = session.exec(select(ImportJob).where(ImportJob.status == "queued").order_by(ImportJob.created_at)).first()
            if not job:
                return False
            job_id = job.id
            claimed = session.execute(update(ImportJob).where(ImportJob.id == job_id, ImportJob.status == "queued").values(status="running", started_at=utc_now()))
            session.commit()
            if not claimed.rowcount:
                return False
        try:
            _run(job_id)
        except Exception:
            logger.exception("Import worker interrupted")
            with get_session() as session:
                job = session.get(ImportJob, job_id)
                job.status, job.error = "interrupted", "背景工作中斷，可續跑"
                session.add(job); session.commit()
        return True
    finally:
        _run_lock.release()


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    recover_interrupted()
    _stop.clear()
    def loop():
        while not _stop.is_set():
            if not run_once():
                _wake.wait(1); _wake.clear()
    _thread = threading.Thread(target=loop, name="page-import", daemon=True)
    _thread.start()


def stop():
    _stop.set()
    _wake.set()
