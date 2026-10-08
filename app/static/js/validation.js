(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[<>&"]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
  const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
  const modeLabel = {vlm_direct:"VLM 直讀", ocr_vlm:"OCR＋VLM", ocr_llm:"OCR＋純文字 LLM"};
  const statusLabel = {queued:"排隊中",running:"執行中",cancelling:"取消中",cancelled:"已取消",interrupted:"已中斷",terminated:"已終止",completed:"已完成",succeeded:"成功",partial:"部分完成／待分割",skipped:"確認空白／略過",failed:"失敗",timeout:"逾時"};
  const qualityStateLabel = {usable:"可用",needs_review:"待覆核",insufficient:"資訊不足"};
  const contentKindLabel = {supporting:"佐證文件",document:"單據",unknown:"未判定",blank:"空白頁"};
  let selectedFiles = [], selectedDataset = "", models = {structure:[],classify:[],text:[]};
  let currentRun = "", pollTimer = null, comboSeq = 0;
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
      const [me, modelData] = await Promise.all([request("/api/me"), request("/api/models")]);
      $("userChip").textContent = `${me.name}（${me.role}）`;
      models = modelData;
      addCombo();
      await Promise.all([loadDatasets(), loadRuns()]);
      pollTimer = setInterval(loadRuns, 3000);
    } catch (error) { toast(error.message); }
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
      $("runs").innerHTML = visible.length ? visible.map(run => `
        <div class="run-card">
          <div><div class="run-title">${esc(run.name)} <span class="status-pill ${run.status}">${statusLabel[run.status] || run.status}</span></div>
            <div class="mini-progress"><span style="width:${Math.round(run.progress * 100)}%"></span></div>
            <div class="run-meta">${esc(run.dataset_name)} · 頁面進度 ${run.done}/${run.total} · 成功 ${run.succeeded} · 失敗 ${run.failed}${run.current ? ` · ${esc(run.current)}` : ""}</div>
          </div>
          <div class="run-actions">
            <button class="btn btn-sm view-run" data-id="${run.id}">查看</button>
            ${["queued","running"].includes(run.status) ? `<button class="btn btn-sm cancel-run" data-id="${run.id}">取消</button>` : ""}
            ${["cancelled","interrupted"].includes(run.status) ? `<button class="btn btn-sm resume-run" data-id="${run.id}">續跑</button>` : ""}
            ${["queued","running","cancelling","cancelled","interrupted"].includes(run.status) ? `<button class="btn btn-sm terminate-run" data-id="${run.id}">終止批次</button>` : ""}
            ${run.failed && !["terminated","queued","running","cancelling"].includes(run.status) ? `<button class="btn btn-sm retry-run" data-id="${run.id}">重跑失敗</button>` : ""}
          </div>
        </div>`).join("") : `<div class="hint">目前沒有顯示的驗證批次。</div>`;
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
    try { await request(`/api/validation/runs/${id}/${action}`, {method:"POST"}); toast("操作已送出"); loadRuns(); }
    catch (error) { toast(error.message); }
  }

  async function openRun(id) {
    currentRun = id; $("resultsPanel").classList.remove("hidden");
    $("exportJson").href = `/api/validation/runs/${id}/export?format=json`;
    $("exportCsv").href = `/api/validation/runs/${id}/export?format=csv`;
    $("exportZip").href = `/api/validation/runs/${id}/export?format=zip`;
    await refreshCurrentRun();
    $("resultsPanel").scrollIntoView({behavior:"smooth",block:"start"});
  }
  async function refreshCurrentRun() {
    if (!currentRun) return;
    try {
      const run = await request(`/api/validation/runs/${currentRun}`);
      $("runTitle").textContent = `${run.name}｜${statusLabel[run.status] || run.status}｜頁面 ${run.done}/${run.total}`;
      $("stats").innerHTML = run.statistics.length ? run.statistics.map(stat => `
        <div class="stat-card"><strong>${esc(stat.combo_name)}</strong><div class="hint">${esc(stat.actual_path)}</div>
          <div>頁擷取完成率 ${stat.completed_rate == null ? "—" : (stat.completed_rate*100).toFixed(1)+"%"}</div>
          <div>單據 ${stat.document_count ?? "—"} · 資訊不足 ${stat.insufficient_count ?? "—"}</div>
          <div>定位問題 ${stat.location_issue_count ?? "—"} · 待覆核 ${stat.needs_review_count ?? "—"}</div>
          <div>警告頁 ${stat.warning_page_count ?? "—"} · 警告單據 ${stat.warning_document_count ?? "—"}</div>
          <div>品質未評估 ${stat.quality_unassessed_count ?? "—"}</div>
          <div>首筆 ${fmt(stat.first_seconds)}s · 平均 ${fmt(stat.average_seconds)}s</div>
          <div>P50 ${fmt(stat.p50_seconds)}s · P95 ${fmt(stat.p95_seconds)}s</div>
          <div>吞吐 ${fmt(stat.throughput_per_minute)} 張／分</div></div>`).join("") : `<div class="hint">等待結果中…</div>`;
      const filter = $("comboFilter"), old = filter.value;
      filter.innerHTML = `<option value="">所有模型</option>` + run.settings.combinations.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
      filter.value = old;
      await loadResults();
    } catch (error) { console.error(error); }
  }
  async function loadResults() {
    if (!currentRun) return;
    const qs = new URLSearchParams({page_size:"500"});
    if ($("comboFilter").value) qs.set("combo_id", $("comboFilter").value);
    if ($("statusFilter").value) qs.set("status", $("statusFilter").value);
    if ($("sourceFilter").value.trim()) qs.set("source", $("sourceFilter").value.trim());
    const data = await request(`/api/validation/runs/${currentRun}/results?${qs}`);
    $("results").innerHTML = data.items.length ? data.items.map(item => `
      <div class="result-row" data-id="${item.id}"><div class="source">${esc(item.relative_path)}${item.page_no > 1 ? ` p${item.page_no}` : ""}</div>
        <div class="meta">${esc(item.combo_name)} · ${modeLabel[item.mode]} · 第 ${item.repeat_no || "暖機"} 輪</div>
        <div><span class="status-pill ${item.status}">${statusLabel[item.status] || item.status}</span> <span class="meta">${Array.isArray(item.documents) ? `· ${item.documents.length} 張單據` : ""} ${item.actual_path ? esc(item.actual_path) : ""} ${item.timings?.complete_seconds ? `· ${fmt(item.timings.complete_seconds)}s` : ""}</span></div>
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
  async function showDetail(id, row) {
    document.querySelectorAll(".result-row").forEach(x=>x.classList.remove("active")); row?.classList.add("active");
    $("detail").innerHTML = `<div class="detail-empty">載入中…</div>`;
    try {
      const data = await request(`/api/validation/results/${id}`);
      const documents = Array.isArray(data.documents) ? data.documents : [];
      const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
      if (!documents.length && ["partial", "skipped"].includes(data.status)) {
        const pageMessage = data.status === "partial" ? "此頁待人工分割或仍有文件未完成辨識；尚未建立完整單據結果。" : "此頁已確認為空白，未建立單據結果。";
        const pageImageArtifacts = artifacts.filter(a=>["original", "input", "annotated"].includes(a.kind));
        $("detail").innerHTML = `<div class="detail-head"><div><h3>${esc(data.relative_path)}${data.page_no>1?` p${data.page_no}`:""}</h3><div class="hint">${esc(data.combo_name)} · ${modeLabel[data.mode]} · 實際 ${esc(data.actual_path)}</div></div><span class="status-pill ${esc(data.status)}">${esc(statusLabel[data.status] || data.status)}</span></div>
          <div class="detail-box"><strong>${pageMessage}</strong>${data.error ? `<div class="hint warn">${esc(data.error)}</div>` : ""}${qualityBlock(data.quality)}</div>
          ${evidencePanel(pageImageArtifacts, "evidenceImage")}
          <details class="detail-box"><summary>頁面層級 OCR 與模型追蹤</summary><pre>${esc(JSON.stringify({ocr:data.ocr,trace:data.trace},null,2))}</pre></details>`;
        bindEvidenceTabs(); return;
      }
      if (!documents.length && data.status !== "succeeded") {
        $("detail").innerHTML = `<div class="detail-box"><div class="detail-head"><h3>${esc(data.relative_path)}</h3><span class="status-pill ${data.status}">${statusLabel[data.status] || data.status}</span></div><pre>${esc(data.error || "尚未執行")}</pre></div>`; return;
      }
      const legacyDocuments = documents.length ? documents : [{id:data.id,document_index:0,status:data.status,voucher:data.voucher||{},origin:data.origin,error:data.error,timings:data.timings,trace:data.trace,ocr:data.ocr,boxes:data.boxes,quality:data.quality}];
      const documentHtml = legacyDocuments.map((doc,index) => {
        const voucher = doc.voucher || {}, boxMetadata = doc.boxes || voucher.boxes || {};
        const boxes = boxMetadata.coordinates || boxMetadata, boxSources = boxMetadata.sources || {};
        const fields = {doc_type:voucher.doc_type,voucher_id:voucher.voucher_id,date:voucher.date,
          buyer_name:voucher.buyer?.name,buyer_tax_id:voucher.buyer?.tax_id,
          seller_name:voucher.seller?.name,seller_tax_id:voucher.seller?.tax_id,
          subtotal:voucher.subtotal,tax_amount:voucher.tax_amount,total_amount:voucher.total_amount,
          currency:voucher.currency,note:voucher.note,...(voucher.fields||{})};
        const fieldHtml = Object.entries(fields).map(([key,value])=>`<div class="field"><label>${esc(key)}</label><div>${esc(valueText(value))}</div>${boxes[key] ? `<small>框線 ${esc(JSON.stringify(boxes[key]))}${boxSources[key] ? ` · ${esc(boxSources[key])}` : ""}</small>` : `<span class="unlocated">無定位</span>`}</div>`).join("");
        const origin = doc.origin || voucher.origin || {}, quality = voucher.quality || doc.quality;
        const qualityHtml = qualityBlock(quality);
        const originHtml = origin.region || origin.page_no != null || origin.image_sha256 ? `<div>來源頁 ${esc(origin.page_no ?? data.page_no ?? "—")} · 區域 ${esc(origin.region ? JSON.stringify(origin.region) : "整頁／未提供")}</div><div class="hint">${origin.image_sha256 ? `影像 SHA-256 ${esc(origin.image_sha256)}` : "未提供影像雜湊"}${origin.rotation != null ? ` · 旋轉 ${esc(origin.rotation)}°` : ""}${origin.parent_result_id ? ` · 父結果 ${esc(origin.parent_result_id)}` : ""}</div>` : `<div>來源頁 ${esc(data.page_no ?? "—")} · 未提供來源定位</div>`;
        const timingRows = Object.entries(doc.timings || data.timings || {}).map(([key,value])=>({階段:key,秒數:value}));
        const documentNumber = Number(doc.document_index ?? index) + 1;
        const docArtifacts = Array.isArray(doc.artifacts) ? doc.artifacts : [];
        return `<details class="document-result" ${index===0?"open":""}><summary>單據 ${esc(documentNumber)} · <span class="status-pill ${esc(doc.status || data.status)}">${esc(statusLabel[doc.status || data.status] || doc.status || data.status)}</span>${voucher.voucher_id ? ` · ${esc(voucher.voucher_id)}` : ""}</summary>
          ${doc.error ? `<div class="hint warn">${esc(doc.error)}</div>` : ""}
          <div class="detail-box"><h4>來源與品質</h4>${originHtml}${qualityHtml}</div>
          ${evidencePanel(docArtifacts, `documentEvidence${index}`, "此單據沒有可顯示的實際輸入圖或含框圖。")}
          <div class="detail-box"><h4>全部欄位</h4><div class="field-grid">${fieldHtml}</div></div>
          <div class="detail-box"><h4>明細</h4>${table(voucher.items)}</div>
          <div class="detail-box"><h4>分錄</h4>${table(voucher.entries)}</div>
          <div class="detail-box"><h4>逐階段計時</h4>${table(timingRows)}</div>
          <details class="detail-box"><summary>OCR、框線與模型追蹤</summary><pre>${esc(JSON.stringify({ocr:doc.ocr,boxes:boxMetadata,trace:doc.trace},null,2))}</pre></details>
          <details class="detail-box"><summary>完整 JSON</summary><pre>${esc(JSON.stringify({voucher,origin,quality},null,2))}</pre></details></details>`;
      }).join("");
      $("detail").innerHTML = `
        <div class="detail-head"><div><h3>${esc(data.relative_path)}${data.page_no>1?` p${data.page_no}`:""}</h3><div class="hint">${esc(data.combo_name)} · ${modeLabel[data.mode]} · 實際 ${esc(data.actual_path)}</div></div><span class="status-pill ${esc(data.status)}">${esc(statusLabel[data.status] || data.status)} · 待人工核對</span></div>
        ${evidencePanel(artifacts, "evidenceImage")}
        <div class="detail-sections">${documentHtml}<details class="detail-box"><summary>頁面層級 OCR 與模型追蹤（${data.trace?.filter(x=>x.kind==="model_call").length||0} 次）</summary><pre>${esc(JSON.stringify({ocr:data.ocr,trace:data.trace},null,2))}</pre></details></div>`;
      bindEvidenceTabs();
    } catch (error) { $("detail").innerHTML = `<div class="detail-box">${esc(error.message)}</div>`; }
  }

  init();
})();
