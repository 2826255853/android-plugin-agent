"use strict";
for (const capability of ["data.read", "data.write"]) {
  const input = document.getElementById(capability === "data.read" ? "read-grant" : "write-grant");
  input.closest("label").hidden = !PREVIEW_CONFIG.capabilities.includes(capability);
  input.addEventListener("change", () => document.querySelector("iframe").contentWindow.postMessage(
    { type: "preview-grant", capability, granted: input.checked }, "*"));
}
