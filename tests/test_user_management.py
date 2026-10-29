"""Offline account, session, migration and API tests using only synthetic databases."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import session as db_session
from app.db.models import Company, ImportJob, ItemAccountMapping, MappingHistory, UserAccount, VoucherRecord
from app.services import auth, user_management
from app.services import item_account_mapping


def isolated_app():
    """Use real main endpoints/middleware without its production initialization."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app/main.py").read_text(encoding="utf-8"))
    namespace = {"Body": Body, "Request": Request, "JSONResponse": JSONResponse, "auth": auth}
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "_PUBLIC_API" for target in node.targets))
    namespace["_PUBLIC_API"] = ast.literal_eval(assignment.value)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "auth_guard", "api_login", "api_logout", "api_me"}:
            node.decorator_list = []
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "app/main.py", "exec"), namespace)
    app = FastAPI()
    app.include_router(user_management.router)
    app.middleware("http")(namespace["auth_guard"])
    app.post("/api/login")(namespace["api_login"])
    app.post("/api/logout")(namespace["api_logout"])
    app.get("/api/me")(namespace["api_me"])
    app.api_route("/api/config", methods=["GET", "POST"])(lambda: {"ok": True})
    app.api_route("/api/accounts", methods=["GET", "POST"])(lambda: {"ok": True})
    return app


class UserManagementTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.engine = create_engine(f"sqlite:///{root / 'users.db'}", connect_args={"check_same_thread": False})
        stack.callback(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        SQLModel.metadata.create_all(self.engine)
        stack.enter_context(patch.object(auth, "get_session", side_effect=lambda: Session(self.engine)))
        stack.enter_context(patch.object(auth, "_sessions", {}))
        self.admin = auth.create_user("root", "合成管理員", "Synthetic-123", is_admin=True)
        self.client = stack.enter_context(TestClient(isolated_app()))
        self.login("root")

    def login(self, username, password="Synthetic-123"):
        self.client.cookies.clear()
        return self.client.post("/api/login", json={"username": username, "password": password})

    def add_user(self, username="synthetic-user", *, is_admin=False):
        return auth.create_user(username, "合成人員", "Synthetic-123", is_admin=is_admin)

    def assert_account_error(self, code, function, *args, **kwargs):
        with self.assertRaises(auth.AccountError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, code)

    def test_api_authorization_for_every_method_and_no_store(self):
        victim = self.add_user()
        for identity in (None, "user", "admin"):
            with self.subTest(identity=identity):
                self.client.cookies.clear()
                if identity:
                    account = self.add_user(f"user-{identity}", is_admin=identity == "admin")
                    self.login(account["username"])
                expected = 401 if identity is None else 200 if identity == "admin" else 403
                for method, path, payload, success in (
                    ("GET", "/api/users", None, 200),
                    ("POST", "/api/users", {"username": "created", "name": "合成", "password": "New-user-123"}, 201),
                    ("DELETE", f"/api/users/{victim['id']}", None, 200),
                ):
                    response = self.client.request(method, path, **({"json": payload} if payload else {}))
                    self.assertEqual(response.status_code, success if expected == 200 else expected)
                    self.assertEqual(response.headers["cache-control"], "no-store")

    def test_navigation_settings_authorization_and_shared_chart_read(self):
        self.add_user("ordinary")
        for identity, expected in ((None, 401), ("ordinary", 403), ("root", 200)):
            self.client.cookies.clear()
            if identity:
                self.login(identity)
            for method, path in (("GET", "/api/config"), ("POST", "/api/config"),
                                 ("GET", "/api/accounts?company=example"), ("POST", "/api/accounts")):
                with self.subTest(identity=identity, method=method, path=path):
                    self.assertEqual(self.client.request(method, path).status_code, expected)
            shared_status = 401 if identity is None else 200
            self.assertEqual(self.client.get("/api/accounts?company_id=1").status_code, shared_status)

    def test_create_user_and_login_returns_safe_identity(self):
        response = self.client.post("/api/users", json={"username": "new-user", "name": "合成人員",
            "password": "New-user-123"})
        self.assertEqual(response.status_code, 201)
        account = response.json()
        self.assertEqual(set(account), {"id", "username", "name", "is_admin", "is_active", "created_at"})
        self.assertTrue(account["is_active"])
        self.assertFalse(account["is_admin"])
        identity = auth.authenticate(account["username"], "New-user-123")
        self.assertEqual(set(identity), {"id", "username", "name", "is_admin"})
        self.assertFalse(identity["is_admin"])
        response = self.client.get("/api/users")
        self.assertEqual(len(response.json()), 2)
        for secret in ("password", "password_hash", "salt", "New-user-123"):
            self.assertNotIn(secret, response.text)

    def test_web_cannot_create_admin_or_submit_obsolete_role(self):
        base = {"username": "test", "name": "系統管理員", "password": "ValidPass-123"}
        for extra in ({"is_admin": True}, {"role": "admin"}, {"role": "other:admin"}):
            self.assertEqual(self.client.post("/api/users", json={**base, **extra}).status_code, 422)
        self.assertEqual(self.client.post("/api/users", json=base).status_code, 201)
        self.login("test", "ValidPass-123")
        for endpoint in ("/api/users", "/api/backup", "/api/sysconfig"):
            self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.post("/api/users", json={**base, "is_admin": True}).status_code, 403)
        self.assert_account_error(403, auth.create_user, "no-admin", "合成", "ValidPass-123",
                                  is_admin=True, actor_id=self.admin["id"])

    def test_patch_authorization_and_secrets(self):
        user = self.add_user()
        for identity in (None, "user", "admin"):
            self.client.cookies.clear()
            if identity:
                other = self.add_user(f"patch-{identity}", is_admin=identity == "admin")
                self.login(other["username"])
            response = self.client.patch(f"/api/users/{user['id']}", json={
                "username": "updated", "name": "更新名稱", "password": "NewSecret-123"})
            self.assertEqual(response.status_code, 401 if identity is None else 200 if identity == "admin" else 403)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("NewSecret-123", response.text)
            self.assertNotIn("password_hash", response.text)
            self.assertNotIn("salt", response.text)

    def test_edit_account_preserves_identity_and_history(self):
        user = self.add_user()
        actor_snapshot = auth.authenticate(user["username"], "Synthetic-123")
        token = auth.create_token(actor_snapshot)
        with Session(self.engine) as session:
            session.add(VoucherRecord(confirmed_by=user["id"], confirmed_by_name="原名稱"))
            session.add(ImportJob(id="edited-owner-job", owner_id=user["id"]))
            session.commit()
        response = self.client.patch(f"/api/users/{user['id']}", json={
            "username": "  renamed-user  ", "name": "  更新名稱  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.json()["id"], response.json()["username"], response.json()["name"], response.json()["is_admin"]),
                         (user["id"], "renamed-user", "更新名稱", False))
        self.assertIsNone(auth.authenticate(user["username"], "Synthetic-123"))
        self.assertIsNotNone(auth.authenticate("renamed-user", "Synthetic-123"))
        self.assertEqual(auth.get_user_by_token(token)["username"], "renamed-user")
        with Session(self.engine) as session:
            self.assertEqual(auth.user_identity(session, actor_snapshot), (user["id"], "更新名稱"))
            with self.assertRaisesRegex(ValueError, "無法識別登入人員"):
                auth.user_identity(session, {"id": 99999, "username": "renamed-user"})
            data = {"items": [{"item_id": "item-a", "account_code": "123", "account_name": "合成"}]}
            item_account_mapping.audit_items(session, data, {"items": []}, actor_snapshot)
            self.assertEqual(data["items"][0]["account_updated_by"], user["id"])
            self.assertEqual(session.exec(select(VoucherRecord)).one().confirmed_by_name, "原名稱")
            self.assertEqual(session.get(ImportJob, "edited-owner-job").owner_id, user["id"])

    def test_edit_duplicate_inactive_missing_and_validation(self):
        user = self.add_user()
        disabled = self.add_user("reserved")
        auth.disable_user(disabled["id"], actor_id=self.admin["id"])
        self.assertEqual(self.client.patch(f"/api/users/{user['id']}", json={"username": "reserved", "name": "測試"}).status_code, 409)
        response = self.client.patch(f"/api/users/{disabled['id']}", json={"username": "inactive-renamed", "name": "停用修改"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_active"])
        self.assertIsNone(auth.authenticate("inactive-renamed", "Synthetic-123"))
        self.assertEqual(self.client.patch("/api/users/99999", json={"username": "x", "name": "測試"}).status_code, 404)
        for bad in ({"username": " ", "name": "測試"}, {"username": "ok", "name": " "},
                    {"username": "ok", "name": "測試", "password": "short"},
                    {"username": "ok", "name": "測試", "role": "admin"}):
            response = self.client.patch(f"/api/users/{user['id']}", json=bad)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("short", response.text)
        self.assertIsNotNone(auth.authenticate(user["username"], "Synthetic-123"))

    def test_password_reset_revokes_other_process_and_self_sessions(self):
        user = self.add_user()
        identity = auth.authenticate(user["username"], "Synthetic-123")
        token = auth.create_token(identity)
        old_cached = dict(auth._sessions[token])
        response = self.client.patch(f"/api/users/{user['id']}", json={
            "username": user["username"], "name": user["name"], "password": "NewPassword-123"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(auth.get_user_by_token(token))
        auth._sessions[token] = old_cached  # token held by another server process
        self.assertIsNone(auth.get_user_by_token(token))
        self.assertIsNone(auth.authenticate(user["username"], "Synthetic-123"))
        self.assertIsNotNone(auth.authenticate(user["username"], "NewPassword-123"))
        response = self.client.patch(f"/api/users/{self.admin['id']}", json={
            "username": "root", "name": "合成管理員", "password": "NewAdminPassword-123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.login("root", "NewAdminPassword-123").status_code, 200)

    def test_login_and_password_reset_are_serialized(self):
        user = self.add_user()
        barrier = threading.Barrier(2)

        def login():
            barrier.wait(timeout=10)
            return auth.authenticate_session(user["username"], "Synthetic-123")

        def reset():
            barrier.wait(timeout=10)
            return auth.update_user(user["id"], user["username"], user["name"], "ChangedPassword-123", actor_id=self.admin["id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            login_result = pool.submit(login)
            reset_result = pool.submit(reset)
            old_login = login_result.result(timeout=15)
            reset_result.result(timeout=15)
        if old_login:
            self.assertIsNone(auth.get_user_by_token(old_login[1]))
        self.assertIsNotNone(auth.authenticate(user["username"], "ChangedPassword-123"))

    def test_concurrent_renames_keep_usernames_unique(self):
        first, second = self.add_user("rename-a"), self.add_user("rename-b")
        barrier = threading.Barrier(2)

        def rename(user):
            barrier.wait(timeout=10)
            try:
                auth.update_user(user["id"], "shared-name", user["name"], actor_id=self.admin["id"])
                return 200
            except auth.AccountError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(rename, [first, second]))
        self.assertEqual(sorted(results), [200, 409])

    def test_inputs_password_policy_and_validation_never_echo_secrets(self):
        valid = {"username": "valid", "name": "合成", "password": "Do-not-echo-123"}
        for change in ({"role": "owner"}, {"username": "  "}, {"name": "  "}, {"username": "x" * 65},
                       {"name": "x" * 101}, {"username": 42}, {"password": 12345678}, {"password": "x" * 129},
                       {"password": "short"}, {"password": "12345678"}, {"password": "password"},
                       {"password": "admin123"}, {"password": "clerk123"}, {"password": "acc123"},
                       {"salt": "forbidden-secret"}):
            with self.subTest(fields=list(change)):
                response = self.client.post("/api/users", json={**valid, **change})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("Do-not-echo-123", response.text)
                self.assertNotIn("forbidden-secret", response.text)
                self.assertNotIn('"input"', response.text)
        response = self.client.post("/api/users", json=[valid])
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(valid["password"], response.text)

    def test_trim_case_sensitive_names_and_reserve_disabled_username(self):
        user = auth.create_user("  CaseUser  ", "  合成人員  ", "Synthetic-123")
        self.assertEqual((user["username"], user["name"]), ("CaseUser", "合成人員"))
        self.add_user("caseuser")
        auth.disable_user(user["id"], actor_id=self.admin["id"])
        self.assert_account_error(409, auth.create_user, " CaseUser ", "合成", "Synthetic-123")
        with Session(self.engine) as session:
            session.add(UserAccount(username="CaseUser"))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_disable_revokes_all_sessions_and_checks_other_process_tokens(self):
        user = self.add_user()
        identity = auth.authenticate(user["username"], "Synthetic-123")
        tokens = [auth.create_token(identity) for _ in range(3)]
        for token in tokens:
            self.assertIsNotNone(auth.get_user_by_token(token))
        response = self.client.delete(f"/api/users/{user['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_active"])
        self.assertIsNone(auth.authenticate(user["username"], "Synthetic-123"))
        for token in tokens:
            self.assertIsNone(auth.get_user_by_token(token))
        # A different worker process may still have a cached session identity.
        auth._sessions[tokens[0]] = identity
        self.assertIsNone(auth.get_user_by_token(tokens[0]))
        self.assertNotIn(tokens[0], auth._sessions)
        self.assertEqual(self.login(user["username"]).status_code, 401)
        auth._sessions.clear()  # restart clears sessions, but not the disabled state
        auth.ensure_seed_users()
        self.assertIsNone(auth.authenticate(user["username"], "Synthetic-123"))
        self.login("root")
        accounts = self.client.get("/api/users").json()
        self.assertFalse(next(account for account in accounts if account["id"] == user["id"])["is_active"])
        self.assertEqual(self.client.delete(f"/api/users/{user['id']}").status_code, 200)

    def test_sessions_reflect_current_admin_flag_and_deleted_identity(self):
        token = self.client.cookies.get("session")
        with Session(self.engine) as session:
            user = session.get(UserAccount, self.admin["id"])
            user.is_admin, user.name = False, "新顯示名稱"
            session.add(user)
            session.commit()
        self.assertEqual(auth.get_user_by_token(token)["name"], "新顯示名稱")
        self.assertFalse(self.client.get("/api/me").json()["is_admin"])
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        with Session(self.engine) as session:
            session.delete(session.get(UserAccount, self.admin["id"]))
            session.commit()
        self.assertIsNone(auth.get_user_by_token(token))
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_disable_keeps_all_history_and_foreign_keys(self):
        user = self.add_user()
        with Session(self.engine) as session:
            company = Company(name="合成公司", account_chart_key="synthetic")
            session.add(company)
            session.flush()
            voucher = VoucherRecord(confirmed_by=user["id"], confirmed_by_name="原確認人")
            session.add(voucher)
            session.flush()
            mapping = ItemAccountMapping(company_id=company.id, item_name_key="合成品名", created_by=user["id"],
                updated_by=user["id"], source_voucher_id=voucher.id)
            session.add(mapping)
            session.flush()
            session.add(MappingHistory(mapping_id=mapping.id, version=1, actor_id=user["id"]))
            session.add(ImportJob(id="synthetic-job", owner_id=user["id"]))
            session.commit()
        auth.disable_user(user["id"], actor_id=self.admin["id"])
        with Session(self.engine) as session:
            voucher = session.exec(select(VoucherRecord)).one()
            self.assertEqual(voucher.confirmed_by, user["id"])
            self.assertEqual(voucher.confirmed_by_name, "原確認人")
            mapping = session.exec(select(ItemAccountMapping)).one()
            self.assertEqual((mapping.created_by, mapping.updated_by), (user["id"], user["id"]))
            self.assertEqual(session.exec(select(MappingHistory)).one().actor_id, user["id"])
            self.assertEqual(session.get(ImportJob, "synthetic-job").owner_id, user["id"])
            self.assertEqual(list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")), [])

    def test_protected_accounts_missing_ids_and_permission_recheck(self):
        self.assertEqual(self.client.delete(f"/api/users/{self.admin['id']}").status_code, 409)
        self.assert_account_error(409, auth.disable_user, self.admin["id"])
        self.assertEqual(self.client.delete("/api/users/99999").status_code, 404)
        self.assertEqual(self.client.delete("/api/users/invalid").status_code, 422)
        second = self.add_user("second-admin", is_admin=True)
        auth.disable_user(second["id"], actor_id=self.admin["id"])
        self.assert_account_error(401, auth.disable_user, self.admin["id"], actor_id=second["id"])
        self.assert_account_error(401, auth.create_user, "forbidden", "合成", "Synthetic-123", actor_id=second["id"])
        clerk = self.add_user("clerk")
        self.assert_account_error(403, auth.disable_user, self.admin["id"], actor_id=clerk["id"])

    def test_concurrent_creation_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)

        def create():
            barrier.wait(timeout=10)
            try:
                auth.create_user("concurrent", "合成", "Synthetic-123", actor_id=self.admin["id"])
                return 201
            except auth.AccountError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(results), [201, 409])

    def test_concurrent_mutual_deactivation_keeps_an_active_admin(self):
        second = self.add_user("second-admin", is_admin=True)
        barrier = threading.Barrier(2)

        def disable(pair):
            actor, target = pair
            barrier.wait(timeout=10)
            try:
                auth.disable_user(target, actor_id=actor)
                return 200
            except auth.AccountError as exc:
                return exc.status_code

        pairs = [(self.admin["id"], second["id"]), (second["id"], self.admin["id"])]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(disable, pairs))
        self.assertEqual(sorted(results), [200, 401])
        self.assertEqual(sum(user["is_active"] for user in auth.list_users()), 1)

    def test_concurrent_cli_deactivation_protects_last_admin(self):
        second = self.add_user("second-admin", is_admin=True)
        barrier = threading.Barrier(2)

        def disable(user_id):
            barrier.wait(timeout=10)
            try:
                auth.disable_user(user_id)
                return 200
            except auth.AccountError as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(disable, [self.admin["id"], second["id"]]))
        self.assertEqual(sorted(results), [200, 409])

    def test_server_cli_add_list_delete_uses_shared_policy(self):
        import scripts.manage_users as cli
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(db_session, "_engine", self.engine))
        stack.enter_context(patch("builtins.print"))
        stack.enter_context(patch.object(cli, "_prompt_password", return_value="Synthetic-123"))
        with patch.object(cli.sys, "argv", ["manage_users.py", "add", "cli-user", "CLI合成"]):
            cli.main()
        with patch.object(cli.sys, "argv", ["manage_users.py", "add", "--admin", "cli-admin", "合成管理員"]):
            cli.main()
        self.assertTrue(next(user for user in auth.list_users() if user["username"] == "cli-admin")["is_admin"])
        account = next(user for user in auth.list_users() if user["username"] == "cli-user")
        token = auth.create_token(auth.authenticate("cli-user", "Synthetic-123"))
        with patch.object(cli.sys, "argv", ["manage_users.py", "passwd", "cli-user"]):
            cli.main()
        self.assertIsNone(auth.get_user_by_token(token))
        with patch.object(cli.sys, "argv", ["manage_users.py", "list"]):
            cli.main()
        with patch.object(cli.sys, "argv", ["manage_users.py", "delete", "cli-user"]), patch("builtins.input", return_value="yes"):
            cli.main()
        self.assertFalse(next(user for user in auth.list_users() if user["id"] == account["id"])["is_active"])


class UserMigrationTests(unittest.TestCase):
    def test_legacy_migration_is_idempotent_keeps_passwords_and_enforces_uniqueness(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'old.db'}")
            stack.callback(engine.dispose)
            salt, password_hash = auth._hash("Legacy-123")
            with engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, name VARCHAR, role VARCHAR, salt VARCHAR, password_hash VARCHAR, created_at DATETIME)")
                rows = [(1, "legacy", "admin"), (2, "clerk-user", "clerk"), (3, "accountant-user", "accountant"),
                        (4, "custom-admin", "other:admin"), (5, "custom-chinese", "other:管理員")]
                for user_id, username, role in rows:
                    conn.exec_driver_sql("INSERT INTO users VALUES (?, ?, '合成舊帳號', ?, ?, ?, '2026-01-01 00:00:00')",
                                         (user_id, username, role, salt, password_hash))
                conn.exec_driver_sql("CREATE TABLE history (id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id))")
                conn.exec_driver_sql("INSERT INTO history VALUES (1, 4)")
            with patch.object(db_session, "_engine", engine), patch.object(auth, "get_session", side_effect=lambda: Session(engine)):
                db_session._migrate_users()
                db_session._migrate_users()
                self.assertEqual(auth.authenticate("legacy", "Legacy-123")["id"], 1)
                with Session(engine) as session:
                    user = session.get(UserAccount, 1)
                    self.assertIs(user.is_active, True)
                    self.assertIs(user.is_admin, True)
                    self.assertEqual((user.salt, user.password_hash), (salt, password_hash))
                    for user_id in (2, 3, 4, 5):
                        self.assertIs(session.get(UserAccount, user_id).is_admin, False)
                    columns = {row[1] for row in session.connection().exec_driver_sql("PRAGMA table_info(users)")}
                    self.assertIn("is_admin", columns)
                    self.assertNotIn("role", columns)
                    self.assertEqual(session.connection().exec_driver_sql("SELECT actor_id FROM history").scalar(), 4)
                    self.assertEqual(list(session.connection().exec_driver_sql("PRAGMA foreign_key_check")), [])
                    session.add(UserAccount(username="legacy"))
                    with self.assertRaises(IntegrityError):
                        session.commit()

    def test_conflicting_existing_admin_flag_rolls_back_without_dropping_role(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'conflict.db'}")
            stack.callback(engine.dispose)
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, role VARCHAR, is_admin INTEGER)")
                conn.exec_driver_sql("INSERT INTO users VALUES (1, 'legacy-admin', 'admin', 0)")
            with patch.object(db_session, "_engine", engine):
                with self.assertRaisesRegex(RuntimeError, "不一致"):
                    db_session._migrate_users()
            with engine.connect() as conn:
                self.assertEqual(tuple(conn.exec_driver_sql("SELECT * FROM users").one()), (1, "legacy-admin", "admin", 0))
                columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
                self.assertIn("role", columns)
                self.assertNotIn("is_active", columns)

    def test_missing_admin_evidence_refuses_to_guess_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'missing.db'}")
            stack.callback(engine.dispose)
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)")
                conn.exec_driver_sql("INSERT INTO users VALUES (1, 'legacy')")
            with patch.object(db_session, "_engine", engine):
                with self.assertRaisesRegex(RuntimeError, "無法判斷管理員"):
                    db_session._migrate_users()
            with engine.connect() as conn:
                self.assertEqual({row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}, {"id", "username"})

    def test_text_admin_flag_is_rejected_before_role_removal(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'text-admin.db'}")
            stack.callback(engine.dispose)
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, role VARCHAR, is_admin VARCHAR)")
                conn.exec_driver_sql("INSERT INTO users VALUES (1, 'legacy', 'clerk', '0')")
            with patch.object(db_session, "_engine", engine):
                with self.assertRaisesRegex(RuntimeError, "必須為整數"):
                    db_session._migrate_users()
            with engine.connect() as conn:
                self.assertIn("role", {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")})

    def test_new_install_seed_creates_only_one_admin(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'new.db'}")
            stack.callback(engine.dispose)
            SQLModel.metadata.create_all(engine)
            with patch.object(auth, "get_session", side_effect=lambda: Session(engine)):
                auth.ensure_seed_users()
                auth.ensure_seed_users()
                users = auth.list_users()
                self.assertEqual(len(users), 3)
                self.assertEqual([user["username"] for user in users if user["is_admin"]], ["admin"])
                self.assertTrue(all("role" not in user for user in users))

    def test_init_db_preserves_existing_disabled_accounts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'existing.db'}")
            stack.callback(engine.dispose)
            salt, password_hash = auth._hash("Legacy-123")
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, name VARCHAR, "
                    "role VARCHAR, salt VARCHAR, password_hash VARCHAR, is_active INTEGER NOT NULL DEFAULT 1, created_at DATETIME)")
                for user_id, username, role, active in ((1, "live-admin", "admin", 1),
                        (2, "disabled-admin", "admin", 0), (3, "disabled-user", "other:主管", 0)):
                    conn.exec_driver_sql("INSERT INTO users VALUES (?, ?, '合成人員', ?, ?, ?, ?, '2026-01-01 00:00:00')",
                                         (user_id, username, role, salt, password_hash, active))
            with patch.object(db_session, "_engine", engine), \
                    patch.object(db_session, "_migrate_account_mappings"), \
                    patch.object(auth, "get_session", side_effect=lambda: Session(engine)):
                db_session.init_db()
                db_session.init_db()
                self.assertTrue(auth.authenticate("live-admin", "Legacy-123")["is_admin"])
                self.assertIsNone(auth.authenticate("disabled-admin", "Legacy-123"))
                self.assertIsNone(auth.authenticate("disabled-user", "Legacy-123"))
                self.assertEqual([(user["id"], user["is_admin"], user["is_active"]) for user in auth.list_users()],
                                 [(1, True, True), (2, True, False), (3, False, False)])
                with engine.connect() as conn:
                    self.assertNotIn("role", {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")})

    def test_duplicate_legacy_usernames_fail_without_rewriting_identities(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            engine = create_engine(f"sqlite:///{Path(directory) / 'duplicate.db'}")
            stack.callback(engine.dispose)
            with engine.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR)")
                conn.exec_driver_sql("INSERT INTO users VALUES (1, 'duplicate'), (2, 'duplicate')")
            with patch.object(db_session, "_engine", engine):
                with self.assertRaisesRegex(RuntimeError, "重複帳號"):
                    db_session._migrate_users()
            with engine.connect() as conn:
                self.assertEqual(list(conn.exec_driver_sql("SELECT * FROM users")), [(1, "duplicate"), (2, "duplicate")])
                self.assertNotIn("is_active", [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")])
