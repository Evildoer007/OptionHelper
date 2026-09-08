import { createThinkingOrb } from "/app/frontend/shared/thinking-orb.js";
const secretKeys = new Set(["password", "token", "api_key", "secret", "secret_value", "private_key"]);
const sessionEventKey = "optionhelper.session.event";

function notifySignedOut() {
  const event = { type: "signed_out", nonce: globalThis.crypto?.randomUUID?.() || String(Date.now()) };
  try {
    localStorage.setItem(sessionEventKey, JSON.stringify(event));
  } catch {
    // Native shell notification remains available when browser storage is disabled.
  }
  window.webkit?.messageHandlers?.optionhelperSession?.postMessage(event);
  window.chrome?.webview?.postMessage(event);
}

addEventListener("storage", (event) => {
  if (event.key === sessionEventKey && event.newValue) location.replace("/");
});

export async function request(path, options = {}) {
  const { headers = {}, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    headers: { "Content-Type": "application/json", ...headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.message || body.detail || body.reason || body.next_step || body.error?.next_step || body.error || "请求未完成");
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

export function safeJson(value, { allowSecrets = new Set() } = {}) {
  const visit = (item) => {
    if (Array.isArray(item)) return item.map(visit);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.entries(item).map(([key, child]) => {
        if (secretKeys.has(key) && !allowSecrets.has(key)) throw new Error("明文凭据只能通过设置中心的专用凭据表单保存。");
        return [key, visit(child)];
      }));
    }
    return item;
  };
  return JSON.stringify(visit(value));
}

export function message(node, text, isError = false) {
  if (!node) return;
  node.hidden = false;
  node.textContent = text;
  node.classList.toggle("notice--error", isError);
}

export function clearMessage(node) {
  if (!node) return;
  node.hidden = true;
  node.textContent = "";
  node.classList.remove("notice--error");
}

export function settingsURLFor(sourceLocation = location, section = "") {
  const source = new URL(sourceLocation.href);
  const settings = new URL("/settings", source.origin);
  if (["/optchat", "/optdesk"].includes(source.pathname)) {
    settings.searchParams.set("return_to", `${source.pathname}${source.search}${source.hash}`);
  }
  if (section) settings.hash = section;
  return `${settings.pathname}${settings.search}${settings.hash}`;
}

function choiceLabel(select) {
  const explicit = select.getAttribute("aria-label");
  if (explicit) return explicit;
  if (select.id) {
    const explicitLabel = Array.from(select.ownerDocument.querySelectorAll("label[for]")).find((label) => label.htmlFor === select.id);
    if (explicitLabel?.textContent.trim()) return explicitLabel.textContent.trim();
  }
  const label = select.closest("label");
  if (!label) return "选择选项";
  const clone = label.cloneNode(true);
  clone.querySelectorAll("select, button, [role=listbox]").forEach((node) => node.remove());
  return clone.textContent.trim() || "选择选项";
}

function optionFor(select, value) {
  return Array.from(select.options).find((option) => option.value === value) || select.selectedOptions[0] || select.options[0];
}

function isChoiceOpen(choice) {
  return choice.dataset.open === "true";
}

function positionChoiceMenu(choice) {
  const trigger = choice.querySelector(".choice-trigger");
  const menu = choice.querySelector(".choice-menu");
  if (!trigger || !menu || menu.hidden) return;
  const rect = trigger.getBoundingClientRect();
  const view = choice.ownerDocument.defaultView;
  const viewportHeight = view?.innerHeight || document.documentElement.clientHeight;
  let top = 0;
  let bottom = viewportHeight;
  for (let parent = choice.parentElement; parent; parent = parent.parentElement) {
    if (!/(auto|scroll|hidden|clip)/.test(view.getComputedStyle(parent).overflowY)) continue;
    const bounds = parent.getBoundingClientRect();
    top = Math.max(top, bounds.top);
    bottom = Math.min(bottom, bounds.bottom);
  }
  const desiredHeight = Math.min(menu.scrollHeight || 160, 280);
  const roomBelow = Math.max(0, bottom - rect.bottom - 6);
  const roomAbove = Math.max(0, rect.top - top - 6);
  choice.dataset.placement = roomBelow < desiredHeight && roomAbove > roomBelow ? "top" : "bottom";
  menu.style.maxHeight = `${Math.min(280, viewportHeight * .42, choice.dataset.placement === "top" ? roomAbove : roomBelow)}px`;
}

function setChoiceOpen(choice, open, { focus = false } = {}) {
  const trigger = choice.querySelector(".choice-trigger");
  const menu = choice.querySelector(".choice-menu");
  if (!trigger || !menu || (trigger.disabled && open)) return;
  if (open) choice.ownerDocument.querySelectorAll('[data-choice-root][data-open="true"]').forEach((other) => {
    if (other !== choice) setChoiceOpen(other, false);
  });
  choice.dataset.open = String(open);
  trigger.setAttribute("aria-expanded", String(open));
  menu.hidden = !open;
  if (open) positionChoiceMenu(choice);
  if (open && focus) {
    const selected = menu.querySelector('[role="option"][aria-selected="true"]:not([disabled])') || menu.querySelector('[role="option"]:not([disabled])');
    selected?.focus({ preventScroll: true });
  } else if (!open && focus) {
    trigger.focus({ preventScroll: true });
  }
}

function syncChoice(select) {
  const choice = select.closest("[data-choice-root]");
  if (!choice) return;
  const trigger = choice.querySelector(".choice-trigger");
  const value = choice.querySelector(".choice-trigger__value");
  const menu = choice.querySelector(".choice-menu");
  if (!trigger || !value || !menu) return;
  const selected = optionFor(select, select.value);
  value.textContent = selected?.textContent || "未选择";
  trigger.disabled = select.disabled || !select.options.length;
  trigger.setAttribute("aria-disabled", String(trigger.disabled));
  if (select.getAttribute("aria-invalid") === "true") trigger.setAttribute("aria-invalid", "true");
  else trigger.removeAttribute("aria-invalid");
  const describedBy = select.getAttribute("aria-describedby");
  if (describedBy) trigger.setAttribute("aria-describedby", describedBy);
  else trigger.removeAttribute("aria-describedby");
  const doc = select.ownerDocument;
  const focusedValue = menu.contains(doc.activeElement) ? doc.activeElement.dataset.value : null;
  menu.replaceChildren(...Array.from(select.options).map((option) => {
    const item = doc.createElement("button");
    item.type = "button";
    item.className = "choice-option";
    item.setAttribute("role", "option");
    item.dataset.value = option.value;
    item.textContent = option.textContent;
    item.disabled = select.disabled || option.disabled || Boolean(option.closest("optgroup")?.disabled);
    item.setAttribute("aria-selected", String(option.selected));
    item.tabIndex = -1;
    item.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (item.disabled || select.disabled) return;
      const changed = select.value !== option.value;
      setChoiceOpen(choice, false);
      select.value = option.value;
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
      if (trigger.isConnected && !select.disabled) trigger.focus({ preventScroll: true });
    });
    return item;
  }));
  if (trigger.disabled) setChoiceOpen(choice, false);
  else if (focusedValue !== null && isChoiceOpen(choice)) {
    const options = Array.from(menu.querySelectorAll('[role="option"]:not([disabled])'));
    (options.find((item) => item.dataset.value === focusedValue) || options.find((item) => item.dataset.value === select.value) || trigger).focus({ preventScroll: true });
    positionChoiceMenu(choice);
  }
}

