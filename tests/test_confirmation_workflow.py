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

from app.db.models import Company, ReviewStatus, VoucherRecord
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


def _voucher(*, role="voucher", amount=100, entries=None):
    data = {
        "doc_type_key": "receipt", "doc_type": "收據", "voucher_id": "R-1",
        "date": "2026-10-01", "fields": {"total_amount": amount},
        "entries": entries if entries is not None else [{"description": "合成費用", "amount": amount,
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
        with Session(self.engine) as session:
            session.add(Company(name="Synthetic Company", short="Synthetic Co", tax_id="",
                                account_chart_key="synthetic"))
            session.commit()
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
        balanced_entries = [
            {"description": "費用", "amount": 100, "debit_account": "6000", "debit_account_name": "費用"},
            {"description": "現金", "amount": 100, "credit_account": "1000", "credit_account_name": "現金"},
        ]
        confirmed = review.save_voucher(_voucher(entries=balanced_entries), company="Synthetic Co",
                                        status=ReviewStatus.confirmed)
        confirmed = review.manage_entries(confirmed.id, "confirm", confirmed.updated_at.isoformat(), None)
        self.assertEqual(confirmed.entries_status, "ready")
        for record in (prepared, pending, confirmed):
            record.period = "2026-10"
        with Session(self.engine, expire_on_commit=False) as session:
            session.add(prepared)
            session.add(pending)
            session.add(confirmed)
            session.commit()

        built = journal.build_journal("2026-10")
        self.assertEqual(len(built["rows"]), 2)
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
        noted = review.set_notes(record.id, correction_note="核對品名", dev_note="ignored", manual_entry=True)
        self.assertEqual(noted.status, ReviewStatus.confirmed)
        self.assertEqual(noted.confirmed_at, confirmed_at)
        self.assertEqual(noted.correction_note, "核對品名")
        self.assertFalse(hasattr(noted, "dev_note"))

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

    def test_feedback_log_uses_correction_notes_without_dev_note(self):
        record = self.save(status=ReviewStatus.pending)
        review.set_notes(record.id, correction_note="核對品名")
        items = review.feedback_log()
        self.assertEqual([item["id"] for item in items], [record.id])
        self.assertEqual(items[0]["correction_note"], "核對品名")
        self.assertNotIn("dev_note", items[0])

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
                    (id,status,clerk_note,accountant_note,accountant_approved_at,updated_at,dev_note)
                    VALUES (1,'accountant_approved','記帳士修正','會計師核定',
                            '2026-09-30 12:00:00','2026-10-01 12:00:00','開發備註將捨棄')""")
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
                    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(voucher_records)")}
                    self.assertFalse(columns & set(db_session._LEGACY_COLUMNS))
                    self.assertNotIn("開發備註將捨棄", confirmed[2])
                    conn.exec_driver_sql("UPDATE voucher_records SET correction_note='人工後續編修' WHERE id=1")
                    conn.commit()
                db_session._migrate()
                with engine.connect() as conn:
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT correction_note FROM voucher_records WHERE id=1").scalar_one(), "人工後續編修")
                with Session(engine) as session:
                    self.assertEqual(session.get(VoucherRecord, 1).status, ReviewStatus.confirmed)
            engine.dispose()

    def test_partial_legacy_schema_and_current_schema_migrate_idempotently(self):
        from app.db import session as db_session

        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(f"sqlite:///{Path(tmp) / 'partial.db'}")
            SQLModel.metadata.create_all(engine)
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE voucher_records ADD COLUMN dev_note VARCHAR DEFAULT ''")
                conn.exec_driver_sql("""INSERT INTO voucher_records
                    (id,source_file,record_role,recognition_history,company,period,status,confidence,
                     ai_json,current_json,correction_note,manual_entry,ocr_provider,structure_model,
                     image_path,attachments,created_at,updated_at,dev_note,confirmed_by_name,entries_status,
                     entries_confirmed_by_name,entries_proposal_json,entries_history)
                    VALUES (1,'','voucher','[]','','','pending','','{}','{}','',0,'','','','[]',
                            '2026-10-01 00:00:00','2026-10-01 00:00:00','x','','none','','{}','[]')""")
            with patch.object(db_session, "_engine", engine):
                db_session._migrate()
                db_session._migrate()
                with engine.connect() as conn:
                    columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(voucher_records)")}
                    self.assertFalse(columns & set(db_session._LEGACY_COLUMNS))
                    self.assertIn("correction_note", columns)
                    self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM voucher_records").scalar_one(), 1)
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
        self.assertIn("request", params)
        self.assertIn("expected_updated_at", params)
        self.assertNotIn("doc_type_key", params)

    def test_record_confirmation_endpoint_passes_expected_version(self):
        from fastapi import Body

        request = SimpleNamespace(cookies={"session": "signed-token"})
        actor = {"username": "reviewer", "role": "accountant"}
        auth_stub = SimpleNamespace(get_user_by_token=Mock(return_value=actor))
        record = SimpleNamespace(id=13)
        review_stub = SimpleNamespace(confirm_record=Mock(return_value=record))
        endpoint = _load_endpoint("api_confirm_record", {
            "review": review_stub, "JSONResponse": Mock(), "Body": Body, "auth": auth_stub,
            "_rec_to_dict": lambda value: {"id": value.id},
        })
        result = endpoint(request, 13, "2026-10-01T12:00:00")
        review_stub.confirm_record.assert_called_once_with(13, "2026-10-01T12:00:00", actor)
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
            "review": review_stub, "JSONResponse": JSONResponse, "Body": Body, "auth": auth,
            "_rec_to_dict": lambda value: {"id": value.id},
        })

        def guarded_request(token):
            request = SimpleNamespace(
                url=SimpleNamespace(path="/api/vouchers/13/confirm"), method="POST",
                cookies={"session": token} if token else {},
            )

            async def call_next(_request):
                return JSONResponse(confirm(_request, 13, "2026-10-01T12:00:00"))

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
            unittest.mock.call(13, "2026-10-01T12:00:00", users["clerk-token"]),
            unittest.mock.call(13, "2026-10-01T12:00:00", users["accountant-token"]),
            unittest.mock.call(13, "2026-10-01T12:00:00", users["admin-token"]),
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
        import ast
        import inspect
        from fastapi import Body

        review_stub = SimpleNamespace(set_notes=Mock(return_value=SimpleNamespace(id=7)))
        endpoint = _load_endpoint("api_notes", {
            "review": review_stub, "JSONResponse": Mock(), "Body": Body,
            "_rec_to_dict": lambda value: {"id": value.id},
        })
        endpoint(7, correction_note="人工修正", manual_entry=None)
        review_stub.set_notes.assert_called_once_with(7, correction_note="人工修正")
        self.assertEqual(list(inspect.signature(endpoint).parameters),
                         ["rec_id", "correction_note", "manual_entry"])

        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        routes = {
            decorator.args[0].value
            for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant)
        }
        self.assertNotIn("/api/vouchers/{rec_id}/dev-note", routes)
        self.assertIn("/api/feedback", routes)
        self.assertNotIn("dev_note", source)


class VoucherDeleteTests(unittest.TestCase):
    """Deleting records removes only app-managed files that no other record still uses."""

    def setUp(self):
        import uuid
        self.uuid = uuid
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.upload, self.data = self.root / "uploads", self.root / "data"
        for folder in (self.upload / "pages", self.upload / "_pages", self.data / "logs"):
            folder.mkdir(parents=True)
        self.engine = create_engine(f"sqlite:///{self.root / 'review.db'}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        for patcher in (
            patch.object(review, "get_session", side_effect=lambda: Session(self.engine, expire_on_commit=False)),
            patch.object(review.config, "UPLOAD_DIR", self.upload),
            patch.object(review.config, "DATA_DIR", self.data),
            patch.object(review.config, "LOG_DIR", self.data / "logs"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        from PIL import Image
        self.source = self.root / "scan.png"
        Image.new("RGB", (20, 20), "white").save(self.source)

    def make_upload(self):
        """A managed upload plus its rendered page; returns (group, upload file name, token)."""
        name = self.uuid.uuid4().hex + ".pdf"
        (self.upload / name).write_bytes(b"%PDF")
        (self.upload / "_pages" / f"{Path(name).stem}_p0.png").write_bytes(b"png")
        token = self.uuid.uuid4().hex
        (self.upload / "_pages" / token / "ocr_cache").mkdir(parents=True)
        (self.upload / "_pages" / token / "input.png").write_bytes(b"png")
        return self.uuid.uuid4().hex, name, token

    def record(self, upload=None, *, role="voucher", status=ReviewStatus.pending, parent=None, auto_split=True):
        origin = {"record_role": role, "layout": {"auto_split": auto_split}}
        if upload:
            group, name, token = upload
            origin.update(source_group_id=group, upload_file=name, source_token=token)
        voucher = Voucher.model_validate({"doc_type_key": "receipt", "doc_type": "收據", "origin": origin,
                                          "source_file": "scan.pdf p1"})
        voucher.source_image = str(self.source)
        rec = review.save_voucher(voucher, status=status, source_parent_id=parent.id if parent else None)
        pages = self.upload / "pages"
        for name in (f"{rec.id}.orig.png", f"{rec.id}_att1.png", f"{rec.id}_model_input.png",
                     f"{rec.id}0.png"):          # the last one belongs to another record id
            (pages / name).write_bytes(b"png")
        (pages / f"{rec.id}_page_ocr").mkdir()
        (pages / f"{rec.id}_page_ocr" / "ocr.json").write_text("{}")
        (self.data / "recognition" / str(rec.id)).mkdir(parents=True)
        return rec

    def exists(self, *parts):
        return self.upload.joinpath(*parts).exists()

    def ids(self):
        return sorted(r.id for r in review.list_vouchers(include_sources=True))

    def family(self, upload, children=2, **child_kwargs):
        parent = self.record(upload, role="source_page")
        return parent, [self.record(upload, parent=parent, **child_kwargs) for _ in range(children)]

    def test_deleting_one_document_keeps_siblings_source_and_shared_files(self):
        upload = self.make_upload()
        parent, (first, second) = self.family(upload)

        result = review.delete_vouchers(first.id, "record")

        self.assertEqual(result["deleted_ids"], [first.id])
        self.assertEqual(self.ids(), [parent.id, second.id])
        for name in (f"{first.id}.png", f"{first.id}.orig.png", f"{first.id}_att1.png",
                     f"{first.id}_model_input.png", f"{first.id}_page_ocr"):
            self.assertFalse(self.exists("pages", name), name)
        self.assertFalse((self.data / "recognition" / str(first.id)).exists())
        self.assertTrue(self.exists("pages", f"{first.id}0.png"), "id prefix must not match other ids")
        self.assertTrue(self.exists("pages", f"{second.id}.png"))
        self.assertTrue(self.exists("pages", f"{parent.id}.png"))
        self.assertTrue(self.exists("_pages", upload[2]), "token still used by the source page")
        self.assertTrue(self.exists(upload[1]))

    def test_deleting_last_document_removes_hidden_source_and_unreferenced_upload(self):
        upload = self.make_upload()
        other = self.make_upload()
        other_parent, (other_child,) = self.family(other, children=1)
        parent, (only,) = self.family(upload, children=1)

        result = review.delete_vouchers(only.id, "record")

        self.assertEqual(sorted(result["deleted_ids"]), [parent.id, only.id])
        self.assertEqual(self.ids(), [other_parent.id, other_child.id])
        self.assertFalse(self.exists("_pages", upload[2]))
        self.assertFalse(self.exists(upload[1]))
        self.assertFalse(self.exists("_pages", f"{Path(upload[1]).stem}_p0.png"))
        self.assertTrue(self.exists(other[1]) and self.exists("_pages", other[2]))
        self.assertTrue(self.exists("pages", f"{other_child.id}.png"))

    def test_source_page_waiting_for_manual_split_is_kept_after_last_document_is_deleted(self):
        upload = self.make_upload()
        parent = self.record(upload, role="source_page", auto_split=False)
        child = self.record(upload, parent=parent)

        review.delete_vouchers(child.id, "record")

        self.assertEqual(self.ids(), [parent.id])
        self.assertTrue(self.exists("pages", f"{parent.id}.png"))
        self.assertTrue(self.exists(upload[1]))

    def test_upload_scope_deletes_the_whole_group_and_nothing_else(self):
        upload, other = self.make_upload(), self.make_upload()
        parent, (first, second) = self.family(upload)
        split_token = self.uuid.uuid4().hex
        (self.upload / "_pages" / split_token).mkdir()
        manual = self.record((upload[0], upload[1], split_token), parent=parent)
        other_parent, (other_child,) = self.family(other, children=1)

        preview = review.delete_preview(first.id, "upload")
        self.assertEqual((preview["voucher_count"], preview["source_page_count"]), (3, 1))
        self.assertEqual(review.delete_preview(first.id, "record")["ids"], [first.id])
        result = review.delete_vouchers(first.id, "upload")

        self.assertEqual(result["deleted_ids"], sorted([parent.id, first.id, second.id, manual.id]))
        self.assertEqual(self.ids(), [other_parent.id, other_child.id])
        for token in (upload[2], split_token):
            self.assertFalse(self.exists("_pages", token))
        self.assertFalse(self.exists(upload[1]))
        self.assertTrue(self.exists(other[1]) and self.exists("_pages", other[2]))

    def test_upload_scope_without_group_id_falls_back_to_source_page_and_children(self):
        parent, (child, sibling) = self.family(None)
        other_parent, (other_child,) = self.family(None, children=1)

        result = review.delete_vouchers(child.id, "upload")

        self.assertEqual(result["deleted_ids"], sorted([parent.id, child.id, sibling.id]))
        self.assertEqual(self.ids(), [other_parent.id, other_child.id])

    def test_deleting_a_source_page_takes_its_documents_with_it(self):
        upload = self.make_upload()
        parent, (first, second) = self.family(upload)

        result = review.delete_vouchers(parent.id, "record")

        self.assertEqual(result["deleted_ids"], sorted([parent.id, first.id, second.id]))
        self.assertEqual(self.ids(), [])

    def test_confirmed_records_are_admin_only_and_audited_without_content(self):
        upload = self.make_upload()
        parent, _ = self.family(upload, children=0)
        pending = self.record(upload, parent=parent)
        confirmed = self.record(upload, parent=parent, status=ReviewStatus.confirmed)

        self.assertTrue(review.delete_preview(confirmed.id, "record")["requires_admin"])
        self.assertTrue(review.delete_preview(pending.id, "upload")["requires_admin"])
        self.assertFalse(review.delete_preview(pending.id, "record")["requires_admin"])
        for rec_id, scope in ((confirmed.id, "record"), (pending.id, "upload")):
            with self.assertRaises(review.DeleteForbidden):
                review.delete_vouchers(rec_id, scope, is_admin=False)
        self.assertIn(confirmed.id, self.ids())
        self.assertTrue(self.exists("pages", f"{confirmed.id}.png"))

        review.delete_vouchers(pending.id, "record", is_admin=False, actor="clerk")
        review.delete_vouchers(confirmed.id, "record", is_admin=True, actor="admin")
        lines = [json.loads(line) for line in (self.data / "logs" / "deletions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["actor"] for entry in lines], ["clerk", "admin"])
        self.assertEqual(lines[1]["records"][0], {"id": confirmed.id, "role": "voucher", "status": "confirmed"})
        self.assertNotIn("fields", json.dumps(lines))

    def test_paths_outside_managed_folders_are_never_removed(self):
        outside = self.root / "scan-source" / "keep.png"
        outside.parent.mkdir()
        outside.write_bytes(b"png")
        rec = self.record(None)
        with review.get_session() as session:
            row = session.get(VoucherRecord, rec.id)
            row.image_path = str(outside)
            row.attachments = json.dumps(["../../scan-source/keep.png"])
            session.add(row)
            session.commit()

        result = review.delete_vouchers(rec.id, "record")

        self.assertTrue(outside.exists())
        self.assertEqual(len(result["cleanup_failed"]), 2)
        self.assertEqual(self.ids(), [])

    def test_cleanup_failure_is_reported_without_failing_the_delete(self):
        upload = self.make_upload()
        parent, (child,) = self.family(upload, children=1)

        with patch.object(review.shutil, "rmtree", side_effect=PermissionError("locked")):
            result = review.delete_vouchers(child.id, "upload")

        self.assertTrue(result["ok"])
        self.assertEqual(self.ids(), [])
        self.assertTrue(result["cleanup_failed"])
        self.assertTrue(any("locked" in item for item in result["cleanup_failed"]))

    def test_missing_record_and_invalid_scope(self):
        self.assertIsNone(review.delete_vouchers(999, "record"))
        self.assertIsNone(review.delete_preview(999, "upload"))
        rec = self.record(None)
        with self.assertRaises(ValueError):
            review.delete_vouchers(rec.id, "everything")
        self.assertEqual(self.ids(), [rec.id])

    def test_delete_endpoints_enforce_scope_busy_and_role(self):
        from fastapi.responses import JSONResponse

        users = {"admin-token": {"role": "admin", "username": "admin"}, "clerk-token": {"role": "clerk", "username": "clerk"}}
        auth = SimpleNamespace(get_user_by_token=lambda token: users.get(token))
        state = {"_job": {"status": "idle"}, "_ejob": {"status": "idle"}}
        busy = _load_endpoint("_delete_busy", dict(state))
        namespace = {"review": review, "auth": auth, "JSONResponse": JSONResponse, "_delete_busy": busy, **state}
        delete = _load_endpoint("api_delete", dict(namespace))
        preview = _load_endpoint("api_delete_preview", dict(namespace))
        request = lambda token: SimpleNamespace(cookies={"session": token})
        upload = self.make_upload()
        parent, (pending,) = self.family(upload, children=1)
        confirmed = self.record(upload, parent=parent, status=ReviewStatus.confirmed)

        self.assertEqual(delete(request("clerk-token"), pending.id, "bogus").status_code, 422)
        self.assertEqual(preview(request("clerk-token"), pending.id, "bogus").status_code, 422)
        self.assertEqual(delete(request("clerk-token"), 999, "record").status_code, 404)
        self.assertEqual(preview(request("clerk-token"), 999, "").status_code, 404)

        data = preview(request("clerk-token"), pending.id, "")
        self.assertEqual(data["can_delete"], {"record": True, "upload": False})
        self.assertEqual(preview(request("admin-token"), pending.id, "")["can_delete"], {"record": True, "upload": True})
        self.assertEqual(delete(request("clerk-token"), pending.id, "upload").status_code, 403)
        self.assertEqual(delete(request("clerk-token"), confirmed.id, "record").status_code, 403)

        state["_job"]["status"] = "running"
        busy_delete = _load_endpoint("api_delete", {**namespace, "_delete_busy": _load_endpoint("_delete_busy", dict(state))})
        self.assertEqual(busy_delete(request("admin-token"), pending.id, "record").status_code, 409)
        self.assertIn(pending.id, self.ids())
        state["_job"]["status"] = "idle"

        self.assertEqual(delete(request("clerk-token"), pending.id, "record")["deleted_ids"], [pending.id])
        self.assertEqual(delete(request("admin-token"), confirmed.id, "upload")["deleted_ids"],
                         sorted([parent.id, confirmed.id]))
        self.assertEqual(delete(request("admin-token"), confirmed.id, "record").status_code, 404)


if __name__ == "__main__":
    unittest.main()
