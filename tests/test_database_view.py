import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import auth, database_view


class DatabaseViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "app.db"
        con = sqlite3.connect(self.db)
        con.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, salt TEXT);
            INSERT INTO users VALUES (1, '管理員', 'secret-hash', 'secret-salt');
            CREATE TABLE voucher_records (
                id INTEGER PRIMARY KEY, company TEXT, current_json TEXT,
                correction_note TEXT DEFAULT '', confirmed_at TEXT
            );
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

    def login(self, role="admin"):
        token = auth.create_token({"username": "test", "role": role, "name": "測試"})
        self.tokens.append(token)
        self.client.cookies.set("session", token)

    def rows(self, **params):
        return self.client.get("/api/database/rows", params={"database": "app", "table": "voucher_records", **params})

    def test_requires_admin_for_both_endpoints(self):
        for role, status in [(None, 401), ("clerk", 403), ("accountant", 403), ("admin", 200)]:
            if role:
                self.login(role)
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
        self.assertEqual(next(t for t in tables if t["name"] == "voucher_records")["label"], "憑證紀錄")
        self.assertEqual(next(t for t in tables if t["name"] == "voucher_records")["date_label"], "憑證日期")
        voucher_columns = self.rows().json()["columns"]
        labels = {column["name"]: column["label"] for column in voucher_columns}
        self.assertEqual(labels["correction_note"], "修正備註")
        self.assertEqual(labels["confirmed_at"], "確認完成時間")
        self.assertIsNone(next(t for t in tables if t["name"] == "empty_table")["date_label"])
        for table in tables:
            columns = self.rows(table=table["name"]).json()["columns"]
            self.assertTrue(all(c["label"] for c in columns))
        self.assertEqual(sum(len(meta["columns"]) for meta in database_view.TABLE_META.values()), 94)
        self.assertEqual(len(database_view.TABLE_META), 8)

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
