(() => {
  "use strict";
  const PAGE_SIZE = 50;
  const byId = (id) => document.getElementById(id);
  const state = { page: 1, total: 0, pages: 0, listSeq: 0, detailSeq: 0, current: null, companies: [], types: {}, imagePage: 0 };
  const missing = "—";
  const text = (value) => value == null || value === "" ? missing : String(value);
  const node = (tag, value, className) => {
    const el = document.createElement(tag);
    if (value != null) el.textContent = String(value);
    if (className) el.className = className;
    return el;
  };
  async function readJson(response) {
    let data = {};
    try { data = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      if (response.status === 401) location.href = "/";
      const detail = data.detail;
      const readable = Array.isArray(detail) ? detail.map((part) => {
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return String(part ?? "");
        const where = Array.isArray(part.loc) ? part.loc.filter((x) => x !== "body").join(" → ") : "";
        return [where, part.msg || "請求資料無效"].filter(Boolean).join("：");
      }).filter(Boolean).join("；") : typeof detail === "string" ? detail : detail?.msg || `HTTP ${response.status}`;
      const error = new Error(readable || `HTTP ${response.status}`); error.status = response.status; error.data = data; throw error;
    }
    return data;
  }
  function message(target, value, show = true) {
    const el = byId(target); el.textContent = value || ""; el.classList.toggle("hidden", !show || !value);
  }
  function formatDate(value) {
    if (!value) return missing;
    const s = String(value); return /^\d{4}-\d{2}-\d{2}(?:$|T)/.test(s) ? s.slice(0, 10) : s;
  }
  function formatTime(value) {
    if (!value) return missing;
    const raw = String(value);
    const d = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(raw) ? raw : `${raw}+08:00`);
    return Number.isNaN(d.getTime()) ? raw : d.toLocaleString("zh-TW", { timeZone: "Asia/Taipei", hour12: false });
  }
  function formatAmount(amount, currency) {
    if (amount == null || amount === "") return missing;
    const n = Number(amount);
    const shown = Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(amount);
    return `${shown}${currency ? ` ${currency}` : ""}`;
  }
  function setPageStatus() {
    byId("resultCount").textContent = `${state.total.toLocaleString()} 筆`;
    byId("pageStatus").textContent = state.pages ? `第 ${state.page} / ${state.pages} 頁` : "第 0 / 0 頁";
    byId("prevPage").disabled = state.page <= 1;
    byId("nextPage").disabled = !state.pages || state.page >= state.pages;
  }
  function companyName(record) { return record.company || "未指定公司"; }
  function renderList(items) {
    const body = byId("confirmedRows"); body.replaceChildren();
    for (const record of items || []) {
      const row = node("tr");
      const number = node("button", text(record.voucher_id), "voucher-link"); number.type = "button";
      number.addEventListener("click", () => openDetail(record.id));
      const typeLabel = state.types[record.doc_type_key]?.label || record.doc_type;
      const noCell = node("td"); noCell.append(number, node("div", text(typeLabel), "row-sub"));
      const values = [companyName(record), formatDate(record.date), formatAmount(record.total_amount, record.currency), text(record.confirmed_by_name), formatTime(record.confirmed_at)];
      row.append(noCell, ...values.map((v) => node("td", v)));
      body.append(row);
    }
  }
  function filterParams() {
    const params = new URLSearchParams();
    const company = byId("companyFilter").value, from = byId("dateFrom").value, to = byId("dateTo").value;
    if (company) params.set("company_id", company);
    if (from) params.set("date_from", parseFilterDate(from).iso);
    if (to) params.set("date_to", parseFilterDate(to).iso);
    params.set("page", String(state.page)); params.set("page_size", String(PAGE_SIZE));
    return params;
  }
  function parseFilterDate(value) {
    const match = value.trim().match(/^(\d{4})\s*(?:年|[／/-])\s*(\d{1,2})\s*(?:月|[／/-])\s*(\d{1,2})\s*日?$/);
    if (!match) throw new Error("請以年／月／日輸入有效的單據日期");
    const [, year, month, day] = match.map(Number);
    const parsed = new Date(0);
    parsed.setUTCFullYear(year, month - 1, day);
    if (year < 1 || parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day)
      throw new Error("請輸入有效的單據日期");
    return { iso: `${match[1]}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
      display: `${match[1]}年${month}月${day}日` };
  }
  function normalizeDateInput(id) {
    const input = byId(id), picker = byId(`${id}Picker`);
    try {
      const date = input.value.trim() ? parseFilterDate(input.value) : { iso: "", display: "" };
      input.value = date.display; picker.value = date.iso; input.setCustomValidity("");
      return true;
    } catch (error) { input.setCustomValidity(error.message); return false; }
  }
  function openDatePicker(id) {
    const input = byId(id), picker = byId(`${id}Picker`);
    try { picker.value = input.value.trim() ? parseFilterDate(input.value).iso : ""; }
    catch (_) { picker.value = ""; }
    try { picker.showPicker(); } catch (_) { picker.focus(); picker.click(); }
  }
  async function loadList() {
    const seq = ++state.listSeq;
    message("listMessage", "載入中…", true);
    try {
      const data = await readJson(await fetch(`/api/confirmed-vouchers?${filterParams()}`));
      if (seq !== state.listSeq) return;
      state.total = Number(data.total) || 0; state.pages = Number(data.pages) || 0;
      state.page = Number(data.page) > 0 ? Number(data.page) : state.page;
      renderList(data.items); message("listMessage", data.items?.length ? "" : "沒有符合條件的已確認單據。", !data.items?.length); setPageStatus();
    } catch (error) {
      if (seq !== state.listSeq) return;
      renderList([]); message("listMessage", `載入失敗：${error.message} `, true);
      const retry = node("button", "重試", "btn btn-sm"); retry.type = "button"; retry.addEventListener("click", loadList); byId("listMessage").append(" ", retry);
      state.total = 0; state.pages = 0; setPageStatus();
    }
  }
  function fillCompanies(records) {
    state.companies = records || [];
    const select = byId("companyFilter");
    select.replaceChildren(select.children[0] || node("option", "全部公司"));
    for (const company of state.companies) { const option = node("option", company.name); option.value = String(company.id); select.append(option); }
  }
  function appendKV(parent, entries) {
    const dl = node("div", null, "kv");
    for (const [label, value] of entries) { dl.append(node("span", label, "k"), node("span", text(value))); }
    parent.append(dl);
  }
  function appendTable(parent, headings, rows) {
    const table = node("table", null, "detail-table"); const thead = node("thead"), tr = node("tr");
    headings.forEach((h) => tr.append(node("th", h))); thead.append(tr); table.append(thead);
    const tbody = node("tbody");
    (rows || []).forEach((row) => { const r = node("tr"); headings.forEach((_, i) => r.append(node("td", text(row[i])))); tbody.append(r); });
    table.append(tbody); parent.append(table);
  }
  function fieldRows(current) {
    const type = state.types[current.doc_type_key] || {};
    const fields = current.fields || {};
    const configured = (type.fields || []).filter((f) => f.key in fields).map((f) => [f.label || f.key, fields[f.key]]);
    const known = new Set((type.fields || []).map((field) => field.key));
    for (const [key, value] of Object.entries(fields)) if (!known.has(key)) configured.push([key, value]);
    return configured;
  }
  function currentImageUrl() {
    const c = state.current; return `/api/vouchers/${encodeURIComponent(c.id)}/image?expected_updated_at=${encodeURIComponent(c.updated_at || "")}${state.imagePage ? `&att=${state.imagePage - 1}` : ""}`;
  }
  function showImage(parent) {
    const c = state.current;
    const count = Number(c.attachments) || 0;
    const hasImage = !!c.has_image;
    if (!hasImage && count === 0) { parent.append(node("p", "沒有單據影像。", "hint")); return; }
    if (hasImage || state.imagePage > 0) {
      const img = node("img"); img.alt = state.imagePage ? `附頁 ${state.imagePage}` : "單據影像";
      const error = node("p", "影像載入失敗，請重新載入。", "hint hidden");
      const retry = node("button", "重新載入影像", "btn btn-sm hidden"); retry.type = "button";
      const load = () => { error.classList.toggle("hidden", true); retry.classList.toggle("hidden", true); img.src = currentImageUrl(); };
      img.addEventListener("error", () => { error.classList.toggle("hidden", false); retry.classList.toggle("hidden", false); });
      retry.addEventListener("click", load); load(); parent.append(img, error, retry);
    }
    if (count > 0) {
      const pages = node("div", null, "drawer-pages");
      const firstPage = hasImage ? 0 : 1;
      for (let i = firstPage; i <= count; i++) { const b = node("button", i ? `附頁 ${i}` : "主頁", "btn btn-sm"); b.type = "button"; if (i === state.imagePage) b.setAttribute("aria-current", "page"); b.addEventListener("click", () => { state.imagePage = i; renderDetail(c); }); pages.append(b); }
      parent.append(pages);
    }
  }
  function renderDetail(record) {
    const body = byId("detailBody"); body.replaceChildren();
    const current = record.current || {};
    const imageCard = node("section", null, "card"); imageCard.append(node("h3", "影像")); showImage(imageCard); body.append(imageCard);
    const fields = node("section", null, "card"); fields.append(node("h3", "單據欄位"));
    const typeLabel = state.types[current.doc_type_key]?.label || current.doc_type || record.doc_type;
    appendKV(fields, [["公司", displayCompany(record)], ["單據號碼", current.voucher_id], ["單據種類", typeLabel], ["單據日期", formatDate(current.date)], ["幣別", current.currency], ...fieldRows(current)]); body.append(fields);
    if (current.items?.length) {
      const sec = node("section", null, "card"); sec.append(node("h3", "明細項目")); const type = state.types[current.doc_type_key] || {};
      const fallback = [{ key: "description", label: "品名／摘要" }, { key: "quantity", label: "數量" }, { key: "unit_price", label: "單價" }, { key: "amount", label: "金額" }];
      const columns = uniqueColumns([...(type.item_fields?.length ? type.item_fields : fallback), ...["account_code", "account_name"].filter((key) => current.items.some((item) => item[key] != null)).map((key) => ({ key, label: key === "account_code" ? "科目代號" : "科目名稱" }))]);
      appendTable(sec, columns.map((x) => x.label || x.key), current.items.map((x) => columns.map((f) => x[f.key]))); body.append(sec);
    }
    if (current.entries?.length) {
      const sec = node("section", null, "card"); sec.append(node("h3", "原稿傳票明細"));
      const labels = { description: "摘要", amount: "金額", debit_account: "借方科目代號", debit_account_name: "借方科目名稱", debit_name: "借方科目名稱", credit_account: "貸方科目代號", credit_account_name: "貸方科目名稱", credit_name: "貸方科目名稱", dept: "部門", offset_code: "立沖帳目" };
      const entries = current.entries;
      const configured = state.types[current.doc_type_key]?.entry_fields || [];
      const fallbackKeys = Object.keys(labels).filter((key) => entries.some((entry) => entry[key] != null));
      const cols = uniqueColumns(configured.length ? configured : fallbackKeys.map((key) => ({ key, label: labels[key] || key }))).map((f) => ({ ...f, label: labels[f.key] || f.label || f.key }));
      appendTable(sec, cols.map((x) => x.label || x.key), entries.map((x) => cols.map((f) => x[f.key]))); body.append(sec);
    }
    const totals = node("section", null, "card"); totals.append(node("h3", "摘要與確認資訊"));
    appendKV(totals, [["小計", formatAmount(current.subtotal, current.currency)], ["稅額", formatAmount(current.tax_amount, current.currency)], ["總額", formatAmount(current.total_amount, current.currency)], ["摘要", current.note], ["確認人", record.confirmed_by_name], ["確認時間", formatTime(record.confirmed_at)], ["更新時間", formatTime(record.updated_at)]]); body.append(totals);
    const print = node("button", "列印辨識報告", "btn primary"); print.type = "button";
    const ready = !!record.recognition_report_ready;
    print.disabled = !ready;
    print.title = ready ? "" : "需重新確認後才能列印辨識報告";
    print.addEventListener("click", () => printReport(record)); totals.append(print);
    if (!ready) totals.append(node("p", "辨識報告尚未就緒，需重新確認。", "hint"));
  }
  function uniqueColumns(columns) {
    const seen = new Set(); return columns.filter((column) => column?.key && !seen.has(column.key) && seen.add(column.key));
  }
  function displayCompany(record) { return record.company || state.companies.find((company) => Number(company.id) === Number(record.company_id))?.name || "未指定公司"; }
  async function openDetail(id) {
    const seq = ++state.detailSeq;
    state.imagePage = 0; state.current = null; byId("detailBody").replaceChildren(); message("detailMessage", "載入明細…", true);
    if (!byId("detailDialog").open) byId("detailDialog").showModal();
    try {
      const record = await readJson(await fetch(`/api/vouchers/${encodeURIComponent(id)}`));
      if (seq !== state.detailSeq) return;
      if (record.status !== "confirmed" || record.record_role === "source_page") {
        byId("detailDialog").close(); await loadList(); message("listMessage", "此單據已回到待確認流程或屬來源頁，無法在此查看。", true); return;
      }
      if (!record.has_image && Number(record.attachments) > 0) state.imagePage = 1;
      state.current = record; message("detailMessage", "", false); renderDetail(record);
    } catch (error) {
      if (seq !== state.detailSeq) return;
      if (error.status === 404) { byId("detailDialog").close(); await loadList(); message("listMessage", "單據已不存在或已回待確認，清單已更新。", true); }
      else message("detailMessage", `明細載入失敗：${error.message}`, true);
    }
  }
  async function printReport(record) {
    if (!record.recognition_report_ready) { message("detailMessage", "辨識報告需重新確認後才能列印。", true); return; }
    const popup = window.open("", "_blank");
    if (!popup) { message("detailMessage", "瀏覽器未允許開啟報告視窗。", true); return; }
    const seq = state.detailSeq;
    const isCurrent = () => state.current === record && state.detailSeq === seq && byId("detailDialog").open;
    try {
      const version = encodeURIComponent(record.updated_at || "");
      const response = await fetch(`/api/vouchers/${encodeURIComponent(record.id)}/recognition-report?expected_updated_at=${version}`);
      if (!isCurrent()) { popup.close(); return; }
      if (response.status === 409) { popup.close(); record.recognition_report_ready = false; renderDetail(record); message("detailMessage", "單據版本已變更，請重新確認後再列印。", true); return; }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const reportHtml = await response.text();
      if (!isCurrent() || popup.closed) { popup.close(); return; }
      popup.document.open(); popup.document.write(reportHtml); popup.document.close();
    } catch (error) { popup.close(); if (isCurrent()) message("detailMessage", `報告載入失敗：${error.message}`, true); }
  }
  function showStartError(error) {
    message("listMessage", `頁面載入失敗：${error.message}`, true);
    const retry = node("button", "重試", "btn btn-sm"); retry.type = "button"; retry.addEventListener("click", start); byId("listMessage").append(" ", retry);
  }
  async function start() {
    message("listMessage", "載入中…", true);
    let me;
    try {
      me = await readJson(await fetch("/api/me"));
      byId("userName").textContent = me.name || me.username || "";
    } catch (error) { if (error.status === 401) location.href = "/"; else showStartError(error); return; }
    const get = async (url) => readJson(await fetch(url));
    const [companiesResult, typesResult] = await Promise.allSettled([get("/api/companies"), get("/api/doc-types")]);
    const failures = [companiesResult, typesResult].filter((result) => result.status === "rejected").map((result) => result.reason);
    if (failures.some((error) => error.status === 401)) { location.href = "/"; return; }
    if (companiesResult.status === "fulfilled") fillCompanies(companiesResult.value.companies || []);
    if (typesResult.status === "fulfilled") state.types = typesResult.value.types || {};
    else state.types = {};
    if (companiesResult.status === "rejected") { showStartError(companiesResult.reason); return; }
    await loadList();
    if (typesResult.status === "rejected") showStartError(typesResult.reason);
    const match = location.hash.match(/^#v=(\d+)$/); if (match && Number(match[1]) > 0) openDetail(Number(match[1]));
  }
  ["dateFrom", "dateTo"].forEach((id) => {
    byId(id).addEventListener("input", () => byId(id).setCustomValidity(""));
    byId(id).addEventListener("blur", () => normalizeDateInput(id));
    byId(`${id}PickerButton`).addEventListener("click", () => openDatePicker(id));
    byId(`${id}Picker`).addEventListener("change", () => {
      byId(id).value = byId(`${id}Picker`).value; normalizeDateInput(id);
    });
  });
  byId("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    for (const id of ["dateFrom", "dateTo"]) if (!normalizeDateInput(id)) { byId(id).reportValidity(); return; }
    state.page = 1; loadList();
  });
  byId("clearFilters").addEventListener("click", () => {
    byId("companyFilter").value = "";
    ["dateFrom", "dateTo"].forEach((id) => { byId(id).value = ""; normalizeDateInput(id); });
    state.page = 1; loadList();
  });
  byId("prevPage").addEventListener("click", () => { if (state.page > 1) { state.page--; loadList(); } });
  byId("nextPage").addEventListener("click", () => { if (state.page < state.pages) { state.page++; loadList(); } });
  byId("closeDetail").addEventListener("click", () => byId("detailDialog").close());
  byId("detailDialog").addEventListener("close", () => { state.detailSeq++; state.current = null; });
  window.addEventListener("hashchange", () => { const match = location.hash.match(/^#v=(\d+)$/); if (match) openDetail(Number(match[1])); });
  window.ConfirmedPage = { filterParams, loadList, openDetail, printReport, state };
  start();
})();
