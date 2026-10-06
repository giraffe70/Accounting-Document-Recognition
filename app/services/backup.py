"""地端備份模組：將資料庫與設定打包為 zip，供地端保存與還原。"""
import zipfile
from datetime import datetime

from app import config

_BACKUP_DIR = config.DATA_DIR / "backups"
_INCLUDE = ["app.db", "settings.json", "accounts.json", "companies.json", "accounting_policies.json"]


def create_backup() -> dict:
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backup_{stamp}.zip"
    path = _BACKUP_DIR / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in _INCLUDE:
            f = config.DATA_DIR / fn
            if f.exists():
                z.write(f, fn)
    return {"name": name, "size": path.stat().st_size}


def list_backups() -> list:
    if not _BACKUP_DIR.exists():
        return []
    items = []
    for f in sorted(_BACKUP_DIR.glob("backup_*.zip"), reverse=True):
        stat = f.stat()
        items.append({
            "name": f.name,
            "size": stat.st_size,
            "time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return items


def backup_path(name: str):
    """安全取得備份檔路徑（防目錄穿越）。"""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".zip"):
        return None
    p = _BACKUP_DIR / name
    return p if p.exists() else None
