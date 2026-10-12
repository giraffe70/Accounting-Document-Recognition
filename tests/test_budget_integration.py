"""SDK I/O replacements and persistent parent recovery, without model/network use."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image
from langchain_ollama import ChatOllama
from ollama import ChatResponse, Message, ResponseError
from app import config, validation
from app.core import invocation_trace, model_coordinates, model_invocation, pipeline, prompts, recognition_checkpoint, request_budget


class BudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(config, "OLLAMA_NUM_CTX", 8192)
        context.start()
        self.addCleanup(context.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=config.DATA_DIR / "test-tmp-plan")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.profile = {"profile_id":"offline", "processor":model_coordinates.CHECKED_PROCESSORS["0.35.0"]}
        self.tokenizer = SimpleNamespace(encode=lambda text, **kw:SimpleNamespace(ids=list(range(len(text)//3))))

    def test_sdk_context_failure_keeps_first_read_manual_value_and_raw_evidence(self):
        image = self.folder / "source.png"
        Image.new("RGB",(1653,2339),"white").save(image)
        raw = prompts.extraction_example("invoice")
        raw["fields"].update(total_amount=500,seller_name="合成商店")
        response = ChatResponse(model="offline",message=Message(role="assistant",content=json.dumps(raw,ensure_ascii=False)),done=True)
        chat = ChatOllama(model="offline",base_url="http://127.0.0.1:1",num_ctx=8192)
        with (patch.object(request_budget,"runtime_profile",return_value=(self.profile,self.tokenizer)),
              patch("app.core.settings_store.get_settings",return_value={"ocr":"disabled/none","structure":"fake/offline"}),
              patch("app.core.barcode.decode_diagnostics",return_value={"candidates":[]}),
              patch.object(chat._client,"chat",autospec=True,side_effect=[iter([response]), ResponseError("exceeds the available context size",status_code=400)]) as sdk,
              invocation_trace.capture(self.folder / "trace.json") as trace):
            result = pipeline.extract_evidence(str(image),"invoice",chat,[],"",confirmed_fields={"total_amount":499})
        self.assertEqual(sdk.call_count,2)
        self.assertEqual(result.total_amount,499)
        self.assertEqual(result.initial_ai["fields"]["total_amount"],500)
        self.assertEqual(result.quality["state"],"needs_review")
        self.assertIn("supplement_failed",[i["code"] for i in result.quality["issues"]])
        self.assertNotIn("base64",(self.folder / "trace.json").read_text(encoding="utf-8"))
        checkpoint = json.loads((self.folder / "first_read_checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["manual_provenance"]["fields"]["total_amount"],499)
        self.assertEqual(len([r for r in trace if r.get("kind")=="model_call"]),2)
        for record in result.recognition_diagnostics["requests"]:
            self.assertLessEqual(record["estimated_input_tokens"]+record["output_limit"]+512,8192)

    def test_parent_checkpoint_recovery_is_cancel_safe_and_contract_valid(self):
        directory = self.folder / "run" / "doc"
        directory.mkdir(parents=True)
        source = directory / "original.png"
        Image.new("RGB",(100,100),"white").save(source)
        raw = prompts.extraction_example("invoice")
        raw["fields"]["total_amount"] = 0
        raw["box_issues"] = []
        voucher = pipeline._build_voucher(raw,"invoice")
        with patch.object(model_invocation,"source_versions",return_value={"execution_version_sha256":"version"}), invocation_trace.capture(directory / "trace.json"):
            checkpoint = recognition_checkpoint.save(source,source,voucher,raw)
            recognition_checkpoint.mark_supplement(checkpoint)
        payload = {"run_id":"run","id":"doc","phase":"document","page_path":str(source),"execution_version_sha256":"version","combo":{"mode":"ocr_vlm"}}
        con = Mock()
        con.__enter__ = Mock(return_value=con)
        con.__exit__ = Mock(return_value=False)
        con.execute.return_value.fetchone.return_value = {"status":"running","cancel_requested":0}
        with (patch.object(validation,"RUNS",self.folder),patch.object(validation,"_connect",return_value=con),
              patch.object(validation,"runtime_status",return_value={"ready":True}),
              patch.object(model_invocation,"source_versions",return_value={"execution_version_sha256":"version"})):
            restored = validation._recover_first_read(payload,{"ok":False,"timeout":True,"error":"timeout","wall_seconds":15})
            self.assertTrue(restored["ok"])
            self.assertTrue(validation._document_contract(restored["data"]))
            self.assertEqual(restored["data"]["voucher"]["total_amount"],0)
            self.assertFalse(restored["timeout"])
            con.execute.return_value.fetchone.return_value = {"status":"running","cancel_requested":1}
            self.assertFalse(validation._recover_first_read(payload,{"ok":False,"error":"cancel"})["ok"])
            con.execute.return_value.fetchone.return_value = {"status":"running","cancel_requested":0}
            self.assertFalse(validation._recover_first_read({**payload,"execution_version_sha256":"wrong"},{"ok":False})["ok"])
            self.assertFalse(validation._recover_first_read(payload,{"ok":False,"cancelled":True})["ok"])

    def test_repair_rebudgets_fragment_and_context_400_never_downgrades(self):
        chat = ChatOllama(model="offline",base_url="http://127.0.0.1:1",num_ctx=8192)
        calls = []
        def invoke(_chat,messages):
            calls.append(messages)
            return '{"bad":true}' if len(calls)==1 else '{"ok":true}'
        def validate(obj):
            if "ok" not in obj:
                raise ValueError("requires ok")
            return obj
        with patch.object(request_budget,"runtime_profile",return_value=(self.profile,self.tokenizer)), invocation_trace.capture() as trace:
            model_invocation.invoke_structured(chat,"中文原文 123",validate,invoke=invoke,schema={"type":"object"})
        budgets = [r for r in trace if r.get("kind")=="request_budget"]
        self.assertEqual([b["round"] for b in budgets],[1,2])
        self.assertNotEqual(budgets[0]["input_sha256"],budgets[1]["input_sha256"])
        with (patch.object(request_budget,"runtime_profile",return_value=(self.profile,self.tokenizer)),
              patch.object(chat._client,"chat",autospec=True,side_effect=ResponseError("exceed_context_size_error",status_code=400)) as sdk):
            with self.assertRaises(ResponseError):
                model_invocation.invoke_structured(chat,"合成",lambda obj:obj,schema={"type":"object"})
        self.assertEqual(sdk.call_count,1)

    def test_schema_downgrade_is_budgeted_again(self):
        chat = ChatOllama(model="offline",base_url="http://127.0.0.1:1",num_ctx=8192)
        response = ChatResponse(model="offline",message=Message(role="assistant",content='{"ok":true}'),done=True)
        with (patch.object(request_budget,"runtime_profile",return_value=(self.profile,self.tokenizer)),
              patch.object(chat._client,"chat",autospec=True,side_effect=[ResponseError("unsupported format",status_code=400),iter([response])]),
              invocation_trace.capture() as trace):
            model_invocation.invoke_structured(chat,"合成",lambda obj:obj,schema={"type":"object"})
        budgets = [r for r in trace if r.get("kind")=="request_budget"]
        self.assertEqual([b["output_mode"] for b in budgets],["schema","json"])
        self.assertGreater(budgets[0]["schema_tokens"],budgets[1]["schema_tokens"])


if __name__ == "__main__":
    unittest.main()
