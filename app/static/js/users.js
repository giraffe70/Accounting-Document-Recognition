(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let currentUser = null;
  let users = [];
  let busy = false;
  let editingUser = null;

  function closeEdit() {
    editingUser = null;
    $("editForm").reset();
    $("editPanel").classList.add("hidden");
    message("editMessage", "");
  }

  function message(id, value, error = false) {
    $(id).textContent = value;
    $(id).classList.toggle("users-error", error);
  }

  function deny(status) {
    currentUser = null;
    users = [];
    $("usersBody").replaceChildren();
    $("userForm").reset();
    closeEdit();
    $("usersPanel").classList.add("hidden");
    message("formMessage", "");
    message("listMessage", "");
    message("accessMessage", status === 401
      ? "尚未登入或登入已失效，請返回主頁重新登入。" : "此頁僅供管理員使用。", true);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...options });
    if (response.status === 401 || response.status === 403) {
      deny(response.status);
      throw new Error("管理員登入已失效或權限不足");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "操作失敗，請稍後重試。");
    return data;
  }

  function renderUsers() {
    const body = $("usersBody");
    body.replaceChildren();
    const adminCount = users.filter((user) => user.is_active && user.is_admin === true).length;
    for (const user of users) {
      const row = document.createElement("tr");
      const date = new Date(user.created_at);
      const cells = [user.username + (user.is_admin ? "（系統管理員）" : ""), user.name,
        user.is_active ? "啟用" : "已停用",
        Number.isNaN(date.getTime()) ? user.created_at : date.toLocaleString("zh-TW")];
      for (const value of cells) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      }
      const actions = document.createElement("td");
      const group = document.createElement("div");
      group.className = "users-action-group";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn";
      edit.textContent = "編輯";
      edit.disabled = busy;
      edit.addEventListener("click", () => beginEdit(user));
      group.appendChild(edit);
      if (user.is_active) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn users-disable-button";
        button.textContent = "刪除（停用）";
        const isSelf = user.id === currentUser.id;
        const isLastAdmin = user.is_admin === true && adminCount <= 1;
        button.disabled = busy || isSelf || isLastAdmin;
        button.title = isSelf ? "不能停用目前登入的管理員" : isLastAdmin ? "不能停用最後一位管理員" : "停用此帳號並保留歷史紀錄";
        button.addEventListener("click", () => disableUser(user));
        group.appendChild(button);
      } else {
        const inactive = document.createElement("span");
        inactive.textContent = "已停用";
        group.appendChild(inactive);
      }
      actions.appendChild(group);
      row.appendChild(actions);
      body.appendChild(row);
    }
  }

  function setBusy(value) {
    busy = value;
    for (const field of $("userForm").elements) field.disabled = busy;
    for (const field of $("editForm").elements) field.disabled = busy;
    $("refreshUsers").disabled = busy;
    if (currentUser) renderUsers();
  }

  async function loadUsers() {
    users = await api("/api/users");
    renderUsers();
    message("listMessage", `共 ${users.length} 個帳號（包含已停用帳號）`);
  }

  async function refreshUsers() {
    if (busy || !currentUser) return;
    setBusy(true);
    try { await loadUsers(); }
    catch (error) { if (currentUser) message("listMessage", error.message, true); }
    finally { setBusy(false); }
  }

  async function createUser(event) {
    event.preventDefault();
    if (busy || !currentUser) return;
    if ($("password").value !== $("confirmPassword").value) {
      message("formMessage", "兩次輸入的密碼不一致。", true);
      return;
    }
    const payload = { username: $("username").value, name: $("displayName").value,
      password: $("password").value };
    setBusy(true);
    message("formMessage", "正在新增帳號…");
    try {
      const user = await api("/api/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      $("userForm").reset();
      message("formMessage", `已新增帳號 ${user.username}。`);
      try { await loadUsers(); }
      catch (error) { if (currentUser) message("listMessage", `帳號已新增，列表更新失敗：${error.message}`, true); }
    } catch (error) {
      if (currentUser) message("formMessage", error.message, true);
    } finally { setBusy(false); }
  }

  function beginEdit(user) {
    if (busy || !currentUser) return;
    editingUser = user;
    $("editForm").reset();
    $("editUsername").value = user.username;
    $("editDisplayName").value = user.name;
    message("editMessage", "");
    $("editPanel").classList.remove("hidden");
    $("editPanel").scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    $("editUsername").focus?.();
  }

  async function saveEdit(event) {
    event.preventDefault();
    if (busy || !currentUser || !editingUser) return;
    const password = $("editPassword").value;
    const confirmation = $("editConfirmPassword").value;
    if (password !== confirmation) {
      message("editMessage", "兩次輸入的新密碼不一致。", true);
      return;
    }
    if (password && (password.length < 8 || password.length > 128)) {
      message("editMessage", "新密碼須為 8 至 128 個字元。", true);
      return;
    }
    const userId = editingUser.id;
    const isSelf = userId === currentUser.id;
    const payload = { username: $("editUsername").value, name: $("editDisplayName").value };
    if (password) payload.password = password;
    setBusy(true);
    message("editMessage", "正在儲存修改…");
    try {
      const updated = await api(`/api/users/${userId}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      if (isSelf && password) {
        deny(401);
        message("accessMessage", "密碼已變更，請返回主頁以新密碼重新登入。", true);
        return;
      }
      if (isSelf) currentUser = { ...currentUser, username: updated.username, name: updated.name };
      users = users.map((item) => item.id === updated.id ? updated : item);
      closeEdit();
      renderUsers();
      message("listMessage", `已更新帳號 ${updated.username}。${password ? "該帳號的既有登入已失效。" : ""}`);
    } catch (error) {
      if (currentUser) message("editMessage", error.message, true);
    } finally { setBusy(false); }
  }

  async function disableUser(user) {
    if (busy || !currentUser) return;
    if (!window.confirm(`確定刪除（停用）帳號「${user.username}」（${user.name}）？\n該帳號將無法登入，既有登入將失效，歷史紀錄會保留。`)) return;
    setBusy(true);
    try {
      const updated = await api(`/api/users/${user.id}`, { method: "DELETE" });
      users = users.map((item) => item.id === updated.id ? updated : item);
      renderUsers();
      message("listMessage", `已停用帳號 ${updated.username}，歷史紀錄已保留。`);
    } catch (error) {
      if (currentUser) message("listMessage", error.message, true);
    } finally { setBusy(false); }
  }

  async function init() {
    setBusy(true);
    try {
      currentUser = await api("/api/me");
      if (currentUser.is_admin !== true) { deny(403); return; }
      await loadUsers();
      message("accessMessage", "");
      $("usersPanel").classList.remove("hidden");
    } catch (error) {
      if (currentUser) message("accessMessage", `無法載入帳號：${error.message}`, true);
      else if (!$("accessMessage").classList.contains("users-error")) message("accessMessage", "無法確認登入狀態，請重新載入頁面。", true);
    } finally { setBusy(false); }
  }

  $("userForm").addEventListener("submit", createUser);
  $("editForm").addEventListener("submit", saveEdit);
  $("editCancel").addEventListener("click", closeEdit);
  $("refreshUsers").addEventListener("click", refreshUsers);
  init();
})();
