"""Offline API-contract tests compiled from app.main without importing main or starting ASGI."""
from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Body, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, SQLModel, select, create_engine

from app.db.models import Company, ItemAccountMapping, MappingHistory, ReviewStatus, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import accounts, auth, companies, entry_workflow, item_account_mapping as mapping, review


ROOT = Path(__file__).resolve().parents[1]


def _compile_endpoint(name: str, namespace: dict[str, object]):
    """Compile one actual app.main top-level function with its route decorators removed."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"),
                     filename=str(ROOT / "app" / "main.py"))
    function = next(node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    function = ast.fix_missing_locations(ast.Module(body=[ast.FunctionDef(
        name=function.name, args=function.args, body=function.body,
        decorator_list=[], returns=function.returns, type_comment=function.type_comment)
        if isinstance(function, ast.FunctionDef) else ast.AsyncFunctionDef(
        name=function.name, args=function.args, body=function.body,
        decorator_list=[], returns=function.returns, type_comment=function.type_comment)], type_ignores=[]))
    exec(compile(function, str(ROOT / "app" / "main.py"), "exec"), namespace)
    return namespace[name]


class ItemAccountApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'api.db'}",
                                    connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session:
            self.company_ids = []
            for name, short, chart in (("合成甲公司", "甲", "chart-a"),
                                       ("合成乙公司", "乙", "chart-b"),
                                       ("合成丙公司", "丙", "chart-c")):
                company = Company(name=name, short=short, tax_id="", account_chart_key=chart)
                session.add(company)
                self.company_ids.append(company)
            self.user = UserAccount(username="api-reviewer", name="API 審核者")
            session.add(self.user)
            session.commit()
            for row in (*self.company_ids, self.user):
                session.refresh(row)
            self.company_ids = [row.id for row in self.company_ids]
            self.user_id = self.user.id

        self.review_session_patch = patch.object(review, "get_session",
            side_effect=lambda: Session(self.engine, expire_on_commit=False))
        self.review_session_patch.start()
        self.addCleanup(self.review_session_patch.stop)

        from app import config
        from app.services import importdecl, taxid
        namespace = {
            "Body": Body, "Request": Request, "JSONResponse": JSONResponse,
            "VoucherRecord": VoucherRecord, "review": review, "companies": companies,
            "item_account_mapping": mapping, "entry_workflow": entry_workflow,
            "accounts": accounts, "auth": auth,
            "taxid": taxid, "importdecl": importdecl, "config": config, "json": json,
        }
        main_tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
        namespace["_PUBLIC_API"] = ast.literal_eval(next(node.value for node in main_tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_PUBLIC_API"
                for target in node.targets)))
        for module, name, value in ((importdecl, "is_import", False), (taxid, "check_voucher", {}),
                                    (taxid, "check_date", {}), (taxid, "check_amounts", {})):
            fixture_patch = patch.object(module, name, return_value=value)
            fixture_patch.start()
            self.addCleanup(fixture_patch.stop)
        for name in ("_rec_to_dict", "api_save_current", "api_lookup_item_accounts",
                     "api_update", "api_get_accounts", "api_confirm_record",
                     "mapping_conflict_handler", "recognition_protected_handler", "auth_guard"):
            _compile_endpoint(name, namespace)
        self.api = namespace
        self.request = SimpleNamespace(cookies={"session": "synthetic-session"})
        self.actor = {"id": self.user_id, "username": "api-reviewer", "name": "API 審核者", "role": "clerk"}

    def voucher(self, description="合成明細", *, account_code="", account_name="", **extras):
        row = {"description": description, "amount": 100, "account_code": account_code,
               "account_name": account_name, **extras}
        return Voucher.model_validate({"doc_type_key": "receipt", "doc_type": "收據",
            "date": "2026-10-12", "items": [row], "fields": {}})

    def new_record(self, company_index=0, description="合成明細"):
        return review.save_voucher(self.voucher(description), company=("甲", "乙", "丙")[company_index])

    def payload(self, rec, *, action="save", items=None, company_index=None, **extra):
        voucher = json.loads(rec.current_json)
        if items is not None:
            voucher["items"] = items
        return {"action": action, "voucher": voucher,
                "company_id": rec.company_id if company_index is None else self.company_ids[company_index],
                "expected_updated_at": rec.updated_at.isoformat(), **extra}

    @staticmethod
    def response_body(response):
        return json.loads(response.body.decode("utf-8"))

    def call_save(self, rec_id, payload):
        with patch.object(auth, "get_user_by_token", return_value=self.actor):
            try:
                return self.api["api_save_current"](self.request, rec_id, payload)
            except mapping.MappingConflict as exc:
                return asyncio.run(self.api["mapping_conflict_handler"](self.request, exc))
            except review.RecognitionProtected as exc:
                return asyncio.run(self.api["recognition_protected_handler"](self.request, exc))

    def call_update(self, rec_id, payload):
        with patch.object(auth, "get_user_by_token", return_value=self.actor):
            try:
                return self.api["api_update"](self.request, rec_id, payload)
            except mapping.MappingConflict as exc:
                return asyncio.run(self.api["mapping_conflict_handler"](self.request, exc))
            except review.RecognitionProtected as exc:
                return asyncio.run(self.api["recognition_protected_handler"](self.request, exc))

    def test_auth_guard_rejects_anonymous_and_all_three_roles_can_save(self):
        request = SimpleNamespace(url=SimpleNamespace(path="/api/vouchers/1/save"),
            method="POST", cookies={"session": "synthetic-session"})
        async def call_next(_request):
            return {"passed": True}

        with patch.object(auth, "get_user_by_token", return_value=None):
            rejected = asyncio.run(self.api["auth_guard"](request, call_next))
        self.assertEqual(rejected.status_code, 401)
        for role in ("clerk", "accountant", "admin"):
            with self.subTest(role=role), patch.object(auth, "get_user_by_token", return_value={"role": role}):
                self.assertEqual(asyncio.run(self.api["auth_guard"](request, call_next)), {"passed": True})

    def test_save_endpoint_returns_current_and_mapping_history_uses_authenticated_actor(self):
        rec = self.new_record()
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00120", account_name="文具費")
        payload = self.payload(rec, items=[row], actor={"username": "spoof", "name": "偽造"})
        with patch.object(auth, "get_user_by_token", return_value=self.actor), \
             patch.object(self.api["importdecl"], "is_import", return_value=False), \
             patch.object(self.api["taxid"], "check_voucher", return_value={}), \
             patch.object(self.api["taxid"], "check_date", return_value={}), \
             patch.object(self.api["taxid"], "check_amounts", return_value={}):
            response = self.call_save(rec.id, payload)
        self.assertIsInstance(response, dict)
        self.assertEqual(response["id"], rec.id)
        self.assertEqual(response["current"]["items"][0]["account_code"], "00120")
        self.assertIn("mapping_skipped", response)
        with Session(self.engine) as session:
            rule = session.exec(select(ItemAccountMapping)).one()
            history = session.exec(select(MappingHistory)).one()
            self.assertEqual(rule.created_by, self.user_id)
            self.assertEqual(rule.updated_by_name, "API 審核者")
            self.assertEqual((history.actor_id, history.actor_name), (self.user_id, "API 審核者"))

    def test_lookup_is_company_scoped_and_strict_accounts_do_not_fall_back(self):
        rec = self.new_record(0)
        row = json.loads(rec.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費")
        self.assertIsInstance(self.call_save(rec.id, self.payload(rec, items=[row])), dict)
        found = self.api["api_lookup_item_accounts"]({"company_id": self.company_ids[0],
            "items": [{"description": "合成明細"}]})
        missing = self.api["api_lookup_item_accounts"]({"company_id": self.company_ids[1],
            "items": [{"description": "合成明細"}]})
        self.assertEqual(found["items"][0]["match"]["account_code"], "00600")
        self.assertIsNone(missing["items"][0]["match"])
        with patch.object(accounts, "_load_all", return_value={"_default": [{"code": "0001", "name": "通用"}]}), \
             patch.object(accounts, "_company_key", side_effect=lambda value: value), \
             patch.object(accounts, "companies_with_chart", return_value=[]):
            strict = self.api["api_get_accounts"](company_id=self.company_ids[2], strict=True)
            fallback = self.api["api_get_accounts"](company_id=self.company_ids[2], strict=False)
        self.assertEqual(strict["accounts"], [])
        self.assertEqual(fallback["accounts"], [{"code": "0001", "name": "通用"}])

    def test_conflicting_mapping_and_stale_document_return_structured_409(self):
        first = self.new_record()
        row = json.loads(first.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費")
        self.call_save(first.id, self.payload(first, items=[row]))
        second = self.new_record()
        changed = json.loads(second.current_json)["items"][0]
        changed.update(account_code="00700", account_name="差旅費")
        conflict = self.call_save(second.id, self.payload(second, items=[changed]))
        self.assertIsInstance(conflict, JSONResponse)
        self.assertEqual(conflict.status_code, 409)
        body = self.response_body(conflict)
        self.assertEqual(body["conflicts"][0]["old"]["account_code"], "00600")
        self.assertEqual(body["latest"], None)
        self.assertEqual(review.get_voucher(second.id).current_json, second.current_json)

        current = review.get_voucher(second.id)
        stale_payload = self.payload(current, expected_updated_at="2000-01-01T00:00:00")
        stale = self.call_save(current.id, stale_payload)
        self.assertEqual(stale.status_code, 409)
        latest = self.response_body(stale)["latest"]
        self.assertEqual(latest["updated_at"], current.updated_at.isoformat())
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).one().version, 1)

    def test_legacy_put_requires_version_audits_local_only_and_rejects_stale_version(self):
        rec = self.new_record()
        voucher = json.loads(rec.current_json)
        voucher["items"][0].update(account_code="00800", account_name="郵電費")
        with patch.object(auth, "get_user_by_token", return_value=self.actor):
            no_version = self.call_update(rec.id, dict(voucher))
        self.assertEqual(no_version.status_code, 409)
        self.assertIn("updated_at", self.response_body(no_version)["latest"])
        self.assertIn("expected_updated_at", self.response_body(no_version)["detail"])

        current = review.get_voucher(rec.id)
        voucher = json.loads(current.current_json)
        voucher["items"][0].update(account_code="00800", account_name="郵電費")
        voucher["expected_updated_at"] = current.updated_at.isoformat()
        with patch.object(auth, "get_user_by_token", return_value=self.actor), \
             patch.object(self.api["importdecl"], "is_import", return_value=False), \
             patch.object(self.api["taxid"], "check_voucher", return_value={}), \
             patch.object(self.api["taxid"], "check_date", return_value={}), \
             patch.object(self.api["taxid"], "check_amounts", return_value={}):
            saved = self.api["api_update"](self.request, rec.id, voucher)
        self.assertEqual(saved["current"]["items"][0]["account_code"], "00800")
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])
            updated = session.get(VoucherRecord, rec.id)
            saved_row = json.loads(updated.current_json)["items"][0]
            self.assertEqual(saved_row["account_updated_by"], self.user_id)

        stale = dict(voucher, expected_updated_at="2000-01-01T00:00:00")
        before_stale_write = review.get_voucher(rec.id)
        with patch.object(auth, "get_user_by_token", return_value=self.actor):
            with self.assertRaises(entry_workflow.EntryConflict) as conflict:
                self.api["api_update"](self.request, rec.id, stale)
        latest = review.get_voucher(rec.id)
        self.assertEqual(conflict.exception.latest["updated_at"], before_stale_write.updated_at.isoformat())
        self.assertEqual(conflict.exception.latest["current"], json.loads(before_stale_write.current_json))
        self.assertEqual(latest.current_json, before_stale_write.current_json)

    def test_malformed_company_items_and_item_uuid_map_to_422(self):
        rec = self.new_record()
        wrong_company = self.call_save(rec.id, self.payload(rec, company_id=999))
        self.assertEqual(wrong_company.status_code, 422)
        wrong_items = self.api["api_lookup_item_accounts"]({"company_id": self.company_ids[0], "items": ["bad"]})
        self.assertEqual(wrong_items.status_code, 422)
        row = json.loads(rec.current_json)["items"][0]
        row["item_id"] = "not-a-uuid"
        malformed_id = self.call_save(rec.id, self.payload(rec, items=[row]))
        self.assertEqual(malformed_id.status_code, 422)

    def test_raw_confirmation_allows_partial_or_missing_item_accounts_without_publishing_rules(self):
        partial = self.new_record()
        row = json.loads(partial.current_json)["items"][0]
        row.update(account_code="00600", account_name="")
        partial_result = self.call_save(partial.id, self.payload(partial, action="confirm", items=[row]))
        self.assertIsInstance(partial_result, dict)
        self.assertEqual(partial_result["status"], ReviewStatus.confirmed)
        self.assertEqual(partial_result["entries_status"], "none")
        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(ItemAccountMapping)).all(), [])

        missing = self.new_record()
        row = json.loads(missing.current_json)["items"][0]
        row.update(account_code="00600", account_name="文具費")
        no_company = self.call_save(missing.id, self.payload(missing, action="confirm",
            company_index=None, items=[row], company_id=None))
        self.assertIsInstance(no_company, dict)
        self.assertEqual(no_company["status"], ReviewStatus.confirmed)

        empty = self.new_record()
        with patch.object(self.api["importdecl"], "is_import", return_value=False), \
             patch.object(self.api["taxid"], "check_voucher", return_value={}), \
             patch.object(self.api["taxid"], "check_date", return_value={}), \
             patch.object(self.api["taxid"], "check_amounts", return_value={}):
            confirmed = self.call_save(empty.id, self.payload(empty, action="confirm", company_index=None,
                company_id=None))
        self.assertIsInstance(confirmed, dict)
        self.assertEqual(confirmed["status"], ReviewStatus.confirmed)


if __name__ == "__main__":
    unittest.main()
