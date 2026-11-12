export const PLUGIN_ID = 'org.example.bridge-demo';
export const API_VERSION = '1.0.0';

let sequence = 0;
const envelope = (capability, params) => ({
  id: `request_${++sequence}`, apiVersion: API_VERSION, pluginId: PLUGIN_ID, capability, params,
});
const denial = (id, code) => ({ id, ok: false, error: { code } });

export class AndroidAdapter {
  constructor(transport = globalThis.PluginHostBridge, timeoutMs = 3000) {
    if (!transport || typeof transport.postMessage !== 'function') {
      throw new Error('BRIDGE_UNAVAILABLE');
    }
    this.environment = 'android';
    this.pending = new Map();
    this.transport = transport;
    this.timeoutMs = timeoutMs;
    transport.onmessage = event => {
      let response;
      try { response = JSON.parse(event.data); } catch { return; }
      if (!response || typeof response.id !== 'string' || typeof response.ok !== 'boolean') return;
      if (response.ok ? !response.result : typeof response.error?.code !== 'string') return;
      const pending = this.pending.get(response.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(response.id);
      pending.resolve(response);
    };
  }

  request(capability, params) {
    if (this.pending.size >= 16) return Promise.reject(new Error('TOO_MANY_PENDING_REQUESTS'));
    const request = envelope(capability, params);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(request.id);
        reject(new Error('BRIDGE_TIMEOUT'));
      }, this.timeoutMs);
      this.pending.set(request.id, { resolve, timer });
      try { this.transport.postMessage(JSON.stringify(request)); }
      catch {
        clearTimeout(timer);
        this.pending.delete(request.id);
        reject(new Error('BRIDGE_UNAVAILABLE'));
      }
    });
  }
}

/** A preview simulation only: this class never invokes Android or grants native permissions. */
export class BrowserAdapter {
  constructor(onSimulatedToast = () => {}) {
    this.environment = 'browser-simulation';
    this.toastGranted = false;
    this.onSimulatedToast = onSimulatedToast;
  }

  setToastGranted(granted) { this.toastGranted = granted === true; }

  async request(capability, params) {
    const { id } = envelope(capability, params);
    if (capability !== 'ui.toast') return denial(id, 'CAPABILITY_NOT_DECLARED');
    if (!this.toastGranted) return denial(id, 'PERMISSION_DENIED');
    if (!params || Object.keys(params).length !== 1 || typeof params.text !== 'string' ||
        !params.text.trim() || params.text.length > 120 || /[\u0000-\u001f\u007f-\u009f]/u.test(params.text)) {
      return denial(id, 'INVALID_PARAMS');
    }
    this.onSimulatedToast(params.text);
    return { id, ok: true, result: { environment: this.environment, simulated: true } };
  }
}
