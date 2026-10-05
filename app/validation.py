"""Dataset validation API, persistent queue, recognition runner and exports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import multiprocessing as mp
import os
import platform
import shutil
import sqlite3
import statistics
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont

from app import config

logger = logging.getLogger("pdf_scan.validation")
router = APIRouter(prefix="/api/validation", tags=["validation"])

ROOT = config.DATA_DIR / "validation"
DB = ROOT / "validation.db"
DATASETS = ROOT / "datasets"
RUNS = ROOT / "runs"
MAX_FILE = int(os.getenv("VALIDATION_MAX_FILE_MB", "100")) * 1024 * 1024
MAX_TOTAL = int(os.getenv("VALIDATION_MAX_TOTAL_MB", "2048")) * 1024 * 1024
MAX_FILES = int(os.getenv("VALIDATION_MAX_FILES", "5000"))
SUPPORTED = config.ALLOWED_SUFFIXES

_db_lock = threading.RLock()
_wake = threading.Event()
_scheduler_thread = None
_active_lock = threading.Lock()
_active_process = None
_active_run_id = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_validation() -> None:
    global _scheduler_thread
    for path in (ROOT, DATASETS, RUNS):
        path.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS datasets (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
          file_count INTEGER DEFAULT 0, page_count INTEGER DEFAULT 0,
          errors_json TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS pages (
          id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, voucher_key TEXT NOT NULL,
          relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, page_no INTEGER NOT NULL,
          original_path TEXT NOT NULL, page_path TEXT NOT NULL, render_seconds REAL DEFAULT 0,
          FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, name TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          settings_json TEXT NOT NULL, total INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
          succeeded INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, current TEXT DEFAULT '',
          cancel_requested INTEGER DEFAULT 0, FOREIGN KEY(dataset_id) REFERENCES datasets(id)
        );
        CREATE TABLE IF NOT EXISTS results (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, page_id TEXT NOT NULL,
          combo_id TEXT NOT NULL, combo_name TEXT NOT NULL, mode TEXT NOT NULL,
          repeat_no INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
          warmup INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
          queued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          actual_path TEXT DEFAULT '', voucher_json TEXT DEFAULT '{}', timings_json TEXT DEFAULT '{}',
          trace_json TEXT DEFAULT '[]', ocr_json TEXT DEFAULT '[]', boxes_json TEXT DEFAULT '{}',
          error TEXT DEFAULT '', result_dir TEXT DEFAULT '',
          FOREIGN KEY(run_id) REFERENCES runs(id), FOREIGN KEY(page_id) REFERENCES pages(id)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, result_id TEXT NOT NULL, kind TEXT NOT NULL,
          path TEXT NOT NULL, media_type TEXT NOT NULL,
          FOREIGN KEY(result_id) REFERENCES results(id)
        );
        CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_pages_dataset ON pages(dataset_id);
        """)
        con.execute("UPDATE runs SET status='interrupted', current='' WHERE status='running'")
        con.execute("UPDATE results SET status='queued', started_at=NULL WHERE status='running'")
    if not _scheduler_thread or not _scheduler_thread.is_alive():
        _scheduler_thread = threading.Thread(target=_scheduler, name="validation-scheduler", daemon=True)
        _scheduler_thread.start()


def _row(row):
    return dict(row) if row else None


def _safe_rel(name: str) -> str:
    value = str(name or "").replace("\\", "/").lstrip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"不安全的相對路徑：{name}")
    return str(path)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_stream(upload: UploadFile, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with target.open("wb") as out:
        while True:
            block = upload.file.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_FILE:
                raise ValueError(f"{upload.filename} 超過單檔限制 {MAX_FILE // 1024 // 1024} MB")
            out.write(block)
    return size


def _expand_zip(zip_path: Path, output: Path) -> list[tuple[str, Path]]:
    items = []
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise ValueError(f"ZIP 超過 {MAX_FILES} 個檔案")
        for info in infos:
            rel = _safe_rel(info.filename)
            # Unix symlink flag in external attributes.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"ZIP 不允許符號連結：{rel}")
            if info.file_size > MAX_FILE or total + info.file_size > MAX_TOTAL:
                raise ValueError("ZIP 解壓大小超過驗證資料集限制")
            target = output / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                written = 0
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > MAX_FILE or total + written > MAX_TOTAL:
                        raise ValueError("ZIP 實際解壓大小超過驗證資料集限制")
                    dst.write(block)
            total += written
            items.append((rel, target))
    return items


def _register_source(con, dataset_id: str, rel: str, original: Path, pages_dir: Path):
    suffix = original.suffix.lower()
    if suffix not in SUPPORTED:
        return 0, f"不支援：{rel}"
    rendered = []
    started = time.perf_counter()
    try:
        if suffix == ".pdf":
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(original))
            for index in range(len(pdf)):
                image = pdf[index].render(scale=200 / 72).to_pil().convert("RGB")
                page_path = pages_dir / f"{uuid.uuid4().hex}.jpg"
                image.save(page_path, quality=95)
                rendered.append((index + 1, page_path))
        else:
            with Image.open(original) as image:
                image.verify()
            rendered.append((1, original))
    except Exception as exc:
        return 0, f"無法讀取 {rel}：{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    sha = _sha(original)
    per_page_render = elapsed / len(rendered) if rendered else 0
    for page_no, page_path in rendered:
        pid = uuid.uuid4().hex
        voucher_key = hashlib.sha256(f"{sha}:{page_no}".encode()).hexdigest()[:20]
        con.execute("""INSERT INTO pages
          (id,dataset_id,voucher_key,relative_path,sha256,page_no,original_path,page_path,render_seconds)
          VALUES (?,?,?,?,?,?,?,?,?)""",
          (pid, dataset_id, voucher_key, rel, sha, page_no, str(original), str(page_path), per_page_render))
    return len(rendered), None


