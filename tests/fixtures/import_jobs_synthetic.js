const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const timers = new Map(), requests = [], events = [], responses = new Map();
const importJobs = new Map(), importJobTimers = new Map(), importSeenPages = new Map();
let nextTimer = 1, resolveUpload, resolveLate;
const nodes = {};
function makeNode(id) {
  return nodes[id] = {
    id, value: "", textContent: "", innerHTML: "", disabled: false, checked: false,
    style: {}, attrs: {}, listeners: {}, classList: {
      values: new Set(),
      toggle(name, force) { if (force === undefined ? !this.values.has(name) : force) this.values.add(name); else this.values.delete(name); },
      add(name) { this.values.add(name); }, remove(name) { this.values.delete(name); },
      contains(name) { return this.values.has(name); },
    },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    click() { events.push(["click", id]); },
    querySelectorAll() { return []; },
  };
}
const ids = ["dropZone","fileInput","progress","progressText","modal","importState","importHint","btnImportReview","btnImportRetry","btnImportCancel","uploadCompany","btnScanImport","appearanceRecovery","btnAppearanceRetry","btnAppearanceCancel","appearancePosition","btnScanList","scanFiles","scanDirHint","btnExtractAll","listBody","listEmpty","listTable","btnMerge","confirmationFilter","queueTitle"];
ids.forEach(makeNode);
nodes.uploadCompany.value = "3";
nodes.confirmationFilter.value = "prepared";
const document = {
  getElementById: (id) => nodes[id] || null,
  querySelectorAll(selector) {
    if (selector === ".mchk:checked") return (nodes.listBody.checks || []).filter(x => x.checked);
    return [];
  },
};
const context = {
  console, Map, Set, Array, JSON, String, Number, encodeURIComponent, document, nodes, events, dropZone: nodes.dropZone, fileInput: nodes.fileInput, progress: nodes.progress, progressText: nodes.progressText, modal: nodes.modal,
  currentUser: { id: 7 }, currentVoucher: {}, currentStage: "import", currentId: null,
  recognitionDirty: false, editBusy: false, importJobs, importJobTimers, importSeenPages,
  uploadSending: false, importRestorePending: false, importJobGeneration: 0,
  $: (id) => nodes[id] || null,
  json: () => ({ "Content-Type": "application/json" }),
  alert: (message) => events.push(["alert", message]),
  toast: (message) => events.push(["toast", message]),
  refreshStats: () => events.push(["refreshStats"]),
  loadList: async () => events.push(["loadList"]),
  setStage: (stage) => { context.currentStage = stage; events.push(["setStage", stage]); },
  openReview: async (id) => { context.currentId = id; events.push(["openReview", id]); },
  clearReview: () => events.push(["clearReview"]),
  errText: (data, fallback) => data && data.detail || fallback,
  esc: (x) => String(x == null ? "" : x),
  nf: (x) => String(x == null ? "—" : x),
  setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, { fn, delay }); return id; },
  clearTimeout(id) { timers.delete(id); },
  setInterval() { return 1; }, clearInterval() {},
  FormData: class { constructor() { this.values = []; } append(key, value) { this.values.push([key, value]); } },
  fetch: async (url, options = {}) => {
    requests.push({ url, method: options.method || "GET", body: options.body });
    if (url === "/api/import-jobs" && options.method === "POST") {
      return new Promise(resolve => { resolveUpload = resolve; });
    }
    if (url === "/api/import-jobs/late") {
      return new Promise(resolve => { resolveLate = resolve; });
    }
    const response = responses.get(url) || { ok: true, status: 200, body: { items: [] } };
    return { ok: response.ok !== false, status: response.status || 200, json: async () => response.body || {} };
  },
};
vm.createContext(context);
const importStart = source.indexOf("  // ── 上傳：");
const importEnd = source.indexOf("  // ── ③掃描清單", importStart);
if (importStart < 0 || importEnd < 0) throw new Error("production upload/import functions not found");
vm.runInContext(source.slice(importStart, importEnd) +
  "\nglobalThis.testImport = { handleFiles, runBatch, activeImportJob, uploadsBlocked, recoveryImportJob, currentReviewIds, renderImportState, trackImportJob, pollImportJob, stopImportPolling, actOnImportJob, restoreImportJobs, restorePending: () => importRestorePending };", context);

