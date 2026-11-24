// Generated snapshots through ADB and the system picker, with optional Stage 3.3 iterations.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const [store, taskId, directory, serial, ...iterations] = process.argv.slice(2);
assert(store && taskId && directory && serial?.startsWith('emulator-'),
  'Usage: verify-stage31.mjs <task-store> <task-id> <handoff-directory> <emulator-serial>');
assert(iterations.length === 0 || iterations.length === 6, 'Supply all three Stage 3.3 task/directory pairs');
const adbPath = process.env.ADB ?? join(process.env.LOCALAPPDATA, 'Android/Sdk/platform-tools/adb.exe');
const python = process.env.PYTHON ?? 'python';
const appId = 'com.example.pluginhost';
const adb = (...args) => execFileSync(adbPath, ['-s', serial, ...args], { encoding: 'utf8', windowsHide: true, timeout: 60000 });
const adbBytes = (...args) => execFileSync(adbPath, ['-s', serial, ...args], { windowsHide: true, timeout: 60000 });
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const receipt = JSON.parse(await readFile(join(directory, 'delivery.json'), 'utf8'));
assert.match(receipt.pluginId, /^org\.generated\.handoff-[a-f0-9]{12}$/);
assert.match(taskId, /^task-[a-f0-9]{32}$/);
const pluginId = receipt.pluginId;
const remoteName = `stage31-${taskId}.zip`;
const badName = `stage31-bad-${taskId}.zip`;
const iterationBuilds = [];
for (let i = 0; i < iterations.length; i += 2) {
  const [id, path] = iterations.slice(i, i + 2);
  assert.match(id, /^task-[a-f0-9]{32}$/);
  const next = JSON.parse(await readFile(join(path, 'delivery.json'), 'utf8'));
  assert.equal(next.pluginId, pluginId);
  assert.equal(next.pluginVersion, `${2 + i / 2}.0.0`);
  iterationBuilds.push({ id, path, receipt: next, remoteName: `stage31-${id}.zip` });
}
const uiPath = `/sdcard/stage31-${taskId}.xml`;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, label, timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { const value = await fn(); if (value) return value; } catch (error) { last = error; }
    await pause(250);
  }
  throw new Error(`Timed out waiting for ${label}`, { cause: last });
}
function nodes() {
  const dump = spawnSync(adbPath, ['-s', serial, 'shell', 'uiautomator', 'dump', uiPath], { encoding: 'utf8', windowsHide: true, timeout: 20000 });
  assert([0, 137].includes(dump.status), dump.stderr || dump.stdout);
  const xml = adb('shell', 'cat', uiPath);
  return [...xml.matchAll(/<node\b([^>]*)>/g)].map(([, attrs]) => {
    const value = name => attrs.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] ?? '';
    const bounds = value('bounds').match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/);
    if (!bounds) return null;
    return { text: value('text').replaceAll('&quot;', '"').replaceAll('&amp;', '&'), description: value('content-desc'),
      checked: value('checked') === 'true', checkable: value('checkable') === 'true',
      x: (Number(bounds[1]) + Number(bounds[3])) / 2, y: (Number(bounds[2]) + Number(bounds[4])) / 2 };
  }).filter(Boolean);
}
async function tap(text) {
  const node = await until(() => nodes().find(item => item.text === text || item.description === text), text);
  await tapNode(node);
}
async function tapNode(node) {
  adb('shell', 'input', 'tap', String(Math.floor(node.x)), String(Math.floor(node.y)));
  await pause(300);
}
async function action(text) {
  const selected = await until(() => {
    const current = nodes();
    const identity = current.find(item => item.text === pluginId);
    if (!identity) { adb('shell', 'input', 'swipe', '540', '1000', '540', '300', '400'); return null; }
    const button = current.filter(item => item.text === text)
      .sort((a, b) => Math.abs(a.y - identity.y) - Math.abs(b.y - identity.y))[0];
    if (button) return button;
    // The expanded row may be clipped even when its identity is still visible.
    const y = Math.floor(identity.y);
    adb('shell', 'input', 'swipe', '540', String(y), '540', String(Math.max(0, y - 500)), '400');
    return null;
  }, `${text} ${pluginId}`);
  adb('shell', 'input', 'tap', String(Math.floor(selected.x)), String(Math.floor(selected.y)));
  await pause(300);
}
async function selectZip(name) {
  await tap('导入 ZIP');
  let current = await until(() => {
    const snapshot = nodes();
    return snapshot.some(item => /Show roots|显示根目录|打开导航抽屉/.test(item.description) || item.text === name) ? snapshot : null;
  }, 'system file picker');
  if (!current.some(item => item.text === name)) {
    const drawer = current.find(item => /Show roots|显示根目录|打开导航抽屉/.test(item.description));
    if (drawer) {
      adb('shell', 'input', 'tap', String(Math.floor(drawer.x)), String(Math.floor(drawer.y)));
      current = await until(() => {
        const snapshot = nodes();
        return snapshot.some(item => ['Downloads', 'Download', '下载'].includes(item.text)) ? snapshot : null;
      }, 'Downloads root');
    }
    // ADB copies may be absent from the indexed Downloads provider. Select
    // primary storage and its actual Download directory instead. Drawer nodes
    // come after the obscured page; don't tap a same-named background title.
    const model = adb('shell', 'getprop', 'ro.product.model').trim();
    const storage = current.findLast(item => item.text === model || item.text === 'Internal storage');
    if (storage) {
      await tapNode(storage);
      const folder = await until(() => nodes().findLast(item => ['Download', '下载'].includes(item.text)), 'primary storage Download folder');
      await tapNode(folder);
    } else {
      const downloads = current.findLast(item => ['Downloads', 'Download', '下载'].includes(item.text));
      assert(downloads, 'Downloads root unavailable');
      await tapNode(downloads);
    }
    const listView = nodes().find(item => item.description === 'List view' || item.description === '列表视图');
    if (listView) await tap(listView.description);
  }
  const selected = await until(() => {
    const item = nodes().find(item => item.text === name);
    if (item) return item;
    adb('shell', 'input', 'swipe', '540', '1950', '540', '650', '350');
    return null;
  }, `ZIP ${name}`, 45000);
  adb('shell', 'input', 'tap', String(Math.floor(selected.x)), String(Math.floor(selected.y)));
  await pause(300);
}
class Cdp {
  constructor(url) {
    this.ws = new WebSocket(url); this.sequence = 0; this.pending = new Map();
    this.ws.addEventListener('message', event => {
      const msg = JSON.parse(event.data); const item = this.pending.get(msg.id); if (!item) return;
      this.pending.delete(msg.id); clearTimeout(item.timer);
      msg.error ? item.reject(new Error(JSON.stringify(msg.error))) : item.resolve(msg.result);
    });
  }
  async ready() { await new Promise((resolve, reject) => { this.ws.addEventListener('open', resolve, { once: true }); this.ws.addEventListener('error', reject, { once: true }); }); }
  send(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
      this.pending.set(id, { resolve, reject, timer }); this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails)); return result.result.value;
  }
}
let cdp, forwarded;
let installed = false;
async function start() {
  adb('shell', 'am', 'start', '-W', '-n', `${appId}/.MainActivity`);
  await tap('打开会话菜单');
  await tap('我的插件');
}
async function connect() {
  const pid = adb('shell', 'pidof', appId).trim();
  forwarded = adb('forward', 'tcp:0', `localabstract:webview_devtools_remote_${pid}`).trim();
  const target = await until(async () => (await (await fetch(`http://127.0.0.1:${forwarded}/json/list`)).json())
    .find(page => page.type === 'page' && page.url.startsWith('https://p-')), 'installed plugin WebView');
  cdp = new Cdp(target.webSocketDebuggerUrl); await cdp.ready();
  await until(() => cdp.evaluate("document.readyState === 'complete' && !!document.querySelector('#refresh')"), 'generated plugin page');
}
async function disconnect() {
  cdp?.ws.close(); cdp = undefined;
  if (forwarded) { adb('forward', '--remove', `tcp:${forwarded}`); forwarded = undefined; }
}
function installedHashes(id, expectedVersion, relativeSlot = '') {
  const state = JSON.parse(execFileSync(python, [join(root, 'scripts/task_runtime.py'), '--store', store, 'show', id], { encoding: 'utf8', windowsHide: true }));
  const files = state.builds.at(-1).files;
  const prefix = `files/plugins/${pluginId}/${relativeSlot}`;
  for (const [name, expected] of Object.entries(files)) {
    assert.match(name, /^(manifest\.json|index\.html|app\.js|app\.css)$/);
    assert.equal(hash(adbBytes('exec-out', 'run-as', appId, 'cat', prefix + name)), expected, name);
  }
  const manifest = JSON.parse(adb('exec-out', 'run-as', appId, 'cat', prefix + 'manifest.json'));
  assert.equal(manifest.id, pluginId); assert.equal(manifest.version, expectedVersion);
}
function transfer(id, path, expected) {
  const transferred = JSON.parse(execFileSync(python, [join(root, 'scripts/plugin_delivery.py'), '--store', store,
    'push', id, '--directory', path, '--serial', serial, '--adb', adbPath], { encoding: 'utf8', windowsHide: true }));
  assert.equal(transferred.status, 'transferred'); assert.equal(transferred.imported, false);
  assert.equal(transferred.deliverable, false); assert.equal(transferred.packageSha256, expected.packageSha256);
  assert.equal(adb('shell', 'toybox', 'sha256sum', transferred.remotePath).split(/\s/)[0], expected.packageSha256);
}
const firstNote = 'Fictional stage31 note';
const storageKey = 'stage33-fictional-page-state';
async function checkNotes(notes, pageTitle, storageValue, addNote) {
  await action('打开'); await connect();
  assert.equal(await cdp.evaluate('document.title'), pageTitle);
  const stored = await cdp.evaluate(`localStorage.getItem(${JSON.stringify(storageKey)})`);
  assert.equal(stored, storageValue, 'Page origin storage across versions');
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'grants reset');
  await tap('授权读取');
  if (addNote) {
    await tap('授权写入');
    await cdp.evaluate(`localStorage.setItem(${JSON.stringify(storageKey)}, ${JSON.stringify(addNote)})`);
    await cdp.evaluate(`document.querySelector('#new-note').click(); document.querySelector('#title').value=${JSON.stringify(addNote)}; document.querySelector('#note-form').requestSubmit()`);
  } else {
    await cdp.evaluate("document.querySelector('#refresh').click()");
  }
  await until(() => cdp.evaluate(`${JSON.stringify(notes)}.every(note => document.querySelector('#notes').innerText.includes(note))`),
    'notes preserved across versions');
  await disconnect(); await tap('返回插件列表');
}
async function verifyIterations() {
  const [second, third, incompatible] = iterationBuilds;
  const notes = [firstNote];
  let stored = firstNote;
  for (const build of [second, third]) {
    transfer(build.id, build.path, build.receipt);
    await selectZip(build.remoteName);
    await until(() => nodes().some(item => item.text === '确认更新插件'), 'update confirmation');
    await tap('更新');
    await until(() => nodes().some(item => item.text.includes(`更新完成：${build.receipt.pluginName} ${build.receipt.pluginVersion}`)),
      'generated update installed');
    installedHashes(build.id, build.receipt.pluginVersion);
    const note = `Fictional note added on ${build.receipt.pluginVersion}`;
    notes.push(note);
    await checkNotes(notes, build.receipt.pluginName, stored, note);
    stored = note;
    console.log(`PASS updated to ${build.receipt.pluginVersion}, exact resources and data from earlier versions`);
  }
  transfer(incompatible.id, incompatible.path, incompatible.receipt);
  await selectZip(incompatible.remoteName);
  await until(() => nodes().some(item => item.text.includes('数据模型不兼容')), 'reject tightened model');
  installedHashes(third.id, third.receipt.pluginVersion);
  await checkNotes(notes, third.receipt.pluginName, stored);
  installedHashes(second.id, second.receipt.pluginVersion, '.previous/');
  console.log('PASS incompatible update preserves installed resources and all records');

  await action('回退到 2.0.0');
  await tap('取消');
  installedHashes(third.id, third.receipt.pluginVersion);
  await action('回退到 2.0.0');
  await until(() => nodes().some(item => item.text.includes(pluginId) && item.text.includes('包括更新后新增的数据')),
    'rollback data policy and exact plugin in confirmation');
  await tap('确认回退');
  await until(() => nodes().some(item => item.text.includes('回退完成')), 'rollback completed');
  installedHashes(second.id, second.receipt.pluginVersion);
  assert(!adb('shell', 'run-as', appId, 'ls', '-a', `files/plugins/${pluginId}`).split(/\s+/).includes('.previous'),
    'Rollback consumes the saved previous version');
  await checkNotes(notes, second.receipt.pluginName, stored);
  adb('shell', 'am', 'force-stop', appId); await start();
  installedHashes(second.id, second.receipt.pluginVersion);
  await checkNotes(notes, second.receipt.pluginName, stored);
  console.log('PASS rollback confirmation and cancellation, exact restored resources, all three notes and page storage after restart');
  console.log('ALL STAGE 3.3 ANDROID CHECKS PASSED');
}
try {
  assert.equal(adb('shell', 'getprop', 'ro.kernel.qemu').trim(), '1', 'Emulator required');
  transfer(taskId, directory, receipt);
  console.log('PASS explicit target transfer and device-side ZIP SHA-256');
  adb('install', '-r', join(root, 'android-app/app/build/outputs/apk/debug/app-debug.apk'));
  adb('shell', 'am', 'force-stop', appId); await start();
  await selectZip(remoteName);
  await until(() => adb('shell', 'run-as', appId, 'ls', `files/plugins/${pluginId}/manifest.json`).trim(), 'installed generated package');
  installed = true; installedHashes(taskId, receipt.pluginVersion);
  console.log('PASS system picker import, installed ID/version and every source hash');
  await action('打开'); await connect();
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'default denied');
  await tap('授权写入');
  await cdp.evaluate("document.querySelector('#new-note').click(); document.querySelector('#title').value='Fictional stage31 note'; document.querySelector('#body').value='Emulator handoff acceptance'; document.querySelector('#note-form').requestSubmit()");
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('笔记已保存，列表读取失败')"), 'write granted separately');
  await tap('授权读取'); await cdp.evaluate("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluate("document.querySelector('#notes').innerText.includes('Fictional stage31 note')"), 'read saved note');
  console.log('PASS generated page default deny, separate grants and actual note interaction');
  await disconnect(); adb('shell', 'am', 'force-stop', appId); await start();
  await action('打开'); await connect();
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'grants reset after restart');
  await tap('授权读取'); await cdp.evaluate("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluate("document.querySelector('#notes').innerText.includes('Fictional stage31 note')"), 'persistent imported plugin data');
  if (iterationBuilds.length) {
    await cdp.evaluate(`localStorage.setItem(${JSON.stringify(storageKey)}, ${JSON.stringify(firstNote)})`);
  }
  await disconnect(); await tap('返回插件列表');
  adb('push', join(root, 'plugin-protocol/fixtures/invalid-hash-mismatch.zip'), `/sdcard/Download/${badName}`);
  await selectZip(badName);
  await until(() => nodes().some(item => item.text.includes('插件资源摘要校验失败')), 'tampered package rejection');
  installedHashes(taskId, receipt.pluginVersion);
  console.log('PASS restart persistence and corrupt package rejected with installed resources intact');
  console.log(JSON.stringify({ device: serial, api: adb('shell', 'getprop', 'ro.build.version.sdk').trim(),
    pluginId, pluginVersion: receipt.pluginVersion, packageSha256: receipt.packageSha256, imported: true, deliverable: false }));
  console.log('ALL STAGE 3.1 ANDROID CHECKS PASSED');
  if (iterationBuilds.length) await verifyIterations();
} finally {
  await disconnect();
  if (installed) {
    // Also recover from failures while the external picker is in the foreground.
    adb('shell', 'am', 'force-stop', appId);
    adb('shell', 'input', 'keyevent', '4');
    await start();
    await action('移除');
    await until(() => nodes().some(item => item.text.includes(`（${pluginId}）`)), 'test plugin identity in removal confirmation');
    const label = await until(() => nodes().find(item => item.text.includes('同时永久删除此插件的宿主数据和页面来源存储')), 'test data deletion option');
    adb('shell', 'input', 'tap', String(Math.floor(label.x)), String(Math.floor(label.y)));
    assert(nodes().some(item => item.checkable && item.checked), 'Explicit deletion must be selected');
    await tap('移除安装资源');
    await until(() => !nodes().some(item => item.text === pluginId), 'temporary plugin cleanup');
  }
  adb('shell', 'rm', '-f', ...[remoteName, badName, ...iterationBuilds.map(build => build.remoteName)]
    .map(name => `/sdcard/Download/${name}`), uiPath);
}