@router.post("/datasets")
def upload_dataset(name: str = Form(""), paths: str = Form("[]"),
                   files: list[UploadFile] = File(...)):
    dataset_id = uuid.uuid4().hex
    base = DATASETS / dataset_id
    originals, pages_dir = base / "originals", base / "pages"
    originals.mkdir(parents=True)
    pages_dir.mkdir(parents=True)
    try:
        rels = json.loads(paths or "[]")
    except json.JSONDecodeError:
        rels = []
    if len(files) > MAX_FILES:
        return JSONResponse({"detail": f"一次最多 {MAX_FILES} 個檔案"}, status_code=400)
    received, errors, total = [], [], 0
    try:
        for index, upload in enumerate(files):
            rel = _safe_rel(rels[index] if index < len(rels) else upload.filename)
            target = originals / rel
            total += _copy_stream(upload, target)
            if total > MAX_TOTAL:
                raise ValueError("資料集總大小超過限制")
            if target.suffix.lower() == ".zip":
                received.extend(_expand_zip(target, originals / f"zip_{index}"))
            else:
                received.append((rel, target))
    except (ValueError, zipfile.BadZipFile) as exc:
        shutil.rmtree(base, ignore_errors=True)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    with _db_lock, _connect() as con:
        con.execute("INSERT INTO datasets(id,name,created_at) VALUES(?,?,?)",
                    (dataset_id, name.strip() or f"資料集 {dataset_id[:8]}", _now()))
        page_count = 0
        for rel, path in received:
            count, err = _register_source(con, dataset_id, rel, path, pages_dir)
            page_count += count
            if err:
                errors.append(err)
        con.execute("UPDATE datasets SET file_count=?,page_count=?,errors_json=? WHERE id=?",
                    (len(received), page_count, json.dumps(errors, ensure_ascii=False), dataset_id))
    return get_dataset(dataset_id)


@router.get("/datasets")
def list_datasets():
    with _connect() as con:
        rows = con.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
    return {"items": [_dataset_dict(x) for x in rows]}


def _dataset_dict(row):
    item = _row(row)
    item["errors"] = json.loads(item.pop("errors_json") or "[]")
    return item


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str):
    with _connect() as con:
        ds = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        pages = con.execute("""SELECT id,voucher_key,relative_path,sha256,page_no,render_seconds
                             FROM pages WHERE dataset_id=? ORDER BY relative_path,page_no""",
                            (dataset_id,)).fetchall()
    if not ds:
        return JSONResponse({"detail": "找不到資料集"}, status_code=404)
    return {**_dataset_dict(ds), "pages": [_row(x) for x in pages]}


def _normalize_combos(payload: dict):
    from app.core import model_factory

    allowed = {"vlm_direct", "ocr_vlm", "ocr_llm"}
    combos = []
    for idx, raw in enumerate(payload.get("combinations") or []):
        mode = raw.get("mode")
        structure = str(raw.get("structure_model") or "")
        classify = str(raw.get("classify_model") or structure)
        if mode not in allowed or "/" not in structure or "/" not in classify:
            raise ValueError(f"第 {idx + 1} 組模型設定不完整")
        if mode in ("vlm_direct", "ocr_vlm"):
            invalid = list(dict.fromkeys(
                m for m in (structure, classify) if not model_factory.is_vision_model(m)))
            if invalid:
                raise ValueError(f"第 {idx + 1} 組的 VLM 模式只能選擇可讀取圖片的模型：{', '.join(invalid)}")
        elif any(model_factory.is_embedding_model(m) for m in (structure, classify)):
            raise ValueError(f"第 {idx + 1} 組不能使用向量嵌入模型進行辨識或分類")
        combos.append({"id": str(raw.get("id") or f"combo-{idx + 1}"),
                       "name": str(raw.get("name") or f"模型組合 {idx + 1}"),
                       "mode": mode, "structure_model": structure,
                       "classify_model": classify})
    if not combos:
        raise ValueError("至少需要一組模型設定")
    return combos


