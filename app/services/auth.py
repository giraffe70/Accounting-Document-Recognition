"""帳號密碼登入與管理員權限（基本資安）。

密碼以 PBKDF2-HMAC-SHA256 雜湊（stdlib，不加依賴）；session token 存記憶體。
帳號分為系統管理員及一般使用者。
"""
import hashlib
import hmac
import secrets
import threading

from sqlmodel import select
from sqlalchemy.exc import IntegrityError

from app.db.models import UserAccount
from app.db.session import get_session

# token -> {id, username, is_admin, name}; authorization always reloads the account.
_sessions: dict[str, dict] = {}
_session_lock = threading.RLock()
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
_COMMON_PASSWORDS = {"admin123", "clerk123", "acc123", "12345678", "password"}


class AccountError(ValueError):
    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.status_code = status_code


def validate_password(password: str):
    if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AccountError("密碼須為 8 至 128 字元")
    if password in _COMMON_PASSWORDS:
        raise AccountError("請勿使用常見或預設密碼")


def resolve_user(session, actor):
    """Use stable IDs for authenticated actors, while supporting historical callers."""
    if actor.get("id") is not None:
        return session.get(UserAccount, actor["id"])
    return session.exec(select(UserAccount).where(UserAccount.username == actor.get("username", ""))).first()


def _validate_account_fields(username: str, name: str):
    if not isinstance(username, str) or not 1 <= len(username.strip()) <= 64:
        raise AccountError("帳號須為 1 至 64 字元")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise AccountError("顯示名稱須為 1 至 100 字元")
    return username.strip(), name.strip()


def _identity(user: UserAccount):
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin, "name": user.name}


def public_account(user: UserAccount):
    return {**_identity(user), "is_active": user.is_active, "created_at": user.created_at}


def _check_admin(session, actor_id):
    # None is reserved for the trusted server-side CLI, never supplied by HTTP input.
    if actor_id is None:
        return
    actor = session.get(UserAccount, actor_id)
    if actor is None or not actor.is_active:
        raise AccountError("未登入", 401)
    if not actor.is_admin:
        raise AccountError("需管理員權限", 403)


def list_users(*, actor_id: int | None = None):
    with get_session() as session:
        _check_admin(session, actor_id)
        return [public_account(user) for user in session.exec(select(UserAccount).order_by(UserAccount.id)).all()]


def create_user(username: str, name: str, password: str, *, is_admin: bool = False, actor_id: int | None = None):
    username, name = _validate_account_fields(username, name)
    if not isinstance(is_admin, bool):
        raise AccountError("管理員權限設定不合法")
    if is_admin and actor_id is not None:
        raise AccountError("網頁新增帳號只能建立一般使用者", 403)
    validate_password(password)
    salt, password_hash = _hash(password)
    with get_session() as session:
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            _check_admin(session, actor_id)
            if session.exec(select(UserAccount).where(UserAccount.username == username)).first():
                raise AccountError("帳號已存在（包含已停用帳號）", 409)
            user = UserAccount(username=username, name=name, is_admin=is_admin, salt=salt, password_hash=password_hash)
            session.add(user)
            session.flush()
            result = public_account(user)
            session.commit()
            return result
        except IntegrityError:
            session.rollback()
            raise AccountError("帳號已存在（包含已停用帳號）", 409) from None


def update_user(user_id: int, username: str, name: str, password: str | None = None, *, actor_id: int):
    username, name = _validate_account_fields(username, name)
    if password is not None:
        validate_password(password)
        salt, password_hash = _hash(password)
    old_hash = None
    with get_session() as session:
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            _check_admin(session, actor_id)
            user = session.get(UserAccount, user_id)
            if user is None:
                raise AccountError("找不到帳號", 404)
            existing = session.exec(select(UserAccount).where(UserAccount.username == username)).first()
            if existing is not None and existing.id != user.id:
                raise AccountError("帳號已存在（包含已停用帳號）", 409)
            user.username, user.name = username, name
            if password is not None:
                old_hash = user.password_hash
                user.salt, user.password_hash = salt, password_hash
            session.add(user)
            session.flush()
            result = public_account(user)
            session.commit()
        except IntegrityError:
            session.rollback()
            raise AccountError("帳號已存在（包含已停用帳號）", 409) from None
    if old_hash is not None:
        revoke_user_sessions(user_id, password_hash=old_hash)
    return result


