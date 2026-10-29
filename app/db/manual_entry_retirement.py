"""Retire the record-wide manual flag while retaining selected manual values."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def _protected_content(raw):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("單據內容必須是物件")
    evidence = data.setdefault("field_evidence", {})
    if not isinstance(evidence, dict) or not isinstance(data.get("fields", {}), dict):
        raise ValueError("人工修正證據或欄位格式不合法")
    values = {f"fields.{key}": value for key, value in data.get("fields", {}).items()}
    values.update({key: data[key] for key in ("items", "entries", "note", "doc_type_key", "doc_type")
                   if key in data})
    for path, value in values.items():
        records = evidence.setdefault(path, [])
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("人工修正證據格式不合法")
        if not any(row.get("source") == "manual" and row.get("selected") for row in records):
            for row in records:
                row["selected"] = False
            records.append({"source": "manual", "value": value, "selected": True,
                            "reason": "由舊版整張單據人工輸入標記轉入"})
    return json.dumps(data, ensure_ascii=False)


def _backup(connection, database):
    if not database or database == ":memory:":
        return None
    database = Path(database).resolve()
    folder = database.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"{database.stem}_before_manual_entry_retirement_{stamp}_{uuid4().hex}.db"
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(target)) as destination:
            connection.connection.driver_connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("移除人工輸入欄位前的備份驗證失敗")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def retire_manual_entry(engine):
    """Back up first, convert flagged records, then drop the column atomically."""
    with engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(voucher_records)")}
        if "manual_entry" not in columns:
            return None
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("資料庫有既存外鍵錯誤，未移除人工輸入欄位")
        connection.commit()
        backup = _backup(connection, engine.url.database)
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            records = connection.exec_driver_sql(
                "SELECT id, manual_entry, current_json FROM voucher_records").all()
            for ident, flag, raw in records:
                normalized = str(flag).strip().lower() if flag is not None else "0"
                if normalized in ("0", "false", ""):
                    continue
                if normalized not in ("1", "true"):
                    raise ValueError(f"單據 {ident} 的人工輸入標記不合法")
                try:
                    converted = _protected_content(raw)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"單據 {ident} 的人工輸入保護無法轉換，資料庫未變更") from exc
                connection.exec_driver_sql(
                    "UPDATE voucher_records SET current_json=? WHERE id=?", (converted, ident))
            connection.exec_driver_sql("ALTER TABLE voucher_records DROP COLUMN manual_entry")
            if connection.exec_driver_sql("SELECT COUNT(*) FROM voucher_records").scalar_one() != len(records):
                raise RuntimeError("移除人工輸入欄位後筆數不符，已回滾")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError("移除人工輸入欄位後外鍵驗證失敗，已回滾")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return backup
