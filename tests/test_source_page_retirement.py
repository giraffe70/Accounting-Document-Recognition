"""Synthetic SQLite regressions for retiring redundant source-page records."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    ImportFile,
    ImportJob,
    ImportPage,
    ReviewStatus,
    UserAccount,
    VoucherRecord,
)
from app.db.source_page_retirement import retire_source_pages


class SourcePageRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "synthetic.db"
        self.upload_dir = self.root / "uploads"
        (self.upload_dir / "pages").mkdir(parents=True)
        (self.upload_dir / "_pages").mkdir()
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(UserAccount(id=1, username="synthetic", password_hash="unused"))
            session.commit()
            session.add(ImportJob(id="job-a", owner_id=1, status="awaiting_review"))
            session.commit()
            session.add(ImportFile(id="file-a", job_id="job-a", path="uploads/input.pdf",
                                   name="input.pdf", group_id="group-a"))
            session.commit()

    def write_page_file(self, relative):
        path = self.upload_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic image bytes")
        for suffix in ("baseline", "deskew", "orig"):
            path.with_suffix(f".{suffix}{path.suffix}").write_bytes(f"{suffix} bytes".encode())
        return path

    def add_record(self, ident, *, role="voucher", parent=None, image="", attachments=(),
                   status=ReviewStatus.prepared, current=None, ai=None, confirmed_at=None,
                   confirmed_by=None, confirmed_by_name="", confirmed_version=0):
        record = VoucherRecord(
            id=ident,
            source_file=f"scan-{ident}.pdf",
            record_role=role,
            source_parent_id=parent,
            status=status,
            ai_json=json.dumps(ai if ai is not None else {}, ensure_ascii=False),
            current_json=json.dumps(current if current is not None else {}, ensure_ascii=False),
            image_path=image,
            attachments=json.dumps(list(attachments)),
            confirmed_at=confirmed_at,
            confirmed_by=confirmed_by,
            confirmed_by_name=confirmed_by_name,
            recognition_confirmed_version=confirmed_version,
        )
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
        return record

    def rows(self):
        with Session(self.engine) as session:
            return session.exec(select(VoucherRecord).order_by(VoucherRecord.id)).all()

    def raw_record(self, ident):
        with self.engine.connect() as connection:
            return dict(connection.exec_driver_sql(
                "SELECT * FROM voucher_records WHERE id=?", (ident,)).mappings().one())

    def add_page(self, ident, token, *, source_page_id=None, status="preview_ready", phase="preview_ready"):
        with Session(self.engine) as session:
            session.add(ImportPage(
                id=ident, job_id="job-a", file_id="file-a", page_no=int(ident[-1]),
                source_token=token, source_page_id=source_page_id, status=status, phase=phase,
            ))
            session.commit()

    def test_dry_run_reports_sources_without_changing_database_files_or_creating_backup(self):
        token = "a" * 32
        image = "pages/dryrun.jpg"
        self.write_page_file(image)
        self.add_record(10, role="source_page", image=image,
                        current={"origin": {"source_token": token}})
        self.add_record(11, parent=10, current={"origin": {"source_token": token}})
        before = {row.id: self.raw_record(row.id) for row in self.rows()}

        report = retire_source_pages(self.engine, self.upload_dir)

        self.assertEqual(report["source_ids"], [10])
        self.assertEqual(report["retained_count"], 1)
        self.assertFalse(report["applied"])
        self.assertIsNone(report["backup"])
        self.assertEqual({row.id: self.raw_record(row.id) for row in self.rows()}, before)
        self.assertTrue((self.upload_dir / image).is_file())
        self.assertEqual(list((self.db_path.parent / "backups").glob("*.db")) if (self.db_path.parent / "backups").exists() else [], [])

    def test_apply_preserves_all_real_vouchers_and_resolves_import_pages_from_origins(self):
        tokens = {name: char * 32 for name, char in (("one", "a"), ("two", "b"), ("empty", "c"))}
        for image in ("pages/shared.jpg", "pages/shared-attachment.jpg", "pages/pdf-render.jpg",
                      "pages/retired.jpg"):
            self.write_page_file(image)
        external = self.root.parent / f"keep-outside-{self.root.name}.pdf"
        external.write_bytes(b"external synthetic file")
        self.addCleanup(lambda: external.unlink(missing_ok=True))
        descriptors = {
            tokens["two"]: {"path": str(self.upload_dir / "pages/pdf-render.jpg"),
                            "render_source": str(self.upload_dir / "pages/pdf-render.jpg")},
        }
        for token, descriptor in descriptors.items():
            descriptor_path = self.upload_dir / "_pages" / token / "pdf_source.json"
            descriptor_path.parent.mkdir(parents=True)
            descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

        self.add_record(10, role="source_page", image="pages/shared.jpg",
                        attachments=("pages/shared-attachment.jpg",),
                        current={"origin": {"source_token": tokens["one"]}})
        self.add_record(11, role="source_page", image="pages/pdf-render.jpg",
                        attachments=(str(external),),
                        current={"origin": {"source_token": tokens["two"]}})
        self.add_record(12, role="source_page", image="pages/retired.jpg",
                        current={"origin": {"source_token": tokens["empty"]}})
        one = {"fields": {"total_amount": 100}, "origin": {"import_page_id": "page-1",
               "import_job_id": "job-a", "source_token": tokens["one"]}}
        two = {"fields": {"total_amount": 200}, "origin": {"import_page_id": "page-1",
               "import_job_id": "job-a", "source_token": tokens["one"]}}
        three = {"fields": {"total_amount": 300}, "origin": {"source_token": tokens["two"]}}
        confirmed_at = datetime(2026, 10, 1, 8, 30)
        self.add_record(20, parent=10, image="pages/shared.jpg",
                        attachments=("pages/shared-attachment.jpg",), status=ReviewStatus.confirmed,
                        current=one, ai={"model": "synthetic-one"}, confirmed_at=confirmed_at,
                        confirmed_by=1, confirmed_by_name="Synthetic reviewer", confirmed_version=7)
        self.add_record(21, parent=10, image="pages/shared.jpg", status=ReviewStatus.pending,
                        current=two, ai={"model": "synthetic-two"})
        self.add_record(22, parent=11, image="pages/voucher-22.jpg", status=ReviewStatus.queued,
                        current=three, ai={"model": "synthetic-three"})
        self.add_page("page-1", tokens["one"], source_page_id=11)
        self.add_page("page-2", tokens["two"], source_page_id=11)
        self.add_page("page-3", tokens["empty"], source_page_id=10)
        # A duplicate stale source reference with no matching origin must not inherit page-1's vouchers.
        with Session(self.engine) as session:
            session.add(ImportJob(id="job-b", owner_id=1, status="awaiting_review"))
            session.commit()
            session.add(ImportFile(id="file-b", job_id="job-b", path="uploads/other.pdf",
                                   name="other.pdf", group_id="group-b"))
            session.commit()
            session.add(ImportPage(id="page-4", job_id="job-b", file_id="file-b", page_no=4,
                                   source_token="d" * 32, source_page_id=10,
                                   status="preview_ready", phase="preview_ready"))
            session.commit()

        preserved = {ident: {key: self.raw_record(ident)[key] for key in (
            "id", "ai_json", "current_json", "status", "confirmed_at", "confirmed_by",
            "confirmed_by_name", "recognition_confirmed_version", "image_path", "attachments")}
            for ident in (20, 21, 22)}
        report = retire_source_pages(self.engine, self.upload_dir, apply=True)

        self.assertTrue(report["applied"])
        self.assertEqual(report["source_ids"], [10, 11, 12])
        self.assertTrue(Path(report["backup"]).is_file())
        self.assertEqual([row.id for row in self.rows()], [20, 21, 22])
        for ident in (20, 21, 22):
            after = self.raw_record(ident)
            expected = dict(preserved[ident], source_parent_id=None)
            for key, value in expected.items():
                self.assertEqual(after[key], value, f"voucher {ident} field {key} changed")

        with self.engine.connect() as connection:
            pages = {row["id"]: dict(row) for row in connection.exec_driver_sql(
                "SELECT * FROM import_pages").mappings()}
        self.assertIsNone(pages["page-1"]["source_page_id"])
        self.assertEqual(json.loads(pages["page-1"]["voucher_ids_json"]), [20, 21])
        self.assertIsNone(pages["page-2"]["source_page_id"])
        self.assertEqual(json.loads(pages["page-2"]["voucher_ids_json"]), [22])
        self.assertEqual(json.loads(pages["page-4"]["voucher_ids_json"]), [])
        self.assertEqual(pages["page-4"]["status"], "completed")
        self.assertEqual(pages["page-4"]["phase"], "discarded")
        self.assertEqual(json.loads(pages["page-3"]["voucher_ids_json"]), [])
        self.assertEqual(pages["page-3"]["status"], "completed")
        self.assertEqual(pages["page-3"]["phase"], "discarded")

        self.assertTrue((self.upload_dir / "pages/shared.jpg").exists())
        self.assertTrue((self.upload_dir / "pages/shared-attachment.jpg").exists())
        for relative in ("pages/shared.baseline.jpg", "pages/shared.deskew.jpg", "pages/shared.orig.jpg",
                         "pages/shared-attachment.baseline.jpg", "pages/shared-attachment.deskew.jpg",
                         "pages/shared-attachment.orig.jpg"):
            self.assertTrue((self.upload_dir / relative).exists(), relative)
        self.assertTrue((self.upload_dir / "pages/pdf-render.jpg").exists())
        self.assertFalse((self.upload_dir / "pages/retired.jpg").exists())
        for suffix in ("baseline", "deskew", "orig"):
            self.assertFalse((self.upload_dir / "pages" / f"retired.{suffix}.jpg").exists())
        self.assertTrue(external.exists())
        self.assertTrue(any("不在憑證影像清理範圍" in error for error in report["cleanup_failed"]))
        self.assertGreaterEqual(report["files_retained"], 2)

        with closing(sqlite3.connect(report["backup"])) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            backup_sources = backup.execute(
                "SELECT id FROM voucher_records WHERE record_role='source_page' ORDER BY id").fetchall()
            self.assertEqual(backup_sources, [(10,), (11,), (12,)])
            self.assertEqual(backup.execute(
                "SELECT ai_json,current_json,status,confirmed_by_name FROM voucher_records WHERE id=20").fetchone(),
                (preserved[20]["ai_json"], preserved[20]["current_json"], "confirmed", "Synthetic reviewer"))

        second = retire_source_pages(self.engine, self.upload_dir, apply=True)
        self.assertEqual(second["source_ids"], [])
        self.assertFalse(second["applied"])
        self.assertEqual([row.id for row in self.rows()], [20, 21, 22])

    def test_orphan_source_page_is_removed_without_touching_real_voucher_ids(self):
        token = "e" * 32
        image = "pages/orphan.jpg"
        self.write_page_file(image)
        self.add_record(30, role="source_page", image=image,
                        current={"origin": {"source_token": token}})
        self.add_record(35, current={"fields": {"total_amount": 25}})

        report = retire_source_pages(self.engine, self.upload_dir, apply=True)

        self.assertEqual(report["source_ids"], [30])
        self.assertEqual([row.id for row in self.rows()], [35])
        self.assertFalse((self.upload_dir / image).exists())
        self.assertEqual(report["files_removed"], 4)

    def test_foreign_key_errors_and_source_content_abort_without_mutation(self):
        token = "f" * 32
        image = "pages/unsafe.jpg"
        self.write_page_file(image)
        self.add_record(40, role="source_page", image=image,
                        current={"origin": {"source_token": token}})
        self.add_record(41, parent=40, current={"origin": {"source_token": token}})
        before = {row.id: self.raw_record(row.id) for row in self.rows()}
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("UPDATE voucher_records SET source_parent_id=999 WHERE id=41")
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        damaged = {row.id: self.raw_record(row.id) for row in self.rows()}

        with self.assertRaisesRegex(RuntimeError, "外鍵錯誤"):
            retire_source_pages(self.engine, self.upload_dir, apply=True)
        self.assertEqual({row.id: self.raw_record(row.id) for row in self.rows()}, damaged)
        self.assertTrue((self.upload_dir / image).exists())

        # Restore the valid reference, then make the source itself contain recognized fields.
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE voucher_records SET source_parent_id=40 WHERE id=41")
            connection.exec_driver_sql(
                "UPDATE voucher_records SET current_json=? WHERE id=40",
                (json.dumps({"fields": {"total_amount": 1}, "origin": {"source_token": token}}),))
        changed = {row.id: self.raw_record(row.id) for row in self.rows()}
        with self.assertRaisesRegex(ValueError, "未預期的單據內容"):
            retire_source_pages(self.engine, self.upload_dir, apply=True)
        self.assertEqual({row.id: self.raw_record(row.id) for row in self.rows()}, changed)
        self.assertTrue((self.upload_dir / image).exists())
        backups = list((self.db_path.parent / "backups").glob("*.db"))
        self.assertEqual(backups, [])

    def test_delete_trigger_failure_rolls_back_updates_and_keeps_backup_readable(self):
        token = "9" * 32
        image = "pages/rollback.jpg"
        self.write_page_file(image)
        self.add_record(50, role="source_page", image=image,
                        current={"origin": {"source_token": token}})
        self.add_record(51, parent=50, image="pages/child.jpg", status=ReviewStatus.pending,
                        current={"fields": {"total_amount": 51}, "origin": {
                            "import_page_id": "page-9", "import_job_id": "job-a",
                            "source_token": token}}, ai={"model": "rollback-synthetic"})
        self.add_page("page-9", token, source_page_id=50)
        before_records = {row.id: self.raw_record(row.id) for row in self.rows()}
        with self.engine.connect() as connection:
            before_page = dict(connection.exec_driver_sql(
                "SELECT * FROM import_pages WHERE id='page-9'").mappings().one())
            connection.exec_driver_sql("""
                CREATE TRIGGER fail_source_page_delete
                BEFORE DELETE ON voucher_records
                WHEN OLD.record_role='source_page'
                BEGIN
                    SELECT RAISE(ABORT, 'synthetic delete interruption');
                END
            """)
            connection.commit()

        with self.assertRaises(SQLAlchemyError) as raised:
            retire_source_pages(self.engine, self.upload_dir, apply=True)

        self.assertIn("synthetic delete interruption", str(raised.exception))
        self.assertEqual({row.id: self.raw_record(row.id) for row in self.rows()}, before_records)
        self.assertTrue((self.upload_dir / image).is_file())
        with self.engine.connect() as connection:
            after_page = dict(connection.exec_driver_sql(
                "SELECT * FROM import_pages WHERE id='page-9'").mappings().one())
        self.assertEqual(after_page, before_page)

        backups = list((self.db_path.parent / "backups").glob("*_before_source_page_retirement_*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            rows = backup.execute(
                "SELECT id,record_role,source_parent_id,status FROM voucher_records ORDER BY id").fetchall()
            self.assertEqual(rows, [(50, "source_page", None, "prepared"),
                                   (51, "voucher", 50, "pending")])
            self.assertEqual(backup.execute(
                "SELECT source_page_id,voucher_ids_json,status,phase FROM import_pages WHERE id='page-9'"
            ).fetchone(), (50, "[]", "preview_ready", "preview_ready"))


if __name__ == "__main__":
    unittest.main()
