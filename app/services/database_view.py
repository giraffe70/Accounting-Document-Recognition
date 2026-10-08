"""Admin-only, read-only browser for the application's SQLite databases."""
import sqlite3
from contextlib import contextmanager
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app import config
from app.services import auth


def require_admin(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    user = auth.get_user_by_token(request.cookies.get("session"))
    if not user:
        raise HTTPException(401, "未登入")
    if user["role"] != "admin":
        raise HTTPException(403, "需管理員權限")


router = APIRouter(prefix="/api/database", tags=["database"],
                   dependencies=[Depends(require_admin)])
MASKED = "（已遮蔽）"
SENSITIVE = {"password_hash", "salt"}

# The metadata is shared by the table selector, headers and query validation.
TABLE_META = {
    ("app", "users"): {
        "label": "使用者帳號", "description": "登入帳號、角色及建立時間；密碼資料只顯示遮蔽標記。",
        "date_label": "帳號建立日期", "date_column": "created_at",
        "columns": {
            "id": "帳號編號", "username": "登入帳號", "password_hash": "密碼雜湊",
            "salt": "密碼鹽值", "role": "角色", "name": "顯示名稱", "created_at": "建立時間",
        },
    },
    ("app", "voucher_records"): {
        "label": "憑證紀錄", "description": "上傳憑證的辨識內容、人工修正及審核狀態。",
        "date_label": "憑證日期", "date_column": "current_json.date",
        "statuses": {
            "prepared": "待外觀確認", "queued": "待提取", "pending": "待審",
            "clerk_reviewed": "記帳士已確認", "accountant_approved": "會計師已核定",
            "rejected": "退回",
        },
        "columns": {
            "id": "憑證編號", "source_file": "來源檔名", "company": "歸屬公司",
            "period": "所屬期別", "status": "審核狀態", "confidence": "辨識信心度",
            "ai_json": "AI 辨識內容", "current_json": "目前憑證內容",
            "clerk_note": "記帳士備註", "accountant_note": "會計師備註",
            "dev_note": "開發備註", "clerk_reviewed_at": "記帳士確認時間",
            "accountant_approved_at": "會計師核定時間", "manual_entry": "人工輸入",
            "ocr_provider": "OCR 來源", "structure_model": "欄位萃取模型",
            "image_path": "掃描影像路徑", "attachments": "附頁清單",
            "record_role": "紀錄角色", "source_parent_id": "來源憑證編號",
            "recognition_history": "辨識歷程",
            "created_at": "建立時間", "updated_at": "更新時間",
        },
    },
    ("validation", "datasets"): {
        "label": "驗證資料集", "description": "用於測試辨識效果的一批來源檔案及頁面。",
        "date_label": "資料集建立日期", "date_column": "created_at",
        "utc_columns": ("created_at",),
        "columns": {
            "id": "資料集編號", "name": "資料集名稱", "created_at": "建立時間",
            "file_count": "檔案數", "page_count": "頁數", "errors_json": "匯入錯誤",
        },
    },
    ("validation", "pages"): {
        "label": "驗證頁面", "description": "資料集中的每一頁及其來源、影像路徑。",
        "columns": {
            "id": "頁面編號", "dataset_id": "資料集編號", "voucher_key": "憑證分組鍵",
            "relative_path": "相對檔案路徑", "sha256": "檔案雜湊值", "page_no": "頁碼",
            "original_path": "原始檔案路徑", "page_path": "頁面影像路徑",
            "render_seconds": "轉頁耗時（秒）",
        },
    },
    ("validation", "runs"): {
        "label": "驗證批次", "description": "一次驗證執行的設定、進度及完成情況。",
        "date_label": "批次建立日期", "date_column": "created_at",
        "utc_columns": ("created_at", "started_at", "finished_at"),
        "statuses": {
            "queued": "排隊中", "running": "執行中", "cancelling": "取消中",
            "cancelled": "已取消", "interrupted": "已中斷", "terminated": "已終止",
            "completed": "已完成",
        },
        "columns": {
            "id": "批次編號", "dataset_id": "資料集編號", "name": "批次名稱",
            "status": "批次狀態", "created_at": "建立時間", "started_at": "開始時間",
            "finished_at": "完成時間", "settings_json": "執行設定", "total": "總項目數",
            "done": "已完成數", "succeeded": "成功數", "failed": "失敗數",
            "current": "目前處理項目", "cancel_requested": "要求取消",
        },
    },
    ("validation", "results"): {
        "label": "逐項驗證結果", "description": "每頁在各模型組合下的辨識結果、耗時及錯誤。",
        "date_label": "排入佇列日期", "date_column": "queued_at",
        "utc_columns": ("queued_at", "started_at", "finished_at"),
        "statuses": {
            "queued": "排隊中", "running": "執行中", "succeeded": "成功",
            "partial": "部分完成／待分割", "skipped": "確認空白／略過",
            "failed": "失敗", "timeout": "逾時", "cancelled": "已取消",
            "terminated": "已終止",
        },
        "columns": {
            "id": "結果編號", "run_id": "批次編號", "page_id": "頁面編號",
            "combo_id": "模型組合編號", "combo_name": "模型組合名稱",
            "mode": "辨識模式", "repeat_no": "重複輪次", "attempt": "嘗試次數",
            "warmup": "暖機", "status": "辨識狀態", "queued_at": "排入時間",
            "started_at": "開始時間", "finished_at": "完成時間",
            "actual_path": "實際執行路徑", "voucher_json": "辨識結果",
            "timings_json": "耗時明細", "trace_json": "執行追蹤",
            "ocr_json": "OCR 文字", "boxes_json": "欄位座標",
            "error": "錯誤訊息", "result_dir": "結果目錄",
            "page_json": "頁面版面、來源與品質",
        },
    },
    ("validation", "document_results"): {
        "label": "單據辨識結果", "description": "每頁辨識出的單據、來源結果及各單據辨識內容。",
        "columns": {
            "id": "單據結果編號", "parent_result_id": "來源頁結果編號",
            "document_index": "頁內單據序號", "status": "單據狀態",
            "data_json": "單據辨識內容", "error": "錯誤訊息",
        },
    },
    ("validation", "artifacts"): {
        "label": "驗證產出檔", "description": "逐項驗證結果產生的檔案位置及類型。",
        "columns": {
            "id": "產出檔編號", "result_id": "驗證結果編號", "kind": "產出類型",
            "path": "檔案路徑", "media_type": "檔案媒體類型",
        },
    },
}


def table_info(database, table):
    meta = TABLE_META.get((database, table), {})
    return {
        "label": meta.get("label", table),
        "description": meta.get("description", "此表尚未設定中文說明。"),
        "date_label": meta.get("date_label"),
        "statuses": [{"value": value, "label": label}
                     for value, label in meta.get("statuses", {}).items()],
    }


def date_expression(database, date_column):
    if date_column == "current_json.date":
        return "date(CASE WHEN json_valid(\"current_json\") THEN json_extract(\"current_json\", '$.date') END)"
    column = quote(date_column)
    return f"date({column}, '+8 hours')" if database == "validation" else f"date({column})"


def databases():
    # Fixed paths only: callers cannot select an arbitrary filesystem path.
    return {"app": ("憑證與帳號", config.DB_FILE),
            "validation": ("資料集驗證", config.DATA_DIR / "validation" / "validation.db")}


@contextmanager
def connect(database):
    item = databases().get(database)
    if not item or not item[1].is_file():
        raise HTTPException(404, "找不到資料庫")
    con = None
    try:
        con = sqlite3.connect(item[1].resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con
    except sqlite3.Error:
        raise HTTPException(503, "資料庫暫時無法讀取，請稍後重試") from None
    finally:
        if con is not None:
            con.close()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if not r[0].startswith("sqlite_")]


@router.get("/tables")
def list_tables():
    result = []
    for key, (label, path) in databases().items():
        if not path.is_file():
            continue
        with connect(key) as con:
            for name in tables(con):
                count = con.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
                result.append({"database": key, "database_label": label, "name": name,
                               "count": count, **table_info(key, name)})
    return {"tables": result}


@router.get("/rows")
def list_rows(database: str, table: str, q: str = Query("", max_length=200),
              page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
              date_from: date | None = None, date_to: date | None = None, status: str = ""):
    with connect(database) as con:
        if table not in tables(con):
            raise HTTPException(404, "找不到資料表")
        meta = TABLE_META.get((database, table), {})
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "起日不能晚於迄日")
        if (date_from or date_to) and not meta.get("date_column"):
            raise HTTPException(422, "此資料表沒有日期篩選")
        if status and status not in meta.get("statuses", {}):
            raise HTTPException(422, "此資料表沒有此狀態")
        schema = list(con.execute(f"PRAGMA table_info({quote(table)})"))
        columns = [{"name": r["name"], "label": meta.get("columns", {}).get(r["name"], r["name"]),
                    "type": r["type"], "utc": r["name"] in meta.get("utc_columns", ()),
                    "masked": r["name"].lower() in SENSITIVE} for r in schema]
        visible = [c["name"] for c in columns if not c["masked"]]
        params = []
        clauses = []
        if q:
            # instr treats %, _ and quotes literally; never search masked columns.
            predicates = [f"instr(lower(CAST({quote(c)} AS TEXT)), lower(?)) > 0" for c in visible]
            clauses.append("(" + " OR ".join(predicates) + ")" if predicates else "0")
            params = [q] * len(visible)
        if date_from or date_to:
            expression = date_expression(database, meta["date_column"])
            if date_from:
                clauses.append(f"{expression} >= ?")
                params.append(date_from.isoformat())
            if date_to:
                clauses.append(f"{expression} <= ?")
                params.append(date_to.isoformat())
        if status:
            clauses.append('"status" = ?')
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = con.execute(f"SELECT COUNT(*) FROM {quote(table)}{where}", params).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        primary = [r["name"] for r in sorted(schema, key=lambda r: r["pk"]) if r["pk"]]
        order = ", ".join(quote(c) for c in (primary or visible)) or "1"
        fields = ", ".join(
            f"NULL AS {quote(c['name'])}" if c["masked"] else quote(c["name"]) for c in columns)
        rows = con.execute(f"SELECT {fields} FROM {quote(table)}{where} ORDER BY {order} LIMIT ? OFFSET ?",
                           [*params, page_size, (page - 1) * page_size])
        items = [{c["name"]: MASKED if c["masked"] else
                  ("[BLOB] " + row[c["name"]].hex() if isinstance(row[c["name"]], bytes) else row[c["name"]])
                  for c in columns} for row in rows]
        return {"columns": columns, "rows": items, "page": page, "page_size": page_size,
                "total": total, "pages": pages}
