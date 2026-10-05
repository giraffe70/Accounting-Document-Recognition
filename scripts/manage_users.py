"""帳號管理工具：列出、新增、改密碼、停用。

系統的網頁介面只有登入／登出，沒有帳號管理畫面，因此種子帳號的預設密碼
（admin123 等）原本無法變更——這對處理財務資料的系統是不可接受的。
本工具提供最小可行的管理手段，供管理員在伺服器端執行。

用法（於專案根目錄）：
    python scripts/manage_users.py list
    python scripts/manage_users.py passwd <帳號>
    python scripts/manage_users.py add <帳號> <角色 clerk|accountant|admin> <顯示名稱>
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


def _prompt_password(username: str) -> str:
    while True:
        pw = getpass.getpass(f"請輸入 {username} 的新密碼（至少 {MIN_LEN} 字元）：")
        if len(pw) < MIN_LEN:
            print(f"  密碼太短（{len(pw)} 字元），請至少 {MIN_LEN} 字元。")
            continue
        if pw in ("admin123", "clerk123", "acc123", "12345678", "password"):
            print("  這是常見或預設密碼，請換一組。")
            continue
        if pw != getpass.getpass("請再輸入一次確認："):
            print("  兩次輸入不一致，請重新輸入。")
            continue
        return pw


def main():
    from sqlmodel import select

    from app.db.models import UserAccount
    from app.db.session import get_session
    from app.services import auth

    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    with get_session() as s:
        if cmd == "list":
            users = s.exec(select(UserAccount)).all()
            print(f"{'帳號':<16}{'角色':<12}{'顯示名稱':<16}{'密碼狀態'}")
            for u in users:
                # 用預設密碼驗一次，就能標出「尚未改過密碼」的帳號
                default = {"admin": "admin123", "clerk": "clerk123", "accountant": "acc123"}
                weak = ""
                d = default.get(u.username)
                if d:
                    _, ph = auth._hash(d, u.salt)
                    weak = "⚠ 仍為預設密碼" if ph == u.password_hash else "已變更"
                print(f"{u.username:<16}{ROLES.get(u.role, u.role):<12}{u.name:<16}{weak}")
            return

        if cmd == "passwd" and len(args) >= 2:
            u = s.exec(select(UserAccount).where(UserAccount.username == args[1])).first()
            if not u:
                sys.exit(f"找不到帳號：{args[1]}")
            salt, ph = auth._hash(_prompt_password(u.username))
            u.salt, u.password_hash = salt, ph
            s.add(u)
            s.commit()
            print(f"✓ 已變更 {u.username} 的密碼。該帳號現有的登入階段仍有效，"
                  f"如需強制登出請重新啟動伺服器。")
            return

        if cmd == "add" and len(args) >= 4:
            username, role, name = args[1], args[2], " ".join(args[3:])
            if role not in ROLES:
                sys.exit(f"角色須為 {'／'.join(ROLES)} 其中之一")
            if s.exec(select(UserAccount).where(UserAccount.username == username)).first():
                sys.exit(f"帳號已存在：{username}")
            salt, ph = auth._hash(_prompt_password(username))
            s.add(UserAccount(username=username, password_hash=ph, salt=salt,
                              role=role, name=name))
            s.commit()
            print(f"✓ 已新增 {username}（{ROLES[role]}／{name}）")
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

        if cmd == "delete" and len(args) >= 2:
            u = s.exec(select(UserAccount).where(UserAccount.username == args[1])).first()
            if not u:
                sys.exit(f"找不到帳號：{args[1]}")
            admins = [x for x in s.exec(select(UserAccount)).all() if x.role == "admin"]
            if u.role == "admin" and len(admins) <= 1:
                sys.exit("這是最後一個管理員帳號，刪除後將無人可管理系統，已中止。")
            if input(f"確定刪除 {u.username}（{u.name}）？輸入 yes 確認：") != "yes":
                sys.exit("已取消")
            s.delete(u)
            s.commit()
            print(f"✓ 已刪除 {u.username}")
            return

    print(__doc__)


if __name__ == "__main__":
    main()
