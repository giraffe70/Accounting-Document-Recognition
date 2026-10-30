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
const start = source.indexOf('  async function persistCurrentEdits(');
const end = source.indexOf('  const json = () =>', start);
if (start < 0 || end < 0) throw new Error('could not locate production confirmation functions');
const code = source.slice(start, end);
const events = [];
const nodes = {
  'revCompanySel': { value: '1' },
  'itemsBody': { querySelectorAll: () => [] },
  'itemsSection': { classList: { toggle: () => {}, remove: () => {} } },
  'revForm': { querySelectorAll: () => [] },
  'btnSave': { disabled: false, addEventListener: () => {} },
  'btnConfirm': { disabled: false, addEventListener: () => {} },
  'btnReturnImport': { disabled: false, addEventListener: () => {} },
};
let currentId = input.currentId ?? 12;
let currentVoucher = { fields: { amount: 125 }, items: [], entries: [{ id: 'untouched-entry' }], status: 'pending' };
let currentVersion = 'original-ts';
let recognitionReady = !!input.recognitionReady, recognitionDirty = !!input.recognitionDirty;
let reviewedItems = input.reviewedItems || {};
let saveCalls = 0;
let editBusy = false;
let inferBusy = 0;
const fileInput = { value: 'selected-file' };
const progressText = { textContent: '' };
const progress = { classList: { remove: () => {} } };
const barFill = { style: {} };
function $(id) { return nodes[id] || null; }
function collectForm() { return { ...currentVoucher, fields: { amount: 125 } }; }
function json() { return { 'Content-Type': 'application/json' }; }
function updateRecognitionState() { events.push({ type: 'recognitionState', ready: recognitionReady, dirty: recognitionDirty }); }
function applyRecognitionState(record) {
  recognitionReady = !!record.recognition_report_ready;
  recognitionDirty = false;
  updateRecognitionState();
}
function applyRecordStatus(record) {
  events.push({ type: 'status', id: record.id, status: record.status });
  if (record.updated_at) currentVersion = record.updated_at;
  applyRecognitionState(record);
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
    recognition_report_ready: parsed.action === 'confirm', updated_at: 'saved-ts', current: currentVoucher });
}

const context = {
  console, events, nodes, input, get currentId() { return currentId; },
  set currentId(value) { currentId = value; }, currentVoucher, currentVersion,
  DOC_TYPES: { other: { has_items: false } },
  reviewedItems, editBusy, inferBusy,
  recognitionReady, recognitionDirty, fileInput, progressText, progress, barFill, $, collectForm, json,
  applyRecordStatus, applyRecognitionState, updateRecognitionState, errText: (record, fallback) => record?.detail || fallback,
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
    currentId: vm.runInContext('currentId', context),
    currentVersion: vm.runInContext('currentVersion', context),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


PRINT_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const stateStart = source.indexOf('  function updateRecognitionState()');
const stateEnd = source.indexOf('  function setStatus(', stateStart);
const printStart = source.indexOf('  async function printVoucher()', stateEnd);
const printEnd = source.indexOf('  $("btnPrintVoucher").addEventListener', printStart);
if ([stateStart, stateEnd, printStart, printEnd].some((p) => p < 0)) throw new Error('could not locate recognition/print functions');
const events = [], requests = [], formHandlers = {};
const nodes = {
  btnPrintVoucher: { disabled: false, addEventListener() {} },
  recognitionStatus: { textContent: '', className: '' },
  detailWarnings: { textContent: '', classList: { toggle() {} } },
  revForm: { addEventListener(type, handler) { formHandlers[type] = handler; } },
};
function $(id) { return nodes[id] || null; }
let currentId = 12, currentVersion = 'version-ready';
let recognitionReady = false, recognitionDirty = false, editBusy = false;
const preview = { document: { write(html) { events.push({ type: 'write', html }); }, close() { events.push({ type: 'close' }); } } };
const window = { open() { events.push({ type: 'open' }); return preview; } };
function alert(message) { events.push({ type: 'alert', message }); }
function errText(error, fallback) { return error?.detail || fallback; }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  return { ok: true, text: async () => '<html>recognition report</html>' };
}
const context = { console, input, events, requests, nodes, $, window, alert, errText, fetch,
  currentId, currentVersion, recognitionReady, recognitionDirty, editBusy };
