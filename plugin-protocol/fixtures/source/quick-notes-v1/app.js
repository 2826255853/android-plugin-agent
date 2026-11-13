const PLUGIN_ID = "__PLUGIN_ID__";
const API_VERSION = "1.0.0";
let sequence = 0;
let cursor = "0";
let canLoadMore = false;
const pending = new Map();
const status = document.querySelector("#status");
const noteList = document.querySelector("#notes");
const moreButton = document.querySelector("#load-more");
const bridge = globalThis.PluginHostBridge;

if (!bridge || typeof bridge.postMessage !== "function") {
  status.textContent = "宿主数据接口不可用。";
} else {
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
  status.textContent = "数据默认受限；请在宿主原生界面单独授权读取和写入。";
}

function call(capability, params) {
  if (!bridge || typeof bridge.postMessage !== "function") return Promise.reject(new Error("BRIDGE_UNAVAILABLE"));
  const id = `notes_${++sequence}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("BRIDGE_TIMEOUT"));
    }, 5000);
    pending.set(id, { resolve, reject, timer });
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
  status.textContent = "正在读取笔记…";
  try {
    const response = await call("data.read", { collection: "notes", limit: 100, after: append ? cursor : "0" });
    if (!response.ok) return displayError(response);
    renderRecords(response.result.records, append);
    cursor = response.result.nextCursor ?? cursor;
    canLoadMore = response.result.nextCursor !== null;
    moreButton.hidden = !canLoadMore;
    status.textContent = `已显示 ${noteList.children.length} 条笔记。`;
  } catch (error) { status.textContent = error.message; }
}

document.querySelector("#note-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const title = document.querySelector("#title").value;
  const body = document.querySelector("#body").value;
  if (!title.trim()) { status.textContent = "标题不能为空。"; return; }
  const saveButton = form.querySelector("button");
  saveButton.disabled = true;
  status.textContent = "正在保存…";
  try {
    const response = await call("data.write", {
      collection: "notes",
      record: body.length ? { title, body } : { title },
    });
    if (!response.ok) return displayError(response);
    form.reset();
    await loadNotes(false);
    status.textContent = "笔记已保存。";
  } catch (error) { status.textContent = error.message; }
  finally { saveButton.disabled = false; }
});

moreButton.addEventListener("click", () => { if (canLoadMore) loadNotes(true); });
document.querySelector("#refresh").addEventListener("click", () => loadNotes(false));
if (bridge) loadNotes();
