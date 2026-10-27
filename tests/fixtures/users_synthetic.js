const fs = require("fs");
const vm = require("vm");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const calls = [];
const confirms = [];
const innerHTMLWrites = [];
const elements = new Map();

class SyntheticElement {
  constructor(tagName = "div", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    this.title = "";
    this.type = "";
    this.name = "";
    this._textContent = "";
    this.className = "";
    this.classList = {
      values: new Set(),
      add: (name) => this.classList.values.add(name),
      remove: (name) => this.classList.values.delete(name),
      contains: (name) => this.classList.values.has(name),
      toggle: (name, force) => {
        const shouldAdd = force === undefined ? !this.classList.values.has(name) : !!force;
        if (shouldAdd) this.classList.values.add(name);
        else this.classList.values.delete(name);
        return shouldAdd;
      },
    };
  }
  get textContent() { return this._textContent; }
  set textContent(value) {
    this._textContent = value == null ? "" : String(value);
    this.children = [];
  }
  get innerHTML() { return ""; }
  set innerHTML(value) { innerHTMLWrites.push({ id: this.id, value: String(value) }); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; this._textContent = ""; }
  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }
  async dispatch(type, event = {}) {
    const pending = (this.listeners[type] || []).map((listener) => listener({
      preventDefault() { event.defaultPrevented = true; }, ...event,
    }));
    await Promise.all(pending);
  }
  reset() {
    for (const field of this.elements) {
      field.value = field.id === "role" ? "clerk" : "";
      field.disabled = false;
    }
  }
}

function element(id) {
  if (!elements.has(id)) {
    const tag = id === "userForm" || id === "editForm" ? "form" : id === "usersBody" ? "tbody" : "div";
    const node = new SyntheticElement(tag, id);
    if (id === "usersPanel" || id === "editPanel" || id === "customRoleWrap") node.classList.add("hidden");
    elements.set(id, node);
  }
  return elements.get(id);
}

const formFields = ["username", "displayName", "role", "customRole", "password", "confirmPassword", "createUserBtn"]
  .map((id) => element(id));
element("userForm").elements = formFields;
element("editForm").elements = ["editUsername", "editDisplayName", "editPassword", "editConfirmPassword", "editSave", "editCancel"].map(element);
element("role").value = "clerk";

function response(status, data) {
  return { status, ok: status >= 200 && status < 300, json: async () => data };
}

const queues = { list: [...(input.scenario.listResponses || [])] };
async function fetchMock(url, options = {}) {
  const method = options.method || "GET";
  const call = { url, method, body: options.body ? JSON.parse(options.body) : null };
  calls.push(call);
  const scenario = input.scenario;
  if (url === "/api/me") return response(scenario.meStatus || 200, scenario.me || { id: 1, name: "管理員", role: "admin" });
  if (url === "/api/users" && method === "GET") {
    return queues.list.length ? response(queues.list[0].status, queues.list.shift().data) : response(200, scenario.users || []);
  }
  if (url === "/api/users" && method === "POST") {
    const result = scenario.postResponse || { status: 201, data: { id: 9, username: call.body.username, name: call.body.name, role: call.body.role, is_active: true } };
    return response(result.status, result.data);
  }
  if (url.startsWith("/api/users/") && method === "DELETE") {
    const result = scenario.deleteResponse || { status: 200, data: { id: Number(url.split("/").pop()), username: "clerk", name: "記帳士", role: "clerk", is_active: false, created_at: "2026-01-01" } };
    return response(result.status, result.data);
  }
  if (url.startsWith("/api/users/") && method === "PATCH") {
    const result = scenario.patchResponse || { status: 200, data: {
      id: Number(url.split("/").pop()), username: call.body.username, name: call.body.name,
      role: scenario.editRole || "clerk", is_active: true, created_at: "2026-01-01" } };
    return response(result.status, result.data);
  }
  throw new Error(`unexpected request ${method} ${url}`);
}

