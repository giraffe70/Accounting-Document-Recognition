"""Contract tests for the production voucher confirmation UI flow."""
from __future__ import annotations

import json
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
APP_HTML = ROOT / "app" / "static" / "index.html"
NODE = Path(r"C:\Program Files\nodejs\node.exe")

NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  function collectNotes()');
const end = source.indexOf('  $("btnConfirm").addEventListener("click"', start);
if (start < 0 || end < 0) throw new Error('could not locate production confirmation functions');
const code = source.slice(start, end);
const events = [];
const nodes = {
  'f-correction_note': { value: '人工依原圖修正' },
  'f-manual_entry': { checked: true },
  'devNoteWrap': { classList: { contains: () => true } },
  'f-dev_note': { value: '' },
  'revCompanySel': { value: 'company-1' },
  'btnSave': { disabled: false, addEventListener: () => {} },
  'btnConfirm': { disabled: false, addEventListener: () => {} },
  'btnReturnImport': { disabled: false, addEventListener: () => {} },
  'dirInput': { value: 'directory-selection' },
};
let currentId = input.currentId ?? 12;
let currentVoucher = { fields: { amount: 125 }, status: 'pending' };
let editBusy = false;
let inferBusy = 0;
const fileInput = { value: 'selected-file' };
const progressText = { textContent: '' };
const progress = { classList: { remove: () => {} } };
const barFill = { style: {} };
function $(id) { return nodes[id] || null; }
function collectForm() { return { fields: { amount: 125 }, entries: [] }; }
function json() { return { 'Content-Type': 'application/json' }; }
function applyRecordStatus(record) { events.push({ type: 'status', id: record.id, status: record.status }); }
function renderWarnings() { events.push({ type: 'renderWarnings' }); }
function clearReview() { events.push({ type: 'clearReview' }); currentId = null; }
function setStage(stage) { events.push({ type: 'setStage', stage }); }
function loadList() { events.push({ type: 'loadList' }); }
function alert(message) { events.push({ type: 'alert', message }); }
function toast(message) { events.push({ type: 'toast', message }); }

function response(ok, status, body) {
  return { ok, status, json: async () => body };
}
async function fetch(url, options = {}) {
  const parsed = options.body ? JSON.parse(options.body) : null;
  events.push({ type: 'fetch', url, method: options.method || 'GET', body: parsed });
  const path = url.endsWith('/company') ? 'company' :
    url.endsWith('/notes') ? 'notes' :
    url.endsWith('/confirm') ? 'confirm' :
    options.method === 'PUT' ? 'put' : 'other';
  if (path === input.failAt) return response(false, input.failStatus || 500, { detail: `${path} failed` });
  if (path === 'company') return response(true, 200, { id: 12, status: 'pending', updated_at: 'company-ts' });
  if (path === 'put') return response(true, 200, {
    id: 12, status: 'pending', updated_at: 'put-ts', current: currentVoucher,
  });
  if (path === 'notes') {
    if (input.switchCurrentOnNotes) currentId = input.switchCurrentOnNotes;
    return response(true, 200, { id: 12, status: 'pending', updated_at: 'notes-ts' });
  }
  if (path === 'confirm') {
    const status = input.confirmStatus || 200;
    return response(status >= 200 && status < 300, status,
      status === 200 ? { id: 12, status: 'confirmed', updated_at: 'confirm-ts' } : { detail: 'stale record' });
  }
  throw new Error(`unexpected request ${options.method || 'GET'} ${url}`);
}

