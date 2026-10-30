"""Authenticated access to the published user manual and its referenced images."""
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app import config
from app.services import auth

MANUAL_DIR = config.PROJECT_ROOT / "docs" / "manual"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def require_user(request: Request):
    if not auth.get_user_by_token(request.cookies.get("session")):
        raise HTTPException(401, "請先登入後瀏覽使用者手冊", headers=HEADERS)


router = APIRouter(prefix="/api/manual", tags=["manual"], dependencies=[Depends(require_user)])


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        source = urlsplit(dict(attrs).get("src", ""))
        path = PurePosixPath(unquote(source.path))
        if (not source.scheme and not source.netloc and len(path.parts) == 2
                and path.parts[0] == "img" and path.name not in {".", ".."}
                and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}):
            self.names.add(path.name)


def _file(relative):
    path = MANUAL_DIR / relative
    if not path.is_file() or not path.resolve().is_relative_to(MANUAL_DIR.resolve()):
        raise HTTPException(404, "找不到手冊檔案，請聯絡管理員確認部署內容", headers=HEADERS)
    return path


@router.get("/", include_in_schema=False)
def view_manual():
    return FileResponse(_file("使用者手冊.html"), media_type="text/html", headers=HEADERS)


@router.get("/download")
def download_manual():
    return FileResponse(_file("使用者手冊.pdf"), media_type="application/pdf",
                        filename="憑證辨識系統使用者手冊.pdf", headers=HEADERS)


@router.get("/img/{filename}", include_in_schema=False)
def manual_image(filename: str):
    images = _Images()
    images.feed(_file("使用者手冊.html").read_text(encoding="utf-8"))
    if filename not in images.names:
        raise HTTPException(404, "找不到手冊圖片", headers=HEADERS)
    return FileResponse(_file(PurePosixPath("img") / filename), headers=HEADERS)
