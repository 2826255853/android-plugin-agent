// Android emulator acceptance for the manually entered contacts template.
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const [packagePath, serial] = process.argv.slice(2);
assert(packagePath && serial?.startsWith('emulator-'), 'Usage: verify-stage-c1.mjs <package.zip> <emulator-serial>');
const adbPath = process.env.ADB ?? join(process.env.LOCALAPPDATA ?? '', 'Android/Sdk/platform-tools/adb.exe');
const appId = 'com.example.pluginhost';
const pluginId = 'org.generated.manual-contacts';
const zipName = 'c1-manual-contacts.zip';
const uiFile = '/sdcard/c1-manual-contacts.xml';
const adb = (...args) => execFileSync(adbPath, ['-s', serial, ...args], {
  encoding: 'utf8', windowsHide: true, timeout: 60000,
});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, label, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try { const value = await fn(); if (value) return value; } catch (error) { lastError = error; }
    await pause(250);
  }
  throw new Error(`Timed out waiting for ${label}`, { cause: lastError });
}
function nodes() {
  const result = spawnSync(adbPath, ['-s', serial, 'shell', 'uiautomator', 'dump', uiFile], {
    encoding: 'utf8', windowsHide: true, timeout: 20000,
  });
  assert([0, 137].includes(result.status), result.stderr || result.stdout);
  let xml = '';
  try { xml = adb('shell', 'cat', uiFile); } catch { return []; }
  return [...xml.matchAll(/<node\b([^>]*)>/g)].map(([, attrs]) => {
    const value = name => attrs.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] ?? '';
    const bounds = value('bounds').match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/);
    if (!bounds) return null;
    return { text: value('text').replaceAll('&quot;', '"').replaceAll('&amp;', '&'),
      description: value('content-desc'), x: (Number(bounds[1]) + Number(bounds[3])) / 2,
      y: (Number(bounds[2]) + Number(bounds[4])) / 2 };
  }).filter(Boolean);
}
async function tap(text) {
  const node = await until(() => nodes().find(item => item.text === text || item.description === text), text);
  adb('shell', 'input', 'tap', String(Math.floor(node.x)), String(Math.floor(node.y)));
  await pause(400);
}
async function enterLibrary() {
  await adb('shell', 'am', 'start', '-W', '-n', `${appId}/.MainActivity`);
  await until(() => nodes().some(item => item.text === '我的插件'
    || ['打开会话菜单', '关闭会话菜单'].includes(item.description)), 'host navigation');
  if (!nodes().some(item => item.text === '我的插件')) await tap('打开会话菜单');
  await tap('我的插件');
}
async function pickPackage() {
  await tap('导入 ZIP');
  let snapshot = await until(() => {
    const current = nodes();
    return current.some(item => item.text === zipName || item.description.includes('Show roots')
      || item.description.includes('显示根目录')) ? current : null;
  }, 'system ZIP picker');
  if (!snapshot.some(item => item.text === zipName)) {
    const drawer = snapshot.find(item => item.description.includes('Show roots') || item.description.includes('显示根目录'));
    if (drawer) {
      await tap(drawer.description);
      snapshot = await until(() => nodes().some(item => ['Downloads', 'Download', '下载'].includes(item.text))
        ? nodes() : null, 'Downloads root');
    }
    const model = adb('shell', 'getprop', 'ro.product.model').trim();
    const storage = snapshot.findLast(item => item.text === model || item.text === 'Internal storage');
    if (storage) {
      adb('shell', 'input', 'tap', String(Math.floor(storage.x)), String(Math.floor(storage.y)));
      const folder = await until(() => nodes().findLast(item => ['Download', '下载'].includes(item.text)), 'Download folder');
      adb('shell', 'input', 'tap', String(Math.floor(folder.x)), String(Math.floor(folder.y)));
    } else {
      const downloads = snapshot.findLast(item => ['Downloads', 'Download', '下载'].includes(item.text));
      assert(downloads, 'Downloads folder was unavailable in the system picker');
      adb('shell', 'input', 'tap', String(Math.floor(downloads.x)), String(Math.floor(downloads.y)));
    }
  }
  const file = await until(() => nodes().find(item => item.text === zipName), zipName, 45000);
  adb('shell', 'input', 'tap', String(Math.floor(file.x)), String(Math.floor(file.y)));
  await until(() => nodes().some(item => item.text === pluginId), 'installed plugin row');
}
class Cdp {
  constructor(url) {
    this.socket = new WebSocket(url); this.sequence = 0; this.pending = new Map();
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data); const pending = this.pending.get(message.id); if (!pending) return;
      clearTimeout(pending.timer); this.pending.delete(message.id);
      message.error ? pending.reject(new Error(JSON.stringify(message.error))) : pending.resolve(message.result);
    });
  }
  async ready() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true });
      this.socket.addEventListener('error', reject, { once: true });
    });
  }
  send(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP timeout ${method}`)); }, 10000);
      this.pending.set(id, { resolve, reject, timer }); this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    assert(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
}
let cdp;
let forwarded;
async function connectPlugin() {
  const pid = adb('shell', 'pidof', appId).trim();
  forwarded = adb('forward', 'tcp:0', `localabstract:webview_devtools_remote_${pid}`).trim();
  const page = await until(async () => (await (await fetch(`http://127.0.0.1:${forwarded}/json/list`)).json())
    .find(target => target.type === 'page' && target.url.startsWith('https://p-')), 'plugin WebView');
  cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.ready();
  await until(() => cdp.evaluate("document.readyState === 'complete' && !!document.querySelector('#contact-form')"), 'contacts form');
}
async function closePlugin() {
  cdp?.socket.close(); cdp = undefined;
  if (forwarded) { adb('forward', '--remove', `tcp:${forwarded}`); forwarded = undefined; }
}
async function pluginAction(label) {
  const current = nodes();
  const identity = current.find(item => item.text === pluginId);
  assert(identity, 'Plugin row missing');
  const button = current.filter(item => item.text === label)
    .sort((left, right) => Math.abs(left.y - identity.y) - Math.abs(right.y - identity.y))[0];
  assert(button, `Plugin action missing: ${label}`);
  adb('shell', 'input', 'tap', String(Math.floor(button.x)), String(Math.floor(button.y)));
}
async function removeFixturePlugin() {
  await pluginAction('移除');
  const deletion = await until(() => nodes().find(item => item.text.includes('同时永久删除此插件的宿主数据和页面来源存储')),
    'data deletion option');
  adb('shell', 'input', 'tap', String(Math.floor(deletion.x)), String(Math.floor(deletion.y)));
  await tap('移除安装资源');
  await until(() => !nodes().some(item => item.text === pluginId), 'fixture removal');
}

