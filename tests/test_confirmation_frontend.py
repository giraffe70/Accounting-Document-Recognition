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
const start = source.indexOf('  async function responseError(');
const end = source.indexOf('  const json = () =>', start);
if (start < 0 || end < 0) throw new Error('could not locate production confirmation functions');
const code = source.slice(start, end);
const events = [];
const nodes = {
  'revCompanySel': { value: '1' },
  'itemsBody': { querySelectorAll: () => [] },
  'itemsSection': { classList: { toggle: () => {} } },
  'revForm': { querySelectorAll: () => [] },
  'btnSave': { disabled: false, addEventListener: () => {} },
  'btnConfirm': { disabled: false, addEventListener: () => {} },
  'btnReturnImport': { disabled: false, addEventListener: () => {} },
  'dirInput': { value: 'directory-selection' },
};
let currentId = input.currentId ?? 12;
let currentVoucher = { fields: { amount: 125 }, status: 'pending' };
let currentVersion = 'original-ts';
let currentEntryStatus = 'none', currentProposal = {}, currentEntryChecks = {};
let reviewedItems = input.reviewedItems || {};
let saveCalls = 0;
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
function renderEntries(entries = [], debitTotal = null, creditTotal = null, balanceCheck = false) {
  events.push({ type: 'renderEntries', count: entries.length, debitTotal, creditTotal, balanceCheck });
}
function applyEntryState(record) {
  currentEntryStatus = record.entries_status || 'none';
  currentProposal = record.entries_proposal || {};
  currentEntryChecks = record.entries_checks || {};
  events.push({ type: 'entryState', status: currentEntryStatus, proposal: currentProposal.status || null });
}
async function refreshItemMatch() {}
function selectedCompanyId() { return 1; }
function renderItems() { events.push({ type: 'renderItems' }); }
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
  if (!url.endsWith('/save')) throw new Error(`unexpected request ${options.method || 'GET'} ${url}`);
  const status = input.saveStatuses?.[saveCalls] || input.saveStatus || 200;
  saveCalls += 1;
  if (status === 409 && input.latest) return response(false, 409, { latest: input.latest });
  if (status < 200 || status >= 300) return response(false, status, { detail: 'stale record' });
  return response(true, status, { id: 12, status: parsed.action === 'confirm' ? 'confirmed' : 'pending',
    updated_at: 'saved-ts', current: currentVoucher });
}

