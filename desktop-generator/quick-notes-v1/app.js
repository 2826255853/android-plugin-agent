"use strict";
const PLUGIN_ID = "__PLUGIN_ID__";
const API_VERSION = "1.0.0";
const bridge = globalThis.PluginHostBridge;
const pending = new Map();
const status = document.querySelector("#status");
const noteList = document.querySelector("#notes");
const moreButton = document.querySelector("#load-more");
let sequence = 0;
let cursor = "0";
let loading = false;
let saving = false;
let hasMore = false;

function showEditor(editing) {
  document.querySelector("#list-page").hidden = editing;
  document.querySelector("#editor-page").hidden = !editing;
  if (editing) document.querySelector("#title").focus();
}

if (bridge && typeof bridge.postMessage === "function") {
  bridge.onmessage = event => {
    let response;
    try { response = JSON.parse(event.data); } catch { return; }
    if (!response || typeof response.id !== "string") return;
    const request = pending.get(response.id);
    if (!request) return;
    clearTimeout(request.timer);
    pending.delete(response.id);
    request.resolve(response);
  };
  status.textContent = "请在宿主原生界面分别授权读取和写入。";
} else {
  status.textContent = "宿主数据接口不可用；浏览器模拟将在预览阶段提供。";
}

function call(capability, params) {
  if (!bridge || typeof bridge.postMessage !== "function") return Promise.reject(new Error("BRIDGE_UNAVAILABLE"));
  if (pending.size >= 4) return Promise.reject(new Error("BRIDGE_BUSY"));
  const id = `notes_${++sequence}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("BRIDGE_TIMEOUT"));
    }, 5000);
    pending.set(id, { resolve, timer });
    try {
      bridge.postMessage(JSON.stringify({ id, apiVersion: API_VERSION, pluginId: PLUGIN_ID, capability, params }));
    } catch {
      clearTimeout(timer);
      pending.delete(id);
      reject(new Error("BRIDGE_UNAVAILABLE"));
    }
  });
}

function displayError(response) {
  status.textContent = response?.error?.code ? `请求被拒绝：${response.error.code}` : "请求失败。";
}

function renderRecords(records, append) {
  if (!append) noteList.replaceChildren();
  for (const item of records) {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = item.data.title;
    li.append(title);
    if (typeof item.data.body === "string") {
      const body = document.createElement("p");
      body.textContent = item.data.body;
      li.append(body);
    }
    noteList.append(li);
  }
}

async function loadNotes(append = false) {
  if (loading) return false;
  loading = true;
  moreButton.disabled = true;
  document.querySelector("#refresh").disabled = true;
  status.textContent = "正在读取笔记…";
  try {
    const response = await call("data.read", { collection: "notes", limit: 100, after: append ? cursor : "0" });
    if (!response.ok) {
      if (response.error?.code === "PERMISSION_DENIED" || response.error?.code === "CAPABILITY_NOT_DECLARED") {
        noteList.replaceChildren();
        cursor = "0";
        hasMore = false;
        moreButton.hidden = true;
      }
      displayError(response);
      return false;
    }
    if (!Array.isArray(response.result?.records)) throw new Error("INVALID_RESPONSE");
    renderRecords(response.result.records, append);
    cursor = response.result.nextCursor ?? "0";
    hasMore = typeof response.result.nextCursor === "string";
    moreButton.hidden = !hasMore;
    status.textContent = `已显示 ${noteList.children.length} 条笔记。`;
    return true;
  } catch (error) { status.textContent = error.message; return false; }
  finally {
    loading = false;
    moreButton.disabled = false;
    document.querySelector("#refresh").disabled = false;
  }
}

document.querySelector("#new-note").addEventListener("click", () => showEditor(true));
document.querySelector("#cancel-edit").addEventListener("click", () => { if (!saving) showEditor(false); });
document.querySelector("#note-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (saving || loading) return;
  const form = event.currentTarget;
  const title = document.querySelector("#title").value;
  const body = document.querySelector("#body").value;
  if (!title.trim()) { status.textContent = "标题不能为空。"; return; }
  saving = true;
  const saveButton = form.querySelector("button[type=submit]");
  saveButton.disabled = true;
  status.textContent = "正在保存…";
  try {
    const response = await call("data.write", { collection: "notes", record: body.length ? { title, body } : { title } });
    if (!response.ok) { displayError(response); return; }
    form.reset();
    showEditor(false);
    if (await loadNotes(false)) status.textContent = "笔记已保存。";
    else status.textContent = `笔记已保存，列表读取失败：${status.textContent}`;
  } catch (error) { status.textContent = error.message; }
  finally { saving = false; saveButton.disabled = false; }
});
moreButton.addEventListener("click", () => { if (hasMore) loadNotes(true); });
document.querySelector("#refresh").addEventListener("click", () => loadNotes(false));
if (bridge && typeof bridge.postMessage === "function") loadNotes();
