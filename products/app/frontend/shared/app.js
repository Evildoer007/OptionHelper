const secretKeys = new Set(["password", "token", "api_key", "secret", "secret_value", "private_key"]);

export async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || body.reason || body.next_step || body.error?.next_step || body.error || "请求未完成");
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

export function escapeText(value) {
  const element = document.createElement("span");
  element.textContent = String(value || "");
  return element.innerHTML;
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
  const viewportHeight = choice.ownerDocument.defaultView?.innerHeight || document.documentElement.clientHeight;
  const desiredHeight = Math.min(menu.scrollHeight || 0, 280) + 14;
  const roomBelow = viewportHeight - rect.bottom;
  const roomAbove = rect.top;
  choice.dataset.placement = roomBelow < desiredHeight && roomAbove > roomBelow ? "top" : "bottom";
}

function setChoiceOpen(choice, open, { focus = false } = {}) {
  const trigger = choice.querySelector(".choice-trigger");
  const menu = choice.querySelector(".choice-menu");
  if (!trigger || !menu || (trigger.disabled && open)) return;
  choice.dataset.open = String(open);
  trigger.setAttribute("aria-expanded", String(open));
  menu.hidden = !open;
  if (open) positionChoiceMenu(choice);
  if (open && focus) {
    const selected = menu.querySelector('[role="option"][aria-selected="true"]:not([disabled])') || menu.querySelector('[role="option"]:not([disabled])');
    selected?.focus();
  } else if (!open && focus) {
    trigger.focus();
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
  menu.replaceChildren(...Array.from(select.options).map((option) => {
    const item = doc.createElement("button");
    item.type = "button";
    item.className = "choice-option";
    item.setAttribute("role", "option");
    item.dataset.value = option.value;
    item.textContent = option.textContent;
    item.disabled = option.disabled;
    item.setAttribute("aria-selected", String(option.selected));
    item.tabIndex = -1;
    item.addEventListener("click", () => {
      if (option.disabled) return;
      select.value = option.value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      setChoiceOpen(choice, false);
      trigger.focus();
    });
    return item;
  }));
  if (trigger.disabled) setChoiceOpen(choice, false);
}

function moveChoice(select, direction) {
  const enabled = Array.from(select.options).filter((option) => !option.disabled);
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
        if (event.target.closest("[data-choice-root]") || event.target === select) return;
        event.preventDefault();
        trigger.focus();
      });
    }
    const close = (focusTrigger = false) => setChoiceOpen(choice, false, { focus: focusTrigger });
    trigger.addEventListener("click", () => setChoiceOpen(choice, !isChoiceOpen(choice), { focus: isChoiceOpen(choice) === false }));
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (isChoiceOpen(choice)) moveChoice(select, event.key === "ArrowDown" ? 1 : -1);
        else setChoiceOpen(choice, true, { focus: true });
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        const options = Array.from(select.options).filter((option) => !option.disabled);
        const option = options[event.key === "Home" ? 0 : options.length - 1];
        if (option) { select.value = option.value; select.dispatchEvent(new Event("change", { bubbles: true })); }
      } else if (event.key === "Escape") {
        close();
      } else if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        setChoiceOpen(choice, !isChoiceOpen(choice), { focus: !isChoiceOpen(choice) });
      }
    });
    menu.addEventListener("keydown", (event) => {
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
        close();
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
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
  }
}

