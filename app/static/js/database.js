(() => {
  const $ = (id) => document.getElementById(id);
  let page = 1, pages = 1, query = "", dateFrom = "", dateTo = "", requestId = 0;
  let selectedTables = [];
  let busy = false;
  let detailTrigger = null;
  const hiddenColumns = {
    users: new Set(["password_hash", "salt"]),
    companies: new Set(["id", "account_chart_key"]),
    item_account_mappings: new Set(["id", "created_by", "updated_by", "version"]),
    voucher_records: new Set(["source_file", "attachments", "recognition_history", "company_id", "confirmed_by", "manual_entry"]),
  };
  const statusLabels = Object.fromEntries([
    ["prepared", "待外觀確認"], ["queued", "待提取"], ["pending", "待確認"],
    ["confirmed", "已確認"], ["running", "執行中"], ["succeeded", "成功"],
    ["failed", "失敗"], ["timeout", "逾時"], ["cancelled", "已取消"],
    ["cancelling", "取消中"], ["interrupted", "已中斷"],
    ["terminated", "已終止"], ["completed", "已完成"],
  ]);

  function selectedTable() {
    return selectedTables.find((table) => table.database === $("databaseSelect").value
      && table.name === $("tableSelect").value);
  }

  function resetFilters() {
    query = ""; page = pages = 1;
    dateFrom = dateTo = "";
    $("searchInput").value = "";
    $("dateFrom").value = "";
    $("dateTo").value = "";
    $("dateFromPicker").value = "";
    $("dateToPicker").value = "";
    $("statusSelect").value = "";
    $("companySelect").value = "";
  }

  function parseDate(value) {
    const match = value.trim().match(/^(\d{4})\s*(?:年|[/-])\s*(\d{1,2})\s*(?:月|[/-])\s*(\d{1,2})\s*(?:日)?$/);
    if (!match) return null;
    const [, year, month, day] = match.map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1
        || parsed.getUTCDate() !== day) return null;
    return { iso: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
      display: `${year}年${month}月${day}日` };
  }

  function openDatePicker(fieldId) {
    const input = $(fieldId);
    const picker = $(`${fieldId}Picker`);
    const date = input.value ? parseDate(input.value) : null;
    picker.value = date?.iso || "";
    try {
      picker.showPicker();
    } catch {
      picker.focus();
      picker.click();
    }
  }

  function syncDatePicker(fieldId) {
    const picker = $(`${fieldId}Picker`);
    if (!picker.value) return;
    const date = parseDate(picker.value);
    if (date) $(fieldId).value = date.display;
  }

  function populateTables(preferred) {
    const database = $("databaseSelect").value;
    const available = selectedTables.filter((table) => table.database === database);
    $("tableSelect").replaceChildren();
    available.forEach((table) => {
      const option = document.createElement("option");
      option.value = table.name;
      option.textContent = `${table.label}（${table.count} 筆）`;
      option.title = table.name;
      $("tableSelect").append(option);
    });
    const defaultName = database === "app" ? "voucher_records" : "results";
    const chosen = available.find((table) => table.name === preferred)
      || available.find((table) => table.name === defaultName) || available[0];
    if (chosen) $("tableSelect").value = chosen.name;
    updateFilterControls();
  }

  function updateFilterControls() {
    const table = selectedTable();
    $("tableDescription").textContent = table?.description || "";
    const hasDate = Boolean(table?.date_label);
    $("dateFromWrap").classList.toggle("hidden", !hasDate);
    $("dateToWrap").classList.toggle("hidden", !hasDate);
    $("dateFromLabel").textContent = `${table?.date_label || "日期"}：起日`;
    $("dateToLabel").textContent = `${table?.date_label || "日期"}：迄日`;
    const statuses = table?.statuses || [];
    $("statusWrap").classList.toggle("hidden", !statuses.length);
    $("statusSelect").replaceChildren();
    const all = document.createElement("option");
    all.value = ""; all.textContent = "全部狀態";
    $("statusSelect").append(all);
    statuses.forEach(({value, label}) => {
      const option = document.createElement("option");
      option.value = value; option.textContent = label;
      $("statusSelect").append(option);
    });
    const companies = table?.companies;
    $("companyWrap").classList.toggle("hidden", !companies);
    const previousCompany = $("companySelect").value;
    $("companySelect").replaceChildren();
    const allCompanies = document.createElement("option");
    allCompanies.value = ""; allCompanies.textContent = "全部公司";
    $("companySelect").append(allCompanies);
    (companies || []).forEach((company) => {
      const option = document.createElement("option");
      option.value = company; option.textContent = company;
      $("companySelect").append(option);
    });
    if (companies?.includes(previousCompany)) $("companySelect").value = previousCompany;
  }

  function displayValue(column, value, table) {
    if (value === null) return "NULL";
    if (column.name === "status") {
      return (table?.statuses || []).find((item) => item.value === value)?.label || statusLabels[value] || String(value);
    }
    if (column.utc) {
      const timestamp = new Date(value);
      if (!Number.isNaN(timestamp.getTime())) {
        return new Intl.DateTimeFormat("zh-TW", { timeZone: "Asia/Taipei", hourCycle: "h23",
          year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
          minute: "2-digit", second: "2-digit" }).format(timestamp);
      }
    }
    if (typeof value === "string" && /^(datetime|timestamp)\b/i.test(column.type || "")) {
      return value.replace(/^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\.\d+(?=$|[zZ]$|[+-]\d{2}:?\d{2}$)/, "$1");
    }
    if ((column.name === "warmup" || column.name === "cancel_requested" || column.name === "is_admin")
      && (value === 0 || value === 1)) return value ? "是" : "否";
    return String(value);
  }

  function formatDetail(value) {
    const text = value === null ? "NULL" : String(value);
    try {
      return { text: JSON.stringify(JSON.parse(text), null, 2), label: "JSON" };
    } catch { return { text, label: "欄位內容" }; }
  }

  function isVoucherDetail(column, table = selectedTable()) {
    return table?.database === "app" && table.name === "voucher_records"
      && ["ai_json", "current_json"].includes(column.name);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function detailText(value) {
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    return typeof value === "string" && value.trim() ? value : "";
  }

  function detailFields(rows) {
    const list = document.createElement("dl");
    list.className = "database-voucher-fields";
    rows.forEach(([label, value]) => {
      const text = detailText(value);
      if (!text) return;
      const field = document.createElement("div");
      const name = document.createElement("dt");
      const content = document.createElement("dd");
      name.textContent = label;
      content.textContent = text;
      field.append(name, content);
      list.append(field);
    });
    return list;
  }

  function appendDetailSection(container, title, lists, numbered = false) {
    const populated = lists.filter((list) => list.childElementCount);
    if (!populated.length) return;
    const section = document.createElement("section");
    section.className = "database-voucher-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    section.append(heading);
    if (numbered) {
      const list = document.createElement("ol");
      list.className = "database-voucher-items";
      populated.forEach((fields) => {
        const item = document.createElement("li");
        item.append(fields);
        list.append(item);
      });
      section.append(list);
    } else { section.append(...populated); }
    container.append(section);
  }

  function entryDetailFields(entry) {
    const rows = [["摘要", entry.description]];
    let hasSide = false;
    for (const [side, label] of [["debit", "借方"], ["credit", "貸方"]]) {
      const account = [entry[`${side}_account`], entry[`${side}_account_name`]]
        .map(detailText).filter(Boolean).join(" ");
      const directed = entry.source_direction === side;
      const amount = Object.hasOwn(entry, side) ? entry[side]
        : account || directed ? entry.amount : undefined;
      if (account || directed || detailText(amount)) {
        hasSide = true;
        const sourceAccount = directed ? [entry.account_code, entry.account_name]
          .map(detailText).filter(Boolean).join(" ") : "";
        rows.push([`${label}科目`, account || sourceAccount], [`${label}金額`, amount]);
      }
    }
    if (!hasSide) {
      rows.push(["科目", [entry.account_code || entry.raw_account_code,
        entry.account_name || entry.raw_account_name].map(detailText).filter(Boolean).join(" ")],
      ["金額", entry.amount]);
    }
    return detailFields(rows);
  }

  function renderVoucherDetail(value, container) {
    if (value == null || (typeof value === "string" && !value.trim())) {
      container.textContent = "尚無憑證內容";
      return;
    }
    let voucher;
    try { voucher = JSON.parse(value); }
    catch {
      container.textContent = "憑證內容格式有誤，無法顯示";
      return;
    }
    if (voucher === null) { container.textContent = "尚無憑證內容"; return; }
    if (!isObject(voucher)) {
      container.textContent = "憑證內容格式有誤，無法顯示";
      return;
    }
    const fields = isObject(voucher.fields) ? voucher.fields : {};
    // An explicitly cleared common field must not fall back to an older value.
    const field = (key) => Object.hasOwn(voucher, key) ? voucher[key] : fields[key];
    const party = (side, key) => Object.hasOwn(voucher, side)
      ? (isObject(voucher[side]) ? voucher[side][key] : undefined) : fields[`${side}_${key}`];
    const basic = detailFields([
      ["單據類型", field("doc_type")], ["憑證號碼", field("voucher_id")], ["日期", field("date")],
      ["買方名稱", party("buyer", "name")], ["買方統編", party("buyer", "tax_id")],
      ["賣方名稱", party("seller", "name")], ["賣方統編", party("seller", "tax_id")],
    ]);
    const journal = voucher.doc_type_key === "journal_voucher";
    const rows = journal
      ? [["借方合計", field("debit_total")], ["貸方合計", field("credit_total")]]
      : [["未稅小計", field("subtotal")], ["稅額", field("tax_amount")], ["總金額", field("total_amount")]];
    const details = journal
      ? (Array.isArray(voucher.entries) ? voucher.entries : [])
        .filter((entry) => isObject(entry) && entry.entry_origin !== "generated").map(entryDetailFields)
      : (Array.isArray(voucher.items) ? voucher.items : []).filter(isObject).map((item) => detailFields([
        ["品名／摘要", item.description], ["數量", item.quantity],
        ["單價", item.unit_price], ["金額", item.amount],
      ]));
    const note = field("note");
    const currency = detailText(field("currency"));
    const hasContent = basic.childElementCount || rows.some(([, amount]) => detailText(amount))
      || detailText(note) || details.some((list) => list.childElementCount);
    if (currency && (hasContent || currency !== "TWD")) rows.push(["幣別", currency]);
    rows.push(["備註", note]);
    appendDetailSection(container, "基本資料", [basic]);
    appendDetailSection(container, "金額與備註", [detailFields(rows)]);
    appendDetailSection(container, journal ? "原稿傳票明細" : "品項明細", details, true);
    if (!container.childElementCount) container.textContent = "尚無憑證內容";
  }

  function isDetailValue(value) {
    if (value === null) return false;
    const text = String(value);
    return text.length > 100 || /^[\[{]/.test(text);
  }

  function closeDetail({ restoreFocus = true } = {}) {
    $("detailDrawer").classList.remove("open");
    $("detailDrawer").setAttribute("aria-hidden", "true");
    $("detailBackdrop").classList.add("hidden");
    $("detailBackdrop").setAttribute("aria-hidden", "true");
    document.body.classList.remove("database-drawer-open");
    if (restoreFocus && detailTrigger) detailTrigger.focus();
    detailTrigger = null;
  }

  function openDetail(column, value, row, trigger) {
    detailTrigger = trigger;
    $("detailTitle").textContent = column.label;
    const container = $("detailContent");
    container.replaceChildren();
    if (isVoucherDetail(column)) {
      $("detailMeta").textContent = `憑證重點${row.id != null ? ` · 紀錄 ${row.id}` : ""}`;
      renderVoucherDetail(value, container);
    } else {
      const detail = formatDetail(value);
      $("detailMeta").textContent = `${column.name} · ${detail.label}${row.id != null ? ` · 紀錄 ${row.id}` : ""}`;
      const text = document.createElement("pre");
      text.className = "database-detail-raw";
      text.textContent = detail.text;
      container.append(text);
    }
    container.scrollTop = 0;
    $("detailBackdrop").classList.remove("hidden");
    $("detailBackdrop").setAttribute("aria-hidden", "false");
    $("detailDrawer").classList.add("open");
    $("detailDrawer").setAttribute("aria-hidden", "false");
    document.body.classList.add("database-drawer-open");
    $("closeDetail").focus();
  }

  async function api(url) {
    const response = await fetch(url, { cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      requestId++;
      $("dataHead").replaceChildren();
      $("dataBody").replaceChildren();
      $("databasePanel").classList.add("hidden");
      $("accessMessage").classList.remove("hidden");
      $("accessMessage").textContent = response.status === 401
        ? "尚未登入或登入已失效，請返回憑證系統登入管理員帳號。" : "此頁僅供管理員使用。";
      throw new Error("需管理員登入");
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body.detail;
      throw new Error(typeof detail === "string" ? detail
        : response.status === 422 ? "日期格式或篩選條件無效。" : "無法讀取資料，請稍後重新整理。");
    }
    return response.json();
  }

  function setBusy(value) {
    busy = value;
    $("databasePanel").setAttribute("aria-busy", String(value));
    $("searchForm").querySelectorAll("button, input, select").forEach((el) => { el.disabled = value; });
    $("previousPage").disabled = value || page <= 1;
    $("nextPage").disabled = value || page >= pages;
  }

  function render(data) {
    closeDetail({ restoreFocus: false });
    $("dataHead").replaceChildren();
    $("dataBody").replaceChildren();
    const table = selectedTable();
    const hidden = table?.database === "app" ? hiddenColumns[table.name] : undefined;
    const columns = data.columns.filter((column) => !hidden?.has(column.name));
    const header = document.createElement("tr");
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = column.label || column.name;
      th.title = column.name;
      const type = document.createElement("small");
      type.textContent = column.masked ? "已遮蔽" : column.utc ? "台灣時間" : column.type;
      th.append(type); header.append(th);
    });
    $("dataHead").append(header);
    data.rows.forEach((row) => {
      const tr = document.createElement("tr");
      columns.forEach((column) => {
        const td = document.createElement("td");
        const value = row[column.name];
        const text = displayValue(column, value, table);
        if (value === null) td.classList.add("database-null");
        if (isVoucherDetail(column, table) || isDetailValue(value)) {
          td.classList.add("database-detail-cell");
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn btn-sm database-detail-button";
          button.textContent = "查看內容";
          button.setAttribute("aria-label", `查看 ${column.label || column.name} 內容`);
          button.addEventListener("click", () => openDetail(column, value, row, button));
          td.append(button);
        } else { td.textContent = text; if (text !== String(value)) td.title = String(value); }
        tr.append(td);
      });
      $("dataBody").append(tr);
    });
    page = data.page; pages = data.pages;
    $("pageInfo").textContent = `第 ${page} / ${pages} 頁`;
    const start = data.total ? (page - 1) * data.page_size + 1 : 0;
    $("dataMessage").textContent = data.total
      ? `共 ${data.total} 筆，顯示第 ${start}–${start + data.rows.length - 1} 筆。憑證重點與詳細內容可在右側面板查看。`
      : (query || dateFrom || dateTo || $("statusSelect").value || $("companySelect").value)
        ? "沒有符合篩選條件的資料。" : "此資料表目前沒有資料。";
    $("tableWrap").scrollTo(0, 0);
  }

  async function loadRows() {
    const selected = selectedTable();
    if (!selected) return;
    const id = ++requestId;
    setBusy(true);
    $("dataMessage").classList.remove("database-error");
    $("dataMessage").textContent = "載入中…";
    $("dataHead").replaceChildren(); $("dataBody").replaceChildren();
    $("pageInfo").textContent = "";
    try {
      const params = new URLSearchParams({ database: selected.database, table: selected.name,
        q: query, page: String(page), page_size: $("pageSize").value });
      if (selected.date_label && dateFrom) params.set("date_from", dateFrom);
      if (selected.date_label && dateTo) params.set("date_to", dateTo);
      if (selected.statuses?.length && $("statusSelect").value) params.set("status", $("statusSelect").value);
      if (selected.companies && $("companySelect").value) params.set("company", $("companySelect").value);
      const data = await api(`/api/database/rows?${params}`);
      if (id === requestId) render(data);
    } catch (error) {
      if (id === requestId) {
        page = pages = 1;
        $("dataMessage").textContent = error.message;
        $("dataMessage").classList.add("database-error");
      }
    } finally { if (id === requestId) setBusy(false); }
  }

  async function refreshTables() {
    const previous = selectedTable();
    setBusy(true);
    try {
      const data = await api("/api/database/tables");
      selectedTables = data.tables.filter((table) =>
        !(table.database === "app" && table.name === "mapping_history"));
      const databases = [...new Map(selectedTables.map((table) =>
        [table.database, table.database_label])).entries()];
      $("databaseSelect").replaceChildren();
      databases.forEach(([id, label]) => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = label;
        $("databaseSelect").append(option);
      });
      $("databaseSelect").value = databases.some(([id]) => id === previous?.database)
        ? previous.database : databases.some(([id]) => id === "app") ? "app" : databases[0]?.[0] || "";
      populateTables(previous?.database === $("databaseSelect").value ? previous?.name : undefined);
      $("accessMessage").classList.add("hidden");
      $("databasePanel").classList.remove("hidden");
      if (selectedTables.length) await loadRows();
      else {
        $("dataHead").replaceChildren(); $("dataBody").replaceChildren();
        page = pages = 1; $("pageInfo").textContent = "";
        $("dataMessage").textContent = "目前沒有可檢視的資料表。";
      }
    } catch (error) {
      $("dataHead").replaceChildren(); $("dataBody").replaceChildren();
      $("dataMessage").textContent = error.message;
      $("accessMessage").classList.remove("hidden");
      if (!$("databasePanel").classList.contains("hidden")) $("accessMessage").textContent = error.message;
      else if ($("accessMessage").textContent.includes("正在確認")) $("accessMessage").textContent = error.message;
    } finally { setBusy(false); }
  }

  $("searchForm").addEventListener("submit", (event) => {
    event.preventDefault(); if (busy) return;
    const from = $("dateFrom").value ? parseDate($("dateFrom").value) : { iso: "", display: "" };
    const to = $("dateTo").value ? parseDate($("dateTo").value) : { iso: "", display: "" };
    if (!from || !to) {
      $("dataMessage").textContent = "請輸入正確日期，例如：2026年10月28日";
      $("dataMessage").classList.add("database-error");
      return;
    }
    if (from.iso && to.iso && from.iso > to.iso) {
      $("dataMessage").textContent = "起日不能晚於迄日";
      $("dataMessage").classList.add("database-error");
      return;
    }
    dateFrom = from.iso; dateTo = to.iso;
    $("dateFrom").value = from.display;
    $("dateTo").value = to.display;
    query = $("searchInput").value.trim(); page = 1; loadRows();
  });
  $("databaseSelect").addEventListener("change", () => {
    populateTables(); resetFilters(); loadRows();
  });
  $("tableSelect").addEventListener("change", () => {
    updateFilterControls(); resetFilters(); loadRows();
  });
  $("pageSize").addEventListener("change", () => { page = 1; loadRows(); });
  $("clearSearch").addEventListener("click", () => { resetFilters(); loadRows(); });
  ["dateFrom", "dateTo"].forEach((fieldId) => {
    $(`${fieldId}PickerButton`).addEventListener("click", () => openDatePicker(fieldId));
    $(fieldId).addEventListener("click", () => openDatePicker(fieldId));
    $(`${fieldId}Picker`).addEventListener("change", () => syncDatePicker(fieldId));
  });
  $("previousPage").addEventListener("click", () => { if (!busy && page > 1) { page--; loadRows(); } });
  $("nextPage").addEventListener("click", () => { if (!busy && page < pages) { page++; loadRows(); } });
  $("refresh").addEventListener("click", refreshTables);
  $("closeDetail").addEventListener("click", () => closeDetail());
  $("detailBackdrop").addEventListener("click", () => closeDetail());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("detailDrawer").classList.contains("open")) closeDetail();
  });
  refreshTables();
})();
