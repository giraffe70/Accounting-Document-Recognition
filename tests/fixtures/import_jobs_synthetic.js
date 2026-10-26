const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('  const IMPORT_ACTIVE =');
const end = source.indexOf('  // ── ③掃描清單', start);
if (start < 0 || end < 0) throw new Error('production import job functions not found');
const timers = new Map(), requests = [], events = [];
const boundHandlers = new Map();
let nextTimer = 1, responseByUrl = {}, createdUrls = 0, revokedUrls = 0;
const importJobs = new Map(), importJobTimers = new Map(), importSeenPages = new Map();
const jobNode = {
  innerHTML: '', classList: { toggle() {} },
  querySelectorAll(selector) {
    const attr = selector === '[data-import-review-id]' ? 'data-import-review-id' : 'data-job-id';
    const regex = new RegExp(`${attr}="([^"]+)"`, 'g');
    return Array.from(this.innerHTML.matchAll(regex), (match) => {
      const handlerKey = `${attr}:${match[1]}`;
      const button = { dataset: attr === 'data-import-review-id' ? { importReviewId: match[1] } : { jobId: match[1], jobAction: 'cancel' }, handler: boundHandlers.get(handlerKey) || null,
        addEventListener(_type, handler) { this.handler = handler; boundHandlers.set(handlerKey, handler); } };
      return button;
    });
  },
};
let recognitionDirty = true, currentId = 77, currentUser = { id: 1 };
function $(id) { return id === 'importJobs' ? jobNode : null; }
function setTimeoutFake(fn, delay) { const id = nextTimer++; timers.set(id, { fn, delay }); return id; }
function clearTimeoutFake(id) { timers.delete(id); }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET' });
  const value = responseByUrl[url] || {};
  return { ok: value.ok !== false, status: value.status || 200, json: async () => value.body || value };
}
function errText(data, fallback) { return data?.detail || fallback; }
function loadList() { events.push('loadList'); }
function refreshStats() { events.push('refreshStats'); }
function alert(msg) { events.push(`alert:${msg}`); }
function toast(msg) { events.push(`toast:${msg}`); }
function setStage(stage) { events.push(['setStage', stage]); }
function openReview(id) { events.push(['openReview', id]); }
  const context = { console, Map, Set, URL: { createObjectURL: () => `blob:${++createdUrls}`, revokeObjectURL: () => { revokedUrls += 1; } }, $, importJobs, importJobTimers, importSeenPages,
  currentUser, fetch, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake, errText, loadList, refreshStats, alert, toast, editBusy: false,
  encodeURIComponent, esc: (x) => String(x ?? ''), recognitionDirty, currentId, importJobGeneration: 0, setStage, openReview };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + '\nglobalThis.testApi = { trackImportJob, pollImportJob, actOnImportJob, restoreImportJobs, renderImportJobs, stopImportPolling };', context);
