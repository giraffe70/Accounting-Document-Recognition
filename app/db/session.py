"""資料庫連線與初始化（SQLite）。"""
from sqlmodel import Session, SQLModel, create_engine

from app import config

_engine = create_engine(
    f"sqlite:///{config.DB_FILE}",
    connect_args={"check_same_thread": False},
)


def init_db():
    # 確保 model 已載入再建表
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(_engine)
    _migrate()


def _migrate():
    """輕量遷移：對既有 DB 補上新欄位（SQLite create_all 不會自動加欄位）。

    型別要跟著欄位走：布林欄位若建成 VARCHAR，SQLite 會把 0 存成字串 '0'，
    而 Python 的 bool('0') 是 True——整個資料表會全部讀成「真」。
    """
    # col -> (SQL 型別, 預設值)
    added = {
        "image_path": ("VARCHAR", "''"),
        "dev_note": ("VARCHAR", "''"),
        "attachments": ("VARCHAR", "'[]'"),
        "manual_entry": ("INTEGER", "0"),
        "record_role": ("VARCHAR", "'voucher'"),
        "source_parent_id": ("INTEGER", "NULL"),
        "recognition_history": ("VARCHAR", "'[]'"),
    }
    with _engine.connect() as conn:
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
        conn.commit()


def get_session() -> Session:
    return Session(_engine)
