"use strict";

const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const source = input.source;

function sliceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end <= start) throw new Error(`Could not locate ${startMarker}`);
  return source.slice(start, end);
}

const statusHelper = sliceBetween("  function statusText(", "  const fallbackFieldLabels");
const qualityHelper = sliceBetween("  function qualityBlock(", "  function reasonLabel(");
const helpers = sliceBetween("  function reasonLabel(", "  async function showDetail(");
const showDetail = sliceBetween("  async function showDetail(", "  function bindDiagnostics(");
const refreshRun = sliceBetween("  async function refreshCurrentRun(", "  async function loadResults(");

class FakeNode {
  constructor(id = "") {
    this.id = id;
    this.dataset = {};
    this.style = {};
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.title = "";
    this.naturalWidth = id === "pageOriginalImage" ? 1200 : 640;
    this.naturalHeight = id === "pageOriginalImage" ? 1600 : 480;
    this.complete = true;
    this.isConnected = true;
    this.listeners = new Map();
    this.classList = {
      add() {}, remove() {},
      toggle() {}, contains() { return false; },
    };
    this._innerHTML = "";
  }
  set innerHTML(value) { this._innerHTML = String(value); }
  get innerHTML() { return this._innerHTML; }
  addEventListener(type, callback) {
    const values = this.listeners.get(type) || [];
    values.push(callback);
    this.listeners.set(type, values);
  }
  async click(event = {}) {
    const values = this.listeners.get("click") || [];
    for (const callback of values) await callback({
      target: this, preventDefault() {}, pointerId: 1, clientX: 0, clientY: 0, ...event,
    });
  }
  setPointerCapture() {}
  getBoundingClientRect() { return {width:800, height:600}; }
}

const nodes = new Map();
let parsedControlsHtml = null;
let parsedControls = [];
const $ = (id) => {
  if (!nodes.has(id)) nodes.set(id, new FakeNode(id));
  return nodes.get(id);
};
const document = {
  getElementById: $,
  querySelectorAll(selector) {
    if (selector === ".result-row" || selector === ".saved-diagnostics") return [];
    if (selector === ".document-choice,.document-region") {
      const html = $("detail").innerHTML;
      if (html === parsedControlsHtml) return parsedControls;
      const controls = [];
      const patterns = [
        /<button class="[^"]*document-choice[^"]*"[^>]*data-document-index="(\d+)"[^>]*>/g,
        /<rect class="[^"]*document-region[^"]*"[^>]*data-document-index="(\d+)"[^>]*>/g,
      ];
      for (const pattern of patterns) {
        let match;
        while ((match = pattern.exec(html))) {
          const control = new FakeNode();
          control.dataset.documentIndex = match[1];
          control.tagName = pattern.source.startsWith("<button") ? "BUTTON" : "rect";
          controls.push(control);
        }
      }
      parsedControlsHtml = html;
      parsedControls = controls;
      return controls;
    }
    return [];
  },
};

const runContext = {
  input, source, document, window: {matchMedia:()=>({matches:false}), addEventListener(){}},
  console, setTimeout, clearTimeout,
  $,
  esc: value => String(value ?? "").replace(/[<>&"]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c])),
  valueText: value => value == null || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value),
  fmt: value => value == null ? "—" : Number(value).toFixed(2),
  modeLabel: {ocr_vlm:"OCR＋VLM"},
  statusLabel: {succeeded:"成功",partial:"部分完成／待分割",failed:"失敗"},
  qualityStateLabel: {insufficient:"資訊不足"},
  contentKindLabel: {},
  processingReasonLabel: {manual_split:"等待人工分割",document_partial_failure:"單據部分擷取失敗",insufficient_data:"擷取完成但資料不足"},
  fallbackFieldLabels: {},
  documentTypes: input.docTypes,
  async request(url) { return String(url).includes("/runs/") ? input.run : input.result; },
  bindDiagnostics() {},
  async loadResults() {},
  resultView: "latest", currentRun: "synthetic-run",
};
vm.createContext(runContext);
vm.runInContext(`
${statusHelper}
${qualityHelper}
${helpers}
function bindDiagnostics() {}
${showDetail}
${refreshRun}
globalThis.testApi = {pageWorkbench, showDetail, refreshCurrentRun};
`, runContext);

(async () => {
  await runContext.testApi.showDetail("synthetic-result", {classList:{add(){}}});
  const initialHtml = $("detail").innerHTML;
  const initialCanvasTransform = $("pageImageCanvas").style.transform;
  await $("pageZoomIn").click();
  const zoomedCanvasTransform = $("pageImageCanvas").style.transform;
  const region = document.querySelectorAll(".document-choice,.document-region")
    .find(node => node.tagName === "rect" && node.dataset.documentIndex === "1");
  if (!region) throw new Error(`No second SVG region found in result HTML: ${initialHtml.slice(0, 1200)}`);
  await region.click();
  const selectedHtml = $("detail").innerHTML;
  const reasonHtml = ["manual_split", "document_partial_failure", "insufficient_data"].map(reason =>
    runContext.testApi.pageWorkbench({...input.result, processing_reason:reason, documents:[]}, [], 0, ""));
  await runContext.testApi.refreshCurrentRun();
  process.stdout.write(JSON.stringify({
    initialHtml,
    selectedHtml,
    reasonHtml,
    initialCanvasTransform,
    zoomedCanvasTransform,
    statsHtml: $("stats").innerHTML,
    runTitle: $("runTitle").textContent,
  }));
})().catch(error => { console.error(error); process.exitCode = 1; });