function moveChoice(select, direction) {
  const enabled = Array.from(select.options).filter((option) => !option.disabled && !option.closest("optgroup")?.disabled);
  if (!enabled.length) return;
  const index = Math.max(0, enabled.findIndex((option) => option.value === select.value));
  const next = enabled[Math.max(0, Math.min(enabled.length - 1, index + direction))];
  if (!next || next.value === select.value) return;
  select.value = next.value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

/**
 * Renders an accessible, product-owned choice menu while retaining the source
 * select for form serialization and programmatic integrations. The original
 * select is intentionally removed from the tab order; the trigger supports
 * the standard select keyboard sequence (Space/Enter, arrows, Home/End, Esc).
 */
export function enhanceSelects(root = document) {
  const doc = root.ownerDocument || root;
  const selects = root.querySelectorAll?.("select[data-choice]") || [];
  selects.forEach((select) => {
    if (select.closest("[data-choice-root]")) {
      syncChoice(select);
      return;
    }
    const menuId = `choice-${globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
    const choice = doc.createElement("span");
    choice.className = "choice optionhelper-choice";
    choice.dataset.choiceRoot = "true";
    const trigger = doc.createElement("button");
    trigger.type = "button";
    trigger.id = `${menuId}-trigger`;
    trigger.className = "choice-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", menuId);
    trigger.setAttribute("aria-label", choiceLabel(select));
    trigger.innerHTML = '<span class="choice-trigger__value"></span><span class="choice-trigger__chevron" aria-hidden="true"></span>';
    const menu = doc.createElement("span");
    menu.id = menuId;
    menu.className = "choice-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", choiceLabel(select));
    menu.hidden = true;
    select.classList.add("choice-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.after(choice);
    choice.append(select, trigger, menu);
    const explicitLabels = Array.from(doc.querySelectorAll("label[for]")).filter((label) => label.htmlFor === select.id);
    explicitLabels.forEach((label) => { label.htmlFor = trigger.id; });
    const wrappingLabel = choice.closest("label");
    if (wrappingLabel && !wrappingLabel.htmlFor) {
      wrappingLabel.addEventListener("click", (event) => {
        if (event.target.closest('[data-choice-root], input, select, textarea, button, a[href], [contenteditable="true"]')) return;
        event.preventDefault();
        trigger.focus();
      });
    }
    const close = (focusTrigger = false) => setChoiceOpen(choice, false, { focus: focusTrigger });
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      setChoiceOpen(choice, !isChoiceOpen(choice), { focus: isChoiceOpen(choice) === false });
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !isChoiceOpen(choice)) return;
      if (["ArrowDown", "ArrowUp", "Home", "End", " ", "Enter", "Escape"].includes(event.key)) event.stopPropagation();
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (isChoiceOpen(choice)) moveChoice(select, event.key === "ArrowDown" ? 1 : -1);
        else setChoiceOpen(choice, true, { focus: true });
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const options = Array.from(select.options).filter((option) => !option.disabled && !option.closest("optgroup")?.disabled);
        const option = options[event.key === "Home" ? 0 : options.length - 1];
        if (option && option.value !== select.value) { select.value = option.value; select.dispatchEvent(new Event("change", { bubbles: true })); }
      } else if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        if (event.repeat) return;
        setChoiceOpen(choice, !isChoiceOpen(choice), { focus: !isChoiceOpen(choice) });
      }
    });
    menu.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "Home", "End", " ", "Enter", "Escape"].includes(event.key)) event.stopPropagation();
      const options = Array.from(menu.querySelectorAll('[role="option"]:not([disabled])'));
      const at = options.indexOf(doc.activeElement);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        (options[Math.max(0, Math.min(options.length - 1, at + (event.key === "ArrowDown" ? 1 : -1)))] || options[0])?.focus();
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        options[event.key === "Home" ? 0 : options.length - 1]?.focus();
      } else if (event.key === "Escape") {
        event.preventDefault(); close(true);
      } else if (event.key === "Tab") {
        close(true);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        if (event.repeat) return;
        doc.activeElement?.click();
      }
    });
    select.addEventListener("change", () => syncChoice(select));
    new MutationObserver(() => syncChoice(select)).observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["aria-describedby", "aria-invalid", "disabled", "selected", "label"] });
    syncChoice(select);
  });
  if (doc.documentElement.dataset.choiceOutsideReady !== "true") {
    doc.documentElement.dataset.choiceOutsideReady = "true";
    doc.addEventListener("pointerdown", (event) => {
      doc.querySelectorAll('[data-choice-root][data-open="true"]').forEach((choice) => {
        if (!choice.contains(event.target)) setChoiceOpen(choice, false);
      });
    });
    doc.addEventListener("focusin", (event) => {
      doc.querySelectorAll('[data-choice-root][data-open="true"]').forEach((choice) => {
        if (!choice.contains(event.target)) setChoiceOpen(choice, false);
      });
    });
  }
}

export async function initializeWorkspace(mode, { onModeChange } = {}) {
  const [session, settingsResponse] = await Promise.all([
    request("/api/me"),
    request("/api/settings").catch(() => null),
  ]);
  const savedTheme = settingsResponse?.settings?.preferences?.theme;
  if (["light", "dark", "auto"].includes(savedTheme)) {
    const { applyServerThemePreference } = await import("./theme.js");
    applyServerThemePreference(savedTheme);
  }
  const hasDesk = session.capabilities.includes("optdesk");
  if (mode === "desk" && !hasDesk) {
    const redirect = new URL("/optchat", location.origin);
    const taskId = taskIdFromLocation();
    if (taskId) redirect.searchParams.set("task", taskId);
    location.replace(`${redirect.pathname}${redirect.search}`);
    return null;
  }

  const shell = document.querySelector("[data-workspace-shell]");
  if (!shell) return session;
  shell.dataset.mode = mode;
  shell.dataset.hasDesk = String(hasDesk);
  const modeSwitch = shell.querySelector("[data-mode-switch]");
  let deskButton = modeSwitch?.querySelector('button[data-mode="desk"]');
  if (hasDesk && modeSwitch && !deskButton) {
    deskButton = document.createElement("button");
    deskButton.type = "button";
    deskButton.dataset.mode = "desk";
    deskButton.textContent = "OptDesk";
    modeSwitch.append(deskButton);
  } else if (!hasDesk && deskButton) {
    deskButton.remove();
  }
  document.querySelectorAll("button[data-mode]").forEach((button) => {
    const target = button.dataset.mode;
    const allowed = target !== "desk" || hasDesk;
    button.setAttribute("aria-pressed", String(target === mode));
    button.addEventListener("click", async () => {
      if (target !== shell.dataset.mode && allowed) {
        await onModeChange?.(target);
      }
    });
  });

  const settingsLink = document.querySelector("[data-settings-link]");
  if (settingsLink) settingsLink.href = settingsURLFor(location);

  const accountMenuRoot = shell.querySelector("[data-account-menu-root]");
  const accountRail = accountMenuRoot?.closest(".workspace-rail");
  const accountMenuToggle = accountMenuRoot?.querySelector("[data-account-menu-toggle]");
  const accountMenu = accountMenuRoot?.querySelector(".rail-account-menu");
  const logoutButton = accountMenuRoot?.querySelector("[data-logout]");
  const principal = String(session.identity?.principal_id || "");
  const accountLabel = principal.includes(":")
    ? principal.slice(principal.lastIndexOf(":") + 1)
    : (principal || (session.identity?.role === "admin" ? "管理员" : "用户"));
  accountMenuRoot?.querySelectorAll("[data-account-label], [data-account-menu-label]").forEach((label) => {
    label.textContent = accountLabel;
  });
  accountMenuRoot?.querySelectorAll("[data-account-avatar]").forEach((avatar) => {
    avatar.textContent = Array.from(accountLabel.trim())[0]?.toUpperCase() || "U";
  });
  const accountMenuItems = () => Array.from(accountMenu?.querySelectorAll('[role="menuitem"]:not([disabled])') || []);
  const focusAccountMenuItem = (position) => {
    const items = accountMenuItems();
    if (!items.length) return;
    const activeIndex = items.indexOf(document.activeElement);
    if (position === "first") items[0].focus();
    else if (position === "last") items[items.length - 1].focus();
    else items[(activeIndex + position + items.length) % items.length].focus();
  };
  const setAccountMenuOpen = (open, { focus = false } = {}) => {
    if (!accountMenu || !accountMenuToggle) return;
    accountMenu.hidden = !open;
    accountMenuToggle.setAttribute("aria-expanded", String(open));
    accountRail?.setAttribute("data-account-menu-open", String(open));
    if (open && focus) focusAccountMenuItem(focus === "last" ? "last" : "first");
  };
  if (accountMenuRoot && accountMenuToggle && accountMenu && accountMenuRoot.dataset.accountMenuReady !== "true") {
    accountMenuRoot.dataset.accountMenuReady = "true";
    accountMenuToggle.addEventListener("click", () => setAccountMenuOpen(accountMenu.hidden, { focus: accountMenu.hidden ? "first" : false }));
    accountMenuToggle.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      setAccountMenuOpen(true, { focus: ["ArrowUp", "End"].includes(event.key) ? "last" : "first" });
    });
    // Mouse and keyboard activation share click; close before item actions or navigation.
    accountMenu.addEventListener("click", (event) => {
      const item = event.target?.closest?.('[role="menuitem"]');
      if (!item || !accountMenu.contains(item) || item.hasAttribute("disabled")) return;
      setAccountMenuOpen(false);
    }, { capture: true });
    accountMenu.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        focusAccountMenuItem(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        focusAccountMenuItem(event.key === "Home" ? "first" : "last");
      } else if (event.key === "Escape") {
        event.preventDefault();
        setAccountMenuOpen(false);
        accountMenuToggle.focus();
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!accountMenuRoot.contains(event.target)) setAccountMenuOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || accountMenu.hidden) return;
      setAccountMenuOpen(false);
      accountMenuToggle.focus();
    });
  }
  logoutButton?.addEventListener("click", async () => {
    if (logoutButton.disabled) return;
    logoutButton.disabled = true;
    try {
      await request("/api/auth/logout", { method: "POST", body: "{}" });
      notifySignedOut();
      location.replace("/");
    } catch {
      logoutButton.disabled = false;
      logoutButton.title = "退出未完成，请稍后重试。";
    }
  });
  enhanceSelects(document);
  installLayoutControls(shell);
  return session;
}

export const MODEL_CONFIGURATION_CHANGED = "optionhelper:model-configuration-changed";

export function notifyModelConfigurationChanged() {
  // Only an invalidation signal is shared; credentials stay on the server.
  try { localStorage.setItem(MODEL_CONFIGURATION_CHANGED, String(Date.now())); } catch { /* Focus refresh also works without storage. */ }
  window.dispatchEvent(new Event(MODEL_CONFIGURATION_CHANGED));
}

export async function configureModelPicker(picker) {
  if (!picker) return;
  const catalog = await request("/api/settings/model-providers");
  const enabled = (catalog.providers || []).flatMap((provider) => (provider.credential_configured ? provider.models
    .filter((model) => model.enabled)
    .map((model) => ({ provider, model })) : []));
  const previousSelection = picker.dataset.userExplicitSelection === "true" ? picker.value : "";
  picker.replaceChildren();
  if (!enabled.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "未配置模型";
    picker.append(option);
    picker.disabled = true;
    picker.title = "请在设置中心保存模型服务和API Key，并启用要使用的模型。";
  } else {
    const defaultSelection = catalog.default_model_selection || {};
    enabled.forEach(({ provider, model }) => {
      const option = document.createElement("option");
      option.value = JSON.stringify({ provider_id: provider.provider_id, model_id: model.model_id });
      option.textContent = `${provider.display_name} / ${model.display_name || model.model_id}`;
      option.dataset.inputModalities = JSON.stringify(
        Array.isArray(model.input_modalities) ? model.input_modalities : ["text"],
      );
      option.selected = provider.provider_id === defaultSelection.provider_id && model.model_id === defaultSelection.model_id;
      picker.append(option);
    });
    picker.disabled = false;
    picker.removeAttribute("title");
  }
  picker.dataset.userExplicitSelection = "false";
  if (previousSelection && Array.from(picker.options).some(option => option.value === previousSelection)) {
    picker.value = previousSelection;
    picker.dataset.userExplicitSelection = "true";
  }
  if (picker.dataset.modelPickerReady !== "true") {
    picker.dataset.modelPickerReady = "true";
    picker.addEventListener("change", async () => {
      if (!picker.value) return;
      picker.dataset.userExplicitSelection = "true";
      try {
        const selection = JSON.parse(picker.value);
        await request("/api/settings/model-provider/default", { method: "POST", body: JSON.stringify(selection) });
      } catch {
        // The next refresh restores the persisted default; never block a
        // local message just because the convenience default write failed.
      }
    });
  }
  enhanceSelects(picker.parentElement || document);
}

function installLayoutControls(shell) {
  if (shell.dataset.layoutControlsReady === "true") return;
  shell.dataset.layoutControlsReady = "true";
  const reportToggle = shell.querySelector("[data-report-toggle]");
  const reportClose = shell.querySelector("[data-report-close]");
  const reportScrim = shell.querySelector("[data-report-scrim]");
  const contextPanel = shell.querySelector("#task-context");
  const taskHistoryToggle = shell.querySelector("[data-task-history-toggle]");
  const taskHistoryClose = shell.querySelector("[data-task-history-close]");
  const taskHistoryScrim = shell.querySelector("[data-task-history-scrim]");
  const taskHistoryPanel = shell.querySelector("#mobile-task-drawer");
  const railSplitter = shell.querySelector('[data-workspace-splitter="rail"]');
  const contextSplitter = shell.querySelector('[data-workspace-splitter="context"]');
  const compactReportMedia = window.matchMedia("(max-width: 1220px)");
  const mobileTaskMedia = window.matchMedia("(max-width: 700px)");
  const limits = {
    rail: { variable: "--rail-width", minimum: 197, maximum: 440, fallback: 236 },
    report: { variable: "--report-width", minimum: 280, maximum: 480, fallback: 332 },
  };
  const readStored = (key, fallback) => {
    try {
      const stored = localStorage.getItem(`optionhelper.workspace.${key}`);
      const value = stored?.trim() ? Number(stored) : NaN;
      return Number.isFinite(value) && value > 0 ? value : fallback;
    } catch { return fallback; }
  };
  const saveStored = (key, value) => {
    try { localStorage.setItem(`optionhelper.workspace.${key}`, String(Math.round(value))); } catch { /* local persistence is optional */ }
  };
  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  const maximumFor = (kind) => {
    const current = limits[kind];
    const reportWidth = shell.dataset.reportOpen === "true" && !compactReportMedia.matches ? readWidth("report") : 0;
    const otherWidth = kind === "rail" ? reportWidth : readWidth("rail");
    const splitterWidth = shell.dataset.reportOpen === "true" ? 16 : 8;
    return Math.max(current.minimum, Math.min(current.maximum, Math.floor(shell.getBoundingClientRect().width - 560 - splitterWidth - otherWidth)));
  };
  const readWidth = (kind) => {
    const current = limits[kind];
    const value = Number.parseFloat(getComputedStyle(shell).getPropertyValue(current.variable));
    return Number.isFinite(value) && value > 0 ? value : current.fallback;
  };
  const applyWidth = (kind, value, persist = false) => {
    const current = limits[kind];
    const next = clamp(value, current.minimum, maximumFor(kind));
    shell.style.setProperty(current.variable, `${next}px`);
    const splitter = kind === "rail" ? railSplitter : contextSplitter;
    splitter?.setAttribute("aria-valuenow", String(Math.round(next)));
    splitter?.setAttribute("aria-valuemin", String(current.minimum));
    splitter?.setAttribute("aria-valuemax", String(Math.round(maximumFor(kind))));
    if (persist) saveStored(kind, next);
  };
  const constrain = () => {
    applyWidth("rail", readWidth("rail"));
    applyWidth("report", readWidth("report"));
  };
  const panelFocusables = (panel) => Array.from(panel?.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  ) || []).filter((element) => {
    if (element.tabIndex < 0 || element.matches(":disabled")) return false;
    for (let node = element; node && node !== panel.parentElement; node = node.parentElement) {
      if (node.hidden || node.inert || node.getAttribute("aria-hidden") === "true") return false;
      const style = getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    }
    return true;
  });
  const trapPanelFocus = (event, panel) => {
    if (event.key !== "Tab") return;
    const focusables = panelFocusables(panel);
    if (!focusables.length) {
      event.preventDefault();
      panel?.focus?.();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (!focusables.includes(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
  let reportReturnFocus = null;
  let taskHistoryReturnFocus = null;
  const syncOverlayDocumentState = () => {
    const overlayOpen = (compactReportMedia.matches && shell.dataset.reportOpen === "true")
      || (mobileTaskMedia.matches && shell.dataset.taskHistoryOpen === "true");
    document.documentElement.classList.toggle("workspace-overlay-open", overlayOpen);
  };
  function setTaskHistoryOpen(open, { restoreFocus = true } = {}) {
    const next = Boolean(open && mobileTaskMedia.matches && taskHistoryPanel);
    if (next && shell.dataset.reportOpen === "true") setReportOpen(false, { restoreFocus: false });
    if (next) taskHistoryReturnFocus = document.activeElement;
    shell.dataset.taskHistoryOpen = String(next);
    taskHistoryToggle?.setAttribute("aria-expanded", String(next));
    if (taskHistoryPanel) {
      taskHistoryPanel.hidden = !next;
      taskHistoryPanel.inert = !next;
      taskHistoryPanel.setAttribute("aria-hidden", String(!next));
      taskHistoryPanel.setAttribute("role", "dialog");
      taskHistoryPanel.setAttribute("aria-modal", "true");
    }
    if (taskHistoryScrim) taskHistoryScrim.hidden = !next;
    syncOverlayDocumentState();
    if (next) requestAnimationFrame(() => taskHistoryClose?.focus());
    else if (restoreFocus && taskHistoryReturnFocus?.isConnected) taskHistoryReturnFocus.focus();
  }
  function setReportOpen(open, { restoreFocus = true } = {}) {
    const next = Boolean(open && contextPanel);
    if (next && shell.dataset.taskHistoryOpen === "true") setTaskHistoryOpen(false, { restoreFocus: false });
    if (next) reportReturnFocus = document.activeElement;
    shell.dataset.reportOpen = String(next);
    reportToggle?.setAttribute("aria-expanded", String(next));
    reportToggle?.classList.toggle("is-active", next);
    if (reportToggle) {
      const label = next ? reportToggle.dataset.closeLabel : reportToggle.dataset.openLabel;
      if (label) {
        reportToggle.setAttribute("aria-label", label);
        reportToggle.setAttribute("title", label);
        const accessibleText = reportToggle.querySelector(".sr-only");
        if (accessibleText) accessibleText.textContent = label;
      }
    }
    if (contextPanel) {
      contextPanel.hidden = !next;
      contextPanel.setAttribute("aria-hidden", String(!next));
      contextPanel.inert = !next;
      if (compactReportMedia.matches) {
        contextPanel.setAttribute("role", "dialog");
        contextPanel.setAttribute("aria-modal", "true");
      } else {
        contextPanel.removeAttribute("role");
        contextPanel.removeAttribute("aria-modal");
      }
    }
    if (contextSplitter) {
      const splitterAvailable = next && !compactReportMedia.matches;
      contextSplitter.tabIndex = splitterAvailable ? 0 : -1;
      contextSplitter.setAttribute("aria-hidden", String(!splitterAvailable));
    }
    if (reportScrim) reportScrim.hidden = !(next && compactReportMedia.matches);
    syncOverlayDocumentState();
    if (next) {
      constrain();
      requestAnimationFrame(() => reportClose?.focus());
    } else if (restoreFocus && reportReturnFocus?.isConnected) {
      reportReturnFocus.focus();
    }
  }
  const syncResponsivePanels = () => {
    if (!mobileTaskMedia.matches && shell.dataset.taskHistoryOpen === "true") {
      setTaskHistoryOpen(false, { restoreFocus: false });
    }
    if (contextPanel) {
      if (compactReportMedia.matches) {
        contextPanel.setAttribute("role", "dialog");
        contextPanel.setAttribute("aria-modal", "true");
      } else {
        contextPanel.removeAttribute("role");
        contextPanel.removeAttribute("aria-modal");
      }
    }
    if (reportScrim) reportScrim.hidden = !(compactReportMedia.matches && shell.dataset.reportOpen === "true");
    if (contextSplitter) {
      const splitterAvailable = shell.dataset.reportOpen === "true" && !compactReportMedia.matches;
      contextSplitter.tabIndex = splitterAvailable ? 0 : -1;
      contextSplitter.setAttribute("aria-hidden", String(!splitterAvailable));
    }
    syncOverlayDocumentState();
    constrain();
  };
  const setRailCollapsed = (collapsed, persist = false) => {
    shell.dataset.railCollapsed = String(collapsed);
    if (collapsed) {
      const accountRoot = shell.querySelector("[data-account-menu-root]");
      const accountMenu = accountRoot?.querySelector(".rail-account-menu");
      const accountToggle = accountRoot?.querySelector("[data-account-menu-toggle]");
      if (accountMenu) accountMenu.hidden = true;
      accountToggle?.setAttribute("aria-expanded", "false");
      accountRoot?.closest(".workspace-rail")?.setAttribute("data-account-menu-open", "false");
    }
    if (railSplitter) {
      railSplitter.tabIndex = collapsed ? -1 : 0;
      railSplitter.setAttribute("aria-hidden", String(collapsed));
    }
    if (!collapsed) constrain();
    if (persist) {
      try { localStorage.setItem("optionhelper.workspace.railCollapsed", String(collapsed)); } catch { /* local persistence is optional */ }
    }
  };

  applyWidth("rail", readStored("rail", limits.rail.fallback));
  applyWidth("report", readStored("report", limits.report.fallback));
  setReportOpen(false);
  setTaskHistoryOpen(false);
  try { setRailCollapsed(document.documentElement.dataset.nativeShell === "macos" && localStorage.getItem("optionhelper.workspace.railCollapsed") === "true"); } catch { setRailCollapsed(false); }
  requestAnimationFrame(() => requestAnimationFrame(() => { shell.dataset.layoutReady = "true"; }));

  const bindSplitter = (splitter, kind) => {
    if (!splitter) return;
    splitter.addEventListener("pointerdown", (event) => {
      if (kind === "report" && shell.dataset.reportOpen !== "true") return;
      const startX = event.clientX;
      const startWidth = readWidth(kind);
      shell.dataset.resizing = "true";
      splitter.setPointerCapture?.(event.pointerId);
      const move = (next) => {
        const delta = next.clientX - startX;
        applyWidth(kind, startWidth + (kind === "rail" ? delta : -delta));
      };
      const stop = () => {
        shell.dataset.resizing = "false";
        saveStored(kind, readWidth(kind));
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop, { once: true });
      window.addEventListener("pointercancel", stop, { once: true });
    });
    splitter.addEventListener("keydown", (event) => {
      if (kind === "report" && shell.dataset.reportOpen !== "true") return;
      const step = event.shiftKey ? 40 : 16;
      if (event.key === "ArrowLeft") applyWidth(kind, readWidth(kind) + (kind === "rail" ? -step : step), true);
      else if (event.key === "ArrowRight") applyWidth(kind, readWidth(kind) + (kind === "rail" ? step : -step), true);
      else return;
      event.preventDefault();
    });
  };

  reportToggle?.addEventListener("click", () => setReportOpen(shell.dataset.reportOpen !== "true"));
  reportClose?.addEventListener("click", () => {
    setReportOpen(false);
  });
  reportScrim?.addEventListener("click", () => setReportOpen(false));
  taskHistoryToggle?.addEventListener("click", () => setTaskHistoryOpen(shell.dataset.taskHistoryOpen !== "true"));
  taskHistoryClose?.addEventListener("click", () => setTaskHistoryOpen(false));
  taskHistoryScrim?.addEventListener("click", () => setTaskHistoryOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented) return;
    if (event.key === "Escape" && shell.dataset.taskHistoryOpen === "true") {
      event.preventDefault();
      setTaskHistoryOpen(false);
    } else if (event.key === "Escape" && shell.dataset.reportOpen === "true") {
      event.preventDefault();
      setReportOpen(false);
    } else if (shell.dataset.taskHistoryOpen === "true") {
      trapPanelFocus(event, taskHistoryPanel);
    } else if (compactReportMedia.matches && shell.dataset.reportOpen === "true") {
      trapPanelFocus(event, contextPanel);
    }
  });
  window.addEventListener("optionhelper:toggle-task-rail", () => setRailCollapsed(shell.dataset.railCollapsed !== "true", true));
  bindSplitter(railSplitter, "rail");
  bindSplitter(contextSplitter, "report");
  compactReportMedia.addEventListener?.("change", syncResponsivePanels);
  mobileTaskMedia.addEventListener?.("change", syncResponsivePanels);
  window.addEventListener("resize", syncResponsivePanels);
  syncResponsivePanels();
}

export function renderTaskList(target, tasks, activeTaskId, onSelect, actions = {}) {
  if (!target) return;
  target._taskMenuCleanup?.();
  delete target._taskMenuCleanup;
  if (!tasks.length) {
    const empty = document.createElement("p");
    empty.className = "task-item task-item--empty";
    empty.textContent = "尚无任务";
    target.replaceChildren(empty);
    return;
  }
  const doc = target.ownerDocument;
  const createIcon = (paths) => {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = doc.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 16 16");
    icon.setAttribute("fill", "none");
    icon.setAttribute("aria-hidden", "true");
    paths.forEach((pathData) => {
      const path = doc.createElementNS(namespace, "path");
      path.setAttribute("d", pathData);
      path.setAttribute("fill", "currentColor");
      icon.append(path);
    });
    return icon;
  };
  const ellipsisIcon = () => createIcon([
    "M9.15001 3.40146C9.15001 4.03659 8.63513 4.55146 8.00001 4.55146C7.36488 4.55146 6.85001 4.03659 6.85001 3.40146C6.85001 2.76634 7.36488 2.25146 8.00001 2.25146C8.63513 2.25146 9.15001 2.76634 9.15001 3.40146Z",
    "M9.15001 8.00001C9.15001 8.63513 8.63513 9.15001 8.00001 9.15001C7.36488 9.15001 6.85001 8.63513 6.85001 8.00001C6.85001 7.36488 7.36488 6.85001 8.00001 6.85001C8.63513 6.85001 9.15001 7.36488 9.15001 8.00001Z",
    "M9.15001 12.5986C9.15001 13.2338 8.63513 13.7486 8.00001 13.7486C7.36488 13.7486 6.85001 13.2338 6.85001 12.5986C6.85001 11.9635 7.36488 11.4486 8.00001 11.4486C8.63513 11.4486 9.15001 11.9635 9.15001 12.5986Z",
  ]);
  const editIcon = () => createIcon([
    "M9.94076 1.34942C10.7047 0.90231 11.6503 0.902415 12.4143 1.34942C12.7061 1.52015 12.9688 1.79118 13.3104 2.13284C13.6521 2.47448 13.9231 2.73721 14.0939 3.02894C14.5408 3.79294 14.5409 4.73856 14.0939 5.50251C13.9231 5.79415 13.652 6.05704 13.3104 6.39861L6.65932 13.0497C6.28068 13.4284 6.00695 13.7108 5.66543 13.9097C5.32391 14.1085 4.94315 14.2074 4.42705 14.3498L3.24394 14.6761C2.77527 14.8054 2.34538 14.9262 2.00131 14.9684C1.65196 15.0112 1.17964 15.0013 0.810764 14.6325C0.441921 14.2637 0.432107 13.7913 0.47486 13.442C0.517035 13.0979 0.6379 12.668 0.767181 12.1993L1.09352 11.0162C1.23588 10.5001 1.33481 10.1193 1.5336 9.77784C1.7325 9.43632 2.0149 9.1626 2.39355 8.78395L9.04466 2.13284C9.38625 1.79126 9.64911 1.52016 9.94076 1.34942ZM15.5427 14.8398H7.55223L8.96707 13.425H15.5427V14.8398ZM3.39382 9.78422C2.965 10.213 2.84244 10.3436 2.75709 10.49C2.67183 10.6366 2.61862 10.8079 2.45733 11.3925L2.13099 12.5756C2.00183 13.0439 1.92194 13.3419 1.88863 13.5536C2.10041 13.5204 2.39872 13.4416 2.86764 13.3123L4.05075 12.9859C4.63544 12.8246 4.80669 12.7715 4.95323 12.6862C5.09968 12.6008 5.23022 12.4783 5.65905 12.0494L10.721 6.98644L8.45577 4.72121L3.39382 9.78422ZM11.7 2.57079C11.3774 2.38198 10.9777 2.38198 10.6551 2.57079C10.5602 2.62647 10.4487 2.72931 10.0449 3.13311L9.45604 3.72094L11.7213 5.98617L12.3102 5.39833C12.7139 4.99457 12.8168 4.88307 12.8725 4.78818C13.0613 4.46561 13.0612 4.06585 12.8725 3.74326C12.8169 3.64827 12.7146 3.53752 12.3102 3.13311C11.9057 2.72863 11.795 2.6264 11.7 2.57079Z",
  ]);
  const trashIcon = () => createIcon([
    "M14.4782 4.84067L14.2138 10.1152C14.1102 12.1872 14.067 13.0115 13.3866 13.9607C13.1044 14.3546 12.7498 14.6912 12.3424 14.9535C11.8239 15.2872 11.2415 15.4316 10.5585 15.4998C9.88727 15.5668 9.04946 15.5656 7.99998 15.5656C6.95051 15.5656 6.1127 15.5668 5.44142 15.4998C4.75851 15.4316 4.17602 15.2872 3.65753 14.9535C3.25012 14.6912 2.89559 14.3546 2.61332 13.9607C1.93296 13.0115 1.88979 12.1872 1.78619 10.1152L1.52179 4.84067L2.89006 4.77277L3.15343 10.0463C3.26221 12.2218 3.32452 12.6015 3.72646 13.1624C3.90825 13.4161 4.13686 13.6334 4.39927 13.8023C4.66204 13.9714 5.00263 14.0792 5.57825 14.1367C6.16562 14.1953 6.92298 14.1963 7.99998 14.1963C9.07699 14.1963 9.83434 14.1953 10.4217 14.1367C10.9973 14.0792 11.3379 13.9714 11.6007 13.8023C11.8631 13.6334 12.0917 13.4161 12.2735 13.1624C12.6755 12.6015 12.7378 12.2218 12.8465 10.0463L13.1099 4.77277L14.4782 4.84067ZM5.43011 6.22849H6.7994V11.3909H5.43011V6.22849ZM9.20056 6.22849H10.5699V11.3909H9.20056V6.22849ZM8.53597 0.434431C9.17976 0.434431 9.6522 0.426926 10.0966 0.571258C10.2357 0.616451 10.3717 0.672554 10.502 0.738948C10.9182 0.951107 11.2464 1.29099 11.7015 1.74612L12.4978 2.54136H15.3742V3.91169H0.625732V2.54136H3.50218L4.29845 1.74612C4.75358 1.29099 5.08174 0.951107 5.49801 0.738948C5.62831 0.672554 5.76425 0.616451 5.90334 0.571258C6.34776 0.426926 6.82021 0.434431 7.46399 0.434431H8.53597ZM7.46399 1.80476C6.73208 1.80476 6.51641 1.81187 6.32617 1.87369C6.25545 1.89667 6.18668 1.92533 6.12041 1.95907C5.96398 2.03878 5.82348 2.16253 5.44142 2.54136H10.5585C10.1765 2.16253 10.036 2.03878 9.87955 1.95907C9.81329 1.92533 9.74452 1.89667 9.6738 1.87369C9.48356 1.81187 9.26789 1.80476 8.53597 1.80476H7.46399Z",
  ]);
  let openMenu = null;
  const taskMenuItems = (menu) => Array.from(menu?.querySelectorAll('[role="menuitem"]:not([disabled])') || []);
  const focusTaskMenuItem = (menu, position) => {
    const items = taskMenuItems(menu);
    if (!items.length) return;
    const activeIndex = items.indexOf(doc.activeElement);
    if (position === "first") items[0].focus();
    else if (position === "last") items[items.length - 1].focus();
    else items[(activeIndex + position + items.length) % items.length].focus();
  };
  const closeMenu = ({ restoreFocus = false } = {}) => {
    if (!openMenu) return;
    const { menu, row, trigger } = openMenu;
    menu.hidden = true;
    menu.removeAttribute("style");
    if (!row.contains(menu)) row.append(menu);
    trigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) trigger.focus();
    openMenu = null;
  };
  const onDocumentPointerDown = (event) => {
    if (openMenu && !openMenu.row.contains(event.target) && !openMenu.menu.contains(event.target)) closeMenu();
  };
  const onDocumentKeyDown = (event) => {
    if (!openMenu) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu({ restoreFocus: true });
    } else if (openMenu.menu.contains(event.target) && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();
      focusTaskMenuItem(openMenu.menu, event.key === "ArrowDown" ? 1 : -1);
    } else if (openMenu.menu.contains(event.target) && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      focusTaskMenuItem(openMenu.menu, event.key === "Home" ? "first" : "last");
    }
  };
  const onDocumentFocusIn = (event) => {
    if (openMenu && event.target !== openMenu.trigger && !openMenu.menu.contains(event.target)) closeMenu();
  };
  const positionOpenMenu = () => {
    if (!openMenu) return;
    const { menu, trigger } = openMenu;
    const rect = trigger.getBoundingClientRect();
    const margin = 12;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const viewportWidth = doc.defaultView?.innerWidth || doc.documentElement.clientWidth;
    const viewportHeight = doc.defaultView?.innerHeight || doc.documentElement.clientHeight;
    const left = Math.min(Math.max(rect.right - width, margin), viewportWidth - width - margin);
    const openBelow = viewportHeight - rect.bottom >= height + 4 || rect.top < height + 4;
    const top = openBelow ? rect.bottom + 4 : rect.top - height - 4;
    menu.style.left = `${left}px`;
    menu.style.top = `${Math.min(Math.max(top, margin), viewportHeight - height - margin)}px`;
  };
  const onViewportChange = () => positionOpenMenu();
  doc.addEventListener("pointerdown", onDocumentPointerDown);
  doc.addEventListener("keydown", onDocumentKeyDown);
  doc.addEventListener("focusin", onDocumentFocusIn);
  doc.addEventListener("scroll", onViewportChange, true);
  doc.defaultView?.addEventListener("resize", onViewportChange);
  target._taskMenuCleanup = () => {
    closeMenu();
    doc.removeEventListener("pointerdown", onDocumentPointerDown);
    doc.removeEventListener("keydown", onDocumentKeyDown);
    doc.removeEventListener("focusin", onDocumentFocusIn);
    doc.removeEventListener("scroll", onViewportChange, true);
    doc.defaultView?.removeEventListener("resize", onViewportChange);
  };
  const entries = tasks.map((task) => {
    const row = doc.createElement("div");
    row.className = "task-list__row";
    const button = doc.createElement("button");
    button.className = "task-item";
    button.type = "button";
    button.dataset.taskId = String(task.task_id || "");
    button.setAttribute("aria-current", String(task.task_id === activeTaskId));
    const taskTitle = document.createElement("strong");
    taskTitle.className = "task-item__title";
    const titleText = doc.createElement("span");
    titleText.textContent = String(task.subject || "新建研究任务");
    taskTitle.append(titleText);
    if (Array.isArray(task.active_operations) && task.active_operations.length) {
      const running = doc.createElement("span");
      running.className = "task-item__running-dot";
      running.setAttribute("aria-label", "正在运行");
      running.title = "正在运行";
      taskTitle.append(running);
    }
    const taskDate = document.createElement("small");
    taskDate.textContent = formatTaskDate(task.updated_at || task.created_at);
    button.append(taskTitle, taskDate);
    button.addEventListener("click", () => onSelect(button.dataset.taskId));
    row.append(button);
    if (typeof actions.onRename === "function" || typeof actions.onDelete === "function") {
      const targetScope = String(target.id || "tasks").replace(/[^A-Za-z0-9_-]/g, "-");
      const menuId = `task-menu-${targetScope}-${task.task_id}`;
      const trigger = doc.createElement("button");
      trigger.type = "button";
      trigger.className = "task-menu-trigger";
      trigger.setAttribute("aria-label", `管理任务：${taskTitle.textContent}`);
      trigger.setAttribute("aria-haspopup", "menu");
      trigger.setAttribute("aria-controls", menuId);
      trigger.setAttribute("aria-expanded", "false");
      trigger.append(ellipsisIcon());
      const menu = doc.createElement("div");
      menu.id = menuId;
      menu.className = "task-menu";
      menu.setAttribute("role", "menu");
      menu.hidden = true;
      const addAction = (label, className, handler, icon) => {
        if (typeof handler !== "function") return;
        const item = doc.createElement("button");
        item.type = "button";
        item.className = className;
        item.setAttribute("role", "menuitem");
        const iconSlot = doc.createElement("span");
        iconSlot.className = "task-menu__icon";
        iconSlot.append(icon());
        const labelSlot = doc.createElement("span");
        labelSlot.className = "task-menu__label";
        labelSlot.textContent = label;
        item.append(iconSlot, labelSlot);
        item.addEventListener("click", (event) => {
          event.stopPropagation();
          closeMenu();
          handler(task);
        });
        menu.append(item);
      };
      addAction("重命名", "task-menu__action", actions.onRename, editIcon);
      addAction("删除任务", "task-menu__action task-menu__action--danger", actions.onDelete, trashIcon);
      trigger.addEventListener("click", (event) => {
        event.stopPropagation();
        if (openMenu?.trigger === trigger) {
          closeMenu({ restoreFocus: true });
          return;
        }
        closeMenu();
        menu.hidden = false;
        doc.body.append(menu);
        menu.style.position = "fixed";
        trigger.setAttribute("aria-expanded", "true");
        openMenu = { row, menu, trigger };
        positionOpenMenu();
      });
      trigger.addEventListener("keydown", (event) => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        if (menu.hidden) trigger.click();
        focusTaskMenuItem(menu, ["ArrowUp", "End"].includes(event.key) ? "last" : "first");
      });
      row.append(trigger, menu);
    }
    return row;
  });
  target.replaceChildren(...entries);
}

function formatTaskDate(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "未命名任务";
  const part = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}/${part(date.getMonth() + 1)}/${part(date.getDate())}`;
}

export function bindComposerKeyboard(form) {
  const textarea = form?.elements?.content;
  if (!textarea || textarea.dataset.keyboardReady === "true") return;
  textarea.dataset.keyboardReady = "true";
  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    form.requestSubmit();
  });
}

export function reasoningSummary(text, running = false) {
  return running ? "正在思考" : "思考过程";
}

const reasoningControllers = new WeakMap();

export function createReasoningDisclosure(text = "", { running = false } = {}) {
  const details = document.createElement("details");
  details.className = "assistant-reasoning";
  details.open = true;
  const summary = document.createElement("summary");
  const orb = createThinkingOrb({ state: "solving", size: 28, paused: !running });
  orb.element.setAttribute("aria-hidden", "true");
  const title = document.createElement("span");
  title.className = "assistant-reasoning__title";
  const chevron = document.createElement("span");
  chevron.className = "assistant-reasoning__chevron";
  chevron.setAttribute("aria-hidden", "true");
  const body = document.createElement("div");
  body.className = "assistant-reasoning__body";
  summary.append(orb.element, title, chevron);
  details.append(summary, body);
  const update = (nextText, nextRunning = false) => {
    const value = String(nextText ?? "");
    details.dataset.running = String(nextRunning);
    title.textContent = reasoningSummary(value, nextRunning);
    body.textContent = value;
    details.hidden = !value && !nextRunning;
    // Stopping preserves the current visual and phase; only velocity settles.
    if (nextRunning) orb.setState("solving");
    orb.setPaused(!nextRunning);
    // The user's disclosure choice survives every streamed delta and completion.
    // Start expanded; updates never override the user's disclosure choice.
  };
  update(text, running);
  const controller = { element: details, body, update, destroy: orb.destroy };
  reasoningControllers.set(details, controller);
  return controller;
}

function createAttachmentReference(reference, options = {}) {
  const kind = reference?.kind === "image" ? "image" : "document";
  const name = String(reference?.name || (kind === "image" ? "图片附件" : "研究文档"));
  const button = document.createElement("button");
  button.type = "button";
  button.className = `message-attachment message-attachment--${kind}`;
  button.setAttribute("aria-label", `${kind === "image" ? "预览" : "下载"}${name}`);
  const mark = document.createElement("span");
  mark.className = "message-attachment__mark";
  mark.textContent = kind === "image" ? "图" : "文";
  const label = document.createElement("span");
  label.className = "message-attachment__label";
  label.textContent = name;
  button.append(mark, label);
  button.addEventListener("click", () => options.onAttachmentOpen?.(reference));
  return button;
}

function safeArtifactReference(text) {
  const match = String(text || "").match(/\/api\/reports\/([^\s/?#]+)\/artifacts\/([^\s?#]+)/);
  if (!match) return null;
  let reportRunId;
  let artifactName;
  try {
    reportRunId = decodeURIComponent(match[1]);
    artifactName = decodeURIComponent(match[2]);
  } catch {
    return null;
  }
  if (!/^[A-Za-z0-9._:-]{1,160}$/.test(reportRunId)
    || !artifactName
    || artifactName.startsWith("/")
    || artifactName.split("/").some((part) => !part || part === "." || part === "..")) return null;
  const encodedName = artifactName.split("/").map(encodeURIComponent).join("/");
  return {
    path: `/api/reports/${encodeURIComponent(reportRunId)}/artifacts/${encodedName}`,
    name: artifactDisplayName(artifactName.split("/").at(-1) || "交付文件"),
  };
}

function artifactDisplayName(name) {
  const raw = String(name || "交付文件");
  const lower = raw.toLowerCase();
  const extension = lower.endsWith(".pdf") ? ".pdf" : lower.endsWith(".html") ? ".html" : "";
  if (/(^|[-_.])multicard([-_.]|$)/.test(lower)) return `多结构研究简报${extension}`;
  if (/(^|[-_.])multireport([-_.]|$)/.test(lower)) return `多结构完整研究报告${extension}`;
  return raw;
}

export function reportPresentation(report = {}) {
  const outputType = report.output_type || report.report_request?.output_type || report.report_request?.kind;
  const sourceArtifacts = Array.isArray(report.artifacts)
    ? report.artifacts
    : (Array.isArray(report.artifact_manifest) ? report.artifact_manifest : []);
  const artifacts = sourceArtifacts
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const fallbackName = String(item.name || "").split("/").at(-1) || "交付文件";
      return {
        ...item,
        display_name: artifactDisplayName(String(item.display_name || "").trim() || fallbackName),
      };
    });
  const deliveryMode = report.delivery_mode
    || report.report_request?.subject_ref?.delivery_mode
    || report.report_request?.delivery_mode
    || "single";
  const legacyComparison = deliveryMode === "comparison"
    || artifacts.some((item) => /(^|\/)(candidate-|multicard(?:[-_.]|$)|multireport(?:[-_.]|$))/i.test(String(item?.name || "")));
  const comparison = typeof report.comparison === "boolean" ? report.comparison : legacyComparison;
  const templateLabel = comparison && outputType === "card" ? "多结构研究简报"
    : comparison && outputType === "report" ? "多结构完整研究报告"
    : comparison && outputType === "quote" ? "多结构参考报价"
    : ({ card: "简单报告", quote: "参考报价", report: "详细报告" })[outputType] || "交付物";
  const preferred = artifacts.find((item) => item.name?.endsWith(".html"))
    || artifacts.find((item) => item.name?.endsWith(".pdf"))
    || artifacts[0];
  const title = report.title || report.report_request?.metadata?.title || templateLabel;
  return { outputType, deliveryMode, comparison, title, artifacts, preferred };
}

export function setMessageActionIcon(control, label, path) {
  control.setAttribute("aria-label", label);
  control.title = label;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  svg.append(shape);
  control.replaceChildren(svg);
}

function createMessageArtifactCard(reference) {
  const card = document.createElement("section");
  card.className = "message-artifact";
  const heading = document.createElement("div");
  heading.className = "message-artifact__heading";
  const title = document.createElement("strong");
  title.textContent = reference.name;
  const format = document.createElement("span");
  format.textContent = reference.format === "docx" ? "Word" : reference.format?.toUpperCase() || (reference.name.toLowerCase().endsWith(".pdf") ? "PDF" : "HTML");
  heading.append(title, format);
  const actions = document.createElement("div");
  actions.className = "message-artifact__actions";
  const preview = document.createElement("a");
  preview.href = reference.path;
  preview.target = "_blank";
  preview.rel = "noopener";
  setMessageActionIcon(preview, "预览报告", "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Zm13 0a3 3 0 1 0-6 0 3 3 0 0 0 6 0Z");
  const download = document.createElement("a");
  download.href = reference.downloadUrl || `${reference.path}?download=1`;
  download.target = "_blank";
  download.rel = "noopener";
  setMessageActionIcon(download, "下载报告", "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5");
  actions.append(preview, download);
  if (reference.reportId) {
    const edit = document.createElement("a");
    edit.href = `/reports/${encodeURIComponent(reference.reportId)}/edit`;
    edit.target = "_blank"; edit.rel = "noopener";
    setMessageActionIcon(edit, "编辑报告", "m15 5 4 4M4 20l5-1L21 7a2.8 2.8 0 0 0-4-4L5 15l-1 5Z");
    actions.append(edit);
  }
  const mark = document.createElement("span");
  mark.className = "message-artifact__mark";
  setMessageActionIcon(mark, "报告文件", "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8M8 17h5");
  mark.removeAttribute("aria-label");
  mark.removeAttribute("title");
  mark.setAttribute("aria-hidden", "true");
  card.append(mark, heading, actions);
  return card;
}

function createQuestionCard(block, options, active) {
  const card = document.createElement("form");
  card.className = "message-question";
  card.dataset.questionId = String(block.question_id || "");
  const prompt = document.createElement("p");
  prompt.className = "message-question__prompt";
  prompt.textContent = String(block.prompt || block.text || "请补充以下信息。");
  card.append(prompt);
  const optionRows = Array.isArray(block.options) ? block.options.slice(0, 3) : [];
  if (optionRows.length) {
    const choices = document.createElement("div");
    choices.className = "message-question__choices";
    for (const row of optionRows) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "message-question__choice";
      button.disabled = !active;
      const label = document.createElement("strong");
      label.textContent = String(row?.label || "选择此项");
      if (row?.recommended === true) {
        const badge = document.createElement("span");
        badge.textContent = "推荐";
        label.append(badge);
      }
      const description = document.createElement("span");
      description.textContent = String(row?.description || "");
      button.append(label, description);
      button.addEventListener("click", () => {
        if (active) options.onQuestionAnswer?.(String(row?.value || row?.label || ""), block.question_id);
      });
      choices.append(button);
    }
    card.append(choices);
  }
  if (block.allow_free_text === true) {
    const free = document.createElement("div");
    free.className = "message-question__free";
    const field = document.createElement("textarea");
    field.rows = 2;
    field.maxLength = 2_000;
    field.disabled = !active;
    field.placeholder = active ? "补充你的要求" : "该问题已处理";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = active ? "发送" : "已回答";
    submit.disabled = !active;
    free.append(field, submit);
    card.append(free);
    card.addEventListener("submit", (event) => {
      event.preventDefault();
      const answer = field.value.trim();
      if (active && answer) options.onQuestionAnswer?.(answer, block.question_id);
    });
  }
  return card;
}

function createConversationMessage(entry, options = {}) {
  const article = document.createElement("article");
  const user = entry?.role === "user";
  article.className = `message message--${user ? "user" : "assistant"}`;
  const messageText = String(entry?.content ?? "");
  const actions = document.createElement("div");
  actions.className = "message__actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "message__action";
  const copyLabel = user ? "复制我的消息" : "复制回复";
  const copyPath = "M9 9h12v12H9ZM5 15H3V3h12v2";
  setMessageActionIcon(copy, copyLabel, copyPath);
  const copyStatus = document.createElement("span");
  copyStatus.className = "message__copy-status";
  copyStatus.setAttribute("role", "status");
  let resetCopy = 0;
  copy.addEventListener("click", async () => {
    if (copy.dataset.busy === "true") return;
    copy.dataset.busy = "true";
    let copied = false;
    try {
      await navigator.clipboard.writeText(messageText);
      copied = true;
    } catch {
      const field = document.createElement("textarea");
      field.value = messageText;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      try {
        field.select();
        copied = document.execCommand("copy");
      } catch {
        copied = false;
      } finally {
        field.remove();
        copy.focus({ preventScroll: true });
      }
    }
    copy.dataset.busy = "false";
    if (!copied) {
      copyStatus.textContent = "复制失败，请重试";
      copy.title = "复制失败，请重试";
      return;
    }
    window.clearTimeout(resetCopy);
    setMessageActionIcon(copy, "已复制", "m5 12 4 4L19 6");
    copyStatus.textContent = "已复制";
    copy.dataset.copied = "true";
    resetCopy = window.setTimeout(() => {
      setMessageActionIcon(copy, copyLabel, copyPath);
      copyStatus.textContent = "";
      copy.dataset.copied = "false";
    }, 1000);
  });
  actions.append(copy, copyStatus);
  const createdAt = Date.parse(String(entry?.created_at ?? ""));
  if (Number.isFinite(createdAt)) {
    const time = document.createElement("time");
    time.className = "message__time";
    time.dateTime = new Date(createdAt).toISOString();
    time.textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(createdAt);
    actions.append(time);
  }
  if (user) {
    const body = document.createElement("p");
    body.className = "message__body";
    body.textContent = messageText;
    if (messageText) article.append(body);
    const attachments = Array.isArray(entry?.attachments) ? entry.attachments : [];
    if (attachments.length) {
      const list = document.createElement("div");
      list.className = "message-attachments";
      list.append(...attachments.map((reference) => createAttachmentReference(reference, options)));
      article.append(list);
    }
    article.append(actions);
    return article;
  }
  const content = document.createElement("div");
  content.className = "message__content";
  const rawBlocks = Array.isArray(entry?.content_blocks) ? entry.content_blocks : [];
  const blocks = rawBlocks.length > 0 ? rawBlocks : [{ type: "text", text: String(entry?.content ?? "") }];
  let messageReasoning = null;
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    const type = String(block.type || "").replaceAll("_", "-");
    const text = String(block.text ?? "");
    if (type === "document" && /^[A-Za-z0-9._:-]+$/.test(block.report_run_id || "")) {
      const prefix = `/api/reports/${encodeURIComponent(block.report_run_id)}/`;
      if (String(block.preview_url || "").startsWith(prefix) && String(block.download_url || "").startsWith(prefix)) {
        content.append(createMessageArtifactCard({name:block.title, path:block.preview_url, downloadUrl:block.download_url, format:block.format, reportId:block.report_run_id}));
      }
    } else if (type === "reasoning" && text) {
      if (!messageReasoning) {
        messageReasoning = createReasoningDisclosure(text);
        content.append(messageReasoning.element);
      } else {
        messageReasoning.update(`${messageReasoning.body.textContent}\n\n${text}`);
      }
    } else if (type === "text" && text) {
      if (blocks.some(item => item?.type === "question" && String(item.prompt || "").trim() === text.trim())) continue;
      const body = document.createElement("p");
      body.className = "message__body";
      body.textContent = text;
      content.append(body);
    } else if (type === "question") {
      content.append(createQuestionCard(block, options, options.questionActive === true));
    }
  }
  const artifact = safeArtifactReference(messageText);
  if (artifact && !blocks.some(block => block?.type === "document")) content.append(createMessageArtifactCard(artifact));
  article.append(content, actions);
  return article;
}