export async function initializeWorkspace(mode, { onModeChange } = {}) {
  const session = await request("/api/me");
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
  if (settingsLink) {
    settingsLink.href = "/settings";
    settingsLink.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || (event.button !== undefined && event.button !== 0)) return;
      event.preventDefault();
      shell.dataset.navigating = "settings";
      window.setTimeout(() => { location.href = settingsLink.href; }, 130);
    });
  }

  const accountMenuRoot = shell.querySelector("[data-account-menu-root]");
  const accountMenuToggle = accountMenuRoot?.querySelector("[data-account-menu-toggle]");
  const accountMenu = accountMenuRoot?.querySelector(".rail-account-menu");
  const logoutButton = accountMenuRoot?.querySelector("[data-logout]");
  const setAccountMenuOpen = (open, { focus = false } = {}) => {
    if (!accountMenu || !accountMenuToggle) return;
    accountMenu.hidden = !open;
    accountMenuToggle.setAttribute("aria-expanded", String(open));
    if (open && focus) accountMenu.querySelector('[role="menuitem"]')?.focus();
  };
  if (accountMenuRoot && accountMenuToggle && accountMenu && accountMenuRoot.dataset.accountMenuReady !== "true") {
    accountMenuRoot.dataset.accountMenuReady = "true";
    accountMenuToggle.addEventListener("click", () => setAccountMenuOpen(accountMenu.hidden));
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
      location.assign("/");
    } catch {
      logoutButton.disabled = false;
      logoutButton.title = "退出未完成，请稍后重试。";
    }
  });
  enhanceSelects(document);
  installLayoutControls(shell);
  return session;
}

export async function configureModelPicker(picker) {
  if (!picker) return;
  const { settings } = await request("/api/settings");
  const providerName = String(settings?.model_service?.provider_name || "").trim();
  const modelName = String(settings?.model_service?.model_name || "").trim();
  const configured = providerName && providerName !== "unconfigured";
  const option = document.createElement("option");
  option.value = configured ? providerName : "";
  option.textContent = configured ? (modelName ? `${providerName} / ${modelName}` : providerName) : "未配置模型";
  picker.replaceChildren(option);
  picker.disabled = !configured;
  enhanceSelects(picker.parentElement || document);
}

function installLayoutControls(shell) {
  if (shell.dataset.layoutControlsReady === "true") return;
  shell.dataset.layoutControlsReady = "true";
  const railToggle = shell.querySelector("[data-rail-collapse-toggle]");
  const reportToggle = shell.querySelector("[data-report-toggle]");
  const reportClose = shell.querySelector("[data-report-close]");
  const contextPanel = shell.querySelector("#task-context");
  const railSplitter = shell.querySelector('[data-workspace-splitter="rail"]');
  const contextSplitter = shell.querySelector('[data-workspace-splitter="context"]');
  const limits = {
    rail: { variable: "--rail-width", minimum: 176, maximum: 384, fallback: 248 },
    report: { variable: "--report-width", minimum: 280, maximum: 480, fallback: 332 },
  };
  const readStored = (key, fallback) => {
    try {
      const value = Number(localStorage.getItem(`optionhelper.workspace.${key}`));
      return Number.isFinite(value) ? value : fallback;
    } catch { return fallback; }
  };
  const saveStored = (key, value) => {
    try { localStorage.setItem(`optionhelper.workspace.${key}`, String(Math.round(value))); } catch { /* local persistence is optional */ }
  };
  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  const maximumFor = (kind) => {
    const current = limits[kind];
    const reportWidth = shell.dataset.reportOpen === "true" ? readWidth("report") : 0;
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
  const setReportOpen = (open) => {
    shell.dataset.reportOpen = String(open);
    reportToggle?.setAttribute("aria-expanded", String(open));
    reportToggle?.classList.toggle("is-active", open);
    if (contextPanel) {
      contextPanel.setAttribute("aria-hidden", String(!open));
      contextPanel.inert = !open;
    }
    if (contextSplitter) {
      contextSplitter.tabIndex = open ? 0 : -1;
      contextSplitter.setAttribute("aria-hidden", String(!open));
    }
    if (open) constrain();
  };
  const setRailCollapsed = (collapsed, persist = false) => {
    shell.dataset.railCollapsed = String(collapsed);
    railToggle?.setAttribute("aria-expanded", String(!collapsed));
    railToggle?.setAttribute("aria-label", collapsed ? "展开任务栏" : "收起任务栏");
    railToggle?.setAttribute("title", collapsed ? "展开任务栏" : "收起任务栏");
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
  try { setRailCollapsed(localStorage.getItem("optionhelper.workspace.railCollapsed") === "true"); } catch { setRailCollapsed(false); }
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
    reportToggle?.focus();
  });
  railToggle?.addEventListener("click", () => setRailCollapsed(shell.dataset.railCollapsed !== "true", true));
  bindSplitter(railSplitter, "rail");
  bindSplitter(contextSplitter, "report");
  window.addEventListener("resize", constrain);
}

