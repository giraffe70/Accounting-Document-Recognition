"""Offline workflow contracts for fast, human-first page preparation."""
import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
from sqlmodel import SQLModel, Session, create_engine, select

from app.core import doc_router, model_factory, ocr_engine, page_layout, pipeline, preparation, settings_store, text_evidence
from app.db import session as db_session
from app.db.models import ImportFile, ImportJob, ImportPage, ReviewStatus, UserAccount, VoucherRecord
from app.models.schemas import Voucher
from app.services import import_jobs, review


class FastWorkflowContracts(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine(
            f"sqlite:///{self.root / 'workflow.sqlite'}", connect_args={"check_same_thread": False}
        )
        SQLModel.metadata.create_all(self.engine)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.session_patch_import = patch.object(import_jobs, "get_session", self.session_context)
        self.session_patch_review = patch.object(review, "get_session", self.session_context)
        self.session_patch_import.start()
        self.session_patch_review.start()
        self.upload_patch_import = patch.object(import_jobs.config, "UPLOAD_DIR", self.uploads)
        self.upload_patch_review = patch.object(review.config, "UPLOAD_DIR", self.uploads)
        self.data_patch = patch.object(review.config, "DATA_DIR", self.root / "data")
        self.upload_patch_import.start()
        self.upload_patch_review.start()
        self.data_patch.start()
        self.versions_patch = patch.object(import_jobs.model_invocation, "source_versions", return_value={
            "execution_version_sha256": "test-version",
        })
        self.versions_patch.start()
        self.addCleanup(self.versions_patch.stop)
        self.addCleanup(self.upload_patch_review.stop)
        self.addCleanup(self.upload_patch_import.stop)
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.session_patch_review.stop)
        self.addCleanup(self.session_patch_import.stop)
        self.addCleanup(self.engine.dispose)
        with Session(self.engine) as session:
            session.add(UserAccount(id=1, username="workflow-owner"))
            session.add(UserAccount(id=2, username="other-workflow-owner"))
            session.commit()

    @contextmanager
    def session_context(self):
        with Session(self.engine) as session:
            yield session

    def _source(self, name="source.png"):
        path = self.root / name
        Image.new("RGB", (40, 30), (240, 240, 240)).save(path)
        return path

    def _job(self, *, name="source.png", mode=None, owner=1):
        source = self._source(name)
        kwargs = {} if mode is None else {"mode": mode}
        return source, import_jobs.create_job(
            [{"path": str(source), "name": name}], owner, **kwargs
        )

    def _rows(self, model):
        with Session(self.engine) as session:
            return session.exec(select(model)).all()

    @staticmethod
    def _vouchers(token, source, file_name="source.png"):
        prep = preparation.fresh(pdf=False)
        parent = Voucher(origin={"source_token": token, "record_role": "source_page",
                                 "preparation": dict(prep),
                                 "layout": {"auto_split": False, "content_kind": "unknown",
                                            "regions": [], "issues": []}})
        child = Voucher(origin={"source_token": token, "preparation": dict(prep)})
        for voucher in (parent, child):
            voucher.source_image = str(source)
            voucher.source_file = file_name
        return [parent, child]

    def _manual_record(self, name):
        image = self.uploads / "pages" / name
        image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 30), (220, 230, 240)).save(image)
        payload = Voucher(origin={"preparation": preparation.fresh(), "source_token": uuid.uuid4().hex}).model_dump()
        blob = json.dumps(payload, ensure_ascii=False)
        with Session(self.engine) as session:
            rec = VoucherRecord(status=ReviewStatus.prepared, ai_json=blob, current_json=blob,
                                image_path=f"pages/{name}", source_file=name)
            session.add(rec)
            session.commit()
            session.refresh(rec)
            return rec.id

    def test_default_manual_job_prepares_copies_without_model_work_and_worker_moves_on(self):
        source1, first = self._job(name="first.png")
        source2, second = self._job(name="second.png", owner=2)

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: source1), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=AssertionError("automatic preparation")) as automatic, \
             patch.object(model_factory, "get_ocr", side_effect=AssertionError("OCR started")) as get_ocr, \
             patch.object(page_layout, "detect", side_effect=AssertionError("layout detection started")) as detect, \
             patch.object(pipeline, "prepare_image", side_effect=AssertionError("image preparation started")) as prepare_image:
            self.assertTrue(import_jobs.run_once())

            first_view = import_jobs.get_job(first["id"], 1)
            self.assertEqual(first_view["mode"], "manual_review")
            self.assertEqual(first_view["status"], "awaiting_review")
            self.assertEqual(first_view["awaiting_review_pages"], 1)
            page = self._rows(ImportPage)[0]
            self.assertEqual(page.status, "preview_ready")
            ids = json.loads(page.voucher_ids_json)
            records = {row.id: row for row in self._rows(VoucherRecord)}
            child = records[ids[0]]
            parent = records[page.source_page_id]
            self.assertEqual(parent.record_role, "source_page")
            self.assertEqual(child.status, ReviewStatus.prepared)
            self.assertEqual(child.record_role, "voucher")
            self.assertNotEqual(self.uploads / child.image_path, source1)
            self.assertTrue((self.uploads / child.image_path).is_file())
            self.assertEqual((self.uploads / child.image_path).read_bytes(), source1.read_bytes())
            self.assertEqual((self.uploads / parent.image_path).read_bytes(), source1.read_bytes())
            automatic.assert_not_called()
            get_ocr.assert_not_called()
            detect.assert_not_called()
            prepare_image.assert_not_called()

            # The first job is now waiting for a person; the worker can take another job.
            with patch.object(import_jobs.pipeline, "render_page", side_effect=lambda *_: source2):
                self.assertTrue(import_jobs.run_once())
            self.assertEqual(import_jobs.get_job(second["id"], 2)["status"], "awaiting_review")

    def test_legacy_snapshot_without_mode_uses_automatic_path(self):
        source, job = self._job(name="legacy.png", mode="automatic")
        with Session(self.engine) as session:
            saved = session.get(ImportJob, job["id"])
            snapshot = json.loads(saved.settings_json)
            snapshot.pop("mode")
            snapshot.pop("sequential_review")
            saved.settings_json = json.dumps(snapshot)
            session.add(saved)
            session.commit()

        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source), \
             patch.object(import_jobs.pipeline, "prepare_page", side_effect=lambda _s, _p, _n, *, token, group_id:
                          self._vouchers(token, source, "legacy.png")) as automatic, \
             patch.object(import_jobs.pipeline, "prepare_manual_page", side_effect=AssertionError("manual path")) as manual:
            self.assertTrue(import_jobs.run_once())
        automatic.assert_called_once()
        manual.assert_not_called()
        result = import_jobs.get_job(job["id"], 1)
        self.assertEqual(result["mode"], "automatic")
        self.assertEqual(result["status"], "completed")

    def test_appearance_requires_version_and_queues_without_direction_checkbox(self):
        source, job = self._job(name="confirm.png")
        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source):
            self.assertTrue(import_jobs.run_once())
        page = self._rows(ImportPage)[0]
        child_id = json.loads(page.voucher_ids_json)[0]
        child = review.get_voucher(child_id)

        with self.assertRaises(review.RecognitionProtected):
            review.confirm_appearance(child_id, "invoice")

        with patch.object(model_factory, "get_ocr", side_effect=AssertionError("OCR started")) as get_ocr, \
             patch.object(page_layout, "detect", side_effect=AssertionError("layout detection started")) as detect, \
             patch.object(pipeline, "prepare_image", side_effect=AssertionError("image preparation started")) as prepare_image:
            saved = review.confirm_appearance(
                child_id, "invoice", expected_updated_at=child.updated_at.isoformat(),
                actor={"id": 1},
            )
            self.assertEqual(saved.status, ReviewStatus.queued)
            data = json.loads(saved.current_json)
            self.assertTrue(preparation.confirmed(data, self.uploads / saved.image_path))
            self.assertTrue(preparation.state(data)["type_confirmed"])
            self.assertEqual(preparation.state(data)["confirmed_by"], 1)
            self.assertTrue(preparation.state(data)["confirmed_at"])
            get_ocr.assert_not_called()
            detect.assert_not_called()
            prepare_image.assert_not_called()
        self.assertEqual(import_jobs.get_job(job["id"], 1)["pages"][0]["status"], "completed")

    def test_recognize_and_rerecognize_reject_unconfirmed_record_without_models(self):
        rec_id = self._manual_record("unconfirmed.png")
        with patch.object(model_factory, "get_ocr", side_effect=AssertionError("OCR started")) as get_ocr, \
             patch.object(page_layout, "detect", side_effect=AssertionError("layout detection started")) as detect, \
             patch.object(pipeline, "prepare_image", side_effect=AssertionError("image preparation started")) as prepare_image, \
             patch.object(pipeline, "recognize_image", side_effect=AssertionError("recognition started")) as recognize_image, \
             patch.object(pipeline, "extract_for_type", side_effect=AssertionError("extraction started")) as extract:
            for operation in (review.recognize_record, review.re_recognize):
                with self.assertRaises(review.RecognitionProtected):
                    operation(rec_id)
            for model in (get_ocr, detect, prepare_image, recognize_image, extract):
                model.assert_not_called()

    def test_real_text_evidence_reuses_one_paddle_result_and_routes_without_manual_evidence(self):
        auto_id = self._manual_record("auto-type.png")
        auto = review.get_voucher(auto_id)
        review.confirm_appearance(auto_id, expected_updated_at=auto.updated_at.isoformat(),
                                  direction_confirmed=True)

        class SyntheticPaddle:
            calls = 0

            def predict(self, _image_path):
                self.calls += 1
                return [{"rec_texts": ["Acme Invoice 123"],
                         "dt_polys": [[[2, 2], [32, 2], [32, 10], [2, 10]]],
                         "rec_scores": [0.99]}]

        backend = SyntheticPaddle()
        settings = {"ocr": "paddle/chinese_cht", "structure": "ollama/synthetic"}
        with patch.object(ocr_engine, "_get_paddle", return_value=backend), \
             patch.object(settings_store, "get_settings", return_value=settings), \
             patch.object(model_factory, "get_chat_model", return_value=object()), \
             patch.object(page_layout, "detect", side_effect=AssertionError("page layout called")), \
             patch.object(pipeline, "prepare_image", side_effect=AssertionError("orientation OCR called")), \
             patch.object(doc_router, "classify", return_value=("invoice", 1)) as classify, \
             patch.object(pipeline, "extract_evidence", side_effect=lambda _image, type_key, *_args, **_kwargs:
                          Voucher(doc_type_key=type_key, fields={"invoice_number": "INV-123"})) as extract:
            first = review.recognize_record(auto_id)
            second = review.re_recognize(auto_id)

            self.assertEqual(first.status, ReviewStatus.pending)
            self.assertEqual(second.status, ReviewStatus.pending)
            self.assertEqual(backend.calls, 1)
            self.assertEqual(classify.call_count, 1)
            self.assertTrue(all(call.kwargs["mode"] == "ocr_text" and
                                call.kwargs["ocr_text"] == "Acme Invoice 123"
                                for call in classify.call_args_list))
            self.assertEqual(extract.call_count, 2)
            evidence = json.loads(second.current_json)["field_evidence"]["doc_type_key"]
            self.assertEqual(evidence[0]["source"], "route")
            self.assertFalse(any(item.get("source") == "manual" for item in evidence))

        manual_id = self._manual_record("manual-type.png")
        manual = review.get_voucher(manual_id)
        manual = review.confirm_appearance(manual_id, "invoice",
                                           expected_updated_at=manual.updated_at.isoformat(),
                                           direction_confirmed=True)
        with patch.object(ocr_engine, "_get_paddle", return_value=backend), \
             patch.object(settings_store, "get_settings", return_value=settings), \
             patch.object(model_factory, "get_chat_model", return_value=object()), \
             patch.object(doc_router, "classify") as classify_manual, \
             patch.object(pipeline, "extract_evidence", side_effect=lambda _image, type_key, *_args, **_kwargs:
                          Voucher(doc_type_key=type_key, fields={"invoice_number": "INV-123"})):
            recognized = review.recognize_record(manual_id)
            self.assertEqual(recognized.status, ReviewStatus.pending)
            classify_manual.assert_not_called()

    def test_rotation_invalidates_confirmation_and_returns_record_to_prepared(self):
        rec_id = self._manual_record("rotate.png")
        rec = review.get_voucher(rec_id)
        rec = review.confirm_appearance(rec_id, "invoice", expected_updated_at=rec.updated_at.isoformat(),
                                        direction_confirmed=True)
        self.assertTrue(preparation.confirmed(json.loads(rec.current_json), self.uploads / rec.image_path))

        self.assertTrue(review.rotate_image(rec_id, expected_updated_at=rec.updated_at.isoformat()))
        updated = review.get_voucher(rec_id)
        data = json.loads(updated.current_json)
        self.assertEqual(updated.status, ReviewStatus.prepared)
        self.assertFalse(preparation.confirmed(data, self.uploads / updated.image_path))
        self.assertTrue(preparation.state(data)["type_confirmed"])

    def _patterned(self, name):
        """A manual working copy whose pixels change visibly under rotate/crop/deskew."""
        rec_id = self._manual_record(name)
        path = self.uploads / review.get_voucher(rec_id).image_path
        image = Image.new("RGB", (60, 40), (250, 250, 250))
        draw = ImageDraw.Draw(image)
        draw.rectangle((5, 8, 50, 12), fill=(0, 0, 0))
        draw.rectangle((5, 22, 30, 24), fill=(20, 20, 120))
        image.save(path)
        return rec_id, path

    @staticmethod
    def _version(rec_id):
        return review.get_voucher(rec_id).updated_at.isoformat()

    def test_prepared_record_keeps_a_baseline_beside_the_working_image(self):
        source = self._source("baseline-source.png")
        voucher = Voucher(origin={"preparation": preparation.fresh()})
        voucher.source_image = str(source)
        prepared = review.save_voucher(voucher, status=ReviewStatus.prepared)
        baseline = (self.uploads / prepared.image_path).with_suffix(".baseline.png")
        self.assertEqual(baseline.read_bytes(), source.read_bytes())

        voucher.source_image = str(source)
        pending = review.save_voucher(voucher, status=ReviewStatus.pending)
        self.assertFalse((self.uploads / pending.image_path).with_suffix(".baseline.png").exists())

    def test_restore_returns_baseline_pixels_and_clears_direction_confirmation(self):
        rec_id, path = self._patterned("restore.png")
        original = path.read_bytes()
        self.assertFalse(review.image_restore_available(review.get_voucher(rec_id)))
        with self.assertRaises(review.RecognitionProtected):   # nothing was changed yet, so no restore point
            review.restore_image(rec_id, expected_updated_at=self._version(rec_id))

        review.confirm_appearance(rec_id, "invoice", expected_updated_at=self._version(rec_id), direction_confirmed=True)
        self.assertTrue(review.rotate_image(rec_id, expected_updated_at=self._version(rec_id)))
        self.assertTrue(review.crop_image(rec_id, [0, 0, 0.8, 1], expected_updated_at=self._version(rec_id)))
        self.assertTrue(review.rotate_fine(rec_id, 2, expected_updated_at=self._version(rec_id)))
        self.assertNotEqual(path.read_bytes(), original)
        self.assertTrue(review.image_restore_available(review.get_voucher(rec_id)))

        self.assertTrue(review.restore_image(rec_id, expected_updated_at=self._version(rec_id)))
        restored = review.get_voucher(rec_id)
        state = preparation.state(json.loads(restored.current_json))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(restored.status, ReviewStatus.prepared)
        self.assertFalse(state["direction_confirmed"])
        self.assertEqual(state["transforms"], [])
        self.assertEqual(state["fine_angle"], 0)
        self.assertFalse(path.with_suffix(".deskew.png").exists())
        self.assertFalse(review.image_restore_available(restored))

    def test_restore_is_version_checked_and_refused_for_source_pages(self):
        rec_id, _path = self._patterned("restore-guard.png")
        self.assertTrue(review.rotate_image(rec_id, expected_updated_at=self._version(rec_id)))
        with self.assertRaises(review.entry_flow.EntryConflict):
            review.restore_image(rec_id, expected_updated_at="2000-01-01T00:00:00")
        with Session(self.engine) as session:
            stored = session.get(VoucherRecord, rec_id)
            stored.record_role = "source_page"
            session.add(stored)
            session.commit()
        with self.assertRaises(review.RecognitionProtected):
            review.restore_image(rec_id, expected_updated_at=self._version(rec_id))

    def test_fine_rotation_is_cumulative_from_one_original(self):
        stepped_id, stepped = self._patterned("fine-stepped.png")
        direct_id, direct = self._patterned("fine-direct.png")
        for angle in (1, 1.5):
            self.assertTrue(review.rotate_fine(stepped_id, angle, expected_updated_at=self._version(stepped_id)))
        self.assertTrue(review.rotate_fine(direct_id, 1.5, expected_updated_at=self._version(direct_id)))

        with Image.open(stepped) as a, Image.open(direct) as b:
            self.assertEqual(a.size, b.size)
            self.assertEqual(a.tobytes(), b.tobytes())
        state = preparation.state(json.loads(review.get_voucher(stepped_id).current_json))
        self.assertEqual(state["fine_angle"], 1.5)
        self.assertEqual(state["transforms"], [])
        self.assertFalse(state["pdf_enabled"])

    def test_resetting_fine_rotation_restores_the_deskew_original_and_keeps_other_edits(self):
        rec_id, path = self._patterned("fine-reset.png")
        self.assertTrue(review.rotate_image(rec_id, expected_updated_at=self._version(rec_id)))
        rotated = path.read_bytes()
        self.assertTrue(review.rotate_fine(rec_id, 3, expected_updated_at=self._version(rec_id)))
        self.assertTrue(path.with_suffix(".deskew.png").is_file())
        self.assertNotEqual(path.read_bytes(), rotated)

        self.assertTrue(review.rotate_fine(rec_id, 0, reset=True, expected_updated_at=self._version(rec_id)))
        state = preparation.state(json.loads(review.get_voucher(rec_id).current_json))
        self.assertEqual(path.read_bytes(), rotated)
        self.assertFalse(path.with_suffix(".deskew.png").exists())
        self.assertEqual(state["fine_angle"], 0)
        self.assertEqual(len(state["transforms"]), 1)

    def test_rotating_or_cropping_finalizes_the_applied_fine_rotation(self):
        for action in ("rotate", "crop"):
            with self.subTest(action=action):
                rec_id, path = self._patterned(f"fine-final-{action}.png")
                self.assertTrue(review.rotate_fine(rec_id, 4, expected_updated_at=self._version(rec_id)))
                if action == "rotate":
                    self.assertTrue(review.rotate_image(rec_id, expected_updated_at=self._version(rec_id)))
                else:
                    self.assertTrue(review.crop_image(rec_id, [0.1, 0.1, 0.9, 0.9], expected_updated_at=self._version(rec_id)))
                finalized = path.read_bytes()
                state = preparation.state(json.loads(review.get_voucher(rec_id).current_json))
                self.assertEqual(state["fine_angle"], 0)
                self.assertFalse(path.with_suffix(".deskew.png").exists())

                # The next deskew starts from the finalized working image, not from the old original.
                self.assertTrue(review.rotate_fine(rec_id, 2, expected_updated_at=self._version(rec_id)))
                self.assertEqual(path.with_suffix(".deskew.png").read_bytes(), finalized)

    def test_fine_rotation_rejects_angles_beyond_the_limit(self):
        rec_id, path = self._patterned("fine-limit.png")
        before = path.read_bytes()
        for angle in (15.5, -16, float("nan"), "abc"):
            with self.subTest(angle=angle), self.assertRaises(review.RecognitionProtected):
                review.rotate_fine(rec_id, angle, expected_updated_at=self._version(rec_id))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(review.rotate_fine(rec_id, -15, expected_updated_at=self._version(rec_id)))

    def test_manual_confirmation_only_accepts_one_document_per_page(self):
        for mode in ("auto_split", "manual_split"):
            with self.subTest(mode=mode):
                rec_id = self._manual_record(f"mode-{mode}.png")
                with self.assertRaises(review.RecognitionProtected):
                    review.confirm_appearance(rec_id, "invoice", expected_updated_at=self._version(rec_id),
                                              direction_confirmed=True, document_mode=mode)
                self.assertEqual(review.get_voucher(rec_id).status, ReviewStatus.prepared)

    def test_appearance_batch_returns_success_and_version_conflict_per_item(self):
        good_id = self._manual_record("batch-good.png")
        stale_id = self._manual_record("batch-stale.png")
        good, stale = review.get_voucher(good_id), review.get_voucher(stale_id)

        result = review.appearance_batch([
            {"id": good_id, "expected_updated_at": good.updated_at.isoformat(),
             "direction_confirmed": True, "doc_type_key": "invoice"},
            {"id": stale_id, "expected_updated_at": "2000-01-01T00:00:00",
             "direction_confirmed": True, "doc_type_key": "invoice"},
        ], "confirm", actor={"id": 1})

        self.assertTrue(result["ok"])
        self.assertEqual([item["ok"] for item in result["items"]], [True, False])
        self.assertEqual(result["items"][0]["status"], 200)
        self.assertEqual(result["items"][1]["status"], 409)

    def test_auto_split_confirmation_queues_layout_for_worker(self):
        source = self.root / "auto-split.png"
        source_image = Image.new("RGB", (40, 30), (240, 240, 240))
        draw = ImageDraw.Draw(source_image)
        draw.rectangle((0, 0, 19, 29), fill=(232, 225, 215))
        draw.rectangle((20, 0, 39, 29), fill=(215, 226, 237))
        source_image.save(source)
        job = import_jobs.create_job([{"path": str(source), "name": source.name}], 1)
        with patch.object(import_jobs.pipeline, "page_count", return_value=1), \
             patch.object(import_jobs.pipeline, "render_page", return_value=source):
            self.assertTrue(import_jobs.run_once())
        page = self._rows(ImportPage)[0]
        rec_id = json.loads(page.voucher_ids_json)[0]
        rec = review.get_voucher(rec_id)
        # 外觀確認已不再接受 auto_split；這裡直接重現舊請求留下的狀態，繼續驗證保留的版面背景工作。
        with self.assertRaises(review.RecognitionProtected):
            review.confirm_appearance(rec_id, "invoice", expected_updated_at=rec.updated_at.isoformat(),
                                      direction_confirmed=True, document_mode="auto_split", actor={"id": 1})
        with review.get_session() as session:
            stored = session.get(VoucherRecord, rec_id)
            stored_data = json.loads(stored.current_json)
            preparation.confirm(stored_data, self.uploads / stored.image_path, document_mode="auto_split", type_confirmed=True)
            stored_data["origin"]["preparation"]["layout_pending"] = True
            stored.current_json = json.dumps(stored_data, ensure_ascii=False)
            stored.updated_at = datetime.now()
            session.add(stored)
            import_jobs.queue_layout(session, stored, 1)
            session.commit()
        queued = review.get_voucher(rec_id)
        self.assertEqual(queued.status, ReviewStatus.prepared)
        self.assertTrue(json.loads(queued.current_json)["origin"]["preparation"]["layout_pending"])
        self.assertEqual(import_jobs.get_job(job["id"], 1)["pages"][0]["status"], "queued")

        current = review.get_voucher(rec_id)
        data = json.loads(current.current_json)
        token = data["origin"]["source_token"]
        folder = self.uploads / "_pages" / token
        image_path = self.uploads / current.image_path
        identity = text_evidence.identity(image_path)
        lines = [
            {"text": "Invoice Left 001", "box": [[2, 2], [12, 2], [12, 9], [2, 9]], "score": 0.99},
            {"text": "Invoice Right 002", "box": [[28, 2], [38, 2], [38, 9], [28, 9]], "score": 0.99},
        ]
        text_evidence._store(folder, {"identity": identity, "source": "ocr", "lines": lines,
                                      "text": "\n".join(line["text"] for line in lines),
                                      "size": identity["size"], "frame": "working_image_pixels",
                                      "matrix": [[1, 0, 0], [0, 1, 0]]})
        layout = {"type": "invoice", "auto_split": True, "content_kind": "document", "issues": [],
                  "regions": [
                      {"type": "invoice", "reason": "left synthetic document", "rotate": 0,
                       "box": [0, 0, 500, 1000]},
                      {"type": "invoice", "reason": "right synthetic document", "rotate": 0,
                       "box": [500, 0, 1000, 1000]},
                  ]}
        observed = {}

        def detect(_image, *, native_path=None, ocr_lines=None, ocr_text=None, orientation_confirmed=False):
            observed.update(native_path=native_path, ocr_lines=ocr_lines, ocr_text=ocr_text,
                            orientation_confirmed=orientation_confirmed)
            return layout

        with patch.object(page_layout, "detect", side_effect=detect), \
             patch.object(model_factory, "get_ocr", side_effect=AssertionError("direction OCR started")) as get_ocr:
            self.assertTrue(import_jobs.run_once())
        get_ocr.assert_not_called()
        self.assertTrue(observed["orientation_confirmed"])
        self.assertEqual(observed["ocr_text"], "Invoice Left 001\nInvoice Right 002")
        self.assertEqual([line["text"] for line in observed["ocr_lines"]],
                         ["Invoice Left 001", "Invoice Right 002"])
        self.assertEqual(import_jobs.get_job(job["id"], 1)["pages"][0]["status"], "preview_ready")
        source_page = review.get_voucher(rec_id)
        self.assertEqual(source_page.record_role, "source_page")
        page = self._rows(ImportPage)[0]
        child_ids = json.loads(page.voucher_ids_json)
        self.assertEqual(len(child_ids), 2)
        self.assertEqual(len(set(child_ids)), 2)
        self.assertNotIn(rec_id, child_ids)
        children = [review.get_voucher(ident) for ident in child_ids]
        self.assertTrue(all(child.source_parent_id == rec_id for child in children))
        self.assertEqual(len({child.image_path for child in children}), 2)
        self.assertEqual(sorted(text_evidence.read(self.uploads / child.image_path, folder=folder)["text"]
                                for child in children), ["Invoice Left 001", "Invoice Right 002"])

        # Resume returns to human review without repeating the committed layout.
        record_count = len(self._rows(VoucherRecord))
        with Session(self.engine) as session:
            saved_job = session.get(ImportJob, job["id"])
            saved_job.status = "running"
            session.add(saved_job)
            session.commit()
        import_jobs.recover_interrupted()
        import_jobs.action(job["id"], 1, "resume")
        self.assertFalse(import_jobs.run_once())
        self.assertEqual(len(self._rows(VoucherRecord)), record_count)
        self.assertEqual(json.loads(self._rows(ImportPage)[0].voucher_ids_json), child_ids)

        # Explicitly clearing an inherited route choice reuses the child's OCR
        # cache while asking the router to classify the unchanged pixels again.
        child = review.get_voucher(child_ids[0])
        child_data = json.loads(child.current_json)
        route_evidence = child_data["field_evidence"]["doc_type_key"]
        self.assertTrue(any(item.get("source") == "route" and item.get("selected")
                            for item in route_evidence))
        cleared = review.set_type(child.id, "", expected_updated_at=child.updated_at.isoformat())
        cleared_data = json.loads(cleared.current_json)
        self.assertFalse(cleared_data.get("doc_type_key"))
        self.assertFalse(cleared_data.get("field_evidence", {}).get("doc_type_key"))

        confirmed = review.confirm_appearance(
            child.id, expected_updated_at=cleared.updated_at.isoformat(), direction_confirmed=True,
        )
        with patch.object(model_factory, "get_ocr", side_effect=AssertionError("cached OCR was missed")) as get_ocr, \
             patch.object(model_factory, "get_chat_model", return_value=object()), \
             patch.object(doc_router, "classify", return_value=("invoice", 1)) as classify_again, \
             patch.object(pipeline, "extract_evidence", side_effect=lambda _image, type_key, *_args, **_kwargs:
                          Voucher(doc_type_key=type_key, fields={"invoice_number": "INV-CACHED"})) as extract_again:
            recognized = review.recognize_record(child.id)
        self.assertEqual(confirmed.status, ReviewStatus.queued)
        self.assertEqual(recognized.status, ReviewStatus.pending)
        get_ocr.assert_not_called()
        classify_again.assert_called_once()
        self.assertEqual(classify_again.call_args.kwargs["ocr_text"], "Invoice Left 001")
        extract_again.assert_called_once()
        recognized_data = json.loads(recognized.current_json)
        self.assertEqual(recognized_data["doc_type_key"], "invoice")
        self.assertEqual(recognized_data["field_evidence"]["doc_type_key"][0]["source"], "route")

    def test_manual_split_creates_unconfirmed_prepared_copy(self):
        rec_id = self._manual_record("split.png")
        rec = review.get_voucher(rec_id)
        original_path = self.uploads / rec.image_path
        original_pixels = original_path.read_bytes()

        child = review.split_voucher(rec_id, [0, 0, 0.5, 1], expected_updated_at=rec.updated_at.isoformat())

        self.assertIsNotNone(child)
        self.assertEqual(child.status, ReviewStatus.prepared)
        self.assertNotEqual(child.id, rec_id)
        child_data = json.loads(child.current_json)
        self.assertFalse(preparation.state(child_data)["direction_confirmed"])
        self.assertFalse(preparation.state(child_data)["type_confirmed"])
        self.assertEqual(preparation.state(child_data)["document_mode"], "single")
        self.assertEqual(preparation.state(child_data)["transforms"][-1]["kind"], "crop")
        self.assertNotEqual(original_path.read_bytes(), original_pixels)
        updated_parent = review.get_voucher(rec_id)
        self.assertEqual(updated_parent.status, ReviewStatus.prepared)
        self.assertFalse(preparation.confirmed(
            json.loads(updated_parent.current_json), original_path,
        ))
        self.assertTrue((self.uploads / child.image_path).is_file())

    def test_legacy_import_pages_get_additive_preparation_json_column(self):
        legacy_engine = create_engine(f"sqlite:///{self.root / 'legacy.sqlite'}")
        self.addCleanup(legacy_engine.dispose)
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE import_pages (id VARCHAR PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO import_pages (id) VALUES ('legacy-page')")
            connection.exec_driver_sql(
                "CREATE TABLE voucher_records (id INTEGER PRIMARY KEY, status VARCHAR, current_json VARCHAR)"
            )

        with patch.object(db_session, "_engine", legacy_engine):
            db_session._migrate()
            db_session._migrate()

        with legacy_engine.connect() as connection:
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(import_pages)")}
            value = connection.exec_driver_sql(
                "SELECT preparation_json FROM import_pages WHERE id='legacy-page'"
            ).scalar_one()
        self.assertIn("preparation_json", columns)
        self.assertEqual(value, "{}")


if __name__ == "__main__":
    unittest.main()
