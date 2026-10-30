const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const timers = new Map(), requests = [], events = [], responses = new Map();
const importJobs = new Map(), importJobTimers = new Map(), importSeenPages = new Map();
let nextTimer = 1, resolveUpload, resolveLate;
const nodes = {};
function makeNode(id) {
  const node = {
    id, value: "", textContent: "", innerHTML: "", disabled: false, checked: false,
    options: [], focusCount: 0,
    style: {}, attrs: {}, listeners: {}, classList: {
      values: new Set(),
      toggle(name, force) { if (force === undefined ? !this.values.has(name) : force) this.values.add(name); else this.values.delete(name); },
      add(name) { this.values.add(name); }, remove(name) { this.values.delete(name); },
      contains(name) { return this.values.has(name); },
    },
    setAttribute(name, value) { this.attrs[name] = String(value); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    click() { events.push(["click", id]); },
    focus() { this.focusCount++; events.push(["focus", id]); },
    querySelectorAll() { return []; },
  };
  let value = "", html = "";
  Object.defineProperty(node, "value", { configurable: true, get() { return value; }, set(next) { value = String(next ?? ""); } });
  Object.defineProperty(node, "innerHTML", { configurable: true, get() { return html; }, set(next) {
    html = String(next); this.options = [];
    if (id === "uploadCompany" || id === "revCompanySel") {
      this.options = Array.from(html.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi), match => {
        const attrs = match[1], valueMatch = attrs.match(/\bvalue="([^"]*)"/i);
        return { value: valueMatch ? valueMatch[1] : "", text: match[2].replace(/<[^>]*>/g, ""),
          disabled: /\bdisabled\b/i.test(attrs), hidden: /\bhidden\b/i.test(attrs), selected: /\bselected\b/i.test(attrs) };
      });
      const selected = this.options.find(option => option.selected);
      value = selected ? selected.value : (this.options[0]?.value || "");
    }
  }});
  return nodes[id] = node;
}
const ids = ["dropZone","fileInput","progress","progressText","modal","importState","importHint","btnImportReview","btnImportRetry","btnImportCancel","uploadCompany","uploadCompanyStatus","uploadCompanyTaxId","revCompanySel","revCompanyTaxId","btnScanImport","appearanceRecovery","btnAppearanceRetry","btnAppearanceCancel","appearancePosition","btnScanList","scanFiles","scanDirHint","btnExtractAll","listBody","listEmpty","listTable","importView","queueView","confirmationFilter","queueTitle"];
ids.forEach(makeNode);
nodes.confirmationFilter.value = "prepared";
const document = {
  getElementById: (id) => nodes[id] || null,
  querySelectorAll() { return []; },
};
const context = {
  console, Map, Set, Array, JSON, String, Number, encodeURIComponent, document, nodes, events, dropZone: nodes.dropZone, fileInput: nodes.fileInput, progress: nodes.progress, progressText: nodes.progressText, modal: nodes.modal,
  currentUser: { id: 7 }, currentVoucher: {}, currentStage: "import", currentId: null,
  recognitionDirty: false, editBusy: false, importJobs, importJobTimers, importSeenPages,
  uploadSending: false, importRestorePending: false, importJobGeneration: 0,
  companyRecords: [], companyLoadState: "loading",
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
    const response = responses.get(url) || { ok: true, status: 200, body: url === "/api/companies" ? { companies: [{ id: 3, short: "甲", name: "合成甲公司", tax_id: "12345678" }] } : { items: [] } };
    return { ok: response.ok !== false, status: response.status || 200, json: async () => response.body || {} };
  },
};
vm.createContext(context);
const importStart = source.indexOf("  // ── 上傳：");
const importEnd = source.indexOf("  // ── ③掃描清單", importStart);
if (importStart < 0 || importEnd < 0) throw new Error("production upload/import functions not found");
vm.runInContext(source.slice(importStart, importEnd) +
  "\nglobalThis.testImport = { handleFiles, runBatch, activeImportJob, uploadsBlocked, recoveryImportJob, currentReviewIds, renderImportState, trackImportJob, pollImportJob, stopImportPolling, actOnImportJob, restoreImportJobs, restorePending: () => importRestorePending, selectedUploadCompany, uploadCompanyMessage, requireUploadCompany, companyState: () => companyLoadState };", context);

