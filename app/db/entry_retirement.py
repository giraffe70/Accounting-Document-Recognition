"""Retire posting metadata without rewriting recognition JSON or foreign references."""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


RETIRED_COLUMNS = frozenset({
    "entries_status", "entries_confirmed_at", "entries_confirmed_by",
    "entries_confirmed_by_name", "entries_proposal_json", "entries_history",
})
_TEMP_TABLE = "_voucher_records_without_posting"
_TOKEN = re.compile(r'''"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]|'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9]*|[^\s]''')


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _identifiers(sql):
    """Ignore SQL string literals when finding retired column references."""
    return {token.strip('"`[]').replace('""', '"').lower()
            for token in _TOKEN.findall(sql) if not token.startswith("'")}


def _definitions(sql):
    """Split CREATE TABLE at top-level commas, preserving types/defaults/constraints."""
    start = sql.index("(")
    depth, quote, parts, previous, index = 0, None, [], start + 1, start
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if quote != "]" and index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in "'\"`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                parts.append(sql[previous:index].strip())
                return parts, sql[index + 1:]
        elif char == "," and depth == 1:
            parts.append(sql[previous:index].strip())
            previous = index + 1
        index += 1
    raise RuntimeError("憑證資料表定義無法解析，未執行分錄欄位移除")


def _replacement_sql(sql):
    definitions, suffix = _definitions(sql)
    kept = []
    for definition in definitions:
        tokens = _TOKEN.findall(definition)
        first = tokens[0].strip('"`[]').lower()
        if first in RETIRED_COLUMNS:
            continue
        references = _identifiers(definition) & RETIRED_COLUMNS
        if references:
            # The ORM emits table-level FKs. Do not silently discard a composite
            # constraint which also protects surviving data.
            foreign = re.search(r"\bFOREIGN\s+KEY\s*\(([^)]*)\)", definition, re.I)
            if foreign and _identifiers(foreign.group(1)) <= RETIRED_COLUMNS:
                continue
            raise RuntimeError("分錄欄位與其他約束共用，無法安全移除；資料庫未變更")
        kept.append(definition)
    return f'CREATE TABLE {_quote(_TEMP_TABLE)} (\n' + ",\n".join(kept) + ")" + suffix


def _backup(connection, database):
    """Use SQLite's consistent backup API; never overwrite a prior snapshot."""
    if not database or database == ":memory:":
        return None
    database = Path(database).resolve()
    folder = database.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"{database.stem}_before_entry_retirement_{stamp}_{uuid4().hex}.db"
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(target)) as destination:
            connection.connection.driver_connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("分錄欄位移除前的資料庫備份驗證失敗")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def retire_entry_management(engine):
    """Backup then atomically remove only the six retired columns; repeatable."""
    with engine.connect() as connection:
        columns = list(connection.exec_driver_sql("PRAGMA table_xinfo(voucher_records)"))
        if not any(row[1] in RETIRED_COLUMNS for row in columns):
            return None
        ddl = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='voucher_records'").scalar_one()
        replacement = _replacement_sql(ddl)
        objects = list(connection.exec_driver_sql(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='voucher_records' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"))
        preserved = []
        for kind, name, sql in objects:
            if _identifiers(sql) & RETIRED_COLUMNS:
                if kind == "trigger":
                    raise RuntimeError("有觸發器依賴已退役分錄欄位，資料庫未變更")
                continue
            preserved.append(sql)
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("資料庫有既存外鍵錯誤，未執行分錄欄位移除")
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        connection.commit()
        backup = _backup(connection, engine.url.database)
        # SQLite requires changing this PRAGMA outside the rebuild transaction.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.exec_driver_sql(replacement)
            # Generated columns stay in the CREATE statement but cannot be inserted.
            names = ",".join(_quote(row[1]) for row in columns
                             if row[1] not in RETIRED_COLUMNS and not row[6])
            connection.exec_driver_sql(
                f"INSERT INTO {_quote(_TEMP_TABLE)} ({names}) SELECT {names} FROM voucher_records")
            before = connection.exec_driver_sql("SELECT COUNT(*) FROM voucher_records").scalar_one()
            after = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote(_TEMP_TABLE)}").scalar_one()
            if before != after or connection.exec_driver_sql(
                    f"SELECT {names} FROM voucher_records EXCEPT SELECT {names} FROM {_quote(_TEMP_TABLE)} LIMIT 1").first():
                raise RuntimeError("憑證資料複製驗證失敗，已回滾")
            connection.exec_driver_sql("DROP TABLE voucher_records")
            connection.exec_driver_sql(f"ALTER TABLE {_quote(_TEMP_TABLE)} RENAME TO voucher_records")
            for sql in preserved:
                connection.exec_driver_sql(sql)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError("分錄欄位移除後外鍵驗證失敗，已回滾")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
            connection.commit()
        return backup
