import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERS_JS = ROOT / "app" / "static" / "js" / "users.js"
HARNESS = ROOT / "tests" / "fixtures" / "users_synthetic.js"
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"


@unittest.skipUnless(Path(NODE).is_file(), "Node.js is required for the offline frontend harness")
class UsersFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = USERS_JS.read_text(encoding="utf-8")

    def run_frontend(self, **scenario):
        result = subprocess.run(
            [NODE, str(HARNESS)],
            input=json.dumps({"source": self.source, "scenario": scenario}),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_auth_failure_and_non_admin_never_load_user_list(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result = self.run_frontend(meStatus=status, me={"detail": "denied"})
                self.assertTrue(result["usersPanelHidden"])
                self.assertFalse(any(call["url"] == "/api/users" for call in result["calls"]))
                self.assertEqual(result["usersRows"], [])

        result = self.run_frontend(me={"id": 8, "is_admin": False, "name": "使用者"})
        self.assertTrue(result["usersPanelHidden"])
        self.assertFalse(any(call["url"] == "/api/users" for call in result["calls"]))
        self.assertEqual(result["usersRows"], [])

    def test_admin_loads_and_displays_users_as_text(self):
        hostile = '<img src=x onerror="alert(1)">'
        result = self.run_frontend(users=[
            {"id": 2, "username": hostile, "name": hostile, "is_admin": False, "is_active": True, "created_at": "bad-date"},
        ])
        self.assertFalse(result["usersPanelHidden"])
        self.assertEqual(len(result["usersRows"]), 1)
        cells = result["usersRows"][0]["children"]
        self.assertEqual(cells[0]["textContent"], hostile)
        self.assertEqual(cells[1]["textContent"], hostile)
        self.assertEqual([cell["textContent"] for cell in cells], [hostile, hostile, "啟用", "bad-date", ""])
        self.assertEqual(result["innerHTMLWrites"], [])
        self.assertEqual([call["url"] for call in result["calls"]], ["/api/me", "/api/users"])

    def test_own_last_admin_and_inactive_accounts_have_no_enabled_disable_action(self):
        self_user = {"id": 1, "username": "root", "name": "管理員", "is_admin": True, "is_active": True, "created_at": "2026-01-01"}
        other_admin = {"id": 2, "username": "admin2", "name": "另一管理員", "is_admin": True, "is_active": True, "created_at": "2026-01-01"}
        inactive = {"id": 3, "username": "old", "name": "已停用", "is_admin": False, "is_active": False, "created_at": "2026-01-01"}
        own = self.run_frontend(users=[self_user, other_admin, inactive])
        self.assertTrue(own["usersRows"][0]["children"][-1]["children"][0]["children"][1]["disabled"])
        self.assertFalse(own["usersRows"][1]["children"][-1]["children"][0]["children"][1]["disabled"])
        inactive_actions = own["usersRows"][2]["children"][-1]["children"][0]["children"]
        self.assertEqual([action["textContent"] for action in inactive_actions], ["編輯", "已停用"])

        last_admin = self.run_frontend(me={"id": 10, "is_admin": True, "name": "管理員"}, users=[
            {"id": 20, "username": "sole", "name": "唯一管理員", "is_admin": True, "is_active": True, "created_at": "2026-01-01"},
        ])
        self.assertTrue(last_admin["usersRows"][0]["children"][-1]["children"][0]["children"][1]["disabled"])

    def test_password_mismatch_does_not_post(self):
        result = self.run_frontend(action="submit", form={"password": "password-123", "confirmPassword": "different-123"})
        self.assertFalse(any(call["method"] == "POST" for call in result["calls"]))
        self.assertIn("不一致", result["messages"]["formMessage"])

    def test_create_success_posts_only_account_fields_and_refreshes_list(self):
        result = self.run_frontend(action="submit", form={
            "username": "new-user", "name": "新使用者",
            "password": "password-123", "confirmPassword": "password-123",
        }, listResponses=[
            {"status": 200, "data": []},
            {"status": 200, "data": [{"id": 9, "username": "new-user", "name": "新使用者", "is_admin": False, "is_active": True, "created_at": "2026-01-01"}]},
        ])
        self.assertEqual([(call["method"], call["url"]) for call in result["calls"]], [
            ("GET", "/api/me"), ("GET", "/api/users"), ("POST", "/api/users"), ("GET", "/api/users"),
        ])
        self.assertEqual(result["calls"][2]["body"], {"username": "new-user", "name": "新使用者", "password": "password-123"})
        self.assertEqual(result["form"], {"username": "", "displayName": "", "password": "", "confirmPassword": ""})
        self.assertIn("已新增帳號", result["messages"]["formMessage"])
        self.assertEqual(len(result["usersRows"]), 1)

    def test_delete_cancel_shows_informed_confirmation_without_request(self):
        result = self.run_frontend(action="delete", confirm=False, users=[
            {"id": 2, "username": "clerk", "name": "記帳士", "is_admin": False, "is_active": True, "created_at": "2026-01-01"},
        ])
        self.assertEqual(len(result["confirms"]), 1)
        self.assertIn("停用", result["confirms"][0])
        self.assertIn("保留", result["confirms"][0])
        self.assertFalse(any(call["method"] == "DELETE" for call in result["calls"]))

    def test_delete_confirmed_marks_account_inactive_and_reports_history_retention(self):
        result = self.run_frontend(action="delete", confirm=True, users=[
            {"id": 2, "username": "clerk", "name": "記帳士", "is_admin": False, "is_active": True, "created_at": "2026-01-01"},
        ])
        self.assertTrue(any(call["method"] == "DELETE" and call["url"] == "/api/users/2" for call in result["calls"]))
        self.assertEqual(result["usersRows"][0]["children"][2]["textContent"], "已停用")
        self.assertEqual(result["usersRows"][0]["children"][-1]["children"][0]["children"][1]["textContent"], "已停用")
        self.assertIn("歷史紀錄已保留", result["messages"]["listMessage"])

    def test_operation_auth_failure_clears_users_and_passwords_and_hides_panel(self):
        for status in (401, 403):
            with self.subTest(status=status):
                result = self.run_frontend(action="submit", form={
                    "username": "new-user", "name": "新使用者",
                    "password": "password-123", "confirmPassword": "password-123",
                }, postResponse={"status": status, "data": {"detail": "denied"}})
                self.assertTrue(result["usersPanelHidden"])
                self.assertEqual(result["usersRows"], [])
                self.assertEqual(result["form"]["password"], "")
                self.assertEqual(result["form"]["confirmPassword"], "")
                self.assertFalse(result["calls"][-1]["url"] == "/api/users" and result["calls"][-1]["method"] == "GET")

    def test_server_conflict_is_shown_to_the_user(self):
        result = self.run_frontend(action="submit", postResponse={
            "status": 409, "data": {"detail": "帳號已存在"},
        })
        self.assertEqual(result["messages"]["formMessage"], "帳號已存在")
        self.assertTrue(result["messageErrors"]["formMessage"])

    def test_create_form_has_no_role_or_permission_input(self):
        html = (ROOT / "app/static/users.html").read_text(encoding="utf-8")
        form = html.split('<form id="userForm"', 1)[1].split('</form>', 1)[0]
        self.assertNotIn('id="role"', form)
        self.assertNotIn('id="customRole"', form)
        self.assertNotIn('name="role"', form)
        self.assertNotIn('name="customRole"', form)

    def test_admin_and_regular_users_have_clear_account_labels(self):
        result = self.run_frontend(users=[
            {"id": 2, "username": "root", "name": "管理員", "is_admin": True, "is_active": True, "created_at": "2026-01-01"},
            {"id": 3, "username": "staff", "name": "員工", "is_admin": False, "is_active": True, "created_at": "2026-01-01"},
        ])
        self.assertEqual(result["usersRows"][0]["children"][0]["textContent"], "root（系統管理員）")
        self.assertEqual(result["usersRows"][1]["children"][0]["textContent"], "staff")
        self.assertEqual([len(row["children"]) for row in result["usersRows"]], [5, 5])

    def test_edit_username_name_and_optional_password(self):
        user = {"id": 2, "username": "old", "name": "原名稱", "is_admin": False, "is_active": True, "created_at": "2026-01-01"}
        result = self.run_frontend(action="edit", users=[user], editForm={"username": "new", "name": "新名稱"})
        patch = next(call for call in result["calls"] if call["method"] == "PATCH")
        self.assertEqual((patch["url"], patch["body"]), ("/api/users/2", {"username": "new", "name": "新名稱"}))
        self.assertTrue(result["editPanelHidden"])
        self.assertEqual(result["usersRows"][0]["children"][0]["textContent"], "new")
        reset = self.run_frontend(action="edit", users=[user], editForm={"username": "old", "name": "原名稱", "password": "FreshPassword-123"})
        self.assertEqual(next(call for call in reset["calls"] if call["method"] == "PATCH")["body"]["password"], "FreshPassword-123")
        self.assertIn("登入已失效", reset["messages"]["listMessage"])
        self.assertEqual(reset["editForm"]["editPassword"], "")

    def test_edit_cancel_mismatch_and_server_error(self):
        user = {"id": 2, "username": "old", "name": "原名稱", "is_admin": False, "is_active": False, "created_at": "2026-01-01"}
        cancelled = self.run_frontend(action="cancelEdit", users=[user])
        self.assertTrue(cancelled["editPanelHidden"])
        self.assertEqual(cancelled["editForm"]["editPassword"], "")
        self.assertFalse(any(call["method"] == "PATCH" for call in cancelled["calls"]))
        mismatch = self.run_frontend(action="edit", users=[user], editForm={"password": "goodpass-123", "confirmPassword": "different-123"})
        self.assertFalse(any(call["method"] == "PATCH" for call in mismatch["calls"]))
        self.assertIn("不一致", mismatch["messages"]["editMessage"])
        weak = self.run_frontend(action="edit", users=[user], editForm={"password": "short"})
        self.assertFalse(any(call["method"] == "PATCH" for call in weak["calls"]))
        conflict = self.run_frontend(action="edit", users=[user], patchResponse={"status": 409, "data": {"detail": "帳號已存在"}})
        self.assertFalse(conflict["editPanelHidden"])
        self.assertEqual(conflict["messages"]["editMessage"], "帳號已存在")

    def test_self_password_change_and_expired_admin_clear_password_fields(self):
        admin = {"id": 1, "username": "root", "name": "管理員", "is_admin": True, "is_active": True, "created_at": "2026-01-01"}
        result = self.run_frontend(action="edit", users=[admin], editIsAdmin=True, editForm={"password": "NewAdmin-123"})
        self.assertTrue(result["usersPanelHidden"])
        self.assertTrue(result["editPanelHidden"])
        self.assertIn("重新登入", result["messages"]["accessMessage"])
        self.assertEqual(result["editForm"]["editPassword"], "")
        self.assertEqual(result["editForm"]["editConfirmPassword"], "")
        self.assertEqual(result["calls"][-1]["method"], "PATCH")
        denied = self.run_frontend(action="edit", users=[admin], editForm={"password": "NewAdmin-123"},
                                   patchResponse={"status": 401, "data": {"detail": "未登入"}})
        self.assertTrue(denied["usersPanelHidden"])
        self.assertEqual(denied["editForm"]["editPassword"], "")


if __name__ == "__main__":
    unittest.main()
