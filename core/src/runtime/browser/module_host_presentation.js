/* Capability-owned presentation switch for the five embedded module pages. */
(() => {
  "use strict";
  const query = new URLSearchParams(location.search);
  if (query.get("host") !== "optdesk" || window.parent === window) return;
  document.documentElement.dataset.optionhelperAppHosted = "true";
  const applyBodyMarker = () => document.body?.classList.add("optionhelper-embedded");
  applyBodyMarker();
  document.addEventListener("DOMContentLoaded", applyBodyMarker, { once: true });
})();
