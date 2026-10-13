"""帳號密碼登入與角色權限（基本資安）。

密碼以 PBKDF2-HMAC-SHA256 雜湊（stdlib，不加依賴）；session token 存記憶體。
角色：clerk（記帳士）、accountant（會計師）、admin（管理員）。
"""
import hashlib
import hmac
import secrets

from sqlmodel import select

from app.db.models import UserAccount
from app.db.session import get_session

# token -> {username, role, name}
_sessions: dict[str, dict] = {}

# 預設種子帳號（首次啟動建立；正式部署請立即改密碼）
_SEED = [
    ("admin", "admin123", "admin", "系統管理員"),
    ("clerk", "clerk123", "clerk", "記帳士"),
    ("accountant", "acc123", "accountant", "會計師"),
]


def _hash(pw: str, salt: str = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return salt, dk.hex()


def ensure_seed_users():
    with get_session() as s:
        exists = s.exec(select(UserAccount)).first()
        if exists:
            return
        for username, pw, role, name in _SEED:
            salt, ph = _hash(pw)
            s.add(UserAccount(username=username, password_hash=ph, salt=salt, role=role, name=name))
        s.commit()


def authenticate(username: str, password: str):
    with get_session() as s:
        user = s.exec(select(UserAccount).where(UserAccount.username == username)).first()
        if not user:
            return None
        _, ph = _hash(password, user.salt)
        if hmac.compare_digest(ph, user.password_hash):
            return {"id": user.id, "username": user.username, "role": user.role, "name": user.name}
        return None


def create_token(user: dict) -> str:
    token = secrets.token_urlsafe(24)
    _sessions[token] = user
    return token


def get_user_by_token(token: str | None):
    return _sessions.get(token) if token else None


def logout(token: str | None):
    if token:
        _sessions.pop(token, None)