async function runListSelectionScenario() {
  const listStart = source.indexOf('  async function loadList()');
  const listEnd = source.indexOf('  // 合併多頁為一張', listStart);
  if (listStart < 0 || listEnd < 0) throw new Error('production loadList function not found');
  let rowData = [{ id: 1 }, { id: 2 }];
  const nodes = {
    confirmationFilter: { value: 'pending' }, listEmpty: { textContent: '', classList: { add() {}, remove() {} } },
    listTable: { classList: { add() {}, remove() {} } }, btnMerge: { disabled: false },
    listBody: {
      checks: [], rows: [],
      set innerHTML(html) {
        this.checks = Array.from(html.matchAll(/class="mchk" data-id="(\d+)"( checked)?/g), (m) => ({ dataset: { id: m[1] }, checked: !!m[2], classList: { contains: () => false }, addEventListener() {} }));
        this.rows = Array.from(html.matchAll(/<tr data-id="(\d+)"/g), (m) => ({ dataset: { id: m[1] }, addEventListener() {} }));
      },
      querySelectorAll(selector) { return selector === '.mchk:checked' ? this.checks.filter((cb) => cb.checked) : selector === '.mchk' ? this.checks : this.rows; },
    },
  };
  const listContext = {
    console, currentStage: 'prepared', currentId: null, DOC_TYPES: {}, STATUS_CLASS: {}, STATUS_LABEL: {},
    STAGE_STATUS: { prepared: 'prepared' },
    $: (id) => nodes[id] || (nodes[id] = { value: "", checked: false, addEventListener() {} }),
    document: { querySelectorAll: (selector) => selector === '.mchk:checked' ? nodes.listBody.checks.filter((cb) => cb.checked) : [] },
    fetch: async () => ({ json: async () => ({ items: rowData }) }),
    queueViewModel: (items) => ({ vouchers: items, emptyText: 'empty' }),
    esc: (value) => String(value ?? ''), nf: (value) => String(value ?? ''),
    updateMergeBtn() { nodes.btnMerge.disabled = nodes.listBody.checks.filter((cb) => cb.checked).length < 2; },
    refreshStats() {}, openReview() {}, encodeURIComponent,
  };
  vm.createContext(listContext);
  vm.runInContext(source.slice(listStart, listEnd) + '\nglobalThis.testLoadList = loadList;', listContext);
  await listContext.testLoadList();
  nodes.listBody.checks.forEach((cb) => { cb.checked = true; });
  rowData = [{ id: 2 }, { id: 3 }];
  await listContext.testLoadList();
  return nodes.listBody.checks.filter((cb) => cb.checked).map((cb) => cb.dataset.id);
}
(async () => {
  context.responseByUrl = responseByUrl;
  const api = context.testApi;
  responseByUrl['/api/import-jobs/a'] = { body: { id: 'a', status: 'running', phase: 'ocr', total_pages: 3, done_pages: 1, pages: [
    { id: 10, file_name: 'a.pdf', page_no: 1, status: 'completed', phase: 'layout', source_page_id: 90, voucher_ids: [91] },
    { id: 11, file_name: 'a.pdf', page_no: 2, status: 'completed', source_page_id: 92, voucher_ids: [] },
    { id: 12, file_name: 'a.pdf', page_no: 3, status: 'failed', source_page_id: 93, voucher_ids: [94] },
  ] } };
  responseByUrl['/api/import-jobs/b'] = { body: { id: 'b', status: 'running', total_pages: 2, done_pages: 0, pages: [] } };
  responseByUrl['/api/import-jobs/failed-job'] = { body: { id: 'failed-job', status: 'partial_failed', total_pages: 1, done_pages: 1, pages: [] } };
  api.trackImportJob({ id: 'a', status: 'running' }, [{ name: 'image.png', type: 'image/png' }]);
  api.trackImportJob({ id: 'a', status: 'running' }, [{ name: 'image.png', type: 'image/png' }]);
  api.trackImportJob({ id: 'b', status: 'running' });
  api.trackImportJob({ id: 'failed-job', status: 'partial_failed' });
  responseByUrl['/api/import-jobs'] = { body: { items: [{ id: 'restored', status: 'interrupted' }] } };
  responseByUrl['/api/import-jobs/restored'] = { body: { id: 'restored', status: 'interrupted', pages: [] } };
  await api.restoreImportJobs();
  await api.pollImportJob('a'); await api.pollImportJob('b');
  const afterFirst = events.filter((x) => x === 'loadList').length;
  await api.pollImportJob('a');
  const afterDuplicate = events.filter((x) => x === 'loadList').length;
  responseByUrl['/api/import-jobs/awaiting-review'] = { body: { id: 'awaiting-review', status: 'awaiting_review', total_pages: 1, done_pages: 1, pages: [
    { id: 13, file_name: 'review.png', page_no: 1, status: 'preview_ready', phase: 'awaiting_review', source_page_id: 95, voucher_ids: [96] },
  ] } };
  api.trackImportJob({ id: 'awaiting-review', status: 'awaiting_review' });
  await api.pollImportJob('awaiting-review');
  const awaitingReviewStoppedPolling = !importJobTimers.has('awaiting-review');
  const pollReviewClicks = events.filter((event) => Array.isArray(event) && (event[0] === 'setStage' || event[0] === 'openReview'));
  const dirtyPreserved = context.recognitionDirty;
  const reviewButtons = jobNode.querySelectorAll('[data-import-review-id]');
  reviewButtons.forEach((button) => button.handler());
  const dirtyClicksBlocked = events.filter((event) => typeof event === 'string' && event.startsWith('alert:請先儲存目前修改')).length === 3;
  context.recognitionDirty = false;
  reviewButtons.forEach((button) => button.handler());
  const reviewClicks = events.filter((event) => Array.isArray(event) && (event[0] === 'setStage' || event[0] === 'openReview'));
  responseByUrl['/api/import-jobs/a/retry-failed'] = { body: { ok: true } };
  await api.actOnImportJob('a', 'retry-failed');
  responseByUrl['/api/import-jobs/a/cancel'] = { body: { ok: true } };
  await api.actOnImportJob('a', 'cancel');
  const normalHtml = jobNode.innerHTML;
  const timersBeforeLogout = Array.from(importJobTimers.keys());
  const restoredStatus = importJobs.get('restored')?.status;
  const jobIds = Array.from(importJobs.keys());
  const seenIds = Array.from(importSeenPages.get('a') || []);
  importJobs.set('expired', { id: 'expired', status: 'running', files: new Map() });
  responseByUrl['/api/import-jobs/expired'] = { status: 401, ok: false, body: { detail: 'unauthorized' } };
  await api.pollImportJob('expired');
  importJobs.set('missing', { id: 'missing', status: 'running', files: new Map() });
  responseByUrl['/api/import-jobs/missing'] = { status: 404, ok: false, body: { detail: 'missing' } };
  await api.pollImportJob('missing');
  const authHtml = jobNode.innerHTML;
  importJobs.set('late', { id: 'late', status: 'running', files: new Map() });
  let resolveLate;
  context.fetch = () => new Promise((resolve) => { resolveLate = resolve; });
  const latePoll = api.pollImportJob('late');
  await Promise.resolve();
  api.stopImportPolling();
  resolveLate({ ok: true, status: 200, json: async () => ({ id: 'late', status: 'running', pages: [] }) });
  await latePoll;
  process.stdout.write(JSON.stringify({
    requests, timers: Array.from(timers.values()).map((t) => t.delay), timerJobIds: timersBeforeLogout,
    jobIds, restoredStatus, seen: seenIds,
    loadListAfterPage: afterFirst, loadListAfterDuplicate: afterDuplicate,
    awaitingReviewStoppedPolling,
    dirtyPreserved, dirtyClicksBlocked, currentIdPreserved: context.currentId, createdUrls, revokedUrls,
    staleTimerAfterLogout: importJobTimers.has('late'), authHtml, html: normalHtml, reviewButtonIds: reviewButtons.map((button) => Number(button.dataset.importReviewId)), reviewClicks,
    pollReviewClicks, selectedIdsAfterRefresh: await runListSelectionScenario(),
  }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
