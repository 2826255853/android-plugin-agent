// Node 24+ and Microsoft Edge; verifies the package-backed browser preview end to end.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packagePath = process.argv[2];
assert(packagePath, 'Usage: node scripts/verify-stage24.mjs <verified-package.zip>');
const profile = await mkdtemp(join(tmpdir(), 'plugin-preview-stage24-'));
let server, browser, cdp;
let pluginCdp;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, label) {
  let last;
  for (let i = 0; i < 120; i++) {
    try { const value = await fn(); if (value) return value; } catch (error) { last = error; }
    await pause(100);
  }
  throw new Error(`Timed out waiting for ${label}`, { cause: last });
}
class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.sequence = 0; this.pending = new Map();
    this.socket.addEventListener('message', event => { const msg = JSON.parse(event.data); const item = this.pending.get(msg.id); if (!item) return; this.pending.delete(msg.id); clearTimeout(item.timer); msg.error ? item.reject(new Error(JSON.stringify(msg.error))) : item.resolve(msg.result); }); }
  async ready() { await new Promise((resolve, reject) => { this.socket.addEventListener('open', resolve, { once: true }); this.socket.addEventListener('error', reject, { once: true }); }); }
  send(method, params = {}) { const id = ++this.sequence; return new Promise((resolve, reject) => { const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000); this.pending.set(id, { resolve, reject, timer }); this.socket.send(JSON.stringify({ id, method, params })); }); }
  async evaluate(expression) { const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); assert(!r.exceptionDetails, JSON.stringify(r.exceptionDetails)); return r.result.value; }
  async evaluatePlugin(expression) { return pluginCdp.evaluate(expression); }
}
try {
  server = spawn('python', [join(root, 'scripts', 'browser_preview.py'), resolve(packagePath), '--no-open'], { cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'inherit'] });
  let stdout = '';
  server.stdout.setEncoding('utf8');
  server.stdout.on('data', chunk => { stdout += chunk; });
  const info = await until(() => { const line = stdout.split(/\r?\n/).find(item => item.startsWith('{')); if (!line) return null; return JSON.parse(line); }, 'preview server');
  assert.equal(info.ok, true); assert.equal(info.deliverable, false); assert.equal(info.executedGeneratedCode, false);
  const browserPath = process.env.BROWSER_PATH ?? 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
  browser = spawn(browserPath, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-proxy-server', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  const portFile = join(profile, 'DevToolsActivePort');
  const debugPort = await until(async () => (await readFile(portFile, 'utf8')).split('\n')[0], 'Edge debug port');
  const targets = await until(async () => { const r = await fetch(`http://127.0.0.1:${debugPort}/json/list`); return r.ok ? r.json() : null; }, 'Edge targets');
  cdp = new Cdp(targets.find(item => item.type === 'page').webSocketDebuggerUrl); await cdp.ready();
  await cdp.send('Runtime.enable');
  await cdp.evaluate(`location.href=${JSON.stringify(info.url)}`);
  const iframeTarget = await until(async () => (await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item => item.type === 'iframe' && item.url.includes('/plugin/')), 'isolated plugin frame');
  pluginCdp = new Cdp(iframeTarget.webSocketDebuggerUrl); await pluginCdp.ready(); await pluginCdp.send('Runtime.enable');
  await until(() => cdp.evaluatePlugin("document.querySelector('#status')?.textContent.includes('PERMISSION_DENIED')"), 'default-denied preview');
  assert.match(await cdp.evaluate('document.body.innerText'), /浏览器模拟/);
  assert.match(await cdp.evaluate('document.body.innerText'), /不代表 Android 设备能力已验证/);
  assert.equal(await cdp.evaluatePlugin('(() => { try { return !!parent.document.querySelector("header"); } catch { return false; } })()'), false, 'plugin must not access the preview control page');
  console.log('PASS preview banner and device boundary');
  await cdp.evaluate("document.querySelector('#write-grant').click()");
  await cdp.evaluatePlugin("document.querySelector('#new-note').click()");
  await cdp.evaluatePlugin("document.querySelector('#title').value='阶段2.4样例'");
  await cdp.evaluatePlugin("document.querySelector('#body').value='浏览器模拟数据'");
  await cdp.evaluatePlugin("document.querySelector('#note-form').requestSubmit()");
  await until(() => cdp.evaluatePlugin("document.querySelector('#status').textContent.includes('笔记已保存，列表读取失败')"), 'write succeeded with read denied');
  assert.equal(await cdp.evaluatePlugin("document.querySelector('#notes').children.length"), 0);
  await cdp.evaluate("document.querySelector('#read-grant').click()");
  await pause(50);
  await cdp.evaluatePlugin("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluatePlugin("document.querySelector('#notes').innerText.includes('阶段2.4样例')"), 'authorized note read');
  assert.match(await cdp.evaluatePlugin("document.querySelector('#notes').innerText"), /浏览器模拟数据/);
  console.log('PASS separate read/write grants and basic note interaction');
  await cdp.evaluate("document.querySelector('#read-grant').click()");
  await pause(50);
  await cdp.evaluatePlugin("document.querySelector('#refresh').click()");
  await until(() => cdp.evaluatePlugin("document.querySelector('#status').textContent.includes('PERMISSION_DENIED')"), 'revoked read');
  assert.equal(await cdp.evaluatePlugin("document.querySelector('#notes').children.length"), 0);
  assert.equal((await fetch(new URL('/plugin/../not-declared.js', info.url))).status, 404);
  console.log('PASS revocation and package-only resource routing');
  console.log('ALL STAGE 2.4 CHECKS PASSED');
} finally {
  cdp?.socket.close();
  pluginCdp?.socket.close();
  if (browser && browser.exitCode === null) { browser.kill(); await until(() => browser.exitCode !== null, 'Edge shutdown').catch(() => {}); }
  if (server && server.exitCode === null) { server.kill(); await until(() => server.exitCode !== null, 'preview server shutdown').catch(() => {}); }
  await rm(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }).catch(() => {});
}