export function renderMessages(target, messages, emptyText = "输入任务要求后开始对话。", options = {}) {
  if (!target) return;
  target.closest(".chat-surface")?.classList.toggle("chat-surface--empty", !messages?.length);
  if (!messages?.length) {
    target.innerHTML = `<section class="conversation-start"><div><div class="conversation-start__mark" aria-hidden="true"><img src="/capability/assets/icons/optionhelper-app-icon-tile-light.svg" alt=""></div><h2>开始一项结构化产品研究</h2><p class="conversation-start__copy">直接说想研究什么，不必记住产品编号或填完参数。</p><div class="conversation-starters"><button class="conversation-starter" type="button" data-starter-prompt="我想了解一种期权产品的收益与风险，请帮我从产品特点开始。"><strong>了解产品</strong><span>看懂收益、风险和适用条件。</span></button><button class="conversation-starter" type="button" data-starter-prompt="请根据我的标的、期限和风险偏好筛选合适的期权结构。"><strong>筛选结构</strong><span>从标的、期限和风险偏好出发。</span></button><button class="conversation-starter" type="button" data-starter-prompt="我想为已有产品定价或回测，请帮我确认需要的条款与数据。"><strong>定价与回测</strong><span>使用已有条款，补齐必要的数据。</span></button><button class="conversation-starter" type="button" data-starter-prompt="请把当前任务已有的研究结果整理成报告。"><strong>整理报告</strong><span>汇总已有分析，继续编辑或导出。</span></button></div><p class="empty">选择一个起点，或直接在下方输入；缺少的信息会在需要时向你确认。</p></div></section>`;
    return;
  }
  let activeQuestionIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const entry = messages[index];
    if (entry?.role === "user") break;
    if (entry?.role === "assistant") {
      if (Array.isArray(entry.content_blocks)
        && entry.content_blocks.some((block) => block?.type === "question")) activeQuestionIndex = index;
      break;
    }
  }
  const previousReasoning = [...target.querySelectorAll(".assistant-reasoning")];
  const rendered = messages.map((entry, index) => createConversationMessage(entry, {
    ...options,
    questionActive: index === activeQuestionIndex,
  }));
  for (const article of rendered) {
    for (const next of article.querySelectorAll(".assistant-reasoning")) {
      const text = reasoningControllers.get(next)?.body.textContent;
      const index = previousReasoning.findIndex(node => reasoningControllers.get(node)?.body.textContent === text);
      if (index < 0) continue;
      const [previous] = previousReasoning.splice(index, 1);
      reasoningControllers.get(next)?.destroy();
      reasoningControllers.get(previous)?.update(text, false);
      next.replaceWith(previous);
    }
  }
  target.replaceChildren(...rendered);
  target.scrollTop = target.scrollHeight;
}

