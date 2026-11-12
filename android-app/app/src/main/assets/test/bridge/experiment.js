import { AndroidAdapter, BrowserAdapter } from './adapters.js';

const environment = document.querySelector('#environment');
const result = document.querySelector('#result');
const isAndroid = location.origin === 'https://appassets.androidplatform.net';
let adapter;
try {
  adapter = isAndroid ? new AndroidAdapter() : new BrowserAdapter(text => {
    document.querySelector('#simulated-toast').textContent = `浏览器模拟提示：${text}`;
  });
  environment.textContent = isAndroid ? 'Android 实际宿主能力（默认未授权）' : '浏览器模拟：不调用 Android';
} catch (error) {
  environment.textContent = `Android 桥接不可用：${error.message}`;
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
}

const grant = document.querySelector('#simulate-grant');
grant.hidden = isAndroid;
grant.addEventListener('click', () => {
  adapter.setToastGranted(!adapter.toastGranted);
  grant.textContent = adapter.toastGranted ? '撤销模拟授权' : '允许模拟授权';
});

async function run(capability, params) {
  try { result.textContent = JSON.stringify(await adapter.request(capability, params), null, 2); }
  catch (error) { result.textContent = error.message; }
}
document.querySelector('#toast').addEventListener('click', () => run('ui.toast', { text: '桥接实验示例' }));
document.querySelector('#undeclared').addEventListener('click', () => run('data.write', { text: '虚构数据' }));
document.querySelector('#invalid').addEventListener('click', () => run('ui.toast', { text: 42 }));
