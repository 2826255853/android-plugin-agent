const report = result => parent.postMessage({ type: 'bridge-frame-result', result }, new URL(document.referrer).origin);
if (typeof PluginHostBridge === 'undefined') {
  report({ bridgeExposed: false });
} else {
  PluginHostBridge.onmessage = event => report({ bridgeExposed: true, response: JSON.parse(event.data) });
  PluginHostBridge.postMessage(JSON.stringify({
    id: 'iframe_request', apiVersion: '1.0.0', pluginId: 'org.example.bridge-demo',
    capability: 'ui.toast', params: { text: '不应显示的 iframe 提示' },
  }));
}
