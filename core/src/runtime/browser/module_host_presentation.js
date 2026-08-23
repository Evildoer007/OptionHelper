/* Capability-owned presentation switch for the five embedded module pages. */
(() => {
  "use strict";

  const query = new URLSearchParams(location.search);
  if (query.get("host") !== "optdesk" || window.parent === window) return;
  document.documentElement.dataset.optionhelperAppHosted = "true";

  const moduleScrollContainers = [
    ".library-panel",
    ".source-panel",
    ".inspector-panel",
    ".settings-panel",
    ".canvas-viewport",
    ".selection-panel",
    ".json-block",
    ".preview-table-wrap",
    ".table-wrap",
    ".audit-details pre",
    ".trade-inspector",
    ".ledger",
    "textarea",
  ];

  const initializeHostedPresentation = async () => {
    document.body?.classList.add("optionhelper-embedded");
    try {
      const { installScrollbarActivity } = await import("/app/frontend/shared/scrollbar-activity.js");
      installScrollbarActivity({ selectors: moduleScrollContainers, includeDocument: false });
    } catch (error) {
      console.warn("OptionHelper overlay scrollbars are unavailable; native scrolling remains enabled.", error);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeHostedPresentation, { once: true });
  } else {
    initializeHostedPresentation();
  }
})();