export function renderTaskList(target, tasks, activeTaskId, onSelect) {
  if (!target) return;
  if (!tasks.length) {
    target.innerHTML = '<p class="task-item task-item--empty">尚无任务</p>';
    return;
  }
  target.innerHTML = tasks.map((task) => `
    <button class="task-item" type="button" data-task-id="${escapeText(task.task_id)}" aria-current="${task.task_id === activeTaskId}">
      <strong class="task-item__title">${escapeText(task.subject)}</strong>
      <small>${escapeText(formatTaskDate(task.updated_at || task.created_at))}</small>
      <span class="task-item__more" aria-hidden="true">⋮</span>
    </button>`).join("");
  target.querySelectorAll("[data-task-id]").forEach((button) => {
    button.addEventListener("click", () => onSelect(button.dataset.taskId));
  });
}

function formatTaskDate(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "本机任务";
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

export function renderMessages(target, messages, emptyText = "输入任务要求后开始对话。") {
  if (!target) return;
  target.closest(".chat-surface")?.classList.toggle("chat-surface--empty", !messages?.length);
  if (!messages?.length) {
    target.innerHTML = `<section class="conversation-start"><div class="conversation-start__mark"><img src="/capability/assets/icons/optionhelper-mark.svg" alt=""></div><div><h2>开始一项结构化产品研究</h2><p class="conversation-start__copy">先描述研究目标，随后可进入条款、估值、回测和报告。</p><div class="conversation-starters"><button class="conversation-starter" type="button" data-starter-prompt="请根据我的标的、期限和风险偏好筛选合适的期权结构。"><strong>筛选候选结构</strong><span>根据标的、期限与风险约束缩小选择范围。</span></button><button class="conversation-starter" type="button" data-starter-prompt="请帮助我设计期权产品条款。"><strong>设计产品条款</strong><span>从执行价、障碍和票息整理合约条款。</span></button><button class="conversation-starter" type="button" data-starter-prompt="请列出本次估值需要的市场与模型输入。"><strong>准备估值输入</strong><span>梳理估值日、现价、波动率和利率假设。</span></button><button class="conversation-starter" type="button" data-starter-prompt="请建立本次期权结构的历史回测方案。"><strong>建立回测方案</strong><span>定义样本区间、入场规则和观察口径。</span></button></div><p class="conversation-start__note">${escapeText(emptyText)}</p></div></section>`;
    return;
  }
  target.innerHTML = messages.map((entry) => `
    <article class="message message--${entry.role === "user" ? "user" : "assistant"}">
      <p class="message__body">${escapeText(entry.content)}</p>
    </article>`).join("");
  target.scrollTop = target.scrollHeight;
}

export function renderReports(target, reports) {
  if (!target) return;
  if (!reports?.length) {
    target.innerHTML = '<p class="report-empty">当前任务尚无报告。</p>';
    return;
  }
  target.innerHTML = reports.map((report) => {
    const outputType = report.report_request?.output_type || report.report_request?.kind;
    const title = outputType === "card" ? "Card简报" : "详细报告";
    const artifact = report.artifact_manifest?.find?.((item) => item.name?.endsWith(".html"));
    const href = artifact ? `/api/reports/${encodeURIComponent(report.report_run_id)}/artifacts/${encodeURIComponent(artifact.name)}` : "#";
    return `<a class="report-item" href="${href}" target="_blank" rel="noopener"><strong>${title}</strong><span class="report-meta">${escapeText(report.status || "已生成")}</span></a>`;
  }).join("");
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
