"""Offline contracts for one-file, page-by-page PDF appearance review."""
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from sqlmodel import SQLModel, Session, create_engine, select

from app.core import model_factory, pipeline, preparation
from app.db.models import ImportFile, ImportJob, ImportPage, ReviewStatus, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import entry_workflow, import_jobs, review


class SequentialImportTests(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            f"sqlite:///{self.root / 'sequential.sqlite'}",
            connect_args={"check_same_thread": False, "timeout": 20},
        )
        SQLModel.metadata.create_all(self.engine)
        self.session_patch = patch.object(import_jobs, "get_session", self.session_context)
        self.review_session_patch = patch.object(review, "get_session", self.session_context)
        self.session_patch.start()
        self.review_session_patch.start()
        self.upload_patch = patch.object(review.config, "UPLOAD_DIR", self.root / "uploads")
        self.upload_patch.start()
        self.addCleanup(self.upload_patch.stop)
        self.addCleanup(self.review_session_patch.stop)
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.engine.dispose)
        self.versions_patch = patch.object(import_jobs.model_invocation, "source_versions", return_value={
            "execution_version_sha256": "sequential-test-version",
        })
        self.versions_patch.start()
        self.addCleanup(self.versions_patch.stop)
        with Session(self.engine) as session:
            session.add(UserAccount(id=1, username="sequential-owner"))
            session.add(UserAccount(id=2, username="other-owner"))
            session.commit()

    @contextmanager
    def session_context(self):
        with Session(self.engine) as session:
            yield session

    def _source(self, name="scan.pdf"):
        path = self.root / name
        path.write_bytes(b"synthetic multipage PDF")
        return path

    def _create(self, name="scan.pdf", owner=1, **kwargs):
        path = self._source(name)
        return import_jobs.create_job([{"path": str(path), "name": name}], owner, **kwargs)

    def _rows(self, model):
        with Session(self.engine) as session:
            return session.exec(select(model)).all()

    def _prepared_vouchers(self, source_image, token, name, page_no):
        origin = {"source_token": token, "preparation": preparation.fresh(pdf=False)}
        parent = Voucher(origin={**origin, "record_role": "source_page"})
        first = Voucher(doc_type_key="invoice", origin=dict(origin))
        second = Voucher(doc_type_key="invoice", origin=dict(origin))
        for voucher in (parent, first, second):
            voucher.source_image = str(source_image)
            voucher.source_file = f"{name} p{page_no}"
        return [parent, first, second]

    def test_default_is_single_file_sequential_and_owner_lock_is_scoped(self):
        first_path = self._source("owner-one.pdf")
        first = import_jobs.create_job([{"path": str(first_path), "name": first_path.name}], 1)
        saved = next(row for row in self._rows(ImportJob) if row.id == first["id"])
        snapshot = json.loads(saved.settings_json)
        self.assertTrue(snapshot["sequential_review"])

        with self.assertRaises(import_jobs.SingleFileRequired):
            import_jobs.create_job([
                {"path": str(first_path), "name": first_path.name},
                {"path": str(self._source("second.pdf")), "name": "second.pdf"},
            ], 1)
        for status in ("queued", "running", "awaiting_review", "failed", "interrupted"):
            with Session(self.engine) as session:
                active = session.get(ImportJob, first["id"])
                active.status = status
                session.add(active)
                session.commit()
            with self.subTest(status=status), self.assertRaises(import_jobs.JobConflict):
                self._create(f"same-owner-{status}.pdf", owner=1)

        # The active sequential lock is per owner, so another user can import.
        other = self._create("owner-two.pdf", owner=2)
        self.assertNotEqual(first["id"], other["id"])

        with Session(self.engine) as session:
            active = session.get(ImportJob, first["id"])
            active.status = "cancelled"
            session.add(active)
            session.commit()
        released = self._create("after-cancel.pdf", owner=1)
        self.assertTrue(released["id"])
        with Session(self.engine) as session:
            completed = session.get(ImportJob, released["id"])
            completed.status = "completed"
            session.add(completed)
            session.commit()
        self.assertTrue(self._create("after-complete.pdf", owner=1)["id"])

    def test_concurrent_same_owner_submissions_have_exactly_one_winner(self):
        paths = [self._source("concurrent-a.pdf"), self._source("concurrent-b.pdf")]
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def submit(path):
            barrier.wait(timeout=5)
            try:
                result = import_jobs.create_job([{"path": str(path), "name": path.name}], 1)
                outcome = ("ok", result["id"])
            except import_jobs.JobConflict:
                outcome = ("conflict", None)
            except Exception as exc:  # Capture unexpected database/thread errors for a clear assertion.
                outcome = ("error", repr(exc))
            with outcome_lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=submit, args=(path,)) for path in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(item[0] for item in outcomes), ["conflict", "ok"])
        self.assertEqual(len(self._rows(ImportJob)), 1)

    def test_two_page_pdf_waits_for_all_first_page_vouchers_before_advancing(self):
        pdf = self._source()
        job = import_jobs.create_job([{"path": str(pdf), "name": pdf.name}], 1)
        source_image = self.root / "rendered-page.png"
        Image.new("RGB", (32, 24), "white").save(source_image)
        prepare_calls = []

        def prepare(source, path, page_no, *, token, group_id):
            prepare_calls.append(page_no)
            return self._prepared_vouchers(source, token, Path(path).name, page_no)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_manual_page", side_effect=prepare):
            self.assertTrue(import_jobs.run_once())

        first_view = import_jobs.get_job(job["id"], 1)
        self.assertEqual(first_view["status"], "awaiting_review")
        self.assertEqual([page["status"] for page in first_view["pages"]], ["preview_ready", "queued"])
        self.assertEqual(prepare_calls, [1])
        self.assertEqual(len(first_view["pages"][0]["voucher_ids"]), 2)

        # Process restarts preserve a page waiting for a person.
        import_jobs.recover_interrupted()
        self.assertEqual(import_jobs.get_job(job["id"], 1)["status"], "awaiting_review")

        first_page = next(page for page in self._rows(ImportPage) if page.page_no == 1)
        first_child_id, second_child_id = json.loads(first_page.voucher_ids_json)
        with Session(self.engine) as session:
            first_child = session.get(VoucherRecord, first_child_id)
            first_child.status = ReviewStatus.pending
            session.add(first_child)
            import_jobs.mark_review_complete(session, first_child)
            session.commit()
        waiting = import_jobs.get_job(job["id"], 1)
        self.assertEqual(waiting["pages"][0]["status"], "preview_ready")
        self.assertEqual(waiting["pages"][1]["status"], "queued")

        with Session(self.engine) as session:
            last_child = session.get(VoucherRecord, second_child_id)
            last_child.status = ReviewStatus.pending
            session.add(last_child)
            import_jobs.mark_review_complete(session, last_child)
            session.commit()
        advanced = import_jobs.get_job(job["id"], 1)
        self.assertEqual(advanced["pages"][0]["status"], "completed")
        self.assertEqual(advanced["pages"][1]["status"], "queued")
        self.assertEqual(advanced["status"], "queued")

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_manual_page", side_effect=prepare):
            self.assertTrue(import_jobs.run_once())
        final = import_jobs.get_job(job["id"], 1)
        self.assertEqual(final["status"], "awaiting_review")
        self.assertEqual([page["status"] for page in final["pages"]], ["completed", "preview_ready"])
        self.assertEqual(prepare_calls, [1, 2])

    def test_automatic_preparation_still_pauses_after_one_page_for_review(self):
        pdf = self._source("automatic-sequential.pdf")
        job = import_jobs.create_job(
            [{"path": str(pdf), "name": pdf.name}], 1, mode="automatic",
        )
        source_image = self.root / "automatic-render.png"
        Image.new("RGB", (32, 24), "white").save(source_image)
        prepared_pages = []

        def prepare(source, path, page_no, *, token, group_id):
            prepared_pages.append(page_no)
            return self._prepared_vouchers(source, token, Path(path).name, page_no)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=prepare), \
             patch.object(import_jobs.pipeline, "prepare_manual_page", side_effect=AssertionError("manual preparation selected")):
            self.assertTrue(import_jobs.run_once())

        view = import_jobs.get_job(job["id"], 1)
        self.assertEqual(view["status"], "awaiting_review")
        self.assertEqual([page["status"] for page in view["pages"]], ["preview_ready", "queued"])
        self.assertEqual(prepared_pages, [1])

    def test_automatic_uncertain_source_page_gets_working_copy_and_pauses(self):
        pdf = self._source("uncertain-layout.pdf")
        job = self._create(pdf.name, mode="automatic")
        source_image = self.root / "uncertain-render.png"
        image = Image.new("RGB", (40, 30), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 4, 16, 25), fill="black")
        draw.rectangle((24, 4, 37, 25), fill="black")
        image.save(source_image)
        original_bytes = source_image.read_bytes()
        source_only = Voucher(origin={
            "source_token": "uncertain-layout-token", "record_role": "source_page",
            "layout": {"content_kind": "unknown", "auto_split": False, "regions": []},
        })
        source_only.source_image = str(source_image)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_page", return_value=[source_only]):
            self.assertTrue(import_jobs.run_once())

        view = import_jobs.get_job(job["id"], 1)
        self.assertEqual(view["status"], "awaiting_review")
        self.assertEqual([page["status"] for page in view["pages"]], ["preview_ready", "queued"])
        self.assertEqual(len(view["pages"][0]["voucher_ids"]), 1)
        page = next(row for row in self._rows(ImportPage) if row.page_no == 1)
        source_record = review.get_voucher(page.source_page_id)
        working_copy = review.get_voucher(view["pages"][0]["voucher_ids"][0])
        source_data = json.loads(source_record.current_json)
        working_data = json.loads(working_copy.current_json)
        self.assertEqual(source_record.record_role, "source_page")
        self.assertEqual(working_copy.record_role, "voucher")
        self.assertEqual(source_data["origin"]["layout"]["content_kind"], "unknown")
        self.assertEqual(working_data["origin"]["import_job_id"], job["id"])
        self.assertEqual(working_data["origin"]["import_page_id"], page.id)
        self.assertNotIn("record_role", working_data["origin"])
        self.assertNotIn("layout", working_data["origin"])
        self.assertTrue(preparation.manual(working_data))
        self.assertFalse(preparation.state(working_data)["direction_confirmed"])
        self.assertEqual(source_image.read_bytes(), original_bytes)

    def test_automatic_working_copy_split_is_model_free_and_tracks_every_child(self):
        pdf = self._source("automatic-manual-split.pdf")
        job = self._create(pdf.name, mode="automatic")
        source_image = self.root / "split-render.png"
        image = Image.new("RGB", (40, 30), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((3, 4, 16, 25), fill="black")
        draw.rectangle((24, 4, 37, 25), fill="black")
        image.save(source_image)
        parent = Voucher(origin={
            "source_token": "automatic-split-token", "record_role": "source_page",
            "layout": {"content_kind": "unknown", "auto_split": False, "regions": []},
        })
        original_working = Voucher(doc_type_key="invoice", origin={"source_token": "automatic-split-token"})
        for voucher in (parent, original_working):
            voucher.source_image = str(source_image)

        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_page", return_value=[parent, original_working]):
            self.assertTrue(import_jobs.run_once())
        first = import_jobs.get_job(job["id"], 1)
        self.assertEqual(first["status"], "awaiting_review")
        self.assertEqual([page["status"] for page in first["pages"]], ["preview_ready", "queued"])
        page = next(row for row in self._rows(ImportPage) if row.page_no == 1)
        self.assertEqual(len(first["pages"][0]["voucher_ids"]), 1)
        working_id = first["pages"][0]["voucher_ids"][0]
        working = review.get_voucher(working_id)
        self.assertNotIn("preparation", json.loads(working.current_json)["origin"])

        with patch.object(pipeline, "prepare_image", side_effect=AssertionError("orientation/model prep called")) as prepare_image, \
             patch.object(model_factory, "get_ocr", side_effect=AssertionError("OCR called")) as get_ocr, \
             patch.object(model_factory, "get_chat_model", side_effect=AssertionError("chat model called")) as get_chat:
            split = review.split_voucher(
                working_id, [0, 0, 0.5, 1], expected_updated_at=working.updated_at.isoformat(),
                actor={"id": 1, "username": "sequential-owner"},
            )
            self.assertIsNotNone(split)
            prepare_image.assert_not_called()
            get_ocr.assert_not_called()
            get_chat.assert_not_called()

        page_view = import_jobs.get_job(job["id"], 1)["pages"][0]
        self.assertEqual(page_view["status"], "preview_ready")
        self.assertEqual(set(page_view["voucher_ids"]), {working_id, split.id})
        self.assertEqual(len(page_view["voucher_ids"]), 2)
        for ident in page_view["voucher_ids"]:
            record = review.get_voucher(ident)
            data = json.loads(record.current_json)
            self.assertEqual(data["origin"]["import_job_id"], job["id"])
            self.assertEqual(data["origin"]["import_page_id"], page.id)
            self.assertTrue(preparation.manual(data))

        first_confirmed = review.get_voucher(working_id)
        review.confirm_appearance(
            working_id, expected_updated_at=first_confirmed.updated_at.isoformat(), direction_confirmed=True,
        )
        self.assertEqual(import_jobs.get_job(job["id"], 1)["pages"][0]["status"], "preview_ready")
        self.assertEqual(import_jobs.get_job(job["id"], 1)["pages"][1]["status"], "queued")
        last_confirmed = review.get_voucher(split.id)
        review.confirm_appearance(
            split.id, expected_updated_at=last_confirmed.updated_at.isoformat(), direction_confirmed=True,
        )
        advanced = import_jobs.get_job(job["id"], 1)
        self.assertEqual(advanced["pages"][0]["status"], "completed")
        self.assertEqual(advanced["pages"][1]["status"], "queued")
        self.assertEqual(advanced["status"], "queued")

    def test_stale_and_noncurrent_page_appearance_confirmations_are_rejected(self):
        pdf = self._source("turn-order.pdf")
        job = import_jobs.create_job([{"path": str(pdf), "name": pdf.name}], 1)
        source_image = self.root / "turn-order-render.png"
        Image.new("RGB", (32, 24), "white").save(source_image)
        with patch.object(import_jobs.pipeline, "page_count", return_value=2), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source_image), \
             patch.object(import_jobs.pipeline, "prepare_manual_page", side_effect=lambda source, path, page_no, *, token, group_id:
                          self._prepared_vouchers(source, token, Path(path).name, page_no)):
            self.assertTrue(import_jobs.run_once())

        view = import_jobs.get_job(job["id"], 1)
        self.assertEqual([page["status"] for page in view["pages"]], ["preview_ready", "queued"])
        first_page, second_page = self._rows(ImportPage)
        first_child_id = json.loads(first_page.voucher_ids_json)[0]
        first_child = review.get_voucher(first_child_id)
        with self.assertRaises(entry_workflow.EntryConflict):
            review.confirm_appearance(
                first_child_id, expected_updated_at="2000-01-01T00:00:00",
                direction_confirmed=True,
            )

        # The worker has not created this copy yet; seed one to exercise the
        # server-side current-page guard independently of the UI list.
        second_origin = {
            "source_token": second_page.source_token,
            "import_job_id": job["id"],
            "import_page_id": second_page.id,
            "preparation": preparation.fresh(pdf=False),
        }
        source_record = Voucher(origin={**second_origin, "record_role": "source_page"})
        second_working_copy = Voucher(doc_type_key="invoice", origin=dict(second_origin))
        for voucher in (source_record, second_working_copy):
            voucher.source_image = str(source_image)
        with Session(self.engine) as session:
            records = review.save_prepared_page([source_record, second_working_copy], "", session)
            session.commit()
            noncurrent_id = records[1].id
            noncurrent_updated_at = records[1].updated_at.isoformat()

        with self.assertRaises(import_jobs.JobConflict):
            review.confirm_appearance(
                noncurrent_id, expected_updated_at=noncurrent_updated_at,
                direction_confirmed=True,
            )
        self.assertEqual(review.get_voucher(noncurrent_id).status, ReviewStatus.prepared)

    def test_cancelled_job_cannot_resume_while_new_owner_job_holds_lock(self):
        old = self._create("old.pdf", owner=1)
        with Session(self.engine) as session:
            saved = session.get(ImportJob, old["id"])
            saved.status = "cancelled"
            session.add(saved)
            session.commit()
        current = self._create("current.pdf", owner=1)
        with self.assertRaises(import_jobs.JobConflict):
            import_jobs.action(old["id"], 1, "resume")
        self.assertEqual(import_jobs.get_job(current["id"], 1)["status"], "queued")

    def test_cancel_waiting_failed_and_interrupted_jobs_releases_owner_lock(self):
        for status in ("awaiting_review", "failed", "interrupted"):
            with self.subTest(status=status):
                job = self._create(f"cancel-{status}.pdf", owner=1)
                with Session(self.engine) as session:
                    saved = session.get(ImportJob, job["id"])
                    saved.status = status
                    session.add(saved)
                    session.commit()

                cancelled = import_jobs.action(job["id"], 1, "cancel")
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertTrue(self._create(f"after-{status}.pdf", owner=1)["id"])
                # Release the newly created job before the next subcase.
                with Session(self.engine) as session:
                    current = session.exec(select(ImportJob).where(
                        ImportJob.owner_id == 1, ImportJob.status == "queued"
                    )).one()
                    current.status = "cancelled"
                    session.add(current)
                    session.commit()

    def test_legacy_orphan_review_page_can_be_cancelled_without_deleting_source(self):
        source = self._source("legacy-orphan.pdf")
        original_bytes = source.read_bytes()
        job = import_jobs.create_job([{"path": str(source), "name": source.name}], 1)
        with Session(self.engine) as session:
            saved_job = session.get(ImportJob, job["id"])
            snapshot = json.loads(saved_job.settings_json)
            snapshot["mode"] = "manual_review"
            snapshot.pop("sequential_review", None)
            saved_job.settings_json = json.dumps(snapshot)
            saved_job.status = "awaiting_review"
            file = session.exec(select(ImportFile).where(ImportFile.job_id == job["id"])).one()
            session.add(ImportPage(
                id="legacy-orphan-page", job_id=job["id"], file_id=file.id,
                page_no=1, source_token="legacy-orphan-token", status="preview_ready",
                phase="awaiting_review", voucher_ids_json=json.dumps([987654321]),
            ))
            session.add(saved_job)
            session.commit()

        waiting = import_jobs.get_job(job["id"], 1)
        self.assertFalse(waiting["sequential_review"])
        self.assertEqual(waiting["status"], "awaiting_review")
        self.assertEqual(waiting["review_ids"], [])

        cancelled = import_jobs.action(job["id"], 1, "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(source.is_file())
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(self._rows(VoucherRecord), [])
        self.assertTrue(self._create("after-legacy-cancel.pdf", owner=1)["id"])


if __name__ == "__main__":
    unittest.main()
