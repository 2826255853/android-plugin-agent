// Node 24+, no npm dependencies. Android mode requires a running, authorized device and Debug APK.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const mode = process.argv[2];
assert(['browser', 'android'].includes(mode), 'Usage: node scripts/verify-bridge.mjs browser|android');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, description) {
  let error;
  for (let i = 0; i < 100; i++) {
    try { const value = await fn(); if (value) return value; } catch (caught) { error = caught; }
    await pause(100);
  }
  throw new Error(`Timed out: ${description}`, { cause: error });
}

class Cdp {
  static async connect(url) {
    const client = new Cdp();
    client.socket = new WebSocket(url);
    client.sequence = 0;
    client.pending = new Map();
    client.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      const pending = client.pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      client.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    });
    await new Promise((resolve, reject) => {
      client.socket.addEventListener('open', resolve, { once: true });
      client.socket.addEventListener('error', reject, { once: true });
    });
    return client;
  }
  send(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP timeout: ${method}`));
      }, 10000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    assert(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  close() { this.socket.close(); }
}

let cdp, browser, profile, server, forwardedPort;
const adbPath = process.env.ADB ?? join(process.env.ANDROID_HOME ?? join(process.env.LOCALAPPDATA ?? '', 'Android', 'Sdk'), 'platform-tools', 'adb.exe');
const adb = (...args) => execFileSync(adbPath, [...(process.env.ANDROID_SERIAL ? ['-s', process.env.ANDROID_SERIAL] : []), ...args], { encoding: 'utf8', windowsHide: true });
const bridgePath = '/assets/test/bridge/index.html';
let origin, port;

async function connectPage() {
  const target = await until(async () => {
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    return targets.find(target => target.type === 'page');
  }, 'debug page');
  cdp = await Cdp.connect(target.webSocketDebuggerUrl);
}
async function navigate(path) {
  await cdp.send('Page.navigate', { url: origin + path });
  await until(async () => await cdp.evaluate(`location.pathname === ${JSON.stringify(path)} && document.readyState === 'complete'`), path);
}
const request = (capability, params) => cdp.evaluate(`testAdapter.request(${JSON.stringify(capability)}, ${JSON.stringify(params)})`);
const code = (response, expected) => {
  assert.equal(response.ok, false, JSON.stringify(response));
  assert.equal(response.error.code, expected);
};
function pass(name, details = '') { console.log(`PASS ${mode}: ${name}${details ? ' ' + details : ''}`); }

try {
  if (mode === 'browser') {
    const assetRoot = join(root, 'android-app', 'app', 'src', 'main', 'assets');
    server = createServer(async (req, res) => {
      try {
        const path = new URL(req.url, 'http://localhost').pathname;
        assert(path.startsWith('/assets/test/'));
        const target = resolve(assetRoot, '.' + path.slice('/assets'.length));
        assert(target.startsWith(assetRoot + sep));
        const mime = target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html';
        const content = await readFile(target);
        res.writeHead(200, { 'Content-Type': `${mime}; charset=utf-8` });
        res.end(content);
      } catch { res.writeHead(404); res.end(); }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
    profile = await mkdtemp(join(tmpdir(), 'plugin-bridge-browser-'));
    const browserPath = process.env.BROWSER_PATH ?? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
    browser = spawn(browserPath, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
    browser.on('error', error => { console.error(error.message); });
    port = await until(async () => (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0], 'browser debug port');
  } else {
    origin = 'https://appassets.androidplatform.net';
    adb('shell', 'am', 'force-stop', 'com.example.pluginhost');
    adb('shell', 'am', 'start', '-W', '-n', 'com.example.pluginhost/.MainActivity');
    const pid = await until(() => adb('shell', 'pidof', 'com.example.pluginhost').trim(), 'host process');
    port = adb('forward', 'tcp:0', `localabstract:webview_devtools_remote_${pid}`).trim();
    forwardedPort = port;
    console.log(`Android API ${adb('shell', 'getprop', 'ro.build.version.sdk').trim()}; ${adb('shell', 'dumpsys', 'webviewupdate').match(/Current WebView package[^\r\n]*/)?.[0]}`);
  }
  await connectPage();
  await navigate(bridgePath);
  assert.match(await cdp.evaluate("document.querySelector('#environment').textContent"), mode === 'browser' ? /浏览器模拟/ : /Android 实际/);
  await cdp.evaluate(`import('./adapters.js').then(module => { globalThis.testAdapter = new module.${mode === 'browser' ? 'BrowserAdapter' : 'AndroidAdapter'}(); })`);
  code(await request('ui.toast', { text: '默认拒绝样例' }), 'PERMISSION_DENIED');
  pass('default denial');

  async function nativeState() {
    const path = '/sdcard/plugin-host-bridge-window.xml';
    adb('shell', 'rm', '-f', path);
    try { adb('shell', 'uiautomator', 'dump', path); }
    catch (error) {
      // API 37 may kill the legacy dumper after it has successfully written the XML.
      if (error.status !== 137 || !error.stdout?.includes(`dumped to: ${path}`)) throw error;
    }
    const xml = adb('shell', 'cat', path);
    assert(xml.includes('<hierarchy'), 'uiautomator did not produce a current hierarchy');
    return xml;
  }
  async function toggleNativeGrant(label) {
    const xml = await nativeState();
    const node = xml.match(new RegExp(`<node[^>]*text="${label}"[^>]*>`))?.[0];
    assert(node, `Native grant control missing: ${label}`);
    const bounds = node.match(/bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/).slice(1).map(Number);
    adb('shell', 'input', 'tap', String(Math.floor((bounds[0] + bounds[2]) / 2)), String(Math.floor((bounds[1] + bounds[3]) / 2)));
  }
  if (mode === 'browser') await cdp.evaluate('testAdapter.setToastGranted(true)');
  else await toggleNativeGrant('授权提示');
  const success = await request('ui.toast', { text: '桥接往返虚构样例' });
  assert.equal(success.ok, true, JSON.stringify(success));
  assert.equal(success.result.simulated, mode === 'browser');
  assert.equal(success.result.environment, mode === 'browser' ? 'browser-simulation' : 'android');
  pass('authorized round trip', JSON.stringify(success));
  code(await request('data.write', { text: '越权样例' }), 'CAPABILITY_NOT_DECLARED');
  code(await request('ui.toast', { text: 42 }), 'INVALID_PARAMS');
  code(await request('ui.toast', { text: 'x', extra: true }), 'INVALID_PARAMS');
  code(await request('ui.toast', { text: 'x'.repeat(121) }), 'INVALID_PARAMS');
  code(await request('ui.toast', { text: '\n' }), 'INVALID_PARAMS');
  pass('undeclared capability and invalid parameters rejected');

  if (mode === 'android') {
    await cdp.evaluate(`globalThis.rawRequest = raw => new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('raw timeout')), 3000);
      PluginHostBridge.onmessage = event => { clearTimeout(timer); resolve(JSON.parse(event.data)); };
      PluginHostBridge.postMessage(raw);
    })`);
    const raw = value => cdp.evaluate(`rawRequest(${JSON.stringify(typeof value === 'string' ? value : JSON.stringify(value))})`);
    const base = { id: 'negative_case', apiVersion: '1.0.0', pluginId: 'org.example.bridge-demo', capability: 'ui.toast', params: { text: '应拒绝样例' } };
    code(await raw({ ...base, pluginId: 'org.example.forged' }), 'PLUGIN_ID_MISMATCH');
    code(await raw({ ...base, apiVersion: '2.0.0' }), 'UNSUPPORTED_API_VERSION');
    code(await raw({ ...base, injectedGrant: true }), 'INVALID_REQUEST');
    code(await raw('broken JSON'), 'INVALID_REQUEST');
    code(await raw(JSON.stringify(base) + '{}'), 'INVALID_REQUEST');
    code(await raw(JSON.stringify(base).replace('"id":"negative_case"', '"id":"first","id":"negative_case"')), 'INVALID_REQUEST');
    code(await raw({ ...base, params: [[[[[[[[[{}]]]]]]]]] }), 'INVALID_REQUEST');
    code(await raw(JSON.stringify(base).replace('"negative_case"', "'negative_case'")), 'INVALID_REQUEST');
    code(await raw('x'.repeat(4097)), 'REQUEST_TOO_LARGE');
    code(await raw({ ...base, id: 42 }), 'INVALID_REQUEST');
    pass('forged identity, API, extra fields, malformed and oversized requests rejected');

    for (const sandbox of [false, true]) {
      const result = await cdp.evaluate(`new Promise((resolve, reject) => {
        const frame = document.createElement('iframe');
        const timer = setTimeout(() => { frame.remove(); reject(new Error('iframe timeout')); }, 3000);
        const listener = event => {
          if (event.source !== frame.contentWindow || event.data?.type !== 'bridge-frame-result') return;
          clearTimeout(timer); window.removeEventListener('message', listener); frame.remove(); resolve(event.data.result);
        };
        window.addEventListener('message', listener);
        ${sandbox ? "frame.setAttribute('sandbox', 'allow-scripts');" : ''}
        frame.src = 'frame.html'; document.body.append(frame);
      })`);
      if (sandbox) assert.equal(result.bridgeExposed, false, JSON.stringify(result));
      else { assert.equal(result.bridgeExposed, true); code(result.response, 'SOURCE_NOT_ALLOWED'); }
      pass(sandbox ? 'opaque-origin iframe has no native object' : 'same-origin subframe rejected');
    }
    assert.match(await nativeState(), /已显示提示：1/);
    pass('all denied requests produced no toast side effects');
    await toggleNativeGrant('撤销提示授权');
    // Restore adapter callback after raw transport tests.
    await cdp.evaluate("import('./adapters.js').then(module => { globalThis.testAdapter = new module.AndroidAdapter(); })");
  } else await cdp.evaluate('testAdapter.setToastGranted(false)');
  code(await request('ui.toast', { text: '撤销后应拒绝' }), 'PERMISSION_DENIED');
  pass('revocation takes effect');

  // Transport behavior is independent of native effects, so fake transports are labelled here.
  const transportResults = await cdp.evaluate(`import('./adapters.js').then(async ({ AndroidAdapter }) => {
    let unavailable;
    try { new AndroidAdapter(null); } catch (error) { unavailable = error.message; }
    const silent = new AndroidAdapter({ postMessage() {} }, 30);
    let timedOut;
    try { await silent.request('ui.toast', { text: 'timeout sample' }); } catch (error) { timedOut = error.message; }
    const transport = { postMessage(raw) {
      const { id } = JSON.parse(raw);
      setTimeout(() => this.onmessage({ data: JSON.stringify({ id, ok: true, result: { simulated: true } }) }), id.endsWith('0') ? 5 : 1);
    } };
    const adapter = new AndroidAdapter(transport);
    const results = await Promise.all([adapter.request('ui.toast', {}), adapter.request('ui.toast', {})]);
    return { unavailable, timedOut, uniqueIds: results[0].id !== results[1].id, pending: adapter.pending.size };
  })`);
  assert.deepEqual(transportResults, { unavailable: 'BRIDGE_UNAVAILABLE', timedOut: 'BRIDGE_TIMEOUT', uniqueIds: true, pending: 0 });
  pass('adapter unavailable, timeout and concurrent correlation (fake transport)');

  if (mode === 'android') {
    await navigate('/assets/test/index.html');
    const loads = await cdp.evaluate("Number(document.querySelector('#loads').textContent)");
    const wrongPage = await cdp.evaluate(`new Promise(resolve => {
      PluginHostBridge.onmessage = event => resolve(JSON.parse(event.data));
      PluginHostBridge.postMessage(JSON.stringify({ id: 'wrong_page', apiVersion: '1.0.0', pluginId: 'org.example.bridge-demo', capability: 'ui.toast', params: { text: '错误页面' } }));
    })`);
    code(wrongPage, 'SOURCE_NOT_ALLOWED');
    await cdp.send('Page.navigate', { url: origin + '/assets/test/missing.html' });
    await until(async () => (await cdp.evaluate('document.body.textContent')).includes('页面加载失败'), 'host error page');
    await cdp.evaluate("document.querySelector('#error-recovery').click()");
    await until(async () => await cdp.evaluate("document.querySelector('#loads') !== null"), 'return from error page');
    pass('wrong main page rejected and missing-resource error preserved');
    await toggleNativeGrant('授权提示');
    cdp.close(); cdp = null;
    adb('shell', 'am', 'force-stop', 'com.example.pluginhost');
    adb('shell', 'am', 'start', '-W', '-n', 'com.example.pluginhost/.MainActivity');
    const pid = await until(() => adb('shell', 'pidof', 'com.example.pluginhost').trim(), 'restarted host');
    adb('forward', `tcp:${port}`, `localabstract:webview_devtools_remote_${pid}`);
    await connectPage();
    await until(async () => await cdp.evaluate("document.querySelector('#loads') !== null"), 'restarted environment page');
    assert((await cdp.evaluate("Number(document.querySelector('#loads').textContent)")) > loads);
    await navigate(bridgePath);
    await cdp.evaluate("import('./adapters.js').then(module => { globalThis.testAdapter = new module.AndroidAdapter(); })");
    code(await request('ui.toast', { text: '重启后应拒绝' }), 'PERMISSION_DENIED');
    assert.match(await nativeState(), /已显示提示：0/);
    pass('process restart preserves localStorage and resets native grants');
  }
  console.log(`ALL ${mode.toUpperCase()} CHECKS PASSED`);
} finally {
  if (mode === 'browser' && cdp) await cdp.send('Browser.close').catch(() => {});
  cdp?.close();
  if (forwardedPort) {
    adb('forward', '--remove', `tcp:${forwardedPort}`);
    adb('shell', 'rm', '-f', '/sdcard/plugin-host-bridge-window.xml');
  }
  server?.close();
  if (browser && browser.exitCode === null) {
    await until(() => browser.exitCode !== null, 'browser exit').catch(() => browser.kill());
  }
  if (profile && resolve(profile).startsWith(resolve(tmpdir()) + sep + 'plugin-bridge-browser-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }).catch(() => {});
  }
}
