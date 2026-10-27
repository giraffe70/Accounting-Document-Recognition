import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.services import auth, database_view


class DatabaseViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.auth_engine = create_engine(f"sqlite:///{root / 'auth.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.auth_engine.dispose)
        SQLModel.metadata.create_all(self.auth_engine)
        auth_patch = patch.object(auth, "get_session", side_effect=lambda: Session(self.auth_engine))
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        self.identities = {}
        self.db = root / "app.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, salt TEXT, is_admin INTEGER);
            INSERT INTO users VALUES (1, '管理員', 'secret-hash', 'secret-salt', 1);
            CREATE TABLE voucher_records (
                id INTEGER PRIMARY KEY, company TEXT, current_json TEXT,
                correction_note TEXT DEFAULT '', confirmed_at TEXT,
                entries_status TEXT DEFAULT 'none', entries_confirmed_at TEXT,
                entries_confirmed_by INTEGER, entries_confirmed_by_name TEXT DEFAULT '',
                entries_proposal_json TEXT DEFAULT '{}', entries_history TEXT DEFAULT '[]'
            );
            CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT, short TEXT, tax_id TEXT, account_chart_key TEXT);
            CREATE TABLE item_account_mappings (id INTEGER PRIMARY KEY, company_id INTEGER, item_name_key TEXT,
                account_code TEXT, account_name TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE mapping_history (id INTEGER PRIMARY KEY, mapping_id INTEGER, version INTEGER, created_at TEXT);
            CREATE TABLE empty_table (id INTEGER PRIMARY KEY);
        """)
        con.executemany("INSERT INTO voucher_records (id, company, current_json) VALUES (?, ?, ?)",
                        [(i, "台灣公司" if i % 2 else "100%_公司", '{"total": 123}') for i in range(1, 58)])
        con.commit()
        con.close()
        self.original = self.db.read_bytes()
        mapping = {"app": ("憑證與帳號", self.db), "missing": ("尚未建立", root / "missing.db")}
        self.patch = patch.object(database_view, "databases", return_value=mapping)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app = FastAPI()
        app.include_router(database_view.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.tokens = []
        self.addCleanup(lambda: [auth.logout(token) for token in self.tokens])

    def login(self, identity="admin"):
        if identity not in self.identities:
            self.identities[identity] = auth.create_user(
                f"test-{identity}", "測試", "Synthetic-123", is_admin=identity == "admin")
        token = auth.create_token(self.identities[identity])
        self.tokens.append(token)
        self.client.cookies.set("session", token)

    def rows(self, **params):
        return self.client.get("/api/database/rows", params={"database": "app", "table": "voucher_records", **params})

    def test_requires_admin_for_both_endpoints(self):
        for identity, status in [(None, 401), ("clerk", 403), ("accountant", 403), ("admin", 200)]:
            if identity:
                self.login(identity)
            self.assertEqual(self.client.get("/api/database/tables").status_code, status)
            self.assertEqual(self.rows().status_code, status)

    def test_pagination_search_and_empty_tables(self):
        self.login()
        response = self.rows(page=2)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual((data["total"], data["pages"]), (57, 3))
        self.assertEqual([r["id"] for r in data["rows"]], list(range(26, 51)))
        last = self.rows(page=999).json()
        self.assertEqual(last["page"], 3)
        self.assertEqual(len(last["rows"]), 7)
        self.assertEqual(self.rows(q="台灣").json()["total"], 29)
        self.assertEqual(self.rows(q="%_").json()["total"], 28)
        self.assertEqual(self.rows(q="不存在").json()["rows"], [])
        self.assertEqual(self.rows(table="empty_table").json()["total"], 0)
        self.assertEqual(self.rows(q="123").json()["total"], 57)

    def test_secrets_never_returned_or_searchable(self):
        self.login()
        response = self.rows(table="users")
        self.assertNotIn("secret-hash", response.text)
        self.assertNotIn("secret-salt", response.text)
        self.assertEqual(response.json()["rows"][0]["password_hash"], database_view.MASKED)
        self.assertEqual(self.rows(table="users", q="secret").json()["total"], 0)

    def test_invalid_inputs_and_no_writes(self):
        self.login()
        self.assertEqual(self.rows(table='users"; DROP TABLE users;--').status_code, 404)
        self.assertEqual(self.rows(database="../../app.db").status_code, 404)
        self.assertEqual(self.rows(q="' OR 1=1 --").json()["total"], 0)
        for params in ({"page": 0}, {"page_size": 101}, {"page_size": 0}, {"q": "x" * 201}):
            self.assertEqual(self.rows(**params).status_code, 422)
        self.assertEqual(self.client.post("/api/database/rows").status_code, 405)
        with database_view.connect("app") as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM users")
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_multiple_databases_and_missing_files(self):
        self.login()
        other = Path(self.tmp.name) / "validation.db"
        con = sqlite3.connect(other)
        con.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        con.close()
        with patch.object(database_view, "databases", return_value={
                "app": ("憑證與帳號", self.db), "validation": ("資料集驗證", other)}):
            result = self.client.get("/api/database/tables").json()["tables"]
            self.assertIn(("validation", "runs"), [(t["database"], t["name"]) for t in result])
            self.assertEqual(self.rows(database="validation", table="runs").json()["total"], 0)
        self.assertEqual(self.rows(database="missing").status_code, 404)
        self.assertFalse((Path(self.tmp.name) / "missing.db").exists())

    def test_chinese_metadata_covers_current_schema(self):
        self.login()
        tables = self.client.get("/api/database/tables").json()["tables"]
        self.assertEqual(next(t for t in tables if t["name"] == "users")["label"], "使用者帳號")
        self.assertEqual(next(t for t in tables if t["name"] == "voucher_records")["label"], "憑證紀錄")
        self.assertEqual(next(t for t in tables if t["name"] == "voucher_records")["date_label"], "憑證日期")
        voucher_columns = self.rows().json()["columns"]
        labels = {column["name"]: column["label"] for column in voucher_columns}
        self.assertEqual(labels["correction_note"], "修正備註")
        self.assertEqual(labels["confirmed_at"], "確認完成時間")
        self.assertEqual(labels["entries_status"], "分錄狀態")
        self.assertEqual(labels["entries_confirmed_at"], "分錄確認時間")
        self.assertEqual(labels["entries_confirmed_by"], "分錄確認者編號")
        self.assertEqual(labels["entries_confirmed_by_name"], "分錄確認者名稱")
        self.assertEqual(labels["entries_proposal_json"], "分錄候選內容")
        self.assertEqual(labels["entries_history"], "分錄異動歷程")
        self.assertIsNone(next(t for t in tables if t["name"] == "empty_table")["date_label"])
        for table in tables:
            columns = self.rows(table=table["name"]).json()["columns"]
            self.assertTrue(all(c["label"] for c in columns))
        self.assertEqual(sum(len(meta["columns"]) for meta in database_view.TABLE_META.values()), 131)
        users_response = self.rows(table="users").json()
        user_columns = {column["name"]: column["label"] for column in users_response["columns"]}
        self.assertEqual(user_columns["is_admin"], "管理員權限")
        self.assertEqual(users_response["rows"][0]["is_admin"], 1)
        columns = database_view.TABLE_META[("app", "voucher_records")]["columns"]
        self.assertEqual(columns["confirmed_by"], "辨識確認者編號")
        self.assertEqual(columns["confirmed_by_name"], "辨識確認者名稱")
        self.assertEqual(columns["recognition_confirmed_version"], "辨識報告確認版本")
        voucher_labels = database_view.TABLE_META[("app", "voucher_records")]["columns"]
        for legacy in ("clerk_note", "accountant_note", "dev_note",
                       "clerk_reviewed_at", "accountant_approved_at"):
            self.assertNotIn(legacy, voucher_labels)
        self.assertEqual(len(database_view.TABLE_META), 11)
        self.assertEqual(next(t for t in tables if t["name"] == "item_account_mappings")["label"], "品名科目對應")
        self.assertEqual(next(t for t in tables if t["name"] == "mapping_history")["label"], "品名科目對應歷程")

    def test_item_mapping_timestamps_are_marked_utc(self):
        self.login()
        for table, expected in (("item_account_mappings", {"created_at", "updated_at"}),
                                ("mapping_history", {"created_at"})):
            columns = self.rows(table=table).json()["columns"]
            marked = {column["name"] for column in columns if column["utc"]}
            self.assertEqual(marked, expected)

    def test_app_item_mapping_utc_dates_filter_by_taipei_calendar_day(self):
        self.login()
        with closing(sqlite3.connect(self.db)) as con:
            con.executemany("""INSERT INTO item_account_mappings
                (id, company_id, item_name_key, account_code, account_name, created_at, updated_at)
                VALUES (?, 1, ?, '00600', '文具費', ?, ?)""", [
                    (1, "日界線前", "2026-09-22T15:59:00+00:00", "2026-09-22T15:59:00+00:00"),
                    (2, "日界線後", "2026-09-22T16:01:00+00:00", "2026-09-22T16:01:00+00:00"),
                    (3, "隔日較晚", "2026-09-23T02:00:00+00:00", "2026-09-23T02:00:00+00:00"),
                ])
            con.commit()
        before = self.rows(table="item_account_mappings", date_from="2026-09-22",
                           date_to="2026-09-22").json()
        after = self.rows(table="item_account_mappings", date_from="2026-09-23",
                          date_to="2026-09-23").json()
        self.assertEqual([row["id"] for row in before["rows"]], [1])
        self.assertEqual([row["id"] for row in after["rows"]], [2, 3])

    def test_voucher_date_status_and_keyword_are_combined(self):
        self.login()
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE voucher_records SET current_json=? WHERE id<=20",
                        ('{"date":"2026-09-20","total":123}',))
            con.execute("UPDATE voucher_records SET current_json=? WHERE id BETWEEN 21 AND 40",
                        ('{"date":"2026-09-21","total":123}',))
            con.execute("UPDATE voucher_records SET current_json=? WHERE id>40",
                        ('{"date":"2026-09-22","total":123}',))
            con.execute("UPDATE voucher_records SET current_json='not json' WHERE id=57")
            con.execute("ALTER TABLE voucher_records ADD COLUMN status TEXT DEFAULT 'pending'")
            con.execute("UPDATE voucher_records SET status='confirmed' WHERE id BETWEEN 21 AND 30")
            con.commit()
        response = self.rows(date_from="2026-09-21", date_to="2026-09-21",
                             status="confirmed", q="台灣", page_size=3)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual((data["total"], data["pages"], len(data["rows"])), (5, 2, 3))
        self.assertEqual(self.rows(date_to="2026-09-20").json()["total"], 20)
        self.assertEqual(self.rows(date_from="2026-09-22").json()["total"], 16)
        self.assertEqual(self.rows(date_from="2026-09-22", status="confirmed").json()["total"], 0)
        self.assertEqual(self.rows(date_from="2026-09-23", date_to="2026-09-22").status_code, 422)
        self.assertEqual(self.rows(date_from="2026-02-30").status_code, 422)
        self.assertEqual(self.rows(status="not-a-status").status_code, 422)
        self.assertEqual(self.rows(table="empty_table", date_from="2026-09-21").status_code, 422)

    def test_validation_dates_use_taiwan_day_and_statuses_are_separate(self):
        self.login()
        other = Path(self.tmp.name) / "validation.db"
        with closing(sqlite3.connect(other)) as con:
            con.execute("CREATE TABLE results (id TEXT PRIMARY KEY, queued_at TEXT, status TEXT, error TEXT)")
            con.executemany("INSERT INTO results VALUES (?, ?, ?, ?)", [
                ("before", "2026-09-22T15:59:00+00:00", "succeeded", ""),
                ("success", "2026-09-22T16:01:00+00:00", "succeeded", ""),
                ("failed", "2026-09-22T17:00:00+00:00", "failed", "辨識失敗"),
                ("timeout", "2026-09-22T18:00:00+00:00", "timeout", ""),
                ("terminated", "2026-09-22T19:00:00+00:00", "terminated", ""),
            ])
            con.commit()
        with patch.object(database_view, "databases", return_value={
                "app": ("憑證與帳號", self.db), "validation": ("資料集驗證", other)}):
            def query(**params):
                return self.rows(database="validation", table="results", **params)

            self.assertEqual(query(date_from="2026-09-23", date_to="2026-09-23").json()["total"], 4)
            self.assertEqual(query(date_to="2026-09-22").json()["rows"][0]["id"], "before")
            self.assertEqual(query(date_from="2026-09-23", status="succeeded").json()["total"], 1)
            self.assertEqual(query(status="failed").json()["total"], 1)
            self.assertEqual(query(status="timeout").json()["total"], 1)
            self.assertEqual(query(status="terminated").json()["total"], 1)
            self.assertTrue(query().json()["columns"][1]["utc"])


if __name__ == "__main__":
    unittest.main()
