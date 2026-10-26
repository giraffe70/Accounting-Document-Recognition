(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[<>&"]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
  const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
  const modeLabel = {vlm_direct:"VLM 直讀", ocr_vlm:"OCR＋VLM", ocr_llm:"OCR＋純文字 LLM"};
  const statusLabel = {queued:"排隊中",running:"執行中",cancelling:"取消中",cancelled:"已取消",interrupted:"已中斷",terminated:"已終止",completed:"已完成",succeeded:"成功",partial:"部分完成／待分割",skipped:"來源附頁／略過",failed:"失敗",timeout:"逾時"};
  const qualityStateLabel = {usable:"可用",needs_review:"待覆核",insufficient:"資訊不足"};
  const contentKindLabel = {supporting:"佐證文件",non_voucher:"非憑證頁",document:"單據",unknown:"未判定",blank:"空白頁"};
  const processingReasonLabel = {manual_split:"等待人工分割",page_review:"頁面角色／單張判斷待覆核",document_partial_failure:"單據部分擷取失敗",insufficient_data:"擷取完成但資料不足",completed:"擷取完成",skipped:"已略過",execution_failure:"執行失敗",input_budget_insufficient:"輸入預算不足"};
  function statusText(data) {
    return data.status === "partial" && processingReasonLabel[data.processing_reason] ? processingReasonLabel[data.processing_reason] : statusLabel[data.status] || data.status;
  }
  const fallbackFieldLabels = {doc_type:"單據類型",voucher_id:"單號",date:"日期",seller_name:"賣方／收款方",seller_tax_id:"賣方統編",buyer_name:"買方／付款方",buyer_tax_id:"買方統編",subtotal:"未稅金額",tax_amount:"稅額",total_amount:"總金額",currency:"幣別",note:"備註"};
  let selectedFiles = [], selectedDataset = "", models = {structure:[],classify:[],text:[]};
  let documentTypes = {};
  let currentRun = "", pollTimer = null, comboSeq = 0;
  let runtimeReady = false, resultView = "latest", runtimeData = null, lastRuntimeCheck = 0;
  const hiddenKey = "validation-hidden-history-v1";
  let hiddenHistory = {datasets:[], runs:[]};
  try {
    const saved = JSON.parse(localStorage.getItem(hiddenKey) || "{}");
    if (Array.isArray(saved.datasets) && Array.isArray(saved.runs)) hiddenHistory = saved;
  } catch (_) { /* Browser storage can be unavailable; clearing still works for this page. */ }
  function saveHiddenHistory() {
    try { localStorage.setItem(hiddenKey, JSON.stringify(hiddenHistory)); } catch (_) { /* Keep in memory. */ }
    $("showHistory").classList.toggle("hidden", !hiddenHistory.datasets.length && !hiddenHistory.runs.length);
  }

  function toast(message) {
    $("toast").textContent = message; $("toast").classList.remove("hidden");
    setTimeout(() => $("toast").classList.add("hidden"), 2800);
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 401) { location.href = "/"; throw new Error("請先登入"); }
    const type = response.headers.get("content-type") || "";
    const data = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`);
    return data;
  }
  const jsonHeaders = {"Content-Type":"application/json"};

  async function init() {
    try {
      saveHiddenHistory();
      ensureRuntimeBanner(); ensureResultViewControl();
      if ($("startRun")) $("startRun").disabled = true;
      const [me, modelData, typeData] = await Promise.all([request("/api/me"), request("/api/models"), request("/api/doc-types").catch(() => ({types:{}}))]);
      $("userChip").textContent = `${me.name}（${me.role}）`;
      models = modelData;
      documentTypes = typeData.types || {};
      addCombo();
      await Promise.all([refreshRuntime(), loadDatasets(), loadRuns()]);
      pollTimer = setInterval(() => { loadRuns(); if (Date.now() - lastRuntimeCheck > 15000) refreshRuntime(); }, 3000);
    } catch (error) { toast(error.message); }
  }

  function ensureRuntimeBanner() {
    if ($("runtimeBanner")) return;
    const banner = document.createElement("div"); banner.id = "runtimeBanner"; banner.className = "hint warn";
    banner.setAttribute("role", "status"); banner.classList.add("hidden");
    const panel = $("startRun")?.closest(".panel") || $("startRun")?.parentElement;
    panel?.insertBefore(banner, panel.firstChild);
  }
  async function refreshRuntime() {
    lastRuntimeCheck = Date.now();
    try {
      const data = await request("/api/validation/runtime"); runtimeData = data;
      runtimeReady = Boolean(data.ready && !data.restart_required && data.schema_ready);
      const banner = $("runtimeBanner");
      if (banner) {
        const mismatch = data.restart_required || (data.loaded_version && data.disk_version && data.loaded_version !== data.disk_version);
        banner.textContent = runtimeReady
          ? `驗證環境就緒 · 協定 ${data.worker_protocol ?? "—"} · 版本 ${data.loaded_version ?? "—"}`
          : `驗證環境尚未就緒${mismatch ? "：載入版本與磁碟版本不一致" : data.token_budget?.ready === false ? `：輸入預算設定未就緒（${data.token_budget.reason || "請準備本機詞表"}）` : ""}。請完成設定並完整重新啟動服務，再載入此頁面。`;
        banner.classList.toggle("hidden", runtimeReady);
        banner.classList.toggle("warn", !runtimeReady);
      }
    } catch (error) {
      runtimeData = null; runtimeReady = false;
      const banner = $("runtimeBanner");
      if (banner) {
        banner.textContent = "無法確認驗證環境版本（runtime API 不存在或無法連線）。請更新並完整重新啟動服務，再重新載入此頁面。";
        banner.classList.remove("hidden");
      }
      console.error("validation runtime check failed", error);
    }
    const start = $("startRun");
    if (start) { start.disabled = !runtimeReady; start.title = runtimeReady ? "" : "驗證環境尚未就緒，請重新啟動服務並載入頁面"; }
    document.querySelectorAll(".resume-run,.retry-run").forEach(button => {
      button.disabled = !runtimeReady;
      if (!runtimeReady) button.title = "驗證環境尚未就緒，請重新啟動服務並載入頁面";
    });
  }
  function ensureResultViewControl() {
    if ($("resultView")) return;
    const actions = document.querySelector(".export-actions");
    if (!actions) return;
    const label = document.createElement("label"); label.className = "hint"; label.style.marginRight = "8px";
    label.innerHTML = `結果檢視 <select id="resultView" class="select"><option value="latest">最新結果</option><option value="history">完整歷史</option></select>`;
    actions.prepend(label);
    $("resultView").onchange = () => { resultView = $("resultView").value === "history" ? "history" : "latest"; updateExportLinks(); refreshCurrentRun(); };
  }
  function updateExportLinks() {
    if (!currentRun) return;
    const view = encodeURIComponent(resultView);
    $("exportJson").href = `/api/validation/runs/${currentRun}/export?format=json&view=${view}`;
    $("exportCsv").href = `/api/validation/runs/${currentRun}/export?format=csv&view=${view}`;
    $("exportZip").href = `/api/validation/runs/${currentRun}/export?format=zip&view=${view}`;
  }

  function takeFiles(list) {
    selectedFiles = Array.from(list || []);
    $("selectedCount").textContent = selectedFiles.length;
    $("uploadBtn").disabled = !selectedFiles.length;
    if (!$("datasetName").value && selectedFiles[0]) {
      const rel = selectedFiles[0].webkitRelativePath;
      $("datasetName").value = rel ? rel.split("/")[0] : `資料集 ${new Date().toLocaleDateString("zh-TW")}`;
    }
  }
  $("folderInput").addEventListener("change", e => takeFiles(e.target.files));
  $("filesInput").addEventListener("change", e => takeFiles(e.target.files));
  $("uploadBtn").addEventListener("click", async () => {
    const fd = new FormData();
    selectedFiles.forEach(file => fd.append("files", file, file.name));
    fd.append("paths", JSON.stringify(selectedFiles.map(file => file.webkitRelativePath || file.name)));
    fd.append("name", $("datasetName").value.trim());
    $("uploadBtn").disabled = true; $("uploadStatus").textContent = "上傳並建立頁面清單中…";
    try {
      const data = await request("/api/validation/datasets", {method:"POST", body:fd});
      selectedDataset = data.id;
      $("uploadStatus").textContent = `完成：${data.file_count} 個檔案、${data.page_count} 張憑證頁面、${data.errors.length} 個問題。`;
      selectedFiles = []; $("selectedCount").textContent = "0";
      await loadDatasets();
    } catch (error) { $("uploadStatus").textContent = `失敗：${error.message}`; }
    finally { $("uploadBtn").disabled = !selectedFiles.length; }
  });

  async function loadDatasets() {
    const data = await request("/api/validation/datasets");
    const visible = data.items.filter(ds => !hiddenHistory.datasets.includes(ds.id));
    if (selectedDataset && !visible.some(ds => ds.id === selectedDataset)) selectedDataset = "";
    if (!selectedDataset && visible.length) selectedDataset = visible[0].id;
    $("datasets").innerHTML = visible.length ? visible.map(ds => `
      <div class="dataset-card ${ds.id === selectedDataset ? "active" : ""}" data-id="${ds.id}">
        <strong>${esc(ds.name)}</strong>
        <div class="meta">${ds.file_count} 個檔案 · ${ds.page_count} 張憑證 · ${ds.errors.length} 個問題</div>
        <div class="meta">${new Date(ds.created_at).toLocaleString("zh-TW")}</div>
        ${ds.errors.length ? `<details class="meta"><summary>查看匯入問題</summary>${ds.errors.map(x=>`<div>${esc(x)}</div>`).join("")}</details>` : ""}
      </div>`).join("") : `<div class="hint">目前沒有顯示的資料集；可上傳新資料集或顯示歷史紀錄。</div>`;
    document.querySelectorAll(".dataset-card").forEach(card => card.onclick = () => {
      selectedDataset = card.dataset.id; loadDatasets();
    });
  }

  function options(values, selected) {
    return (values || []).map(value => `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(value)}</option>`).join("");
  }
  function addCombo(seed = {}) {
    comboSeq += 1;
    const id = seed.id || `combo-${comboSeq}`;
    const mode = seed.mode || "ocr_vlm";
    const textModels = models.text?.length ? models.text : models.classify;
    const structureValues = mode === "ocr_llm" ? textModels : models.structure;
    const classifyValues = mode === "ocr_llm" ? textModels : models.structure;
    const row = document.createElement("div");
    row.className = "combo-row"; row.dataset.id = id;
    row.innerHTML = `
      <label class="combo-control"><span>組合名稱</span><input class="select combo-name" value="${esc(seed.name || `模型組合 ${comboSeq}`)}"></label>
      <label class="combo-control"><span>辨識模式</span><select class="select combo-mode">
        <option value="vlm_direct" ${mode === "vlm_direct" ? "selected" : ""}>VLM 直讀</option>
        <option value="ocr_vlm" ${mode === "ocr_vlm" ? "selected" : ""}>OCR＋VLM</option>
        <option value="ocr_llm" ${mode === "ocr_llm" ? "selected" : ""}>OCR＋純文字 LLM</option>
      </select></label>
      <label class="combo-control"><span>欄位萃取模型</span><select class="select structure-model">${options(structureValues, seed.structure_model)}</select></label>
      <label class="combo-control"><span>單據分類模型</span><select class="select classify-model">${options(classifyValues, seed.classify_model || seed.structure_model)}</select></label>
      <button class="btn btn-sm remove-combo" title="刪除">✕</button>`;
    row.querySelector(".remove-combo").onclick = () => { if ($("combos").children.length > 1) row.remove(); };
    row.querySelector(".combo-mode").onchange = event => {
      const current = row.querySelector(".structure-model").value;
      const textModels = models.text?.length ? models.text : models.classify;
      const vals = event.target.value === "ocr_llm" ? textModels : models.structure;
      row.querySelector(".structure-model").innerHTML = options(vals, vals.includes(current) ? current : vals[0]);
      const classify = row.querySelector(".classify-model"), selected = classify.value;
      classify.innerHTML = options(vals, vals.includes(selected) ? selected : vals[0]);
    };
    $("combos").appendChild(row);
  }
  $("addCombo").onclick = () => addCombo();
  function collectCombos() {
    return Array.from(document.querySelectorAll(".combo-row")).map(row => ({
      id: row.dataset.id, name: row.querySelector(".combo-name").value.trim(),
      mode: row.querySelector(".combo-mode").value,
      structure_model: row.querySelector(".structure-model").value,
      classify_model: row.querySelector(".classify-model").value,
    }));
  }

  $("startRun").onclick = async () => {
    if (!runtimeReady) return toast("驗證環境尚未就緒，請重新啟動服務並重新載入頁面");
    if (!selectedDataset) return toast("請先選擇資料集");
    const payload = {dataset_id:selectedDataset, name:$("runName").value.trim(), combinations:collectCombos(),
      repeats:Number($("repeats").value), timeout_seconds:Number($("timeout").value),
      max_edge:Number($("maxEdge").value), warmup:$("warmup").checked};
    try {
      const run = await request("/api/validation/runs", {method:"POST",headers:jsonHeaders,body:JSON.stringify(payload)});
      toast("驗證批次已加入佇列"); currentRun = run.id; await loadRuns(); await openRun(run.id);
    } catch (error) { toast(error.message); }
  };
  $("refreshRuns").onclick = loadRuns;
  $("clearView").onclick = async () => {
    try {
      const [datasets, runs] = await Promise.all([
        request("/api/validation/datasets"), request("/api/validation/runs")]);
      hiddenHistory.datasets = [...new Set([...hiddenHistory.datasets, ...datasets.items.map(x => x.id)])];
      hiddenHistory.runs = [...new Set([...hiddenHistory.runs, ...runs.items.map(x => x.id)])];
      saveHiddenHistory();
      selectedDataset = ""; currentRun = ""; selectedFiles = [];
      $("folderInput").value = ""; $("filesInput").value = "";
      $("datasetName").value = ""; $("runName").value = "";
      $("selectedCount").textContent = "0"; $("uploadBtn").disabled = true;
      $("uploadStatus").textContent = "";
      $("resultsPanel").classList.add("hidden");
      await Promise.all([loadDatasets(), loadRuns()]);
      toast("畫面已清空；驗證資料與執行中的批次均未刪除或停止");
    } catch (error) { toast(error.message); }
  };
  $("showHistory").onclick = async () => {
    hiddenHistory = {datasets:[], runs:[]}; saveHiddenHistory();
    await Promise.all([loadDatasets(), loadRuns()]);
  };

  async function loadRuns() {
    try {
      const data = await request("/api/validation/runs");
      const visible = data.items.filter(run => !hiddenHistory.runs.includes(run.id));
      $("runs").innerHTML = visible.length ? visible.map(run => {
        const latest = run.latest_summary || {}, done = latest.done ?? run.done, total = latest.total ?? run.total;
        return `
        <div class="run-card">
          <div><div class="run-title">${esc(run.name)} <span class="status-pill ${run.status}">${statusLabel[run.status] || run.status}</span></div>
            <div class="mini-progress"><span style="width:${Math.round((total ? done / total : 0) * 100)}%"></span></div>
            <div class="run-meta">${esc(run.dataset_name)} · 已處理／排程 ${done}/${total} · 擷取完成 ${latest.succeeded ?? run.succeeded} · 失敗 ${latest.failed ?? run.failed} · 待分割 ${latest.manual_split_count ?? "—"} · 單據部分失敗 ${latest.document_partial_failure_count ?? "—"} · 略過 ${latest.skipped ?? "—"}${run.current ? ` · ${esc(run.current)}` : ""}</div>
          </div>
          <div class="run-actions">
            <button class="btn btn-sm view-run" data-id="${run.id}">查看</button>
            ${["queued","running"].includes(run.status) ? `<button class="btn btn-sm cancel-run" data-id="${run.id}">取消</button>` : ""}
            ${["cancelled","interrupted"].includes(run.status) ? `<button class="btn btn-sm resume-run" data-id="${run.id}" ${runtimeReady ? "" : "disabled title=\"驗證環境尚未就緒，請重新啟動服務並載入頁面\""}>續跑</button>` : ""}
            ${["queued","running","cancelling","cancelled","interrupted"].includes(run.status) ? `<button class="btn btn-sm terminate-run" data-id="${run.id}">終止批次</button>` : ""}
            ${((run.latest_summary?.failed || run.latest_summary?.partial) ?? run.failed) && !["terminated","queued","running","cancelling"].includes(run.status) ? `<button class="btn btn-sm retry-run" data-id="${run.id}" ${runtimeReady ? "" : "disabled title=\"驗證環境尚未就緒，請重新啟動服務並載入頁面\""}>重跑失敗</button>` : ""}
          </div>
        </div>`;
      }).join("") : `<div class="hint">目前沒有顯示的驗證批次。</div>`;
      document.querySelectorAll(".view-run").forEach(x => x.onclick = () => openRun(x.dataset.id));
      document.querySelectorAll(".cancel-run").forEach(x => x.onclick = () => runAction(x.dataset.id,"cancel"));
      document.querySelectorAll(".resume-run").forEach(x => x.onclick = () => runAction(x.dataset.id,"resume"));
      document.querySelectorAll(".terminate-run").forEach(x => x.onclick = () => {
        if (confirm("確定終止這次驗證批次？未處理項目將停止，已完成結果會保留；終止後不能續跑。"))
          runAction(x.dataset.id,"terminate");
      });
      document.querySelectorAll(".retry-run").forEach(x => x.onclick = () => runAction(x.dataset.id,"retry-failed"));
      if (currentRun) refreshCurrentRun();
    } catch (error) { console.error(error); }
  }
  async function runAction(id, action) {
    if (["resume", "retry-failed"].includes(action) && !runtimeReady) return toast("驗證環境尚未就緒，請重新啟動服務並重新載入頁面");
    try { await request(`/api/validation/runs/${id}/${action}`, {method:"POST"}); toast("操作已送出"); loadRuns(); }
    catch (error) { toast(error.message); }
  }

  async function openRun(id) {
    currentRun = id; $("resultsPanel").classList.remove("hidden");
    updateExportLinks();
    await refreshCurrentRun();
    $("resultsPanel").scrollIntoView({behavior:"smooth",block:"start"});
  }
  async function refreshCurrentRun() {
    if (!currentRun) return;
    try {
      const run = await request(`/api/validation/runs/${currentRun}`);
      const stats = resultView === "latest" ? (run.latest_statistics || []) : (run.statistics || []);
      const summary = resultView === "latest" ? (run.latest_summary || {}) : {};
      const reliable = resultView !== "latest" || (summary.integrity_ok === true && stats.length > 0 && stats.every(stat => stat.integrity_ok === true));
      $("runTitle").textContent = `${run.name}｜${statusLabel[run.status] || run.status}｜${resultView === "latest" ? "已處理／排程" : "歷史嘗試"} ${summary.done ?? run.done}/${summary.total ?? run.total}`;
      const qualitySummary = summary.quality || summary;
      const latestOverview = resultView === "latest" ? `<div class="hint">最新頁級狀態：已處理 ${summary.done ?? "—"}/${summary.total ?? "—"}（排程） · 擷取完成 ${summary.succeeded ?? "—"} · 等待人工分割 ${summary.manual_split_count ?? summary.partial ?? "—"} · 頁面判斷待覆核 ${summary.page_review_count ?? 0} · 單據部分擷取失敗 ${summary.document_partial_failure_count ?? "—"} · 擷取完成但資料不足 ${summary.insufficient_page_count ?? "—"} · 執行失敗 ${summary.failed ?? "—"} · 略過 ${summary.skipped ?? "—"} · 單據 ${summary.document_count ?? "—"} · 欄位資訊不足 ${qualitySummary.insufficient_count ?? "—"} · 定位問題 ${qualitySummary.location_issue_count ?? "—"} · 待覆核 ${qualitySummary.needs_review_count ?? "—"}</div>${!reliable ? `<div class="hint warn">此舊批次的版本或結果契約不完整，完成率無法可靠評估。</div>` : ""}` : `<div class="hint">完整歷史嘗試；同一頁可能包含多次執行，不代表唯一頁面完成率。</div>`;
      $("stats").innerHTML = `<div class="statistics-overview">${latestOverview}</div>` + (stats.length ? stats.map(stat => `
        <div class="stat-card"><strong>${esc(stat.combo_name)}</strong><div class="hint">${esc(stat.actual_path)}</div>
          ${resultView === "latest" && stat.path_counts ? `<div class="hint">實際路徑：${Object.entries(stat.path_counts).map(([path, count]) => `${esc(path)} ${count} 頁`).join(" · ")}</div>` : ""}
          ${resultView === "latest" ? `<div>擷取完成率（已處理非略過頁） ${stat.completed_rate == null ? (stat.integrity_ok && stat.skipped > 0 && stat.processed_samples === stat.skipped ? "無需擷取" : "無法可靠評估") : (stat.completed_rate*100).toFixed(1)+"%"}</div><div>已處理 ${stat.processed_samples ?? "—"} · 擷取完成 ${stat.extraction_completed ?? stat.succeeded ?? "—"} · 排程 ${stat.scheduled_samples ?? stat.selected_samples ?? "—"}</div>` : `<div>歷史執行成功率 ${stat.completed_rate == null ? "無法可靠評估" : (stat.completed_rate*100).toFixed(1)+"%"}</div>`}
          <div>單據 ${stat.document_count ?? "—"} · 資訊不足 ${stat.insufficient_count ?? "—"}</div>
          <div>定位問題 ${stat.location_issue_count ?? "—"} · 待覆核 ${stat.needs_review_count ?? "—"}</div>
          <div>警告頁 ${stat.warning_page_count ?? "—"} · 警告單據 ${stat.warning_document_count ?? "—"}</div>
          <div>品質未評估 ${stat.quality_unassessed_count ?? "—"}</div>
          <div>首筆 ${fmt(stat.first_seconds)}s · 平均 ${fmt(stat.average_seconds)}s</div>
          <div>P50 ${fmt(stat.p50_seconds)}s · P95 ${fmt(stat.p95_seconds)}s</div>
          <div>吞吐 ${fmt(stat.throughput_per_minute)} 張／分</div></div>`).join("") : `<div class="hint">等待結果中…</div>`);
      const filter = $("comboFilter"), old = filter.value;
      filter.innerHTML = `<option value="">所有模型</option>` + run.settings.combinations.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
      filter.value = old;
      await loadResults();
    } catch (error) { console.error(error); }
  }
  async function loadResults() {
    if (!currentRun) return;
    const qs = new URLSearchParams({page_size:"500", view:resultView});
    if ($("comboFilter").value) qs.set("combo_id", $("comboFilter").value);
    if ($("statusFilter").value) qs.set("status", $("statusFilter").value);
    if ($("sourceFilter").value.trim()) qs.set("source", $("sourceFilter").value.trim());
    const data = await request(`/api/validation/runs/${currentRun}/results?${qs}`);
    $("results").innerHTML = data.items.length ? data.items.map(item => `
      <div class="result-row" role="listitem" data-id="${item.id}"><div class="source">${esc(item.relative_path)}${item.page_no > 1 ? ` p${item.page_no}` : ""}</div>
        <div class="meta">${esc(item.combo_name)} · ${modeLabel[item.mode]} · 第 ${item.repeat_no || "暖機"} 輪</div>
        <div><span class="status-pill ${item.status}">${esc(statusText(item))}</span> <span class="meta">${Array.isArray(item.documents) ? `· ${item.documents.length} 張單據` : ""} ${item.actual_path ? esc(item.actual_path) : ""} ${item.timings?.complete_seconds != null ? `· 總耗時 ${fmt(item.timings.complete_seconds)}s` : ""}</span></div>
        ${item.processing_reason ? `<div class="meta processing-reason">${esc(processingReasonLabel[item.processing_reason] || item.processing_reason)}</div>` : ""}
      </div>`).join("") : `<div class="hint">沒有符合條件的結果。</div>`;
    document.querySelectorAll(".result-row").forEach(row => row.onclick = () => showDetail(row.dataset.id, row));
  }
  $("comboFilter").onchange = loadResults; $("statusFilter").onchange = loadResults;
  let searchDelay; $("sourceFilter").oninput = () => { clearTimeout(searchDelay); searchDelay=setTimeout(loadResults,300); };

  function valueText(value) {
    if (value == null || value === "") return "—";
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }
  function timingBlock(timings, version, heading) {
    const fields = [
      ["orientation_ocr_seconds", "方向判斷 OCR"], ["full_ocr_seconds", "完整 OCR"],
      ["supplement_ocr_seconds", "補讀 OCR"], ["model_seconds", "模型"],
      ["orientation_model_seconds", "方向模型"], ["orientation_model_init_seconds", "方向模型初始化"],
      ["resolution_assessment_seconds", "解析度評估"], ["first_result_seconds", "首筆結果"],
      ["ocr_cache_seconds", "OCR 快取"], ["ocr_init_seconds", "OCR 初始化"],
      ["ocr_cache_hits", "OCR 快取命中"], ["ocr_cache_misses", "OCR 快取未命中"],
      ["queue_seconds", "排隊"], ["complete_seconds", "總耗時"],
    ];
    const recorded = timings && typeof timings === "object";
    const rows = fields.map(([key, label]) => {
      const legacyTotal = key === "queue_seconds" || key === "complete_seconds";
      const v3Only = ["orientation_model_seconds", "orientation_model_init_seconds", "resolution_assessment_seconds", "first_result_seconds"].includes(key);
      const value = recorded && ([2, 3].includes(version) || legacyTotal) &&
        (version === 3 || !v3Only) &&
        timings[key] != null ? timings[key] : null;
      const formatted = value == null || value === "" || !Number.isFinite(Number(value))
        ? (version === 2 && v3Only ? "—" : "未記錄")
        : (key.endsWith("_seconds") ? `${fmt(value)} 秒` : String(value));
      return `<tr><th>${esc(label)}</th><td>${esc(formatted)}</td></tr>`;
    }).join("");
    const incompleteNote = timings?.incomplete === true ? `<div class="hint">處理曾中斷，細項僅包含已完成的呼叫，耗時可能未完整記錄。</div>` : "";
    return `<div class="detail-box timing-block"><h4>${esc(heading)}</h4><table class="data-table"><tbody>${rows}</tbody></table><div class="hint">頁面總耗時不含排隊；OCR 初始化時間已包含在實際 OCR 時間內，不應重複加總。</div>${incompleteNote}</div>`;
  }
  function table(items) {
    if (!items?.length) return `<div class="hint">無資料</div>`;
    const keys = [...new Set(items.flatMap(item => Object.keys(item)))];
    return `<table class="data-table"><thead><tr>${keys.map(k=>`<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>${items.map(item=>`<tr>${keys.map(k=>`<td>${esc(valueText(item[k]))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }
  function evidencePanel(artifacts, imageId, fallbackLabel = "此結果沒有可顯示的證據圖片。") {
    const list = Array.isArray(artifacts) ? artifacts : [];
    const available = ["original", "input", "annotated"].map(kind => list.find(a => a.kind === kind && a.url)).filter(Boolean);
    if (!available.length) return `<div class="hint">${fallbackLabel}</div>`;
    const labels = {original:"原始頁圖",input:"實際輸入圖",annotated:"含框圖片"};
    const selected = available.find(a => a.kind === "annotated") || available.find(a => a.kind === "input") || available[0];
    return `<div class="evidence-tabs">${available.map((a,i)=>`<button class="btn btn-sm evidence-tab ${a===selected?"primary":""}" data-target="${imageId}" data-url="${esc(a.url)}">${labels[a.kind]}</button>`).join("")}</div><div class="evidence-view"><img id="${imageId}" src="${esc(selected.url)}" alt="辨識證據"></div>`;
  }
  function bindEvidenceTabs() {
    document.querySelectorAll(".evidence-tab").forEach(tab => tab.onclick = () => {
      const image = $(tab.dataset.target || "evidenceImage");
      if (image) image.src = tab.dataset.url;
      tab.parentElement.querySelectorAll(".evidence-tab").forEach(x=>x.classList.remove("primary"));
      tab.classList.add("primary");
    });
  }
  function qualityBlock(quality, missing = "品質未評估（舊版結果未提供 quality）") {
    if (!quality) return `<div class="hint">${missing}</div>`;
    const issues = quality.issues?.length ? `<ul>${quality.issues.map(issue=>`<li><strong>${esc(issue.severity || "提示")} · ${esc(issue.category || issue.code || "品質")}</strong>：${esc(issue.message || issue.code || "")}${issue.path ? ` <span class="hint">(${esc(issue.path)})</span>` : ""}</li>`).join("")}</ul>` : `<div class="hint">無品質問題明細</div>`;
    return `<div class="quality-summary">品質：${esc(qualityStateLabel[quality.state] || quality.state || "未判定")} · 內容：${esc(contentKindLabel[quality.content_kind] || quality.content_kind || "未判定")}</div>${issues}`;
  }
  function reasonLabel(data, doc = null) {
    const reason = doc?.processing_reason || data.processing_reason;
    if (processingReasonLabel[reason]) return processingReasonLabel[reason];
    if (reason) return reason;
    if (data.status === "partial") return "等待人工分割或單據尚未完成";
    if (data.status === "succeeded" && (doc?.quality || data.quality)?.state === "insufficient") return "擷取完成但資料不足";
    return "";
  }
  function contractFields(typeKey) {
    return documentTypes[typeKey]?.fields || [];
  }
  function fieldLabel(key, specs = []) {
    return specs.find(field => field.key === key)?.label || fallbackFieldLabels[key] || key;
  }
  function displayFieldRows(voucher, specs) {
    const data = voucher.fields || {};
    const nested = {buyer_name:voucher.buyer?.name, buyer_tax_id:voucher.buyer?.tax_id,
      seller_name:voucher.seller?.name, seller_tax_id:voucher.seller?.tax_id};
    const known = [
      {key:"doc_type", value:voucher.doc_type, label:"單據類型"},
      ...specs.map(spec => ({key:spec.key, value:data[spec.key] ?? nested[spec.key] ?? voucher[spec.key], label:spec.label || fieldLabel(spec.key)})),
    ];
    const seen = new Set(known.map(item => item.key));
    const common = {voucher_id:voucher.voucher_id,date:voucher.date,total_amount:voucher.total_amount,currency:voucher.currency,...nested};
    const extra = Object.entries({...common, ...data}).filter(([key, value]) => !seen.has(key) && value != null)
      .map(([key, value]) => ({key, value, label:fieldLabel(key, specs)}));
    const values = [...known, ...extra];
    return values.map(item => `<tr data-field="${esc(item.key)}"><th>${esc(item.label)}</th><td>${esc(valueText(item.value))}</td></tr>`).join("");
  }
  function labeledTable(items, specs) {
    if (!items?.length) return `<div class="hint">無資料</div>`;
    const keys = [...new Set(items.flatMap(item => Object.keys(item)))];
    const labelFor = key => specs.find(field => field.key === key)?.label || fieldLabel(key);
    return `<table class="data-table"><thead><tr>${keys.map(key=>`<th>${esc(labelFor(key))}</th>`).join("")}</tr></thead><tbody>${items.map(item=>`<tr>${keys.map(key=>`<td>${esc(valueText(item[key]))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }
  function bindPageViewer() {
    const stage = $("pageImageStage"), canvas = $("pageImageCanvas"), image = $("pageOriginalImage");
    if (!stage || !canvas || !image) return;
    let zoom = 1, panX = 0, panY = 0, dragging = false, startX = 0, startY = 0;
    const paint = () => {
      canvas.style.transform = `translate(calc(-50% + ${panX}px), calc(-50% + ${panY}px)) scale(${zoom})`;
      const readout = $("pageZoomReadout"); if (readout) readout.textContent = `${Math.round(zoom * 100)}%`;
    };
    const fit = () => {
      if (!image.naturalWidth || !image.naturalHeight) return;
      const rect = stage.getBoundingClientRect();
      const fitScale = Math.min((rect.width - 28) / image.naturalWidth, (rect.height - 28) / image.naturalHeight);
      canvas.style.width = `${image.naturalWidth * fitScale}px`;
      canvas.style.height = `${image.naturalHeight * fitScale}px`;
      image.style.width = "100%"; image.style.height = "100%";
      zoom = 1; panX = 0; panY = 0; paint();
    };
    image.addEventListener("load", fit, {once:true});
    if (image.complete) fit();
    $("pageZoomIn")?.addEventListener("click", () => { zoom = Math.min(zoom * 1.25, 8); paint(); });
    $("pageZoomOut")?.addEventListener("click", () => { zoom = Math.max(zoom / 1.25, 0.2); paint(); });
    $("pageFit")?.addEventListener("click", fit);
    $("pageBoxToggle")?.addEventListener("change", event => { const svg = $("pageRegionSvg"); if (svg) svg.hidden = !event.target.checked; });
    stage.addEventListener("pointerdown", event => { if (event.target.closest(".document-region")) return; dragging = true; startX = event.clientX - panX; startY = event.clientY - panY; stage.setPointerCapture(event.pointerId); });
    stage.addEventListener("pointermove", event => { if (!dragging) return; panX = event.clientX - startX; panY = event.clientY - startY; paint(); });
    stage.addEventListener("pointerup", () => { dragging = false; });
    stage.addEventListener("pointercancel", () => { dragging = false; });
  }
  function setMobileReviewView(view) {
    const workbench = $("reviewWorkbench"); if (workbench) workbench.dataset.mobileView = view;
    document.querySelectorAll(".review-mobile-tabs button").forEach(button => button.classList.toggle("active", button.dataset.mobileView === view));
  }
  function bindReviewMobileTabs() {
    document.querySelectorAll(".review-mobile-tabs button").forEach(button => button.addEventListener("click", () => setMobileReviewView(button.dataset.mobileView)));
  }
  function pageWorkbench(data, documents, selectedIndex, diagnosticControl) {
    const pageArtifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
    const pageOriginal = pageArtifacts.find(item => item.kind === "original" && item.url);
    const pageFallback = pageOriginal || pageArtifacts.find(item => item.url && ["input","annotated"].includes(item.kind));
    const docs = documents || [];
    const selectedDoc = docs[selectedIndex];
    const regions = docs.map((doc, index) => {
      const region = doc.origin?.region || doc.voucher?.origin?.region;
      if (!Array.isArray(region) || region.length !== 4 || region.some(value => !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 1000)) return "";
      const [x0,y0,x1,y1] = region.map(Number);
      if (x1 <= x0 || y1 <= y0) return "";
      const label = `單據 ${Number(doc.document_index ?? index)+1}`;
      return `<rect class="document-region ${index===selectedIndex?"active":""}" data-document-index="${index}" x="${x0}" y="${y0}" width="${x1-x0}" height="${y1-y0}" aria-label="${label}" tabindex="0"><title>${label}</title></rect>`;
    }).join("");
    const picker = docs.map((doc,index) => `<button type="button" class="btn btn-sm document-choice ${index===selectedIndex?"active":""}" data-document-index="${index}">單據 ${Number(doc.document_index ?? index)+1} · ${esc(doc.voucher?.doc_type || "未分類")}${doc.voucher?.voucher_id ? ` · ${esc(doc.voucher.voucher_id)}` : ""}</button>`).join("");
    const activeMessage = selectedDoc ? "點選原頁框線切換單據，欄位內容會同步更新。" : (reasonLabel(data) || "此頁尚無正式單據結果。");
    const originalNotice = pageOriginal ? "原始頁面" : (pageFallback ? `原頁影像未提供，暫以${pageFallback.kind === "input" ? "頁面輸入圖" : "標記圖"}顯示` : "此結果沒有頁級影像。");
    let pageImage = pageFallback ? `<img id="pageOriginalImage" src="${esc(pageFallback.url)}" alt="${esc(originalNotice)}" draggable="false"><svg id="pageRegionSvg" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="單據區域框線">${regions}</svg>` : `<div class="hint">${esc(originalNotice)}</div>`;
    const statusReason = reasonLabel(data, selectedDoc);
    const selectedVoucher = selectedDoc?.voucher || {};
    const typeKey = selectedVoucher.doc_type_key || selectedVoucher.type_key;
    const typeSpec = documentTypes[typeKey] || {};
    const fieldSpecs = contractFields(typeKey);
    const itemSpecs = typeSpec.item_fields || [];
    const entrySpecs = typeSpec.entry_fields || [];
    const docArtifacts = Array.isArray(selectedDoc?.artifacts) ? selectedDoc.artifacts : [];
    const docOriginal = docArtifacts.find(item => item.kind === "original" && item.url);
    const docInput = docArtifacts.find(item => item.kind === "input" && item.url);
    const sentInputs = docArtifacts.filter(item => /^model_input_\d+$/.test(item.kind) && item.url);
    const inputLabel = data.mode === "ocr_llm" ? "OCR 對照圖（未傳送圖片至模型）" : "模型輸入圖";
    const docImagePanel = [docOriginal,docInput].filter(Boolean).length ? `<details class="detail-box"><summary>單據影像（裁切圖／${esc(inputLabel)}）</summary>${[docOriginal,docInput].filter(Boolean).map(item=>`<div class="document-artifact-preview"><h4>${item.kind==="original"?"單據原圖（裁切圖）":esc(inputLabel)}</h4><a href="${esc(item.url)}" target="_blank" rel="noopener"><img src="${esc(item.url)}" alt="${item.kind==="original"?"單據裁切圖":esc(inputLabel)}"></a></div>`).join("")}</details>` : "";
    const selectedDetail = selectedDoc ? `<div class="detail-box"><h3>${esc(typeSpec.label || selectedVoucher.doc_type || "單據結果")}</h3>${statusReason ? `<div class="processing-reason">${esc(statusReason)}</div>` : ""}${selectedDoc.error ? `<div class="hint warn">${esc(selectedDoc.error)}</div>` : ""}${qualityBlock(selectedDoc.quality || selectedVoucher.quality)}</div>
      ${timingBlock(selectedDoc.timings, selectedDoc.timings_version ?? data.timings_version ?? data.timings?.timings_version, "選取單據耗時")}
      <div class="detail-box"><h4>欄位</h4><table class="document-field-table"><tbody>${displayFieldRows(selectedVoucher, fieldSpecs)}</tbody></table></div>
      <div class="detail-box"><h4>明細</h4>${labeledTable(selectedVoucher.items, itemSpecs)}</div><div class="detail-box"><h4>分錄</h4>${labeledTable(selectedVoucher.entries, entrySpecs)}</div>${docImagePanel}
      <details class="detail-box"><summary>技術診斷</summary><details class="detail-box"><summary>欄位定位、轉換資訊與分類證據</summary><pre class="diagnostic-pre">${esc(JSON.stringify({origin:selectedDoc.origin || selectedVoucher.origin,boxes:selectedDoc.boxes || selectedVoucher.boxes,coordinate_context:selectedDoc.coordinate_context || selectedVoucher.coordinate_context,image_transforms:selectedDoc.image_transforms || selectedVoucher.image_transforms,classification_evidence:selectedDoc.classification_evidence || selectedVoucher.classification_evidence},null,2))}</pre></details>
      <details class="detail-box"><summary>輸入預算、補讀診斷與模型追蹤</summary><pre class="diagnostic-pre">${esc(JSON.stringify({recognition_diagnostics:selectedDoc.recognition_diagnostics || selectedVoucher.recognition_diagnostics,ocr:selectedDoc.ocr,trace:selectedDoc.trace},null,2))}</pre></details>
      ${sentInputs.length ? `<details class="detail-box"><summary>各輪實際模型輸入圖（包含補讀）</summary>${sentInputs.map((item,index)=>`<div class="document-artifact-preview"><h4>模型輸入 ${index+1}</h4><a href="${esc(item.url)}" target="_blank" rel="noopener"><img src="${esc(item.url)}" loading="lazy" alt="模型輸入 ${index+1}"></a></div>`).join("")}</details>` : ""}
      <details class="detail-box"><summary>完整單據 JSON</summary><pre class="diagnostic-pre">${esc(JSON.stringify({voucher:selectedVoucher,origin:selectedDoc.origin,quality:selectedDoc.quality,processing_reason:selectedDoc.processing_reason},null,2))}</pre></details></details>` : `<div class="detail-box"><strong>${esc(statusReason || "尚無可顯示的擷取結果")}</strong>${data.error ? `<div class="hint warn">${esc(data.error)}</div>` : ""}${qualityBlock(data.quality)}</div>`;
    const tabs = `<div class="review-mobile-tabs"><button class="btn btn-sm active" type="button" data-mobile-view="image">圖片</button><button class="btn btn-sm" type="button" data-mobile-view="data">資料</button></div>`;
    const pageTech = `${timingBlock(data.timings, data.timings_version ?? data.timings?.timings_version, "頁面耗時")}<details class="detail-box"><summary>頁面 OCR 與模型追蹤</summary><pre class="diagnostic-pre">${esc(JSON.stringify({ocr:data.ocr,trace:data.trace},null,2))}</pre></details>${diagnosticControl || ""}`;
    return `${tabs}<div id="reviewWorkbench" class="review-workbench" data-mobile-view="image"><section class="review-image-panel"><div class="review-image-toolbar"><span class="hint">${esc(originalNotice)}</span><button id="pageZoomOut" type="button" class="btn btn-sm" aria-label="縮小">－</button><span id="pageZoomReadout" class="zoom-readout">100%</span><button id="pageZoomIn" type="button" class="btn btn-sm" aria-label="放大">＋</button><button id="pageFit" type="button" class="btn btn-sm">適合畫面</button><label class="btn btn-sm"><input id="pageBoxToggle" type="checkbox" checked> 顯示單據框</label></div><div id="pageImageStage" class="evidence-view page-image-stage"><div id="pageImageCanvas">${pageImage}</div></div>${pageTech}</section><aside class="review-document-panel"><div class="detail-box"><div class="detail-head"><h3>單據資料</h3><span class="status-pill ${esc(data.status)}">${esc(statusText(data))}</span></div><div class="hint">${esc(activeMessage)}</div><div class="document-picker">${picker || `<div class="hint">目前沒有單據可供選取。</div>`}</div>${selectedDetail}</div></aside></div>`;
  }
  async function showDetail(id, row) {
    document.querySelectorAll(".result-row").forEach(x=>x.classList.remove("active")); row?.classList.add("active");
    $("detail").innerHTML = `<div class="detail-empty">載入中…</div>`;
    try {
      const data = await request(`/api/validation/results/${id}`);
      const documents = Array.isArray(data.documents) ? data.documents : [];
      const integrityText = data.integrity == null ? "完整性未提供" : (typeof data.integrity === "object" ? JSON.stringify(data.integrity) : String(data.integrity));
      const diagnosticControl = data.diagnostics_available ? `<div class="detail-box"><strong>歷史診斷</strong><div class="hint">結果完整性：${esc(integrityText)} · 保存的裁切結果／未驗收，不納入單據結果、統計或品質重判。</div><button class="btn btn-sm saved-diagnostics" data-id="${esc(id)}">查看保存的裁切結果</button><div class="saved-diagnostics-content"></div></div>` : "";
      const displayDocuments = documents.length ? documents : (data.voucher && Object.keys(data.voucher).length ? [{id:data.id,document_index:0,status:data.status,voucher:data.voucher,origin:data.origin,error:data.error,trace:data.trace,ocr:data.ocr,boxes:data.boxes,quality:data.quality,artifacts:data.document_artifacts}] : []);
      const renderWorkbench = (index, mobileView = "image") => {
        const reviewState = displayDocuments.length ? "待人工核對" : (reasonLabel(data) || statusLabel[data.status] || data.status);
        $("detail").innerHTML = `<div class="detail-head"><div><h3>${esc(data.relative_path)}${data.page_no>1?` p${data.page_no}`:""}</h3><div class="hint">${esc(data.combo_name)} · ${modeLabel[data.mode] || data.mode || "—"} · 實際 ${esc(data.actual_path || "—")}</div></div><span class="status-pill ${esc(data.status)}">${esc(statusText(data))} · ${esc(reviewState)}</span></div>${pageWorkbench(data, displayDocuments, index, diagnosticControl)}`;
        bindPageViewer(); bindDiagnostics(); bindReviewMobileTabs(); setMobileReviewView(mobileView);
        document.querySelectorAll(".document-choice,.document-region").forEach(control => {
          control.addEventListener("click", () => {
            const next = Number(control.dataset.documentIndex);
            renderWorkbench(next, window.matchMedia("(max-width: 900px)").matches ? "data" : mobileView);
          });
          if (control.tagName.toLowerCase() === "rect") control.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); control.click(); } });
        });
      };
      renderWorkbench(0);
    } catch (error) { $("detail").innerHTML = `<div class="detail-box">${esc(error.message)}</div>`; }
  }

  function bindDiagnostics() {
    document.querySelectorAll(".saved-diagnostics").forEach(button => button.onclick = async () => {
      const container = button.parentElement.querySelector(".saved-diagnostics-content");
      button.disabled = true; container.innerHTML = `<div class="hint">讀取中…</div>`;
      try {
        const data = await request(`/api/validation/results/${encodeURIComponent(button.dataset.id)}/diagnostics`);
        const docs = Array.isArray(data.documents) ? data.documents : [];
        const saved = docs.map((doc, index) => {
          const origin = doc.origin || doc.voucher?.origin || {};
          const quality = doc.quality || doc.voucher?.quality;
          const docArtifacts = Array.isArray(doc.artifacts) ? doc.artifacts : (Array.isArray(origin.artifacts) ? origin.artifacts : []);
          const region = origin.region ? JSON.stringify(origin.region) : "未提供區域框";
          return `<details class="document-result"><summary>保存區域 ${index + 1} · ${esc(doc.status || "未驗收")} · ${esc(doc.voucher?.doc_type || "未分類")}</summary>
            <div class="detail-box"><strong>保存的裁切結果／未驗收</strong><div>來源頁 ${esc(origin.page_no ?? "—")} · 區域 ${esc(region)}${origin.rotation != null ? ` · 旋轉 ${esc(origin.rotation)}°` : ""}</div>${qualityBlock(quality)}</div>
            ${evidencePanel(docArtifacts, `savedDiagnostic${index}`, "此保存區域沒有可顯示的原圖或裁切圖。")}
            <details class="detail-box"><summary>保存結果 JSON（不重判）</summary><pre>${esc(JSON.stringify({origin,quality,voucher:doc.voucher,error:doc.error},null,2))}</pre></details></details>`;
        }).join("");
        const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
        container.innerHTML = `<div class="hint warn">保存的裁切結果／未驗收。此診斷只供追查，沒有加入正式 documents，也沒有重新計算品質。</div>${evidencePanel(artifacts,"savedDiagnosticPage","沒有保存的原始頁影像。")}${saved || `<div class="hint">沒有保存的裁切單據。</div>`}<details class="detail-box"><summary>保存的 layout 與 trace</summary><pre>${esc(JSON.stringify({layout:data.layout,trace:data.trace},null,2))}</pre></details>`;
        bindEvidenceTabs();
      } catch (error) { container.innerHTML = `<div class="hint warn">讀取診斷失敗：${esc(error.message)}</div>`; }
      finally { button.disabled = false; }
    });
  }

  window.addEventListener("resize", () => $("pageFit")?.click());
  init();
})();