@router.post("/runs")
def create_run(payload: dict):
    try:
        combos = _normalize_combos(payload)
        repeats = max(1, min(10, int(payload.get("repeats", 1))))
        timeout = max(30, min(3600, int(payload.get("timeout_seconds", 600))))
        max_edge = max(640, min(2400, int(payload.get("max_edge", 1180))))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    dataset_id = str(payload.get("dataset_id") or "")
    with _db_lock, _connect() as con:
        ds = con.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        pages = con.execute("SELECT * FROM pages WHERE dataset_id=? ORDER BY relative_path,page_no",
                            (dataset_id,)).fetchall()
        if not ds or not pages:
            return JSONResponse({"detail": "資料集不存在或沒有可辨識頁面"}, status_code=400)
        rid = uuid.uuid4().hex
        prompt_material = (Path(__file__).with_name("core").joinpath("doc_router.py").read_bytes()
                           + Path(__file__).with_name("core").joinpath("doc_types.py").read_bytes())
        code_material = prompt_material + Path(__file__).read_bytes() + Path(__file__).with_name("core").joinpath("pipeline.py").read_bytes()
        settings = {"combinations": combos, "repeats": repeats,
                    "warmup": bool(payload.get("warmup", False)), "timeout_seconds": timeout,
                    "max_edge": max_edge, "concurrency": 1,
                    "prompt_version_sha256": hashlib.sha256(prompt_material).hexdigest(),
                    "code_version_sha256": hashlib.sha256(code_material).hexdigest(),
                    "model_parameters": {"temperature": 0, "seed": 42},
                    "environment": {"python": platform.python_version(), "platform": platform.platform(),
                                    "processor": platform.processor()}}
        tasks = []
        for combo in combos:
            if settings["warmup"]:
                tasks.append((pages[0], combo, 0, 1))
            for repeat in range(1, repeats + 1):
                tasks.extend((page, combo, repeat, 0) for page in pages)
        con.execute("""INSERT INTO runs
          (id,dataset_id,name,status,created_at,settings_json,total)
          VALUES(?,?,?,?,?,?,?)""", (rid, dataset_id, str(payload.get("name") or f"驗證 {rid[:8]}"),
          "queued", _now(), json.dumps(settings, ensure_ascii=False), len(tasks)))
        for page, combo, repeat, warmup in tasks:
            con.execute("""INSERT INTO results
              (id,run_id,page_id,combo_id,combo_name,mode,repeat_no,warmup,status,queued_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (uuid.uuid4().hex, rid, page["id"], combo["id"], combo["name"], combo["mode"],
               repeat, warmup, "queued", _now()))
    _wake.set()
    return get_run(rid)


@router.get("/runs")
def list_runs():
    with _connect() as con:
        rows = con.execute("""SELECT r.*,d.name dataset_name FROM runs r
                            JOIN datasets d ON d.id=r.dataset_id ORDER BY r.created_at DESC""").fetchall()
    return {"items": [_run_dict(x, include_settings=False) for x in rows]}


def _run_dict(row, include_settings=True):
    item = _row(row)
    settings = json.loads(item.pop("settings_json") or "{}")
    if include_settings:
        item["settings"] = settings
    item["progress"] = item["done"] / item["total"] if item["total"] else 0
    return item


def _percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def _statistics(con, run_id):
    rows = con.execute("""SELECT combo_id,combo_name,mode,status,actual_path,timings_json,warmup
                        FROM results WHERE run_id=? ORDER BY rowid""", (run_id,)).fetchall()
    groups = {}
    for row in rows:
        key = (row["combo_id"], row["actual_path"] or row["mode"])
        g = groups.setdefault(key, {"combo_id": row["combo_id"], "combo_name": row["combo_name"],
                                    "actual_path": key[1], "samples": 0, "succeeded": 0,
                                    "failed": 0, "warmups": 0, "times": [], "wall_seconds": 0,
                                    "first_seconds": None})
        timing = json.loads(row["timings_json"] or "{}")
        if row["warmup"]:
            g["warmups"] += 1
            continue
        if row["status"] not in ("succeeded", "failed", "timeout"):
            continue
        g["samples"] += 1
        total = float(timing.get("complete_seconds") or 0)
        g["wall_seconds"] += total
        if row["status"] == "succeeded":
            g["succeeded"] += 1
            g["times"].append(float(timing.get("recognition_seconds") or total))
            if g["first_seconds"] is None:
                g["first_seconds"] = float(timing.get("recognition_seconds") or total)
        elif row["status"] in ("failed", "timeout"):
            g["failed"] += 1
    output = []
    for g in groups.values():
        vals = g.pop("times")
        g.update({"success_rate": g["succeeded"] / g["samples"] if g["samples"] else None,
                  "average_seconds": statistics.fmean(vals) if vals else None,
                  "p50_seconds": _percentile(vals, .5), "p95_seconds": _percentile(vals, .95),
                  "throughput_per_minute": (g["succeeded"] * 60 / g["wall_seconds"]
                                             if g["wall_seconds"] else None)})
        output.append(g)
    return output


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    with _connect() as con:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return JSONResponse({"detail": "找不到驗證批次"}, status_code=404)
        stats = _statistics(con, run_id)
    return {**_run_dict(run), "statistics": stats}


@router.get("/runs/{run_id}/results")
def list_results(run_id: str, combo_id: str = "", status: str = "", source: str = "",
                 page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500)):
    where, args = ["r.run_id=?"], [run_id]
    if combo_id:
        where.append("r.combo_id=?"); args.append(combo_id)
    if status:
        where.append("r.status=?"); args.append(status)
    if source:
        where.append("p.relative_path LIKE ?"); args.append(f"%{source}%")
    clause = " AND ".join(where)
    with _connect() as con:
        total = con.execute(f"SELECT COUNT(*) FROM results r JOIN pages p ON p.id=r.page_id WHERE {clause}", args).fetchone()[0]
        rows = con.execute(f"""SELECT r.id,r.combo_id,r.combo_name,r.mode,r.repeat_no,r.attempt,r.warmup,
          r.status,r.actual_path,r.started_at,r.finished_at,r.timings_json,r.error,
          p.relative_path,p.page_no,p.voucher_key FROM results r JOIN pages p ON p.id=r.page_id
          WHERE {clause} ORDER BY p.relative_path,p.page_no,r.combo_id,r.repeat_no,r.attempt
          LIMIT ? OFFSET ?""", (*args, page_size, (page - 1) * page_size)).fetchall()
    items = []
    for row in rows:
        item = _row(row); item["timings"] = json.loads(item.pop("timings_json") or "{}")
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/results/{result_id}")
def get_result(result_id: str):
    with _connect() as con:
        result = con.execute("""SELECT r.*,p.relative_path,p.page_no,p.voucher_key,p.sha256,p.render_seconds
          FROM results r JOIN pages p ON p.id=r.page_id WHERE r.id=?""", (result_id,)).fetchone()
        arts = con.execute("SELECT id,kind,media_type FROM artifacts WHERE result_id=? ORDER BY kind",
                           (result_id,)).fetchall()
    if not result:
        return JSONResponse({"detail": "找不到結果"}, status_code=404)
    item = _row(result)
    for key in ("voucher_json", "timings_json", "trace_json", "ocr_json", "boxes_json"):
        item[key.removesuffix("_json")] = json.loads(item.pop(key) or ("[]" if key in ("trace_json", "ocr_json") else "{}"))
    item["artifacts"] = [{**_row(x), "url": f"/api/validation/artifacts/{x['id']}"} for x in arts]
    return item


def _set_run_action(run_id, action):
    global _active_process
    with _db_lock, _connect() as con:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return JSONResponse({"detail": "找不到驗證批次"}, status_code=404)
        if run["status"] == "terminated":
            return JSONResponse({"detail": "此批次已終止，無法續跑或重跑"}, status_code=409)
        if action == "terminate":
            if run["status"] not in ("queued", "running", "cancelling", "cancelled", "interrupted"):
                return JSONResponse({"detail": "此批次已完成，無須終止"}, status_code=409)
            # Persist the final state while holding the same lock used by the
            # scheduler. A late child response must not revive this result.
            with _active_lock:
                if _active_run_id == run_id and _active_process and _active_process.is_alive():
                    _active_process.terminate()
            con.execute("""UPDATE results SET status='terminated',finished_at=?
                           WHERE run_id=? AND status IN ('queued','running','cancelled')""",
                        (_now(), run_id))
            counts = {status: count for status, count in con.execute(
                "SELECT status,COUNT(*) FROM results WHERE run_id=? GROUP BY status", (run_id,))}
            con.execute("""UPDATE runs SET status='terminated',cancel_requested=1,
                           finished_at=?,current='',done=?,succeeded=?,failed=? WHERE id=?""",
                        (_now(), sum(counts.get(x, 0) for x in ("succeeded", "failed", "timeout")),
                         counts.get("succeeded", 0), counts.get("failed", 0) + counts.get("timeout", 0),
                         run_id))
        elif action == "cancel":
            con.execute("UPDATE runs SET cancel_requested=1,status='cancelling' WHERE id=?", (run_id,))
            with _active_lock:
                if _active_run_id == run_id and _active_process and _active_process.is_alive():
                    _active_process.terminate()
        elif action == "resume":
            con.execute("UPDATE results SET status='queued',finished_at=NULL,error='' WHERE run_id=? AND status='cancelled'",
                        (run_id,))
            con.execute("UPDATE runs SET cancel_requested=0,status='queued',finished_at=NULL WHERE id=?", (run_id,))
        elif action == "retry":
            if run["status"] in ("queued", "running", "cancelling"):
                return JSONResponse({"detail": "批次執行中，請完成或取消後再重跑失敗項目"}, status_code=409)
            failed = con.execute("SELECT * FROM results WHERE run_id=? AND status IN ('failed','timeout')", (run_id,)).fetchall()
            for old in failed:
                con.execute("""INSERT INTO results
                  (id,run_id,page_id,combo_id,combo_name,mode,repeat_no,attempt,warmup,status,queued_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (uuid.uuid4().hex, run_id, old["page_id"], old["combo_id"], old["combo_name"],
                   old["mode"], old["repeat_no"], old["attempt"] + 1, old["warmup"], "queued", _now()))
            con.execute("UPDATE runs SET total=total+?,cancel_requested=0,status='queued',finished_at=NULL WHERE id=?",
                        (len(failed), run_id))
    _wake.set()
    return get_run(run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str): return _set_run_action(run_id, "cancel")


@router.post("/runs/{run_id}/terminate")
def terminate_run(run_id: str): return _set_run_action(run_id, "terminate")


@router.post("/runs/{run_id}/resume")
def resume_run(run_id: str): return _set_run_action(run_id, "resume")


@router.post("/runs/{run_id}/retry-failed")
def retry_run(run_id: str): return _set_run_action(run_id, "retry")


def _child_entry(conn, payload):
    try:
        conn.send({"ok": True, "data": _recognize(payload)})
    except BaseException as exc:  # child must return diagnostic details
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()})
    finally:
        conn.close()