const context = {
  console, events, nodes, input, get currentId() { return currentId; },
  set currentId(value) { currentId = value; }, currentVoucher, editBusy, inferBusy,
  fileInput, progressText, progress, barFill, $, collectForm, json, applyRecordStatus,
  renderWarnings, clearReview, setStage, loadList, alert, toast, fetch,
};
vm.createContext(context);
vm.runInContext(`${code}\nglobalThis.api = { confirmCurrent, saveDraft };`, context);
(async () => {
  if (input.action === 'confirm') await vm.runInContext('confirmCurrent()', context);
  else await vm.runInContext(`saveDraft(${input.returnToImport ? 'true' : 'false'})`, context);
  process.stdout.write(JSON.stringify({
    events,
    fileValue: context.fileInput.value,
    dirValue: context.nodes.dirInput.value,
    currentId: vm.runInContext('currentId', context),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


TRACE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  const EMPTY_LIST_TEXT');
const end = source.indexOf('  // ── 清單 ──', start);
if (start < 0 || end < 0) throw new Error('could not locate source page functions');
const esc = (s) => (s == null ? "" : String(s));
const context = { esc, JSON };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { queueViewModel, sourceHintHtml, sourceTrace };', context);
const out = {};
if (input.rows) out.view = context.api.queueViewModel(input.rows, input.stage);
if (input.hint) out.hint = context.api.sourceHintHtml(context.api.sourceTrace(input.hint.row, 0), input.hint.autoSplit);
process.stdout.write(JSON.stringify(out));
"""


class WorkflowMarkupParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs: list[tuple[str, str]] = []
        self.options: list[tuple[str, str]] = []
        self._in_stage_tab: str | None = None
        self._in_confirmation_filter = False
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "stage-tab" in attrs.get("class", "").split():
            self._in_stage_tab = attrs.get("data-stage", "")
        elif tag == "select" and attrs.get("id") == "confirmationFilter":
            self._in_confirmation_filter = True
        elif tag == "option" and self._in_confirmation_filter:
            self._option_value = attrs.get("value", "")
            self._option_text = []

    def handle_data(self, data):
        if self._in_stage_tab is not None:
            self.tabs.append((self._in_stage_tab, data.strip()))
            self._in_stage_tab = None
        if self._option_value is not None:
            self._option_text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "option" and self._option_value is not None:
            self.options.append((self._option_value, "".join(self._option_text)))
            self._option_value = None
        elif tag == "select" and self._in_confirmation_filter:
            self._in_confirmation_filter = False


class ConfirmationFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")
        if not NODE.is_file():
            raise unittest.SkipTest(f"Node.js not found at {NODE}")

    def run_flow(self, **scenario):
        payload = {"source": self.source, **scenario}
        completed = subprocess.run(
            [str(NODE), "-e", NODE_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    @staticmethod
    def requests(result):
        return [event for event in result["events"] if event["type"] == "fetch"]

    @staticmethod
    def event_types(result):
        return [event["type"] for event in result["events"]]

    def test_confirm_persists_company_content_notes_then_uses_notes_timestamp(self):
        result = self.run_flow(action="confirm")

        requests = self.requests(result)
        self.assertEqual([request["url"].rsplit("/", 1)[-1] for request in requests], [
            "company", "12", "notes", "confirm",
        ])
        self.assertEqual([request["method"] for request in requests], ["POST", "PUT", "POST", "POST"])
        self.assertEqual(requests[-1]["body"], {"expected_updated_at": "notes-ts"})
        self.assertIn("已確認", " ".join(e["message"] for e in result["events"] if e["type"] == "toast"))

    def test_confirm_does_not_run_when_any_persist_step_fails(self):
        for failed_step, expected_count in (("company", 1), ("put", 2), ("notes", 3)):
            with self.subTest(failed_step=failed_step):
                result = self.run_flow(action="confirm", failAt=failed_step)
                self.assertEqual(len(self.requests(result)), expected_count)
                self.assertNotIn("confirm", [r["url"].rsplit("/", 1)[-1] for r in self.requests(result)])
                self.assertFalse(any(e["type"] == "toast" for e in result["events"]))
                self.assertTrue(any(e["type"] == "alert" for e in result["events"]))

    def test_confirm_conflict_is_not_reported_as_success(self):
        result = self.run_flow(action="confirm", confirmStatus=409)

        self.assertEqual(self.requests(result)[-1]["url"], "/api/vouchers/12/confirm")
        self.assertFalse(any(e["type"] == "toast" and "已確認" in e["message"] for e in result["events"]))
        self.assertTrue(any(e["type"] == "alert" and "確認失敗" in e["message"] for e in result["events"]))

    def test_draft_and_return_to_import_never_call_confirm(self):
        draft = self.run_flow(action="draft")
        self.assertEqual(len(self.requests(draft)), 3)
        self.assertFalse(any(request["url"].endswith("/confirm") for request in self.requests(draft)))
        self.assertFalse(any(event["type"] in {"clearReview", "setStage"} for event in draft["events"]))

        returned = self.run_flow(action="draft", returnToImport=True)
        self.assertEqual(len(self.requests(returned)), 3)
        self.assertFalse(any(request["url"].endswith("/confirm") for request in self.requests(returned)))
        self.assertLess(self.event_types(returned).index("clearReview"), self.event_types(returned).index("setStage"))
        self.assertIn({"type": "setStage", "stage": "import"}, returned["events"])
        self.assertEqual(returned["fileValue"], "")
        self.assertEqual(returned["dirValue"], "")

    def test_return_to_import_waits_for_all_saves_and_same_record(self):
        for failed_step in ("company", "put", "notes"):
            with self.subTest(failed_step=failed_step):
                result = self.run_flow(action="draft", returnToImport=True, failAt=failed_step)
                self.assertFalse(any(e["type"] in {"clearReview", "setStage"} for e in result["events"]))
                self.assertEqual(result["fileValue"], "selected-file")
                self.assertEqual(result["dirValue"], "directory-selection")
                self.assertTrue(any(e["type"] == "alert" for e in result["events"]))

        switched = self.run_flow(action="draft", returnToImport=True, switchCurrentOnNotes=99)
        self.assertFalse(any(e["type"] in {"clearReview", "setStage"} for e in switched["events"]))
        self.assertEqual(switched["fileValue"], "selected-file")

    def test_confirmation_tabs_and_filter_options_are_present(self):
        parser = WorkflowMarkupParser()
        parser.feed(APP_HTML.read_text(encoding="utf-8"))

        self.assertEqual([stage for stage, _label in parser.tabs], ["import", "prepared", "queued", "pending"])
        self.assertEqual(parser.options, [
            ("pending", "待確認"), ("confirmed", "已確認"), ("pending,confirmed", "全部"),
        ])

    def run_trace(self, rows, stage="prepared", hint=None):
        payload = {"source": self.source, "rows": rows, "stage": stage, "hint": hint}
        completed = subprocess.run(
            [str(NODE), "-e", TRACE_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    @staticmethod
    def source_row(row_id, content_kind, auto_split, regions=0, **extra):
        layout = {"content_kind": content_kind, "auto_split": auto_split, "regions": [{}] * regions}
        return {"id": row_id, "record_role": "source_page", "source_file": f"scan.pdf p{row_id}",
                "origin": {"page_no": row_id, "layout": layout}, **extra}

    def test_queue_hides_split_and_skipped_source_pages_but_keeps_manual_ones_actionable(self):
        rows = [
            self.source_row(1, "document", True, regions=1),
            {"id": 2, "record_role": "voucher", "source_parent_id": 1, "origin": {"page_no": 1}},
            self.source_row(3, "blank", True),
            self.source_row(4, "document", False),
        ]
        result = self.run_trace(rows)["view"]

        # Split and blank source pages are not shown; a page the system could not split stays reachable.
        self.assertEqual([r["id"] for r in result["vouchers"]], [2, 4])
        self.assertEqual(result["hidden"], 2)
        self.assertEqual(result["skipped"], 1)

        later = self.run_trace(rows, stage="queued")["view"]
        self.assertEqual([r["id"] for r in later["vouchers"]], [2])

    def test_blank_only_import_shows_skipped_notice_instead_of_source_rows(self):
        result = self.run_trace([self.source_row(7, "blank", True)])["view"]

        self.assertEqual(result["vouchers"], [])
        self.assertIn("已自動略過 1 個非單據頁", result["emptyText"])
        self.assertEqual(self.run_trace([])["view"]["emptyText"], "此階段目前沒有憑證。")

    def test_source_hint_matches_page_role_without_pointing_to_missing_documents(self):
        hint = self.run_trace(None, hint={"row": self.source_row(1, "blank", True), "autoSplit": True})["hint"]
        self.assertIn("已略過", hint)
        self.assertIn("空白頁", hint)
        self.assertNotIn("拆分出的單據", hint)

        hint = self.run_trace(None, hint={"row": self.source_row(2, "document", True, regions=1), "autoSplit": True})["hint"]
        self.assertIn("單張完整單據", hint)
        self.assertIn("不需重複處理", hint)

        hint = self.run_trace(None, hint={"row": self.source_row(3, "document", False), "autoSplit": False})["hint"]
        self.assertIn("裁切為新憑證", hint)

    def test_source_pages_are_not_rendered_as_a_separate_frontend_area(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertNotIn("追溯資料", html)
        self.assertNotIn("traceBox", html)
        self.assertNotIn("原始頁／追溯資料", self.source)


if __name__ == "__main__":
    unittest.main()