def disable_user(user_id: int, *, actor_id: int | None = None):
    with get_session() as session:
        # Serialize the admin count and the update across all processes.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        _check_admin(session, actor_id)
        user = session.get(UserAccount, user_id)
        if user is None:
            raise AccountError("找不到帳號", 404)
        if not user.is_active:
            return public_account(user)
        if user.id == actor_id:
            raise AccountError("不能停用目前登入的管理員帳號", 409)
        if user.is_admin:
            admins = session.exec(select(UserAccount.id).where(
                UserAccount.is_admin == True, UserAccount.is_active == True)).all()  # noqa: E712
            if len(admins) <= 1:
                raise AccountError("不能停用最後一位啟用中的管理員", 409)
        user.is_active = False
        session.add(user)
        session.flush()
        result = public_account(user)
        session.commit()
    revoke_user_sessions(user_id)
    return result


def revoke_user_sessions(user_id: int, *, password_hash: str | None = None):
    with _session_lock:
        for token in [token for token, user in _sessions.items() if user.get("id") == user_id
                      and (password_hash is None or user.get("_credential_hash") == password_hash)]:
            _sessions.pop(token, None)


# 預設種子帳號（首次啟動建立；正式部署請立即改密碼）
_SEED = [
    ("admin", "admin123", True, "系統管理員"),
    ("clerk", "clerk123", False, "記帳士"),
    ("accountant", "acc123", False, "會計師"),
]


def _hash(pw: str, salt: str = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100_000)
    return salt, dk.hex()


def ensure_seed_users():
    with get_session() as s:
        s.connection().exec_driver_sql("BEGIN IMMEDIATE")
        exists = s.exec(select(UserAccount)).first()
        if exists:
            return
        for username, pw, is_admin, name in _SEED:
            salt, ph = _hash(pw)
            s.add(UserAccount(username=username, password_hash=ph, salt=salt, is_admin=is_admin, name=name))
        s.commit()


def _authenticated_account(session, username: str, password: str):
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    user = session.exec(select(UserAccount).where(UserAccount.username == username)).first()
    if not user or not user.is_active:
        return None
    _, ph = _hash(password, user.salt)
    return user if hmac.compare_digest(ph, user.password_hash) else None


def authenticate(username: str, password: str):
    with get_session() as s:
        user = _authenticated_account(s, username, password)
        return _identity(user) if user else None


def authenticate_session(username: str, password: str):
    """Serialize password verification with account and password changes."""
    with get_session() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = _authenticated_account(session, username, password)
        if user is None:
            return None
        identity = _identity(user)
        return identity, _create_token(identity, user.password_hash)


def _create_token(user: dict, password_hash: str) -> str:
    token = secrets.token_urlsafe(24)
    with _session_lock:
        _sessions[token] = {**user, "_credential_hash": password_hash}
    return token


def create_token(user: dict) -> str:
    with get_session() as session:
        account = session.get(UserAccount, user.get("id"))
        if account is None or not account.is_active:
            raise AccountError("未登入", 401)
        return _create_token(_identity(account), account.password_hash)


def get_user_by_token(token: str | None):
    with _session_lock:
        identity = _sessions.get(token) if token else None
    if identity is None:
        return None
    with get_session() as session:
        user = session.get(UserAccount, identity.get("id")) if identity.get("id") is not None else None
        if (user is not None and user.is_active and isinstance(identity.get("_credential_hash"), str)
                and hmac.compare_digest(identity["_credential_hash"], user.password_hash)):
            return _identity(user)
    logout(token)
    return None


def logout(token: str | None):
    if token:
        with _session_lock:
            _sessions.pop(token, None)
