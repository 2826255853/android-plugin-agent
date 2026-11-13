// Android emulator acceptance for the installed-plugin data API; no third-party packages required.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';

const adbPath = process.env.ADB ?? join(process.env.LOCALAPPDATA ?? '', 'Android', 'Sdk', 'platform-tools', 'adb.exe');
const serial = process.env.ANDROID_SERIAL ?? 'emulator-5554';
const appId = 'com.example.pluginhost';
const id = `org.example.stage14.run${randomBytes(4).toString('hex')}`;
const secondId = `org.example.stage14.copy${randomBytes(4).toString('hex')}`;
const dir = await mkdtemp(join(tmpdir(), 'stage14-acceptance-'));
const zipName = `${id.split('.').at(-1)}.zip`;
const secondZipName = `${secondId.split('.').at(-1)}.zip`;
const updateZipName = 'quick-notes-update.zip';
const downgradeZipName = 'quick-notes-downgrade.zip';
const incompatibleZipName = 'quick-notes-incompatible.zip';
const adb = (...args) => execFileSync(adbPath, ['-s', serial, ...args], { encoding: 'utf8', windowsHide: true });
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, label, timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < end) {
    try { const value = await fn(); if (value) return value; } catch (error) { lastError = error; }
    await pause(250);
  }
  throw new Error(`Timed out waiting for ${label}`, { cause: lastError });
}

function decodeXml(value) {
  return value.replaceAll('&quot;', '"').replaceAll('&apos;', "'").replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>').replaceAll('&amp;', '&');
}
function uiNodes() {
  const result = spawnSync(adbPath, ['-s', serial, 'shell', 'uiautomator', 'dump', '/sdcard/stage14-ui.xml'], {
    encoding: 'utf8', windowsHide: true,
  });
  if (result.status !== 0 && result.status !== 137) throw new Error(result.stderr || result.stdout);
  const xml = adb('shell', 'cat', '/sdcard/stage14-ui.xml');
  const nodes = [];
  for (const match of xml.matchAll(/<node\b([^>]*)>/g)) {
    const attrs = match[1];
    const text = attrs.match(/\btext="([^"]*)"/)?.[1] ?? '';
    const bounds = attrs.match(/\bbounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/);
    if (!bounds) continue;
    nodes.push({
      text: decodeXml(text),
      className: attrs.match(/\bclass="([^"]*)"/)?.[1] ?? '',
      checkable: attrs.match(/\bcheckable="([^"]*)"/)?.[1] === 'true',
      checked: attrs.match(/\bchecked="([^"]*)"/)?.[1] === 'true',
      x: Math.floor((Number(bounds[1]) + Number(bounds[3])) / 2),
      y: Math.floor((Number(bounds[2]) + Number(bounds[4])) / 2),
      top: Number(bounds[2]),
      bottom: Number(bounds[4]),
      left: Number(bounds[1]),
      right: Number(bounds[3]),
    });
  }
  return nodes;
}
async function tapText(text, label = text) {
  const node = await until(() => uiNodes().find(item => item.text === text), label);
  adb('shell', 'input', 'tap', String(node.x), String(node.y));
  await pause(500);
}
async function ensurePluginVisible(pluginId) {
  await until(() => {
    if (uiNodes().some(item => item.text === pluginId)) return true;
    adb('shell', 'input', 'swipe', '540', '1000', '540', '300', '400');
    return false;
  }, `visible plugin ${pluginId}`, 45000);
}
async function tapPluginAction(pluginId, actionText) {
  await ensurePluginVisible(pluginId);
  const nodes = await until(() => {
    const current = uiNodes();
    const idNode = current.find(item => item.text === pluginId);
    if (!idNode) return null;
    const actions = current.filter(item => item.text === actionText);
    return actions.toSorted((a, b) => Math.abs(a.y - idNode.y) - Math.abs(b.y - idNode.y))[0];
  }, `${actionText} for ${pluginId}`);
  adb('shell', 'input', 'tap', String(nodes.x), String(nodes.y));
  await pause(500);
}
async function openDownloadsInPicker() {
  if (uiNodes().some(item => item.text === 'Files in Downloads')) return;
  adb('shell', 'input', 'tap', '50', '120');
  const folder = await until(() => uiNodes().find(item => item.text === 'Downloads' && item.top > 200), 'Downloads in picker navigation');
  adb('shell', 'input', 'tap', String(folder.x), String(folder.y));
  await pause(500);
}

