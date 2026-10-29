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
from app.services import recognition_sources, import_jobs, review


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
        for name, path in (("DATA_DIR", self.root / "data"), ("LOG_DIR", self.root / "logs")):
            patcher = patch.object(review.config, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
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
        with self.assertRaises(recognition_sources.ReviewConflict):
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

    @contextmanager
    def _deletable_pdf(self, count=3):
        """Exercise real PDF rendering and manual preparation, with no models."""
        upload = review.config.UPLOAD_DIR
        upload.mkdir(parents=True, exist_ok=True)
        pdf = upload / ("a" * 32 + ".pdf")
        images = [Image.new("RGB", (40, 50), (240 - n, 250, 255)) for n in range(count)]
        images[0].save(pdf, "PDF", save_all=True, append_images=images[1:])
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        with patch.object(model_factory, "get_ocr", side_effect=AssertionError("OCR called")), \
             patch.object(model_factory, "get_chat_model", side_effect=AssertionError("model called")):
            job = import_jobs.create_job([{"path": str(pdf), "name": "synthetic.pdf"}], 1)
            self.assertTrue(import_jobs.run_once())
            yield job["id"], pdf

    def _current_delete_record(self, job_id):
        view = import_jobs.get_job(job_id, 1)
        return review.get_voucher(view["review_ids"][0])

    def _delete_page(self, rec, **kwargs):
        return review.delete_vouchers(rec.id, "page", actor_id=1,
                                      expected_updated_at=rec.updated_at.isoformat(), **kwargs)

    def test_delete_every_pdf_page_advances_and_unlocks_upload_without_recreating_pages(self):
        with self._deletable_pdf() as (job_id, pdf):
            original_pdf = pdf.read_bytes()
            for page_no in range(1, 4):
                rec = self._current_delete_record(job_id)
                preview = review.delete_previews(rec.id, actor_id=1)
                self.assertEqual(list(preview["previews"]), ["page"])
                info = preview["previews"]["page"]
                self.assertEqual((info["page_no"], info["voucher_count"], info["source_page_count"]),
                                 (page_no, 1, 1))
                self.assertEqual(preview["can_delete"], {"page": True})
                images = [review.config.UPLOAD_DIR / review.get_voucher(ident).image_path for ident in info["ids"]]
                token = json.loads(rec.current_json)["origin"]["source_token"]
                with patch.object(import_jobs._wake, "set") as wake:
                    result = self._delete_page(rec)
                    wake.assert_called_once()
                self.assertEqual(result["deleted_ids"], info["ids"])
                self.assertFalse(result["cleanup_failed"])
                self.assertTrue(all(not path.exists() for path in images))
                self.assertFalse((review.config.UPLOAD_DIR / "_pages" / token).exists())
                view = import_jobs.get_job(job_id, 1)
                deleted_page = view["pages"][page_no - 1]
                self.assertEqual((deleted_page["status"], deleted_page["phase"]), ("completed", "discarded"))
                self.assertIsNone(deleted_page["source_page_id"])
                self.assertIsNone(deleted_page["preview_url"])
                self.assertEqual(deleted_page["voucher_ids"], [])
                if page_no < 3:
                    self.assertEqual(view["status"], "queued")
                    self.assertEqual(pdf.read_bytes(), original_pdf)
                    self.assertTrue(import_jobs.run_once())
                    import_jobs.recover_interrupted()
                    self.assertEqual(import_jobs.get_job(job_id, 1)["current_page"], page_no + 1)
                else:
                    self.assertEqual(view["status"], "completed")
                    self.assertFalse(view["blocks_upload"])
                    self.assertFalse(pdf.exists())
            import_jobs.recover_interrupted()
            self.assertFalse(import_jobs.run_once())
            self.assertEqual(self._rows(VoucherRecord), [])
            self.assertTrue(self._create("next-upload.pdf")["id"])

    def test_delete_middle_and_last_page_preserves_kept_page_and_other_records(self):
        with self._deletable_pdf() as (job_id, pdf):
            original_pdf = pdf.read_bytes()
            kept = self._current_delete_record(job_id)
            kept_image = review.config.UPLOAD_DIR / kept.image_path
            original_image = kept_image.read_bytes()
            unrelated = Voucher(doc_type_key="receipt", source_image=str(kept_image), source_file="other.png")
            other = review.save_voucher(unrelated)
            review.confirm_appearance(kept.id, expected_updated_at=kept.updated_at.isoformat(),
                                      direction_confirmed=True, actor={"id": 1})
            self.assertTrue(import_jobs.run_once())
            self._delete_page(self._current_delete_record(job_id))
            self.assertEqual(pdf.read_bytes(), original_pdf)
            self.assertTrue(import_jobs.run_once())
            self._delete_page(self._current_delete_record(job_id))
            self.assertEqual(import_jobs.get_job(job_id, 1)["status"], "completed")
            self.assertEqual(review.get_voucher(kept.id).status, ReviewStatus.queued)
            self.assertIsNotNone(review.get_voucher(other.id))
            self.assertEqual(kept_image.read_bytes(), original_image)
            self.assertEqual(pdf.read_bytes(), original_pdf)
            restored = review.delete_previews(kept.id, actor_id=1)
            self.assertEqual(list(restored["previews"]), ["record"])

    def test_page_delete_api_guards_agree_with_preview(self):
        from types import SimpleNamespace
        from fastapi.responses import JSONResponse
        from tests.test_confirmation_workflow import _load_endpoint
        busy = [False]
        users = {"owner": {"id": 1}, "other": {"id": 2, "is_admin": True}}
        namespace = {"review": review, "JSONResponse": JSONResponse,
                     "auth": SimpleNamespace(get_user_by_token=lambda token: users.get(token)),
                     "_delete_busy": lambda: busy[0]}
        preview = _load_endpoint("api_delete_preview", dict(namespace))
        delete = _load_endpoint("api_delete", dict(namespace))
        request = lambda token: SimpleNamespace(cookies={"session": token})
        with self._deletable_pdf() as (job_id, pdf):
            rec = self._current_delete_record(job_id)
            version = rec.updated_at.isoformat()
            self.assertTrue(preview(request("owner"), rec.id)["can_delete"]["page"])
            blocked = preview(request("other"), rec.id)
            self.assertFalse(blocked["can_delete"]["page"])
            self.assertIn("只有", blocked["previews"]["page"]["blocked_reason"])
            self.assertEqual(delete(request("other"), rec.id, "page", version).status_code, 403)
            self.assertEqual(delete(request("owner"), rec.id, "page").status_code, 422)
            self.assertEqual(delete(request("owner"), rec.id, "page", "old-version").status_code, 409)
            for scope in ("record", "upload"):
                self.assertFalse(preview(request("owner"), rec.id, scope)["can_delete"][scope])
                self.assertEqual(delete(request("owner"), rec.id, scope).status_code, 403)
            busy[0] = True
            self.assertFalse(preview(request("owner"), rec.id)["can_delete"]["page"])
            self.assertEqual(delete(request("owner"), rec.id, "page", version).status_code, 409)
            busy[0] = False
            for status in ("queued", "running", "interrupted", "failed", "cancelled"):
                with Session(self.engine) as session:
                    job = session.get(ImportJob, job_id)
                    job.status = status
                    session.add(job)
                    session.commit()
                self.assertFalse(preview(request("owner"), rec.id, "page")["can_delete"]["page"])
                self.assertEqual(delete(request("owner"), rec.id, "page", version).status_code, 409)
            with Session(self.engine) as session:
                job = session.get(ImportJob, job_id)
                job.status = "awaiting_review"
                session.add(job)
                session.commit()
            self.assertEqual(review.get_voucher(rec.id).updated_at.isoformat(), version)
            self.assertTrue(pdf.exists())
            result = delete(request("owner"), rec.id, "page", version)
            self.assertTrue(result["ok"])
            self.assertEqual(delete(request("owner"), rec.id, "page", version).status_code, 404)
            self.assertTrue(import_jobs.run_once())
            # SQLite may reuse this ID for the next page; the old version still cannot delete it.
            self.assertEqual(delete(request("owner"), rec.id, "page", version).status_code, 409)
            self.assertEqual(import_jobs.get_job(job_id, 1)["current_page"], 2)

    def test_page_delete_is_atomic_when_commit_fails(self):
        with self._deletable_pdf() as (job_id, pdf):
            rec = self._current_delete_record(job_id)
            before = import_jobs.get_job(job_id, 1)
            image = review.config.UPLOAD_DIR / rec.image_path
            with patch.object(Session, "commit", side_effect=RuntimeError("commit failed")), \
                 patch.object(review, "_cleanup_deleted_files") as cleanup, \
                 patch.object(import_jobs._wake, "set") as wake:
                with self.assertRaisesRegex(RuntimeError, "commit failed"):
                    self._delete_page(rec)
                cleanup.assert_not_called()
                wake.assert_not_called()
            self.assertEqual(import_jobs.get_job(job_id, 1), before)
            self.assertIsNotNone(review.get_voucher(rec.id))
            self.assertTrue(pdf.exists() and image.exists())

    def test_noncurrent_or_already_confirmed_page_cannot_be_deleted(self):
        with self._deletable_pdf(count=2) as (job_id, pdf):
            current = self._current_delete_record(job_id)
            future = next(page for page in self._rows(ImportPage) if page.page_no == 2)
            origin = {"source_token": future.source_token, "import_job_id": job_id,
                      "import_page_id": future.id, "preparation": preparation.fresh()}
            vouchers = [Voucher(origin={**origin, "record_role": "source_page"}), Voucher(origin=origin)]
            for voucher in vouchers:
                voucher.source_image = str(review.config.UPLOAD_DIR / current.image_path)
            with Session(self.engine) as session:
                records = review.save_prepared_page(vouchers, "", session)
                page = session.get(ImportPage, future.id)
                page.status, page.phase = "preview_ready", "awaiting_review"
                page.source_page_id = records[0].id
                page.voucher_ids_json = json.dumps([records[1].id])
                session.add(page)
                session.commit()
                future_id = records[1].id
            future_rec = review.get_voucher(future_id)
            preview = review.delete_previews(future_id, actor_id=1)
            self.assertFalse(preview["can_delete"]["page"])
            with self.assertRaises(review.DeleteConflict):
                self._delete_page(future_rec)
            with Session(self.engine) as session:
                rec = session.get(VoucherRecord, current.id)
                rec.status = ReviewStatus.queued
                session.add(rec)
                session.commit()
            self.assertFalse(review.delete_previews(current.id, "page", actor_id=1)["can_delete"]["page"])
            with self.assertRaises(review.DeleteConflict):
                self._delete_page(current)
            self.assertEqual(import_jobs.get_job(job_id, 1)["current_page"], 1)
            self.assertIsNotNone(review.get_voucher(future_id))

    def test_page_cleanup_preserves_files_when_worker_reuses_deleted_record_ids(self):
        with self._deletable_pdf(count=2) as (job_id, pdf):
            rec = self._current_delete_record(job_id)
            old_token = json.loads(rec.current_json)["origin"]["source_token"]
            cleanup = review._cleanup_deleted_files
            def advance_before_cleanup(deleted):
                self.assertTrue(import_jobs.run_once())
                return cleanup(deleted)
            with patch.object(review, "_cleanup_deleted_files", side_effect=advance_before_cleanup):
                result = self._delete_page(rec)
            self.assertFalse(result["cleanup_failed"])
            next_rec = self._current_delete_record(job_id)
            self.assertEqual(next_rec.id, rec.id)
            self.assertTrue((review.config.UPLOAD_DIR / next_rec.image_path).is_file())
            self.assertTrue(pdf.is_file())
            self.assertFalse((review.config.UPLOAD_DIR / "_pages" / old_token).exists())

    def test_page_scope_includes_all_derived_documents_and_preserves_confirmed_permissions(self):
        with self._deletable_pdf(count=1) as (job_id, pdf):
            rec = self._current_delete_record(job_id)
            sibling_data = Voucher.model_validate(json.loads(rec.current_json))
            sibling_data.source_image = str(review.config.UPLOAD_DIR / rec.image_path)
            sibling = review.save_voucher(sibling_data, status=ReviewStatus.confirmed,
                                          source_parent_id=rec.source_parent_id)
            preview = review.delete_previews(rec.id, actor_id=1)
            self.assertEqual(preview["previews"]["page"]["voucher_count"], 2)
            self.assertFalse(preview["can_delete"]["page"])
            with self.assertRaises(review.DeleteForbidden):
                self._delete_page(rec)
            result = self._delete_page(rec, is_admin=True)
            self.assertEqual(result["deleted_ids"], sorted([rec.id, rec.source_parent_id, sibling.id]))
            self.assertEqual(import_jobs.get_job(job_id, 1)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
