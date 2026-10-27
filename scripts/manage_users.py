"""帳號管理工具：列出、新增、改密碼、停用。

網頁「權限管理」提供新增與停用；本工具供管理員在伺服器端維護，
另可變更密碼與顯示名稱。刪除採停用方式，保留歷史關聯。

用法（於專案根目錄）：
    python scripts/manage_users.py list
    python scripts/manage_users.py passwd <帳號>
    python scripts/manage_users.py add <帳號> <角色 clerk|accountant|admin|other:名稱> <顯示名稱>
    python scripts/manage_users.py rename <帳號> <新顯示名稱>
    python scripts/manage_users.py delete <帳號>

密碼一律以互動方式輸入（不從命令列參數讀取，避免留在 shell 歷史紀錄）。
"""
import getpass
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROLES = {"clerk": "記帳士", "accountant": "會計師", "admin": "管理員"}
MIN_LEN = 8


def _role_label(role: str) -> str:
    return f"其他：{role[6:]}" if role.startswith("other:") else ROLES.get(role, role)


def _prompt_password(username: str) -> str:
    from app.services import auth

    while True:
        pw = getpass.getpass(f"請輸入 {username} 的新密碼（至少 {MIN_LEN} 字元）：")
        try:
            auth.validate_password(pw)
        except auth.AccountError as exc:
            print(f"  {exc}。")
            continue
        if pw != getpass.getpass("請再輸入一次確認："):
            print("  兩次輸入不一致，請重新輸入。")
            continue
        return pw


def main():
    from sqlmodel import select

    from app.db.models import UserAccount
    from app.db.session import get_session, _migrate_users
    from app.services import auth

    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    # Support existing installations before the next web-server restart.
    _migrate_users()

    if cmd == "add" and len(args) >= 4:
        username, role, name = args[1], args[2], " ".join(args[3:])
        try:
            user = auth.create_user(username, name, _prompt_password(username), role)
        except auth.AccountError as exc:
            sys.exit(str(exc))
        print(f"✓ 已新增 {user['username']}（{_role_label(user['role'])}／{user['name']}）")
        return

    if cmd == "delete" and len(args) >= 2:
        with get_session() as session:
            user = session.exec(select(UserAccount).where(UserAccount.username == args[1])).first()
            if user is None:
                sys.exit(f"找不到帳號：{args[1]}")
            user_id, username, name = user.id, user.username, user.name
        if input(f"確定刪除（停用）{username}（{name}）？既有登入將失效，歷史紀錄保留。輸入 yes 確認：") != "yes":
            sys.exit("已取消")
        try:
            auth.disable_user(user_id)
        except auth.AccountError as exc:
            sys.exit(str(exc))
        print(f"✓ 已停用 {username}，歷史紀錄已保留")
        return

    with get_session() as s:
        if cmd == "list":
            users = s.exec(select(UserAccount).order_by(UserAccount.id)).all()
            print(f"{'帳號':<16}{'角色':<12}{'顯示名稱':<16}{'啟用狀態':<12}{'密碼狀態'}")
            for u in users:
                # 用預設密碼驗一次，就能標出「尚未改過密碼」的帳號
                default = {"admin": "admin123", "clerk": "clerk123", "accountant": "acc123"}
                weak = ""
                d = default.get(u.username)
                if d:
                    _, ph = auth._hash(d, u.salt)
                    weak = "⚠ 仍為預設密碼" if ph == u.password_hash else "已變更"
                state = "啟用" if u.is_active else "已停用"
                print(f"{u.username:<16}{_role_label(u.role):<12}{u.name:<16}{state:<12}{weak}")
            return

        if cmd == "passwd" and len(args) >= 2:
            u = s.exec(select(UserAccount).where(UserAccount.username == args[1])).first()
            if not u:
                sys.exit(f"找不到帳號：{args[1]}")
            salt, ph = auth._hash(_prompt_password(u.username))
            u.salt, u.password_hash = salt, ph
            s.add(u)
            s.commit()
            print(f"✓ 已變更 {u.username} 的密碼，該帳號現有的登入階段將於下次請求時失效。")
            return

        if cmd == "rename" and len(args) >= 3:
            u = s.exec(select(UserAccount).where(UserAccount.username == args[1])).first()
            if not u:
                sys.exit(f"找不到帳號：{args[1]}")
            u.name = " ".join(args[2:])
            s.add(u)
            s.commit()
            print(f"✓ 已更名為 {u.name}")
            return

    print(__doc__)


if __name__ == "__main__":
    main()
