/* Shared ModuleHostContext browser adapter for the five Capability pages. */
(() => {
  "use strict";
  const moduleName = location.pathname.split("/").filter(Boolean).at(-2);
  if (moduleName && document.documentElement?.dataset) {
    document.documentElement.dataset.optionhelperModule = moduleName;
  }
  const routes = {
    datafetcher: {"/api/status": "status", "/api/assets": "list_assets", "/api/fetch": "fetch"},
    payoffer: {"/api/catalog": "catalog", "/api/default": "default", "/api/preview": "preview", "/api/run": "run"},
    pricer: {"/api/catalog": "catalog", "/api/run": "run"},
    backtester: {"/api/catalog": "catalog", "/api/preview": "preview", "/api/run": "run"},
    reporter: {"/api/status": "status", "/api/report-sources": "list_report_sources", "/api/run": "run"},
  };
  const queryActions = new Set(["catalog", "list_assets", "list_report_sources", "status"]);
  const query = new URLSearchParams(location.search);
  const embeddedInDesk = query.get("host") === "optdesk";
  const hostedInDesk = embeddedInDesk && window.parent !== window;
  // A per-iframe nonce is more reliable than comparing WindowProxy object
  // identity in WKWebView, where that identity can change during navigation.
  // It also keeps the context hand-off scoped to the iframe the App created.
  const bridgeNonce = query.get("bridge_nonce");
  const allowedThemes = new Set(["light", "dark"]);
  const allowedPreferences = new Set(["light", "dark", "auto"]);
  const directOperationResultModules = new Set(["payoffer", "pricer", "backtester"]);

  function applyTheme(theme, preference = theme) {
    const root = document.documentElement;
    if (!root?.dataset) return;
    root.dataset.theme = allowedThemes.has(theme) ? theme : "light";
    root.dataset.themePref = allowedPreferences.has(preference) ? preference : root.dataset.theme;
  }

  function installStandaloneTheme() {
    let preference = "light";
    try {
      const stored = localStorage.getItem("oh-theme");
      if (allowedPreferences.has(stored)) preference = stored;
    } catch { /* Private previews may not expose local storage. */ }
    const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    applyTheme(preference === "auto" ? (systemDark ? "dark" : "light") : preference, preference);
  }

  installStandaloneTheme();
  let context = null;
  let hostScope = Object.freeze({});
  let contextVersion = 0;
  let acceptHostContext = null;
  let hostContextReady = new Promise((resolve) => { acceptHostContext = resolve; });
  let moduleVisibilityActive = false;
  let moduleVisibilitySeenActive = false;
  const nativeFetch = window.fetch.bind(window);

  function moduleVisibilityTransition(nextActive) {
    const active = nextActive === true;
    const reopened = active && moduleVisibilitySeenActive && !moduleVisibilityActive;
    moduleVisibilityActive = active;
    if (active) moduleVisibilitySeenActive = true;
    return {active, reopened};
  }

  function resetHostContext() {
    context = null;
    hostScope = Object.freeze({});
    hostContextReady = new Promise((resolve) => { acceptHostContext = resolve; });
  }

  function requestHostContext(reason) {
    if (!hostedInDesk) return;
    window.parent.postMessage({
      type: "optionhelper.module-host-context-request",
      module: moduleName,
      bridge_nonce: bridgeNonce,
      context_version: contextVersion || null,
      reason,
    }, location.origin);
  }

  const panelLayoutKey = "optionhelper.desk-panel-widths";
  const panelLayoutBounds = Object.freeze({
    left: {minimum: 200, maximum: 420, fallback: 250},
    right: {minimum: 300, maximum: 520, fallback: 340},
    compactLeft: 160,
    compactRight: 200,
    center: 360,
    compactCenter: 300,
  });

  function clampPanelWidth(value, minimum, maximum, fallback) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
  }

  function normalizePanelLayout(layout) {
    return {
      left: clampPanelWidth(layout?.left, panelLayoutBounds.left.minimum, panelLayoutBounds.left.maximum, panelLayoutBounds.left.fallback),
      right: clampPanelWidth(layout?.right, panelLayoutBounds.right.minimum, panelLayoutBounds.right.maximum, panelLayoutBounds.right.fallback),
    };
  }

  function readPanelLayout(workbench) {
    const style = getComputedStyle(workbench);
    const left = style.getPropertyValue("--library-width") || style.getPropertyValue("--source-width");
    const right = style.getPropertyValue("--inspector-width") || style.getPropertyValue("--settings-width");
    return normalizePanelLayout({left, right});
  }

  function effectivePanelLayout(workbench, layout) {
    const preferred = normalizePanelLayout(layout);
    const width = Math.max(0, workbench.clientWidth || document.documentElement.clientWidth || window.innerWidth || 0);
    if (!width) return preferred;
    const compact = width < 980;
    const minimumLeft = compact ? panelLayoutBounds.compactLeft : panelLayoutBounds.left.minimum;
    const minimumRight = compact ? panelLayoutBounds.compactRight : panelLayoutBounds.right.minimum;
    const centerWidth = compact ? panelLayoutBounds.compactCenter : panelLayoutBounds.center;
    const sideBudget = Math.max(minimumLeft + minimumRight, width - centerWidth);
    let left = preferred.left;
    let right = preferred.right;
    const excess = left + right - sideBudget;
    if (excess > 0) {
      const leftRoom = Math.max(0, left - minimumLeft);
      const rightRoom = Math.max(0, right - minimumRight);
      const room = leftRoom + rightRoom;
      if (room > 0) {
        left -= excess * (leftRoom / room);
        right -= excess * (rightRoom / room);
      }
      if (left + right > sideBudget) {
        const ratio = sideBudget / (minimumLeft + minimumRight);
        left = minimumLeft * ratio;
        right = minimumRight * ratio;
      }
    }
    return {left: Math.floor(left), right: Math.floor(right)};
  }

  function applyPanelLayout(workbench, layout) {
    const {left, right} = effectivePanelLayout(workbench, layout);
    workbench.style.setProperty("--library-width", `${left}px`);
    workbench.style.setProperty("--source-width", `${left}px`);
    workbench.style.setProperty("--inspector-width", `${right}px`);
    workbench.style.setProperty("--settings-width", `${right}px`);
  }

  function storedPanelLayout() {
    try { return JSON.parse(localStorage.getItem(panelLayoutKey) || "null"); }
    catch { return null; }
  }

  function persistPanelLayout(workbench) {
    const layout = normalizePanelLayout(readPanelLayout(workbench));
    try { localStorage.setItem(panelLayoutKey, JSON.stringify(layout)); }
    catch { /* Storage may be unavailable in a standalone private preview. */ }
    window.parent.postMessage({
      type: "optionhelper.desk-panel-layout-change",
      module: moduleName,
      bridge_nonce: bridgeNonce,
      layout,
    }, location.origin);
  }

  function installSharedPanelLayout() {
    if (!hostedInDesk) return;
    const workbench = document.querySelector(".workbench");
    if (!workbench || workbench.dataset.optionhelperSharedLayout === "true") return;
    workbench.dataset.optionhelperSharedLayout = "true";
    let preferredLayout = normalizePanelLayout(storedPanelLayout() || readPanelLayout(workbench));
    applyPanelLayout(workbench, preferredLayout);
    let committedLayout = JSON.stringify(preferredLayout);
    let pendingCommit = 0;
    const commitCurrentLayout = () => {
      pendingCommit = 0;
      const nextLayout = normalizePanelLayout(readPanelLayout(workbench));
      const serialized = JSON.stringify(nextLayout);
      if (serialized === committedLayout) return;
      preferredLayout = nextLayout;
      committedLayout = serialized;
      persistPanelLayout(workbench);
    };
    const scheduleCommit = () => {
      if (pendingCommit) return;
      pendingCommit = requestAnimationFrame(commitCurrentLayout);
    };
    const commit = (event) => {
      if (!event.target?.closest?.(".panel-resizer")) return;
      scheduleCommit();
    };
    document.addEventListener("pointerup", commit, true);
    document.addEventListener("keyup", commit, true);
    new ResizeObserver(() => applyPanelLayout(workbench, preferredLayout)).observe(workbench);
    window.addEventListener("storage", (event) => {
      if (event.key !== panelLayoutKey || !event.newValue) return;
      try {
        preferredLayout = normalizePanelLayout(JSON.parse(event.newValue));
        committedLayout = JSON.stringify(preferredLayout);
        applyPanelLayout(workbench, preferredLayout);
      }
      catch { /* Ignore malformed external storage events. */ }
    });
    window.addEventListener("message", (event) => {
      if (event.origin !== location.origin || event.source !== window.parent) return;
      if (event.data?.type !== "optionhelper.desk-panel-layout") return;
      if (event.data?.bridge_nonce && event.data.bridge_nonce !== bridgeNonce) return;
      preferredLayout = normalizePanelLayout(event.data?.layout);
      committedLayout = JSON.stringify(preferredLayout);
      applyPanelLayout(workbench, preferredLayout);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installSharedPanelLayout, {once: true});
  else installSharedPanelLayout();

  function optionFor(select, value) {
    return Array.from(select.options).find((option) => option.value === value) || select.selectedOptions[0] || select.options[0];
  }

function renderChoiceLabel(node, option) {
  node.replaceChildren();
  const label = node.ownerDocument.createElement("span");
  label.className = "choice-label";
  label.textContent = option?.textContent || "未选择";
  node.append(label);
  if (option?.dataset.english) {
    const english = node.ownerDocument.createElement("span");
    english.className = "choice-english";
    english.lang = "en";
    english.textContent = option.dataset.english;
    node.append(english);
  }
}


  function updateChoice(select) {
    const choice = select.closest("[data-oh-choice]");
    if (!choice) return;
    const trigger = choice.querySelector(".oh-choice__trigger");
    const value = choice.querySelector(".oh-choice__value");
    const menu = choice.querySelector(".oh-choice__menu");
    const focusedValue = menu.contains(document.activeElement) ? document.activeElement.dataset.value : null;
    const selected = optionFor(select, select.value);
    renderChoiceLabel(value, selected);
    trigger.disabled = select.disabled || !select.options.length;
    trigger.setAttribute("aria-disabled", String(trigger.disabled));
    if (select.getAttribute("aria-invalid") === "true") trigger.setAttribute("aria-invalid", "true");
    else trigger.removeAttribute("aria-invalid");
    const describedBy = select.getAttribute("aria-describedby");
    if (describedBy) trigger.setAttribute("aria-describedby", describedBy);
    else trigger.removeAttribute("aria-describedby");
    menu.replaceChildren(...Array.from(select.options).map((option) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "oh-choice__option";
      item.dataset.value = option.value;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(option.selected));
      item.disabled = select.disabled || option.disabled || Boolean(option.closest("optgroup")?.disabled);
      item.tabIndex = -1;
      renderChoiceLabel(item, option);
      item.addEventListener("click", (event) => {
        // Prevent a wrapping label from forwarding activation after a change
        // handler has replaced the selected option or its entire input group.
        event.preventDefault();
        event.stopPropagation();
        if (item.disabled || select.disabled) return;
        const changed = select.value !== option.value;
        setChoiceOpen(choice, false);
        select.value = option.value;
        if (changed) select.dispatchEvent(new Event("change", {bubbles: true}));
        if (trigger.isConnected && !select.disabled) trigger.focus({preventScroll: true});
      });
      return item;
    }));
    if (trigger.disabled) setChoiceOpen(choice, false);
    else if (focusedValue !== null && choice.dataset.open === "true") {
      const options = Array.from(menu.querySelectorAll('[role="option"]:not([disabled])'));
      (options.find((item) => item.dataset.value === focusedValue) || options.find((item) => item.dataset.value === select.value) || trigger).focus({preventScroll: true});
    }
  }

  function positionChoiceMenu(choice) {
    const trigger = choice.querySelector(".oh-choice__trigger");
    const menu = choice.querySelector(".oh-choice__menu");
    const rect = trigger.getBoundingClientRect();
    let top = 0;
    let bottom = window.innerHeight;
    for (let parent = choice.parentElement; parent; parent = parent.parentElement) {
      if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
      const bounds = parent.getBoundingClientRect();
      top = Math.max(top, bounds.top);
      bottom = Math.min(bottom, bounds.bottom);
    }
    const below = Math.max(0, bottom - rect.bottom - 6);
    const above = Math.max(0, rect.top - top - 6);
    const upward = below < Math.min(menu.scrollHeight || 160, 160) && above > below;
    choice.dataset.direction = upward ? "up" : "down";
    menu.style.maxHeight = `${Math.min(280, window.innerHeight * .42, upward ? above : below)}px`;
  }

  function setChoiceOpen(choice, open, focus = false) {
    const trigger = choice.querySelector(".oh-choice__trigger");
    const menu = choice.querySelector(".oh-choice__menu");
    if (trigger.disabled && open) return;
    if (open) document.querySelectorAll('[data-oh-choice][data-open="true"]').forEach((other) => { if (other !== choice) setChoiceOpen(other, false); });
    choice.dataset.open = String(open);
    trigger.setAttribute("aria-expanded", String(open));
    menu.hidden = !open;
    if (open) positionChoiceMenu(choice);
    if (open && focus) (menu.querySelector('[role="option"][aria-selected="true"]:not([disabled])') || menu.querySelector('[role="option"]:not([disabled])'))?.focus();
  }

  function closeChoiceControls() {
    document.querySelectorAll('[data-oh-choice][data-open="true"]').forEach((choice) => setChoiceOpen(choice, false));
  }

  function moveChoice(select, direction) {
    const choices = Array.from(select.options).filter((option) => !option.disabled && !option.closest("optgroup")?.disabled);
    const current = Math.max(0, choices.findIndex((option) => option.value === select.value));
    const next = choices[Math.max(0, Math.min(choices.length - 1, current + direction))];
    if (!next || next.value === select.value) return;
    select.value = next.value;
    select.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function enhanceSelect(select) {
    // Discovery may see the same select again after this function moves it
    // into the product-owned control.  Synchronisation is handled by the
    // select's own change/attribute observer; rebuilding the menu here would
    // mutate the document and re-trigger the discovery observer indefinitely.
    if (select.closest("[data-oh-choice]")) return;
    const explicitLabel = select.id
      ? Array.from(document.querySelectorAll("label[for]")).find((candidate) => candidate.htmlFor === select.id)
      : null;
    const label = select.getAttribute("aria-label") || explicitLabel?.textContent?.trim() || select.closest("label")?.textContent?.trim() || "选择选项";
    const id = `oh-choice-${globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
    const choice = document.createElement("span");
    choice.className = "oh-choice";
    choice.dataset.ohChoice = "true";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.id = `${id}-trigger`;
    trigger.className = "oh-choice__trigger";
    trigger.setAttribute("aria-label", label);
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", id);
    trigger.innerHTML = '<span class="oh-choice__value"></span><span class="oh-choice__chevron" aria-hidden="true"></span>';
    const menu = document.createElement("span");
    menu.id = id;
    menu.className = "oh-choice__menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", label);
    menu.hidden = true;
    select.classList.add("oh-choice__native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.after(choice);
    choice.append(select, trigger, menu);
    const explicitLabels = Array.from(document.querySelectorAll("label[for]")).filter((label) => label.htmlFor === select.id);
    explicitLabels.forEach((label) => { label.htmlFor = trigger.id; });
    const wrappingLabel = choice.closest("label");
    if (wrappingLabel && !wrappingLabel.htmlFor) {
      wrappingLabel.addEventListener("click", (event) => {
        // Composite fields pair an editable amount with a unit choice. Only
        // delegate label text; sibling inputs must retain native focus/editing.
        if (event.target.closest('[data-oh-choice], input, select, textarea, button, a[href], [contenteditable="true"]')) return;
        event.preventDefault();
        trigger.focus();
      });
    }
    trigger.addEventListener("click", () => setChoiceOpen(choice, choice.dataset.open !== "true", choice.dataset.open !== "true"));
    trigger.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "Home", "End", " ", "Enter", "Escape"].includes(event.key)) event.stopPropagation();
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (choice.dataset.open === "true") moveChoice(select, event.key === "ArrowDown" ? 1 : -1);
        else setChoiceOpen(choice, true, true);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const options = Array.from(select.options).filter((option) => !option.disabled && !option.closest("optgroup")?.disabled);
        const next = options[event.key === "Home" ? 0 : options.length - 1];
        if (next && next.value !== select.value) { select.value = next.value; select.dispatchEvent(new Event("change", {bubbles: true})); }
      } else if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        if (event.repeat) return;
        setChoiceOpen(choice, choice.dataset.open !== "true", choice.dataset.open !== "true");
      } else if (event.key === "Escape") { event.preventDefault(); setChoiceOpen(choice, false); }
    });
    menu.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "Home", "End", " ", "Enter", "Escape"].includes(event.key)) event.stopPropagation();
      const options = Array.from(menu.querySelectorAll('[role="option"]:not([disabled])'));
      const index = options.indexOf(document.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        (options[Math.max(0, Math.min(options.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))] || options[0])?.focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        options[event.key === "Home" ? 0 : options.length - 1]?.focus();
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (event.repeat) return;
        document.activeElement?.click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        setChoiceOpen(choice, false);
        trigger.focus();
      } else if (event.key === "Tab") {
        setChoiceOpen(choice, false);
        trigger.focus({preventScroll: true});
      }
    });
    select.addEventListener("input", () => updateChoice(select));
    select.addEventListener("change", () => updateChoice(select));
    new MutationObserver(() => updateChoice(select)).observe(select, {childList: true, subtree: true, attributes: true, attributeFilter: ["aria-describedby", "aria-invalid", "disabled", "selected", "label"]});
    updateChoice(select);
  }

  function installChoiceControls() {
    if (document.getElementById("optionhelper-choice-controls")) return;
    const style = document.createElement("style");
    style.id = "optionhelper-choice-controls";
    style.textContent = `
      .oh-choice { position: relative; display: block; min-width: 0; }
      body.optionhelper-embedded .topbar { display: none !important; }
      .oh-choice__native { position: absolute !important; width: 1px !important; height: 1px !important; min-height: 1px !important; margin: -1px !important; padding: 0 !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; opacity: 0 !important; pointer-events: none !important; }
      .oh-choice__trigger { display: flex; width: 100%; min-height: 38px; align-items: center; justify-content: space-between; gap: 10px; padding: 0 11px; border: 1px solid var(--color-rule, #e2e0dc); border-radius: 8px; background: var(--color-surface, #fff); color: var(--color-ink, #252628); font: inherit; line-height: 1.3; text-align: left; transition: border-color .16s ease, background .16s ease, box-shadow .16s ease, color .16s ease; }
      .oh-choice__value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .oh-choice__chevron { width: 8px; height: 8px; flex: 0 0 8px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; color: var(--color-muted, #6b7075); transform: rotate(45deg) translate(-2px, -2px); transition: transform .16s ease; }
      .oh-choice:hover .oh-choice__trigger, .oh-choice[data-open="true"] .oh-choice__trigger { border-color: var(--color-rule-strong, #c9c5bf); background: var(--color-ground, #f4f4f2); }
      .oh-choice[data-open="true"] .oh-choice__trigger { box-shadow: 0 0 0 3px var(--color-ground, #f4f4f2); color: var(--color-ink, #252628); }
      .oh-choice[data-open="true"] .oh-choice__chevron { transform: rotate(225deg) translate(-1px, -1px); }
      .oh-choice__trigger:focus-visible { outline: 3px solid var(--color-blue-gray, #49647d); outline-offset: 2px; }
      .oh-choice__trigger:disabled { background: var(--color-ground, #f4f2f2); color: var(--color-muted-soft, #978e90); cursor: not-allowed; }
      .oh-choice__native[aria-invalid="true"] ~ .oh-choice__trigger { border-color: var(--color-brand-red, #c8102e); background: var(--color-ground, #f4f4f2); box-shadow: 0 0 0 3px var(--color-ground, #f4f4f2); }
      .oh-choice__menu { position: absolute; z-index: 80; top: calc(100% + 6px); right: 0; left: 0; box-sizing: border-box; max-height: min(280px, 42vh); padding: 5px; overflow: auto; border: 1px solid var(--color-rule, #e2e0dc); border-radius: 9px; background: var(--color-surface, #fff); box-shadow: 0 14px 28px var(--color-surface-shadow, rgb(44 53 62 / .08)); }
      .oh-choice[data-direction="up"] .oh-choice__menu { top: auto; bottom: calc(100% + 6px); }
      .oh-choice__option { display: flex; width: 100%; min-height: 32px; align-items: center; padding: 7px 9px; border: 0; border-radius: 6px; background: transparent; color: var(--color-ink, #252628); font: inherit; font-size: 12px; line-height: 1.35; text-align: left; }
      .oh-choice__option:hover, .oh-choice__option:focus-visible { outline: 0; background: var(--color-ground, #f4f4f2); color: var(--color-ink, #252628); }
      .oh-choice__option[aria-selected="true"] { background: var(--color-ground, #f4f4f2); color: var(--color-ink, #252628); font-weight: 700; }
      .oh-choice__option[aria-selected="true"]::after { width: 5px; height: 9px; margin-left: auto; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; content: ""; transform: rotate(45deg) translate(-2px,-1px); }
      .oh-choice__option:disabled { color: var(--color-muted-soft, #a09698); cursor: not-allowed; }
    `;
    document.head.append(style);
    document.querySelectorAll("select").forEach(enhanceSelect);
    // Only inspect newly inserted subtrees.  A document-wide rescan also sees
    // the option buttons created by updateChoice(), creating a self-sustaining
    // MutationObserver -> replaceChildren() loop in embedded module pages.
    new MutationObserver((records) => records.forEach((record) => {
      record.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        if (node.matches("select")) enhanceSelect(node);
        node.querySelectorAll("select").forEach(enhanceSelect);
      });
    })).observe(document.documentElement, {childList: true, subtree: true});
    document.addEventListener("pointerdown", (event) => document.querySelectorAll('[data-oh-choice][data-open="true"]').forEach((choice) => { if (!choice.contains(event.target)) setChoiceOpen(choice, false); }));
    document.addEventListener("focusin", (event) => document.querySelectorAll('[data-oh-choice][data-open="true"]').forEach((choice) => { if (!choice.contains(event.target)) setChoiceOpen(choice, false); }));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installChoiceControls, {once: true});
  else installChoiceControls();

  function valid(value) {
    const token = /^oh\.([0-9]{1,12})\.([0-9a-f]{64})$/.exec(String(value?.capability_token || ""));
    const expiresAt = Number(token?.[1]);
    return value && value.module === moduleName && value.host_kind && value.context_id && value.capability_token
      && typeof value.page_hash === "string" && /^[0-9a-f]{64}$/.test(value.page_hash)
      && ["task_id", "analysis_case_id", "candidate_id", "catalog_version", "product_id"].every((field) => value[field] === null || typeof value[field] === "string")
      && (value.rule_revision === null || (Number.isSafeInteger(value.rule_revision) && value.rule_revision > 0))
      && Number.isSafeInteger(expiresAt) && expiresAt * 1000 > Date.now();
  }
  function activeHostContext() {
    if (!context) return null;
    const token = /^oh\.([0-9]{1,12})\.[0-9a-f]{64}$/.exec(String(context.capability_token || ""));
    const expiresAt = Number(token?.[1]);
    // A host context is validated before the Gateway dispatches the request.
    // Renew it before it becomes marginal instead of letting an idle Desk page
    // submit one known-expired request after its five-minute lease ends.
    if (!Number.isSafeInteger(expiresAt) || expiresAt * 1000 <= Date.now() + 15_000) {
      resetHostContext();
      return null;
    }
    return context;
  }
  function requestId() { return crypto.randomUUID ? crypto.randomUUID() : `req_${Date.now()}_${Math.random().toString(36).slice(2)}`; }
  function asResponse(status, body) {
    return new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  }
  function pageResultEnvelope(response, envelope) {
    const hasResult = envelope
      && typeof envelope === "object"
      && !Array.isArray(envelope)
      && Object.prototype.hasOwnProperty.call(envelope, "result");
    const result = hasResult ? envelope.result : envelope;
    if (
      !response.ok
      || !result
      || typeof result !== "object"
      || Array.isArray(result)
      || Object.prototype.hasOwnProperty.call(result, "ok")
    ) return result;
    return {ok: true, ...result};
  }
  function asParsedResponse(status, body) {
    const create = () => {
      const response = new Response(null, {status, headers: {"Content-Type": "application/json"}});
      Object.defineProperty(response, "json", {value: async () => body});
      Object.defineProperty(response, "clone", {value: create});
      return response;
    };
    return create();
  }
  function abortError() {
    return new DOMException("Module Host request was aborted", "AbortError");
  }
  function wait(delay, signal) {
    if (signal?.aborted) return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        signal?.removeEventListener("abort", abort);
        resolve();
      }, delay);
      const abort = () => {
        window.clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        reject(abortError());
      };
      signal?.addEventListener("abort", abort, {once: true});
    });
  }
  function retryDelay(failures) {
    return Math.min(4_000, 500 * (2 ** Math.min(Math.max(0, failures - 1), 3)));
  }
  function operationConnectionState(state, operationId, message = "", metadata = {}) {
    window.dispatchEvent(new CustomEvent("optionhelper.module-operation-connection", {
      detail: {state, operation_id: operationId, module: moduleName, message, ...metadata},
    }));
  }
  function isRecoverableResponse(response) {
    return response.status === 408 || response.status === 425 || response.status === 429 || response.status >= 500;
  }
  function waitForHostContext(signal) {
    const current = activeHostContext();
    if (current) return Promise.resolve(current);
    if (signal?.aborted) throw abortError();
    requestHostContext("fetch");
    return new Promise((resolve, reject) => {
      let settled = false;
      let retries = 0;
      let timer = 0;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        resolve(value);
      };
      const abort = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        reject(abortError());
      };
      const requestAgain = () => {
        if (settled) return;
        requestHostContext(retries === 0 ? "timeout" : "recovery");
        retries += 1;
        timer = window.setTimeout(requestAgain, retryDelay(retries));
      };
      timer = window.setTimeout(requestAgain, 1000);
      signal?.addEventListener("abort", abort, {once: true});
      hostContextReady.then(finish);
    });
  }
  function applyHostScope(body) {
    for (const field of ["task_id", "analysis_case_id", "candidate_id", "catalog_version"]) {
      const bound = hostScope[field];
      if (bound == null) delete body[field];
      else body[field] = bound;
    }
    return null;
  }
  function requestedMethod(input, init) {
    return String(init.method || (typeof Request !== "undefined" && input instanceof Request ? input.method : "GET")).toUpperCase();
  }
  function requestedSignal(input, init) {
    return init.signal || (typeof Request !== "undefined" && input instanceof Request ? input.signal : undefined);
  }
  function requestedOperationId(input, init) {
    const supplied = init.headers || (typeof Request !== "undefined" && input instanceof Request ? input.headers : null);
    let value = null;
    if (supplied && typeof supplied.get === "function") value = supplied.get("X-OptionHelper-Request-Id");
    else if (Array.isArray(supplied)) {
      value = supplied.find(([name]) => String(name).toLowerCase() === "x-optionhelper-request-id")?.[1] || null;
    } else if (supplied && typeof supplied === "object") {
      value = supplied["X-OptionHelper-Request-Id"] || supplied["x-optionhelper-request-id"] || null;
    }
    return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(String(value || "")) ? String(value) : "";
  }
  function dataAssetDownloadId(url) {
    if (moduleName !== "datafetcher" || url.origin !== location.origin) return undefined;
    const match = url.pathname.match(/^\/api\/assets\/([^/]+)\/download$/);
    if (!match) return undefined;
    if (url.search || url.hash) return null;
    try {
      const value = decodeURIComponent(match[1]);
      return /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value) ? value : null;
    } catch {
      return null;
    }
  }
  async function hostedDataAssetDownload(url, signal) {
    const hostContext = hostedInDesk ? await waitForHostContext(signal) : activeHostContext();
    if (!hostContext && hostedInDesk) {
      return asResponse(503, {
        ok: false,
        error: "module_context_timeout",
        failure_code: "module_context_timeout",
        stage: "module_host",
        message: "模块页面尚未收到下载上下文，请稍后重试。",
        next_step: "请保持当前OptDesk页面打开；上下文将自动重新同步。",
      });
    }
    if (!hostContext) return nativeFetch(url, {signal});
    return nativeFetch(url.pathname, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-OptionHelper-Module-Context": JSON.stringify(hostContext),
        "X-OptionHelper-Request-Id": requestId(),
      },
      body: "{}",
      signal,
    });
  }
  async function hostedFetch(input, init = {}) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    let action = routes[moduleName]?.[url.pathname];
    const method = requestedMethod(input, init);
    const signal = requestedSignal(input, init);
    const downloadId = dataAssetDownloadId(url);
    if (downloadId !== undefined) {
      if (method !== "GET") return asResponse(405, {ok: false, message: "数据下载只接受GET请求。"});
      if (downloadId === null) return asResponse(400, {ok: false, message: "DataAsset标识非法。"});
      return hostedDataAssetDownload(url, signal);
    }
    const validMethod = queryActions.has(action) ? method === "GET" : method === "POST";
    if (!action || url.origin !== location.origin) return nativeFetch(input, init);
    if (!validMethod) return asResponse(405, {ok: false, message: "Module Host请求方法不支持。"});
    // Module pages load before their parent can post the signed context.  If
    // their initial catalog fetch falls through to the App root, it becomes a
    // GET /api/catalog request and fails before the bridge is installed.
    const hostContext = hostedInDesk ? await waitForHostContext(signal) : activeHostContext();
    if (!hostContext && hostedInDesk) {
      return asResponse(503, {
        ok: false,
        error: "module_context_timeout",
        failure_code: "module_context_timeout",
        stage: "module_host",
        message: "模块页面尚未收到运行上下文，请稍后重试。",
        next_step: "请保持当前OptDesk页面打开；上下文将自动重新同步。",
      });
    }
    if (!hostContext) return nativeFetch(input, init);
    let body = {};
    const rawBody = init.body ?? (typeof Request !== "undefined" && input instanceof Request && method !== "GET" ? await input.clone().text() : undefined);
    if (rawBody) {
      try { body = JSON.parse(rawBody); } catch { return asResponse(400, {ok: false, message: "Module Host只接受JSON请求"}); }
    }
    // Reporter keeps its standalone ``derive`` request so the page remains
    // usable outside OptDesk.  In Desk that request must become a distinct
    // App operation: a PDF is rendered from a saved ReportRun, not submitted
    // as a new report selection.
    if (moduleName === "reporter" && action === "run" && body.derive && typeof body.derive === "object") {
      const derive = body.derive;
      const source = derive.source;
      if (!source || typeof source !== "object") {
        return asResponse(400, {ok: false, message: "PDF交付缺少已保存报告来源。"});
      }
      body = {
        source_report_run_id: source.report_run_id,
        report_run_id: derive.report_run_id,
        format: derive.format,
      };
      action = "rerender";
    }
    body.action = action;
    if (action === "list_report_sources") {
      const requestedTask = url.searchParams.get("task_id");
      if (requestedTask != null) body.task_id = requestedTask;
      body.query = url.searchParams.get("q") || undefined;
    }
    const scopeError = applyHostScope(body);
    if (scopeError) return asResponse(400, {ok: false, message: scopeError});
    if (action === "fetch" || action === "run" || action === "rerender") {
      return hostedBackgroundOperation(action, body, signal, hostContext, requestedOperationId(input, init));
    }
    const response = await nativeFetch(`/api/tools/${encodeURIComponent(moduleName)}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-OptionHelper-Module-Context": JSON.stringify(hostContext),
        "X-OptionHelper-Request-Id": requestId(),
      },
      body: JSON.stringify(body),
      signal,
    });
    const envelope = await response.json();
    const result = pageResultEnvelope(response, envelope);
    if (moduleName === "payoffer" && action === "run" && !result.destination && result.module_run_ref?.run_id) {
      result.destination = "结果已保存到当前研究任务。";
    }
    if (
      response.ok
      && action === "run"
      && (moduleName === "payoffer" || moduleName === "pricer" || moduleName === "backtester")
      && (result.resolved_contract?.identity?.product_id || result.module_run_ref?.run_id)
    ) {
      resetHostContext();
      requestHostContext("run-completed");
    }
    return asResponse(response.status, result);
  }

  async function hostedBackgroundOperation(action, body, signal, hostContext, requestedId = "") {
    const taskId = String(hostContext?.task_id || "");
    if (!taskId) return asResponse(400, {ok: false, message: "当前模块没有可运行的任务。"});
    const stableRequestId = requestedId || requestId();
    const headers = {
      "Content-Type": "application/json",
      "X-OptionHelper-Module-Context": JSON.stringify(hostContext),
      "X-OptionHelper-Request-Id": stableRequestId,
    };
    const submission = await submitHostedOperation(taskId, action, body, headers, signal, stableRequestId);
    const {response: submitted, envelope} = submission;
    if (!submitted.ok) return asResponse(submitted.status, envelope);
    const operationId = envelope.operation?.operation_id;
    if (!operationId) {
      return asResponse(500, {ok: false, message: "后台运行未返回操作标识。"});
    }
    return readHostedOperation(action, body, signal, hostContext, operationId, headers);
  }

  async function readHostedOperation(action, body, signal, hostContext, operationId, headers) {
    const taskId = String(hostContext.task_id);
    const directResult = directOperationResultModules.has(moduleName);
    operationConnectionState("submitted", operationId, "正在读取后台任务状态。");
    let completed;
    let cancelledByCaller = false;
    let cancelController = null;
    let cancelRequest = null;
    try {
      completed = await waitForHostedOperation(taskId, operationId, signal, {includeResult: !directResult});
    } catch (error) {
      if (error?.name !== "AbortError") throw error;
      cancelledByCaller = true;
      operationConnectionState("cancelling", operationId, "正在等待后台确认取消。");
      cancelController = new AbortController();
      cancelRequest = cancelHostedOperation(taskId, operationId, headers, cancelController.signal);
      try {
        completed = await waitForHostedOperation(taskId, operationId, undefined, {includeResult: !directResult});
      } finally {
        cancelController.abort();
        await cancelRequest.catch(() => {});
      }
    }
    if (!completed.ok) return completed;
    if (directResult) {
      // Once the operation is terminal, its result is authoritative. A late
      // local abort must not turn an already-succeeded operation into a
      // client-side cancellation.
      const outcome = await fetchHostedOperationResult(taskId, operationId, undefined);
      if (!outcome.response.ok) return outcome.response;
      return finalizeHostedResult(action, body, outcome.result, outcome.response, hostContext);
    }
    const result = await completed.clone().json().catch(() => ({}));
    return finalizeHostedResult(action, body, result, completed, hostContext);
  }


  // Recovery reads task-owned operation records. It never resubmits a calculation.
  async function restoreModulePage({busy, render, resume, notice}) {
    if (!hostedInDesk || !["pricer", "backtester"].includes(moduleName)) return;
    busy(true);
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const hostContext = await waitForHostContext(controller.signal);
      const taskId = hostContext?.task_id;
      if (!taskId) return;
      const response = await nativeFetch(`/api/tasks/${encodeURIComponent(taskId)}/operations?include_result=false`, {credentials: "same-origin", signal: controller.signal});
      if (!response.ok) throw new Error("运行记录暂未读取，请重新打开当前任务重试。");
      const envelope = await response.json();
      const operations = (envelope.operations || []).filter(item => item.module === moduleName && item.kind === "run" && item.task_id === taskId)
        .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
      const latest = operations[0];
      if (!latest) return;
      const previous = operations.find(item => item.state === "succeeded");
      const active = ["queued", "running", "recovering", "cancelling"].includes(latest.state);
      if (previous) {
        try {
          const outcome = await fetchHostedOperationResult(taskId, previous.operation_id, controller.signal, {retry: false});
          if (!outcome.response.ok || !outcome.result?.ok) throw new Error("已保存结果暂未读取，请重新打开当前任务重试。");
          render(outcome.result);
        } catch (error) {
          // A historical result is optional while a newer operation is active.
          // Its failure must not prevent progress tracking or cancellation.
          if (!active) throw error;
          notice("历史结果暂未恢复", "继续跟踪当前运行；完成后将展示本次结果。");
        }
      }
      if (active) {
        window.clearTimeout(timer);
        busy(false);
        await resume(latest.operation_id);
      } else if (latest.state !== "succeeded") {
        notice(latest.state === "cancelled" ? "上次运行已取消" : "上次运行未完成", `${latest.message || "请检查输入后重试。"}${previous ? " 下方保留上一次完成结果。" : ""}`);
      }
    } catch (error) {
      notice("结果恢复未完成", error.name === "AbortError" ? "读取已保存结果超时，请重新打开当前任务重试。" : error.message || "请重新打开当前任务重试。");
    } finally {
      window.clearTimeout(timer);
      busy(false);
    }
  }

  window.OptionHelperModuleRecovery = Object.freeze({
    restore: restoreModulePage,
    async resume(operationId, signal) {
      const hostContext = await waitForHostContext(signal);
      if (!hostContext?.task_id) return asResponse(400, {ok: false, message: "当前模块没有可恢复的任务。"});
      const headers = {"Content-Type": "application/json", "X-OptionHelper-Module-Context": JSON.stringify(hostContext), "X-OptionHelper-Request-Id": requestId()};
      return readHostedOperation("run", {}, signal, hostContext, operationId, headers);
    },
  });

  async function cancelHostedOperation(taskId, operationId, headers, signal) {
    let failures = 0;
    while (!signal?.aborted) {
      try {
        const response = await nativeFetch(
          `/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}/cancel`,
          {method: "POST", credentials: "same-origin", headers, body: "{}", signal},
        );
        if (response.ok || response.status === 409) return;
        if (!isRecoverableResponse(response)) {
          operationConnectionState("cancel-pending", operationId, "取消请求未被接受，正在核对后台任务状态。");
          return;
        }
      } catch (error) {
        if (signal?.aborted || error?.name === "AbortError") return;
      }
      failures += 1;
      operationConnectionState("cancel-pending", operationId, "取消请求将在连接恢复后自动重试。");
      try {
        await wait(retryDelay(failures), signal);
      } catch (error) {
        if (signal?.aborted || error?.name === "AbortError") return;
        throw error;
      }
    }
  }

  async function submitHostedOperation(taskId, action, body, headers, signal, recoveryId) {
    let failures = 0;
    while (true) {
      if (signal?.aborted) throw abortError();
      try {
        const response = await nativeFetch(`/api/tasks/${encodeURIComponent(taskId)}/operations`, {
          method: "POST", credentials: "same-origin", headers,
          body: JSON.stringify({module: moduleName, action, payload: body}),
        });
        if (!response.ok && isRecoverableResponse(response)) throw new Error(`Operation submission unavailable: ${response.status}`);
        const envelope = await response.json();
        if (failures) operationConnectionState("connected", "", "提交连接已恢复。", {request_id: recoveryId});
        return {response, envelope};
      } catch (error) {
        if (signal?.aborted || error?.name === "AbortError") throw error;
        failures += 1;
        operationConnectionState("recovering", "", "正在恢复任务提交连接。", {request_id: recoveryId});
        await wait(retryDelay(failures), signal);
      }
    }
  }

  function finalizeHostedResult(action, body, result, response = null, requestContext = context) {
    if (moduleName === "payoffer" && action === "run" && !result.destination && result.module_run_ref?.run_id) {
      result.destination = "结果已保存到当前研究任务。";
    }
    if (
      action === "run"
      && (moduleName === "payoffer" || moduleName === "pricer" || moduleName === "backtester")
      && result.module_run_ref?.run_id
    ) {
      resetHostContext();
      requestHostContext("run-completed");
    }
    return response || asResponse(200, result);
  }

  async function waitForHostedOperation(taskId, operationId, signal, {includeResult = true} = {}) {
    let failures = 0;
    let computeRecovering = false;
    let lastStatusSignature = "";
    while (true) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      try {
        const suffix = includeResult ? "" : "?include_result=false";
        const response = await nativeFetch(
          `/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}${suffix}`,
          {credentials: "same-origin", signal},
        );
        if (!response.ok && isRecoverableResponse(response)) throw new Error(`Operation status unavailable: ${response.status}`);
        const envelope = await response.json();
        if (!response.ok) return asResponse(response.status, envelope);
        if (failures) operationConnectionState("connected", operationId);
        failures = 0;
        const operation = envelope.operation || {};
        const progress = typeof operation.progress === "number" ? operation.progress : Number.NaN;
        const metadata = {
          stage: typeof operation.stage === "string" ? operation.stage : "",
          ...(Number.isFinite(progress) ? {progress} : {}),
        };
        const statusSignature = JSON.stringify([operation.state || "queued", metadata.stage, metadata.progress ?? null, operation.message || ""]);
        if (statusSignature !== lastStatusSignature) {
          lastStatusSignature = statusSignature;
          operationConnectionState(operation.state || "queued", operationId, operation.message || "", metadata);
        }
        if (operation.state === "succeeded") return asResponse(200, operation.result || {});
        if (operation.state === "recovering") {
          computeRecovering = true;
          operationConnectionState(
            "compute-recovering",
            operationId,
            operation.message || "计算仍在后台运行，正在恢复计算服务。",
          );
          await wait(750, signal);
          continue;
        }
        if (computeRecovering && operation.state === "running") {
          computeRecovering = false;
          operationConnectionState(
            "compute-running",
            operationId,
            operation.message || "计算服务已恢复，正在继续运行。",
          );
        }
        if (operation.state === "failed" || operation.state === "cancelled" || operation.state === "interrupted") {
          if (!includeResult) {
            const outcome = await fetchHostedOperationResult(taskId, operationId, signal);
            return outcome.response;
          }
          return asResponse(operation.state === "failed" ? 500 : 409, {
            ok: false, error: operation.state, message: operation.message || "后台运行未完成。", ...(operation.result || {}),
            operation_state: operation.state,
          });
        }
        await wait(750, signal);
      } catch (error) {
        if (signal?.aborted || error?.name === "AbortError") throw error;
        failures += 1;
        operationConnectionState("recovering", operationId);
        await wait(retryDelay(failures), signal);
      }
    }
  }

  async function fetchHostedOperationResult(taskId, operationId, signal, {retry = true} = {}) {
    let failures = 0;
    while (true) {
      if (signal?.aborted) throw abortError();
      try {
        const response = await nativeFetch(
          `/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}/result`,
          {credentials: "same-origin", signal},
        );
        if (response.status === 202) {
          if (!retry) throw new Error("已保存结果尚未就绪，请重新打开当前任务重试。");
          await response.json();
          await wait(750, signal);
          continue;
        }
        const result = await response.json();
        if (!response.ok) {
          if (
            result
            && typeof result === "object"
            && !Array.isArray(result)
            && (typeof result.failure_code === "string" || typeof result.stage === "string")
          ) {
            return {response: asResponse(response.status, result), result: null};
          }
          if (isRecoverableResponse(response)) throw new Error(`Operation result unavailable: ${response.status}`);
          return {response: asResponse(response.status, result), result: null};
        }
        if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Operation result is not a JSON object");
        if (failures) operationConnectionState("connected", operationId);
        return {response: asParsedResponse(response.status, result), result};
      } catch (error) {
        if (!retry || signal?.aborted || error?.name === "AbortError") throw error;
        failures += 1;
        operationConnectionState("recovering", operationId);
        await wait(retryDelay(failures), signal);
      }
    }
  }

  async function handleHostedDownload(event) {
    if (!hostedInDesk || moduleName !== "datafetcher") return;
    const anchor = event.target?.closest?.("a[href]");
    if (!anchor || anchor.dataset.optionhelperDownload === "native") return;
    const url = new URL(anchor.href, location.href);
    const assetId = dataAssetDownloadId(url);
    if (assetId === undefined) return;
    event.preventDefault();
    if (assetId === null) return;
    const response = await hostedDataAssetDownload(url);
    if (!response.ok) {
      window.dispatchEvent(new CustomEvent("optionhelper.module-download-error", {detail: {status: response.status}}));
      return;
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    const contentType = String(response.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
    const extension = contentType === "application/json" ? "json" : contentType === "text/csv" ? "csv" : "";
    if (!extension) {
      URL.revokeObjectURL(objectUrl);
      window.dispatchEvent(new CustomEvent("optionhelper.module-download-error", {detail: {status: 415}}));
      return;
    }
    const disposition = String(response.headers.get("Content-Disposition") || "");
    const declaredName = disposition.match(/filename="([A-Za-z0-9][A-Za-z0-9_.-]{0,132})"/i)?.[1] || "";
    const filename = declaredName.toLowerCase().endsWith(`.${extension}`)
      ? declaredName
      : `${assetId}.${extension}`;
    const target = document.createElement("a");
    target.href = objectUrl;
    target.download = filename;
    target.hidden = true;
    target.dataset.optionhelperDownload = "native";
    document.body.append(target);
    target.click();
    target.remove();
    // WKWebView promotes the synthetic anchor to a native download after the
    // click returns.  Keep the Blob alive until that handoff has completed.
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  }

  // Install the routed fetch before product page scripts start their initial
  // catalog request.  Standalone pages retain their existing local-service
  // behavior until a ModuleHostContext is supplied.
  if (hostedInDesk) window.fetch = hostedFetch;
  document.addEventListener("click", (event) => { void handleHostedDownload(event); }, true);

  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin) return;
    if (event.data?.type === "optionhelper.module-visibility") {
      if (hostedInDesk && (!bridgeNonce || event.data?.bridge_nonce !== bridgeNonce || event.source !== window.parent)) return;
      const visibility = moduleVisibilityTransition(event.data?.active);
      const active = visibility.active;
      if (document.documentElement?.dataset) document.documentElement.dataset.optionhelperModuleActive = String(active);
      if (document.body) document.body.inert = !active;
      if (!active) {
        closeChoiceControls();
        document.activeElement?.blur?.();
      }
      if (typeof CustomEvent === "function") {
        document.dispatchEvent(new CustomEvent("optionhelper:modulevisibility", { detail: visibility }));
      }
      return;
    }
    if (event.data?.type === "optionhelper.module-theme") {
      if (hostedInDesk && (!bridgeNonce || event.data?.bridge_nonce !== bridgeNonce || event.source !== window.parent)) return;
      applyTheme(event.data?.theme, event.data?.preference);
      return;
    }
    if (event.data?.type === "optionhelper.ui-scale") {
      if (hostedInDesk && (!bridgeNonce || event.data?.bridge_nonce !== bridgeNonce || event.source !== window.parent)) return;
      const scale = Number(event.data?.scale);
      if (![0.8, 0.9, 1, 1.1, 1.25, 1.4].includes(scale)) return;
      document.documentElement.dataset.uiScale = String(scale);
      document.dispatchEvent(new CustomEvent("optionhelper:uiscalechange", {detail: {scale}}));
      requestAnimationFrame(() => requestAnimationFrame(() => window.dispatchEvent(new Event("resize"))));
      return;
    }
    if (event.data?.type !== "optionhelper.module-host-context") return;
    if (hostedInDesk && (!bridgeNonce || event.data?.bridge_nonce !== bridgeNonce)) return;
    if (!valid(event.data.context)) return;
    const suppliedVersion = Number(event.data.context_version);
    if (event.data.context_version !== undefined && (!Number.isSafeInteger(suppliedVersion) || suppliedVersion < contextVersion)) return;
    context = Object.freeze({...event.data.context});
    contextVersion = Number.isSafeInteger(suppliedVersion) && suppliedVersion > 0 ? suppliedVersion : contextVersion + 1;
    hostScope = Object.freeze({
      task_id: context.task_id,
      analysis_case_id: context.analysis_case_id,
      candidate_id: context.candidate_id,
      catalog_version: context.catalog_version,
    });
    window.fetch = hostedFetch;
    acceptHostContext?.(context);
    window.parent.postMessage({
      type: "optionhelper.module-host-context-ack",
      module: moduleName,
      bridge_nonce: bridgeNonce,
      context_version: contextVersion,
    }, location.origin);
    window.dispatchEvent(new CustomEvent("optionhelper.module-host-context", {detail: {context, contextVersion}}));
    window.dispatchEvent(new CustomEvent("optionhelper.module-host-ready", {detail: {module: moduleName}}));
  });
  if (window.parent !== window) {
    window.parent.postMessage({type: "optionhelper.module-host-ready", module: moduleName, bridge_nonce: bridgeNonce}, location.origin);
    requestHostContext("initial");
  }
})();