const context = {
  console, events, nodes, input, get currentId() { return currentId; },
  set currentId(value) { currentId = value; }, currentVoucher, currentVersion,
  DOC_TYPES: { other: { has_items: false } },
  reviewedItems, editBusy, inferBusy,
  fileInput, progressText, progress, barFill, $, collectForm, json, applyRecordStatus, renderEntries, applyEntryState,
  refreshItemMatch, selectedCompanyId, renderItems,
  clearReview, setStage, loadList, alert, toast, fetch,
};
vm.createContext(context);
vm.runInContext(`${code}
globalThis.reviewLatestVoucher = async (latest) => {
  events.push({ type: 'reviewLatestVoucher', latest });
  return !!input.acceptLatest;
};
globalThis.api = { confirmCurrent, saveDraft, persistCurrentEdits };`, context);
(async () => {
  if (input.action === 'confirm') await vm.runInContext('confirmCurrent()', context);
  else if (input.action === 'temporary') await vm.runInContext('persistCurrentEdits()', context);
  else await vm.runInContext(`saveDraft(${input.returnToImport ? 'true' : 'false'})`, context);
  process.stdout.write(JSON.stringify({
    events,
    fileValue: context.fileInput.value,
    dirValue: context.nodes.dirInput.value,
    currentId: vm.runInContext('currentId', context),
    currentVersion: vm.runInContext('currentVersion', context),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


REVIEW_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const openStart = source.indexOf('  async function openReview(id)');
const openEnd = source.indexOf('  // 多頁憑證：主頁＋附頁縮圖切換', openStart);
const clearStart = source.indexOf('  function clearReview()', openEnd);
const clearEnd = source.indexOf('  // 外觀確認 → 加入掃描清單', clearStart);
const roleStart = source.indexOf('  function applyRole()', clearEnd);
const roleEnd = source.indexOf('  async function enterApp()', roleStart);
const recognizeStart = source.indexOf('  async function runRecognize()', openEnd);
const recognizeEnd = source.indexOf('  function buildActiveFields(', recognizeStart);
if ([openStart, openEnd, clearStart, clearEnd, roleStart, roleEnd, recognizeStart, recognizeEnd].some((p) => p < 0))
  throw new Error('could not locate review/role/recognize functions');
const events = [], requests = [];
const removed = new Set([
  'voucherWarnings', 'f-manual_entry', 'f-correction_note', 'f-dev_note', 'devNoteWrap', 'revSrc',
  'feedbackBtn', 'feedbackModal', 'closeFeedback', 'feedbackBody', 'feedbackEmpty',
  'backupBtn', 'backupModal', 'closeBackup', 'backupNow', 'backupBody', 'backupEmpty',
  'sysconfigBtn', 'sysconfigModal', 'closeSysconfig', 'cfg-ollama_base_url', 'cfg-h200_base_url',
  'cfg-google_api_key', 'cfg-openai_api_key', 'cfg-anthropic_api_key', 'cfg-type', 'cfg-host',
  'cfg-path', 'cfg-username', 'cfg-password', 'cfg-note', 'cfg-provider', 'cfg-region',
  'cfg-bucket', 'cfg-access_key', 'cfg-secret_key', 'cfg-permission', 'cfg-cloud_note',
  'reportBtn', 'reportModal', 'closeReport', 'reportTemplate', 'reportPeriod', 'reportCompany',
  'reportGen', 'reportCancel',
]);
const nodes = {};
function node(id) {
  if (removed.has(id)) return null;
  if (!nodes[id]) nodes[id] = {
    id, value: '', checked: false, disabled: false, textContent: '', innerHTML: '', className: '',
    classList: { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, force) { if (force) this._set.add(c); else this._set.delete(c); }, contains(c) { return this._set.has(c); } },
  };
  return nodes[id];
}
const $ = node;
let currentId = null, currentVoucher = {}, currentIsImport = false, editBusy = false, inferBusy = 0, cropping = false, stageAction = '';
let currentManualEntry = false;
let currentEntryStatus = 'none', currentProposal = {}, currentEntryChecks = {};
const currentUser = { role: input.role || 'clerk' };
const fileInput = { value: 'selected-file' }, progressText = { textContent: '' }, progress = { classList: { remove() {} } }, barFill = { style: {} };
function setCropMode() {}
function setStatus() {}
function setPendingActionVisibility() {}
function applyEntryState(record) {
  currentEntryStatus = record.entries_status || 'none';
  currentProposal = record.entries_proposal || {};
  currentEntryChecks = record.entries_checks || {};
  events.push({ type: 'entryState', status: currentEntryStatus, proposal: currentProposal.status || null });
}
function populateTypeSel() {}
function buildActiveFields() {}
function renderFields() {}
function renderItems() {}
function renderTaxcheck() {}
function renderDatecheck() {}
function renderAmountcheck() {}
function renderEntries() {}
function setupImage() {}
function renderPages() {}
function sourceHintHtml() { return ''; }
function sourceTrace() { return {}; }
function esc(s) { return s == null ? '' : String(s); }
function loadList() { events.push({ type: 'loadList' }); }
function alert(message) { events.push({ type: 'alert', message }); }
function toast(message) { events.push({ type: 'toast', message }); }
function confirm(message) { events.push({ type: 'confirm', message }); return !!input.acceptConfirm; }
function json() { return { 'Content-Type': 'application/json' }; }
async function persistCurrentEdits() {
  events.push({ type: 'persist' });
  if (input.persistFails) throw new Error('save failed');
}
async function withInferProgress(_label, operation) { return operation(); }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  if ((options.method || 'GET') === 'GET') return { ok: true, json: async () => input.records[url.split('/').pop()] };
  if (url.endsWith('/recognize')) return { ok: true, json: async () => ({ ok: true }) };
  if (url.endsWith('/set-type')) return { ok: true, json: async () => ({ ok: true }) };
  throw new Error(`unexpected request ${options.method} ${url}`);
}
const context = { console, events, requests, input, nodes, $, currentUser, fileInput, progressText, progress, barFill,
  get currentId() { return currentId; }, set currentId(v) { currentId = v; },
  get currentVoucher() { return currentVoucher; }, set currentVoucher(v) { currentVoucher = v; },
  get currentManualEntry() { return currentManualEntry; }, set currentManualEntry(v) { currentManualEntry = v; },
  get currentIsImport() { return currentIsImport; }, set currentIsImport(v) { currentIsImport = v; },
  editBusy, inferBusy, cropping, stageAction, currentVersion: '', reviewedItems: { stale: true },
  currentEntryStatus, currentProposal, currentEntryChecks, applyEntryState,
  DOC_TYPES: { other: { fields: [] } },
  async loadItemAccountChart() { events.push({ type: 'loadItemAccountChart', company: node('revCompanySel').value }); },
  setCropMode, setStatus, setPendingActionVisibility, populateTypeSel, buildActiveFields, renderFields, renderItems,
  renderTaxcheck, renderDatecheck, renderAmountcheck, renderEntries, setupImage, renderPages, sourceHintHtml, sourceTrace,
  esc, loadList, alert, toast, confirm, json, persistCurrentEdits, withInferProgress, fetch };
vm.createContext(context);
const reviewCode = source.slice(openStart, openEnd) + '\n' + source.slice(clearStart, clearEnd) + '\n' + source.slice(roleStart, roleEnd) + '\n' + source.slice(recognizeStart, recognizeEnd);
vm.runInContext(reviewCode + '\nglobalThis.api = { openReview, clearReview, applyRole, runRecognize };', context);
(async () => {
  const out = {};
  if (input.action === 'open') {
    await context.api.openReview(input.recordId);
    out.currentId = currentId;
    out.manualEntry = currentManualEntry;
    out.currentVersion = context.currentVersion;
    out.reviewedItems = context.reviewedItems;
    out.absentNodes = [...removed].every((id) => !nodes[id]);
  } else if (input.action === 'role') {
    context.api.applyRole();
    out.visible = Object.fromEntries(['accountsBtn', 'databaseBtn'].map((id) => [id, !node(id).classList.contains('hidden')]));
  } else if (input.action === 'recognize') {
    await context.api.openReview(input.recordId);
    if (input.clearBetween) {
      context.api.clearReview();
      out.manualEntryAfterClear = currentManualEntry;
      await context.api.openReview(input.nextRecordId);
    }
    await context.api.runRecognize();
    out.currentId = currentId;
  }
  out.events = events; out.requests = requests;
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


HASH_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  function openFromHash()');
const end = source.indexOf('  window.addEventListener("hashchange", openFromHash)', start);
if (start < 0 || end < 0) throw new Error('could not locate hash routing function');
const events = [];
const location = { hash: '' };
function openJournal() { events.push({ handler: 'journal' }); }
function openAccounts() { events.push({ handler: 'accounts' }); }
function openReview(id) { events.push({ handler: 'review', id }); }
const context = { events, location, openJournal, openAccounts, openReview };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { openFromHash };', context);
for (const hash of input.hashes) { location.hash = hash; context.api.openFromHash(); }
process.stdout.write(JSON.stringify(events));
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


DELETE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  function isAdminUser()');
const end = source.indexOf('  $("btnDelete").addEventListener', start);
if (start < 0 || end < 0) throw new Error('could not locate delete functions');
const events = [], requests = [];
const classes = {};
const node = (id) => (classes[id] = classes[id] || {
  id, innerHTML: '', disabled: false,
  classList: { _set: new Set(['hidden']), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); }, contains(c) { return this._set.has(c); } },
});
const checked = { value: 'record' };
const document = {
  querySelector: () => (input.scope ? { value: input.scope } : checked),
  querySelectorAll: () => [],
};
let currentId = 12, editBusy = false;
const currentUser = { role: 'admin' };
const esc = (s) => (s == null ? '' : String(s));
const errText = (d, fallback) => (d && d.detail) || fallback;
const $ = (id) => node(id);
function alert(message) { events.push({ type: 'alert', message }); }
function toast(message) { events.push({ type: 'toast', message }); }
function clearReview() { events.push({ type: 'clearReview' }); currentId = null; }
function loadList() { events.push({ type: 'loadList' }); }
function refreshStats() { events.push({ type: 'refreshStats' }); }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  if (options.method === 'DELETE') {
    const status = input.deleteStatus || 200;
    return { ok: status < 300, status, json: async () => input.deleteBody || { ok: true, deleted_ids: [12] } };
  }
  return { ok: true, status: 200, json: async () => input.data };
}
const context = { events, requests, document, $, esc, errText, alert, toast, clearReview, loadList, refreshStats, fetch, input,
  get currentId() { return currentId; }, set currentId(v) { currentId = v; }, editBusy, currentUser, console };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { deleteChoices, openDelete, confirmDelete, closeDeleteModal };', context);
(async () => {
  const out = {};
  if (input.action === 'choices') out.choices = context.api.deleteChoices(input.data);
  else {
    await context.api.openDelete();
    if (input.action === 'cancel') context.api.closeDeleteModal();
    if (input.action === 'confirm') await context.api.confirmDelete();
  }
  out.events = events; out.requests = requests;
  out.modalOpen = !node('deleteModal').classList.contains('hidden');
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
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

    def run_review(self, **scenario):
        payload = {"source": self.source, **scenario}
        completed = subprocess.run(
            [str(NODE), "-e", REVIEW_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    def run_hashes(self, hashes):
        payload = {"source": self.source, "hashes": hashes}
        completed = subprocess.run(
            [str(NODE), "-e", HASH_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    @staticmethod
    def review_record(manual_entry=False):
        return {"id": 12, "status": "pending", "manual_entry": manual_entry, "company_id": 3,
                "updated_at": "version-12", "current": {"fields": {}, "items": [], "entries": []}}

    @staticmethod
    def requests(result):
        return [event for event in result["events"] if event["type"] == "fetch"]

    @staticmethod
    def event_types(result):
        return [event["type"] for event in result["events"]]

    def test_confirm_uses_one_atomic_save_with_confirm_action(self):
        result = self.run_flow(action="confirm")

        requests = self.requests(result)
        self.assertEqual([request["url"] for request in requests], ["/api/vouchers/12/save"])
        self.assertEqual([request["method"] for request in requests], ["POST"])
        self.assertEqual(requests[0]["body"]["action"], "confirm")
        self.assertEqual(requests[0]["body"]["company_id"], 1)
        self.assertEqual(requests[0]["body"]["expected_updated_at"], "original-ts")
        self.assertIn("已確認", " ".join(e["message"] for e in result["events"] if e["type"] == "toast"))

    def test_save_return_and_default_temporary_send_the_right_action(self):
        saved = self.run_flow(action="draft")
        returned = self.run_flow(action="draft", returnToImport=True)
        temporary = self.run_flow(action="temporary")
        self.assertEqual(saved["events"][0]["body"]["action"], "save")
        self.assertEqual(returned["events"][0]["body"]["action"], "save")
        self.assertEqual(temporary["events"][0]["body"]["action"], "temporary")
        self.assertEqual(len(self.requests(saved)), 1)
        self.assertFalse(any(event["type"] in {"clearReview", "setStage"} for event in saved["events"]))
        self.assertLess(self.event_types(returned).index("clearReview"), self.event_types(returned).index("setStage"))
        self.assertEqual(returned["fileValue"], "")
        self.assertEqual(returned["dirValue"], "")

    def test_failed_save_and_conflict_keep_review_input_open(self):
        for status in (500, 409):
            with self.subTest(status=status):
                result = self.run_flow(action="draft", returnToImport=True, saveStatus=status)
                self.assertEqual(len(self.requests(result)), 1)
                self.assertFalse(any(e["type"] in {"clearReview", "setStage"} for e in result["events"]))
                self.assertEqual(result["fileValue"], "selected-file")
                self.assertEqual(result["dirValue"], "directory-selection")
                self.assertEqual(result["currentId"], 12)
                self.assertTrue(any(e["type"] == "alert" and "儲存失敗" in e["message"] for e in result["events"]))

    def test_latest_version_conflict_cancel_keeps_inputs_and_does_not_retry(self):
        latest = {"updated_at": "latest-ts", "company_id": 1,
                  "current": {"fields": {"amount": 200}, "items": []}}
        result = self.run_flow(action="draft", returnToImport=True,
                               saveStatuses=[409], latest=latest, acceptLatest=False)

        requests = self.requests(result)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["body"]["expected_updated_at"], "original-ts")
        self.assertEqual(result["currentId"], 12)
        self.assertEqual(result["currentVersion"], "original-ts")
        self.assertEqual(result["fileValue"], "selected-file")
        self.assertEqual(result["dirValue"], "directory-selection")
        self.assertFalse(any(e["type"] in {"clearReview", "setStage"} for e in result["events"]))

    def test_latest_version_conflict_accept_retries_same_local_payload_at_latest_version(self):
        latest = {"updated_at": "latest-ts", "company_id": 1,
                  "current": {"fields": {"amount": 200}, "items": []}}
        result = self.run_flow(action="draft", saveStatuses=[409, 200], latest=latest, acceptLatest=True)

        requests = self.requests(result)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["currentVersion"], "latest-ts")
        self.assertEqual(requests[0]["body"]["expected_updated_at"], "original-ts")
        self.assertEqual(requests[1]["body"]["expected_updated_at"], "latest-ts")
        first_payload, retry_payload = requests[0]["body"], requests[1]["body"]
        self.assertEqual(first_payload["action"], retry_payload["action"])
        self.assertEqual(first_payload["company_id"], retry_payload["company_id"])
        self.assertEqual(first_payload["voucher"], retry_payload["voucher"])
        self.assertEqual(first_payload["reviewed_items"], retry_payload["reviewed_items"])

    def test_review_opens_and_role_visibility_works_without_removed_elements(self):
        opened = self.run_review(action="open", recordId=12, records={"12": self.review_record(True)})
        self.assertEqual(opened["currentId"], 12)
        self.assertTrue(opened["manualEntry"])
        self.assertEqual(opened["currentVersion"], "version-12")
        self.assertEqual(opened["reviewedItems"], {})
        chart_loads = [e for e in opened["events"] if e["type"] == "loadItemAccountChart"]
        self.assertEqual(chart_loads, [{"type": "loadItemAccountChart", "company": "3"}])
        self.assertTrue(opened["absentNodes"])

        clerk = self.run_review(action="role", role="clerk")
        self.assertFalse(any(clerk["visible"].values()))
        accountant = self.run_review(action="role", role="accountant")
        self.assertFalse(any(accountant["visible"].values()))
        admin = self.run_review(action="role", role="admin")
        self.assertTrue(all(admin["visible"].values()))

    def test_removed_sysconfig_hash_is_noop_and_supported_hashes_still_route(self):
        events = self.run_hashes(["#sysconfig", "#journal", "#accounts", "#v=47"])

        self.assertEqual(events, [
            {"handler": "journal"},
            {"handler": "accounts"},
            {"handler": "review", "id": 47},
        ])

    def test_re_recognition_manual_entry_prompt_cancel_and_accept(self):
        records = {"12": self.review_record(True)}
        cancelled = self.run_review(action="recognize", recordId=12, records=records, acceptConfirm=False)
        self.assertEqual([e["type"] for e in cancelled["events"]],
                         ["loadItemAccountChart", "entryState", "loadList", "confirm"])
        self.assertEqual([r["method"] for r in cancelled["requests"]], ["GET"])

        accepted = self.run_review(action="recognize", recordId=12, records=records, acceptConfirm=True)
        self.assertIn("confirm", [e["type"] for e in accepted["events"]])
        self.assertIn("persist", [e["type"] for e in accepted["events"]])
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in accepted["requests"]], ["12", "set-type", "recognize", "12"])
        self.assertFalse(any("/notes" in r["url"] for r in accepted["requests"]))

    def test_re_recognition_skips_prompt_for_unmarked_record_and_stops_after_save_failure(self):
        records = {"12": self.review_record(False)}
        ordinary = self.run_review(action="recognize", recordId=12, records=records)
        self.assertNotIn("confirm", [e["type"] for e in ordinary["events"]])
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in ordinary["requests"]], ["12", "set-type", "recognize", "12"])

        failed = self.run_review(action="recognize", recordId=12, records=records, persistFails=True)
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in failed["requests"]], ["12"])
        self.assertIn("persist", [e["type"] for e in failed["events"]])
        self.assertFalse(any(r["url"].endswith("/set-type") or r["url"].endswith("/recognize") for r in failed["requests"]))
        self.assertTrue(any(e["type"] == "alert" and "辨識失敗" in e["message"] for e in failed["events"]))

    def test_clearing_review_resets_manual_entry_before_loading_next_record(self):
        records = {"12": self.review_record(True), "13": self.review_record(False)}
        result = self.run_review(action="recognize", recordId=12, records=records, clearBetween=True, nextRecordId=13)
        self.assertFalse(result["manualEntryAfterClear"])
        self.assertNotIn("confirm", [e["type"] for e in result["events"]])
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in result["requests"]], ["12", "13", "set-type", "recognize", "13"])

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

    def run_delete(self, action, **scenario):
        payload = {"source": self.source, "action": action, **scenario}
        completed = subprocess.run(
            [str(NODE), "-e", DELETE_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    @staticmethod
    def preview(ids, **counts):
        return {"ids": ids, "voucher_count": counts.get("vouchers", len(ids)), "source_page_count": counts.get("sources", 0),
                "confirmed_count": counts.get("confirmed", 0), "attachment_count": counts.get("attachments", 0),
                "requires_admin": bool(counts.get("confirmed", 0))}

    def test_delete_choices_describe_scope_and_hide_upload_when_identical(self):
        record = self.preview([5])
        upload = self.preview([3, 4, 5], vouchers=2, sources=1, attachments=1)
        data = {"previews": {"record": record, "upload": upload}, "can_delete": {"record": True, "upload": True}}
        choices = self.run_delete("choices", data=data)["choices"]
        self.assertEqual([c["scope"] for c in choices], ["record", "upload"])
        self.assertEqual(choices[0]["summary"], "1 張單據")
        self.assertEqual(choices[1]["summary"], "2 張單據、1 個原始掃描頁（含 1 張附頁）")
        self.assertTrue(all(c["allowed"] and not c["warning"] for c in choices))

        same = {"previews": {"record": record, "upload": self.preview([5])}, "can_delete": {"record": True, "upload": True}}
        self.assertEqual([c["scope"] for c in self.run_delete("choices", data=same)["choices"]], ["record"])

    def test_delete_choices_warn_about_confirmed_and_block_non_admins(self):
        record = self.preview([5])
        upload = self.preview([3, 5], confirmed=1)
        clerk = {"previews": {"record": record, "upload": upload}, "can_delete": {"record": True, "upload": False}}
        choices = self.run_delete("choices", data=clerk)["choices"]
        self.assertTrue(choices[0]["allowed"])
        self.assertFalse(choices[1]["allowed"])
        self.assertIn("只有管理員", choices[1]["warning"])
        self.assertIn("其中 1 張已確認", choices[1]["summary"])

        admin = {"previews": {"record": record, "upload": upload}, "can_delete": {"record": True, "upload": True}}
        warning = self.run_delete("choices", data=admin)["choices"][1]["warning"]
        self.assertIn("日記帳與報表", warning)

    def test_delete_flow_cancel_failure_and_success(self):
        data = {"previews": {"record": self.preview([12]), "upload": self.preview([11, 12], sources=1)},
                "can_delete": {"record": True, "upload": True}, "busy": False, "ok": True}
        opened = self.run_delete("open", data=data)
        self.assertEqual([r["method"] for r in opened["requests"]], ["GET"])
        self.assertTrue(opened["modalOpen"])

        cancelled = self.run_delete("cancel", data=data)
        self.assertFalse(cancelled["modalOpen"])
        self.assertFalse(any(r["method"] == "DELETE" for r in cancelled["requests"]))

        busy = self.run_delete("open", data={**data, "busy": True})
        self.assertFalse(busy["modalOpen"])
        self.assertTrue(any("進行中" in e["message"] for e in busy["events"] if e["type"] == "alert"))

        failed = self.run_delete("confirm", data=data, deleteStatus=403, deleteBody={"ok": False, "detail": "只有管理員"})
        self.assertEqual([r["method"] for r in failed["requests"]], ["GET", "DELETE"])
        self.assertEqual(failed["requests"][-1]["url"], "/api/vouchers/12?scope=record")
        self.assertTrue(any(e["type"] == "alert" and "只有管理員" in e["message"] for e in failed["events"]))
        self.assertFalse(any(e["type"] in {"clearReview", "loadList", "toast"} for e in failed["events"]))
        self.assertTrue(failed["modalOpen"])

        done = self.run_delete("confirm", data=data, scope="upload",
                               deleteBody={"ok": True, "deleted_ids": [11, 12], "cleanup_failed": ["locked"]})
        self.assertEqual(done["requests"][-1]["url"], "/api/vouchers/12?scope=upload")
        types = [e["type"] for e in done["events"]]
        self.assertIn("clearReview", types)
        self.assertIn("loadList", types)
        self.assertIn("toast", types)
        self.assertTrue(any(e["type"] == "alert" and "locked" in e["message"] for e in done["events"]))
        self.assertFalse(done["modalOpen"])

    def test_delete_button_and_modal_markup_exist(self):
        html = APP_HTML.read_text(encoding="utf-8")
        for marker in ('id="btnDelete"', 'id="deleteModal"', 'id="deleteChoices"', 'id="deleteConfirm"'):
            self.assertIn(marker, html)
        tag_start = html.index('id="btnDelete"')
        self.assertIn('class="btn btn-sm danger"', html[tag_start:html.index(">", tag_start)])
        self.assertIn('status === "confirmed" && !isAdminUser()', self.source)

    def test_folder_import_and_scan_source_directory_are_hidden(self):
        html = APP_HTML.read_text(encoding="utf-8")

        folder_start = html.index('<label class="btn btn-sm folder-btn')
        folder_tag = html[folder_start:html.index(">", folder_start)]
        self.assertIn("hidden", folder_tag.split('"')[1].split())

        panel_start = html.index('id="scanSourcePanel"')
        panel_tag = html[html.rindex("<div", 0, panel_start):html.index(">", panel_start)]
        self.assertIn("hidden", panel_tag)

        # 一般拖拉／檔案選擇匯入仍可使用。
        for marker in ('id="dropZone"', 'id="fileInput"'):
            self.assertIn(marker, html)

    def test_source_pages_are_not_rendered_as_a_separate_frontend_area(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertNotIn("追溯資料", html)
        self.assertNotIn("traceBox", html)
        self.assertNotIn("原始頁／追溯資料", self.source)


if __name__ == "__main__":
    unittest.main()
