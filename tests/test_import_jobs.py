"""Offline contract tests for durable page import jobs."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from app.db.models import ImportFile, ImportJob, ImportPage, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import import_jobs, review


class ImportJobsTests(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            f"sqlite:///{self.root / 'jobs.sqlite'}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        SQLModel.metadata.create_all(self.engine)
        self.session_patch = patch.object(import_jobs, "get_session", self.session_context)
        self.session_patch.start()
        self.upload_patch = patch("app.services.review.config.UPLOAD_DIR", self.root / "uploads")
        self.upload_patch.start()
        self.versions_patch = patch.object(import_jobs.model_invocation, "source_versions", return_value={
            "execution_version_sha256": "test-version",
        })
        self.versions_patch.start()
        self.addCleanup(self.versions_patch.stop)
        self.addCleanup(self.upload_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session:
            session.add(UserAccount(id=1, username="owner-one"))
            session.add(UserAccount(id=2, username="owner-two"))
            session.commit()

    @contextmanager
    def session_context(self):
        with Session(self.engine) as session:
            yield session

    def _job(self, name="synthetic.pdf", owner=1):
        # These contracts exercise the legacy automatic preparation path.
        return import_jobs.create_job([{"path": str(self.root / name), "name": name}], owner,
                                      mode="automatic", sequential=False)

    def _rows(self, model):
        with Session(self.engine) as session:
            return session.exec(select(model)).all()

    @staticmethod
    def _vouchers(token, file_name, page_no):
        return [
            Voucher(origin={"source_token": token, "record_role": "source_page"}),
            Voucher(doc_type_key="invoice", source_file=f"{file_name} p{page_no}",
                    origin={"source_token": token, "record_role": "voucher"}),
        ]

    def test_owner_isolation_and_preview_is_bound_to_job_and_owner(self):
        one = self._job("one.pdf", owner=1)
        two = self._job("two.pdf", owner=2)
        self.assertEqual([item["id"] for item in import_jobs.list_jobs(1)["items"]], [one["id"]])
        with self.assertRaises(import_jobs.JobNotFound):
            import_jobs.get_job(one["id"], 2)

        with Session(self.engine) as session:
            file_id = session.exec(select(ImportFile).where(ImportFile.job_id == one["id"])).one().id
        preview_path = self.root / "preview.png"
        preview_path.write_bytes(b"synthetic preview")
        page = ImportPage(id="preview-page", job_id=one["id"], file_id=file_id,
                          page_no=1, source_token="stable-token", preview_path=str(preview_path))
        page_id = page.id
        with Session(self.engine) as session:
            session.add(page)
            session.commit()
        self.assertEqual(import_jobs.preview(one["id"], page_id, 1), preview_path)
        with self.assertRaises(import_jobs.JobNotFound):
            import_jobs.preview(one["id"], page_id, 2)
        with self.assertRaises(import_jobs.JobNotFound):
            import_jobs.preview(two["id"], page_id, 2)

    def test_pages_commit_independently_and_retry_does_not_duplicate_success(self):
        job = self._job()
        attempts = {1: 0, 2: 0}

        def prepare(_source, _path, page_no, *, token, group_id):
            attempts[page_no] += 1
            if page_no == 2 and attempts[page_no] == 1:
                raise RuntimeError("synthetic page failure")
            return self._vouchers(token, "synthetic.pdf", page_no)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: self.root / "preview.png"), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare), \
             patch.object(import_jobs.logger, "exception"):
            (self.root / "preview.png").write_bytes(b"preview")
            self.assertTrue(import_jobs.run_once())
            first = import_jobs.get_job(job["id"], 1)
            self.assertEqual(first["status"], "partial_failed")
            self.assertEqual([page["status"] for page in first["pages"]], ["completed", "failed"])
            self.assertEqual(len(self._rows(VoucherRecord)), 2)

            import_jobs.action(job["id"], 1, "retry-failed")
            self.assertTrue(import_jobs.run_once())
            retried = import_jobs.get_job(job["id"], 1)
            self.assertEqual(retried["status"], "completed")
            self.assertEqual([page["status"] for page in retried["pages"]], ["completed", "completed"])
            self.assertEqual(attempts, {1: 1, 2: 2})
            self.assertEqual(len(self._rows(VoucherRecord)), 4)
            sources = [row for row in self._rows(VoucherRecord) if row.record_role == "source_page"]
            children = [row for row in self._rows(VoucherRecord) if row.record_role != "source_page"]
            self.assertTrue(all(child.source_parent_id == sources[0].id for child in children if "p1" in child.source_file))
            self.assertEqual(len({row.source_file for row in children}), 2)

    def test_recovery_and_cancel_happen_at_page_boundaries(self):
        job = self._job()
        with Session(self.engine) as session:
            saved = session.get(ImportJob, job["id"])
            saved.status = "running"
            session.add(saved)
            session.commit()
        # Index two pages and leave one claimed to model a worker crash.
        file = self._rows(ImportFile)[0]
        with Session(self.engine) as session:
            for page_no in (1, 2):
                session.add(ImportPage(id=f"page-{page_no}", job_id=job["id"], file_id=file.id,
                                       page_no=page_no, source_token=f"token-{page_no}",
                                       status="running" if page_no == 1 else "queued"))
            session.commit()
        import_jobs.recover_interrupted()
        recovered = import_jobs.get_job(job["id"], 1)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual([page["status"] for page in recovered["pages"]], ["queued", "queued"])

        import_jobs.action(job["id"], 1, "resume")

        def prepare(_source, _path, page_no, *, token, group_id):
            if page_no == 1:
                with Session(self.engine) as session:
                    saved = session.get(ImportJob, job["id"])
                    saved.cancel_requested = True
                    session.add(saved)
                    session.commit()
            return self._vouchers(token, "synthetic.pdf", page_no)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: self.root / "preview.png"), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare):
            (self.root / "preview.png").write_bytes(b"preview")
            self.assertTrue(import_jobs.run_once())
        cancelled = import_jobs.get_job(job["id"], 1)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual([page["status"] for page in cancelled["pages"]], ["completed", "queued"])
        self.assertEqual(len(self._rows(VoucherRecord)), 2)

        import_jobs.action(job["id"], 1, "resume")
        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: self.root / "preview.png"), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare):
            self.assertTrue(import_jobs.run_once())
        resumed = import_jobs.get_job(job["id"], 1)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual([page["status"] for page in resumed["pages"]], ["completed", "completed"])
        self.assertEqual(len(self._rows(VoucherRecord)), 4)

    def test_page_transaction_rolls_back_saved_vouchers_with_job_page(self):
        job = self._job()
        save_prepared = review.save_prepared_page

        def save_then_fail(vouchers, company, session):
            save_prepared(vouchers, company, session)
            raise RuntimeError("synthetic flush transaction failure")

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: self.root / "preview.png"), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=lambda _s, _p, n, *, token, group_id:
                          self._vouchers(token, "synthetic.pdf", n)), \
             patch.object(import_jobs.review, "save_prepared_page", side_effect=save_then_fail), \
             patch.object(import_jobs.logger, "exception"):
            (self.root / "preview.png").write_bytes(b"preview")
            self.assertTrue(import_jobs.run_once())
        result = import_jobs.get_job(job["id"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["pages"][0]["status"], "failed")
        self.assertEqual(self._rows(VoucherRecord), [])
        page = self._rows(ImportPage)[0]
        self.assertIsNone(page.source_page_id)
        self.assertEqual(json.loads(page.voucher_ids_json), [])

    def test_cancel_during_only_page_finishes_completed_after_commit(self):
        job = self._job("last-page.pdf")
        source_image = self.root / "source.png"
        Image.new("RGB", (8, 8), "white").save(source_image)

        def prepare(_source, _path, page_no, *, token, group_id):
            import_jobs.action(job["id"], 1, "cancel")
            vouchers = self._vouchers(token, "last-page.pdf", page_no)
            vouchers[0].source_image = str(source_image)
            return vouchers

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare):
            self.assertTrue(import_jobs.run_once())

        result = import_jobs.get_job(job["id"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["done_pages"], 1)
        self.assertEqual(result["succeeded_pages"], 1)
        self.assertEqual(result["failed_pages"], 0)
        persisted_preview = import_jobs.preview(job["id"], result["pages"][0]["id"], 1)
        self.assertNotEqual(persisted_preview, source_image)
        self.assertTrue(persisted_preview.is_file())

    def test_source_changed_during_prepare_fails_page_without_vouchers(self):
        source_file = self.root / "mutable.pdf"
        source_file.write_bytes(b"original source bytes")
        job = import_jobs.create_job([{"path": str(source_file), "name": "mutable.pdf"}], 1,
                                     mode="automatic", sequential=False)

        def mutate_during_prepare(_source, _path, page_no, *, token, group_id):
            source_file.write_bytes(b"changed source bytes")
            return self._vouchers(token, "mutable.pdf", page_no)

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=str(self.root / "rendered.png")), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=mutate_during_prepare), \
             patch.object(import_jobs.logger, "exception"):
            self.assertTrue(import_jobs.run_once())

        result = import_jobs.get_job(job["id"], 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_pages"], 1)
        self.assertEqual(result["pages"][0]["status"], "failed")
        self.assertIn("來源檔案在處理期間改變", result["pages"][0]["error"])
        self.assertEqual(self._rows(VoucherRecord), [])

    def test_rollback_removes_uploaded_pngs_copied_before_save_failure(self):
        job = self._job("rollback.png")
        source_image = self.root / "source-page.png"
        Image.new("RGB", (8, 8), "white").save(source_image)
        save_prepared = review.save_prepared_page
        upload_pages = self.root / "uploads" / "pages"

        def save_then_fail_after_copy(vouchers, company, session):
            records = save_prepared(vouchers, company, session)
            copied = list(upload_pages.glob("i*.png"))
            self.assertGreaterEqual(len(copied), 1)
            raise RuntimeError("synthetic failure after image copy")

        def prepare(_source, _path, page_no, *, token, group_id):
            vouchers = self._vouchers(token, "rollback.png", page_no)
            vouchers[0].source_image = str(source_image)
            return vouchers

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=str(source_image)), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare), \
             patch.object(import_jobs.review, "save_prepared_page", side_effect=save_then_fail_after_copy), \
             patch.object(import_jobs.logger, "exception"):
            self.assertTrue(import_jobs.run_once())

        self.assertEqual(self._rows(VoucherRecord), [])
        self.assertEqual(list(upload_pages.glob("*.png")), [])
        result = import_jobs.get_job(job["id"], 1)
        self.assertEqual(result["pages"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
