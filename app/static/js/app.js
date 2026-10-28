(() => {
  const $ = (id) => document.getElementById(id);
  const dropZone = $("dropZone"), fileInput = $("fileInput");
  const progress = $("progress"), progressText = $("progressText");
  const modal = $("modal");
  let jobTimer = null;
  const importJobs = new Map();
  const importJobTimers = new Map();
  const importSeenPages = new Map();
  let importJobGeneration = 0;
  let currentId = null;
  let currentVoucher = {};   // 目前載入的 current（保留 entries 等未編輯欄位）
  let currentVersion = "", companyRecords = [], accountChart = [], reviewedItems = {};
  let recognitionReady = false, recognitionDirty = false;
  let currentManualEntry = false;   // 保留既有人工修正紀錄的重新辨識提醒
  let editBusy = false;
  let currentUser = null;
  let currentIsImport = false;
  let DOC_TYPES = {};   // 各單據類型欄位契約（/api/doc-types）
  let cropping = false; // 裁切模式中
  let edgeMode = false; // 沿邊緣拉線對齊模式中
  let imageToolsLocked = false; // 自動分割來源頁不可改影像

  // 後端錯誤轉人看得懂的字串：detail 可能是字串、FastAPI 422 的物件陣列、或整包物件。
  // 直接把 detail 丟給 alert()，遇到後兩者會顯示「[object Object]」，等於錯誤訊息被吃掉。
  function errText(d, fallback) {
    const x = d && d.detail;
    if (x == null || x === "") return fallback;
    if (typeof x === "string") return x;
    if (Array.isArray(x)) {
      return x.map((e) => (e && e.msg)
        ? `${(e.loc || []).join(" → ")}：${e.msg}` : JSON.stringify(e)).join("\n") || fallback;
    }
    if (typeof x === "object") return x.msg || JSON.stringify(x);
    return String(x);
  }

  const STATUS_LABEL = {
    prepared: "待外觀確認", queued: "待提取", pending: "辨識待確認", confirmed: "辨識已確認",
  };
  const STATUS_CLASS = {
    prepared: "prep", queued: "scan", pending: "", confirmed: "high",
  };
  // 階段分頁 → 對應狀態（import 為動作視圖，非狀態）
  const STAGE_STATUS = {
    prepared: "prepared", queued: "queued", pending: "pending",
  };
  const STAGE_TITLE = {
    prepared: "② 外觀確認佇列", queued: "③ 掃描清單（待提取）", pending: "④ 確認",
  };
  let currentStage = "import";
  let stageAction = "";   // 目前紀錄的階段動作：confirm / recognize
  const nf = (n) => (n == null || n === "" ? "—" : Number(n).toLocaleString("zh-TW"));
  const esc = (s) => (s == null ? "" : String(s)).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

  // ── 上傳：每位使用者一次一個檔案，頁面依序確認 ──
  let uploadSending = false, importRestorePending = true;
  dropZone.addEventListener("click", () => { if (!uploadsBlocked()) fileInput.click(); });
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); if (!uploadsBlocked()) dropZone.classList.add("over"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("over"));
  dropZone.addEventListener("drop", (e) => { e.preventDefault(); dropZone.classList.remove("over"); handleFiles(e.dataTransfer.files); });
  fileInput.addEventListener("change", (e) => handleFiles(e.target.files));

  function handleFiles(fileList) {
    const files = Array.from(fileList);
    if (!files.length) return;
    if (files.length !== 1) { alert("一次只能上傳一個檔案；多頁 PDF 將逐頁進行外觀確認"); fileInput.value = ""; return; }
    if (uploadsBlocked()) { alert("請先完成目前檔案的外觀確認，或取消目前匯入"); return; }
    runBatch(files);
  }

  async function runBatch(files) {
    if (files.length !== 1 || uploadsBlocked()) return;
    uploadSending = true; renderImportState();
    const fd = new FormData(); fd.append("files", files[0]);
    fd.append("company", $("uploadCompany").value);
    fd.append("mode", $("uploadMode").value);
    try {
      const response = await fetch("/api/import-jobs", { method: "POST", body: fd });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(errText(data, "匯入失敗"));
      trackImportJob(data);
    } catch (err) { alert("錯誤：" + err.message); await restoreImportJobs(); }
    finally { uploadSending = false; fileInput.value = ""; renderImportState(); }
  }

  const IMPORT_ACTIVE = new Set(["queued", "running", "cancelling"]);
  function activeImportJob() {
    return Array.from(importJobs.values()).filter(job => !["completed", "cancelled"].includes(job.status))
      .sort((a,b) => String(a.created_at || "").localeCompare(String(b.created_at || "")))[0] || null;
  }
  function uploadsBlocked() { return uploadSending || importRestorePending || !!activeImportJob(); }
  function recoveryImportJob() { return activeImportJob() || importJobs.get(currentVoucher.origin?.import_job_id) || null; }
  function currentReviewIds(job = activeImportJob()) {
    if (!job || job.status !== "awaiting_review") return [];
    if (Array.isArray(job.review_ids)) return job.review_ids;
    const page = (job.pages || []).find(page => page.status === "preview_ready");
    return page ? page.voucher_ids?.length ? page.voucher_ids : [page.source_page_id].filter(Boolean) : [];
  }
  function renderImportState() {
    const job = activeImportJob(), blocked = uploadsBlocked();
    fileInput.disabled = blocked;
    dropZone.setAttribute("aria-disabled", String(blocked));
    dropZone.style.opacity = blocked ? "0.6" : "1";
    $("uploadCompany").disabled = blocked; $("uploadMode").disabled = blocked;
    $("btnScanImport").disabled = blocked;
    $("importState").classList.toggle("hidden", !blocked);
    const failed = job && ["failed", "partial_failed", "interrupted"].includes(job.status);
    $("importHint").textContent = uploadSending ? "正在上傳，請稍候…" : importRestorePending ? "正在恢復目前檔案…" :
      failed ? "目前頁面尚未完成，請重試或取消目前匯入。" : job?.status === "awaiting_review" ? "請先完成目前檔案的外觀確認。" : job ? "正在準備目前頁面，請稍候…" : "";
    $("btnImportReview").classList.toggle("hidden", !currentReviewIds(job).length);
    $("btnImportRetry").classList.toggle("hidden", !failed && !(importRestorePending && !uploadSending));
    $("btnImportCancel").classList.toggle("hidden", !job || job.status === "cancelling");
    const reviewJob = recoveryImportJob(), recoverable = reviewJob && ["failed","partial_failed","interrupted","cancelled"].includes(reviewJob.status);
    $("appearanceRecovery").classList.toggle("hidden", currentStage !== "prepared" || !reviewJob);
    $("btnAppearanceRetry").classList.toggle("hidden", !recoverable);
    $("btnAppearanceCancel").classList.toggle("hidden", !reviewJob || ["completed","cancelled","cancelling"].includes(reviewJob.status));
    $("btnAppearanceRetry").textContent = ["interrupted","cancelled"].includes(reviewJob?.status) ? "續跑目前頁面" : "重試目前頁面";
    const position = $("appearancePosition");
    position.classList.toggle("hidden", currentStage !== "prepared" || !job);
    position.textContent = job ? `第 ${job.current_page || 1}／${job.total_pages || "…"} 頁${currentReviewIds(job).length > 1 ? ` · 本頁尚有 ${currentReviewIds(job).length} 張待確認` : ""}` : "";
  }

  function trackImportJob(job) {
    const id = String(job.job_id || job.id);
    if (!id || id === "undefined") return;
    importJobs.set(id, { ...importJobs.get(id), ...job, id });
    renderImportState(); pollImportJob(id);
  }

  async function pollImportJob(id) {
    clearTimeout(importJobTimers.get(id)); importJobTimers.delete(id);
    if (!currentUser || !importJobs.has(id)) return;
    const generation = importJobGeneration;
    try {
      const response = await fetch(`/api/import-jobs/${encodeURIComponent(id)}`);
      if (generation !== importJobGeneration || !currentUser || !importJobs.has(id)) return;
      if (response.status === 401 || response.status === 404) {
        if (response.status === 404) importJobs.delete(id);
        alert(response.status === 401 ? "登入已過期，請重新登入。" : "找不到此匯入工作。"); renderImportState(); return;
      }
      const job = await response.json();
      if (generation !== importJobGeneration || !currentUser || !importJobs.has(id)) return;
      if (!response.ok || !job.id) throw new Error(errText(job, "讀取目前檔案失敗"));
      importJobs.set(id, { ...importJobs.get(id), ...job, id });
      const signature = JSON.stringify([job.status, job.review_ids, job.current_page, job.pages?.map(p => [p.id,p.status,p.voucher_ids])]);
      const changed = importSeenPages.get(id) !== signature;
      importSeenPages.set(id, signature); renderImportState();
      if (changed) {
        refreshStats();
        if (["failed","partial_failed","interrupted"].includes(job.status)) alert(job.error || job.pages?.find(p => p.error)?.error || "目前頁面尚未完成，請重試或取消。");
      }
      const ids = currentReviewIds(job);
      if (activeImportJob()?.id === id && ids.length && !recognitionDirty && !editBusy && changed) {
        if (currentStage !== "prepared") setStage("prepared");
        else await loadList();
      } else if (changed && currentStage !== "import" && !recognitionDirty && !editBusy) await loadList();
      if ((IMPORT_ACTIVE.has(job.status) || job.status === "awaiting_review") && generation === importJobGeneration && currentUser && importJobs.has(id))
        importJobTimers.set(id, setTimeout(() => pollImportJob(id), 1500));
    } catch (err) {
      if (generation !== importJobGeneration || !currentUser || !importJobs.has(id)) return;
      console.warn("import poll failed", id, err);
      $("importHint").textContent = "連線中斷，正在重新連線…";
      importJobTimers.set(id, setTimeout(() => pollImportJob(id), 1500));
    }
  }

  function stopImportPolling() {
    importJobGeneration += 1;
    importJobTimers.forEach(timer => clearTimeout(timer)); importJobTimers.clear();
    importJobs.clear(); importSeenPages.clear(); importRestorePending = true;
    renderImportState();
  }

  async function actOnImportJob(id, action) {
    try {
      const response = await fetch(`/api/import-jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(errText(data, "無法更新目前匯入"));
      importJobs.set(id, {...importJobs.get(id), ...data, id}); renderImportState();
      if (action === "cancel") { clearReview(); setStage("import"); }
      await pollImportJob(id);
    } catch (err) { alert(err.message); }
  }

  async function restoreImportJobs() {
    const generation = importJobGeneration;
    try {
      const response = await fetch("/api/import-jobs");
      const data = await response.json();
      if (generation !== importJobGeneration || !currentUser) return;
      if (!response.ok) throw new Error(errText(data,"恢復目前檔案失敗"));
      const jobs = data.items || [];
      jobs.forEach(job => importJobs.set(String(job.id), {...importJobs.get(String(job.id)),...job}));
      importRestorePending = false; renderImportState();
      const job = activeImportJob();
      if (job) await pollImportJob(job.id);
    } catch (err) { console.warn("could not restore import", err); renderImportState(); }
  }
  async function retryCurrentImport() {
    const job = recoveryImportJob();
    if (!job) { await restoreImportJobs(); return; }
    await actOnImportJob(job.id, ["interrupted","cancelled"].includes(job.status) ? "resume" : "retry-failed");
  }
  function cancelCurrentImport() { const job = recoveryImportJob(); if (job) actOnImportJob(job.id,"cancel"); }
  $("btnImportReview").addEventListener("click", () => setStage("prepared"));
  $("btnImportRetry").addEventListener("click", retryCurrentImport);
  $("btnAppearanceRetry").addEventListener("click", retryCurrentImport);
  $("btnImportCancel").addEventListener("click", cancelCurrentImport);
  $("btnAppearanceCancel").addEventListener("click", cancelCurrentImport);

  // ── ③掃描清單：一鍵批次提取 ──
  $("btnExtractAll").addEventListener("click", async () => {
    const d = await (await fetch("/api/extract-batch", { method: "POST", headers: json(), body: "{}" })).json();
    if (!d.ok) { alert(errText(d, "無法開始提取")); return; }
    $("btnExtractAll").disabled = true;
    clearInterval(jobTimer);
    jobTimer = setInterval(async () => {
      const job = await (await fetch("/api/extract-job")).json();
      const pct = job.total ? job.done / job.total : 0;
      eProgress(`內容提取中 ${job.done}/${job.total}　${job.current || ""}`, pct);
      if (job.status === "completed") {
        clearInterval(jobTimer); eProgressHide(); $("btnExtractAll").disabled = false;
        toast("提取完成，已移至④確認"); loadList(); refreshStats();
        if (job.errors && job.errors.length) alert("部分失敗：\n" + job.errors.join("\n"));
      }
    }, 1500);
  });
  function eProgress(text, pct) { $("eProgress").classList.remove("hidden"); $("eBarFill").style.width = Math.round(pct * 100) + "%"; $("eProgressText").textContent = text; }
  function eProgressHide() { $("eProgress").classList.add("hidden"); }

  // ── ①匯入：伺服器端掃描來源目錄 ──
  $("btnScanList").addEventListener("click", async () => {
    const d = await (await fetch("/api/scan-source/list")).json();
    const box = $("scanFiles");
    if (!d.ok) { box.innerHTML = ""; $("scanDirHint").textContent = errText(d, "讀取失敗"); $("btnScanImport").classList.add("hidden"); return; }
    if (!d.files.length) { box.innerHTML = ""; $("scanDirHint").textContent = `目錄 ${d.dir} 內沒有可匯入的檔案。`; $("btnScanImport").classList.add("hidden"); return; }
    $("scanDirHint").textContent = `目錄：${d.dir}（一次選擇一個檔案）`;
    box.innerHTML = d.files.map((f) => `<label class="scan-row"><input type="radio" name="scanFile" value="${esc(f.name)}">${esc(f.name)}<span class="hint">${(f.size / 1024).toFixed(0)} KB</span></label>`).join("");
    $("btnScanImport").classList.remove("hidden");
  });
  $("btnScanImport").addEventListener("click", async () => {
    const names = Array.from($("scanFiles").querySelectorAll("input:checked")).map((i) => i.value);
    if (names.length !== 1) { alert("請選擇一個檔案"); return; }
    if (uploadsBlocked()) { alert("請先完成目前檔案的外觀確認，或取消目前匯入"); return; }
    uploadSending = true; renderImportState();
    try {
      const response = await fetch("/api/scan-source/import", { method: "POST", headers: json(), body: JSON.stringify({ filenames: names, company: $("uploadCompany").value, background: true, mode: $("uploadMode").value }) });
      const d = await response.json();
      if (!response.ok || !d.ok) throw new Error(errText(d,"匯入失敗"));
      trackImportJob(d);
    } catch (err) { alert(err.message); await restoreImportJobs(); }
    finally { uploadSending = false; renderImportState(); }
  });

  function hideProgress() { progress.classList.add("hidden"); }

  // ── 階段分頁 ──
  document.querySelectorAll(".stage-tab").forEach((t) =>
    t.addEventListener("click", () => setStage(t.dataset.stage)));
  $("confirmationFilter").addEventListener("change", () => loadList());

  function setStage(stage) {
    currentStage = stage;
    document.querySelectorAll(".stage-tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.stage === stage));
    const isImport = stage === "import";
    $("importView").classList.toggle("hidden", !isImport);
    $("queueView").classList.toggle("hidden", isImport);
    $("btnExtractAll").classList.toggle("hidden", stage !== "queued");
    $("confirmationFilter").classList.toggle("hidden", stage !== "pending");
    $("btnMerge").classList.toggle("hidden", stage !== "queued");
    renderImportState();
    if (!isImport) { $("queueTitle").textContent = STAGE_TITLE[stage] || "佇列"; loadList(); }
    refreshStats();
  }

  function updateMergeBtn() {
    $("btnMerge").disabled = document.querySelectorAll(".mchk:checked").length < 2;
  }

  async function refreshStats() {
    try {
      const s = await (await fetch("/api/stats")).json();
      Object.keys(STAGE_STATUS).forEach((k) => {
        const el = $("cnt-" + k); if (el) el.textContent = s[k] || 0;
      });
    } catch (e) { /* ignore */ }
  }

  // ── 來源頁：僅保留原圖供追溯，不顯示於前端清單 ──
  const EMPTY_LIST_TEXT = "此階段目前沒有憑證。";
  function isSourceRecord(r) { return !!r && r.record_role === "source_page"; }
  // 由既有版面判定衍生來源頁狀態（不新增資料庫狀態）：
  // skipped 已略過（空白／佐證／非單據）、split 已拆分、review 待人工處理
  function sourceTrace(r, childCount) {
    const layout = (r && r.origin && r.origin.layout) || {};
    const kind = layout.content_kind || (r && r.quality && r.quality.content_kind) || "";
    const auto = layout.auto_split === true;
    const skipLabel = { blank: "空白頁", supporting: "佐證附頁", non_voucher: "非單據頁" }[kind];
    if (auto && skipLabel) return { state: "skipped", label: `已略過（${skipLabel}）`, reason: kind };
    if (childCount > 0 || (auto && kind === "document")) {
      const regions = Array.isArray(layout.regions) ? layout.regions.length : 0;
      return { state: "split", label: "已拆分", reason: kind || "document", regions };
    }
    return { state: "review", label: "待人工處理", reason: kind };
  }
  // 清單分流：已拆分／已略過的來源頁不顯示；尚未自動分割、需人工處理的來源頁仍須可見，
  // 否則該頁內容會無人處理（②外觀確認階段才有來源頁）
  function queueViewModel(rows, stage) {
    const all = rows || [];
    const counts = {};
    all.forEach((r) => { if (!isSourceRecord(r) && r.source_parent_id != null) counts[r.source_parent_id] = (counts[r.source_parent_id] || 0) + 1; });
    let hidden = 0, skipped = 0;
    const vouchers = all.filter((r) => {
      if (!isSourceRecord(r)) return true;
      // 人工模式保留不可變來源頁，另有可編輯單據；跨階段篩選時兩者未必同時回傳。
      if (r.origin?.preparation?.mode === "manual_review") { hidden += 1; return false; }
      const t = sourceTrace(r, counts[r.id] || 0);
      if (stage === "prepared" && t.state === "review") return true;
      hidden += 1; if (t.state === "skipped") skipped += 1;
      return false;
    });
    const emptyText = !vouchers.length && skipped
      ? `目前沒有待外觀確認的單據（已自動略過 ${skipped} 個非單據頁）。` : EMPTY_LIST_TEXT;
    return { vouchers, hidden, skipped, emptyText };
  }
  // 依來源頁狀態給出準確提示（僅需人工處理的來源頁通常可被開啟）
  function sourceHintHtml(trace, autoSplit) {
    if (trace.state === "skipped") {
      const what = { blank: "空白頁", supporting: "佐證附頁", non_voucher: "非單據頁" }[trace.reason] || "非單據頁";
      return `<b>來源頁（已略過）</b>：此頁確認為${what}，沒有可處理的單據；原圖保留供追溯，不會確認或送入辨識。`;
    }
    if (autoSplit) {
      return trace.regions === 1
        ? "<b>來源頁</b>：此頁為單張完整單據，已建立對應的單據；來源頁僅保留原圖供追溯，請處理清單中的該單據，不需重複處理。"
        : "<b>來源頁</b>：這一頁含有多張單據，系統已自動拆分；來源頁僅保留原圖供追溯，請開啟清單中拆分出的單據逐張處理，或改以一頁一張重新處理。";
    }
    return "<b>來源頁（待人工處理）</b>：系統無法判定此頁只有一張單據。本系統一次只處理一頁一張，請改以一頁一張重新處理；來源頁保留供追溯，不會確認或送入辨識。";
  }

  // ── 清單 ──
  async function loadList() {
    if (currentStage === "import") { refreshStats(); return; }
    const stage = currentStage;
    const status = currentStage === "pending" ? $("confirmationFilter").value : STAGE_STATUS[currentStage] || "";
    const url = "/api/vouchers" + (status ? `?status=${encodeURIComponent(status)}` : "");
    const data = await (await fetch(url)).json();
    if (stage !== currentStage) return;
    const view = queueViewModel(data.items || [], currentStage);
    let rows = view.vouchers;
    if (stage === "prepared") {
      const job = activeImportJob(), ids = currentReviewIds(job);
      if (job) rows = rows.filter(row => ids.includes(row.id));
      rows = [rows.find(row => row.id === currentId) || rows.slice().sort((a,b) => a.id-b.id)[0]].filter(Boolean);
    }
    const body = $("listBody");
    const activeJob = stage === "prepared" ? activeImportJob() : null;
    $("listEmpty").textContent = activeJob && IMPORT_ACTIVE.has(activeJob.status) ? "正在準備目前頁面，請稍候…" : activeJob && ["failed","partial_failed","interrupted"].includes(activeJob.status) ? "目前頁面尚未完成，請重試或取消目前匯入。" : activeJob?.status === "awaiting_review" && !currentReviewIds(activeJob).length ? "目前工作沒有可確認的單據，請取消目前匯入後重新上傳。" : view.emptyText;
    if (!rows.length) { body.innerHTML = ""; $("listTable").classList.add("hidden"); $("listEmpty").classList.remove("hidden"); updateMergeBtn(); refreshStats(); if (stage === "prepared" && !recognitionDirty && !editBusy) clearReview(); renderImportState(); return; }
    $("listEmpty").classList.add("hidden"); $("listTable").classList.remove("hidden");
    const mergeable = currentStage === "queued";
    const selectedIds = new Set(Array.from(body.querySelectorAll(".mchk:checked"), (cb) => cb.dataset.id));
    body.innerHTML = rows.map((r) => {
      const chk = mergeable ? `<input type="checkbox" class="mchk" data-id="${r.id}"${selectedIds.has(String(r.id)) ? " checked" : ""}>` : "";
      const att = r.attachments ? ` <span class="att-badge" title="含 ${r.attachments} 附頁">📎${r.attachments}</span>` : "";
      // 憑證號常是空的（對帳單、部分收據無單號），故以「種類」為第二行辨識依據，
      // 否則清單只剩一排數字看不出是什麼單
      // 以欄位契約的正式標籤為準；current_json.doc_type 是模型回傳的字串，可能簡略或走樣
      const spec = DOC_TYPES[r.doc_type_key];
      const type = esc((spec && spec.label) || r.doc_type) || "自動判斷類型";
      const manual = r.manual_entry ? ` <span class="att-badge" title="欄位已人工修正確認">✍ 人工修正</span>` : "";
      const sourceTag = r.source_parent_id != null
        ? ` <span class="att-badge" title="此單據拆分自原始掃描頁">來自${r.origin?.page_no != null ? ` p${esc(r.origin.page_no)}` : ` #${esc(r.source_parent_id)}`}</span>` : "";
      return `
      <tr data-id="${r.id}" class="${r.id === currentId ? "sel" : ""}" title="${esc(r.source_file || "")}">
        <td>${chk}${currentStage === "prepared" && r.has_image ? `<img src="/api/vouchers/${r.id}/image?expected_updated_at=${encodeURIComponent(r.updated_at)}" alt="單據縮圖" loading="lazy" style="width:72px;height:88px;object-fit:contain;vertical-align:middle">` : ""}${esc(r.voucher_id) || "—"}${att}${sourceTag}<div class="row-sub">${type}${manual}</div></td>
        <td>${esc(r.company) || "—"}</td>
        <td>${esc(r.date) || "—"}</td>
        <td>${nf(r.total_amount)}</td>
        <td><span class="tag ${STATUS_CLASS[r.status] || ""}">${STATUS_LABEL[r.status] || r.status}</span></td>
      </tr>`;
    }).join("");
    body.querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", (e) => {
      if (e.target.classList.contains("mchk")) return;
      openReview(Number(tr.dataset.id));
    }));
    body.querySelectorAll(".mchk").forEach((cb) => cb.addEventListener("click", (e) => { e.stopPropagation(); updateMergeBtn(); }));
    updateMergeBtn();
    refreshStats();
    renderImportState();
    if (stage === "prepared" && rows[0].id !== currentId && !recognitionDirty && !editBusy) await openReview(rows[0].id);
  }

  // 合併多頁為一張（主頁＋附頁）
  $("btnMerge").addEventListener("click", async () => {
    const ids = Array.from(document.querySelectorAll(".mchk:checked")).map((c) => Number(c.dataset.id));
    if (ids.length < 2) { alert("請勾選至少兩頁"); return; }
    const primary = Math.min(...ids);
    if (!confirm(`將勾選的 ${ids.length} 頁合併為一張憑證（以 #${primary} 為主頁、其餘為附頁，僅辨識主頁）？`)) return;
    const d = await (await fetch("/api/vouchers/merge", { method: "POST", headers: json(), body: JSON.stringify({ primary_id: primary, ids }) })).json();
    if (!d.ok) { alert(errText(d, "合併失敗")); return; }
    toast(`已合併（主頁 #${primary}，${d.attachments} 附頁）`); loadList(); openReview(primary);
  });

  // ── 刪除 ──
  function isAdminUser() { return !!currentUser && currentUser.is_admin === true; }
  function deleteScopeText(p) {
    const parts = [];
    if (p.voucher_count) parts.push(`${p.voucher_count} 張單據`);
    if (p.source_page_count) parts.push(`${p.source_page_count} 個原始掃描頁`);
    const extra = [];
    if (p.attachment_count) extra.push(`含 ${p.attachment_count} 張附頁`);
    if (p.confirmed_count) extra.push(`其中 ${p.confirmed_count} 張已確認`);
    return parts.join("、") + (extra.length ? `（${extra.join("，")}）` : "");
  }
  // 由後端預覽組出可選範圍；「整份上傳檔」只在確實比「此單據」多時才提供
  function deleteChoices(data, stage = currentStage) {
    const previews = (data && data.previews) || {};
    const can = (data && data.can_delete) || {};
    const choices = [];
    const add = (scope, label, preview) => {
      const allowed = can[scope] === true;
      const warning = !preview.confirmed_count ? ""
        : allowed ? "含已確認的憑證：刪除後日記帳與報表將不再包含它們。"
          : "含已確認的憑證，只有管理員可以刪除。";
      choices.push({ scope, label, summary: deleteScopeText(preview), allowed, warning,
        blockedReason: preview.blocked_reason || "" });
    };
    if (stage === "queued") {
      if (previews.record) add("record", "只刪除此單據", previews.record);
    } else if (previews.page) {
      add("page", `刪除此頁（第 ${previews.page.page_no} 頁）`, previews.page);
    } else if (previews.record) add("record", "只刪除此單據", previews.record);
    if (stage !== "queued" && !previews.page && previews.upload && previews.record && previews.upload.ids.length > previews.record.ids.length) {
      add("upload", "刪除整份上傳檔", previews.upload);
    }
    return choices;
  }
  let deleteTargetId = null;
  let deleteTargetVersion = null;
  let deleteTargetStage = null;
  let deleteChoiceList = [];
  let deleteSubmitting = false;
  let deletePreviewGeneration = 0;
  function closeDeleteModal() {
    if (deleteSubmitting) return;
    deletePreviewGeneration++;
    $("deleteModal").classList.add("hidden"); deleteTargetId = null; deleteTargetVersion = null; deleteTargetStage = null; deleteChoiceList = [];
  }
  function selectedDeleteChoice() {
    const radio = document.querySelector('input[name="deleteScope"]:checked');
    return deleteChoiceList.find((c) => radio && c.scope === radio.value) || null;
  }
  function updateDeleteConfirm() {
    const choice = selectedDeleteChoice();
    $("deleteConfirm").disabled = deleteSubmitting || !choice || !choice.allowed;
  }
  async function openDelete() {
    if (!currentId || editBusy) return;
    const id = currentId, version = currentVersion, stage = currentStage;
    const generation = ++deletePreviewGeneration;
    $("deleteLoading").classList.remove("hidden"); $("deleteBody").classList.add("hidden");
    $("deleteConfirm").disabled = true;
    $("deleteModal").classList.remove("hidden");
    try {
      const scopeQuery = stage === "queued" ? "?scope=record" : "";
      const resp = await fetch(`/api/vouchers/${id}/delete-preview${scopeQuery}`);
      const data = await resp.json();
      if (generation !== deletePreviewGeneration) return;
      if (id !== currentId || version !== currentVersion || stage !== currentStage) { closeDeleteModal(); return; }
      if (!resp.ok) throw new Error(errText(data, "無法取得刪除範圍"));
      if (data.busy) throw new Error("匯入或提取進行中，請稍後再刪除");
      deleteTargetId = id;
      deleteTargetVersion = version;
      deleteTargetStage = stage;
      deleteChoiceList = deleteChoices(data, stage);
      $("deleteChoices").innerHTML = deleteChoiceList.map((c, i) => `
        <label class="del-opt"><input type="radio" name="deleteScope" value="${c.scope}" ${i === 0 ? "checked" : ""} ${c.allowed ? "" : "disabled"}>
          <span><b>${esc(c.label)}</b><br>${esc(c.summary)}${c.blockedReason ? `<br><span class="del-warn">${esc(c.blockedReason)}</span>` : ""}${c.warning ? `<br><span class="del-warn">${esc(c.warning)}</span>` : ""}</span></label>`).join("");
      document.querySelectorAll('input[name="deleteScope"]').forEach((r) => r.addEventListener("change", updateDeleteConfirm));
      // 預設選取第一個可用範圍
      const firstAllowed = deleteChoiceList.findIndex((c) => c.allowed);
      const radios = document.querySelectorAll('input[name="deleteScope"]');
      if (firstAllowed >= 0 && radios[firstAllowed]) radios[firstAllowed].checked = true;
      $("deleteLoading").classList.add("hidden"); $("deleteBody").classList.remove("hidden");
      updateDeleteConfirm();
    } catch (err) {
      if (generation !== deletePreviewGeneration) return;
      closeDeleteModal(); alert(err.message || "無法取得刪除範圍");
    }
  }
  async function confirmDelete() {
    const choice = selectedDeleteChoice();
    const id = deleteTargetId;
    if (deleteSubmitting || !choice || !choice.allowed || id == null ||
        (deleteTargetStage === "queued" && choice.scope !== "record")) return;
    deleteSubmitting = true;
    editBusy = true;
    $("deleteConfirm").disabled = true;
    $("deleteCancel").disabled = true;
    let resp, d = {};
    try {
      const versionQuery = choice.scope === "page" ? `&expected_updated_at=${encodeURIComponent(deleteTargetVersion || "")}` : "";
      resp = await fetch(`/api/vouchers/${id}?scope=${encodeURIComponent(choice.scope)}${versionQuery}`, { method: "DELETE" });
      d = await resp.json().catch(() => ({}));
    } catch (err) {
      deleteSubmitting = false; editBusy = false; $("deleteCancel").disabled = false; updateDeleteConfirm();
      alert("刪除失敗：" + err.message); return;
    }
    if (!resp.ok || d.ok === false) {
      deleteSubmitting = false; editBusy = false; $("deleteCancel").disabled = false; updateDeleteConfirm();
      alert(errText(d, "刪除失敗")); return;
    }   // 失敗時保留目前畫面
    deleteSubmitting = false;
    closeDeleteModal();
    if (currentId != null && (d.deleted_ids || []).includes(currentId)) clearReview();
    editBusy = false;
    $("deleteCancel").disabled = false;
    toast(`已刪除：${choice.summary}`);
    if (choice.scope === "page") await restoreImportJobs();
    loadList(); refreshStats();
    if (d.cleanup_failed && d.cleanup_failed.length) alert("資料已刪除，但部分檔案未能清理（可稍後手動處理）：\n" + d.cleanup_failed.join("\n"));
  }
  $("btnDelete").addEventListener("click", openDelete);
  $("deleteConfirm").addEventListener("click", confirmDelete);
  $("deleteCancel").addEventListener("click", closeDeleteModal);

  // ── 審核編輯 ──
  async function openReview(id) {
    if (editBusy) return;
    currentId = id;
    if (cropping) setCropMode(false);
    const rec = await (await fetch(`/api/vouchers/${id}`)).json();
    if (id !== currentId) return;
    if (rec.detail) { alert(errText(rec, "操作失敗")); return; }
    const v = rec.current || {};
    currentVoucher = v;
    currentVersion = rec.updated_at;
    reviewedItems = {};
    currentManualEntry = !!rec.manual_entry;
    currentIsImport = !!rec.is_import;
    const autoSplitSource = v.origin?.layout?.auto_split === true;
    const sourcePage = rec.record_role === "source_page" || autoSplitSource;
    $("revForm").classList.toggle("source-only", autoSplitSource);
    $("reviewBody").classList.toggle("hidden", autoSplitSource);
    $("recognitionDetails").classList.toggle("hidden", autoSplitSource);
    $("importBadge").classList.toggle("hidden", !currentIsImport);
    $("revEmpty").classList.add("hidden"); $("revForm").classList.remove("hidden");
    setStatus(rec.status);
    setPendingActionVisibility(rec.status);
    const isPrepared = rec.status === "prepared";
    const isQueued = rec.status === "queued";
    const isPreExtract = isPrepared || isQueued;   // 尚未萃取欄位：隱藏編輯器
    const prep = v.origin?.preparation;
    const manualMode = prep?.mode === "manual_review";
    const timings = v.recognition_diagnostics?.timings;
    $("fastTiming").classList.toggle("hidden", !timings);
    $("fastTiming").textContent = timings ? [["首次預覽",timings.first_preview_seconds],["人工確認等待",timings.human_wait_seconds],["本次辨識",timings.recognition_seconds],["OCR",timings.ocr_seconds],["補讀OCR",timings.supplement_ocr_seconds],["上傳至今",timings.overall_seconds]].filter(([,value]) => value != null).map(([label,value]) => `${label} ${Number(value).toFixed(1)} 秒`).join(" · ") : "";
    $("revForm").classList.toggle("prepared", isPreExtract);
    $("prepHint").classList.toggle("hidden", !isPreExtract && !autoSplitSource);
    $("btnConfirmRecognize").classList.toggle("hidden", !isPreExtract);
    $("btnConfirmRecognize").disabled = sourcePage;
    // 外觀確認階段還沒有欄位可重辨；重新辨識只在待確認／已確認時出現，外觀確認改放「復原」。
    $("btnReRecognize").classList.toggle("hidden", isPreExtract);
    $("btnReRecognize").disabled = sourcePage;
    $("btnRestore").classList.toggle("hidden", !isPrepared);
    $("btnConfirm").disabled = sourcePage;
    imageToolsLocked = autoSplitSource;
    resetFineTool();
    updateRestoreButton(rec.image_restore_available);
    $("revConfWrap").classList.toggle("hidden", isPreExtract || autoSplitSource);
    if (isPrepared) {
      stageAction = "confirm";
      $("btnConfirmRecognize").textContent = "✅ 確認外觀（加入掃描清單）";
      $("prepHint").innerHTML = "<b>② 外觀確認</b>：確認上方「單據類型」正確，並用工具列旋轉／微調／裁切整理這一張影像（改壞了可按「復原」）；完成後按「✅ 確認外觀」加入③掃描清單。";
      if (manualMode) $("prepHint").innerHTML = "<b>② 外觀確認</b>：調整方向、微調或裁切後，按「確認外觀」即可加入③掃描清單。類型未指定時會自動判斷。";
    } else if (isQueued) {
      stageAction = "recognize";
      $("btnConfirmRecognize").textContent = "🔍 辨識此張";
      $("prepHint").innerHTML = "<b>③ 掃描清單</b>：外觀已確認。按「🔍 辨識此張」提取內容欄位，或到清單頂端「⚡ 一鍵提取全部」批次處理。";
    }
    if (sourcePage && isPreExtract) {
      $("prepHint").innerHTML = sourceHintHtml(sourceTrace({ origin: v.origin, quality: v.quality }, 0), autoSplitSource);
    } else if (isPreExtract && rec.source_parent_id != null) {
      $("prepHint").innerHTML += `<br><span class="hint">此單據拆分自來源頁 #${esc(rec.source_parent_id)}${v.origin?.page_no != null ? `（原始頁 p${esc(v.origin.page_no)}）` : ""}。</span>`;
    }
    $("revCompanySel").value = rec.company_id == null ? "" : String(rec.company_id);
    updateReviewCompanyTaxId();
    loadItemAccountChart();
    const typeKey = manualMode && isPreExtract && !prep.type_confirmed ? "" : v.doc_type_key || "other";
    populateTypeSel(typeKey);
    buildActiveFields(typeKey);
    renderFields(v.fields || {});
    const conf = v.confidence || "—";
    $("revConf").textContent = conf;
    $("revConf").className = "tag " + (conf === "high" ? "high" : conf === "low" ? "low" : "warn");
    $("f-note").value = v.note || "";
    const hasItems = !sourcePage;
    $("itemsSection").classList.toggle("hidden", !hasItems);
    renderItems(v.items || []);
    renderTaxcheck(rec.taxcheck || {});
    renderDatecheck(rec.datecheck);
    renderAmountcheck(rec.amountcheck);
    applyRecognitionState(rec);
    setupImage(id, rec.has_image, v.boxes || {});
    renderPages(id, rec.attachments || 0);
    loadList();
  }

  // 多頁憑證：主頁＋附頁縮圖切換
  function renderPages(id, attCount) {
    const box = $("revPages");
    if (!attCount) { box.classList.add("hidden"); box.innerHTML = ""; return; }
    const pages = [{ label: "主頁", att: -1 }];
    for (let i = 0; i < attCount; i++) pages.push({ label: `附頁${i + 1}`, att: i });
    box.innerHTML = pages.map((p, i) => `<button class="pg-thumb ${i === 0 ? "active" : ""}" data-att="${p.att}">${p.label}</button>`).join("");
    box.classList.remove("hidden");
    box.querySelectorAll(".pg-thumb").forEach((b) => b.addEventListener("click", () => {
      box.querySelectorAll(".pg-thumb").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      const att = Number(b.dataset.att);
      const img = $("revImage");
      img.onload = () => { resetZoom(); drawBoxes(att === -1 ? (currentVoucher.boxes || {}) : {}); };
      img.src = `/api/vouchers/${id}/image?t=${Date.now()}${att >= 0 ? `&att=${att}` : ""}`;
    }));
  }

  function updateRecognitionState() {
    const canPrint = recognitionReady && !recognitionDirty && !editBusy;
    $("btnPrintVoucher").disabled = !canPrint;
    const status = $("recognitionStatus");
    if (status) {
      status.textContent = recognitionDirty ? "已修改，請儲存並確認" : recognitionReady ? "已確認，可列印" : "待確認";
      status.className = "hint " + (canPrint ? "ok" : "warn");
    }
  }
  function markRecognitionDirty() {
    if (!currentId) return;
    recognitionDirty = true;
    updateRecognitionState();
  }
  function applyRecognitionState(record) {
    recognitionReady = !!record.recognition_report_ready;
    recognitionDirty = false;
    const warnings = record.detail_account_warnings || [];
    if ($("detailWarnings")) {
      $("detailWarnings").textContent = warnings.join("；");
      $("detailWarnings").classList.toggle("hidden", !warnings.length);
    }
    updateRecognitionState();
  }
  $("revForm").addEventListener("input", markRecognitionDirty);
  $("revForm").addEventListener("change", markRecognitionDirty);

  function setStatus(status) {
    const el = $("revStatus");
    el.className = "tag " + (STATUS_CLASS[status] || "");
    el.textContent = STATUS_LABEL[status] || status;
  }

  // 待確認與已確認共用編輯區；無變動儲存維持確認，實際修改由後端撤銷確認。
  function setPendingActionVisibility(status) {
    const editable = status === "pending" || status === "confirmed";
    $("btnReturnImport").classList.toggle("hidden", !editable);
    $("btnConfirm").classList.toggle("hidden", !editable);
    $("btnConfirm").textContent = "確認";
    // 已確認的憑證只有管理員可刪除（後端同樣強制）
    $("btnDelete").classList.toggle("hidden", status === "confirmed" && !isAdminUser());
  }

  function applyRecordStatus(record) {
    if (record.id !== currentId) return;
    if ("manual_entry" in record) currentManualEntry = !!record.manual_entry;
    if (record.updated_at) currentVersion = record.updated_at;
    if (record.current?.origin) currentVoucher.origin = record.current.origin;
    if ("image_restore_available" in record) updateRestoreButton(record.image_restore_available);
    setStatus(record.status);
    setPendingActionVisibility(record.status);
    applyRecognitionState(record);
  }

  async function refreshCurrentStatus(id = currentId) {
    if (!id) return;
    const resp = await fetch(`/api/vouchers/${id}`);
    if (resp.ok) {
      const dirty = recognitionDirty;
      applyRecordStatus(await resp.json());
      recognitionDirty = dirty;
      updateRecognitionState();
    }
    loadList();
  }

  // ── 動態欄位（依單據類型）+ 掃描影像彩色框對照 ──
  const PALETTE = ["#0969da", "#1a7f37", "#8250df", "#bc4c00", "#cf222e", "#d29922",
    "#1b7c83", "#bf3989", "#6639ba", "#0550ae", "#116329", "#953800"];
  let activeFields = [];

  function populateTypeSel(typeKey) {
    const el = $("revTypeSel");
    el.innerHTML = (currentVoucher.origin?.preparation?.mode === "manual_review" && $("revForm").classList.contains("prepared") ? '<option value="">自動判斷類型</option>' : "") + Object.entries(DOC_TYPES)
      .map(([k, t]) => `<option value="${k}">${esc(t.label)}</option>`).join("");
    el.value = typeKey;
  }

  // 外觀確認 / 重新辨識：以「目前選定的類型」在準備好的影像上做內容辨識
  async function runRecognize() {
    if (!currentId || editBusy || inferBusy) return;
    if (currentManualEntry) {
      if (!confirm("這張已有「人工修正」標記。\n重新辨識會保留人工選定的欄位並新增模型結果，成功後須重新確認。\n\n確定要重新辨識嗎？")) return;
    }
    const id = currentId, tk = $("revTypeSel").value;
    try {
      if (!$("revForm").classList.contains("prepared")) await persistCurrentEdits();
      const autoFastType = currentVoucher.origin?.preparation?.mode === "manual_review" && !currentVoucher.origin.preparation.type_confirmed && !tk;
      if (!autoFastType && (currentVoucher.origin?.preparation?.mode !== "manual_review" || tk !== (currentVoucher.doc_type_key || ""))) {
        const typeResp = await fetch(`/api/vouchers/${id}/set-type`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk, expected_updated_at:currentVersion }) });
        if (!typeResp.ok) throw new Error(await responseError(typeResp, "類型儲存失敗"));
        const typeRecord = await typeResp.json();
        currentVersion = typeRecord.updated_at || currentVersion;
      }
      const d = await withInferProgress("內容辨識中…", () =>
        fetch(`/api/vouchers/${id}/recognize`, { method: "POST", headers: json() }).then((r) => r.json()));
      if (!d.ok) throw new Error(errText(d, "辨識失敗"));
      toast("辨識完成，待確認");
      if (currentId === id) await openReview(id);
      loadList();
    } catch (err) {
      alert(`辨識失敗：${err.message}`);
    }
  }

  function buildActiveFields(typeKey) {
    const spec = DOC_TYPES[typeKey] || DOC_TYPES.other || { fields: [] };
    activeFields = (spec.fields || []).map((f, i) => ({
      key: f.key, label: f.label, kind: f.kind || "text",
      el: "dyn-" + f.key, color: PALETTE[i % PALETTE.length], n: i + 1,
    }));
    return activeFields;
  }

  function renderFields(values) {
    const box = $("dynFields");
    box.innerHTML = activeFields.map((f) => {
      let t = f.kind === "number" ? "number" : "text";
      let v = values[f.key];
      // 舊資料的金額可能是「9,900」這種千分位字串，<input type=number> 會顯示空白
      // （看起來像沒辨識到），空值再被儲存就把正確金額洗掉。先轉數字，轉不了就退成文字框。
      if (t === "number" && v != null && v !== "") {
        const n = Number(String(v).replace(/(?:NT)?\$|[,\s，、＄元]/gi, ""));
        if (Number.isFinite(n)) v = n; else t = "text";
      }
      const evidence = currentVoucher.field_evidence?.[`fields.${f.key}`];
      const companyFilled = Array.isArray(evidence) && evidence.some((record) =>
        record?.source === "company" && record.selected === true);
      return `<label><span class="fld-badge" style="background:${f.color}" data-key="${f.key}" title="點我在影像上定位">${f.n}</span>${esc(f.label)}${companyFilled ? '<span class="company-filled-tag">公司帶入</span>' : ""}<input id="${f.el}" type="${t}" value="${esc(v == null ? "" : v)}"><span class="chk" id="chk-${f.key}"></span></label>`;
    }).join("");
    activeFields.forEach((f) => {
      const el = $(f.el); if (!el) return;
      el.addEventListener("focus", () => focusBox(f.key));
      if (f.key.indexOf("tax_id") >= 0) el.addEventListener("blur", recheckTax);
      const badge = box.querySelector(`.fld-badge[data-key="${f.key}"]`);
      if (badge) badge.addEventListener("click", () => focusBox(f.key));
    });
  }

  function setupImage(id, hasImage, boxes) {
    const wrap = $("revImageWrap");
    $("revSplit").classList.toggle("no-image", !hasImage);
    if (!hasImage) { drawBoxes({}); }
    if (!hasImage) { wrap.classList.add("hidden"); return; }
    const img = $("revImage");
    img.onload = () => { resetZoom(); drawBoxes(boxes); };
    img.onerror = () => wrap.classList.add("hidden");
    img.src = `/api/vouchers/${id}/image?t=${Date.now()}`;
    $("revImageFull").href = `/api/vouchers/${id}/image`;
    wrap.classList.remove("hidden");
  }

  // ── 影像縮放 / 平移 ──
  let zScale = 1, zTx = 0, zTy = 0;
  function applyZoom() {
    const z = $("revZoom");
    if (z) z.style.transform = `translate(${zTx}px,${zTy}px) scale(${zScale})`;
  }
  function resetZoom() { zScale = 1; zTx = 0; zTy = 0; applyZoom(); }
  function _clampPan() {
    const inner = document.querySelector(".rev-image-inner");
    if (!inner) return;
    const vw = inner.clientWidth, vh = inner.clientHeight;
    zTx = Math.max(Math.min(0, vw - vw * zScale), Math.min(0, zTx));
    zTy = Math.max(Math.min(0, vh - vh * zScale), Math.min(0, zTy));
  }
  (function initZoom() {
    const zoom = $("revZoom");
    const inner = document.querySelector(".rev-image-inner");
    if (!zoom || !inner) return;
    inner.addEventListener("wheel", (e) => {
      if (cropping) return;
      e.preventDefault();
      const rect = inner.getBoundingClientRect();
      const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
      const ns = Math.max(1, Math.min(8, zScale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      zTx = cx - (cx - zTx) * (ns / zScale);   // 以游標為中心
      zTy = cy - (cy - zTy) * (ns / zScale);
      zScale = ns; _clampPan(); applyZoom();
    }, { passive: false });
    let panning = false, sx = 0, sy = 0;
    zoom.addEventListener("mousedown", (e) => {
      if (cropping || edgeMode) return;   // 裁切框選與沿邊緣拉線時，拖曳不做平移
      panning = true; sx = e.clientX; sy = e.clientY; zoom.classList.add("panning"); e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!panning) return;
      zTx += e.clientX - sx; zTy += e.clientY - sy; sx = e.clientX; sy = e.clientY;
      _clampPan(); applyZoom();
    });
    window.addEventListener("mouseup", () => { if (panning) { panning = false; zoom.classList.remove("panning"); } });
    zoom.addEventListener("dblclick", resetZoom);
  })();

  // 點欄位自動把影像縮放平移到對應的框（框落在邊緣時免手動找）
  function focusBox(key) {
    const b = (currentVoucher.boxes || {})[key];
    const inner = document.querySelector(".rev-image-inner");
    if (!Array.isArray(b) || b.length < 4 || !inner) return;
    const vw = inner.clientWidth, vh = inner.clientHeight;
    const bx = (b[0] + b[2]) / 2 / 1000 * vw, by = (b[1] + b[3]) / 2 / 1000 * vh;
    const bw = Math.abs(b[2] - b[0]) / 1000 * vw, bh = Math.abs(b[3] - b[1]) / 1000 * vh;
    let s = Math.min(vw * 0.45 / Math.max(bw, 8), vh * 0.35 / Math.max(bh, 8));
    s = Math.max(2, Math.min(6, s));
    zScale = s; zTx = vw / 2 - bx * s; zTy = vh / 2 - by * s;
    _clampPan();
    const z = $("revZoom");
    z.style.transition = "transform .25s ease"; applyZoom();
    setTimeout(() => { z.style.transition = ""; }, 300);
    highlightBox(key);
  }
  function highlightBox(key) {
    document.querySelectorAll(".ov-box").forEach((el) =>
      el.classList.toggle("active", el.dataset.key === key));
  }

  function _pct(v) { return Math.max(0, Math.min(100, v / 10)); } // 0~1000 → 0~100%

  function drawBoxes(boxes) {
    const layer = $("revBoxes"); if (layer) layer.innerHTML = "";
    activeFields.forEach((f) => {
      const el = $(f.el);
      const b = boxes[f.key];
      const has = Array.isArray(b) && b.length >= 4 && b.every((n) => !Number.isNaN(Number(n)));
      if (el) el.style.boxShadow = has ? `inset 3px 0 0 ${f.color}` : "";
      if (!has || !layer) return;
      const [x1, y1, x2, y2] = b.map(Number);
      const d = document.createElement("div");
      d.className = "ov-box"; d.dataset.key = f.key;
      d.style.left = _pct(x1) + "%"; d.style.top = _pct(y1) + "%";
      d.style.width = Math.max(0, _pct(x2) - _pct(x1)) + "%";
      d.style.height = Math.max(0, _pct(y2) - _pct(y1)) + "%";
      d.style.borderColor = f.color;
      d.innerHTML = `<span class="ov-tag" style="background:${f.color}">${f.n}</span>`;
      layer.appendChild(d);
    });
  }

  // ── 統編核對（對映到動態欄位）──
  function chkEl(field) { return $("chk-" + field); }
  function renderTaxcheck(tc) {
    activeFields.forEach((f) => { const c = chkEl(f.key); if (c) { c.textContent = ""; c.className = "chk"; } });
    if (currentIsImport || tc === null) {
      const c = chkEl("seller_tax_id") || chkEl("tax_id");
      if (c) { c.textContent = "進口報單，統編免核（鎖定）"; c.className = "chk warn"; }
      return;
    }
    if (!tc) return;
    applyChk(chkEl("seller_tax_id") || chkEl("tax_id"), tc.seller);
    applyChk(chkEl("buyer_tax_id"), tc.buyer);
  }
  // 日期不合理（多半是民國轉西元算錯）→ period 跟著錯，該憑證會從當期日記帳與
  // 報表中無聲消失，故直接標在日期欄旁。須在 renderTaxcheck 之後呼叫（它會先清空）。
  function renderDatecheck(dc) {
    if (!dc || dc.ok) return;
    const el = chkEl("date") || chkEl("issue_date") || chkEl("payment_date") || chkEl("import_date");
    if (!el) return;
    el.textContent = "⚠ " + dc.warn;
    el.className = "chk bad";
  }

  // 金額算術兜不攏（手寫憑證常見）→ 標在總計欄旁；算術是確定的，模型不是。
  function renderAmountcheck(ac) {
    if (!ac || ac.ok) return;
    const el = chkEl("total_amount");
    if (!el) return;
    el.textContent = "⚠ " + ac.warn;
    el.className = "chk bad";
  }

  function applyChk(el, r) {
    if (!el) return;
    if (!r || !r.tax_id) { el.textContent = ""; el.className = "chk"; return; }
    if (r.checksum_valid === false) { el.textContent = "✗ 統編檢查碼錯誤"; el.className = "chk bad"; return; }
    if (r.name_match === false) { el.textContent = `⚠ 統編有效，但與登錄「${r.known_company}」名稱不符`; el.className = "chk warn"; return; }
    if (r.known_company) { el.textContent = `✓ ${r.known_company}`; el.className = "chk ok"; return; }
    el.textContent = "✓ 檢查碼有效"; el.className = "chk ok";
  }
  async function recheckTax() {
    const r = await (await fetch("/api/taxid/check", { method: "POST", headers: json(), body: JSON.stringify(collectForm()) })).json();
    renderTaxcheck(r);
  }

  function itemSpecs() {
    return [{key:"account_code",label:"科目代號",kind:"text"},
      {key:"account_name",label:"科目名稱",kind:"text"},
      {key:"description",label:"品名／摘要",kind:"text"},
      {key:"quantity",label:"數量",kind:"number"},
      {key:"unit_price",label:"單價",kind:"number"},
      {key:"amount",label:"金額",kind:"number"}];
  }
  function itemExtraSpecs() {
    const spec = DOC_TYPES[$("revTypeSel").value || currentVoucher.doc_type_key] || {};
    const primary = new Set(itemSpecs().map((f) => f.key));
    return (spec.item_fields || []).filter((f) => !primary.has(f.key));
  }
  function newItemId() {
    return globalThis.crypto?.randomUUID?.() || "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,
      (c) => { const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16); });
  }
  function selectedCompanyId() {
    const value = $("revCompanySel")?.value;
    return value ? Number(value) : null;
  }
  function normalizedItemName(value) { return String(value || "").trim().replace(/\s+/g, " "); }
  function itemContext(company, name) { return JSON.stringify([company, normalizedItemName(name)]); }
  const itemBookkeeping = new Set(["item_id", "mapping_id", "mapping_version", "account_source",
    "account_needs_review", "remember_mapping", "account_company_id", "account_item_key",
    "account_updated_by", "account_updated_by_name", "account_updated_at", "source_entry_id", "source_direction",
    "original_account_code", "original_account_name"]);
  async function loadItemAccountChart() {
    const company = selectedCompanyId();
    accountChart = [];
    const list = $("itemAccountCodes");
    if (list) list.innerHTML = "";
    if (company == null) return;
    const response = await fetch(`/api/accounts?company_id=${company}`);
    if (company !== selectedCompanyId() || !response.ok) return;
    const chart = (await response.json()).accounts || [];
    if (company !== selectedCompanyId()) return;
    accountChart = chart;
    if (list) list.innerHTML = accountChart.map((a) => `<option value="${esc(a.code)}">${esc(a.name)}</option>`).join("");
  }
  function renderItems(items) {
    const body = $("itemsBody");
    const header = $("itemsSection").querySelector("thead tr");
    if (header) header.innerHTML = itemSpecs().map((f) => `<th>${esc(f.label)}</th>`).join("") + "<th>其他資訊</th><th></th>";
    body.innerHTML = items.map(itemRow).join("");
    bindItemRows();
    if (editBusy) body.querySelectorAll("input,button").forEach((el) => { el.disabled = true; });
  }
  function itemRow(it) {
    it = {...it, item_id: it.item_id || newItemId()};
    const company = selectedCompanyId();
    const control = (f) => {
      let value = it[f.key], type = f.kind === "number" ? "number" : "text";
      if (type === "number" && value != null && value !== "") {
        const number = Number(String(value).replace(/(?:NT)?\$|[,\s，、＄元]/gi, ""));
        if (Number.isFinite(number)) value = number; else type = "text";
      }
      if (f.key === "description") return `<textarea data-item-key="description" data-kind="text" rows="2">${esc(value || "")}</textarea>`;
      return `<input data-item-key="${esc(f.key)}" data-kind="${f.kind}" type="${type}"${type === "number" ? ' step="any" placeholder="—"' : ""}${f.key === "account_code" ? ' list="itemAccountCodes"' : ""} value="${esc(value ?? "")}">`;
    };
    const source = it.account_needs_review ? "科目待核對" : {observed:"原稿科目",mapping:"由品名對應帶入",manual:"人工指定"}[it.account_source] || "科目待指定";
    return `<tr data-original="${esc(JSON.stringify(it))}" data-item-id="${esc(it.item_id)}"
      data-context="${esc(itemContext(it.account_company_id ?? company, it.account_item_key ?? it.description))}"
      data-source="${esc(it.account_source || "")}" data-needs-review="${it.account_needs_review ? "1" : "0"}">
      ${itemSpecs().map((f) => `<td>${control(f)}</td>`).join("")}
      <td><details class="item-extras"><summary>展開</summary>
      ${itemExtraSpecs().map((f) => `<label>${esc(f.label)}${control(f)}</label>`).join("")}
      <label><input type="checkbox" class="remember-mapping" ${it.remember_mapping === true ? "checked" : ""}>記住品名對應</label>
      <div class="account-status hint">${source}</div>
      <button class="btn btn-sm keep-account ${it.account_needs_review ? "" : "hidden"}" type="button">沿用科目</button>
      <button class="btn btn-sm use-account ${it.account_needs_review ? "" : "hidden"}" type="button">採用新建議</button>
      </details></td><td><button class="btn btn-sm del" type="button">✕</button></td></tr>`;
  }
  function bindItemRows() {
    $("itemsBody").querySelectorAll("tr").forEach((tr) => {
      if (tr.dataset.bound) return;
      tr.dataset.bound = "1";
      tr.querySelector(".del").addEventListener("click", () => { delete reviewedItems[tr.dataset.itemId]; tr.remove(); markRecognitionDirty(); });
      tr.querySelector('[data-item-key="description"]')?.addEventListener("change", () => refreshItemMatch(tr).catch((e) => alert(e.message)));
      ["account_code", "account_name"].forEach((key) => tr.querySelector(`[data-item-key="${key}"]`).addEventListener("input", () => {
        tr._lookupGeneration = (tr._lookupGeneration || 0) + 1;
        delete reviewedItems[tr.dataset.itemId];
        tr.dataset.source = "manual";
        if (key === "account_code") {
          const code = tr.querySelector('[data-item-key="account_code"]').value;
          const known = accountChart.find((a) => a.code === code);
          if (known) tr.querySelector('[data-item-key="account_name"]').value = known.name;
        }
        showItemAccountStatus(tr);
      }));
      tr.querySelector(".keep-account").addEventListener("click", () => acknowledgeItemAccount(tr));
      tr.querySelector(".use-account").addEventListener("click", async () => {
        await refreshItemMatch(tr);
        if (!tr._accountMatch) { alert("此公司與品名尚無可用對應，請手填後選擇沿用"); return; }
        setItemAccount(tr, tr._accountMatch);
        acknowledgeItemAccount(tr);
      });
    });
  }
  function showItemAccountStatus(tr) {
    const pending = tr.dataset.needsReview === "1";
    tr.querySelector(".account-status").textContent = pending ? "科目待核對" :
      tr.dataset.source === "observed" ? "原稿科目" : tr.dataset.source === "mapping" ? "由品名對應帶入" : tr.dataset.source === "manual" ? "人工指定" : "科目待指定";
    tr.querySelectorAll(".keep-account,.use-account").forEach((b) => b.classList.toggle("hidden", !pending));
  }
  function setItemAccount(tr, match) {
    ["account_code", "account_name"].forEach((key) => { tr.querySelector(`[data-item-key="${key}"]`).value = match?.[key] || ""; });
    tr.dataset.source = match ? "mapping" : "";
    tr.dataset.needsReview = "0";
    showItemAccountStatus(tr);
    markRecognitionDirty();
  }
  function acknowledgeItemAccount(tr) {
    reviewedItems[tr.dataset.itemId] = {company_id: selectedCompanyId(),
      item_name_key: normalizedItemName(tr.querySelector('[data-item-key="description"]')?.value),
      account_code: tr.querySelector('[data-item-key="account_code"]').value.trim(),
      account_name: tr.querySelector('[data-item-key="account_name"]').value.trim()};
    tr.dataset.needsReview = "0";
    showItemAccountStatus(tr);
  }
  async function refreshItemMatch(tr) {
    const id = currentId, company = selectedCompanyId(), itemId = tr.dataset.itemId;
    const name = tr.querySelector('[data-item-key="description"]')?.value || "";
    const context = itemContext(company, name), changed = tr.dataset.context !== context;
    const generation = tr._lookupGeneration = (tr._lookupGeneration || 0) + 1;
    // Invalidate the prior context immediately, before a potentially slow request.
    if (changed) {
      delete reviewedItems[itemId];
      if (tr.dataset.source === "mapping") setItemAccount(tr, null);
      else if (tr.querySelector('[data-item-key="account_code"]').value || tr.querySelector('[data-item-key="account_name"]').value) {
        tr.dataset.needsReview = "1"; showItemAccountStatus(tr);
      }
      tr.dataset.context = context;
    }
    const response = await fetch("/api/item-account-mappings/lookup", {method:"POST", headers:json(),
      body:JSON.stringify({company_id: company, items:[{item_id:itemId,description:name}]})});
    if (!response.ok) throw new Error(await responseError(response, "品名對應查詢失敗"));
    const data = await response.json();
    if (generation !== tr._lookupGeneration || id !== currentId || company !== selectedCompanyId() || !tr.isConnected ||
        itemId !== tr.dataset.itemId || context !== itemContext(company, tr.querySelector('[data-item-key="description"]')?.value)) return;
    tr._accountMatch = data.items?.[0]?.match;
    const code = tr.querySelector('[data-item-key="account_code"]').value;
    const accountName = tr.querySelector('[data-item-key="account_name"]').value;
    if (changed && !code && !accountName && tr._accountMatch) setItemAccount(tr, tr._accountMatch);
  }
  $("addItem").addEventListener("click", () => {
    $("itemsBody").insertAdjacentHTML("beforeend", itemRow({})); bindItemRows(); markRecognitionDirty();
  });

  function collectForm() {
    const fields = {...(currentVoucher.fields || {})};
    activeFields.forEach((f) => {
      const el = $(f.el); if (!el) return;
      fields[f.key] = el.value === "" ? null : f.kind === "number" && el.type === "number" ? Number(el.value) : el.value;
    });
    const items = Array.from($("itemsBody").querySelectorAll("tr")).map((tr) => {
      const item = JSON.parse(tr.dataset.original || "{}");
      tr.querySelectorAll("[data-item-key]").forEach((el) => {
        item[el.dataset.itemKey] = el.value === "" ? null : el.dataset.kind === "number" && el.type === "number" ? Number(el.value) : el.value;
      });
      const remember = tr.querySelector?.(".remember-mapping");
      if (remember) item.remember_mapping = remember.checked;
      return item;
    }).filter((it) => Object.entries(it).some(([key,value]) => !itemBookkeeping.has(key) && value != null && value !== ""));
    // The server owns common-field mapping; retain every unrendered field/attribute.
    const typeKey = $("revTypeSel")?.value || currentVoucher.doc_type_key;
    return {...currentVoucher, doc_type_key:typeKey, doc_type:DOC_TYPES[typeKey]?.label || currentVoucher.doc_type,
      fields, items, entries:currentVoucher.entries || [], note: $("f-note").value};
  }

  async function responseError(response, fallback) {
    try { return errText(await response.json(), fallback); }
    catch (_) { return fallback; }
  }
  async function persistCurrentEdits(action = "temporary") {
    if (!currentId) throw new Error("尚未選取憑證");
    const id = currentId;
    await Promise.all(Array.from($("itemsBody").querySelectorAll("tr")).map(refreshItemMatch));
    let choices = {};
    while (true) {
      const voucherResp = await fetch(`/api/vouchers/${id}/save`, {
        method: "POST", headers: json(), body: JSON.stringify({voucher:collectForm(), action,
          company_id:selectedCompanyId(), expected_updated_at:currentVersion,
          reviewed_items:reviewedItems, mapping_choices:choices}),
      });
      const record = await voucherResp.json();
      if (voucherResp.status === 409 && record.conflicts?.length) {
        choices = Object.fromEntries(record.conflicts.map((conflict) => [conflict.item_name_key, {action:"local"}]));
        continue;
      }
      if (voucherResp.status === 409 && record.latest?.updated_at) {
        if (!await reviewLatestVoucher(record.latest)) throw new Error("已取消儲存，輸入內容仍保留");
        currentVersion = record.latest.updated_at;
        choices = {};
        continue;
      }
      if (!voucherResp.ok) throw new Error(errText(record, "儲存修正失敗"));
      applyRecordStatus(record);
      if (currentId === id) {
        currentVoucher = record.current || currentVoucher;
        reviewedItems = {};
        renderItems(currentVoucher.items || []);
        $("itemsSection").classList.remove("hidden");
        applyRecognitionState(record);
      }
      if (record.mapping_skipped?.length) alert("單據已儲存，以下明細未建立共用對應：\n" +
        record.mapping_skipped.map((row) => {
          const item = record.current.items.find((it) => it.item_id === row.item_id) || {};
          return `${item?.description || "空白品名"}：${row.reason}`;
        }).join("\n"));
      return record;
    }
  }

  function chooseMappingConflicts(conflicts) {
    const dialog = $("mappingConflictDialog"), body = $("mappingConflictRows");
    body.innerHTML = conflicts.map((conflict,index) => `<tr><td>${esc(conflict.item_name)}</td>
      <td>${conflict.old ? `${esc(conflict.old.account_code)} ${esc(conflict.old.account_name)}${conflict.old.is_active ? "" : "（已停用）"}` : "尚無規則"}</td>
      <td>${conflict.rows.map((row) => `${esc(row.account_code)} ${esc(row.account_name)}`).join("<br>")}</td>
      <td><select data-conflict-index="${index}"><option value="local">只修改本張</option>
      ${conflict.rows.map((row) => `<option value="${esc(row.item_id)}">更新共用規則：${esc(row.account_code)} ${esc(row.account_name)}</option>`).join("")}</select></td></tr>`).join("");
    return new Promise((resolve) => {
      let finished = false;
      const finish = (value) => { if (!finished) { finished = true; dialog.close(); resolve(value); } };
      $("mappingConflictCancel").onclick = () => finish(null);
      dialog.oncancel = () => finish(null);
      dialog.onclose = () => finish(null);
      $("mappingConflictContinue").onclick = () => {
        const choices = {};
        body.querySelectorAll("select").forEach((select) => {
          const conflict = conflicts[Number(select.dataset.conflictIndex)];
          choices[conflict.item_name_key] = select.value === "local" ? {action:"local"} : {
            action:"update", expected_version:conflict.expected_version, item_id:select.value};
        });
        finish(choices);
      };
      dialog.showModal();
    });
  }

  function reviewLatestVoucher(latest) {
    const local = collectForm(), remote = latest.current || {};
    const spec = DOC_TYPES[local.doc_type_key] || {};
    const labels = Object.fromEntries((spec.fields || []).map((f) => [f.key, f.label]));
    const rows = [];
    const display = (value) => value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
    const add = (label, saved, input) => {
      if (display(saved) !== display(input)) rows.push(`<tr><th>${esc(label)}</th><td>${esc(display(saved))}</td><td>${esc(display(input))}</td></tr>`);
    };
    const companyName = (id) => { const c = companyRecords.find((c) => c.id === id); return c?.short || c?.name || "未指定"; };
    add("歸屬公司", companyName(latest.company_id), companyName(selectedCompanyId()));
    add("單據類型", remote.doc_type, local.doc_type);
    for (const key of new Set([...Object.keys(remote.fields || {}), ...Object.keys(local.fields || {})]))
      add(labels[key] || key, remote.fields?.[key], local.fields?.[key]);
    add("摘要／備註", remote.note, local.note);
    const itemText = (items) => (items || []).map((row) => [row.description, row.account_code, row.account_name,
      row.quantity == null ? "" : `數量 ${row.quantity}`, row.amount == null ? "" : `金額 ${row.amount}`].filter(Boolean).join("／")).join("\n");
    add("明細", itemText(remote.items), itemText(local.items));
    const entryText = (entries) => (entries || []).map((row) => `${row.description || ""}／${row.debit_account ? "借" : "貸"} ${row.debit_account || row.credit_account || ""} ${row.debit_account_name || row.credit_account_name || ""}／${row.amount ?? ""}`).join("\n");
    add("分錄", entryText(remote.entries), entryText(local.entries));
    $("voucherVersionRows").innerHTML = rows.join("") || '<tr><td colspan="3">最新版本已有其他保存紀錄，請確認是否以目前輸入儲存。</td></tr>';
    const dialog = $("voucherVersionDialog");
    return new Promise((resolve) => {
      let finished = false;
      const finish = (value) => { if (!finished) { finished = true; dialog.close(); resolve(value); } };
      $("voucherVersionCancel").onclick = () => finish(false);
      $("voucherVersionContinue").onclick = () => finish(true);
      dialog.oncancel = () => finish(false);
      dialog.onclose = () => finish(false);
      dialog.showModal();
    });
  }

  async function withEditLock(fn) {
    if (editBusy || inferBusy) return;
    editBusy = true;
    const controls = Array.from($("revForm").querySelectorAll("input, textarea, select, button"));
    const disabled = controls.map((control) => control.disabled);
    controls.forEach((control) => { control.disabled = true; });
    try {
      await fn();
    } finally {
      editBusy = false;
      controls.forEach((control, index) => { control.disabled = disabled[index]; });
      $("revForm").querySelectorAll("input,textarea,select,button").forEach((control) => {
        if (!controls.includes(control)) control.disabled = false;
      });
      updateRecognitionState();
    }
  }

  async function saveDraft(returnToImport = false) {
    try {
      const saved = await persistCurrentEdits("save");
      if (returnToImport && currentId === saved.id) {
        clearReview();
        fileInput.value = "";
        setStage("import");
        toast("已儲存，可開始匯入下一份");
      } else {
        toast(saved.status === "confirmed" ? "已儲存修正與備註，維持已確認" : "已儲存草稿，待確認");
      }
      loadList();
    } catch (err) {
      alert(`儲存失敗：${err.message}`);
      loadList();
    }
  }

  async function confirmCurrent() {
    try {
      await persistCurrentEdits("confirm");
      toast("辨識內容已確認，可列印報告");
      loadList();
    } catch (err) {
      alert(`確認失敗：${err.message}`);
      loadList();
    }
  }

  $("btnSave").addEventListener("click", () => withEditLock(() => saveDraft()));
  $("btnReturnImport").addEventListener("click", () => withEditLock(() => saveDraft(true)));
  $("btnConfirm").addEventListener("click", () => withEditLock(() => confirmCurrent()));

  const json = () => ({ "Content-Type": "application/json" });
  function toast(msg) { progressText.textContent = msg; progress.classList.remove("hidden"); setTimeout(hideProgress, 1200); }

  // ── 引擎狀態列（OCR／VLM／LLM）+ 地端推論進度條 ──
  // 地端單張數十秒，使用者需要知道「還在跑」以及「卡在哪一段」。
  // 參考 research_log_agent 的模型活動面板：代理 Ollama /api/ps 取 VRAM／GPU 佔比／卸載倒數。
  let engineTimer = null, engineFast = false, inferBusy = 0, inferLabel = "";

  function unloadIn(expiresAt) {
    if (!expiresAt) return "";
    const s = Math.round((new Date(expiresAt).getTime() - Date.now()) / 1000);
    if (s <= 0) return "即將卸載";
    return s < 60 ? `${s} 秒後卸載` : `${Math.round(s / 60)} 分後卸載`;
  }

  function renderEngine(st) {
    const el = $("eng-" + st.key);
    if (!el) return;
    let cls = "idle", state = "閒置", tip = st.setting;
    if (!st.available) { cls = "bad"; state = "不可用"; tip = st.detail || "不可用"; }
    else if (st.running) { cls = "run"; state = `執行中 ${st.elapsed ?? 0}s`; tip = st.note || st.setting; }
    else if (st.provider === "ollama" && !st.loaded) { cls = "off"; state = "未載入"; }
    else if (st.last_secs != null) { state = `閒置（上次 ${st.last_secs}s）`; }
    if (st.loaded) {
      tip += `\nVRAM ${st.loaded.vram_gb}/${st.loaded.size_gb} GB · ${st.loaded.processor}`
        + `\n${st.loaded.params || ""} ${st.loaded.quant || ""}`.trimEnd()
        + (unloadIn(st.loaded.expires_at) ? `\n${unloadIn(st.loaded.expires_at)}` : "");
    }
    const short = st.key === "ocr" ? "OCR" : st.key === "structure" ? "VLM" : "LLM";
    el.className = "eng";
    el.title = tip;
    el.innerHTML = `<span class="dot ${cls}"></span><b>${short}</b>`
      + `<span class="k">${esc(st.model)}</span>`
      + `<span>${esc(state)}</span>`
      + (st.loaded ? `<span class="k">${st.loaded.vram_gb}GB · ${esc(st.loaded.processor)}</span>` : "");
  }

  function engHint(text, tip) {
    $("engHint").textContent = text;
    $("engHint").title = tip || text;
  }

  async function refreshEngine() {
    try {
      const r = await fetch("/api/engine-status");
      // 讀不到狀態 ≠ Ollama 掛掉，分開講才查得下去
      if (r.status === 401) { engHint("⚠️ 登入逾期，請重新登入"); return; }
      if (r.status === 404) { engHint("⚠️ 伺服器版本較舊（無此端點），請重啟服務"); return; }
      if (!r.ok) { engHint(`⚠️ 狀態讀取失敗（HTTP ${r.status}）`); return; }
      const d = await r.json();
      (d.stages || []).forEach(renderEngine);
      const busy = (d.stages || []).find((s) => s.running);
      if (d.ollama_ok) engHint(d.ollama_stale ? "⌛ Ollama 回應緩慢（沿用前次狀態）" : "");
      else engHint("⚠️ Ollama 未連線", d.ollama_error || "");
      if (inferBusy) {
        $("revProgressText").textContent = busy
          ? `${inferLabel}　目前：${busy.label}（${busy.note || busy.model}）${busy.elapsed ?? 0}s`
          : `${inferLabel}　準備中…`;
      }
      // 有階段在跑就加快輪詢，閒置時退回 3 秒，避免無謂請求
      const wantFast = !!busy || inferBusy > 0;
      if (wantFast !== engineFast) { engineFast = wantFast; startEnginePoll(); }
    } catch (e) { engHint("⚠️ 無法連線伺服器", String(e && e.message || e)); }
  }

  function startEnginePoll() {
    clearInterval(engineTimer);
    engineTimer = setInterval(refreshEngine, engineFast ? 1000 : 3000);
  }

  /** 包住長時間地端推論：顯示不定量進度條與目前階段，結束自動收起。 */
  async function withInferProgress(label, fn) {
    inferBusy++; inferLabel = label;
    $("revProgressText").textContent = label;
    $("revProgress").classList.remove("hidden");
    engineFast = true; startEnginePoll(); refreshEngine();
    try {
      return await fn();
    } finally {
      inferBusy--;
      if (!inferBusy) $("revProgress").classList.add("hidden");
      refreshEngine();
    }
  }

  // ── 影像手動旋轉 + 重新辨識 ──
  async function rotateImage(dir) {
    if (!currentId) return;
    const resp = await fetch(`/api/vouchers/${currentId}/rotate`, { method: "POST", headers: json(), body: JSON.stringify({ direction: dir, expected_updated_at:currentVersion }) });
    if (!resp.ok) { alert(await responseError(resp, "旋轉失敗")); return; }
    const result = await resp.json();
    if (!result.ok) { alert("旋轉失敗"); return; }
    await refreshCurrentStatus();
    reloadImage();   // 旋轉後清框（不再對齊），並丟掉尚未套用的角度預覽
    if (currentVoucher.origin?.preparation?.mode === "manual_review") await openReview(currentId);
    toast("已旋轉；完成影像整理後按「確認外觀」加入掃描清單");
  }
  $("btnRotL").addEventListener("click", () => rotateImage("ccw"));
  $("btnRotR").addEventListener("click", () => rotateImage("cw"));

  function reloadImage() {
    const img = $("revImage");
    img.onload = () => { resetZoom(); drawBoxes({}); };
    img.src = `/api/vouchers/${currentId}/image?t=${Date.now()}`;
    if (currentVoucher) currentVoucher.boxes = {};
    resetFineTool();
  }

  // ── 微調角度（去歪斜）：先用 CSS 預覽，按「套用」才寫入影像 ──
  // fineDelta 是「這一次還要再轉的角度」；已套用的累計角度在 preparation.fine_angle。
  // 寫入時送累計角度，伺服器每次都從微調前的原圖轉一次，不會一層層重取樣。
  const FINE_LIMIT = 15;
  let fineDelta = 0, fineBusy = false, edgeStart = null;
  const round1 = (v) => Math.round(v * 10) / 10;
  const round2 = (v) => Math.round(v * 100) / 100;
  function fineApplied() { return Number(currentVoucher?.origin?.preparation?.fine_angle) || 0; }
  function clampFineDelta(d) {
    const applied = fineApplied();
    return Math.max(-FINE_LIMIT - applied, Math.min(FINE_LIMIT - applied, d));
  }
  function updateFineControls(keepInput) {
    const applied = fineApplied();
    const locked = imageToolsLocked || cropping || fineBusy;
    const slider = $("fineSlider");
    slider.min = String(round2(-FINE_LIMIT - applied)); slider.max = String(round2(FINE_LIMIT - applied));
    slider.value = String(fineDelta);
    if (!keepInput) $("fineAngle").value = String(fineDelta);
    ["btnFineMinus", "btnFinePlus", "fineSlider", "fineAngle", "btnFineEdge"].forEach((id) => { $(id).disabled = locked; });
    $("btnFineRot").disabled = locked || fineDelta === 0;
    $("btnFineCancel").disabled = locked || (fineDelta === 0 && !edgeMode);
    $("btnFineReset").disabled = locked || applied === 0;
    $("btnFineEdge").classList.toggle("active", edgeMode);
    ["btnRotL", "btnRotR", "btnCrop"].forEach((id) => { $(id).disabled = imageToolsLocked || fineBusy; });
    $("fineGuide").classList.toggle("hidden", !(fineDelta !== 0 || edgeMode));
  }
  function renderFinePreview() {
    const img = $("revImage");
    if (fineDelta === 0) { img.style.transform = ""; img.classList.remove("fine-preview"); return; }
    const rad = Math.abs(fineDelta) * Math.PI / 180, c = Math.cos(rad), s = Math.sin(rad);
    const w = img.clientWidth || 1, h = img.clientHeight || 1;
    // 伺服器旋轉後畫布會變大、再縮放顯示；預覽用同樣縮放，整張才看得到。
    const scale = Math.min(w / (w * c + h * s), h / (w * s + h * c));
    img.classList.add("fine-preview");
    img.style.transform = `rotate(${fineDelta}deg) scale(${scale})`;
  }
  function setFineDelta(value, opts = {}) {
    let d = Number(value);
    if (!Number.isFinite(d)) d = 0;
    const clamped = round1(clampFineDelta(d));
    if (clamped !== round1(d) && !opts.quiet) toast(`累計角度上限 ±${FINE_LIMIT}°（目前已套用 ${fineApplied()}°）`);
    fineDelta = clamped;
    renderFinePreview();
    updateFineControls(opts.keepInput);
  }
  function exitEdgeMode() {
    edgeMode = false; edgeStart = null;
    $("edgeSvg").classList.add("hidden");
    const inner = document.querySelector(".rev-image-inner");
    if (inner) inner.classList.remove("in-edge");
  }
  /** 丟掉尚未套用的預覽與拉線，回到已套用的圖（取消、換圖、改圖後都走這裡）。 */
  function resetFineTool() {
    fineDelta = 0;
    exitEdgeMode();
    renderFinePreview();
    updateFineControls();
  }
  function updateRestoreButton(available) {
    $("btnRestore").disabled = !available || imageToolsLocked || fineBusy;
  }
  $("btnFineMinus").addEventListener("click", () => setFineDelta(round1(fineDelta - 0.5)));
  $("btnFinePlus").addEventListener("click", () => setFineDelta(round1(fineDelta + 0.5)));
  $("fineSlider").addEventListener("input", (e) => setFineDelta(e.target.value, { quiet: true }));
  $("fineAngle").addEventListener("input", (e) => setFineDelta(e.target.value, { keepInput: true, quiet: true }));
  $("fineAngle").addEventListener("change", (e) => setFineDelta(e.target.value));
  $("btnFineCancel").addEventListener("click", resetFineTool);
  $("btnFineEdge").addEventListener("click", () => {
    if (cropping || imageToolsLocked) return;
    if (edgeMode) { exitEdgeMode(); updateFineControls(); return; }
    edgeMode = true;
    document.querySelector(".rev-image-inner")?.classList.add("in-edge");
    updateFineControls();
    toast("沿單據邊緣或表格線拉一條線，放開後會算出要轉的角度");
  });
  /** 沿邊緣拉線 → 讓這條線變水平（或垂直）所需的最小順時針角度，折進 ±45°。 */
  function edgeCorrection(dx, dy) {
    const theta = Math.atan2(dy, dx) * 180 / Math.PI;      // 螢幕座標（y 向下），正值＝順時針
    const folded = ((theta + 45) % 90 + 90) % 90 - 45;     // 與水平／垂直的夾角
    return -folded;                                        // 反向轉掉這個夾角
  }
  (function initEdgeLine() {
    const inner = document.querySelector(".rev-image-inner");
    const svg = $("edgeSvg"), line = $("edgeLine");
    if (!inner || !svg || !line) return;
    const point = (e) => {
      const r = inner.getBoundingClientRect();
      return { x: Math.max(0, Math.min(r.width, e.clientX - r.left)), y: Math.max(0, Math.min(r.height, e.clientY - r.top)) };
    };
    inner.addEventListener("mousedown", (e) => {
      if (!edgeMode) return;
      edgeStart = point(e);
      line.setAttribute("x1", edgeStart.x); line.setAttribute("y1", edgeStart.y);
      line.setAttribute("x2", edgeStart.x); line.setAttribute("y2", edgeStart.y);
      svg.classList.remove("hidden"); e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!edgeMode || !edgeStart) return;
      const p = point(e);
      line.setAttribute("x2", p.x); line.setAttribute("y2", p.y);
    });
    window.addEventListener("mouseup", (e) => {
      if (!edgeMode || !edgeStart) return;
      const p = point(e), dx = p.x - edgeStart.x, dy = p.y - edgeStart.y;
      if (Math.hypot(dx, dy) < 30) {
        edgeStart = null; svg.classList.add("hidden");
        toast("線太短，請沿邊緣拉長一點");
        return;
      }
      // 畫面上的線已包含目前預覽的旋轉，所以是在現有預覽上再加一次修正。
      const next = fineDelta + edgeCorrection(dx, dy);
      exitEdgeMode();
      setFineDelta(next);
    });
  })();
  async function applyFine(reset) {
    if (!currentId || fineBusy) return;
    const angle = reset ? 0 : round2(fineApplied() + fineDelta);
    if (reset ? fineApplied() === 0 : fineDelta === 0) return;
    fineBusy = true; updateFineControls();
    try {
      const resp = await fetch(`/api/vouchers/${currentId}/rotate-fine`, { method: "POST", headers: json(), body: JSON.stringify({ angle, reset, expected_updated_at:currentVersion }) });
      if (!resp.ok) { alert(await responseError(resp, reset ? "還原微調失敗" : "微調失敗")); return; }
      if (!(await resp.json()).ok) { alert(reset ? "還原微調失敗" : "微調失敗"); return; }
      await refreshCurrentStatus();
      fineBusy = false;
      reloadImage(); if (currentVoucher.origin?.preparation?.mode === "manual_review") await openReview(currentId);
      toast(reset ? "已還原微調；完成影像整理後按「確認外觀」加入掃描清單" : `已套用微調，累計 ${angle}°；完成影像整理後按「確認外觀」加入掃描清單`);
    } finally {
      fineBusy = false; updateFineControls();
    }
  }
  $("btnFineRot").addEventListener("click", () => applyFine(false));
  $("btnFineReset").addEventListener("click", () => applyFine(true));

  // 復原：影像回到剛進入外觀確認時的狀態（旋轉、微調、裁切全部撤銷），不呼叫模型。
  $("btnRestore").addEventListener("click", async () => {
    if (!currentId || $("btnRestore").disabled) return;
    if (!confirm("復原會把影像換回剛進入外觀確認時的狀態，旋轉、微調與裁切都會撤銷。\n\n確定要復原嗎？")) return;
    const resp = await fetch(`/api/vouchers/${currentId}/restore-image`, { method: "POST", headers: json(), body: JSON.stringify({ expected_updated_at:currentVersion }) });
    if (!resp.ok) { alert(await responseError(resp, "復原失敗")); return; }
    if (!(await resp.json()).ok) { alert("復原失敗"); return; }
    await refreshCurrentStatus();
    reloadImage(); await openReview(currentId);
    toast("已復原為剛進入外觀確認時的影像；完成影像整理後按「確認外觀」加入掃描清單");
  });

  // ── 裁切模式（只修目前這一張，不拆出新憑證）──
  let cropStart = null;
  function setCropMode(on) {
    cropping = on;
    $("btnCrop").classList.toggle("hidden", on);
    $("btnCropOk").classList.toggle("hidden", !on);
    $("btnCropCancel").classList.toggle("hidden", !on);
    const inner = document.querySelector(".rev-image-inner");
    if (inner) inner.classList.toggle("in-crop", on);   // 十字游標（含影像區）
    if (on) { resetZoom(); resetFineTool(); }   // 裁切座標以未旋轉預覽的影像為準
    else updateFineControls();
    if (!on) { $("cropSel").classList.add("hidden"); cropStart = null; }
  }
  $("btnCrop").addEventListener("click", () => setCropMode(true));
  $("btnCropCancel").addEventListener("click", () => setCropMode(false));

  (function initCrop() {
    const inner = document.querySelector(".rev-image-inner");
    const sel = $("cropSel");
    if (!inner || !sel) return;
    inner.addEventListener("mousedown", (e) => {
      if (!cropping) return;
      const r = inner.getBoundingClientRect();
      cropStart = { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height };
      sel.style.left = cropStart.x + "px"; sel.style.top = cropStart.y + "px";
      sel.style.width = "0px"; sel.style.height = "0px";
      sel.classList.remove("hidden"); e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!cropping || !cropStart || cropStart.done) return;   // 放開後不再更新
      const r = inner.getBoundingClientRect();
      const x = Math.max(0, Math.min(r.width, e.clientX - r.left));
      const y = Math.max(0, Math.min(r.height, e.clientY - r.top));
      sel.style.left = Math.min(x, cropStart.x) + "px";
      sel.style.top = Math.min(y, cropStart.y) + "px";
      sel.style.width = Math.abs(x - cropStart.x) + "px";
      sel.style.height = Math.abs(y - cropStart.y) + "px";
    });
    window.addEventListener("mouseup", () => { if (cropping && cropStart) cropStart.done = true; });
  })();

  $("btnCropOk").addEventListener("click", async () => {
    const sel = $("cropSel"), inner = document.querySelector(".rev-image-inner");
    if (!cropStart || sel.classList.contains("hidden")) { setCropMode(false); return; }
    const iw = inner.clientWidth, ih = inner.clientHeight;
    const L = parseFloat(sel.style.left) / iw, T = parseFloat(sel.style.top) / ih;
    const W = parseFloat(sel.style.width) / iw, H = parseFloat(sel.style.height) / ih;
    if (W < 0.02 || H < 0.02) { alert("裁切範圍太小"); return; }
    const box = [L, T, L + W, T + H];
    if (!confirm("「裁切」會取代目前影像，框外的部分會被去除。\n要回到剛進入外觀確認時的影像，請按「復原」。\n\n確定要裁切目前這張嗎？")) return;
    const r = await fetch(`/api/vouchers/${currentId}/crop`, { method: "POST", headers: json(), body: JSON.stringify({ box, expected_updated_at:currentVersion }) });
    setCropMode(false);
    if (!r.ok) { alert(await responseError(r, "裁切失敗")); return; }
    if (!(await r.json()).ok) { alert("裁切失敗"); return; }
    await refreshCurrentStatus();
    reloadImage(); if (currentVoucher.origin?.preparation?.mode === "manual_review") await openReview(currentId);
    toast("已裁切；完成影像整理後按「確認外觀」加入掃描清單");
  });
  function clearReview() {
    $("revForm").classList.add("hidden"); $("revEmpty").classList.remove("hidden"); currentId = null;
    currentManualEntry = false;
    recognitionReady = false; recognitionDirty = false; updateRecognitionState();
  }
  // 外觀確認 → 加入掃描清單（prepared → queued）
  async function confirmAppearance() {
    if (!currentId) return;
    const tk = $("revTypeSel").value;
    // 外觀確認固定一頁一張、一次一張；不再提供多張切分。
    const d = await (await fetch(`/api/vouchers/${currentId}/confirm-appearance`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk,expected_updated_at:currentVersion,document_mode:"single" }) })).json();
    if (!d.ok) { alert(errText(d, "確認失敗")); return; }
    toast("已加入掃描清單"); clearReview(); loadList(); restoreImportJobs();
  }

  $("btnReRecognize").addEventListener("click", runRecognize);
  $("btnConfirmRecognize").addEventListener("click", () => {
    if (stageAction === "confirm") confirmAppearance(); else runRecognize();
  });
  $("revCompanySel").addEventListener("change", async () => {
    updateReviewCompanyTaxId();
    if (!currentId) return;
    try {
      await loadItemAccountChart();
      if ($("revForm").classList.contains("prepared")) {
        const company = companyRecords.find((c) => c.id === selectedCompanyId());
        const r = await fetch(`/api/vouchers/${currentId}/company`, { method:"POST",headers:json(),
          body:JSON.stringify({company:company?.account_chart_key || "", expected_updated_at:currentVersion})});
        if (!r.ok) throw new Error(await responseError(r, "歸屬公司儲存失敗"));
        applyRecordStatus(await r.json());
      } else {
        await Promise.all(Array.from($("itemsBody").querySelectorAll("tr")).map(refreshItemMatch));
      }
    } catch (err) { alert(err.message); }
  });
  $("revTypeSel").addEventListener("change", async () => {
    if (!currentId) return;
    const tk = $("revTypeSel").value;
    if (!$("revForm").classList.contains("prepared")) {
      await withEditLock(async () => {
        try {
          const record = await persistCurrentEdits();
          buildActiveFields(tk); renderFields(record.current?.fields || {});
          loadList();
        } catch (err) { alert(err.message); }
      });
      return;
    }
    const resp = await fetch(`/api/vouchers/${currentId}/set-type`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk, expected_updated_at:currentVersion }) });
    if (!resp.ok) { alert(await responseError(resp, "單據類型儲存失敗")); return; }
    await refreshCurrentStatus();
    currentVoucher.doc_type_key = tk;
    currentVoucher.doc_type = DOC_TYPES[tk]?.label || currentVoucher.doc_type;
    buildActiveFields(tk);
    renderFields(currentVoucher.fields || {});
    loadList();
  });

  // ── 模型設定 modal ──
  $("settingsBtn").addEventListener("click", openSettings);
  $("closeModal").addEventListener("click", () => modal.classList.add("hidden"));
  $("saveSettings").addEventListener("click", saveSettings);

  async function openSettings() {
    if (!isAdminUser()) return;
    const [models, cfg] = await Promise.all([
      (await fetch("/api/models?local_only=true")).json(), (await fetch("/api/config")).json(),
    ]);
    fillSelect("sel-ocr", models.ocr, cfg.ocr);
    const os = models.ocr_status || {};
    $("ocrStatus").innerHTML = os.available === false
      ? `<span class="ocr-bad">⚠️ ${os.detail || "地端 OCR 不可用"}</span>`
      : `<span class="ocr-ok">✅ ${os.detail || "地端 OCR 可用"}</span>`;
    fillSelect("sel-structure", models.structure, cfg.structure);
    fillSelect("sel-classify", models.classify, cfg.classify);
    modal.classList.remove("hidden");
  }
  function fillSelect(id, options, selected) {
    const prefix = id === "sel-ocr" ? "paddle/" : "ollama/";
    const localModels = (options || []).filter((m) => typeof m === "string" && m.startsWith(prefix));
    const placeholder = localModels.includes(selected) ? ""
      : `<option value="" selected disabled>${localModels.length ? "請選擇地端模型" : "無可用的地端模型"}</option>`;
    const label = (m) => (prefix === "paddle/" ? "地端 OCR " : "地端 ") + m.slice(prefix.length);
    $(id).innerHTML = placeholder + localModels.map((m) => `<option value="${esc(m)}" ${m === selected ? "selected" : ""}>${esc(label(m))}</option>`).join("");
  }
  async function saveSettings() {
    const settings = { ocr: $("sel-ocr").value, structure: $("sel-structure").value, classify: $("sel-classify").value };
    if (!settings.ocr.startsWith("paddle/") || !settings.structure.startsWith("ollama/") || !settings.classify.startsWith("ollama/")) {
      alert("請先選擇各階段的地端模型"); return;
    }
    try {
      const response = await fetch("/api/config", { method: "POST", headers: json(), body: JSON.stringify(settings) });
      if (!response.ok) throw new Error(await responseError(response, "模型設定儲存失敗"));
      modal.classList.add("hidden");
    } catch (err) {
      alert(`模型設定儲存失敗：${err.message}`);
    }
  }

  async function printVoucher() {
    if (!recognitionReady || recognitionDirty || editBusy) { alert("請先儲存並確認目前內容，再列印報告"); return; }
    const id = currentId, version = currentVersion;
    const preview = window.open("", "_blank");
    if (!preview) { alert("請允許開啟列印視窗"); return; }
    try {
      const response = await fetch(`/api/vouchers/${id}/recognition-report?expected_updated_at=${encodeURIComponent(version)}`);
      if (!response.ok) {
        const error = await response.json();
        throw new Error(errText(error, "報告無法列印，請重新確認"));
      }
      const html = await response.text();
      if (id !== currentId || version !== currentVersion || recognitionDirty) throw new Error("內容已變更，請重新確認");
      preview.document.write(html);
      preview.document.close();
    } catch (error) { preview.close(); alert(error.message); }
  }
  $("btnPrintVoucher").addEventListener("click", printVoucher);

  // 深連結：#v=<id> 自動開啟該筆審核
  function openFromHash() {
    const m = location.hash.match(/v=(\d+)/);
    if (m) openReview(Number(m[1]));
  }
  window.addEventListener("hashchange", openFromHash);

  // ── 登入 / 角色 ──
  $("loginBtn").addEventListener("click", doLogin);
  $("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("logoutBtn").addEventListener("click", async () => {
    stopImportPolling(); currentUser = null;
    await fetch("/api/logout", { method: "POST" }); location.reload();
  });

  async function doLogin() {
    const r = await fetch("/api/login", { method: "POST", headers: json(),
      body: JSON.stringify({ username: $("loginUser").value, password: $("loginPass").value }) });
    if (!r.ok) { $("loginErr").textContent = "帳號或密碼錯誤"; return; }
    currentUser = await r.json();
    enterApp();
  }

  function applyRole() {
    const canAdmin = currentUser && currentUser.is_admin === true;
    $("databaseBtn").classList.toggle("hidden", !canAdmin);
    $("usersBtn").classList.toggle("hidden", !canAdmin);
    $("settingsBtn").classList.toggle("hidden", !canAdmin);
  }

  async function enterApp() {
    importJobGeneration += 1;
    $("loginOverlay").classList.add("hidden");
    $("logoutBtn").classList.remove("hidden");
    const chip = $("userChip");
    chip.textContent = `${currentUser.name}（${currentUser.is_admin ? "系統管理員" : "一般使用者"}）`;
    chip.classList.remove("hidden");
    applyRole();
    loadCompanies();
    try { DOC_TYPES = (await (await fetch("/api/doc-types")).json()).types || {}; } catch (e) { DOC_TYPES = {}; }
    setStage("import"); refreshStats();
    await restoreImportJobs(); openFromHash();
    refreshEngine(); startEnginePoll();
  }

  async function boot() {
    const r = await fetch("/api/me");
    if (r.ok) { currentUser = await r.json(); enterApp(); }
    else $("loginOverlay").classList.remove("hidden");
  }

  // 載入公司清單到上傳下拉
  let COMPANIES = [];
  function companyTaxIdText(company) {
    if (!company) return "";
    return company.tax_id ? `統編：${company.tax_id}` : "統編未設定";
  }
  function updateUploadCompanyTaxId() {
    const selected = $("uploadCompany").value;
    const company = companyRecords.find((c) => (c.short || c.name) === selected);
    $("uploadCompanyTaxId").textContent = companyTaxIdText(company);
  }
  function updateReviewCompanyTaxId() {
    const selected = $("revCompanySel").value;
    const company = companyRecords.find((c) => String(c.id) === selected);
    $("revCompanyTaxId").textContent = companyTaxIdText(company);
  }
  $("uploadCompany").addEventListener("change", updateUploadCompanyTaxId);
  async function loadCompanies() {
    const data = await (await fetch("/api/companies")).json();
    companyRecords = data.companies || [];
    COMPANIES = (data.companies || []).map((c) => c.short || c.name);
    const opts = COMPANIES.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    $("uploadCompany").innerHTML = `<option value="">未指定</option>` + opts;
    $("revCompanySel").innerHTML = `<option value="">未指定</option>` + companyRecords.map((c) =>
      `<option value="${esc(c.id)}">${esc(c.short || c.name)}</option>`).join("");
    updateUploadCompanyTaxId();
    updateReviewCompanyTaxId();
  }

  // ── 左右欄可拖拉調整寬度 ──
  (function () {
    const layout = document.querySelector(".layout");
    const gutter = $("gutter");
    if (!layout || !gutter) return;
    const saved = localStorage.getItem("hx-left-w");
    if (saved) layout.style.setProperty("--left-w", saved);
    let dragging = false;
    gutter.addEventListener("mousedown", (e) => {
      dragging = true; gutter.classList.add("dragging");
      document.body.classList.add("col-resizing"); e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const rect = layout.getBoundingClientRect();
      let x = e.clientX - rect.left - 20;                 // 扣左邊 padding
      x = Math.max(280, Math.min(rect.width - 40 - 360, x)); // 兩側最小寬
      layout.style.setProperty("--left-w", x + "px");
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false; gutter.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      localStorage.setItem("hx-left-w", layout.style.getPropertyValue("--left-w"));
    });
    gutter.addEventListener("dblclick", () => {
      layout.style.setProperty("--left-w", "1fr");
      localStorage.removeItem("hx-left-w");
    });
  })();

  // ── 審核區內：影像/欄位比例可拖拉 ──
  (function () {
    const split = $("revSplit"), g = $("revGutter");
    if (!split || !g) return;
    const saved = localStorage.getItem("hx-img-w");
    if (saved) split.style.setProperty("--img-w", saved);
    let dragging = false;
    g.addEventListener("mousedown", (e) => {
      dragging = true; g.classList.add("dragging");
      document.body.classList.add("col-resizing"); e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const rect = split.getBoundingClientRect();
      let pct = (e.clientX - rect.left) / rect.width * 100;
      pct = Math.max(25, Math.min(80, pct));            // 影像 25%~80%
      split.style.setProperty("--img-w", pct.toFixed(1) + "%");
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false; g.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      localStorage.setItem("hx-img-w", split.style.getPropertyValue("--img-w"));
    });
    g.addEventListener("dblclick", () => {
      split.style.setProperty("--img-w", "55%");
      localStorage.removeItem("hx-img-w");
    });
  })();

  // 初始：先確認登入狀態
  boot();
})();