def _run_child(payload, timeout):
    global _active_process, _active_run_id
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(False)
    process = ctx.Process(target=_child_entry, args=(child, payload), daemon=True)
    # The scheduler may have selected this item just before a terminate request.
    # Check and start under the same lock order as _set_run_action so the child
    # cannot be launched after the batch has become terminal.
    with _db_lock:
        with _connect() as con:
            run = con.execute("SELECT status FROM runs WHERE id=?", (payload["run_id"],)).fetchone()
        if not run or run["status"] != "running":
            parent.close(); child.close()
            return {"ok": False, "error": "批次已停止", "wall_seconds": 0}
        with _active_lock:
            _active_process, _active_run_id = process, payload["run_id"]
            process.start()
    child.close()
    wall_started = time.perf_counter()
    try:
        if parent.poll(timeout):
            response = parent.recv()
            process.join(5)
        else:
            process.terminate(); process.join(10)
            response = {"ok": False, "timeout": True, "error": f"單張處理超過 {timeout} 秒"}
    except (EOFError, OSError) as exc:
        response = {"ok": False, "error": f"辨識子行程中斷：{exc}"}
    response["wall_seconds"] = round(time.perf_counter() - wall_started, 6)
    parent.close()
    with _active_lock:
        _active_process, _active_run_id = None, None
    return response