async function uploadScenario() {
  const fileA = { name: "a.pdf", type: "application/pdf" };
  const fileB = { name: "b.pdf", type: "application/pdf" };
  const postsBeforeMulti = requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length;
  context.testImport.handleFiles([fileA, fileB]);
  const rejectedMulti = requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length === postsBeforeMulti;
  const multiAlert = events.find(e => e[0] === "alert")?.[1] || "";
  context.testImport.handleFiles([fileA]);
  await Promise.resolve();
  const lockedDuringUpload = context.testImport.uploadsBlocked() && nodes.fileInput.disabled;
  context.testImport.handleFiles([fileB]);
  const requestCountWhileLocked = requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length;
  responses.set("/api/import-jobs/upload-1", { body: { id: "upload-1", status: "queued", pages: [] } });
  context.currentUser = null;
  resolveUpload({ ok: true, status: 200, json: async () => ({ ok: true, job_id: "upload-1", status: "queued" }) });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  context.currentUser = { id: 7 };
  return { rejectedMulti, multiAlert, lockedDuringUpload, requestCountWhileLocked,
    requestCount: requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length,
    formData: requests.find(r => r.url === "/api/import-jobs" && r.method === "POST")?.body?.values || [],
    blockedAfterAccepted: context.testImport.uploadsBlocked(), disabledAfterAccepted: nodes.fileInput.disabled };
}
async function pollScenario() {
  context.currentUser = { id: 7 };
  events.length = 0; importJobs.clear(); importJobTimers.clear(); importSeenPages.clear();
  const api = context.testImport;
  importJobs.set("cross-tab", { id: "cross-tab", status: "awaiting_review" });
  responses.set("/api/import-jobs/cross-tab", { body: { id: "cross-tab", status: "awaiting_review", current_page: 1, total_pages: 3, review_ids: [41], pages: [{ status: "preview_ready", voucher_ids: [41] }] } });
  const idsPreferJobReviewList = JSON.stringify(api.currentReviewIds({ status: "awaiting_review", review_ids: [41], pages: [{ status: "preview_ready", voucher_ids: [99] }] }));
  const idsFromPageVouchers = JSON.stringify(api.currentReviewIds({ status: "awaiting_review", pages: [{ status: "preview_ready", voucher_ids: [42], source_page_id: 99 }] }));
  const idsNeverFallBackToSourcePage = JSON.stringify(api.currentReviewIds({ status: "awaiting_review", pages: [{ status: "preview_ready", voucher_ids: [], source_page_id: 99 }] }));
  context.currentStage = "import"; await api.pollImportJob("cross-tab");
  const first = events.filter(e => e[0] === "setStage").length;
  responses.set("/api/import-jobs/cross-tab", { body: { id: "cross-tab", status: "awaiting_review", current_page: 2, total_pages: 3, review_ids: [42], pages: [{ status: "preview_ready", voucher_ids: [42] }] } });
  await api.pollImportJob("cross-tab");
  const afterPageTwo = events.filter(e => e[0] === "loadList").length;
  const pagePosition = nodes.appearancePosition.textContent;
  context.recognitionDirty = true;
  responses.set("/api/import-jobs/cross-tab", { body: { id: "cross-tab", status: "awaiting_review", current_page: 3, total_pages: 3, review_ids: [43], pages: [] } });
  await api.pollImportJob("cross-tab");
  const afterDirtyPage = events.filter(e => e[0] === "loadList").length;
  const timersAtAwaiting = importJobTimers.get("cross-tab") != null;
  const reviewButtonVisible = !nodes.btnImportReview.classList.contains("hidden");
  context.recognitionDirty = false;
  responses.set("/api/import-jobs/cross-tab", { body: { id: "cross-tab", status: "awaiting_review", current_page: 3, total_pages: 3, review_ids: [], pages: [] } });
  await api.pollImportJob("cross-tab");
  const reviewButtonHiddenWithoutIds = nodes.btnImportReview.classList.contains("hidden");
  importJobs.set("late", { id: "late", status: "running" });
  const latePoll = api.pollImportJob("late");
  await Promise.resolve();
  api.stopImportPolling();
  resolveLate({ ok: true, status: 200, json: async () => ({ id: "late", status: "running", pages: [] }) });
  await latePoll;
  return { first, afterPageTwo, afterDirtyPage, pagePosition, timersAtAwaiting, idsPreferJobReviewList, idsFromPageVouchers, idsNeverFallBackToSourcePage, reviewButtonVisible, reviewButtonHiddenWithoutIds,
    staleTimerAfterLogout: importJobTimers.has("late"), jobsAfterLogout: importJobs.size,
    lateResponseDidNotNavigate: events.filter(e => e[0] === "setStage").length === first };
}
async function recoveryScenario() {
  const api = context.testImport;
  api.stopImportPolling();
  responses.set("/api/import-jobs", { body: { items: [{ id: "recovered", status: "interrupted" }] } });
  responses.set("/api/import-jobs/recovered", { body: { id: "recovered", status: "interrupted", pages: [] } });
  await api.restoreImportJobs();
  return { restoredId: api.recoveryImportJob()?.id, blocked: api.uploadsBlocked(), retryVisible: !nodes.btnImportRetry.classList.contains("hidden"),
    retryLabel: nodes.btnImportRetry.textContent, restorePending: api.restorePending() };
}
async function scanScenario() {
  context.testImport.stopImportPolling();
  await context.testImport.restoreImportJobs();
  const scanStart = source.indexOf("  // ── ①匯入：伺服器端掃描來源目錄");
  const scanEnd = source.indexOf("  function hideProgress()", scanStart);
  if (scanStart < 0 || scanEnd < 0) throw new Error("production scan source handlers not found");
  vm.runInContext(source.slice(scanStart, scanEnd), context);
  nodes.scanFiles.querySelectorAll = () => [{ value: "scan.pdf" }];
  responses.set("/api/scan-source/import", { body: { ok: true, job_id: "scan-1", status: "queued" } });
  await nodes.btnScanImport.listeners.click();
  const request = requests.find(r => r.url === "/api/scan-source/import" && r.method === "POST");
  return JSON.parse(request.body);
}
async function listScenario() {
  const listStart = source.indexOf("  async function loadList()");
  const listEnd = source.indexOf("  // 合併多頁為一張", listStart);
  if (listStart < 0 || listEnd < 0) throw new Error("production loadList not found");
  const body = nodes.listBody;
  body.checks = []; body.rows = [];
  Object.defineProperty(body, "innerHTML", { configurable: true, get() { return this.html || ""; }, set(html) {
    this.html = html;
    this.checks = Array.from(html.matchAll(/class="mchk" data-id="(\d+)"( checked)?/g), m => ({ dataset: { id: m[1] }, checked: !!m[2], addEventListener() {} }));
    this.rows = Array.from(html.matchAll(/<tr data-id="(\d+)"/g), m => ({ dataset: { id: m[1] }, addEventListener() {} }));
  }});
  body.querySelectorAll = (selector) => selector === ".mchk:checked" ? body.checks.filter(cb => cb.checked) : selector === ".mchk" ? body.checks : body.rows;
  nodes.listEmpty.classList.remove("hidden"); nodes.listTable.classList.add("hidden");
  nodes.listBody.html = "";
  nodes.confirmationFilter.value = "prepared";
  const rows = [{ id: 1, status: "prepared", doc_type: "one", origin: { layout: { auto_split: true } } }, { id: 2, status: "prepared", doc_type: "two" }, { id: 3, status: "queued", doc_type: "three" }];
  context.currentStage = "prepared"; context.currentId = null;
  context.EMPTY_LIST_TEXT = "此階段目前沒有憑證。";
  let activeJobForList = { id: "job", status: "awaiting_review", review_ids: [2] };
  context.activeImportJob = () => activeJobForList;
  context.currentReviewIds = (job) => job?.review_ids || [];
  context.STAGE_STATUS = { prepared: "prepared", queued: "queued" };
  context.STAGE_TITLE = { prepared: "ready", queued: "queue" };
  context.STATUS_CLASS = {}; context.STATUS_LABEL = {}; context.DOC_TYPES = {};
  context.updateMergeBtn = () => {};
  context.refreshStats = () => {};
  context.renderImportState = () => {};
  context.fetch = async () => ({ json: async () => ({ items: rows }) });
  vm.runInContext(source.slice(listStart, listEnd) + "\nglobalThis.testLoadList = loadList;", context);
  await context.testLoadList();
  const preparedIds = body.rows.map(row => Number(row.dataset.id));
  const preparedHasCheckboxes = body.checks.length > 0;
  context.currentStage = "queued"; nodes.confirmationFilter.value = "queued";
  await context.testLoadList();
  body.checks.find(cb => cb.dataset.id === "2").checked = true;
  await context.testLoadList();
  const queuedIds = body.rows.map(row => Number(row.dataset.id));
  const queuedCheckedIds = body.checks.filter(cb => cb.checked).map(cb => Number(cb.dataset.id));
  activeJobForList = { id: "job", status: "awaiting_review", review_ids: [] };
  context.currentStage = "prepared"; nodes.confirmationFilter.value = "prepared";
  await context.testLoadList();
  const emptyTextWhenNoReview = nodes.listEmpty.textContent;
  activeJobForList = { id: "job", status: "awaiting_review", review_ids: [], pages: [{ status: "completed", phase: "skipped" }] };
  await context.testLoadList();
  const emptyTextWhenSkipped = nodes.listEmpty.textContent;
  return { preparedIds, preparedHasCheckboxes, queuedIds, queuedCheckedIds, emptyTextWhenNoReview, emptyTextWhenSkipped };
}
(async () => {
  await context.testImport.restoreImportJobs();
  const upload = await uploadScenario();
  const poll = await pollScenario();
  const scan = await scanScenario();
  const recovery = await recoveryScenario();
  const list = await listScenario();
  process.stdout.write(JSON.stringify({ upload, poll, scan, recovery, list, source: {
    hasLegacyJobRenderer: source.includes("renderImportJobs"),
    hasBatchAppearanceControls: ["appearanceBatch", "batchType", "dirInput"].some(s => source.includes(s)),
  } }));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
