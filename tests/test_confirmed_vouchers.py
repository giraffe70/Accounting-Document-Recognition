"""Confirmed-history queries use only a temporary synthetic SQLite database."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import Company, ReviewStatus, VoucherRecord
from app.services import confirmed_vouchers, recognition_details


class ConfirmedVoucherTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "confirmed.db"
        self.engine = create_engine(f"sqlite:///{self.path}", connect_args={"check_same_thread": False})
        stack.callback(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add_all([Company(id=1, name="合成公司甲", short="甲", account_chart_key="a"),
                             Company(id=2, name="合成公司乙", short="乙", account_chart_key="b")])
            session.commit()
        stack.enter_context(patch.object(confirmed_vouchers, "get_session", side_effect=lambda: Session(self.engine)))
        stack.enter_context(patch.object(confirmed_vouchers.auth, "get_user_by_token", side_effect=lambda token:
            {"id": 1, "is_admin": token == "admin"} if token in {"ordinary", "admin"} else None))
        app = FastAPI()
        app.include_router(confirmed_vouchers.router)
        self.client = stack.enter_context(TestClient(app))
        self.client.cookies.set("session", "ordinary")

    def add(self, value="2026-10-30", *, company_id=1, status=ReviewStatus.confirmed,
            role="voucher", ready=True, raw=None, amount=0):
        data = {"date": value, "voucher_id": "SYNTHETIC-1", "doc_type_key": "receipt",
                "doc_type": "收據", "total_amount": amount, "currency": "TWD",
                "details_version": recognition_details.VERSION if ready else 0}
        with Session(self.engine) as session:
            record = VoucherRecord(company_id=company_id, company="甲" if company_id == 1 else "乙" if company_id == 2 else "",
                status=status, record_role=role, current_json=raw if raw is not None else json.dumps(data),
                recognition_confirmed_version=recognition_details.VERSION,
                confirmed_at=datetime(2026, 10, 30, 10), confirmed_by_name="合成人員",
                updated_at=datetime(2026, 10, 30, 10), image_path="synthetic.png", attachments='["synthetic-2.png"]')
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def get(self, **params):
        return self.client.get("/api/confirmed-vouchers", params=params)

    def test_authentication_allows_ordinary_users_and_admins(self):
        self.add()
        for token, expected in (("", 401), ("invalid", 401), ("ordinary", 200), ("admin", 200)):
            self.client.cookies.set("session", token)
            response = self.get()
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_company_status_role_and_unassigned_history(self):
        first = self.add()
        self.add(company_id=2)
        self.add(company_id=None)
        for status in (ReviewStatus.prepared, ReviewStatus.queued, ReviewStatus.pending):
            self.add(status=status)
        self.add(role="source_page")
        self.assertEqual(self.get().json()["total"], 3)
        self.assertEqual([r["id"] for r in self.get(company_id=1).json()["items"]], [first])
        self.assertEqual(self.get(company_id=999).json()["total"], 0)

    def test_inclusive_date_bounds_and_one_sided_ranges_use_document_date(self):
        old = self.add("2026-09-30")
        start = self.add("2026-10-01")
        end = self.add("115年10月30日")
        self.add("2026-10-31")
        result = self.get(date_from="2026-10-01", date_to="2026-10-30").json()
        self.assertEqual([r["id"] for r in result["items"]], [end, start])
        self.assertEqual(self.get(date_from="2026-10-30").json()["total"], 2)
        self.assertEqual([r["id"] for r in self.get(date_to="2026-09-30").json()["items"]], [old])

    def test_missing_incomplete_and_impossible_dates_sort_last_and_do_not_match_ranges(self):
        invalid = [self.add(value) for value in ("", None, "115年10月", "2026-02-30", ["2026-10-30"])]
        malformed = self.add(raw="{invalid json")
        array = self.add(raw="[]")
        valid = self.add("2026-01-01")
        rows = self.get().json()["items"]
        self.assertEqual([r["id"] for r in rows], [valid, array, malformed, *reversed(invalid)])
        self.assertEqual(self.get(date_from="2020-01-01").json()["total"], 1)
        self.assertEqual(self.get(date_to="2030-01-01").json()["total"], 1)

    def test_default_pagination_and_same_day_id_order(self):
        ids = [self.add() for _ in range(57)]
        first = self.get().json()
        second = self.get(page=2).json()
        self.assertEqual((first["total"], first["page"], first["page_size"], first["pages"]), (57, 1, 50, 2))
        self.assertEqual([row["id"] for row in first["items"]], list(reversed(ids))[0:50])
        self.assertEqual([row["id"] for row in second["items"]], list(reversed(ids))[50:])
        self.assertEqual(self.get(page=999).json(), second)

    def test_empty_results_invalid_parameters_and_read_only_endpoint(self):
        self.assertEqual(self.get().json(), {"items": [], "total": 0, "page": 1, "page_size": 50, "pages": 1})
        for params in ({"date_from": "2026-11-01", "date_to": "2026-10-01"},
                       {"date_from": "2026-02-30"}, {"page": 0}, {"page_size": 0},
                       {"page_size": 101}, {"company_id": 0}, {"company_id": "1 OR 1=1"}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 422)
        for method in ("post", "put", "delete"):
            self.assertEqual(getattr(self.client, method)("/api/confirmed-vouchers").status_code, 405)

    def test_summary_and_report_readiness_do_not_expose_full_record(self):
        ready = self.add(amount=0)
        stale = self.add(ready=False, amount=None)
        items = {row["id"]: row for row in self.get().json()["items"]}
        self.assertTrue(items[ready]["recognition_report_ready"])
        self.assertFalse(items[stale]["recognition_report_ready"])
        self.assertEqual(items[ready]["total_amount"], 0)
        self.assertEqual(items[ready]["attachments"], 1)
        self.assertTrue(items[ready]["has_image"])
        self.assertEqual(items[ready]["confirmed_by_name"], "合成人員")
        self.assertFalse({"ai_json", "current_json", "image_path", "recognition_history"} & items[ready].keys())

    def test_queries_do_not_repair_or_write_saved_data(self):
        self.add("115年10月30日")
        self.add(raw="{invalid json")
        before = self.path.read_bytes()
        self.get()
        self.get(date_from="2026-01-01", company_id=1)
        self.assertEqual(self.path.read_bytes(), before)
        with Session(self.engine) as session:
            self.assertEqual(len(session.exec(select(VoucherRecord)).all()), 2)


if __name__ == "__main__":
    unittest.main()