def _scheduler():
    while True:
        task = None
        with _db_lock, _connect() as con:
            run = con.execute("""SELECT * FROM runs WHERE status IN ('queued','running')
                               AND cancel_requested=0 ORDER BY created_at LIMIT 1""").fetchone()
            if run:
                task = con.execute("""SELECT r.*,p.relative_path,p.page_no,p.page_path,p.original_path,
                  p.sha256,p.render_seconds FROM results r JOIN pages p ON p.id=r.page_id
                  WHERE r.run_id=? AND r.status='queued' ORDER BY r.rowid LIMIT 1""", (run["id"],)).fetchone()
                if task:
                    con.execute("UPDATE runs SET status='running',started_at=COALESCE(started_at,?),current=? WHERE id=?",
                                (_now(), f"{task['relative_path']} p{task['page_no']}", run["id"]))
                    started_at = _now()
                    con.execute("UPDATE results SET status='running',started_at=? WHERE id=?", (started_at, task["id"]))
                    task = dict(task)
                    task["started_at"] = started_at
        if not task:
            _finalize_runs()
            _wake.wait(2); _wake.clear()
            continue
        task = _row(task)
        with _connect() as con:
            run = con.execute("SELECT settings_json FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        settings = json.loads(run["settings_json"])
        combo = next(c for c in settings["combinations"] if c["id"] == task["combo_id"])
        payload = {**task, "combo": combo, "max_edge": settings["max_edge"], "run_id": task["run_id"]}
        response = _run_child(payload, settings["timeout_seconds"])
        _save_response(task, response)
        _finalize_runs()


def _save_response(task, response):
    persistence_started = time.perf_counter()
    data = response.get("data") or {}
    if not data.get("timings"):
        data["timings"] = {"complete_seconds": response.get("wall_seconds", 0),
                           "recognition_seconds": response.get("wall_seconds", 0)}
    try:
        queued = datetime.fromisoformat(task["queued_at"])
        started = datetime.fromisoformat(task["started_at"])
        data["timings"]["queue_seconds"] = round((started - queued).total_seconds(), 6)
    except (TypeError, ValueError):
        data["timings"]["queue_seconds"] = None
    with _db_lock, _connect() as con:
        run = con.execute("SELECT status,cancel_requested FROM runs WHERE id=?", (task["run_id"],)).fetchone()
        if run["status"] == "terminated":
            return
        status = ("cancelled" if run["cancel_requested"] else "succeeded" if response.get("ok")
                  else "timeout" if response.get("timeout") else "failed")
        con.execute("""UPDATE results SET status=?,finished_at=?,actual_path=?,voucher_json=?,
          timings_json=?,trace_json=?,ocr_json=?,boxes_json=?,error=?,result_dir=? WHERE id=?""",
          (status, _now(), data.get("actual_path", ""), json.dumps(data.get("voucher", {}), ensure_ascii=False),
           json.dumps(data.get("timings", {}), ensure_ascii=False), json.dumps(data.get("trace", []), ensure_ascii=False),
           json.dumps(data.get("ocr", []), ensure_ascii=False), json.dumps(data.get("boxes", {}), ensure_ascii=False),
           (response.get("error", "") + ("\n" + response["traceback"] if response.get("traceback") else "")),
           data.get("result_dir", ""), task["id"]))
        for kind, info in (data.get("artifacts") or {}).items():
            con.execute("INSERT INTO artifacts(id,result_id,kind,path,media_type) VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, task["id"], kind, info["path"], info["media_type"]))
        persistence = time.perf_counter() - persistence_started
        data["timings"]["persistence_seconds"] = round(persistence, 6)
        data["timings"]["complete_with_storage_seconds"] = round(
            float(data["timings"].get("complete_seconds") or 0) + persistence, 6)
        con.execute("UPDATE results SET timings_json=? WHERE id=?",
                    (json.dumps(data["timings"], ensure_ascii=False), task["id"]))


def _finalize_runs():
    with _db_lock, _connect() as con:
        runs = con.execute("SELECT * FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()
        for run in runs:
            counts = {row[0]: row[1] for row in con.execute(
                "SELECT status,COUNT(*) FROM results WHERE run_id=? GROUP BY status", (run["id"],)).fetchall()}
            done = sum(counts.get(x, 0) for x in ("succeeded", "failed", "timeout", "cancelled"))
            if run["cancel_requested"]:
                con.execute("UPDATE results SET status='cancelled',finished_at=? WHERE run_id=? AND status='queued'",
                            (_now(), run["id"]))
                con.execute("UPDATE runs SET status='cancelled',finished_at=?,current='' WHERE id=?", (_now(), run["id"]))
            elif not counts.get("queued", 0) and not counts.get("running", 0):
                con.execute("UPDATE runs SET status='completed',finished_at=?,current='' WHERE id=?", (_now(), run["id"]))
            con.execute("UPDATE runs SET done=?,succeeded=?,failed=? WHERE id=?",
                        (done, counts.get("succeeded", 0), counts.get("failed", 0) + counts.get("timeout", 0), run["id"]))


def _font(size=18):
    for name in ("C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/arial.ttf"):
        try: return ImageFont.truetype(name, size)
        except OSError: pass
    return ImageFont.load_default()


def _annotate(source: Path, boxes: dict, output: Path):
    image = Image.open(source).convert("RGB")
    draw, w, h = ImageDraw.Draw(image), image.width, image.height
    colors = ("#0969da", "#1a7f37", "#cf222e", "#8250df", "#bc4c00")
    valid = {}
    for idx, (key, raw) in enumerate((boxes or {}).items()):
        if not isinstance(raw, (list, tuple)) or len(raw) < 4: continue
        try: vals = [max(0, min(1000, float(x))) for x in raw[:4]]
        except (TypeError, ValueError): continue
        if vals[2] <= vals[0] or vals[3] <= vals[1]: continue
        coords = (vals[0] / 1000 * w, vals[1] / 1000 * h, vals[2] / 1000 * w, vals[3] / 1000 * h)
        color = colors[idx % len(colors)]
        draw.rectangle(coords, outline=color, width=max(2, int(max(w, h) / 500)))
        draw.text((coords[0] + 3, max(0, coords[1] - 22)), key, fill=color, font=_font(17), stroke_width=2, stroke_fill="white")
        valid[key] = [round(x) for x in vals]
    image.save(output, quality=94)
    return valid


def _text_classify(ocr_text, model_setting):
    from app.core import activity, doc_router, model_factory, pipeline, settings_store
    provider, model = settings_store.parse_setting(model_setting)
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    prompt = (doc_router.ROUTER_PROMPT.replace("這張掃描件", "以下 OCR 文字所屬的掃描件")
              + "\n純文字模式沒有圖片，rotate 固定回 0。\n\nOCR 文字：\n" + ocr_text)
    with activity.stage("structure", model_setting + "（OCR 純文字判類型）"):
        raw = pipeline._invoke_text(chat, prompt)
    obj = pipeline._parse_json(raw)
    cand = str(obj.get("type", "")).lower()
    from app.core import doc_types
    return cand if cand in doc_types.DOC_TYPES else "other"


def _text_extract(image_path, type_key, ocr_lines, ocr_text, model_setting):
    from app.core import activity, doc_types, model_factory, pipeline, settings_store
    provider, model = settings_store.parse_setting(model_setting)
    chat = model_factory.get_chat_model(provider, model, temperature=0.0)
    prompt = doc_types.build_extract_prompt(type_key)
    prompt = prompt.replace("以下提供原圖與 OCR 文字（供校對，數字以圖為準）。",
                            "以下僅提供 OCR 文字，請勿臆測 OCR 中不存在的內容。")
    prompt = "\n".join(line for line in prompt.splitlines()
                         if "boxes" not in line and "邊界框" not in line and "座標正規化" not in line)
    prompt += "\n純文字模式不可輸出 boxes。\n\nOCR 文字：\n" + ocr_text
    with activity.stage("structure", model_setting + "（OCR 純文字欄位萃取）"):
        raw = pipeline._invoke_text(chat, prompt)
    data = pipeline._parse_json(raw)
    data["boxes"] = pipeline._refine_boxes_with_ocr(data.get("fields") or {}, {}, ocr_lines,
                                                     image_path, keep_unmatched=False)
    if doc_types.get(type_key).get("has_entries"):
        pipeline._fix_entry_sides_with_ocr(data, ocr_lines)
    voucher = pipeline._build_voucher(data, type_key)
    voucher.ocr_provider = "paddle/pp-ocrv5"
    voucher.structure_model = model_setting
    return voucher


def _recognize(payload):
    from app.core import activity, invocation_trace, model_factory, pipeline, settings_store
    result_dir = RUNS / payload["run_id"] / payload["id"]
    result_dir.mkdir(parents=True, exist_ok=True)
    original = Path(payload["page_path"])
    input_path = result_dir / "input.jpg"
    t0 = time.perf_counter()
    with Image.open(original) as image:
        image = image.convert("RGB")
        image.thumbnail((payload["max_edge"], payload["max_edge"]), Image.Resampling.LANCZOS)
        image.save(input_path, quality=95)
    preprocess = time.perf_counter() - t0
    combo, ocr_lines = payload["combo"], []
    ocr_fallback = ""
    recognition_started = time.perf_counter()
    with invocation_trace.capture() as trace:
        if combo["mode"] in ("vlm_direct", "ocr_vlm"):
            ocr_setting = "disabled/none" if combo["mode"] == "vlm_direct" else "paddle/pp-ocrv5"
            classify_settings = {"ocr": ocr_setting, "structure": combo["classify_model"],
                                 "classify": combo["classify_model"]}
            with settings_store.isolated_settings(classify_settings):
                type_key = pipeline.prepare_image(str(input_path))
            extract_settings = {"ocr": ocr_setting, "structure": combo["structure_model"],
                                "classify": combo["classify_model"]}
            with settings_store.isolated_settings(extract_settings):
                voucher = pipeline.extract_for_type(str(input_path), type_key)
            ocr_records = [x for x in trace if x.get("kind") == "ocr_result" and x.get("lines")]
            if ocr_records:
                ocr_lines = ocr_records[-1]["lines"]
            actual = combo["mode"]
            if combo["mode"] == "ocr_vlm" and "無，VLM 直讀" in voucher.ocr_provider:
                actual = "ocr_vlm→vlm_direct"
                ocr_fallback = "OCR 不可用或辨識失敗，已依規格降級為 VLM 直讀"
        else:
            ocr = model_factory.get_ocr("paddle")
            with activity.stage("ocr", "paddle/pp-ocrv5（自動轉正）"):
                pipeline._auto_orient_ocr(str(input_path), ocr)
            with activity.stage("ocr", "paddle/pp-ocrv5"):
                ocr_lines = ocr.recognize(str(input_path))
            ocr_text = ocr.plain_text(ocr_lines)
            if not ocr_text.strip():
                raise RuntimeError("OCR＋純文字 LLM 模式未取得任何 OCR 文字")
            type_key = _text_classify(ocr_text, combo["classify_model"])
            voucher = _text_extract(str(input_path), type_key, ocr_lines, ocr_text,
                                    combo["structure_model"])
            actual = "ocr_llm"
    recognition = time.perf_counter() - recognition_started
    voucher.source_file = f"{payload['relative_path']} p{payload['page_no']}"
    data = voucher.model_dump(mode="json")
    if ocr_fallback:
        data["validation_warning"] = ocr_fallback
    evidence_started = time.perf_counter()
    annotated = result_dir / "annotated.jpg"
    valid_boxes = _annotate(input_path, data.get("boxes") or {}, annotated)
    data["boxes"] = valid_boxes
    (result_dir / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (result_dir / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    (result_dir / "ocr.json").write_text(json.dumps(ocr_lines, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence = time.perf_counter() - evidence_started
    stages = {"ocr_seconds": 0.0, "classification_seconds": 0.0, "extraction_seconds": 0.0}
    for rec in trace:
        if rec.get("kind") != "stage": continue
        if rec["stage"] == "ocr": stages["ocr_seconds"] += rec["seconds"]
        elif "判類型" in rec.get("note", ""): stages["classification_seconds"] += rec["seconds"]
        elif rec["stage"] == "structure": stages["extraction_seconds"] += rec["seconds"]
    complete = preprocess + recognition + evidence
    timings = {"preprocess_seconds": round(preprocess, 6), **{k: round(v, 6) for k, v in stages.items()},
               "postprocess_evidence_seconds": round(evidence, 6),
               "recognition_seconds": round(recognition, 6), "complete_seconds": round(complete, 6),
               "pdf_render_seconds": payload.get("render_seconds", 0)}
    source_media = "image/png" if original.suffix.lower() == ".png" else "image/jpeg"
    artifacts = {
        "original": {"path": str(original), "media_type": source_media},
        "input": {"path": str(input_path), "media_type": "image/jpeg"},
        "annotated": {"path": str(annotated), "media_type": "image/jpeg"},
        "result_json": {"path": str(result_dir / "result.json"), "media_type": "application/json"},
        "trace_json": {"path": str(result_dir / "trace.json"), "media_type": "application/json"},
        "ocr_json": {"path": str(result_dir / "ocr.json"), "media_type": "application/json"},
    }
    box_sources = {}
    for key in valid_boxes:
        if combo["mode"] == "vlm_direct":
            box_sources[key] = "vlm"
        elif combo["mode"] == "ocr_llm":
            box_sources[key] = "ocr"
        else:
            value = (data.get("fields") or {}).get(key)
            box_sources[key] = ("ocr" if value not in (None, "") and
                                pipeline._match_ocr_line(str(value), ocr_lines) else "vlm")
    box_metadata = {"coordinates": valid_boxes, "sources": box_sources, "image": "input"}
    return {"actual_path": actual, "voucher": data, "boxes": box_metadata, "trace": trace,
            "ocr": ocr_lines, "timings": timings, "result_dir": str(result_dir), "artifacts": artifacts}


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str):
    with _connect() as con:
        art = con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if not art:
        return JSONResponse({"detail": "找不到證據檔案"}, status_code=404)
    path = Path(art["path"]).resolve()
    try: path.relative_to(ROOT.resolve())
    except ValueError: return JSONResponse({"detail": "證據路徑無效"}, status_code=403)
    if not path.is_file():
        return JSONResponse({"detail": "證據檔案不存在"}, status_code=404)
    return FileResponse(path, media_type=art["media_type"])


def _csv_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _export_data(run_id):
    with _connect() as con:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run: return None
        rows = con.execute("""SELECT r.*,p.relative_path,p.page_no,p.voucher_key,p.sha256
          FROM results r JOIN pages p ON p.id=r.page_id WHERE r.run_id=? ORDER BY r.rowid""", (run_id,)).fetchall()
        stats = _statistics(con, run_id)
        artifacts = con.execute("""SELECT a.*,r.id rid FROM artifacts a JOIN results r ON r.id=a.result_id
                                 WHERE r.run_id=?""", (run_id,)).fetchall()
        sources = con.execute("""SELECT DISTINCT p.original_path,p.relative_path FROM pages p
                               JOIN runs r ON r.dataset_id=p.dataset_id WHERE r.id=?""", (run_id,)).fetchall()
    results = []
    for row in rows:
        item = _row(row)
        for key in ("voucher_json", "timings_json", "trace_json", "ocr_json", "boxes_json"):
            item[key.removesuffix("_json")] = json.loads(item.pop(key) or ("[]" if key in ("trace_json", "ocr_json") else "{}"))
        results.append(item)
    return {"exported_at": _now(), "run": _run_dict(run), "statistics": stats,
            "results": results, "artifacts": [_row(x) for x in artifacts],
            "sources": [_row(x) for x in sources]}


def _csv_files(data):
    files = {}
    specs = {
      "vouchers.csv": ["result_id","source","page","combo","mode","actual_path","repeat","attempt","status","doc_type","voucher_id","date","total_amount","recognition_seconds","complete_seconds","error"],
      "fields.csv": ["result_id","source","field","value","box","located"],
      "items.csv": ["result_id","source","index","description","quantity","unit_price","amount"],
      "entries.csv": ["result_id","source","index","description","amount","debit_account","debit_account_name","credit_account","credit_account_name"]}
    buffers = {name: io.StringIO(newline="") for name in specs}
    writers = {name: csv.DictWriter(buffers[name], fieldnames=fields, extrasaction="ignore") for name, fields in specs.items()}
    for writer in writers.values(): writer.writeheader()
    for result in data["results"]:
        voucher, source = result.get("voucher") or {}, result["relative_path"]
        writers["vouchers.csv"].writerow({"result_id": result["id"], "source": _csv_safe(source), "page": result["page_no"],
          "combo": _csv_safe(result["combo_name"]), "mode": result["mode"], "actual_path": result["actual_path"],
          "repeat": result["repeat_no"], "attempt": result["attempt"], "status": result["status"],
          "doc_type": _csv_safe(voucher.get("doc_type")), "voucher_id": _csv_safe(voucher.get("voucher_id")),
          "date": _csv_safe(voucher.get("date")), "total_amount": voucher.get("total_amount"),
          "recognition_seconds": result["timings"].get("recognition_seconds"),
          "complete_seconds": result["timings"].get("complete_seconds"), "error": _csv_safe(result["error"])})
        boxes = voucher.get("boxes") or {}
        fields = {"voucher_id": voucher.get("voucher_id"), "date": voucher.get("date"),
                  "doc_type": voucher.get("doc_type"), "subtotal": voucher.get("subtotal"),
                  "tax_amount": voucher.get("tax_amount"), "total_amount": voucher.get("total_amount"),
                  **(voucher.get("fields") or {})}
        for key, value in fields.items():
            writers["fields.csv"].writerow({"result_id": result["id"], "source": _csv_safe(source),
              "field": _csv_safe(key), "value": _csv_safe(value), "box": json.dumps(boxes.get(key), ensure_ascii=False) if key in boxes else "",
              "located": "yes" if key in boxes else "no"})
        for index, item in enumerate(voucher.get("items") or [], 1):
            writers["items.csv"].writerow({"result_id": result["id"], "source": _csv_safe(source), "index": index,
                                           **{k: _csv_safe(v) for k, v in item.items()}})
        for index, item in enumerate(voucher.get("entries") or [], 1):
            writers["entries.csv"].writerow({"result_id": result["id"], "source": _csv_safe(source), "index": index,
                                             **{k: _csv_safe(v) for k, v in item.items()}})
    for name, buffer in buffers.items(): files[name] = "\ufeff" + buffer.getvalue()
    return files


@router.get("/runs/{run_id}/export")
def export_run(run_id: str, format: str = Query("zip", pattern="^(zip|json|csv)$")):
    data = _export_data(run_id)
    if not data: return JSONResponse({"detail": "找不到驗證批次"}, status_code=404)
    if format == "json":
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="validation-{run_id}.json"'})
    csv_files = _csv_files(data)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in csv_files.items(): zf.writestr(name, content.encode("utf-8"))
        if format == "zip":
            zf.writestr("validation.json", json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            for art in data["artifacts"]:
                path = Path(art["path"])
                if path.is_file():
                    zf.write(path, f"evidence/{art['rid']}/{art['kind']}{path.suffix}")
            for source in data["sources"]:
                path = Path(source["original_path"])
                if path.is_file():
                    zf.write(path, f"sources/{_safe_rel(source['relative_path'])}")
    filename = f"validation-{run_id}-{'complete' if format == 'zip' else 'csv'}.zip"
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
