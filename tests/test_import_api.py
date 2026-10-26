"""HTTP contract tests for authenticated asynchronous imports."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import import_api
from app.services import import_jobs


class ImportApiTests(unittest.TestCase):
    def setUp(self):
        temp_parent = Path(__file__).resolve().parents[1] / "app" / "data" / "test-tmp-plan"
        temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_main = types.ModuleType("app.main")

        async def save_upload(file):
            if file.filename.endswith(".bad"):
                raise ValueError("invalid multipart file")
            path = self.root / file.filename
            path.write_bytes(await file.read())
            return path

        self.fake_main._save_upload = save_upload
        self.main_patch = patch.dict(sys.modules, {"app.main": self.fake_main})
        self.main_patch.start()
        self.addCleanup(self.main_patch.stop)

        app = FastAPI()
        app.include_router(import_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

        self.user_patch = patch.object(import_api.auth, "get_user_by_token", side_effect=self.user_for_token)
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)
        self.service_patches = [
            patch.object(import_jobs, "create_job", return_value={"ok": True, "id": "job-1", "status": "queued"}),
            patch.object(import_jobs, "list_jobs", return_value={"items": []}),
            patch.object(import_jobs, "get_job", return_value={"id": "job-1", "status": "queued"}),
            patch.object(import_jobs, "action", return_value={"id": "job-1", "status": "running"}),
            patch.object(import_jobs, "preview", return_value=self._preview_file()),
        ]
        self.create_job, self.list_jobs, self.get_job, self.action, self.preview = [p.start() for p in self.service_patches]
        for active in self.service_patches:
            self.addCleanup(active.stop)

    @staticmethod
    def user_for_token(token):
        return {"owner-1": {"id": 1}, "owner-2": {"id": 2}}.get(token)

    def _preview_file(self):
        path = Path(self.temp.name) / "page-preview.png"
        path.write_bytes(b"synthetic preview")
        return path

    def test_create_returns_accepted_and_only_enqueues_job(self):
        with patch.object(import_jobs.pipeline, "prepare_page") as prepare_page:
            response = self.client.post(
                "/api/import-jobs", cookies={"session": "owner-1"},
                files=[("files", ("sample.pdf", b"synthetic pdf", "application/pdf"))],
                data={"company": "Example"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")
        args, kwargs = self.create_job.call_args
        self.assertEqual(args[1:], (1, "Example"))
        self.assertEqual(args[0][0]["name"], "sample.pdf")
        self.assertTrue(Path(args[0][0]["path"]).is_file())
        prepare_page.assert_not_called()

    def test_missing_session_returns_401_without_touching_job_service(self):
        response = self.client.get("/api/import-jobs/job-1")
        self.assertEqual(response.status_code, 401)
        for service in (self.create_job, self.list_jobs, self.get_job, self.action, self.preview):
            service.assert_not_called()

    def test_owner_scoped_detail_and_preview_map_not_found_to_404(self):
        self.get_job.side_effect = import_jobs.JobNotFound("missing")
        self.preview.side_effect = import_jobs.JobNotFound("missing")
        detail = self.client.get("/api/import-jobs/job-1", cookies={"session": "owner-2"})
        preview = self.client.get(
            "/api/import-jobs/job-1/pages/page-1/preview", cookies={"session": "owner-2"}
        )
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(preview.status_code, 404)
        self.get_job.assert_called_once_with("job-1", 2)
        self.preview.assert_called_once_with("job-1", "page-1", 2)

    def test_conflict_and_invalid_action_have_distinct_http_statuses(self):
        self.action.side_effect = import_jobs.JobConflict("cannot resume")
        conflict = self.client.post("/api/import-jobs/job-1/resume", cookies={"session": "owner-1"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self.action.call_args.args, ("job-1", 1, "resume"))

        self.action.reset_mock()
        self.action.side_effect = ValueError("unknown action")
        invalid = self.client.post("/api/import-jobs/job-1/not-an-action", cookies={"session": "owner-1"})
        self.assertEqual(invalid.status_code, 400)

    def test_invalid_multipart_returns_400_without_creating_job(self):
        response = self.client.post(
            "/api/import-jobs", cookies={"session": "owner-1"},
            files=[("files", ("invalid.bad", b"bad", "application/octet-stream"))],
        )
        self.assertEqual(response.status_code, 400)
        self.create_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
