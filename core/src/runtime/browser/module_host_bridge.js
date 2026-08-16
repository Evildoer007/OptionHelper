/* Shared ModuleHostContext browser adapter for the five Capability pages. */
(() => {
  "use strict";
  const moduleName = location.pathname.split("/").filter(Boolean).at(-2);
  const routes = {
    datafetcher: {"/api/status": "status", "/api/assets": "list_assets", "/api/fetch": "fetch"},
    payoffer: {"/api/catalog": "catalog", "/api/preview": "preview", "/api/run": "run"},
    pricer: {"/api/catalog": "catalog", "/api/run": "run"},
    backtester: {"/api/catalog": "catalog", "/api/run": "run"},
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
  let context = null;
  let hostScope = Object.freeze({});
  let acceptHostContext = null;
  const hostContextReady = new Promise((resolve) => { acceptHostContext = resolve; });
  const nativeFetch = window.fetch.bind(window);

  function applyEmbeddedLayout() {
    if (!hostedInDesk) return;
    document.documentElement.classList.add("optionhelper-embedded");
    if (document.body) document.body.classList.add("optionhelper-embedded");
    if (document.getElementById("optionhelper-embedded-module-style")) return;
    const style = document.createElement("style");
    style.id = "optionhelper-embedded-module-style";
    style.textContent = `
      html.optionhelper-embedded, body.optionhelper-embedded, body.optionhelper-embedded .app-shell {
        width: 100%; min-width: 0 !important; min-height: 100%; height: 100%; overflow: hidden;
      }
      body.optionhelper-embedded .topbar { display: none !important; }
      body.optionhelper-embedded .workbench { height: 100vh !important; min-height: 0; }
      body.optionhelper-embedded select {
        appearance: none; -webkit-appearance: none; padding-right: 31px !important;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14' fill='none'%3E%3Cpath d='m3.5 5.25 3.5 3.5 3.5-3.5' stroke='%23766b6d' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important;
        background-repeat: no-repeat !important; background-position: right 9px center !important; background-size: 14px !important;
      }
      body.optionhelper-embedded select:hover { border-color: #c9b6ba !important; background-color: #fffafb !important; }
      body.optionhelper-embedded select:focus { border-color: #c8102e !important; box-shadow: 0 0 0 3px rgba(200, 16, 46, .10) !important; outline: 0; }
    `;
    document.head.append(style);
  }

  applyEmbeddedLayout();
  document.addEventListener("DOMContentLoaded", applyEmbeddedLayout, { once: true });

  const panelLayoutKey = "optionhelper.desk-panel-widths";

  function clampPanelWidth(value, minimum, maximum, fallback) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
  }

  function readPanelLayout(workbench) {
    const style = getComputedStyle(workbench);
    const left = style.getPropertyValue("--library-width") || style.getPropertyValue("--source-width");
    const right = style.getPropertyValue("--inspector-width") || style.getPropertyValue("--settings-width");
    return {
      left: clampPanelWidth(left, 220, 420, 280),
      right: clampPanelWidth(right, 300, 520, 340),
    };
  }

  function applyPanelLayout(workbench, layout) {
    const left = clampPanelWidth(layout?.left, 220, 420, 280);
    const right = clampPanelWidth(layout?.right, 300, 520, 340);
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
    const layout = readPanelLayout(workbench);
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
    applyPanelLayout(workbench, storedPanelLayout() || readPanelLayout(workbench));
    let committedLayout = JSON.stringify(readPanelLayout(workbench));
    let pendingCommit = 0;
    const commitCurrentLayout = () => {
      pendingCommit = 0;
      const nextLayout = readPanelLayout(workbench);
      const serialized = JSON.stringify(nextLayout);
      if (serialized === committedLayout) return;
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
    new MutationObserver(scheduleCommit).observe(workbench, { attributes: true, attributeFilter: ["style"] });
    document.addEventListener("pointerup", commit, true);
    document.addEventListener("keyup", commit, true);
    window.addEventListener("storage", (event) => {
      if (event.key !== panelLayoutKey || !event.newValue) return;
      try { applyPanelLayout(workbench, JSON.parse(event.newValue)); }
      catch { /* Ignore malformed external storage events. */ }
    });
    window.addEventListener("message", (event) => {
      if (event.origin !== location.origin || event.source !== window.parent) return;
      if (event.data?.type !== "optionhelper.desk-panel-layout") return;
      if (event.data?.bridge_nonce && event.data.bridge_nonce !== bridgeNonce) return;
      const layout = {
        left: clampPanelWidth(event.data?.layout?.left, 220, 420, 280),
        right: clampPanelWidth(event.data?.layout?.right, 300, 520, 340),
      };
      committedLayout = JSON.stringify(layout);
      applyPanelLayout(workbench, layout);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installSharedPanelLayout, {once: true});
  else installSharedPanelLayout();

  function optionFor(select, value) {
    return Array.from(select.options).find((option) => option.value === value) || select.selectedOptions[0] || select.options[0];
  }

  function updateChoice(select) {
    const choice = select.closest("[data-oh-choice]");
    if (!choice) return;
    const trigger = choice.querySelector(".oh-choice__trigger");
    const value = choice.querySelector(".oh-choice__value");
    const menu = choice.querySelector(".oh-choice__menu");
    const selected = optionFor(select, select.value);
    value.textContent = selected?.textContent || "未选择";
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
      item.disabled = option.disabled;
      item.tabIndex = -1;
      item.textContent = option.textContent;
      item.addEventListener("click", () => {
        if (option.disabled) return;
        select.value = option.value;
        select.dispatchEvent(new Event("change", {bubbles: true}));
        setChoiceOpen(choice, false);
        trigger.focus();
      });
      return item;
    }));
    if (trigger.disabled) setChoiceOpen(choice, false);
  }

  function setChoiceOpen(choice, open, focus = false) {
    const trigger = choice.querySelector(".oh-choice__trigger");
    const menu = choice.querySelector(".oh-choice__menu");
    if (trigger.disabled && open) return;
    choice.dataset.open = String(open);
    trigger.setAttribute("aria-expanded", String(open));
    menu.hidden = !open;
    if (open && focus) (menu.querySelector('[role="option"][aria-selected="true"]:not([disabled])') || menu.querySelector('[role="option"]:not([disabled])'))?.focus();
  }

  function moveChoice(select, direction) {
    const choices = Array.from(select.options).filter((option) => !option.disabled);
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
        if (event.target.closest("[data-oh-choice]") || event.target === select) return;
        event.preventDefault();
        trigger.focus();
      });
    }
    trigger.addEventListener("click", () => setChoiceOpen(choice, choice.dataset.open !== "true", choice.dataset.open !== "true"));
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (choice.dataset.open === "true") moveChoice(select, event.key === "ArrowDown" ? 1 : -1);
        else setChoiceOpen(choice, true, true);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const options = Array.from(select.options).filter((option) => !option.disabled);
        const next = options[event.key === "Home" ? 0 : options.length - 1];
        if (next) { select.value = next.value; select.dispatchEvent(new Event("change", {bubbles: true})); }
      } else if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        setChoiceOpen(choice, choice.dataset.open !== "true", choice.dataset.open !== "true");
      } else if (event.key === "Escape") setChoiceOpen(choice, false);
    });
    menu.addEventListener("keydown", (event) => {
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
        document.activeElement?.click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        setChoiceOpen(choice, false);
        trigger.focus();
      } else if (event.key === "Tab") setChoiceOpen(choice, false);
    });
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
      .oh-choice__native { position: absolute !important; width: 1px !important; height: 1px !important; min-height: 1px !important; margin: -1px !important; padding: 0 !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; opacity: 0 !important; pointer-events: none !important; }
      .oh-choice__trigger { display: flex; width: 100%; min-height: 38px; align-items: center; justify-content: space-between; gap: 10px; padding: 0 11px; border: 1px solid var(--rule, #d7cfd0); border-radius: 8px; background: var(--surface, #fff); color: var(--ink, #2b2224); font: inherit; line-height: 1.3; text-align: left; transition: border-color .16s ease, background .16s ease, box-shadow .16s ease, color .16s ease; }
      .oh-choice__value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .oh-choice__chevron { width: 8px; height: 8px; flex: 0 0 8px; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; color: var(--muted, #766b6d); transform: rotate(45deg) translate(-2px, -2px); transition: transform .16s ease; }
      .oh-choice:hover .oh-choice__trigger, .oh-choice[data-open="true"] .oh-choice__trigger { border-color: var(--red, #c8102e); background: #fff8f8; }
      .oh-choice[data-open="true"] .oh-choice__trigger { box-shadow: 0 0 0 3px rgba(200,16,46,.10); color: var(--red-deep, #9e1027); }
      .oh-choice[data-open="true"] .oh-choice__chevron { transform: rotate(225deg) translate(-1px, -1px); }
      .oh-choice__trigger:focus-visible { outline: 3px solid rgba(200,16,46,.25); outline-offset: 2px; }
      .oh-choice__trigger:disabled { background: #f4f2f2; color: #978e90; cursor: not-allowed; }
      .oh-choice__native[aria-invalid="true"] ~ .oh-choice__trigger { border-color: #b71b36; background: #fff7f8; box-shadow: 0 0 0 3px rgba(183,27,54,.08); }
      .oh-choice__menu { position: absolute; z-index: 80; top: calc(100% + 6px); right: 0; left: 0; max-height: min(280px, 42vh); padding: 5px; overflow: auto; border: 1px solid #dacacf; border-radius: 9px; background: var(--surface, #fff); box-shadow: 0 14px 28px rgba(60,40,44,.17); }
      .oh-choice__option { display: flex; width: 100%; min-height: 32px; align-items: center; padding: 7px 9px; border: 0; border-radius: 6px; background: transparent; color: var(--ink, #2b2224); font: inherit; font-size: 12px; line-height: 1.35; text-align: left; }
      .oh-choice__option:hover, .oh-choice__option:focus-visible { outline: 0; background: #fff0f2; color: var(--red-deep, #9e1027); }
      .oh-choice__option[aria-selected="true"] { background: #f9e9ec; color: var(--red-deep, #9e1027); font-weight: 700; }
      .oh-choice__option[aria-selected="true"]::after { width: 5px; height: 9px; margin-left: auto; border-right: 1.5px solid currentColor; border-bottom: 1.5px solid currentColor; content: ""; transform: rotate(45deg) translate(-2px,-1px); }
      .oh-choice__option:disabled { color: #a09698; cursor: not-allowed; }
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
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installChoiceControls, {once: true});
  else installChoiceControls();

  function valid(value) {
    const token = /^oh\.([0-9]{1,12})\.([0-9a-f]{64})$/.exec(String(value?.capability_token || ""));
    const expiresAt = Number(token?.[1]);
    return value && value.module === moduleName && value.host_kind && value.context_id && value.capability_token
      && typeof value.page_hash === "string" && /^[0-9a-f]{64}$/.test(value.page_hash)
      && ["task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint"].every((field) => value[field] === null || typeof value[field] === "string")
      && Number.isSafeInteger(expiresAt) && expiresAt * 1000 > Date.now();
  }
  function requestId() { return crypto.randomUUID ? crypto.randomUUID() : `req_${Date.now()}_${Math.random().toString(36).slice(2)}`; }
  function asResponse(status, body) {
    return new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
  }
  function abortError() {
    return new DOMException("Module Host request was aborted", "AbortError");
  }
  function waitForHostContext(signal) {
    if (context) return context;
    if (signal?.aborted) throw abortError();
    return new Promise((resolve, reject) => {
      let settled = false;
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
      const timer = window.setTimeout(() => finish(null), 5000);
      signal?.addEventListener("abort", abort, {once: true});
      hostContextReady.then(finish);
    });
  }
  function applyHostScope(body) {
    for (const field of ["task_id", "analysis_case_id", "candidate_id", "catalog_version", "contract_fingerprint"]) {
      const bound = hostScope[field];
      if (bound == null) delete body[field];
      else body[field] = bound;
    }
    return null;
  }
  function normalizeHostedRequest(body, action) {
    if (action === "run" && hostScope.contract_fingerprint && body.new_contract_variant !== true) {
      delete body.product_id;
      delete body.identity;
      delete body.term_overrides;
    }
    if (moduleName === "backtester" && action === "run" && typeof body.history_reference === "string"
      && /[\\/]/.test(body.history_reference)) delete body.history_reference;
  }
  function requestedMethod(input, init) {
    return String(init.method || (typeof Request !== "undefined" && input instanceof Request ? input.method : "GET")).toUpperCase();
  }
  function requestedSignal(input, init) {
    return init.signal || (typeof Request !== "undefined" && input instanceof Request ? input.signal : undefined);
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
    if (!context && hostedInDesk) await waitForHostContext(signal);
    if (!context && hostedInDesk) {
      return asResponse(503, {ok: false, message: "模块页面尚未收到下载上下文，请稍后重试。"});
    }
    if (!context) return nativeFetch(url, {signal});
    return nativeFetch(url.pathname, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-OptionHelper-Module-Context": JSON.stringify(context),
        "X-OptionHelper-Request-Id": requestId(),
      },
      body: "{}",
      signal,
    });
  }
  async function hostedFetch(input, init = {}) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const action = routes[moduleName]?.[url.pathname];
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
    if (!context && hostedInDesk) await waitForHostContext(signal);
    if (!context && hostedInDesk) {
      return asResponse(503, {ok: false, message: "模块页面尚未收到运行上下文，请稍后重试。"});
    }
    if (!context) return nativeFetch(input, init);
    let body = {};
    const rawBody = init.body ?? (typeof Request !== "undefined" && input instanceof Request && method !== "GET" ? await input.clone().text() : undefined);
    if (rawBody) {
      try { body = JSON.parse(rawBody); } catch { return asResponse(400, {ok: false, message: "Module Host只接受JSON请求"}); }
    }
    body.action = action;
    normalizeHostedRequest(body, action);
    if (action === "list_report_sources") {
      const requestedTask = url.searchParams.get("task_id");
      if (requestedTask != null) body.task_id = requestedTask;
      body.query = url.searchParams.get("q") || undefined;
    }
    const scopeError = applyHostScope(body);
    if (scopeError) return asResponse(400, {ok: false, message: scopeError});
    const response = await nativeFetch(`/api/tools/${encodeURIComponent(moduleName)}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-OptionHelper-Module-Context": JSON.stringify(context),
        "X-OptionHelper-Request-Id": requestId(),
      },
      body: JSON.stringify(body),
      signal,
    });
    const envelope = await response.json();
    const result = envelope.result || envelope;
    if (moduleName === "payoffer" && action === "run" && !result.destination && result.module_run_ref?.run_id) {
      result.destination = "结果已保存到当前研究任务。";
    }
    return asResponse(response.status, result);
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
    const target = document.createElement("a");
    target.href = objectUrl;
    target.download = `${assetId}.csv`;
    target.hidden = true;
    target.dataset.optionhelperDownload = "native";
    document.body.append(target);
    target.click();
    target.remove();
    URL.revokeObjectURL(objectUrl);
  }

  // Install the routed fetch before product page scripts start their initial
  // catalog request.  Standalone pages retain their existing local-service
  // behavior until a ModuleHostContext is supplied.
  if (hostedInDesk) window.fetch = hostedFetch;
  document.addEventListener("click", (event) => { void handleHostedDownload(event); }, true);

  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.type !== "optionhelper.module-host-context") return;
    if (hostedInDesk && (!bridgeNonce || event.data?.bridge_nonce !== bridgeNonce)) return;
    if (!valid(event.data.context)) return;
    context = Object.freeze({...event.data.context});
    hostScope = Object.freeze({
      task_id: context.task_id,
      analysis_case_id: context.analysis_case_id,
      candidate_id: context.candidate_id,
      catalog_version: context.catalog_version,
      contract_fingerprint: context.contract_fingerprint,
    });
    window.fetch = hostedFetch;
    acceptHostContext?.(context);
    window.dispatchEvent(new CustomEvent("optionhelper.module-host-ready", {detail: {module: moduleName}}));
  });
  if (window.parent !== window) {
    window.parent.postMessage({type: "optionhelper.module-host-ready", module: moduleName, bridge_nonce: bridgeNonce}, location.origin);
  }
})();