export function renderReports(target, reports) {
  if (!target) return;
  if (!reports?.length) {
    target.innerHTML = '<p class="report-empty">当前任务尚无报告。</p>';
    return;
  }
  target.replaceChildren(...reports.map((report) => {
    const { title, preferred } = reportPresentation(report);
    const card = document.createElement("article");
    card.className = "report-item";
    const heading = document.createElement("div");
    heading.className = "report-item__heading";
    const name = document.createElement("strong");
    name.textContent = title;
    const meta = document.createElement("span");
    meta.className = "report-meta";
    const status = String(report.status || "");
    meta.textContent = status === "succeeded" ? "已完成"
      : status === "partial" ? "部分完成"
        : status || "状态未知";
    meta.dataset.state = status;
    heading.append(name, meta);
    card.append(heading);
    if (preferred?.name) {
      const encodedArtifactName = String(preferred.name).split("/").map(encodeURIComponent).join("/");
      const artifactPath = preferred.url || `/api/reports/${encodeURIComponent(report.report_run_id)}/artifacts/${encodedArtifactName}`;
      const actions = document.createElement("div");
      actions.className = "report-item__actions";
      const preview = document.createElement("a");
      preview.href = artifactPath;
      preview.target = "_blank";
      preview.rel = "noopener";
      preview.textContent = "预览";
      preview.setAttribute("aria-label", `预览${title}`);
      const download = document.createElement("a");
      download.href = `${artifactPath}?download=1#display_name=${encodeURIComponent(preferred.display_name || title)}`;
      download.target = "_blank";
      download.rel = "noopener";
      download.textContent = "下载";
      download.setAttribute("aria-label", `下载${title}`);
      actions.append(preview, download);
      card.append(actions);
    }
    return card;
  }));
}

export function taskIdFromLocation() {
  return new URLSearchParams(location.search).get("task") || "";
}

export function setTaskLocation(taskId, extra = {}) {
  const next = new URL(location.href);
  next.searchParams.set("task", taskId);
  Object.entries(extra).forEach(([key, value]) => {
    if (value) next.searchParams.set(key, value);
    else next.searchParams.delete(key);
  });
  history.replaceState(null, "", next);
}
