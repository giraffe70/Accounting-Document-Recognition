"""Retire redundant source rows without cascading into their actual documents."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


def _object(raw):
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("憑證內容不是物件，來源頁清理已停止")
    return value


def _backup(connection, database):
    if not database or database == ":memory:":
        return None
    database = Path(database).resolve()
    folder = database.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"{database.stem}_before_source_page_retirement_{stamp}_{uuid4().hex}.db"
    with closing(sqlite3.connect(target)) as destination:
        connection.connection.driver_connection.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("來源頁清理備份驗證失敗")
    return str(target)


def _sidecars(path):
    return [path, *(path.with_suffix(f".{kind}{path.suffix}") for kind in ("baseline", "deskew", "orig"))]


def cleanup_images(engine, upload_dir, candidates):
    """Only remove named, unreferenced page files; never recursively delete caches."""
    upload_dir = Path(upload_dir).resolve()
    pages = (upload_dir / "pages").resolve()
    if not pages.is_relative_to(upload_dir):
        raise ValueError("憑證影像目錄超出上傳目錄，未清理檔案")
    protected = set()
    tokens = set()
    with engine.connect() as connection:
        # Serialize reference checks with saves and ID reuse.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        records = connection.exec_driver_sql("SELECT image_path, attachments, ai_json, current_json FROM voucher_records").mappings()
        for record in records:
            for rel in [record["image_path"], *json.loads(record["attachments"] or "[]")]:
                if rel:
                    protected.update(path.resolve() for path in _sidecars(upload_dir / rel))
            for key in ("ai_json", "current_json"):
                origin = _object(record[key]).get("origin") or {}
                token = origin.get("source_token")
                if isinstance(token, str) and re.fullmatch(r"[a-f0-9]{32}", token):
                    tokens.add(token)
        tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if "import_pages" in tables:
            for row in connection.exec_driver_sql("SELECT preview_path,source_token FROM import_pages"):
                if row[0]:
                    protected.add(Path(row[0]).resolve())
                if isinstance(row[1], str) and re.fullmatch(r"[a-f0-9]{32}", row[1]):
                    tokens.add(row[1])
        for token in tokens:
            descriptor = upload_dir / "_pages" / token / "pdf_source.json"
            if descriptor.is_file():
                data = _object(descriptor.read_text(encoding="utf-8"))
                protected.update(Path(data[key]).resolve() for key in ("path", "render_source") if data.get(key))
        removed, failed, retained = 0, [], 0
        for raw in sorted(set(str(path) for path in candidates)):
            path = Path(raw).resolve()
            if path in protected:
                retained += 1
                continue
            if path == pages or not path.is_relative_to(pages):
                failed.append(f"{raw}: 不在憑證影像清理範圍")
                continue
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                failed.append(f"{raw}: {exc}")
        connection.rollback()
    return {"files_removed": removed, "files_retained": retained, "cleanup_failed": failed}


def retire_source_pages(engine, upload_dir, *, apply=False):
    """Dry-run by default; preserve every non-source row byte-for-byte except its parent."""
    upload_dir = Path(upload_dir).resolve()
    with engine.connect() as connection:
        tables = {row[0] for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        if "voucher_records" not in tables:
            return {"source_ids": [], "retained_count": 0, "applied": False}
        records = [dict(row) for row in connection.exec_driver_sql("SELECT * FROM voucher_records ORDER BY id").mappings()]
        sources = [row for row in records if row.get("record_role") == "source_page"]
        kept = [row for row in records if row.get("record_role") != "source_page"]
        source_ids = {row["id"] for row in sources}
        report = {"source_ids": sorted(source_ids), "retained_count": len(kept), "applied": False,
                  "backup": None, "files_removed": 0, "files_retained": 0, "cleanup_failed": []}
        if not sources:
            return report
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("資料庫有外鍵錯誤，來源頁清理已停止")
        origins = {row["id"]: _object(row["current_json"]).get("origin") or {} for row in records}
        for source in sources:
            for key in ("ai_json", "current_json"):
                data = _object(source[key])
                if source.get("status") != "prepared" or any(data.get(key) not in (None, "", [], {})
                        for key in ("voucher_id", "date", "fields", "items", "entries", "note")) or any(
                        data.get(key) not in (None, 0, "") for key in ("subtotal", "tax_amount", "total_amount")):
                    raise ValueError(f"來源頁 {source['id']} 含有未預期的單據內容，未執行刪除")
        candidates = [path for row in sources for rel in [row.get("image_path"), *json.loads(row.get("attachments") or "[]")]
                      if rel for path in _sidecars(upload_dir / rel)]
        report["image_candidates"] = sorted({str(path) for path in candidates if path.is_file()})
        if not apply:
            return report
        connection.commit()
        report["backup"] = _backup(connection, engine.url.database)
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            current = [dict(row) for row in connection.exec_driver_sql("SELECT * FROM voucher_records ORDER BY id").mappings()]
            if current != records:
                raise RuntimeError("清理預檢後資料已改變，請停止服務後重試")
            if "import_pages" in tables:
                source_tokens = {origin.get("source_token") for ident, origin in origins.items() if ident in source_ids}
                for page in connection.exec_driver_sql("SELECT * FROM import_pages").mappings().all():
                    documents = [row for row in kept if (
                        origins[row["id"]].get("import_page_id") == page["id"]
                        and origins[row["id"]].get("import_job_id", page["job_id"]) == page["job_id"])
                        or (not origins[row["id"]].get("import_page_id")
                            and bool(page["source_token"]) and origins[row["id"]].get("source_token") == page["source_token"])]
                    affected = page["source_page_id"] in source_ids or page["source_token"] in source_tokens or bool(documents)
                    if not affected:
                        continue
                    ids = [row["id"] for row in documents]
                    preview = str(upload_dir / documents[0]["image_path"]) if documents and documents[0].get("image_path") else ""
                    status, phase = page["status"], page["phase"]
                    if not ids and status == "preview_ready":
                        status, phase = "completed", "discarded"
                    connection.exec_driver_sql("UPDATE import_pages SET source_page_id=NULL,voucher_ids_json=?,preview_path=?,status=?,phase=?,preparation_json=? WHERE id=?",
                        (json.dumps(ids), preview, status, phase, "{}" if not ids else page["preparation_json"], page["id"]))
                connection.exec_driver_sql("UPDATE import_pages SET source_page_id=NULL WHERE source_page_id IS NOT NULL")
            for table in ("item_account_mappings", "mapping_history"):
                if table in tables:
                    connection.exec_driver_sql(f"UPDATE {table} SET source_voucher_id=NULL WHERE source_voucher_id IN (SELECT id FROM voucher_records WHERE record_role='source_page')")
            connection.exec_driver_sql("UPDATE voucher_records SET source_parent_id=NULL WHERE source_parent_id IS NOT NULL")
            connection.exec_driver_sql("DELETE FROM voucher_records WHERE record_role='source_page'")
            remaining = [dict(row) for row in connection.exec_driver_sql("SELECT * FROM voucher_records ORDER BY id").mappings()]
            expected = [{**row, "source_parent_id": None} for row in kept]
            if remaining != expected or connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError("來源頁清理後資料驗證不符，已回滾")
            connection.commit()
            report["applied"] = True
        except Exception:
            connection.rollback()
            raise
    if report["backup"]:
        manifest = Path(report["backup"]).with_suffix(".json")
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        report.update(cleanup_images(engine, upload_dir, candidates))
    except (OSError, ValueError, sqlite3.Error) as exc:
        report["cleanup_failed"].append(str(exc))
    if report["backup"]:
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