vm.createContext(context);
vm.runInContext(source.slice(stateStart, stateEnd) + '\n' + source.slice(printStart, printEnd) +
  '\nglobalThis.api = { applyRecognitionState, printVoucher };', context);
(async () => {
  context.api.applyRecognitionState({ recognition_report_ready: !!input.ready });
  if (input.event) formHandlers[input.event]({ target: nodes.revForm });
  const buttonDisabledAfterEdit = nodes.btnPrintVoucher.disabled;
  await context.api.printVoucher();
  process.stdout.write(JSON.stringify({ events, requests, buttonDisabledAfterEdit,
    ready: vm.runInContext('recognitionReady', context), dirty: vm.runInContext('recognitionDirty', context) }));
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
const companyTaxStart = source.indexOf('  function companyTaxIdText(company)');
const companyTaxEnd = source.indexOf('\n  $("uploadCompany")', companyTaxStart);
if ([openStart, openEnd, clearStart, clearEnd, roleStart, roleEnd, recognizeStart, recognizeEnd, companyTaxStart, companyTaxEnd].some((p) => p < 0))
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
const currentUser = { is_admin: !!input.is_admin };
const companyRecords = input.companies || [{ id: 3, name: '甲公司', short: '甲公司', tax_id: '12345678' }];
const fileInput = { value: 'selected-file' }, progressText = { textContent: '' }, progress = { classList: { remove() {} } }, barFill = { style: {} };
function setCropMode() {}
function resetFineTool() {}
const restoreCalls = [];
function updateRestoreButton(available) { restoreCalls.push(available); }
function setStatus() {}
function setPendingActionVisibility() {}
function updateRecognitionState() {}
function applyRecognitionState(record) {
  events.push({ type: 'recognitionState', ready: !!record.recognition_report_ready });
  updateRecognitionState();
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
  get currentIsImport() { return currentIsImport; }, set currentIsImport(v) { currentIsImport = v; },
  editBusy, inferBusy, cropping, stageAction, currentVersion: '', reviewedItems: { stale: true },
  updateRecognitionState, applyRecognitionState,
  DOC_TYPES: { other: { fields: [] } }, companyRecords,
  setCropMode, resetFineTool, updateRestoreButton, setStatus, setPendingActionVisibility, populateTypeSel, buildActiveFields, renderFields, renderItems,
  renderTaxcheck, renderDatecheck, renderAmountcheck, renderEntries, setupImage, renderPages,
  esc, loadList, alert, toast, confirm, json, persistCurrentEdits, withInferProgress, fetch };
vm.createContext(context);
const reviewCode = source.slice(openStart, openEnd) + '\n' + source.slice(clearStart, clearEnd) + '\n' + source.slice(roleStart, roleEnd) + '\n' + source.slice(recognizeStart, recognizeEnd) + '\n' + source.slice(companyTaxStart, companyTaxEnd);
vm.runInContext(reviewCode + '\nglobalThis.api = { openReview, clearReview, applyRole, runRecognize };', context);
(async () => {
  const out = {};
  if (input.action === 'open') {
    await context.api.openReview(input.recordId);
    out.currentId = currentId;
    out.currentVersion = context.currentVersion;
    out.reviewedItems = context.reviewedItems;
    out.absentNodes = [...removed].every((id) => !nodes[id]);
    out.restoreCalls = restoreCalls;
    out.hiddenTools = Object.fromEntries(['btnReRecognize', 'btnRestore'].map((id) => [id, node(id).classList.contains('hidden')]));
    out.confirmDisabled = node('btnConfirm').disabled;
    out.reviewBodyHidden = node('reviewBody').classList.contains('hidden');
    out.detailsHidden = node('recognitionDetails').classList.contains('hidden');
    out.imageToolsLocked = !!context.imageToolsLocked;
  } else if (input.action === 'role') {
    context.api.applyRole();
    out.visible = Object.fromEntries(['databaseBtn', 'usersBtn', 'settingsBtn'].map((id) => [id, !node(id).classList.contains('hidden')]));
  } else if (input.action === 'recognize') {
    await context.api.openReview(input.recordId);
    if (input.clearBetween) {
      context.api.clearReview();
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
function openReview(id) { events.push({ handler: 'review', id }); }
const context = { events, location, openReview };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { openFromHash };', context);
for (const hash of input.hashes) { location.hash = hash; context.api.openFromHash(); }
process.stdout.write(JSON.stringify(events));
"""


DELETE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
input.previewResolvers = [];
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
let currentId = 12, currentVersion = input.currentVersion || 'opened-version', currentStage = input.currentStage || 'pending', editBusy = false;
const currentUser = { is_admin: true };
const esc = (s) => (s == null ? '' : String(s));
const errText = (d, fallback) => (d && d.detail) || fallback;
const $ = (id) => node(id);
function alert(message) { events.push({ type: 'alert', message }); }
function toast(message) { events.push({ type: 'toast', message }); }
function clearReview() { events.push({ type: 'clearReview', editBusy: runtimeContext.editBusy }); currentId = null; }
function loadList() { events.push({ type: 'loadList' }); }
function refreshStats() { events.push({ type: 'refreshStats' }); }
async function restoreImportJobs() { events.push({ type: 'restoreImportJobs', editBusy: runtimeContext.editBusy }); }
let runtimeContext;
let previewRequest = 0;
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  if (options.method === 'DELETE') {
    events.push({ type: 'deleteStarted', editBusy: runtimeContext.editBusy, cancelDisabled: node('deleteCancel').disabled,
      confirmDisabled: node('deleteConfirm').disabled });
    const status = input.deleteStatus || 200;
    return { ok: status < 300, status, json: async () => input.deleteBody || { ok: true, deleted_ids: [12] } };
  }
  const previewIndex = previewRequest++;
  if (input.deferPreview) await new Promise((resolve) => { input.previewResolvers[previewIndex] = resolve; });
  return { ok: true, status: 200, json: async () => (input.previewData && input.previewData[previewIndex]) || input.data };
}
const context = { events, requests, document, $, esc, errText, alert, toast, clearReview, loadList, refreshStats, fetch, input,
  restoreImportJobs, get currentId() { return currentId; }, set currentId(v) { currentId = v; },
  get currentVersion() { return currentVersion; }, set currentVersion(v) { currentVersion = v; },
  get currentStage() { return currentStage; }, set currentStage(v) { currentStage = v; }, editBusy, currentUser, console };
runtimeContext = context;
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { deleteChoices, openDelete, confirmDelete, closeDeleteModal };', context);
(async () => {
  const out = {};
  if (input.action === 'choices') out.choices = context.api.deleteChoices(input.data);
  else if (input.action === 'cancelDuringPreview' || input.action === 'changeDuringPreview') {
    const pending = context.api.openDelete();
    if (input.action === 'cancelDuringPreview') context.api.closeDeleteModal();
    else {
      context.currentId = input.nextId || 13;
      context.currentVersion = input.nextVersion || 'new-version';
      context.currentStage = input.nextStage || currentStage;
    }
    input.previewResolvers[0]();
    await pending;
  } else if (input.action === 'supersedePreview') {
    const older = context.api.openDelete();
    const newer = context.api.openDelete();
    input.previewResolvers[1]();
    await newer;
    input.previewResolvers[0]();
    await older;
  }
  else {
    await context.api.openDelete();
    if (input.afterOpenVersion) context.currentVersion = input.afterOpenVersion;
    if (input.action === 'cancel') context.api.closeDeleteModal();
    if (input.action === 'confirm') await context.api.confirmDelete();
    if (input.action === 'confirmTwice') await Promise.all([context.api.confirmDelete(), context.api.confirmDelete()]);
  }
  out.events = events; out.requests = requests; out.choicesHtml = node('deleteChoices').innerHTML; out.currentId = currentId;
  out.currentStage = currentStage;
  out.editBusy = editBusy; out.cancelDisabled = node('deleteCancel').disabled; out.confirmDisabled = node('deleteConfirm').disabled;
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

    def run_print(self, **scenario):
        payload = {"source": self.source, **scenario}
        completed = subprocess.run(
            [str(NODE), "-e", PRINT_HARNESS],
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
    def review_record():
        return {"id": 12, "status": "pending", "company_id": 3,
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
        self.assertEqual(requests[0]["body"]["voucher"]["entries"], [{"id": "untouched-entry"}])
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

    def test_failed_save_and_conflict_keep_review_input_open(self):
        for status in (500, 409):
            with self.subTest(status=status):
                result = self.run_flow(action="draft", returnToImport=True, saveStatus=status)
                self.assertEqual(len(self.requests(result)), 1)
                self.assertFalse(any(e["type"] in {"clearReview", "setStage"} for e in result["events"]))
                self.assertEqual(result["fileValue"], "selected-file")
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

    def test_review_opens_and_admin_visibility_works_without_removed_elements(self):
        opened = self.run_review(action="open", recordId=12, records={"12": self.review_record()})
        self.assertEqual(opened["currentId"], 12)
        self.assertEqual(opened["currentVersion"], "version-12")
        self.assertEqual(opened["reviewedItems"], {})
        self.assertFalse(any(e["type"] == "loadItemAccountChart" for e in opened["events"]))
        self.assertTrue(opened["absentNodes"])

        regular = self.run_review(action="role", is_admin=False)
        self.assertFalse(any(regular["visible"].values()))
        admin = self.run_review(action="role", is_admin=True)
        self.assertTrue(all(admin["visible"].values()))

    def test_appearance_stage_swaps_re_recognize_for_restore(self):
        prepared = {**self.review_record(), "status": "prepared", "image_restore_available": True}
        opened = self.run_review(action="open", recordId=12, records={"12": prepared})
        self.assertEqual(opened["hiddenTools"], {"btnReRecognize": True, "btnRestore": False})
        self.assertEqual(opened["restoreCalls"], [True])

        queued = self.run_review(action="open", recordId=12, records={"12": {**prepared, "status": "queued"}})
        self.assertEqual(queued["hiddenTools"], {"btnReRecognize": True, "btnRestore": True})

        pending = self.run_review(action="open", recordId=12, records={"12": self.review_record()})
        self.assertEqual(pending["hiddenTools"], {"btnReRecognize": False, "btnRestore": True})

    def test_appearance_confirmation_does_not_require_direction_checkbox(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertNotIn('id="directionConfirmed"', html)
        self.assertNotIn("我已確認目前影像方向正確", html)
        self.assertNotIn('id="manualPreparation"', html)

        start = self.source.index("  async function confirmAppearance()")
        end = self.source.index('  $("btnReRecognize").addEventListener', start)
        appearance = self.source[start:end]
        self.assertNotIn("directionConfirmed", appearance)
        self.assertNotIn("direction_confirmed", appearance)
        self.assertIn('expected_updated_at:currentVersion', appearance)
        self.assertIn('document_mode:"single"', appearance)
        self.assertNotIn("請先確認目前影像方向正確", self.source)

    def test_recognition_timing_row_is_not_rendered(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertNotIn('id="fastTiming"', html)
        self.assertNotIn("fastTiming", self.source)
        self.assertNotIn("recognition_diagnostics?.timings", self.source)

    def test_appearance_toolbar_has_one_crop_and_no_split_or_page_mode(self):
        html = APP_HTML.read_text(encoding="utf-8")
        for removed in ('id="btnSplit"', 'id="documentMode"', "裁切為新憑證", "多張混貼"):
            self.assertNotIn(removed, html)
        for marker in ('id="btnCrop"', 'id="btnRestore"', 'id="btnFineMinus"', "−0.5°", 'id="btnFinePlus"', "+0.5°",
                       'id="fineSlider"', 'id="btnFineEdge"', 'id="btnFineRot"', 'id="btnFineCancel"', 'id="btnFineReset"',
                       'id="fineGuide"'):
            self.assertIn(marker, html)
        self.assertEqual(html.count("✂"), 1)
        self.assertNotIn("/split", self.source)
        self.assertNotIn("splitMode", self.source)
        self.assertIn('document_mode:"single"', self.source)
        self.assertIn("/restore-image", self.source)

    def test_edge_line_correction_levels_the_line_with_the_smallest_turn(self):
        script = r"""
const fs = require('fs'); const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8')); const source = input.source;
const start = source.indexOf('  function edgeCorrection('), end = source.indexOf('  (function initEdgeLine()', start);
if (start < 0 || end < 0) throw new Error('could not locate edgeCorrection');
const context = { Math }; vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.f = edgeCorrection;', context);
process.stdout.write(JSON.stringify(input.cases.map(([dx, dy]) => context.f(dx, dy))));
"""
        cases = [[100, 0], [100, 5], [100, -5], [-100, -5], [5, 100], [-5, 100], [100, 100]]
        completed = subprocess.run([str(NODE), "-e", script],
                                   input=json.dumps({"source": self.source, "cases": cases}).encode("utf-8"),
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        result = json.loads(completed.stdout.decode("utf-8"))
        # A line tilted clockwise on screen needs a counter-clockwise (negative) turn, and vice versa.
        expected = [0, -2.862, 2.862, -2.862, 2.862, -2.862, 45]
        for got, want in zip(result, expected):
            self.assertAlmostEqual(got, want, places=2)

    def test_accounts_management_is_removed_from_frontend(self):
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="accountsBtn"', html)
        self.assertNotIn('id="accountsModal"', html)
        self.assertNotIn('function openAccounts()', self.source)

    def test_removed_navigation_hashes_are_noop_and_review_still_routes(self):
        events = self.run_hashes(["#sysconfig", "#journal", "#accounts", "#v=47"])

        self.assertEqual(events, [
            {"handler": "review", "id": 47},
        ])

    def test_re_recognition_ignores_legacy_manual_entry_flag_and_saves_edits(self):
        records = {"12": {**self.review_record(), "manual_entry": True}}
        result = self.run_review(action="recognize", recordId=12, records=records)
        self.assertNotIn("confirm", [e["type"] for e in result["events"]])
        self.assertIn("persist", [e["type"] for e in result["events"]])
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in result["requests"]], ["12", "set-type", "recognize", "12"])
        self.assertFalse(any("/notes" in r["url"] for r in result["requests"]))

    def test_re_recognition_skips_prompt_for_unmarked_record_and_stops_after_save_failure(self):
        records = {"12": self.review_record()}
        ordinary = self.run_review(action="recognize", recordId=12, records=records)
        self.assertNotIn("confirm", [e["type"] for e in ordinary["events"]])
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in ordinary["requests"]], ["12", "set-type", "recognize", "12"])

        failed = self.run_review(action="recognize", recordId=12, records=records, persistFails=True)
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in failed["requests"]], ["12"])
        self.assertIn("persist", [e["type"] for e in failed["events"]])
        self.assertFalse(any(r["url"].endswith("/set-type") or r["url"].endswith("/recognize") for r in failed["requests"]))
        self.assertTrue(any(e["type"] == "alert" and "辨識失敗" in e["message"] for e in failed["events"]))

    def test_confirmation_tabs_and_filter_options_are_present(self):
        parser = WorkflowMarkupParser()
        parser.feed(APP_HTML.read_text(encoding="utf-8"))

        self.assertEqual([stage for stage, _label in parser.tabs], ["import", "prepared", "queued", "pending"])
        self.assertEqual(parser.options, [
            ("pending", "待確認"), ("confirmed", "已確認"), ("pending,confirmed", "全部"),
        ])

    def test_print_requires_clean_confirmed_recognition_and_uses_report_version(self):
        dirty = self.run_print(ready=True, event="input")
        unready = self.run_print(ready=False)
        for result in (dirty, unready):
            self.assertEqual(result["requests"], [])
            self.assertNotIn({"type": "write", "html": "<html>recognition report</html>"}, result["events"])
            self.assertNotIn({"type": "open"}, result["events"])
        self.assertTrue(dirty["buttonDisabledAfterEdit"])
        self.assertFalse(unready["ready"])

        ready = self.run_print(ready=True)
        self.assertEqual(ready["requests"], [{
            "url": "/api/vouchers/12/recognition-report?expected_updated_at=version-ready", "method": "GET",
        }])
        self.assertFalse(any(request["url"].endswith("/save") for request in ready["requests"]))
        self.assertTrue(any(event["type"] == "write" for event in ready["events"]))
        for event in ("input", "change"):
            edited = self.run_print(ready=True, event=event)
            self.assertTrue(edited["buttonDisabledAfterEdit"])
            self.assertEqual(edited["requests"], [])

    def test_auto_split_voucher_remains_editable_and_source_page_branches_are_absent(self):
        record = self.review_record()
        record["current"]["origin"] = {"layout": {"auto_split": True}}
        result = self.run_review(action="open", recordId=12, records={"12": record})

        self.assertFalse(result["confirmDisabled"])
        self.assertFalse(result["reviewBodyHidden"])
        self.assertFalse(result["detailsHidden"])
        self.assertFalse(result["imageToolsLocked"])
        self.assertNotIn("record_role === \"source_page\"", self.source)
        self.assertNotIn("source_parent_id", self.source)
        self.assertNotIn("sourceHintHtml", self.source)

    def test_skipped_page_notice_is_derived_from_import_job_status(self):
        self.assertIn('pages?.filter(page => page.phase === "skipped")', self.source)
        self.assertNotIn("sourceTrace", self.source)

        fixture = ROOT / "tests" / "fixtures" / "import_jobs_synthetic.js"
        completed = subprocess.run(
            [str(NODE), str(fixture), str(APP_JS)], capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        result = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(result["poll"]["idsFromPageVouchers"], "[42]")
        self.assertEqual(result["poll"]["idsNeverFallBackToSourcePage"], "[]")
        self.assertIn("已略過 1 個非單據頁", result["list"]["emptyTextWhenSkipped"])

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
        return {"ids": ids, "voucher_count": counts.get("vouchers", len(ids)),
                "confirmed_count": counts.get("confirmed", 0), "attachment_count": counts.get("attachments", 0),
                "requires_admin": bool(counts.get("confirmed", 0))}

    def test_delete_choices_describe_scope_and_hide_upload_when_identical(self):
        record = self.preview([5])
        upload = self.preview([3, 4, 5], vouchers=2, attachments=1)
        data = {"previews": {"record": record, "upload": upload}, "can_delete": {"record": True, "upload": True}}
        choices = self.run_delete("choices", data=data)["choices"]
        self.assertEqual([c["scope"] for c in choices], ["record", "upload"])
        self.assertEqual(choices[0]["summary"], "1 張單據")
        self.assertEqual(choices[1]["summary"], "2 張單據（含 1 張附頁）")
        self.assertNotIn("source_page_count", self.source)
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
        self.assertIn("憑證與明細彙總", warning)

    def test_queued_delete_only_previews_and_deletes_current_record(self):
        page = self.preview([12])
        page.update(page_no=4, blocked_reason="")
        data = {"previews": {"record": self.preview([12]), "upload": self.preview([10, 11, 12]), "page": page},
                "can_delete": {"record": True, "upload": True, "page": True}, "busy": False}
        choices = self.run_delete("choices", currentStage="queued", data=data)["choices"]
        self.assertEqual([c["scope"] for c in choices], ["record"])

        failed = self.run_delete("confirm", currentStage="queued", data=data, deleteStatus=403,
                                 deleteBody={"ok": False, "detail": "刪除受限"})
        self.assertEqual(failed["requests"][0]["url"], "/api/vouchers/12/delete-preview?scope=record")
        self.assertEqual(failed["requests"][-1]["url"], "/api/vouchers/12?scope=record")
        self.assertFalse(any(e["type"] in {"clearReview", "loadList", "refreshStats", "toast", "restoreImportJobs"}
                             for e in failed["events"]))

        done = self.run_delete("confirm", currentStage="queued", data=data,
                               deleteBody={"ok": True, "deleted_ids": [12]})
        self.assertEqual(done["requests"][0]["url"], "/api/vouchers/12/delete-preview?scope=record")
        self.assertEqual(done["requests"][-1]["url"], "/api/vouchers/12?scope=record")
        self.assertIn("loadList", [e["type"] for e in done["events"]])

    def test_queued_delete_choices_ignore_page_and_upload_previews(self):
        page = self.preview([12])
        page.update(page_no=1, blocked_reason="")
        data = {"previews": {"page": page, "upload": self.preview([11, 12]), "record": self.preview([12])},
                "can_delete": {"page": True, "upload": True, "record": True}}
        opened = self.run_delete("open", currentStage="queued", data=data)
        self.assertIn('value="record" checked', opened["choicesHtml"])
        self.assertNotIn('value="page"', opened["choicesHtml"])
        self.assertNotIn('value="upload"', opened["choicesHtml"])

    def test_importing_page_is_the_only_choice_and_carries_page_metadata(self):
        page = self.preview([12])
        page.update(page_no=3, blocked_reason="此頁已確認")
        data = {"previews": {"page": page, "record": self.preview([12]), "upload": self.preview([10, 12])},
                "can_delete": {"page": False, "record": True, "upload": True}}
        choices = self.run_delete("choices", data=data)["choices"]
        self.assertEqual([c["scope"] for c in choices], ["page"])
        self.assertEqual(choices[0]["label"], "刪除此頁（第 3 頁）")
        self.assertEqual(choices[0]["blockedReason"], "此頁已確認")
        opened = self.run_delete("open", data=data)
        self.assertIn('value="page" checked disabled', opened["choicesHtml"])
        self.assertIn("此頁已確認", opened["choicesHtml"])

    def test_delete_flow_cancel_failure_and_success(self):
        data = {"previews": {"record": self.preview([12]), "upload": self.preview([11, 12])},
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
        self.assertEqual(failed["currentId"], 12)
        started = next(e for e in failed["events"] if e["type"] == "deleteStarted")
        self.assertTrue(started["editBusy"])
        self.assertTrue(started["cancelDisabled"])
        self.assertTrue(started["confirmDisabled"])
        self.assertFalse(failed["editBusy"])
        self.assertFalse(failed["cancelDisabled"])
        self.assertFalse(failed["confirmDisabled"])

        done = self.run_delete("confirm", data=data, scope="upload",
                               deleteBody={"ok": True, "deleted_ids": [11, 12], "cleanup_failed": ["locked"]})
        self.assertEqual(done["requests"][-1]["url"], "/api/vouchers/12?scope=upload")
        types = [e["type"] for e in done["events"]]
        self.assertIn("clearReview", types)
        self.assertIn("loadList", types)
        self.assertIn("toast", types)
        self.assertTrue(any(e["type"] == "alert" and "locked" in e["message"] for e in done["events"]))
        self.assertFalse(done["modalOpen"])

    def test_late_preview_is_ignored_after_cancel_or_page_change(self):
        data = {"previews": {"record": self.preview([12])}, "can_delete": {"record": True}, "busy": False}
        cancelled = self.run_delete("cancelDuringPreview", data=data, deferPreview=True)
        self.assertFalse(cancelled["modalOpen"])
        self.assertFalse(any(e["type"] == "alert" for e in cancelled["events"]))
        self.assertEqual(cancelled["choicesHtml"], "")

        for change in ({"nextId": 13}, {"nextVersion": "updated-version"}, {"nextStage": "queued"}):
            with self.subTest(change=change):
                stale = self.run_delete("changeDuringPreview", data=data, deferPreview=True, **change)
                self.assertFalse(stale["modalOpen"])
                self.assertFalse(any(e["type"] == "alert" for e in stale["events"]))
                self.assertEqual(stale["choicesHtml"], "")

        older_data = {"previews": {"record": self.preview([12], vouchers=1)}, "can_delete": {"record": True}}
        newer_data = {"previews": {"record": self.preview([12], vouchers=2)}, "can_delete": {"record": True}}
        superseded = self.run_delete("supersedePreview", data=data, deferPreview=True,
                                     previewData=[older_data, newer_data])
        self.assertTrue(superseded["modalOpen"])
        self.assertIn("2 張單據", superseded["choicesHtml"])
        self.assertNotIn("1 張單據", superseded["choicesHtml"])

    def test_page_delete_uses_opened_version_and_restores_import_before_refresh(self):
        page = self.preview([12])
        page.update(page_no=2, blocked_reason="")
        data = {"previews": {"page": page}, "can_delete": {"page": True}, "busy": False}
        done = self.run_delete("confirm", currentStage="prepared", data=data, scope="page", currentVersion="version /+中文",
                               afterOpenVersion="changed-after-open",
                               deleteBody={"ok": True, "deleted_ids": [12]})
        self.assertEqual(done["requests"][-1]["url"],
                         "/api/vouchers/12?scope=page&expected_updated_at=version%20%2F%2B%E4%B8%AD%E6%96%87")
        types = [e["type"] for e in done["events"]]
        self.assertLess(types.index("restoreImportJobs"), types.index("loadList"))
        self.assertIn("clearReview", types)
        self.assertTrue(next(e for e in done["events"] if e["type"] == "clearReview")["editBusy"])
        self.assertFalse(next(e for e in done["events"] if e["type"] == "restoreImportJobs")["editBusy"])
        self.assertFalse(done["editBusy"])
        self.assertFalse(done["cancelDisabled"])
        general = self.run_delete("confirm", data={"previews": {"record": self.preview([12])},
                                                    "can_delete": {"record": True}, "busy": False})
        self.assertNotIn("restoreImportJobs", [e["type"] for e in general["events"]])

    def test_duplicate_confirm_does_not_send_duplicate_delete(self):
        data = {"previews": {"record": self.preview([12])}, "can_delete": {"record": True}, "busy": False}
        done = self.run_delete("confirmTwice", data=data)
        self.assertEqual(sum(r["method"] == "DELETE" for r in done["requests"]), 1)

    def test_delete_button_and_modal_markup_exist(self):
        html = APP_HTML.read_text(encoding="utf-8")
        for marker in ('id="btnDelete"', 'id="deleteModal"', 'id="deleteChoices"', 'id="deleteConfirm"'):
            self.assertIn(marker, html)
        tag_start = html.index('id="btnDelete"')
        self.assertIn('class="btn btn-sm danger"', html[tag_start:html.index(">", tag_start)])
        self.assertIn('title="預覽並確認刪除範圍"', html[tag_start:html.index(">", tag_start)])
        self.assertIn('status === "confirmed" && !isAdminUser()', self.source)

    def test_scan_source_directory_is_hidden_and_file_picker_remains(self):
        html = APP_HTML.read_text(encoding="utf-8")


        panel_start = html.index('id="scanSourcePanel"')
        panel_tag = html[html.rindex("<div", 0, panel_start):html.index(">", panel_start)]
        self.assertIn("hidden", panel_tag)

        # 一般拖拉／檔案選擇匯入仍可使用。
        for marker in ('id="dropZone"', 'id="fileInput"'):
            self.assertIn(marker, html)

    def test_legacy_trace_area_is_not_rendered(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertNotIn("追溯資料", html)
        self.assertNotIn("traceBox", html)
        self.assertNotIn("原始頁／追溯資料", self.source)


if __name__ == "__main__":
    unittest.main()
