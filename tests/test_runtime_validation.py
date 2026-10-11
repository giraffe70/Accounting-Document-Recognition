import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from app import validation


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"app/data/test-tmp-plan")
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(validation, k, v) for k,v in {
            "ROOT":self.root, "DB":self.root/"validation.db", "RUNS":self.root/"runs",
            "DATASETS":self.root/"datasets", "_scheduler_thread":Mock(is_alive=lambda:True)}.items()]
        for p in self.patches:
            p.start()
        validation.init_validation()
        self.versions = validation._versioning.source_versions()
        settings = {**self.versions, "worker_protocol":validation.WORKER_PROTOCOL, "parent_build":validation.LOADED_BUILD}
        with validation._connect() as con:
            con.execute("INSERT INTO datasets(id,name,created_at) VALUES('dataset','test','now')")
            con.execute("INSERT INTO pages(id,dataset_id,voucher_key,relative_path,sha256,page_no,original_path,page_path) VALUES('page','dataset','p','test.png','hash',1,'test.png',?)", (str(self.root/"datasets/test.png"),))
            con.execute("INSERT INTO runs(id,dataset_id,name,status,created_at,settings_json) VALUES('run','dataset','test','completed','now',?)", (json.dumps(settings),))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def payload(self):
        return {"id":"a", "run_id":"run", "worker_protocol":validation.WORKER_PROTOCOL,
                "parent_build":validation.LOADED_BUILD, "execution_version_sha256":self.versions["execution_version_sha256"]}

    def result(self, identifier, attempt, status, *, repeat=1, warmup=0, contract=2, path=""):
        page = {"result_contract_version":contract, "layout":{"type":"receipt"}, "quality":{"state":"usable"}}
        with validation._connect() as con:
            con.execute("INSERT INTO results(id,run_id,page_id,combo_id,combo_name,mode,repeat_no,attempt,warmup,status,queued_at,actual_path,page_json,timings_json) VALUES(?, 'run','page','combo','test','ocr_vlm',?,?,?,?, 'now',?,?,?)",
                (identifier,repeat,attempt,warmup,status,path,json.dumps(page),json.dumps({"complete_seconds":10})))
            if status == "succeeded":
                document = {"voucher":{"doc_type_key":"receipt", "fields":{"total_amount":500}, "items":[], "entries":[],
                    "total_amount":500, "quality":{"version":1, "state":"usable", "issues":[]}}, "timings":{"complete_seconds":3}}
                con.execute("INSERT INTO document_results VALUES(?,?,0,'succeeded',?,'')", (identifier+"-doc",identifier,json.dumps(document)))

    def test_loaded_disk_version_and_schema_guard(self):
        self.assertTrue(validation.runtime_status()["ready"])
        with patch.object(validation, "LOADED_BUILD", "old"):
            runtime = validation.runtime_status()
            self.assertTrue(runtime["restart_required"])
            self.assertFalse(runtime["ready"])
        with validation._connect() as con:
            con.execute("DROP TABLE document_results")
        self.assertFalse(validation.runtime_status()["schema_ready"])
        with validation._connect() as con:
            with self.assertRaisesRegex(RuntimeError, "migration missing"):
                validation._documents(con,"a")

    def test_handshake_blocks_missing_old_parent_and_changed_batch_before_model(self):
        pipe = Mock()
        with patch.object(validation, "_recognize") as recognize:
            validation._child_entry(pipe, {"id":"a","run_id":"run"})
            recognize.assert_not_called()
        self.assertFalse(pipe.send.call_args[0][0]["ok"])
        validation._handshake(self.payload())
        for key,value in (("parent_build","old"),("worker_protocol",1),("execution_version_sha256","changed")):
            with self.assertRaises(ValueError):
                validation._handshake({**self.payload(),key:value})
        with patch.object(validation, "_recognize", side_effect=lambda _: setattr(validation,"LOADED_BUILD","changed")), patch.object(validation,"LOADED_BUILD",validation.LOADED_BUILD):
            validation._child_entry(pipe,self.payload())
            self.assertFalse(pipe.send.call_args[0][0]["ok"])

    def test_additive_legacy_migration_keeps_terminal_history(self):
        self.result("legacy",1,"failed",contract=None)
        with validation._connect() as con:
            before = tuple(con.execute("SELECT id,status,error,voucher_json FROM results WHERE id='legacy'").fetchone())
            con.execute("DROP TABLE document_results")
            con.execute("ALTER TABLE results DROP COLUMN page_json")
            con.execute("PRAGMA user_version=0")
        validation.init_validation()
        with validation._connect() as con:
            self.assertTrue(validation._schema_ready(con))
            self.assertEqual(before,tuple(con.execute("SELECT id,status,error,voucher_json FROM results WHERE id='legacy'").fetchone()))

    def test_latest_selection_precedes_status_paging_and_exports(self):
        self.result("old-fail",1,"failed")
        self.result("new-success",2,"succeeded",path="ocr_vlm→vlm_direct")
        self.result("repeat-fail",3,"failed",repeat=2)
        self.result("repeat-new",4,"failed",repeat=2)
        self.result("warm",9,"failed",warmup=1)
        rows = validation.list_results("run", status="failed",page=1,page_size=1,view="latest")
        self.assertEqual(rows["total"],1)
        self.assertEqual(rows["items"][0]["id"],"repeat-new")
        run = validation.get_run("run")
        self.assertEqual(run["latest_summary"]["total"],2)
        self.assertEqual(run["latest_statistics"][0]["samples"],2)
        self.assertEqual(run["latest_statistics"][0]["failed"],1)
        data = validation._export_data("run","latest")
        files = validation._csv_files(data)
        pages = list(csv.DictReader(files["pages.csv"].lstrip("\ufeff").splitlines()))
        self.assertEqual({r["result_id"] for r in pages},{"new-success","repeat-new"})
        self.assertTrue(all(r["is_latest"] == "True" for r in pages))
        history = validation._export_data("run")
        self.assertEqual(len(history["results"]),5)
        self.assertEqual(sum(r["is_latest"] for r in history["results"]),2)
        validation.retry_run("run")
        with validation._connect() as con:
            retries = con.execute("SELECT page_id,repeat_no,attempt FROM results WHERE status='queued'").fetchall()
        self.assertEqual([tuple(r) for r in retries],[("page",2,5)])

    def test_old_contract_rate_unreliable_and_diagnostics_are_read_only(self):
        self.result("old",1,"succeeded",contract=None)
        with validation._connect() as con:
            con.execute("DELETE FROM document_results")
        directory = self.root/"runs/run/old"
        directory.mkdir(parents=True)
        (directory/"documents.json").write_text(json.dumps([{"voucher":{"total_amount":999}}]),encoding="utf-8")
        before = validation.get_run("run")
        self.assertIsNone(before["latest_statistics"][0]["completed_rate"])
        diag = validation.get_diagnostics("old")
        self.assertFalse(diag["trusted"])
        self.assertEqual(diag["documents"][0]["voucher"]["total_amount"],999)
        self.assertEqual(validation.get_run("run"),before)
        with validation._connect() as con:
            con.execute("UPDATE results SET result_dir=? WHERE id='old'", (str(self.root.parent),))
        self.assertEqual(validation.get_diagnostics("old").status_code,409)

    def test_empty_success_rejected_and_pending_page_stays_in_denominator(self):
        self.result("empty",1,"running")
        task = {**self.payload(),"id":"empty", "queued_at":None,"started_at":None}
        validation._save_response(task,{"ok":True,"data":{"page_status":"succeeded","documents":[]}})
        with validation._connect() as con:
            row = dict(con.execute("SELECT * FROM results WHERE id='empty'").fetchone())
        self.assertEqual(row["status"],"failed")
        self.assertIn("契約不完整",row["error"])
        self.assertFalse(validation._integrity(row,[])["ok"])
        self.assertTrue((self.root/"runs/run/empty/trace.json").is_file())
        self.result("good",2,"succeeded")
        self.result("pending",1,"queued",repeat=2)
        stat = validation.get_run("run")["latest_statistics"][0]
        self.assertEqual(stat["selected_samples"],2)
        self.assertEqual(stat["completed_rate"],.5)

    def test_malformed_child_preserves_diagnostic_and_other_document(self):
        from PIL import Image
        image = self.root/"page.png"
        Image.new("RGB", (600,800), "white").save(image)
        regions = [{"type":"receipt","rotate":0,"box":box} for box in
                   ([0,0,450,1000], [550,0,1000,1000])]
        valid = {"doc_type_key":"receipt", "fields":{"total_amount":0}, "items":[], "entries":[],
                 "quality":{"version":1,"state":"usable","issues":[]}}
        def runner(payload,_timeout):
            if payload["phase"] == "layout":
                return {"ok":True,"data":{"layout":{"content_kind":"document","auto_split":True,"regions":regions}}}
            return {"ok":True,"data":{"voucher":"invalid shape" if payload["origin"]["region"][0] == 0 else valid}}
        payload = {**self.payload(), "page_path":str(image), "page_no":1, "combo":{"mode":"ocr_vlm"}}
        data = validation._run_page_child(payload,30,runner)["data"]
        self.assertEqual(data["page_status"],"partial")
        self.assertEqual([d["status"] for d in data["documents"]],["failed","succeeded"])
        self.assertEqual(data["documents"][0]["unvalidated_voucher"],"invalid shape")
        self.assertEqual(data["documents"][1]["voucher"]["fields"]["total_amount"],0)
        self.assertTrue(Path(data["documents"][0]["artifacts"]["trace_json"]["path"]).is_file())
        self.result("partial",1,"running")
        task = {**self.payload(),"id":"partial", "queued_at":None,"started_at":None}
        validation._save_response(task,{"ok":True,"data":{"page_status":"partial","documents":[]}})
        with validation._connect() as con:
            self.assertEqual(con.execute("SELECT status FROM results WHERE id='partial'").fetchone()[0],"failed")


if __name__ == "__main__":
    unittest.main()
