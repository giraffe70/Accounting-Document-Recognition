const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const source = fs.readFileSync(process.argv[2], "utf8");
function section(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  if (a < 0 || b < 0) throw new Error(`Production section missing: ${start}`);
  return source.slice(a, b);
}
const events = [], requests = [], timers = [], nodes = new Map();
function $(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, { disabled: false, value: "other", textContent: "", style: {},
      classList: { add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
        contains(c) { return classes.has(c); }, toggle(c, force) { if (force) classes.add(c); else classes.delete(c); } },
      addEventListener() {},
    });
  }
  return nodes.get(id);
}
const stage = input.stage || "queued";
if (stage === "queued") $("revForm").classList.add("prepared");
let pollResolve, openResolve, changed = false, context;
const pendingItems = input.pendingItems || [{ id: 3, status: "pending" }, { id: 12, status: "pending" }, { id: 22, status: "pending" }];
function mutate() {
  if (changed || !input.change) return;
  changed = true;
  if (input.change === "stage") context.setStage("import");
  if (input.change === "roundtrip") { context.setStage("import"); context.setStage("queued"); }
  if (input.change === "selection") { context.currentId = 99; context.reviewInteractionVersion++; }
  if (input.change === "edit") context.markRecognitionDirty();
  if (input.change === "saving") context.editBusy = true;
  if (input.change === "logout") context.currentUser = null;
  if (input.change === "newBatch") context.extractGeneration++;
}
function response(data, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => data }; }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || "GET" });
  if (url === "/api/extract-batch") return response({ ok: !input.startFailure, job_id: "batch-1", detail: "無法開始" }, input.startFailure ? 409 : 200);
  if (url === "/api/extract-job") {
    if (input.mutateAt === "poll") mutate();
    if (input.deferPoll) return new Promise((resolve) => { pollResolve = resolve; });
    return response(job(), input.pollFailure ? 500 : 200);
  }
  if (url === "/api/vouchers?status=pending") {
    if (input.mutateAt === "pending") mutate();
    if (input.pendingFailure) return response({ detail: "讀取清單失敗" }, 500);
    return response({ items: pendingItems });
  }
  if (url.endsWith("/recognize")) {
    if (input.mutateAt === "recognize") mutate();
    return response({ ok: !input.singleFailure, detail: "辨識失敗" });
  }
  if (url.endsWith("/set-type")) return response({ updated_at: "saved-version" });
  if (url === "/api/vouchers/22") return new Promise((resolve) => { openResolve = resolve; });
  throw new Error(`Unexpected request: ${url}`);
}
function job() {
  return { id: input.wrongJob ? "another-batch" : "batch-1", status: "completed", total: 2, done: 2,
    results: (input.results || [22, 12]).map((id) => ({ id })), errors: input.errors || [] };
}
context = {
  $, console, Set, currentStage: stage, currentId: 12, currentUser: { id: 1 },
  reviewInteractionVersion: 0, extractGeneration: 0, jobTimer: null,
  recognitionReady: false, recognitionDirty: !!input.initialDirty, editBusy: false, inferBusy: 0,
  currentVoucher: { doc_type_key: "other", origin: { preparation: { mode: "manual_review", type_confirmed: true } } },
  currentVersion: "original-version", cropping: false,
  STAGE_TITLE: { pending: "④ 確認" },
  document: { querySelectorAll() { return []; } },
  setInterval(fn) { timers.push(fn); return timers.length; },
  clearInterval(id) { events.push({ type: "clearInterval", id }); },
  json() { return { "Content-Type": "application/json" }; }, fetch,
  errText(data, fallback) { return data.detail || fallback; },
  responseError: async () => "類型儲存失敗",
  toast(message) { events.push({ type: "toast", message }); },
  alert(message) { events.push({ type: "alert", message }); },
  loadList() { events.push({ type: "loadList", stage: context.currentStage }); },
  refreshStats() { events.push({ type: "stats" }); }, renderImportState() {}, updateRecognitionState() {},
  async persistCurrentEdits() { events.push({ type: "save" }); },
  async withInferProgress(_label, fn) { return fn(); },
  async openReview(id) { context.currentId = id; context.reviewInteractionVersion++; events.push({ type: "open", id, stage: context.currentStage }); },
};
vm.createContext(context);
const code = [
  section('  function setStage(stage)', '  async function refreshStats()'),
  section('  function clearReview()', '  // 外觀確認 → 加入掃描清單'),
  section('  function markRecognitionDirty()', '  function applyRecognitionState('),
  section('  function recognitionView()', '  function buildActiveFields('),
  section('  // ── ③掃描清單：一鍵批次提取', '  // ── ①匯入：伺服器端掃描來源目錄'),
  section('  async function openReview(id)', '  // 多頁憑證：主頁＋附頁縮圖切換').replace('function openReview(id)', 'function delayedOpenReview(id)'),
].join("\n");
vm.runInContext(code, context);
const tick = () => new Promise(setImmediate);
(async () => {
  if (input.action === "openRace") {
    const opening = context.delayedOpenReview(22);
    await tick(); mutate();
    openResolve(response({ id: 22, status: "pending", updated_at: "late-version", current: {} }));
    await opening;
  } else if (input.action === "single") {
    await context.runRecognize();
  } else {
    await context.startExtraction();
    if (timers.length) {
      const poll = timers[0];
      const first = poll();
      if (input.overlap) await poll();
      if (input.deferPoll) { await tick(); pollResolve(response(job())); }
      await first;
      await poll(); await poll(); // Simulate already-queued timer callbacks after completion.
    }
  }
  process.stdout.write(JSON.stringify({ events, requests, stage: context.currentStage, id: context.currentId,
    version: context.currentVersion, dirty: context.recognitionDirty,
    disabled: $("btnExtractAll").disabled, timers: timers.length }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