const document = {
  getElementById: element,
  createElement(tag) { return new SyntheticElement(tag); },
};
const window = { confirm(message) { confirms.push(message); return !!input.scenario.confirm; } };
vm.runInNewContext(input.source, { document, window, fetch: fetchMock, console, Date, Number, JSON, Error, String });

const tick = () => new Promise((resolve) => setImmediate(resolve));
async function settle() { for (let i = 0; i < 8; i += 1) await tick(); }

(async () => {
  await settle();
  const scenario = input.scenario;
  if (scenario.action === "submit") {
    element("username").value = scenario.form?.username || "newuser";
    element("displayName").value = scenario.form?.name || "新帳號";
    element("role").value = scenario.form?.role || "clerk";
    await element("role").dispatch("change");
    element("customRole").value = scenario.form?.customRole || "";
    element("password").value = scenario.form?.password || "password-123";
    element("confirmPassword").value = scenario.form?.confirmPassword || scenario.form?.password || "password-123";
    await element("userForm").dispatch("submit");
    await settle();
  } else if (scenario.action === "delete") {
    const row = element("usersBody").children[scenario.rowIndex || 0];
    const actionCell = row?.children[row.children.length - 1];
    const button = actionCell?.children[0]?.children.find((child) => child.textContent === "刪除（停用）");
    if (button && !button.disabled) await button.dispatch("click");
    await settle();
  } else if (scenario.action === "edit" || scenario.action === "cancelEdit") {
    const row = element("usersBody").children[scenario.rowIndex || 0];
    const actionCell = row?.children[row.children.length - 1];
    const button = actionCell?.children[0]?.children.find((child) => child.textContent === "編輯");
    if (button && !button.disabled) await button.dispatch("click");
    if (scenario.action === "cancelEdit") {
      element("editPassword").value = "sensitive-123";
      await element("editCancel").dispatch("click");
    } else {
      if (scenario.editForm?.username !== undefined) element("editUsername").value = scenario.editForm.username;
      if (scenario.editForm?.name !== undefined) element("editDisplayName").value = scenario.editForm.name;
      element("editPassword").value = scenario.editForm?.password || "";
      element("editConfirmPassword").value = scenario.editForm?.confirmPassword ?? scenario.editForm?.password ?? "";
      await element("editForm").dispatch("submit");
    }
    await settle();
  } else if (scenario.action === "roleChange") {
    element("role").value = "other";
    await element("role").dispatch("change");
    element("customRole").value = "自訂角色";
    if (scenario.switchBack) {
      element("role").value = "clerk";
      await element("role").dispatch("change");
    }
  } else if (scenario.action === "refresh") {
    await element("refreshUsers").dispatch("click");
    await settle();
  }

  function serialize(node) {
    return { tagName: node.tagName, id: node.id, textContent: node.textContent, disabled: node.disabled,
      title: node.title, className: node.className, hidden: node.classList.contains("hidden"),
      children: node.children.map(serialize) };
  }
  const out = {
    calls, confirms, innerHTMLWrites,
    usersPanelHidden: element("usersPanel").classList.contains("hidden"),
    editPanelHidden: element("editPanel").classList.contains("hidden"),
    customRoleHidden: element("customRoleWrap").classList.contains("hidden"),
    customRoleRequired: !!element("customRole").required,
    customRoleDisabled: element("customRole").disabled,
    customRoleValue: element("customRole").value,
    usersRows: element("usersBody").children.map(serialize),
    messages: Object.fromEntries(["accessMessage", "formMessage", "listMessage", "editMessage"].map((id) => [id, element(id).textContent])),
    messageErrors: Object.fromEntries(["accessMessage", "formMessage", "listMessage", "editMessage"].map((id) => [id, element(id).classList.contains("users-error")])),
    form: Object.fromEntries(["username", "displayName", "role", "password", "confirmPassword"].map((id) => [id, element(id).value])),
    editForm: Object.fromEntries(["editUsername", "editDisplayName", "editPassword", "editConfirmPassword"].map((id) => [id, element(id).value])),
  };
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