class Cdp {
  static async connect(url) {
    const client = new Cdp();
    client.ws = new WebSocket(url);
    client.nextId = 0;
    client.pending = new Map();
    client.ws.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      const pending = client.pending.get(message.id);
      if (!pending) return;
      client.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    });
    await new Promise((resolve, reject) => {
      client.ws.addEventListener('open', resolve, { once: true });
      client.ws.addEventListener('error', reject, { once: true });
    });
    return client;
  }
  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    assert(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  close() { this.ws.close(); }
}

let cdp;
let forwardedPort;
async function connectWebView(pluginId = id) {
  const appPid = adb('shell', 'pidof', appId).trim();
  const port = adb('forward', 'tcp:0', `localabstract:webview_devtools_remote_${appPid}`).trim();
  forwardedPort = port;
  const target = await until(async () => {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    return pages.find(page => page.type === 'page');
  }, 'plugin WebView debugger');
  cdp = await Cdp.connect(target.webSocketDebuggerUrl);
  await until(async () => await cdp.evaluate("document.readyState === 'complete'"), 'plugin page load');
  await cdp.evaluate(`(() => {
    globalThis.__probePending = new Map();
    globalThis.__probeSequence = 0;
    const original = PluginHostBridge.onmessage;
    PluginHostBridge.onmessage = event => {
      let response;
      try { response = JSON.parse(event.data); } catch { return; }
      const pending = globalThis.__probePending.get(response?.id);
      if (pending) { clearTimeout(pending.timer); globalThis.__probePending.delete(response.id); pending.resolve(response); }
      else original?.(event);
    };
    globalThis.__probeRequest = (capability, params, requestPluginId = ${JSON.stringify(pluginId)}) => {
      const requestId = 'probe_' + (++globalThis.__probeSequence);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { globalThis.__probePending.delete(requestId); reject(new Error('probe timeout')); }, 5000);
        globalThis.__probePending.set(requestId, { resolve, reject, timer });
        PluginHostBridge.postMessage(JSON.stringify({ id: requestId, apiVersion: '1.0.0', pluginId: requestPluginId, capability, params }));
      });
    };
    globalThis.__probeRaw = raw => new Promise(resolve => {
      const previous = PluginHostBridge.onmessage;
      PluginHostBridge.onmessage = event => {
        PluginHostBridge.onmessage = previous;
        resolve(JSON.parse(event.data));
      };
      PluginHostBridge.postMessage(raw);
    });
  })()`);
}

async function importPackage(packageName = zipName, pluginId = id, confirmUpdate = false) {
  await tapText('导入 ZIP');
  if (!uiNodes().some(item => item.text === packageName)) {
    await openDownloadsInPicker();
  }
  await tapText(packageName, `select ${packageName}`);
  if (confirmUpdate) await tapText('更新', `confirm update ${packageName}`);
  await ensurePluginVisible(pluginId);
}
async function openPlugin(pluginId = id) {
  await tapPluginAction(pluginId, '打开');
  await connectWebView(pluginId);
  await until(async () => await cdp.evaluate("document.querySelector('#status')?.textContent !== null"), 'Quick Notes page');
}
async function leavePlugin(pluginId = id) {
  cdp?.close(); cdp = undefined;
  await tapText('返回插件列表');
  await ensurePluginVisible(pluginId);
}
async function grant(capability) {
  const label = capability === 'data.read' ? '授权读取' : '授权写入';
  await tapText(label, label);
}
async function removePlugin(deleteData, pluginId = id) {
  await tapPluginAction(pluginId, '移除');
  if (deleteData) {
    const textNode = await until(() => uiNodes().find(item => item.text.includes('同时永久删除此插件的宿主数据和页面来源存储')),
      'data deletion option');
    adb('shell', 'input', 'tap', String(textNode.x), String(textNode.y));
    await pause(500);
    const checked = uiNodes().some(item => item.checkable && item.checked && Math.abs(item.y - textNode.y) < 100);
    assert(checked, 'The explicit deletion option did not become checked');
  }
  await tapText('移除安装资源', 'confirm plugin removal');
  await until(() => !uiNodes().some(item => item.text === pluginId), 'plugin removal');
}

