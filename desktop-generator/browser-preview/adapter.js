"use strict";
(() => {
  const config = globalThis.PREVIEW_CONFIG;
  const grants = new Set();
  const storeKey = `browser-preview:${config.pluginId}:notes`;
  const collection = config.dataModel.collections.find(item => item.id === "notes");
  const fields = new Map((collection?.fields || []).map(field => [field.id, field]));
  const error = code => ({ ok: false, error: { code } });
  const bridge = { onmessage: null, postMessage(raw) {
    let request;
    try { request = JSON.parse(raw); } catch { return; }
    const reply = response => queueMicrotask(() => bridge.onmessage?.({ data: JSON.stringify({ id: request.id, ...response }) }));
    if (!request || typeof request.id !== "string" || !/^[a-z0-9_-]{1,64}$/.test(request.id) ||
        Object.keys(request).sort().join(",") !== "apiVersion,capability,id,params,pluginId") return;
    if (request.pluginId !== config.pluginId) return reply(error("PLUGIN_ID_MISMATCH"));
    if (request.apiVersion !== "1.0.0") return reply(error("UNSUPPORTED_API_VERSION"));
    if (!request || !config.capabilities.includes(request.capability)) return reply({ ok: false, error: { code: "CAPABILITY_NOT_DECLARED" } });
    if (!grants.has(request.capability)) return reply({ ok: false, error: { code: "PERMISSION_DENIED" } });
    if (request.capability === "data.read") {
      if (!request.params || Object.keys(request.params).some(key => !["collection", "limit", "after"].includes(key)) ||
          request.params.collection !== "notes" || !Number.isInteger(request.params.limit) || request.params.limit < 1 || request.params.limit > 100 ||
          typeof request.params.after !== "string" || !/^(0|[1-9][0-9]*)$/.test(request.params.after))
        return reply({ ok: false, error: { code: "INVALID_PARAMS" } });
      const records = JSON.parse(localStorage.getItem(storeKey) || "[]");
      const start = Number(request.params.after || 0);
      const page = records.slice(start, start + request.params.limit);
      return reply({ ok: true, result: { records: page, nextCursor: start + page.length < records.length ? String(start + page.length) : null } });
    }
    if (request.capability === "data.write") {
      const record = request.params?.record;
      if (!request.params || Object.keys(request.params).some(key => !["collection", "record"].includes(key)) ||
          request.params.collection !== "notes" || !record || typeof record !== "object" || Array.isArray(record) ||
          Object.keys(record).some(key => !fields.has(key)) || [...fields.values()].some(field => field.required && !(field.id in record)) ||
          [...Object.entries(record)].some(([key, value]) => {
            const field = fields.get(key);
            return field.type !== "string" || typeof value !== "string" || value.length > field.maxLength;
          }))
        return reply({ ok: false, error: { code: "INVALID_PARAMS" } });
      const records = JSON.parse(localStorage.getItem(storeKey) || "[]");
      const serialized = JSON.stringify(record);
      if (new TextEncoder().encode(serialized).length > 32 * 1024 || records.length >= 1000 ||
          new TextEncoder().encode(JSON.stringify(records)).length + new TextEncoder().encode(serialized).length > 1024 * 1024)
        return reply({ ok: false, error: { code: "QUOTA_EXCEEDED" } });
      records.push({ id: String(records.length + 1), data: record });
      localStorage.setItem(storeKey, JSON.stringify(records));
      return reply({ ok: true, result: { id: String(records.length) } });
    }
    reply({ ok: false, error: { code: "CAPABILITY_NOT_SUPPORTED_IN_BROWSER" } });
  }};
  globalThis.PluginHostBridge = bridge;
  addEventListener("message", event => {
    if (event.source !== parent || event.data?.type !== "preview-grant" || !config.capabilities.includes(event.data.capability)) return;
    if (event.data.granted === true) grants.add(event.data.capability);
    else grants.delete(event.data.capability);
  });
})();
