"""資料庫連線與初始化（SQLite）。"""
from sqlmodel import Session, SQLModel, create_engine
import json
from sqlalchemy import event

from app import config

_engine = create_engine(
    f"sqlite:///{config.DB_FILE}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(_engine, "connect")
def _foreign_keys(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")


def init_db():
    # 確保 model 已載入再建表
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _migrate_users()
    _migrate()
    from app.db.entry_retirement import retire_entry_management
    retire_entry_management(_engine)
    _migrate_account_mappings()


def _migrate_users():
    """Atomically preserve admin access and remove the obsolete role column."""
    with _engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            duplicate = conn.exec_driver_sql(
                "SELECT 1 FROM users GROUP BY username HAVING COUNT(*) > 1 LIMIT 1").first()
            if duplicate:
                raise RuntimeError("帳號資料遷移失敗：存在重複帳號，請先由管理員人工處理；未自動合併或更名。")
            columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
            if "is_active" not in columns:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            if "is_admin" not in columns:
                if "role" not in columns and conn.exec_driver_sql("SELECT 1 FROM users LIMIT 1").first():
                    raise RuntimeError("帳號資料遷移失敗：缺少舊角色及管理員權限欄位，無法判斷管理員。")
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
                if "role" in columns:
                    conn.exec_driver_sql("UPDATE users SET is_admin = CASE WHEN role = 'admin' THEN 1 ELSE 0 END")
            elif "role" in columns:
                conflict = conn.exec_driver_sql("SELECT 1 FROM users WHERE is_admin IS NULL OR "
                    "CAST(is_admin AS INTEGER) != CASE WHEN role = 'admin' THEN 1 ELSE 0 END LIMIT 1").first()
                if conflict:
                    raise RuntimeError("帳號資料遷移失敗：舊角色與管理員權限不一致，請先人工檢查。")
            invalid_admin = conn.exec_driver_sql("SELECT 1 FROM users WHERE is_admin IS NULL OR "
                "typeof(is_admin) != 'integer' OR is_admin NOT IN (0, 1) LIMIT 1").first()
            if invalid_admin:
                raise RuntimeError("帳號資料遷移失敗：管理員權限必須為整數 0 或 1，請先人工檢查。")
            if "role" in columns:
                conn.exec_driver_sql("ALTER TABLE users DROP COLUMN role")
            conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users(username)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# 已由 correction_note、confirmed_at 與新狀態取代的舊審核欄位（遷移後移除）。
_LEGACY_COLUMNS = (
    "clerk_note", "accountant_note", "dev_note",
    "clerk_reviewed_at", "accountant_approved_at",
)


def _migrate():
    """輕量遷移：對既有 DB 補上新欄位（SQLite create_all 不會自動加欄位）。

    型別要跟著欄位走：布林欄位若建成 VARCHAR，SQLite 會把 0 存成字串 '0'，
    而 Python 的 bool('0') 是 True——整個資料表會全部讀成「真」。
    """
    # col -> (SQL 型別, 預設值)
    added = {
        "image_path": ("VARCHAR", "''"),
        "attachments": ("VARCHAR", "'[]'"),
        "manual_entry": ("INTEGER", "0"),
        "record_role": ("VARCHAR", "'voucher'"),
        "source_parent_id": ("INTEGER", "NULL"),
        "recognition_history": ("VARCHAR", "'[]'"),
        "correction_note": ("VARCHAR", "''"),
        "confirmed_at": ("DATETIME", "NULL"),
        "confirmed_by": ("INTEGER", "NULL"),
        "confirmed_by_name": ("VARCHAR", "''"),
        "recognition_confirmed_version": ("INTEGER", "0"),
        "company_id": ("INTEGER", "NULL"),
    }
    with _engine.begin() as conn:
        page_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(import_pages)")}
        if page_columns and "preparation_json" not in page_columns:
            conn.exec_driver_sql("ALTER TABLE import_pages ADD COLUMN preparation_json VARCHAR DEFAULT '{}'")
        info = list(conn.exec_driver_sql("PRAGMA table_info(voucher_records)"))
        cols = {r[1]: (r[2] or "").upper() for r in info}
        for col, (sqltype, default) in added.items():
            if col not in cols:
                conn.exec_driver_sql(
                    f"ALTER TABLE voucher_records ADD COLUMN {col} {sqltype} DEFAULT {default}")
            elif col == "manual_entry" and cols[col] != "INTEGER":
                # 修正早期版本把布林欄位建成 VARCHAR 的資料（全部會讀成 True）
                conn.exec_driver_sql(f"ALTER TABLE voucher_records DROP COLUMN {col}")
                conn.exec_driver_sql(
                    f"ALTER TABLE voucher_records ADD COLUMN {col} {sqltype} DEFAULT {default}")
        # 在 ORM 讀取前轉換舊 enum 字串。只在新增欄位時合併備註，
        # 避免下次啟動將使用者編修或清空的新備註覆寫。
        if "correction_note" not in cols:
            clerk = "COALESCE(clerk_note, '')" if "clerk_note" in cols else "''"
            accountant = "COALESCE(accountant_note, '')" if "accountant_note" in cols else "''"
            conn.exec_driver_sql(f"""
                UPDATE voucher_records SET correction_note =
                    CASE WHEN {clerk} != '' THEN '記帳士備註（舊）：' || {clerk} ELSE '' END ||
                    CASE WHEN {clerk} != '' AND {accountant} != '' THEN char(10) ELSE '' END ||
                    CASE WHEN {accountant} != '' THEN '會計師備註（舊）：' || {accountant} ELSE '' END
            """)
        approved_at = "accountant_approved_at" if "accountant_approved_at" in cols else "NULL"
        conn.exec_driver_sql(f"""
            UPDATE voucher_records SET status = 'confirmed', confirmed_at = {approved_at}
            WHERE status = 'accountant_approved'
        """)
        conn.exec_driver_sql("""
            UPDATE voucher_records SET status = 'pending', confirmed_at = NULL
            WHERE status IN ('clerk_reviewed', 'rejected')
        """)
        # 舊欄位的資料已在上方轉入新欄位／新狀態；dev_note 依決議直接捨棄。
        # 必須排在轉換之後再刪，且逐欄檢查存在性以維持冪等。
        for col in _LEGACY_COLUMNS:
            if col in cols:
                conn.exec_driver_sql(f"ALTER TABLE voucher_records DROP COLUMN {col}")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_voucher_records_company_id ON voucher_records(company_id)")


def _migrate_account_mappings():
    from uuid import UUID, uuid4
    from app.db.models import VoucherRecord
    from app.services import companies, item_account_mapping as mapping
    from sqlmodel import select
    registration = config.DATA_DIR / "companies.json"
    registrations = json.loads(registration.read_text(encoding="utf-8")) if registration.exists() else []
    with Session(_engine) as session:
        companies.seed(session, registrations)
        for rec in session.exec(select(VoucherRecord)).all():
            if rec.company_id is None:
                company = companies.resolve(session, rec.company)
                if company:
                    rec.company_id = company.id
            try:
                data = json.loads(rec.current_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            rows = data.get("items") or []
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                continue
            for index, row in enumerate(rows):
                row = mapping.defaults(row)
                try:
                    row["item_id"] = str(UUID(row["item_id"]))
                except (TypeError, ValueError, AttributeError):
                    row["item_id"] = str(uuid4())
                if any(previous.get("item_id") == row["item_id"] for previous in rows[:index]):
                    row["item_id"] = str(uuid4())
                row.setdefault("account_company_id", rec.company_id)
                row.setdefault("account_item_key", mapping.item_key(row.get("description")))
                rows[index] = row
            if rows:
                data["items"] = rows
                rec.current_json = json.dumps(data, ensure_ascii=False)
            session.add(rec)
        session.commit()


def get_session() -> Session:
    return Session(_engine)
