(() => {
  const $ = (id) => document.getElementById(id);
  const dropZone = $("dropZone"), fileInput = $("fileInput");
  const progress = $("progress"), barFill = $("barFill"), progressText = $("progressText");
  const modal = $("modal");
  let jobTimer = null;
  let currentId = null;
  let currentVoucher = {};   // 目前載入的 current（保留 entries 等未編輯欄位）
  let currentUser = null;
  let currentIsImport = false;
  let DOC_TYPES = {};   // 各單據類型欄位契約（/api/doc-types）
  let cropping = false; // 裁切模式中

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
    prepared: "待外觀確認", queued: "待提取", pending: "待審", clerk_reviewed: "記帳士已確認",
    accountant_approved: "會計師已核定", rejected: "退回",
  };
  const STATUS_CLASS = {
    prepared: "prep", queued: "scan", pending: "", clerk_reviewed: "warn",
    accountant_approved: "high", rejected: "low",
  };
  // 階段分頁 → 對應狀態（import 為動作視圖，非狀態）
  const STAGE_STATUS = {
    prepared: "prepared", queued: "queued", pending: "pending",
    clerk_reviewed: "clerk_reviewed", accountant_approved: "accountant_approved", rejected: "rejected",
  };
  const STAGE_TITLE = {
    prepared: "② 外觀確認佇列", queued: "③ 掃描清單（待提取）", pending: "④ 待審佇列",
    clerk_reviewed: "⑤ 記帳士確認佇列", accountant_approved: "⑥ 會計師核定佇列", rejected: "退回佇列",
  };
  let currentStage = "import";
  let stageAction = "";   // 目前紀錄的階段動作：confirm / recognize
  const nf = (n) => (n == null || n === "" ? "—" : Number(n).toLocaleString("zh-TW"));
  const esc = (s) => (s == null ? "" : String(s)).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

  // ── 上傳 ──
  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("over"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("over"));
  dropZone.addEventListener("drop", (e) => { e.preventDefault(); dropZone.classList.remove("over"); handleFiles(e.dataTransfer.files); });
  fileInput.addEventListener("change", (e) => handleFiles(e.target.files));

  function handleFiles(fileList) {
    const files = Array.from(fileList);
    if (!files.length) return;
    if (files.length === 1) analyzeSingle(files[0]); else runBatch(files);
  }

  async function analyzeSingle(file) {
    showProgress("準備中…（判類型／自動轉正）", 0.3);
    const fd = new FormData(); fd.append("file", file);
    fd.append("company", $("uploadCompany").value);
    try {
      const data = await (await fetch("/api/analyze", { method: "POST", body: fd })).json();
      if (!data.ok) throw new Error(errText(data, "匯入失敗"));
      hideProgress();
      toast(`已匯入 ${data.count || 1} 張，請在②外觀確認`);
      setStage("prepared");
      if (data.id) openReview(data.id);
    } catch (err) { hideProgress(); alert("錯誤：" + err.message); }
  }

  async function runBatch(files) {
    const fd = new FormData(); files.forEach((f) => fd.append("files", f));
    fd.append("company", $("uploadCompany").value);
    showProgress("批次上傳中…", 0.1);
    const data = await (await fetch("/api/batch", { method: "POST", body: fd })).json();
    if (!data.ok) { hideProgress(); alert(errText(data, "批次失敗")); return; }
    pollJob();
  }

  function pollJob() {
    clearInterval(jobTimer);
    jobTimer = setInterval(async () => {
      const job = await (await fetch("/api/job")).json();
      const pct = job.total ? job.done / job.total : 0;
      showProgress(`準備中 ${job.done}/${job.total}　${job.current || ""}`, pct);
      if (job.status === "completed") {
        clearInterval(jobTimer); hideProgress();
        toast("匯入完成，請在②外觀確認"); setStage("prepared");
        if (job.errors && job.errors.length) alert("部分失敗：\n" + job.errors.join("\n"));
      }
    }, 1500);
  }

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
        toast("提取完成，已移至④待審"); loadList(); refreshStats();
        if (job.errors && job.errors.length) alert("部分失敗：\n" + job.errors.join("\n"));
      }
    }, 1500);
  });
  function eProgress(text, pct) { $("eProgress").classList.remove("hidden"); $("eBarFill").style.width = Math.round(pct * 100) + "%"; $("eProgressText").textContent = text; }
  function eProgressHide() { $("eProgress").classList.add("hidden"); }

  // ── ①匯入：瀏覽器選整個資料夾 ──
  $("dirInput").addEventListener("change", (e) => {
    const files = Array.from(e.target.files).filter((f) => /\.(pdf|jpe?g|png|bmp|tiff?|webp)$/i.test(f.name));
    if (!files.length) { alert("資料夾內沒有可匯入的檔案"); return; }
    runBatch(files);
  });

  // ── ①匯入：伺服器端掃描來源目錄 ──
  $("btnScanList").addEventListener("click", async () => {
    const d = await (await fetch("/api/scan-source/list")).json();
    const box = $("scanFiles");
    if (!d.ok) { box.innerHTML = ""; $("scanDirHint").textContent = errText(d, "讀取失敗"); $("btnScanImport").classList.add("hidden"); return; }
    if (!d.files.length) { box.innerHTML = ""; $("scanDirHint").textContent = `目錄 ${d.dir} 內沒有可匯入的檔案。`; $("btnScanImport").classList.add("hidden"); return; }
    $("scanDirHint").textContent = `目錄：${d.dir}（勾選後匯入）`;
    box.innerHTML = d.files.map((f) => `<label class="scan-row"><input type="checkbox" value="${esc(f.name)}" checked>${esc(f.name)}<span class="hint">${(f.size / 1024).toFixed(0)} KB</span></label>`).join("");
    $("btnScanImport").classList.remove("hidden");
  });
  $("btnScanImport").addEventListener("click", async () => {
    const names = Array.from($("scanFiles").querySelectorAll("input:checked")).map((i) => i.value);
    if (!names.length) { alert("請勾選要匯入的檔案"); return; }
    const d = await (await fetch("/api/scan-source/import", { method: "POST", headers: json(), body: JSON.stringify({ filenames: names, company: $("uploadCompany").value }) })).json();
    if (!d.ok) { alert(errText(d, "匯入失敗")); return; }
    toast(`開始匯入 ${d.total} 個檔案…`); pollJob();
  });

  function showProgress(text, pct) { progress.classList.remove("hidden"); barFill.style.width = Math.round(pct * 100) + "%"; progressText.textContent = text; }
  function hideProgress() { progress.classList.add("hidden"); }

  // ── 階段分頁 ──
  document.querySelectorAll(".stage-tab").forEach((t) =>
    t.addEventListener("click", () => setStage(t.dataset.stage)));

  function setStage(stage) {
    currentStage = stage;
    document.querySelectorAll(".stage-tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.stage === stage));
    const isImport = stage === "import";
    $("importView").classList.toggle("hidden", !isImport);
    $("queueView").classList.toggle("hidden", isImport);
    $("btnExtractAll").classList.toggle("hidden", stage !== "queued");
    $("btnMerge").classList.toggle("hidden", !(stage === "prepared" || stage === "queued"));
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

  // ── 清單 ──
  async function loadList() {
    if (currentStage === "import") { refreshStats(); return; }
    const status = STAGE_STATUS[currentStage] || "";
    const url = "/api/vouchers" + (status ? `?status=${status}` : "");
    const data = await (await fetch(url)).json();
    const rows = data.items || [];
    const body = $("listBody");
    if (!rows.length) { $("listTable").classList.add("hidden"); $("listEmpty").classList.remove("hidden"); refreshStats(); return; }
    $("listEmpty").classList.add("hidden"); $("listTable").classList.remove("hidden");
    const mergeable = currentStage === "prepared" || currentStage === "queued";
    body.innerHTML = rows.map((r) => {
      const chk = mergeable ? `<input type="checkbox" class="mchk" data-id="${r.id}">` : "";
      const att = r.attachments ? ` <span class="att-badge" title="含 ${r.attachments} 附頁">📎${r.attachments}</span>` : "";
      // 憑證號常是空的（對帳單、部分收據無單號），故以「種類」為第二行辨識依據，
      // 否則清單只剩一排數字看不出是什麼單
      // 以欄位契約的正式標籤為準；current_json.doc_type 是模型回傳的字串，可能簡略或走樣
      const spec = DOC_TYPES[r.doc_type_key];
      const type = esc((spec && spec.label) || r.doc_type) || "（未判類型）";
      const manual = r.manual_entry ? ` <span class="att-badge" title="欄位已人工修正確認">✍ 人工修正</span>` : "";
      const sourceRole = r.record_role === "source_page" || r.origin?.layout?.auto_split === true;
      const sourceTag = sourceRole
        ? ` <span class="att-badge" title="來源頁保留供追溯${r.origin?.page_no != null ? `；原始頁碼 ${esc(r.origin.page_no)}` : ""}">來源頁${r.origin?.page_no != null ? ` p${esc(r.origin.page_no)}` : ""}</span>`
        : r.source_parent_id != null ? ` <span class="att-badge" title="此單據由來源憑證拆分">來源 #${esc(r.source_parent_id)}</span>` : "";
      return `
      <tr data-id="${r.id}" class="${r.id === currentId ? "sel" : ""}" title="${esc(r.source_file || "")}">
        <td>${chk}${esc(r.voucher_id) || "—"}${att}${sourceTag}<div class="row-sub">${type}${manual}</div></td>
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

  // ── 審核編輯 ──
  async function openReview(id) {
    currentId = id;
    if (cropping) setCropMode(false);
    const rec = await (await fetch(`/api/vouchers/${id}`)).json();
    if (rec.detail) { alert(errText(rec, "操作失敗")); return; }
    const v = rec.current || {};
    currentVoucher = v;
    currentIsImport = !!rec.is_import;
    const autoSplitSource = v.origin?.layout?.auto_split === true;
    const sourcePage = rec.record_role === "source_page" || autoSplitSource;
    $("revForm").classList.toggle("source-only", autoSplitSource);
    $("reviewBody").classList.toggle("hidden", autoSplitSource);
    $("importBadge").classList.toggle("hidden", !currentIsImport);
    $("revEmpty").classList.add("hidden"); $("revForm").classList.remove("hidden");
    setStatus(rec.status);
    setPendingActionVisibility(rec.status);
    const isPrepared = rec.status === "prepared";
    const isQueued = rec.status === "queued";
    // 退回是唯一沒有出口的狀態，補上「重新送審」；其餘階段不顯示
    $("btnResubmit").classList.toggle("hidden", rec.status !== "rejected");
    const isPreExtract = isPrepared || isQueued;   // 尚未萃取欄位：隱藏編輯器
    $("revForm").classList.toggle("prepared", isPreExtract);
    $("prepHint").classList.toggle("hidden", !isPreExtract && !autoSplitSource);
    $("btnConfirmRecognize").classList.toggle("hidden", !isPreExtract);
    $("btnConfirmRecognize").disabled = sourcePage;
    $("btnReRecognize").disabled = sourcePage;
    $("btnClassify").disabled = sourcePage;
    ["btnRotL", "btnRotR", "btnFineRot", "btnCrop", "btnSplit"].forEach((id) => { $(id).disabled = autoSplitSource; });
    $("revConfWrap").classList.toggle("hidden", isPreExtract || autoSplitSource);
    if (isPrepared) {
      stageAction = "confirm";
      $("btnConfirmRecognize").textContent = "✅ 確認外觀（加入掃描清單）";
      $("prepHint").innerHTML = "<b>② 外觀確認</b>：確認上方「單據類型」正確，並用工具列旋轉／裁切／分割整理影像；完成後按「✅ 確認外觀」加入③掃描清單。";
    } else if (isQueued) {
      stageAction = "recognize";
      $("btnConfirmRecognize").textContent = "🔍 辨識此張";
      $("prepHint").innerHTML = "<b>③ 掃描清單</b>：外觀已確認。按「🔍 辨識此張」提取內容欄位，或到清單頂端「⚡ 一鍵提取全部」批次處理。";
    }
    if (sourcePage && isPreExtract) {
      $("prepHint").innerHTML = autoSplitSource
        ? "<b>來源頁</b>：此頁已完成自動分割或確認為空白，保留原圖供追溯；請開啟拆分出的單據處理。"
        : "<b>來源頁</b>：請用上方「裁切為新憑證」建立單據；來源頁保留供追溯，不會確認或送入辨識。";
    }
    $("revCompanySel").value = rec.company || "";
    const typeKey = v.doc_type_key || "other";
    populateTypeSel(typeKey);
    buildActiveFields(typeKey);
    renderFields(v.fields || {});
    const conf = v.confidence || "—";
    $("revConf").textContent = conf;
    $("revConf").className = "tag " + (conf === "high" ? "high" : conf === "low" ? "low" : "warn");
    $("f-note").value = v.note || "";
    $("f-clerk_note").value = rec.clerk_note || "";
    $("f-acc_note").value = rec.accountant_note || "";
    $("f-dev_note").value = rec.dev_note || "";
    $("f-manual_entry").checked = !!rec.manual_entry;
    const hasItems = !!(DOC_TYPES[typeKey] && DOC_TYPES[typeKey].has_items);
    $("itemsSection").classList.toggle("hidden", !hasItems);
    renderItems(v.items || []);
    renderTaxcheck(rec.taxcheck || {});
    renderDatecheck(rec.datecheck);
    renderAmountcheck(rec.amountcheck);
    renderEntries(v.entries || [], v.debit_total, v.credit_total, v.balance_check);
    renderWarnings();
    setupImage(id, rec.has_image, v.boxes || {});
    renderPages(id, rec.attachments || 0);
    $("revSrc").textContent = `${sourcePage ? "來源頁（供追溯）　" : rec.source_parent_id != null ? `拆分自來源 #${rec.source_parent_id}　` : ""}來源：${rec.source_file || "—"}　OCR：${rec.ocr_provider || "—"}　結構化：${rec.structure_model || "—"}`;
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

  function renderEntries(entries, dt, ct, bal) {
    const body = $("entriesBody");
    body.innerHTML = entries.length ? entries.map((e) => `
      <tr><td>${esc(e.description)}</td>
        <td>${esc(e.debit_account)} ${esc(e.debit_account_name)}</td>
        <td>${esc(e.credit_account)} ${esc(e.credit_account_name)}</td>
        <td class="num">${nf(e.amount)}</td></tr>`).join("")
      : `<tr><td colspan="4" class="hint">尚無分錄，可按「自動分類科目」產生。</td></tr>`;
    const info = $("balanceInfo");
    if (entries.length) {
      info.innerHTML = `借方合計 ${nf(dt)}　貸方合計 ${nf(ct)}　` +
        (bal ? '<span class="ok">借貸平衡 ✓</span>' : '<span class="bad">未平衡，請人工核對</span>');
    } else info.textContent = "";
  }

  function setStatus(status) {
    const el = $("revStatus");
    el.className = "tag " + (STATUS_CLASS[status] || "");
    el.textContent = STATUS_LABEL[status] || status;
  }

  // 「暫存／下一份」只屬於內容辨識完成、尚待審核的憑證；不能讓使用者把已確認或
  // 已核定的資料誤送回待審。此函式也供審核動作成功後即時隱藏按鈕使用。
  function setPendingActionVisibility(status) {
    const isPending = status === "pending";
    $("btnSavePending").classList.toggle("hidden", !isPending);
    $("btnReturnImport").classList.toggle("hidden", !isPending);
  }

  // ── 動態欄位（依單據類型）+ 掃描影像彩色框對照 ──
  const PALETTE = ["#0969da", "#1a7f37", "#8250df", "#bc4c00", "#cf222e", "#d29922",
    "#1b7c83", "#bf3989", "#6639ba", "#0550ae", "#116329", "#953800"];
  let activeFields = [];

  function populateTypeSel(typeKey) {
    const el = $("revTypeSel");
    el.innerHTML = Object.entries(DOC_TYPES)
      .map(([k, t]) => `<option value="${k}">${esc(t.label)}</option>`).join("");
    el.value = typeKey;
  }

  // 外觀確認 / 重新辨識：以「目前選定的類型」在準備好的影像上做內容辨識
  async function runRecognize() {
    if (!currentId) return;
    // 重新辨識會以模型輸出覆寫 current_json。只在使用者明確勾了「人工修正」時才問——
    // 用「值有沒有變動」當條件會天天跳提醒（存過一次就不同了），反而被無視。
    if ($("f-manual_entry").checked) {
      if (!confirm("這張已勾選「人工修正」。\n重新辨識會用模型的結果覆蓋目前所有欄位值，你的修正將遺失。\n\n確定要重新辨識嗎？")) return;
    }
    const tk = $("revTypeSel").value;
    await fetch(`/api/vouchers/${currentId}/set-type`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk }) });
    await saveNotes();   // 三個備註欄都是辨識提示，先落地再辨識（含記帳士／會計師備註）
    const d = await withInferProgress("內容辨識中…", () =>
      fetch(`/api/vouchers/${currentId}/recognize`, { method: "POST", headers: json() }).then((r) => r.json()));
    if (!d.ok) { alert(errText(d, "辨識失敗")); return; }
    toast("辨識完成"); openReview(currentId); loadList();
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
      return `<label><span class="fld-badge" style="background:${f.color}" data-key="${f.key}" title="點我在影像上定位">${f.n}</span>${esc(f.label)}<input id="${f.el}" type="${t}" value="${esc(v == null ? "" : v)}"><span class="chk" id="chk-${f.key}"></span></label>`;
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
      if (cropping) return;
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
    const spec = DOC_TYPES[$("revTypeSel").value || currentVoucher.doc_type_key] || DOC_TYPES.other;
    return (spec && spec.item_fields && spec.item_fields.length) ? spec.item_fields : [
      {key:"description",label:"品名",kind:"text"}, {key:"quantity",label:"數量",kind:"number"},
      {key:"unit_price",label:"單價",kind:"number"}, {key:"amount",label:"合計",kind:"number"}];
  }
  function renderItems(items) {
    const body = $("itemsBody");
    const header = $("itemsSection").querySelector("thead tr");
    if (header) header.innerHTML = itemSpecs().map((f) => `<th>${esc(f.label)}</th>`).join("") + "<th></th>";
    body.innerHTML = (items.length ? items : [{}]).map(itemRow).join("");
    bindItemRows();
  }
  function itemRow(it) {
    return `<tr data-original="${esc(JSON.stringify(it))}">${itemSpecs().map((f) => {
      let value = it[f.key], type = f.kind === "number" ? "number" : "text";
      if (type === "number" && value != null && value !== "") {
        const number = Number(String(value).replace(/(?:NT)?\$|[,\s，、＄元]/gi, ""));
        if (Number.isFinite(number)) value = number; else type = "text";
      }
      return `<td><input data-item-key="${esc(f.key)}" data-kind="${f.kind}" type="${type}" value="${esc(value ?? "")}"></td>`;
    }).join("")}<td><button class="btn btn-sm del">✕</button></td></tr>`;
  }
  function bindItemRows() {
    $("itemsBody").querySelectorAll(".del").forEach((b) =>
      b.addEventListener("click", (e) => { e.target.closest("tr").remove(); }));
  }
  $("addItem").addEventListener("click", () => {
    $("itemsBody").insertAdjacentHTML("beforeend", itemRow({})); bindItemRows();
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
      return item;
    }).filter((it) => Object.values(it).some((value) => value != null && value !== ""));
    // The server owns common-field mapping; retain every unrendered field/attribute.
    return {...currentVoucher, fields, items, note: $("f-note").value};
  }

  function renderWarnings() {
    let box = $("voucherWarnings");
    if (!box) {
      box = document.createElement("div"); box.id = "voucherWarnings"; box.className = "hint warn";
      $("balanceInfo").insertAdjacentElement("afterend", box);
    }
    const meta = currentVoucher.classification_meta || {};
    const lines = [...(currentVoucher.validation_warnings || []), ...(meta.warnings || []), ...(meta.assumptions || [])];
    const origin = currentVoucher.origin || {}, quality = currentVoucher.quality;
    if (origin.page_no != null || origin.region) {
      lines.unshift(`來源定位：第 ${origin.page_no ?? "—"} 頁${origin.region ? `，區域 ${JSON.stringify(origin.region)}` : ""}${origin.rotation != null ? `，旋轉 ${origin.rotation}°` : ""}`);
    }
    if (quality) {
      const state = {usable:"可用",needs_review:"待覆核",insufficient:"資訊不足"}[quality.state] || quality.state || "未判定";
      const kind = {supporting:"佐證文件",document:"單據",unknown:"未判定",blank:"空白頁"}[quality.content_kind] || quality.content_kind || "未判定";
      lines.unshift(`文件品質：${state} · ${kind}`);
      for (const issue of quality.issues || []) {
        lines.push(`品質${issue.severity ? `（${issue.severity}）` : "提示"}：${issue.message || issue.code || "需檢查"}${issue.path ? ` [${issue.path}]` : ""}`);
      }
    }
    if (meta.status === "candidate") lines.unshift("候選分錄：請核對證據與假設後再審核。");
    if (meta.original_checks && meta.original_checks.single_sided) lines.unshift("保留原始單面傳票；借貸平衡與原印合計分開檢查。");
    if (meta.status === "skipped" && meta.reason === "supporting") lines.unshift("佐證附頁不單獨產生分錄。");
    box.textContent = Array.from(new Set(lines)).join("；");
    box.classList.toggle("hidden", !lines.length);
  }

  $("btnClassify").addEventListener("click", async () => {
    // 先存目前編輯，再產生分錄
    try {
      await persistCurrentEdits();
      const r = await withInferProgress("科目分類中…", () =>
        fetch(`/api/vouchers/${currentId}/classify`, { method: "POST" }).then((x) => x.json()));
      if (!r.ok) { alert(errText(r, "分類失敗")); return; }
      currentVoucher.entries = r.entries;
      currentVoucher.debit_total = r.debit_total; currentVoucher.credit_total = r.credit_total;
      currentVoucher.balance_check = r.balance_check;
      currentVoucher.classification_meta = r.classification_meta || {};
      currentVoucher.validation_warnings = r.validation_warnings || [];
      currentVoucher.payment_state = r.payment_state || {};
      renderWarnings();
      renderEntries(r.entries, r.debit_total, r.credit_total, r.balance_check);
      toast("已產生會計分錄");
    } catch (err) {
      alert(`分類失敗：${err.message}`);
    }
  });

  // 三個備註欄不在 collectForm()（那是憑證內容），需另存；它們是下次辨識的人工修正提示
  function collectNotes() {
    const n = { clerk_note: $("f-clerk_note").value, accountant_note: $("f-acc_note").value,
                manual_entry: $("f-manual_entry").checked };
    if (!$("devNoteWrap").classList.contains("hidden")) n.dev_note = $("f-dev_note").value;
    return n;
  }
  async function saveNotes() {
    return fetch(`/api/vouchers/${currentId}/notes`, { method: "POST", headers: json(), body: JSON.stringify(collectNotes()) });
  }

  async function responseError(resp, fallback) {
    const data = await resp.json().catch(() => ({}));
    return errText(data, `${fallback}（HTTP ${resp.status}）`);
  }

  // 暫存與一般儲存共用：欄位、明細、分錄、人工修正標記與備註必須全數寫入後
  // 才視為成功，避免頁面切換後把失敗誤認成已保存。
  async function persistCurrentEdits() {
    if (!currentId) throw new Error("尚未選取憑證");
    const voucherResp = await fetch(`/api/vouchers/${currentId}`, {
      method: "PUT", headers: json(), body: JSON.stringify(collectForm()),
    });
    if (!voucherResp.ok) throw new Error(await responseError(voucherResp, "儲存修正失敗"));
    const record = await voucherResp.json();
    currentVoucher = record.current || currentVoucher;
    renderWarnings();

    const notesResp = await saveNotes();
    if (!notesResp.ok) throw new Error(await responseError(notesResp, "儲存備註失敗"));
  }

  $("btnSave").addEventListener("click", async () => {
    try {
      await persistCurrentEdits();
      toast("已儲存修正與備註"); loadList();
    } catch (err) {
      alert(`儲存失敗：${err.message}`);
    }
  });

  async function saveToPending(returnToImport = false) {
    if (!currentId) return;
    const savedId = currentId;
    try {
      await persistCurrentEdits();
      if (returnToImport) {
        clearReview();
        fileInput.value = "";
        $("dirInput").value = "";
        setStage("import");
        toast("已暫存至④待審，可開始辨識下一份");
        return;
      }
      setStage("pending");
      await openReview(savedId);
      toast("已暫存至④待審");
    } catch (err) {
      alert(`暫存失敗：${err.message}`);
    }
  }

  $("btnSavePending").addEventListener("click", () => saveToPending());
  $("btnReturnImport").addEventListener("click", () => saveToPending(true));
  $("btnClerk").addEventListener("click", () => act("clerk-review", $("f-clerk_note").value, "記帳士已確認"));
  $("btnApprove").addEventListener("click", () => act("approve", $("f-acc_note").value, "會計師已核定"));
  $("btnReject").addEventListener("click", () => act("reject", $("f-acc_note").value, "已退回"));
  $("btnResubmit").addEventListener("click", () => act("resubmit", $("f-clerk_note").value, "已重新送審（回到④待審）"));

  async function act(path, note, msg) {
    // 先存目前編輯，再轉狀態
    await fetch(`/api/vouchers/${currentId}`, { method: "PUT", headers: json(), body: JSON.stringify(collectForm()) });
    const resp = await fetch(`/api/vouchers/${currentId}/${path}`, { method: "POST", headers: json(), body: JSON.stringify({ note }) });
    // 原本不檢查回應：端點不存在（404/405，多半是伺服器沒重啟）或權限不足時，
    // 照樣跳「已完成」的 toast，按鈕看起來毫無反應也查不出原因。
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      alert(resp.status === 405 || resp.status === 404
        ? `此功能的後端端點不存在（HTTP ${resp.status}），請重新啟動伺服器後再試。`
        : errText(d, `操作失敗（HTTP ${resp.status}）`));
      return;
    }
    const r = await resp.json();
    if (r.status) {
      setStatus(r.status);
      setPendingActionVisibility(r.status);
    }
    toast(msg); loadList();
  }

  const json = () => ({ "Content-Type": "application/json" });
  function toast(msg) { progressText.textContent = msg; progress.classList.remove("hidden"); barFill.style.width = "100%"; setTimeout(hideProgress, 1200); }

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
    await fetch(`/api/vouchers/${currentId}/rotate`, { method: "POST", headers: json(), body: JSON.stringify({ direction: dir }) });
    const img = $("revImage");
    img.onload = () => { resetZoom(); drawBoxes({}); };   // 旋轉後清框（不再對齊）
    img.src = `/api/vouchers/${currentId}/image?t=${Date.now()}`;
    if (currentVoucher) currentVoucher.boxes = {};
    toast("已旋轉，按「🔄 重新辨識」更新欄位與框");
  }
  $("btnRotL").addEventListener("click", () => rotateImage("ccw"));
  $("btnRotR").addEventListener("click", () => rotateImage("cw"));

  function reloadImage() {
    const img = $("revImage");
    img.onload = () => { resetZoom(); drawBoxes({}); };
    img.src = `/api/vouchers/${currentId}/image?t=${Date.now()}`;
    if (currentVoucher) currentVoucher.boxes = {};
  }

  // 微調角度旋轉（去歪斜）
  $("btnFineRot").addEventListener("click", async () => {
    if (!currentId) return;
    const angle = Number($("fineAngle").value) || 0;
    if (!angle) return;
    await fetch(`/api/vouchers/${currentId}/rotate-fine`, { method: "POST", headers: json(), body: JSON.stringify({ angle }) });
    reloadImage(); toast(`已微調 ${angle}°，按「🔄 重新辨識」更新`);
  });

  // ── 裁切 / 分割模式 ──
  let cropStart = null;
  let splitMode = false;
  function setCropMode(on) {
    cropping = on;
    $("btnCrop").classList.toggle("hidden", on);
    $("btnSplit").classList.toggle("hidden", on);
    $("btnCropOk").classList.toggle("hidden", !on);
    $("btnCropCancel").classList.toggle("hidden", !on);
    const inner = document.querySelector(".rev-image-inner");
    if (inner) inner.classList.toggle("in-crop", on);   // 十字游標（含影像區）
    if (on) resetZoom();
    if (!on) { $("cropSel").classList.add("hidden"); cropStart = null; splitMode = false; }
  }
  $("btnCrop").addEventListener("click", () => { splitMode = false; setCropMode(true); });
  $("btnSplit").addEventListener("click", () => { splitMode = true; setCropMode(true); toast("框選一張憑證，按「確認」建立為新紀錄（該區會從原圖塗白）"); });
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
    if (splitMode) {
      setCropMode(false);
      const srcId = currentId;
      const d = await withInferProgress("分割中…（判類型／自動轉正）", () =>
        fetch(`/api/vouchers/${srcId}/split`, { method: "POST", headers: json(), body: JSON.stringify({ box }) }).then((r) => r.json()));
      if (!d.ok) { alert(errText(d, "分割失敗")); return; }
      toast("已建立新憑證（原圖該區已塗白）"); loadList();
      if (d.source_blank) {
        alert(`原憑證 #${srcId} 的內容已全部分割出去，剩下空白頁，請在②外觀確認中略過或合併掉。`);
      } else if (d.source_extracted) {
        alert(`原憑證 #${srcId} 的影像已變動（該區塗白），先前辨識的欄位對應的是舊影像，請對它按「🔄 重新辨識」。`);
      }
      if (d.id) openReview(d.id);
    } else {
      if (!confirm("「裁切」會取代目前憑證的原圖且無法復原。\n若是要把一頁多張拆開，請改用「✂➕ 裁切為新憑證」。\n\n確定要裁切目前這張嗎？")) return;
      const r = await fetch(`/api/vouchers/${currentId}/crop`, { method: "POST", headers: json(), body: JSON.stringify({ box }) });
      setCropMode(false);
      if (!r.ok) { alert("裁切失敗"); return; }
      reloadImage(); toast("已裁切，按「🔄 重新辨識」更新");
    }
  });
  function clearReview() {
    $("revForm").classList.add("hidden"); $("revEmpty").classList.remove("hidden"); currentId = null;
  }
  // 外觀確認 → 加入掃描清單（prepared → queued）
  async function confirmAppearance() {
    if (!currentId) return;
    const tk = $("revTypeSel").value;
    const d = await (await fetch(`/api/vouchers/${currentId}/confirm`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk }) })).json();
    if (!d.ok) { alert(errText(d, "確認失敗")); return; }
    toast("已加入掃描清單"); clearReview(); loadList();
  }

  $("btnReRecognize").addEventListener("click", runRecognize);
  $("btnConfirmRecognize").addEventListener("click", () => {
    if (stageAction === "confirm") confirmAppearance(); else runRecognize();
  });
  $("revCompanySel").addEventListener("change", async () => {
    if (!currentId) return;
    const company = $("revCompanySel").value;
    const r = await fetch(`/api/vouchers/${currentId}/company`, { method: "POST", headers: json(), body: JSON.stringify({ company }) });
    if (!r.ok) { alert(`歸屬公司儲存失敗（HTTP ${r.status}）`); return; }
    toast(company ? `歸屬公司已設為 ${company}` : "已清除歸屬公司");
    loadList();
  });
  $("revTypeSel").addEventListener("change", async () => {
    if (!currentId) return;
    const tk = $("revTypeSel").value;
    await fetch(`/api/vouchers/${currentId}/set-type`, { method: "POST", headers: json(), body: JSON.stringify({ doc_type_key: tk }) });
    buildActiveFields(tk);
    renderFields(currentVoucher.fields || {});
    loadList();
  });

  // ── 開發者備註（管理員，修正回饋）──
  $("btnDevNote").addEventListener("click", async () => {
    const r = await fetch(`/api/vouchers/${currentId}/dev-note`, { method: "POST", headers: json(), body: JSON.stringify({ note: $("f-dev_note").value }) });
    if (!r.ok) { alert("儲存失敗（需管理員）"); return; }
    toast("開發者備註已儲存"); loadList();
  });

  // ── 修正回饋清單 modal（管理員）──
  $("feedbackBtn").addEventListener("click", openFeedback);
  $("closeFeedback").addEventListener("click", () => $("feedbackModal").classList.add("hidden"));
  async function openFeedback() {
    const rows = (await (await fetch("/api/feedback")).json()).items || [];
    $("feedbackEmpty").classList.toggle("hidden", rows.length > 0);
    const noteCell = (r) => [r.dev_note && "🛠" + r.dev_note, r.clerk_note && "記:" + r.clerk_note, r.accountant_note && "會:" + r.accountant_note].filter(Boolean).join("　") || "—";
    $("feedbackBody").innerHTML = rows.map((r) =>
      `<tr data-id="${r.id}"><td>${esc(r.source_file)}</td><td>${esc(r.doc_type)}</td>
        <td><span class="tag ${STATUS_CLASS[r.status] || ""}">${STATUS_LABEL[r.status] || r.status}</span></td>
        <td>${r.changed ? "✓" : ""}</td><td>${esc(noteCell(r))}</td></tr>`).join("");
    $("feedbackBody").querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", () => {
      $("feedbackModal").classList.add("hidden"); openReview(Number(tr.dataset.id));
    }));
    $("feedbackModal").classList.remove("hidden");
  }

  // ── 系統組態 modal（管理員）──
  const CFG_FIELDS = {
    llm: { ollama_base_url: "cfg-ollama_base_url", h200_base_url: "cfg-h200_base_url",
           google_api_key: "cfg-google_api_key",
           openai_api_key: "cfg-openai_api_key", anthropic_api_key: "cfg-anthropic_api_key" },
    scan_source: { type: "cfg-type", host: "cfg-host", path: "cfg-path",
                   username: "cfg-username", password: "cfg-password", note: "cfg-note" },
    cloud_deploy: { provider: "cfg-provider", region: "cfg-region", bucket: "cfg-bucket",
                    access_key: "cfg-access_key", secret_key: "cfg-secret_key",
                    permission: "cfg-permission", note: "cfg-cloud_note" },
  };
  const SECRET_FIELDS = new Set(["google_api_key", "openai_api_key", "anthropic_api_key",
                                 "password", "access_key", "secret_key"]);
  $("sysconfigBtn").addEventListener("click", openSysconfig);
  $("closeSysconfig").addEventListener("click", () => $("sysconfigModal").classList.add("hidden"));
  document.querySelectorAll(".cfg-save").forEach((b) =>
    b.addEventListener("click", () => saveSysconfig(b.dataset.section)));

  async function openSysconfig() {
    const d = await (await fetch("/api/sysconfig")).json();
    const cfg = d.config || {}, sset = d.secrets_set || {};
    for (const sec in CFG_FIELDS) {
      for (const key in CFG_FIELDS[sec]) {
        const el = $(CFG_FIELDS[sec][key]); if (!el) continue;
        if (SECRET_FIELDS.has(key)) {
          el.value = "";
          el.placeholder = (sset[sec] && sset[sec][key]) ? "已設定（留空不變更）" : "未設定";
        } else {
          el.value = (cfg[sec] && cfg[sec][key]) || "";
        }
      }
    }
    $("sysconfigModal").classList.remove("hidden");
  }

  async function saveSysconfig(section) {
    const values = {};
    for (const key in CFG_FIELDS[section]) {
      const el = $(CFG_FIELDS[section][key]); if (!el) continue;
      if (SECRET_FIELDS.has(key) && el.value === "") continue;  // 留空不變更
      values[key] = el.value;
    }
    const r = await fetch("/api/sysconfig", { method: "POST", headers: json(), body: JSON.stringify({ section, values }) });
    if (!r.ok) { alert("儲存失敗（需管理員）"); return; }
    toast("組態已儲存");
  }

  // ── 地端備份 modal（管理員）──
  $("backupBtn").addEventListener("click", openBackup);
  $("closeBackup").addEventListener("click", () => $("backupModal").classList.add("hidden"));
  $("backupNow").addEventListener("click", async () => {
    const r = await fetch("/api/backup", { method: "POST" });
    if (!r.ok) { alert("備份失敗（需管理員）"); return; }
    toast("備份完成"); loadBackups();
  });

  async function openBackup() { await loadBackups(); $("backupModal").classList.remove("hidden"); }
  async function loadBackups() {
    const data = await (await fetch("/api/backups")).json();
    const rows = data.backups || [];
    $("backupEmpty").classList.toggle("hidden", rows.length > 0);
    $("backupBody").innerHTML = rows.map((b) =>
      `<tr><td>${esc(b.name)}</td><td>${esc(b.time)}</td><td>${(b.size / 1024).toFixed(1)} KB</td>
       <td><a href="/api/backup/download/${encodeURIComponent(b.name)}">下載</a></td></tr>`).join("");
  }

  // ── 報表 modal ──
  $("reportBtn").addEventListener("click", openReport);
  $("closeReport").addEventListener("click", () => $("reportModal").classList.add("hidden"));
  $("reportCancel").addEventListener("click", () => $("reportModal").classList.add("hidden"));
  $("reportGen").addEventListener("click", () => {
    const t = $("reportTemplate").value, p = $("reportPeriod").value, c = $("reportCompany").value.trim();
    if (!p) { alert("請選擇期別"); return; }
    const url = `/api/report?template=${encodeURIComponent(t)}&period=${encodeURIComponent(p)}` + (c ? `&company=${encodeURIComponent(c)}` : "");
    window.open(url, "_blank");
  });

  async function openReport() {
    const [tpls, periods] = await Promise.all([
      (await fetch("/api/report/templates")).json(),
      (await fetch("/api/journal/periods")).json(),
    ]);
    $("reportTemplate").innerHTML = (tpls.templates || []).map((t) => `<option value="${t.id}">${t.name}</option>`).join("");
    const ps = periods.periods || [];
    $("reportPeriod").innerHTML = ps.length ? ps.map((p) => `<option value="${p}">${p}</option>`).join("") : `<option value="">（無資料）</option>`;
    $("reportModal").classList.remove("hidden");
  }

  // ── 科目表 modal ──
  $("accountsBtn").addEventListener("click", openAccounts);
  $("closeAccounts").addEventListener("click", () => $("accountsModal").classList.add("hidden"));
  $("accAdd").addEventListener("click", () => { $("accBody").insertAdjacentHTML("beforeend", accRow({})); bindAccDel(); });
  $("accSave").addEventListener("click", saveAccounts);

  async function openAccounts() {
    const sel = $("accCompany");
    if (!sel.options.length) {
      sel.innerHTML = `<option value="">通用（未指定公司）</option>`
        + COMPANIES.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      sel.addEventListener("change", loadAccountsFor);
    }
    await loadAccountsFor();
    $("accountsModal").classList.remove("hidden");
  }
  async function loadAccountsFor() {
    const company = $("accCompany").value;
    const data = await (await fetch(`/api/accounts?company=${encodeURIComponent(company)}`)).json();
    const own = (data.with_chart || []).includes(company);
    $("accBody").innerHTML = (data.accounts || []).map(accRow).join("");
    bindAccDel();
    // 顯示的是不是這家公司自己的表——沒講清楚會誤以為在改 A 公司卻改到通用表
    $("accCompany").title = company && !own
      ? `${company} 尚未自訂科目表，目前顯示的是通用表；儲存後即成為 ${company} 專屬` : "";
  }
  function accRow(a) {
    return `<tr>
      <td><input class="ac-code" value="${esc(a.code)}"></td>
      <td><input class="ac-name" value="${esc(a.name)}"></td>
      <td><input class="ac-cat" value="${esc(a.category)}"></td>
      <td><button class="btn btn-sm del">✕</button></td></tr>`;
  }
  function bindAccDel() {
    $("accBody").querySelectorAll(".del").forEach((b) =>
      b.addEventListener("click", (e) => e.target.closest("tr").remove()));
  }
  async function saveAccounts() {
    const accounts = Array.from($("accBody").querySelectorAll("tr")).map((tr) => ({
      code: tr.querySelector(".ac-code").value.trim(),
      name: tr.querySelector(".ac-name").value.trim(),
      category: tr.querySelector(".ac-cat").value.trim(),
    })).filter((a) => a.code || a.name);
    const company = $("accCompany").value;
    const r = await fetch("/api/accounts", { method: "POST", headers: json(), body: JSON.stringify({ accounts, company }) });
    if (!r.ok) { alert(`科目表儲存失敗（HTTP ${r.status}，需管理員權限）`); return; }
    $("accountsModal").classList.add("hidden");
    toast(`${company || "通用"}科目表已儲存（${accounts.length} 個科目）`);
  }

  // ── 模型設定 modal ──
  $("settingsBtn").addEventListener("click", openSettings);
  $("closeModal").addEventListener("click", () => modal.classList.add("hidden"));
  $("saveSettings").addEventListener("click", saveSettings);

  async function openSettings() {
    const [models, cfg] = await Promise.all([
      (await fetch("/api/models")).json(), (await fetch("/api/config")).json(),
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
    const label = (m) => m.startsWith("ollama/") ? "地端 " + m.replace("ollama/", "")
      : m.startsWith("paddle/") ? "地端 OCR " + m.replace("paddle/", "") : "雲端 " + m;
    $(id).innerHTML = (options || []).map((m) => `<option value="${m}" ${m === selected ? "selected" : ""}>${label(m)}</option>`).join("");
  }
  async function saveSettings() {
    await fetch("/api/config", { method: "POST", headers: json(), body: JSON.stringify({
      ocr: $("sel-ocr").value, structure: $("sel-structure").value, classify: $("sel-classify").value }) });
    modal.classList.add("hidden");
  }

  // ── 日記帳 ──
  $("journalBtn").addEventListener("click", openJournal);
  $("closeJournal").addEventListener("click", () => $("journalModal").classList.add("hidden"));
  $("journalRefresh").addEventListener("click", loadJournal);
  $("journalPeriod").addEventListener("change", loadJournal);
  let journalData = null;

  async function openJournal() {
    const company = $("journalCompany").value.trim();
    const data = await (await fetch("/api/journal/periods" + (company ? `?company=${encodeURIComponent(company)}` : ""))).json();
    const periods = data.periods || [];
    $("journalPeriod").innerHTML = periods.length
      ? periods.map((p) => `<option value="${p}">${p}</option>`).join("")
      : `<option value="">（無已核定憑證）</option>`;
    $("journalModal").classList.remove("hidden");
    if (periods.length) loadJournal(); else showJournal(null);
  }

  async function loadJournal() {
    const period = $("journalPeriod").value;
    if (!period) { showJournal(null); return; }
    const company = $("journalCompany").value.trim();
    const url = `/api/journal?period=${encodeURIComponent(period)}` + (company ? `&company=${encodeURIComponent(company)}` : "");
    journalData = await (await fetch(url)).json();
    showJournal(journalData);
  }

  function showJournal(d) {
    if (!d || !d.rows || !d.rows.length) {
      $("journalWrap").classList.add("hidden"); $("journalEmpty").classList.remove("hidden");
      $("journalEmpty").textContent = d ? "此期別無已核定憑證分錄。" : "選擇期別以檢視日記帳（僅彙整「會計師已核定」的憑證）。";
      return;
    }
    $("journalEmpty").classList.add("hidden"); $("journalWrap").classList.remove("hidden");
    $("journalBody").innerHTML = d.rows.map((r) => `
      <tr><td>${esc(r.date)}</td><td>${esc(r.voucher_id)}</td><td>${esc(r.summary)}</td>
        <td>${esc(r.debit_account)}</td><td>${esc(r.credit_account)}</td>
        <td class="num">${r.debit_amount == null ? "" : nf(r.debit_amount)}</td>
        <td class="num">${r.credit_amount == null ? "" : nf(r.credit_amount)}</td></tr>`).join("");
    $("journalDebit").textContent = nf(d.debit_total);
    $("journalCredit").textContent = nf(d.credit_total);
    $("journalBalance").innerHTML = `${d.count} 張憑證　` +
      (d.balance_check ? '<span class="ok">借貸平衡 ✓</span>' : '<span class="bad">未平衡，請人工核對</span>');
  }

  // ── 列印 / PDF（瀏覽器列印，可另存 PDF）──
  function printWindow(title, bodyHtml, targetWindow = null) {
    const w = targetWindow || window.open("", "_blank");
    w.document.write(`<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><title>${esc(title)}</title>
      <style>body{font-family:"Microsoft JhengHei",sans-serif;padding:24px;color:#1f2328;}
      h1{font-size:20px;margin-bottom:4px;} .meta{color:#636c76;font-size:12px;margin-bottom:16px;}
      table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px;}
      th,td{border:1px solid #999;padding:5px 8px;text-align:left;} th{background:#eee;}
      .num{text-align:right;} .kv td{border:none;padding:2px 8px;}
      tfoot td{font-weight:700;background:#f3f3f3;} pre.audit{white-space:pre-wrap;overflow-wrap:anywhere;font-size:10.5px;}</style></head><body>${bodyHtml}
      <script>window.onload=function(){window.print();}<\/script></body></html>`);
    w.document.close();
  }

  async function printVoucher() {
    const preview = window.open("", "_blank");
    if (!preview) { alert("請允許開啟列印視窗"); return; }
    const response = await fetch(`/api/vouchers/${currentId}`, {method:"PUT",headers:json(),body:JSON.stringify(collectForm())});
    if (!response.ok) { preview.close(); alert("列印前儲存失敗"); return; }
    const record = await response.json();
    const v = record.current || {}; currentVoucher = v;
    const specs = itemSpecs();
    const items = (v.items || []).map((i) => `<tr>${specs.map((f) => `<td>${f.kind === "number" ? nf(i[f.key]) : esc(i[f.key])}</td>`).join("")}</tr>`).join("");
    const meta = v.classification_meta || {};
    const warnings = [meta.status === "candidate" ? "候選分錄，需人工核對" : "", ...(v.validation_warnings || []), ...(meta.warnings || []), ...(meta.assumptions || [])].filter(Boolean);
    const entries = (currentVoucher.entries || []).map((e) => `<tr><td>${esc(e.description)}</td><td>${esc(e.debit_account)} ${esc(e.debit_account_name)}</td><td>${esc(e.credit_account)} ${esc(e.credit_account_name)}</td><td class="num">${nf(e.amount)}</td><td>${esc(e.amount_source)}</td><td>${esc((e.evidence_paths || []).join("、"))}</td></tr>`).join("");
    const audit = {field_evidence: v.field_evidence || {}, validation_warnings: v.validation_warnings || [],
      classification_meta: meta, payment_state: v.payment_state || {}, items: v.items || [], entries: v.entries || []};
    printWindow(`憑證 ${v.voucher_id}`, `
      <h1>會計憑證審核報告</h1>
      <div class="meta">憑證號 ${esc(v.voucher_id)}　日期 ${esc(v.date)}　類型 ${esc(v.doc_type)}</div>
      <table class="kv"><tr><td>賣方</td><td>${esc(v.seller.name)}（${esc(v.seller.tax_id)}）</td><td>買方</td><td>${esc(v.buyer.name)}（${esc(v.buyer.tax_id)}）</td></tr>
      <tr><td>未稅</td><td>${nf(v.subtotal)}</td><td>稅額</td><td>${nf(v.tax_amount)}</td></tr>
      <tr><td>總計</td><td>${nf(v.total_amount)} ${esc(v.currency)}</td><td>摘要</td><td>${esc(v.note)}</td></tr></table>
      ${items ? `<h3>明細</h3><table><thead><tr>${specs.map((f) => `<th>${esc(f.label)}</th>`).join("")}</tr></thead><tbody>${items}</tbody></table>` : ""}
      ${warnings.length ? `<p>${warnings.map(esc).join("；")}</p>` : ""}
      ${entries ? `<h3>會計分錄</h3><table><thead><tr><th>摘要</th><th>借方科目</th><th>貸方科目</th><th>金額</th><th>主要金額來源</th><th>輔助證據路徑</th></tr></thead><tbody>${entries}</tbody></table>` : ""}
      <h3>來源、驗證與政策紀錄</h3><pre class="audit">${esc(JSON.stringify(audit, null, 2))}</pre>`, preview);
  }
  $("btnPrintVoucher").addEventListener("click", printVoucher);

  $("btnPrintJournal").addEventListener("click", () => {
    if (!journalData || !journalData.rows || !journalData.rows.length) { alert("無資料可列印"); return; }
    const rows = journalData.rows.map((r) => `<tr><td>${esc(r.date)}</td><td>${esc(r.voucher_id)}</td><td>${esc(r.summary)}</td><td>${esc(r.debit_account)}</td><td>${esc(r.credit_account)}</td><td class="num">${r.debit_amount == null ? "" : nf(r.debit_amount)}</td><td class="num">${r.credit_amount == null ? "" : nf(r.credit_amount)}</td><td>${esc(r.amount_source)}</td><td>${esc((r.evidence_paths || []).join("、"))}</td><td>${esc(r.payment_state == null ? "" : JSON.stringify(r.payment_state))}</td></tr>`).join("");
    printWindow(`日記帳 ${journalData.period}`, `
      <h1>單月日記帳　${esc(journalData.period)}</h1>
      <div class="meta">${journalData.company ? "公司 " + esc(journalData.company) + "　" : ""}${journalData.count} 張已核定憑證</div>
      <table><thead><tr><th>日期</th><th>憑證號</th><th>摘要</th><th>借方科目</th><th>貸方科目</th><th>借方金額</th><th>貸方金額</th><th>主要金額來源</th><th>輔助證據路徑</th><th>付款狀態</th></tr></thead>
      <tbody>${rows}</tbody>
      <tfoot><tr><td colspan="5">合計</td><td class="num">${nf(journalData.debit_total)}</td><td class="num">${nf(journalData.credit_total)}</td><td></td></tr></tfoot></table>`);
  });

  // 深連結：#v=<id> 自動開啟該筆審核
  function openFromHash() {
    if (/journal/.test(location.hash)) { openJournal(); return; }
    if (/accounts/.test(location.hash)) { openAccounts(); return; }
    if (/sysconfig/.test(location.hash)) { openSysconfig(); return; }
    const m = location.hash.match(/v=(\d+)/);
    if (m) openReview(Number(m[1]));
  }
  window.addEventListener("hashchange", openFromHash);

  // ── 登入 / 角色 ──
  const ROLE_LABEL = { admin: "管理員", clerk: "記帳士", accountant: "會計師" };
  $("loginBtn").addEventListener("click", doLogin);
  $("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  $("logoutBtn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); location.reload(); });

  async function doLogin() {
    const r = await fetch("/api/login", { method: "POST", headers: json(),
      body: JSON.stringify({ username: $("loginUser").value, password: $("loginPass").value }) });
    if (!r.ok) { $("loginErr").textContent = "帳號或密碼錯誤"; return; }
    currentUser = await r.json();
    enterApp();
  }

  function applyRole() {
    const canApprove = currentUser && (currentUser.role === "accountant" || currentUser.role === "admin");
    const canAdmin = currentUser && currentUser.role === "admin";
    $("btnApprove").classList.toggle("hidden", !canApprove);
    $("btnReject").classList.toggle("hidden", !canApprove);
    $("accountsBtn").classList.toggle("hidden", !canAdmin);
    $("backupBtn").classList.toggle("hidden", !canAdmin);
    $("databaseBtn").classList.toggle("hidden", !canAdmin);
    $("sysconfigBtn").classList.toggle("hidden", !canAdmin);
    $("feedbackBtn").classList.toggle("hidden", !canAdmin);
    $("devNoteWrap").classList.toggle("hidden", !canAdmin);
  }

  async function enterApp() {
    $("loginOverlay").classList.add("hidden");
    $("logoutBtn").classList.remove("hidden");
    const chip = $("userChip");
    chip.textContent = `${currentUser.name}（${ROLE_LABEL[currentUser.role] || currentUser.role}）`;
    chip.classList.remove("hidden");
    applyRole();
    loadCompanies();
    try { DOC_TYPES = (await (await fetch("/api/doc-types")).json()).types || {}; } catch (e) { DOC_TYPES = {}; }
    setStage("import"); refreshStats(); openFromHash();
    refreshEngine(); startEnginePoll();
  }

  async function boot() {
    const r = await fetch("/api/me");
    if (r.ok) { currentUser = await r.json(); enterApp(); }
    else $("loginOverlay").classList.remove("hidden");
  }

  // 載入公司清單到上傳下拉
  let COMPANIES = [];
  async function loadCompanies() {
    const data = await (await fetch("/api/companies")).json();
    COMPANIES = (data.companies || []).map((c) => c.short || c.name);
    const opts = COMPANIES.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    $("uploadCompany").innerHTML = `<option value="">未指定</option>` + opts;
    $("revCompanySel").innerHTML = `<option value="">未指定</option>` + opts;
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
