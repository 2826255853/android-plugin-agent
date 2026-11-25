"use strict";
const PLUGIN_ID = "__PLUGIN_ID__";
const bridge = globalThis.PluginHostBridge;
const pending = new Map();
const status = document.querySelector("#status");
const contactList = document.querySelector("#contacts");
let sequence = 0;
let busy = false;

function showEditor(editing) {
  document.querySelector("#list-page").hidden = editing;
  document.querySelector("#editor-page").hidden = !editing;
  if (editing) document.querySelector("#name").focus();
}

function call(capability, params) {
  if (!bridge || typeof bridge.postMessage !== "function") return Promise.reject(new Error("BRIDGE_UNAVAILABLE"));
  if (pending.size >= 4) return Promise.reject(new Error("BRIDGE_BUSY"));
  const id = `contacts_${++sequence}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("BRIDGE_TIMEOUT"));
    }, 5000);
    pending.set(id, { resolve, timer });
    try {
      bridge.postMessage(JSON.stringify({ id, apiVersion: "1.0.0", pluginId: PLUGIN_ID, capability, params }));
    } catch {
      clearTimeout(timer);
      pending.delete(id);
      reject(new Error("BRIDGE_UNAVAILABLE"));
    }
  });
}

if (bridge && typeof bridge.postMessage === "function") {
  bridge.onmessage = event => {
    let response;
    try { response = JSON.parse(event.data); } catch { return; }
    const request = pending.get(response?.id);
    if (!request) return;
    clearTimeout(request.timer);
    pending.delete(response.id);
    request.resolve(response);
  };
} else {
  status.textContent = "宿主数据接口不可用。";
}

function showError(response) {
  status.textContent = response?.error?.code ? `请求被拒绝：${response.error.code}` : "请求失败。";
}

async function loadContacts() {
  if (busy) return;
  busy = true;
  document.querySelector("#refresh").disabled = true;
  status.textContent = "正在读取联系人…";
  try {
    const response = await call("data.read", { collection: "contacts", limit: 100, after: "0" });
    if (!response.ok) {
      if (["PERMISSION_DENIED", "CAPABILITY_NOT_DECLARED"].includes(response.error?.code)) contactList.replaceChildren();
      showError(response);
      return;
    }
    contactList.replaceChildren();
    for (const item of response.result.records) {
      const row = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = item.data.name;
      row.append(name);
      for (const field of ["phone", "email"]) {
        if (typeof item.data[field] === "string") {
          const detail = document.createElement("p");
          detail.textContent = item.data[field];
          row.append(detail);
        }
      }
      contactList.append(row);
    }
    status.textContent = `已显示 ${contactList.children.length} 位联系人。`;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    busy = false;
    document.querySelector("#refresh").disabled = false;
  }
}

document.querySelector("#new-contact").addEventListener("click", () => showEditor(true));
document.querySelector("#cancel-edit").addEventListener("click", () => { if (!busy) showEditor(false); });
document.querySelector("#refresh").addEventListener("click", loadContacts);
document.querySelector("#contact-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  const form = event.currentTarget;
  const name = document.querySelector("#name").value;
  if (!name.trim()) { status.textContent = "姓名不能为空。"; return; }
  const record = { name };
  for (const field of ["phone", "email"]) {
    const value = document.querySelector(`#${field}`).value;
    if (value) record[field] = value;
  }
  busy = true;
  const saveButton = form.querySelector("button[type=submit]");
  saveButton.disabled = true;
  status.textContent = "正在保存…";
  try {
    const response = await call("data.write", { collection: "contacts", record });
    if (!response.ok) { showError(response); return; }
    form.reset();
    showEditor(false);
    busy = false;
    await loadContacts();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    busy = false;
    saveButton.disabled = false;
  }
});

if (bridge && typeof bridge.postMessage === "function") loadContacts();