const companiesStart = source.indexOf("  // 載入公司清單到上傳下拉");
const companiesEnd = source.indexOf("  // ── 左右欄可拖拉調整寬度", companiesStart);
if (companiesStart < 0 || companiesEnd < 0) throw new Error("production company loading functions not found");
vm.runInContext(source.slice(companiesStart, companiesEnd) +
  "\nglobalThis.testCompanies = { loadCompanies, updateUploadCompanyTaxId };", context);
const scanStart = source.indexOf("  // ── ①匯入：伺服器端掃描來源目錄");
const scanEnd = source.indexOf("  function hideProgress()", scanStart);
if (scanStart < 0 || scanEnd < 0) throw new Error("production scan source handlers not found");
vm.runInContext(source.slice(scanStart, scanEnd), context);

async function uploadScenario() {
  events.length = 0;
  const fileA = { name: "a.pdf", type: "application/pdf" };
  const fileB = { name: "b.pdf", type: "application/pdf" };
  const postsBeforeMulti = requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length;
  context.testImport.handleFiles([fileA, fileB]);
  const rejectedMulti = requests.filter(r => r.url === "/api/import-jobs" && r.method === "POST").length === postsBeforeMulti;
  const multiAlert = events.find(e => e[0] === "alert")?.[1] || "";
  nodes.uploadCompany.value = "甲";
  if (nodes.uploadCompany.listeners.change) nodes.uploadCompany.listeners.change();
  context.testImport.handleFiles([fileA]);
  await Promise.resolve();
  const lockedDuringUpload = context.testImport.uploadsBlocked() && nodes.fileInput.disabled;
  const lockedDropZone = nodes.dropZone.attrs["aria-disabled"] === "true";
  const companyLockedDuringUpload = nodes.uploadCompany.disabled;
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
    blockedAfterAccepted: context.testImport.uploadsBlocked(), disabledAfterAccepted: nodes.fileInput.disabled,
    lockedDropZone, companyLockedDuringUpload,
    selectedAfterUpload: nodes.uploadCompany.value };
}
async function attemptImports(file) {
  const before = requests.filter(r => r.method === "POST" && ["/api/import-jobs", "/api/scan-source/import"].includes(r.url)).length;
  context.testImport.handleFiles([file]);
  context.testImport.runBatch([file]);
  nodes.dropZone.listeners.drop({ preventDefault() {}, dataTransfer: { files: [file] } });
  await nodes.btnScanImport.listeners.click();
  return requests.filter(r => r.method === "POST" && ["/api/import-jobs", "/api/scan-source/import"].includes(r.url)).length - before;
}
async function companyScenario() {
  const api = context.testImport;
  api.renderImportState();
  const initialState = api.companyState();
  const file = { name: "unselected.pdf", type: "application/pdf" };
  const focusBefore = nodes.uploadCompany.focusCount;
  nodes.scanFiles.querySelectorAll = () => [{ value: "scan.pdf" }];
  const initialRejectedPosts = await attemptImports(file);
  const initialFocusCount = nodes.uploadCompany.focusCount - focusBefore;

  responses.set("/api/companies", { ok: false, status: 503, body: {} });
  await context.testCompanies.loadCompanies();
  const failedState = api.companyState(), failedDisabled = nodes.fileInput.disabled && nodes.btnScanImport.disabled;
  const failedMessage = nodes.uploadCompanyStatus.textContent;
  responses.set("/api/companies", { body: { companies: [] } });
  await context.testCompanies.loadCompanies();
  const emptyState = api.companyState(), emptyDisabled = nodes.fileInput.disabled && nodes.btnScanImport.disabled;
  const emptyMessage = nodes.uploadCompanyStatus.textContent;

  responses.set("/api/companies", { body: { companies: [
    { id: 3, short: "甲", name: "合成甲公司", tax_id: "12345678" },
    { id: 4, name: "合成乙公司", tax_id: "87654321" },
    { id: 5, short: "", name: "" },
  ] } });
  await context.testCompanies.loadCompanies();
  const readyState = api.companyState();
  const placeholder = nodes.uploadCompany.options[0];
  const selectorEnabledWithoutSelection = !nodes.uploadCompany.disabled;
  const readyImportDisabledWithoutSelection = nodes.fileInput.disabled && nodes.btnScanImport.disabled;
  const unselectedMessage = nodes.uploadCompanyStatus.textContent;
  const readyBlankDropDisabled = nodes.dropZone.attrs["aria-disabled"] === "true";
  const readyBlankRejectedPosts = await attemptImports(file);
  nodes.uploadCompany.value = "not-a-company";
  const invalidRejectedPosts = await attemptImports(file);
  const invalidCompanyRejected = api.selectedUploadCompany() === null;
  nodes.uploadCompany.value = "甲";
  nodes.uploadCompany.listeners.change();
  const taxId = nodes.uploadCompanyTaxId.textContent;
  const controlsEnabledAfterSelection = !nodes.fileInput.disabled && !nodes.btnScanImport.disabled && !nodes.uploadCompany.disabled;
  const selectedDropZoneEnabled = nodes.dropZone.attrs["aria-disabled"] === "false";
  const selectedCompany = api.selectedUploadCompany()?.short || "";
  await context.testCompanies.loadCompanies();
  return { initialState, initialRejectedPosts, initialFocusCount,
    failedState, failedDisabled, failedMessage,
    emptyState, emptyDisabled, emptyMessage, readyState, placeholderValue: placeholder?.value,
    placeholderDisabled: placeholder?.disabled, placeholderHidden: placeholder?.hidden,
    selectorEnabledWithoutSelection, readyImportDisabledWithoutSelection, unselectedMessage,
    readyBlankDropDisabled, readyBlankRejectedPosts, invalidRejectedPosts, invalidCompanyRejected,
    taxId, controlsEnabledAfterSelection, selectedDropZoneEnabled,
    selectedCompany, reloadedValue: nodes.uploadCompany.value, reloadedState: api.companyState() };
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
  nodes.scanFiles.querySelectorAll = () => [{ value: "scan.pdf" }];
  nodes.uploadCompany.value = "合成乙公司";
  if (nodes.uploadCompany.listeners.change) nodes.uploadCompany.listeners.change();
  responses.set("/api/scan-source/import", { body: { ok: true, job_id: "scan-1", status: "queued" } });
  await nodes.btnScanImport.listeners.click();
  const request = requests.find(r => r.url === "/api/scan-source/import" && r.method === "POST");
  return JSON.parse(request.body);
}
async function listScenario() {
  const stageStart = source.indexOf("  function setStage(stage)");
  const stageEnd = source.indexOf("  async function refreshStats()", stageStart);
  const listStart = source.indexOf("  async function loadList()");
  const listEnd = source.indexOf("  // ── 刪除 ──", listStart);
  if (stageStart < 0 || stageEnd < 0) throw new Error("production setStage not found");
  if (listStart < 0 || listEnd < 0) throw new Error("production loadList not found");
  const body = nodes.listBody;
  body.checks = []; body.rows = [];
  Object.defineProperty(body, "innerHTML", { configurable: true, get() { return this.html || ""; }, set(html) {
    this.html = html;
    this.checks = Array.from(html.matchAll(/<input\b[^>]*\btype="checkbox"[^>]*>/g));
    this.rows = Array.from(html.matchAll(/<tr data-id="(\d+)"/g), m => ({
      dataset: { id: m[1] }, listeners: {},
      addEventListener(type, handler) { this.listeners[type] = handler; },
    }));
  }});
  body.querySelectorAll = (selector) => selector === "tr" ? body.rows : [];
  nodes.listEmpty.classList.remove("hidden"); nodes.listTable.classList.add("hidden");
  nodes.listBody.html = "";
  nodes.confirmationFilter.value = "prepared";
  let rows = [{ id: 1, status: "prepared", doc_type: "one", origin: { layout: { auto_split: true } } }, { id: 2, status: "prepared", doc_type: "two" }, { id: 3, status: "queued", doc_type: "three" }];
  context.currentStage = "prepared"; context.currentId = null;
  context.EMPTY_LIST_TEXT = "此階段目前沒有憑證。";
  let activeJobForList = { id: "job", status: "awaiting_review", review_ids: [2] };
  context.activeImportJob = () => activeJobForList;
  context.currentReviewIds = (job) => job?.review_ids || [];
  context.STAGE_STATUS = { prepared: "prepared", queued: "queued" };
  context.STAGE_TITLE = { prepared: "ready", queued: "queue" };
  context.STATUS_CLASS = {}; context.STATUS_LABEL = {}; context.DOC_TYPES = {};
  context.refreshStats = () => {};
  context.renderImportState = () => {};
  context.fetch = async () => ({ json: async () => ({ items: rows }) });
  vm.runInContext(source.slice(stageStart, stageEnd) + source.slice(listStart, listEnd) +
    "\nglobalThis.testLoadList = loadList; globalThis.testSetStage = setStage;", context);
  async function switchStage(stage) {
    context.testSetStage(stage);
    await new Promise(setImmediate);
  }
  await switchStage("prepared");
  const preparedIds = body.rows.map(row => Number(row.dataset.id));
  const preparedHasCheckboxes = body.checks.length > 0;
  await switchStage("queued");
  await context.testLoadList();
  const queuedIds = body.rows.map(row => Number(row.dataset.id));
  const queuedHasCheckboxes = body.checks.length > 0;
  const queuedExtractVisible = !nodes.btnExtractAll.classList.contains("hidden");
  body.rows[2].listeners.click();
  const queuedOpenedId = context.currentId;
  rows = [];
  await context.testLoadList();
  const queuedEmptyVisible = !body.rows.length && !nodes.listEmpty.classList.contains("hidden") && nodes.listTable.classList.contains("hidden");
  await switchStage("pending");
  const pendingExtractHidden = nodes.btnExtractAll.classList.contains("hidden");
  await switchStage("import");
  const importVisible = !nodes.importView.classList.contains("hidden") && nodes.queueView.classList.contains("hidden");
  activeJobForList = { id: "job", status: "awaiting_review", review_ids: [] };
  await switchStage("prepared");
  const emptyTextWhenNoReview = nodes.listEmpty.textContent;
  activeJobForList = { id: "job", status: "awaiting_review", review_ids: [], pages: [{ status: "completed", phase: "skipped" }] };
  await context.testLoadList();
  const emptyTextWhenSkipped = nodes.listEmpty.textContent;
  return { preparedIds, preparedHasCheckboxes, queuedIds, queuedHasCheckboxes, queuedExtractVisible, queuedOpenedId,
    queuedEmptyVisible, pendingExtractHidden, importVisible, emptyTextWhenNoReview, emptyTextWhenSkipped };
}
(async () => {
  await context.testImport.restoreImportJobs();
  const company = await companyScenario();
  const upload = await uploadScenario();
  const poll = await pollScenario();
  const scan = await scanScenario();
  const recovery = await recoveryScenario();
  const list = await listScenario();
  process.stdout.write(JSON.stringify({ company, upload, poll, scan, recovery, list, source: {
    hasLegacyJobRenderer: source.includes("renderImportJobs"),
    hasBatchAppearanceControls: ["appearanceBatch", "batchType", "dirInput"].some(s => source.includes(s)),
  } }));
})().catch(error => { console.error(error.stack || error); process.exit(1); });