try {
  assert(serial.startsWith('emulator-'), `Refusing device acceptance on non-emulator serial: ${serial}`);
  adb('get-state');
  const python = process.env.PYTHON ?? 'python';
  const v1Dir = join(dir, 'v1');
  const v2Dir = join(dir, 'v2');
  const secondDir = join(dir, 'second');
  const incompatibleDir = join(dir, 'incompatible');
  execFileSync(python, [join(process.cwd(), 'scripts', 'build_stage14_fixture.py'), '--out-dir', v1Dir, '--id', id, '--version', '1.0.0'], { stdio: 'inherit' });
  execFileSync(python, [join(process.cwd(), 'scripts', 'build_stage14_fixture.py'), '--out-dir', v2Dir, '--id', id, '--version', '1.1.0'], { stdio: 'inherit' });
  execFileSync(python, [join(process.cwd(), 'scripts', 'build_stage14_fixture.py'), '--out-dir', incompatibleDir, '--id', id, '--version', '1.2.0', '--model-version', '2.0.0'], { stdio: 'inherit' });
  execFileSync(python, [join(process.cwd(), 'scripts', 'build_stage14_fixture.py'), '--out-dir', secondDir, '--id', secondId], { stdio: 'inherit' });
  adb('push', join(v1Dir, zipName), `/sdcard/Download/${zipName}`);
  adb('push', join(v2Dir, zipName), `/sdcard/Download/${updateZipName}`);
  adb('push', join(v1Dir, zipName), `/sdcard/Download/${downgradeZipName}`);
  adb('push', join(incompatibleDir, zipName), `/sdcard/Download/${incompatibleZipName}`);
  adb('push', join(secondDir, secondZipName), `/sdcard/Download/${secondZipName}`);
  const apk = join(process.cwd(), 'android-app', 'app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk');
  adb('install', '-r', apk);
  const startApp = () => adb('shell', 'am', 'start', '-W', '-n', `${appId}/.MainActivity`);
  startApp();
  await importPackage();
  await openPlugin();

  let response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'Unauthorized' } })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  await grant('data.write');
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'Invalid', body: 'x'.repeat(5001) } })");
  assert.equal(response.error.code, 'INVALID_PARAMS');
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'x'.repeat(121) } })");
  assert.equal(response.error.code, 'INVALID_PARAMS');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'Stage 1.4 sample', body: 'persisted after restart' } })");
  assert.equal(response.ok, true, JSON.stringify(response));
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'Forged identity' } }, 'org.example.forged')");
  assert.equal(response.error.code, 'PLUGIN_ID_MISMATCH');
  response = await cdp.evaluate("__probeRequest('ui.toast', { text: 'not declared' })");
  assert.equal(response.error.code, 'CAPABILITY_NOT_DECLARED');
  const duplicateKeyRaw = JSON.stringify({ id: 'dup', apiVersion: '1.0.0', pluginId: id, capability: 'data.write', params: { collection: 'notes', record: { title: 'Duplicate key sample' } } })
    .replace('"id":"dup"', '"id":"first","id":"dup"');
  response = await cdp.evaluate(`__probeRaw(${JSON.stringify(duplicateKeyRaw)})`);
  assert.equal(response.error.code, 'INVALID_REQUEST');
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 1);
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  await tapText('撤销读取授权');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  await cdp.evaluate("localStorage.setItem('stage14-retention-marker', 'kept')");
  console.log('PASS: default deny, separate read/write grants, schema validation, forged identity, undeclared capability, and immediate revocation');

  await leavePlugin();
  adb('shell', 'am', 'force-stop', appId);
  startApp();
  await ensurePluginVisible(id);
  await openPlugin();
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 1);
  assert.equal(await cdp.evaluate("localStorage.getItem('stage14-retention-marker')"), 'kept');
  console.log('PASS: records and origin storage survive process restart while grants reset');
  await leavePlugin();

  adb('shell', 'am', 'force-stop', appId);
  adb('shell', 'run-as', appId, 'mv', `files/plugins/${id}`, `files/plugins/.backup-${id}-12345`);
  startApp();
  await ensurePluginVisible(id);
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  console.log('PASS: interrupted directory swap restores the previous installed package');
  await leavePlugin();

  await importPackage(updateZipName, id, true);
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 1);
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  await leavePlugin();
  await importPackage(downgradeZipName, id);
  await until(() => uiNodes().some(item => item.text.includes('已安装相同或更新版本，未覆盖现有插件。')), 'downgrade rejection');
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  console.log('PASS: confirmed compatible upgrade preserves data; downgrade is rejected with data intact');
  await leavePlugin();
  await importPackage(incompatibleZipName, id);
  await until(() => uiNodes().some(item => item.text.includes('新版本的数据模型不兼容，现有插件保持不变。')), 'incompatible migration rejection');
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  console.log('PASS: unsupported data model migration is rejected while the prior package and records remain intact');
  await leavePlugin();

  await importPackage(secondZipName, secondId);
  await openPlugin(secondId);
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.error.code, 'PERMISSION_DENIED');
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 0);
  await grant('data.write');
  response = await cdp.evaluate("__probeRequest('data.write', { collection: 'notes', record: { title: 'Other plugin data' } })");
  assert.equal(response.ok, true, JSON.stringify(response));
  await cdp.evaluate("localStorage.setItem('stage14-copy-marker', 'other plugin')");
  await leavePlugin();
  await openPlugin(id);
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 1);
  assert.equal(response.result.records[0].data.title, 'Stage 1.4 sample');
  console.log('PASS: records are partitioned by plugin ID');
  await leavePlugin();
  await removePlugin(true, secondId);
  await importPackage(secondZipName, secondId);
  await openPlugin(secondId);
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 0);
  assert.equal(await cdp.evaluate("localStorage.getItem('stage14-copy-marker')"), null);
  await leavePlugin();
  await removePlugin(true, secondId);
  await openPlugin(id);
  assert.equal(await cdp.evaluate("localStorage.getItem('stage14-retention-marker')"), 'kept');
  await leavePlugin();
  console.log('PASS: confirmed deletion is scoped to the selected plugin origin');

  await removePlugin(false);
  await importPackage();
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 1);
  assert.equal(await cdp.evaluate("localStorage.getItem('stage14-retention-marker')"), 'kept');
  console.log('PASS: uninstall retains plugin data and page-origin storage by default');

  await leavePlugin();
  await removePlugin(true);
  await importPackage();
  await openPlugin();
  await grant('data.read');
  response = await cdp.evaluate("__probeRequest('data.read', { collection: 'notes' })");
  assert.equal(response.ok, true, JSON.stringify(response));
  assert.equal(response.result.records.length, 0);
  assert.equal(await cdp.evaluate("localStorage.getItem('stage14-retention-marker')"), null);
  console.log('PASS: confirmed deletion removes host records and page-origin storage');

  await leavePlugin();
  await removePlugin(true);
  console.log('ALL ANDROID PLUGIN DATA CHECKS PASSED');
} finally {
  cdp?.close();
  if (forwardedPort) spawnSync(adbPath, ['-s', serial, 'forward', '--remove', `tcp:${forwardedPort}`], { windowsHide: true });
  if (serial.startsWith('emulator-')) {
    spawnSync(adbPath, ['-s', serial, 'shell', 'rm', '-f', `/sdcard/Download/${zipName}`, `/sdcard/Download/${updateZipName}`, `/sdcard/Download/${downgradeZipName}`, `/sdcard/Download/${incompatibleZipName}`, `/sdcard/Download/${secondZipName}`], { windowsHide: true });
  }
  await rm(dir, { recursive: true, force: true });
}