try {
  assert.equal(adb('shell', 'getprop', 'ro.kernel.qemu').trim(), '1', 'Android emulator required');
  adb('push', packagePath, `/sdcard/Download/${zipName}`);
  adb('install', '-r', join(root, 'android-app/app/build/outputs/apk/debug/app-debug.apk'));
  adb('shell', 'am', 'force-stop', appId);
  await enterLibrary();
  if (nodes().some(item => item.text === pluginId)) {
    await removeFixturePlugin();
  }
  await pickPackage();
  await pluginAction('打开');
  await connectPlugin();
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'default read denied');
  await tap('授权写入');
  await cdp.evaluate("document.querySelector('#new-contact').click(); document.querySelector('#name').value='林澄'; document.querySelector('#phone').value='555-0104'; document.querySelector('#email').value='cheng@example.invalid'; document.querySelector('#contact-form').requestSubmit()");
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'read remains denied after write');
  await tap('授权读取');
  await cdp.evaluate("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluate("document.querySelector('#contacts').innerText.includes('林澄') && document.querySelector('#contacts').innerText.includes('555-0104') && document.querySelector('#contacts').innerText.includes('cheng@example.invalid')"), 'saved contact visible');
  await closePlugin();
  await tap('返回插件列表');
  adb('shell', 'am', 'force-stop', appId);
  await enterLibrary();
  await pluginAction('打开');
  await connectPlugin();
  await until(() => cdp.evaluate("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'grants reset after host restart');
  await tap('授权读取');
  await cdp.evaluate("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluate("document.querySelector('#contacts').innerText.includes('林澄') && document.querySelector('#contacts').innerText.includes('cheng@example.invalid')"), 'contact persists after restart');
  const permissionCount = await cdp.evaluate("document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]') !== null");
  assert.equal(permissionCount, true);
  console.log('PASS C.1 Android emulator import, separate read/write grants, manual contact persistence and restart recovery');
} finally {
  await closePlugin();
  if (nodes().some(item => item.text === '返回插件列表')) {
    await tap('返回插件列表');
    if (nodes().some(item => item.text === pluginId)) {
      await removeFixturePlugin();
    }
  }
  adb('shell', 'rm', '-f', `/sdcard/Download/${zipName}`);
  if (forwarded) adb('forward', '--remove', `tcp:${forwarded}`);
}
