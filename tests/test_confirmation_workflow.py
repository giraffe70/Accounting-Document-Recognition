"""Offline regressions for the explicit voucher confirmation workflow."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlmodel import Session, SQLModel, create_engine

from app.db.models import ReviewStatus, VoucherRecord
from app.models.schemas import Voucher
from app.services import journal, review


ROOT = Path(__file__).resolve().parents[1]


def _load_endpoint(name: str, namespace: dict[str, object]):
    """Compile one endpoint without importing app.main or opening its DB."""
    import ast

    path = ROOT / "app" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    isolated = copy.deepcopy(function)
    isolated.decorator_list = []
    module = ast.Module(body=[isolated], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[name]


def _voucher(*, role="voucher", amount=100):
    data = {
        "doc_type_key": "receipt", "doc_type": "收據", "voucher_id": "R-1",
        "date": "2026-10-01", "fields": {"total_amount": amount},
        "entries": [{"description": "合成費用", "amount": amount,
                     "debit_account": "6000", "credit_account": "1000"}],
        "origin": {"record_role": role},
    }
    return Voucher.model_validate(data)


class ConfirmationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'review.db'}",
                                    connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session_patch = patch.object(review, "get_session",
                                           side_effect=lambda: Session(self.engine, expire_on_commit=False))
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    def save(self, status=ReviewStatus.pending, role="voucher"):
        return review.save_voucher(_voucher(role=role), status=status)

    def test_confirm_checks_version_status_and_source_page(self):
        record = self.save()
        expected = record.updated_at.isoformat()
        confirmed = review.confirm_record(record.id, expected)
        self.assertEqual(confirmed.status, ReviewStatus.confirmed)
        self.assertIsNotNone(confirmed.confirmed_at)

        stale = self.save()
        with self.assertRaises(review.RecognitionProtected):
            review.confirm_record(stale.id, "2000-01-01T00:00:00")
        source_page = self.save(role="source_page")
        with self.assertRaises(review.RecognitionProtected):
            review.confirm_record(source_page.id, source_page.updated_at.isoformat())
        confirmed_at = confirmed.confirmed_at
        repeated = review.confirm_record(confirmed.id, confirmed.updated_at.isoformat())
        self.assertEqual(repeated.confirmed_at, confirmed_at)
        for status in (ReviewStatus.prepared, ReviewStatus.queued):
            draft = self.save(status=status)
            with self.assertRaises(review.RecognitionProtected):
                review.confirm_record(draft.id, draft.updated_at.isoformat())
        self.assertIsNone(review.confirm_record(999999, "missing"))

    def test_list_vouchers_accepts_one_or_multiple_review_statuses(self):
        self.save(status=ReviewStatus.pending)
        self.save(status=ReviewStatus.confirmed)
        self.assertEqual({r.status for r in review.list_vouchers(status=ReviewStatus.confirmed)},
                         {ReviewStatus.confirmed})
        self.assertEqual({r.status for r in review.list_vouchers(
            status=[ReviewStatus.pending, ReviewStatus.confirmed])},
            {ReviewStatus.pending, ReviewStatus.confirmed})

    def test_journal_only_includes_confirmed_and_content_change_returns_to_pending(self):
        prepared = self.save(status=ReviewStatus.prepared)
        pending = self.save()
        confirmed = self.save(status=ReviewStatus.confirmed)
        for record in (prepared, pending, confirmed):
            record.period = "2026-10"
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(prepared)
            session.add(pending)
            session.add(confirmed)
            session.commit()

        built = journal.build_journal("2026-10")
        self.assertEqual(len(built["rows"]), 1)
        self.assertEqual(built["rows"][0]["voucher_id"], "R-1")
        self.assertEqual(built["count"], 1)
        self.assertEqual(journal.periods(), ["2026-10"])

        current = review.get_voucher(confirmed.id)
        with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value):
            unchanged = review.update_current(confirmed.id, {}, mark_manual=True)
        self.assertEqual(unchanged.status, ReviewStatus.confirmed)
        self.assertEqual(unchanged.confirmed_at, current.confirmed_at)

        with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value):
            changed = review.update_current(confirmed.id, {"fields": {"total_amount": 200}})
        self.assertEqual(changed.status, ReviewStatus.pending)
        self.assertIsNone(changed.confirmed_at)

    def test_noop_company_and_notes_preserve_confirmation_but_real_company_change_resets(self):
        record = review.save_voucher(_voucher(), company="Synthetic Co", status=ReviewStatus.confirmed)
        record.confirmed_at = datetime(2026, 10, 1, 9, 0)
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(record)
            session.commit()
        confirmed_at = record.confirmed_at

        same_company = review.set_company(record.id, "Synthetic Co")
        self.assertEqual(same_company.status, ReviewStatus.confirmed)
        self.assertEqual(same_company.confirmed_at, confirmed_at)
        noted = review.set_notes(record.id, correction_note="核對品名", dev_note="synthetic", manual_entry=True)
        self.assertEqual(noted.status, ReviewStatus.confirmed)
        self.assertEqual(noted.confirmed_at, confirmed_at)
        self.assertEqual(noted.correction_note, "核對品名")
        self.assertEqual(noted.dev_note, "synthetic")

        changed = review.set_company(record.id, "Another Co")
        self.assertEqual(changed.status, ReviewStatus.pending)
        self.assertIsNone(changed.confirmed_at)

    def test_model_write_to_confirmed_record_reopens_it_and_preserves_manual_values(self):
        record = review.save_voucher(_voucher(), status=ReviewStatus.confirmed)
        record.confirmed_at = datetime(2026, 10, 1, 9, 0)
        current = json.loads(record.current_json)
        current["fields"]["total_amount"] = 125
        current["field_evidence"] = {"fields.total_amount": [
            {"source": "manual", "value": 125, "selected": True},
        ]}
        record.current_json = json.dumps(current, ensure_ascii=False)
        record.manual_entry = False
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(record)
            session.commit()

        with patch("app.core.pipeline.canonicalize_voucher", side_effect=lambda value: value), \
             patch("app.core.model_invocation.source_versions", return_value={"version": "synthetic"}):
            updated = review._save_recognition(record.id, _voucher(amount=900))
        self.assertEqual(updated.status, ReviewStatus.pending)
        self.assertIsNone(updated.confirmed_at)
        self.assertEqual(json.loads(updated.current_json)["fields"]["total_amount"], 125)
        self.assertEqual(json.loads(updated.ai_json)["fields"]["total_amount"], 100)

    def test_alignment_pairs_queries_confirmed_records_only(self):
        confirmed = self.save(status=ReviewStatus.confirmed)
        pending = self.save(status=ReviewStatus.pending)
        confirmed.current_json = json.dumps({"fields": {"total_amount": 125}})
        confirmed.ai_json = json.dumps({"fields": {"total_amount": 100}})
        pending.current_json = json.dumps({"fields": {"total_amount": 225}})
        pending.ai_json = json.dumps({"fields": {"total_amount": 200}})
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(confirmed)
            session.add(pending)
            session.commit()

        with patch.object(review, "list_vouchers", wraps=review.list_vouchers) as listed:
            pairs = review.alignment_pairs()
        self.assertEqual([pair["id"] for pair in pairs], [confirmed.id])
        self.assertEqual(listed.call_args.kwargs["status"], ReviewStatus.confirmed)


class ReviewMigrationTests(unittest.TestCase):
    def test_legacy_statuses_and_notes_migrate_once(self):
        from app.db import session as db_session

        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'legacy.db'}")
            with engine.begin() as conn:
                conn.exec_driver_sql("""CREATE TABLE voucher_records (
                    id INTEGER PRIMARY KEY, status TEXT, clerk_note TEXT DEFAULT '',
                    accountant_note TEXT DEFAULT '', clerk_reviewed_at DATETIME,
                    accountant_approved_at DATETIME, updated_at DATETIME,
                    current_json TEXT DEFAULT '{}', ai_json TEXT DEFAULT '{}',
                    company TEXT DEFAULT '', period TEXT DEFAULT '', source_file TEXT DEFAULT '',
                    record_role TEXT DEFAULT 'voucher', recognition_history TEXT DEFAULT '[]',
                    confidence TEXT DEFAULT '', dev_note TEXT DEFAULT '', manual_entry INTEGER DEFAULT 0,
                    ocr_provider TEXT DEFAULT '', structure_model TEXT DEFAULT '', image_path TEXT DEFAULT '',
                    attachments TEXT DEFAULT '[]', source_parent_id INTEGER, created_at DATETIME
                )""")
                conn.exec_driver_sql("""INSERT INTO voucher_records
                    (id,status,clerk_note,accountant_note,accountant_approved_at,updated_at)
                    VALUES (1,'accountant_approved','記帳士修正','會計師核定',
                            '2026-09-30 12:00:00','2026-10-01 12:00:00')""")
                conn.exec_driver_sql("INSERT INTO voucher_records (id,status) VALUES (2,'clerk_reviewed')")
                conn.exec_driver_sql("INSERT INTO voucher_records (id,status) VALUES (3,'rejected')")

            with patch.object(db_session, "_engine", engine):
                db_session._migrate()
                with engine.connect() as conn:
                    confirmed = conn.exec_driver_sql(
                        "SELECT status,confirmed_at,correction_note FROM voucher_records WHERE id=1").one()
                    self.assertEqual(confirmed[0], "confirmed")
                    self.assertEqual(confirmed[1], "2026-09-30 12:00:00")
                    self.assertIn("記帳士修正", confirmed[2])
                    self.assertIn("會計師核定", confirmed[2])
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT status FROM voucher_records WHERE id=2").scalar_one(), "pending")
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT status FROM voucher_records WHERE id=3").scalar_one(), "pending")
                    conn.exec_driver_sql("UPDATE voucher_records SET correction_note='人工後續編修' WHERE id=1")
                    conn.commit()
                db_session._migrate()
                with engine.connect() as conn:
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT correction_note FROM voucher_records WHERE id=1").scalar_one(), "人工後續編修")
            engine.dispose()


class ConfirmationApiContractTests(unittest.TestCase):
    def test_appearance_and_record_confirmation_routes_are_separate(self):
        import ast

        path = ROOT / "app" / "main.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        route_by_suffix = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app"
                        and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                    path_value = decorator.args[0].value
                    if path_value.endswith("/confirm") or path_value.endswith("/confirm-appearance"):
                        route_by_suffix[path_value.rsplit("/", 1)[-1]] = node
        self.assertEqual(set(route_by_suffix), {"confirm", "confirm-appearance"})
        self.assertEqual(route_by_suffix["confirm"].name, "api_confirm_record")
        self.assertEqual(route_by_suffix["confirm-appearance"].name, "api_confirm_appearance")
        params = {arg.arg for arg in route_by_suffix["confirm"].args.args}
        self.assertIn("expected_updated_at", params)
        self.assertNotIn("doc_type_key", params)

    def test_record_confirmation_endpoint_passes_expected_version(self):
        from fastapi import Body

        record = SimpleNamespace(id=13)
        review_stub = SimpleNamespace(confirm_record=Mock(return_value=record))
        endpoint = _load_endpoint("api_confirm_record", {
            "review": review_stub, "JSONResponse": Mock(), "Body": Body,
            "_rec_to_dict": lambda value: {"id": value.id},
        })
        result = endpoint(13, "2026-10-01T12:00:00")
        review_stub.confirm_record.assert_called_once_with(13, "2026-10-01T12:00:00")
        self.assertEqual(result["id"], 13)

    def test_auth_guard_allows_each_signed_role_to_confirm_and_rejects_anonymous(self):
        from fastapi import Body, Request
        from fastapi.responses import JSONResponse

        users = {f"{role}-token": {"role": role} for role in ("clerk", "accountant", "admin")}
        auth = SimpleNamespace(get_user_by_token=Mock(side_effect=lambda token: users.get(token)))
        guard = _load_endpoint("auth_guard", {
            "Request": Request, "JSONResponse": JSONResponse, "auth": auth,
            "_PUBLIC_API": {"/api/login", "/api/logout", "/api/me", "/api/health"},
        })
        record = SimpleNamespace(id=13)
        review_stub = SimpleNamespace(confirm_record=Mock(return_value=record))
        confirm = _load_endpoint("api_confirm_record", {
            "review": review_stub, "JSONResponse": JSONResponse, "Body": Body,
            "_rec_to_dict": lambda value: {"id": value.id},
        })

        def guarded_request(token):
            request = SimpleNamespace(
                url=SimpleNamespace(path="/api/vouchers/13/confirm"), method="POST",
                cookies={"session": token} if token else {},
            )

            async def call_next(_request):
                return JSONResponse(confirm(13, "2026-10-01T12:00:00"))

            coroutine = guard(request, call_next)
            try:
                coroutine.send(None)
            except StopIteration as completed:
                return completed.value
            self.fail("auth_guard unexpectedly yielded while using the synchronous fake call_next")

        for role in ("clerk", "accountant", "admin"):
            response = guarded_request(f"{role}-token")
            self.assertEqual(response.status_code, 200, role)
        review_stub.confirm_record.assert_has_calls([
            unittest.mock.call(13, "2026-10-01T12:00:00"),
            unittest.mock.call(13, "2026-10-01T12:00:00"),
            unittest.mock.call(13, "2026-10-01T12:00:00"),
        ])
        anonymous = guarded_request(None)
        self.assertEqual(anonymous.status_code, 401)

    def test_api_list_parses_comma_separated_status_and_rejects_unknown_values(self):
        from fastapi.responses import JSONResponse

        from app.db.models import ReviewStatus
        review_stub = SimpleNamespace(list_vouchers=Mock(return_value=[]))
        endpoint = _load_endpoint("api_list", {
            "ReviewStatus": ReviewStatus, "review": review_stub,
            "_rec_to_dict": lambda record: {"id": record.id},
            "JSONResponse": JSONResponse,
        })
        response = endpoint("pending,confirmed")
        self.assertEqual(response["items"], [])
        self.assertEqual(review_stub.list_vouchers.call_args.kwargs["status"],
                         [ReviewStatus.pending, ReviewStatus.confirmed])
        self.assertEqual(endpoint("pending,unknown").status_code, 422)

    def test_notes_api_accepts_correction_note(self):
        from fastapi import Body, Request

        auth = SimpleNamespace(get_user_by_token=Mock(return_value={"role": "clerk"}))
        review_stub = SimpleNamespace(set_notes=Mock(return_value=SimpleNamespace(id=7)))
        endpoint = _load_endpoint("api_notes", {
            "auth": auth, "review": review_stub, "JSONResponse": Mock(), "Body": Body, "Request": Request,
            "_rec_to_dict": lambda value: {"id": value.id},
        })
        request = SimpleNamespace(cookies={"session": "offline"})
        endpoint(request, 7, correction_note="人工修正", dev_note=None, manual_entry=None)
        review_stub.set_notes.assert_called_once_with(7, correction_note="人工修正")


if __name__ == "__main__":
    unittest.main()
