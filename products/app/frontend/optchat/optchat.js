import { bindComposerKeyboard, clearMessage, configureModelPicker, initializeWorkspace, message, renderMessages, renderReports, renderTaskList, request, safeJson, setTaskLocation, taskIdFromLocation } from "/app/frontend/shared/app.js";
import { createTransitionScope } from "/app/frontend/shared/transition-scope.js";

const modules = new Map([["datafetcher", "数据获取"], ["payoffer", "收益结构"], ["pricer", "估值定价"], ["backtester", "历史回测"], ["reporter", "研究报告"]]);
const thinkingStates = Object.freeze([
  "Working",
  "Searching",
  "Solving",
  "Listening",
  "Connecting",
  "Weaving",
  "Composing",
  "Breathing",
  "Shaping",
]);
const transientPrefix = "optionhelper.workspace.state";
const workspaceEntryKey = "optionhelper-workspace-enter";

function consumeWorkspaceEntryTransition() {
  let shouldAnimate = false;
  try {
    shouldAnimate = sessionStorage.getItem(workspaceEntryKey) === "1";
    sessionStorage.removeItem(workspaceEntryKey);
  } catch { /* The workspace works without optional transition state. */ }
  if (!shouldAnimate || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  document.documentElement.classList.add("workspace-entering");
  requestAnimationFrame(() => window.setTimeout(() => {
    document.documentElement.classList.remove("workspace-entering");
  }, 340));
}

consumeWorkspaceEntryTransition();

export async function startWorkspace(initialMode) {
  const shell = document.querySelector("[data-workspace-shell]");
  const rail = document.querySelector("#rail-task-list");
  const stream = document.querySelector("#conversation-stream");
  const chatSurface = document.querySelector("#chat-surface");
  const chatScrollStage = document.querySelector("#chat-scroll-stage");
  const conversationTitle = document.querySelector("#conversation-title");
  const assistant = document.querySelector(".assistant-region");
  const assistantPanel = assistant.querySelector(".assistant-panel");
  const assistantTranscript = document.querySelector("#assistant-transcript");
  const assistantToggle = document.querySelector("[data-assistant-toggle]");
  const assistantClose = document.querySelector("[data-assistant-close]");
  const form = document.querySelector("#workspace-form");
  const input = form.elements.content;
  const submit = form.querySelector("button[type=submit]");
  const status = document.querySelector("#workspace-status");
  const title = document.querySelector("#task-title");
  const taskState = document.querySelector("#task-state");
  const reports = document.querySelector("#report-list");
  const reportFeedback = document.querySelector("#report-feedback");
  const reportActions = document.querySelector("[data-report-actions]");
  const tabs = document.querySelector("#module-tabs");
  const moduleTabIndicator = tabs?.querySelector(".desk-module-tabs__indicator");
  const mount = document.querySelector("#module-mount");
  let currentMode = initialMode;
  let currentTask = null;
  let currentModule = new URLSearchParams(location.search).get("module") || "datafetcher";
  let assistantOpen = false;
  let restoringScroll = false;
  let modeSwitchRevision = 0;
  let activeModeTransition = null;
  let workspaceStatusTimer = 0;
  const moduleFrames = new Map();
  const moduleContexts = new Map();
  let moduleIndicatorFrame = 0;
  const panelLayoutKey = "optionhelper.desk-panel-widths";
  const clampPanelWidth = (value, minimum, maximum, fallback) => {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
  };
  const normalizePanelLayout = (layout) => ({
    left: clampPanelWidth(layout?.left, 220, 420, 280),
    right: clampPanelWidth(layout?.right, 300, 520, 340),
  });
  let sharedPanelLayout = (() => {
    try { return normalizePanelLayout(JSON.parse(localStorage.getItem(panelLayoutKey) || "null")); }
    catch { return normalizePanelLayout(null); }
  })();

  const createModuleBridgeNonce = () => crypto.randomUUID
    ? crypto.randomUUID()
    : `bridge-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  const clearWorkspaceStatus = () => {
    window.clearTimeout(workspaceStatusTimer);
    workspaceStatusTimer = 0;
    clearMessage(status);
  };
  const showWorkspaceStatus = (text, isError = false, duration = 0) => {
    clearWorkspaceStatus();
    message(status, text, isError);
    if (duration > 0) {
      workspaceStatusTimer = window.setTimeout(() => clearMessage(status), duration);
    }
  };
  const showReportFeedback = (text, isError = false) => message(reportFeedback, text, isError);
  const clearReportFeedback = () => clearMessage(reportFeedback);
  const setComposerSending = (button, sending) => {
    if (sending) button.classList.remove("is-sent");
    button.dataset.sending = String(sending);
    button.disabled = sending || !input.value.trim();
    button.classList.toggle("is-generating", sending);
    form.classList.toggle("is-generating", sending);
    button.setAttribute("aria-busy", String(sending));
    button.setAttribute("aria-label", sending ? "正在发送" : "发送");
  };
  const syncComposerAvailability = () => {
    submit.disabled = submit.dataset.sending === "true" || !input.value.trim();
  };
  const markComposerSent = (button) => {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    button.classList.remove("is-sent");
    void button.offsetWidth;
    button.classList.add("is-sent");
    window.setTimeout(() => button.classList.remove("is-sent"), 460);
  };
  const dissolveComposerInput = (content) => {
    if (!content || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const echo = document.createElement("span");
    echo.className = "composer-send-echo";
    echo.textContent = content;
    echo.setAttribute("aria-hidden", "true");
    form.append(echo);
    requestAnimationFrame(() => echo.classList.add("is-running"));
    window.setTimeout(() => echo.remove(), 360);
  };
  const appendPendingMessage = (content) => {
    const pending = document.createElement("article");
    pending.className = "message message--user message--pending";
    const body = document.createElement("p");
    body.className = "message__body";
    body.textContent = content;
    pending.append(body);
    stream.append(pending);
    stream.scrollTop = stream.scrollHeight;
    return pending;
  };
  const appendThinkingIndicator = () => {
    const thinking = document.createElement("article");
    thinking.className = "message message--assistant message--thinking";
    thinking.setAttribute("role", "status");
    const orbs = document.createElement("span");
    orbs.className = "thinking-orbs";
    orbs.setAttribute("aria-hidden", "true");
    for (let index = 0; index < 3; index += 1) orbs.append(document.createElement("i"));
    const copy = document.createElement("span");
    copy.className = "thinking-copy";
    let stateIndex = 0;
    const setThinkingState = () => {
      const state = thinkingStates[stateIndex];
      thinking.setAttribute("aria-label", `正在处理：${state}`);
      copy.dataset.thinkingState = state.toLowerCase();
      copy.textContent = state;
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
        copy.classList.remove("is-changing");
        void copy.offsetWidth;
        copy.classList.add("is-changing");
      }
    };
    setThinkingState();
    const stateTimer = window.setInterval(() => {
      stateIndex = (stateIndex + 1) % thinkingStates.length;
      setThinkingState();
    }, 940);
    thinking.disposeThinking = () => window.clearInterval(stateTimer);
    thinking.append(orbs, copy);
    stream.append(thinking);
    stream.scrollTop = stream.scrollHeight;
    return thinking;
  };
  const syncReportActions = () => {
    const hasTask = Boolean(currentTask?.task_id);
    reportActions?.querySelectorAll("[data-report-kind]").forEach((button) => {
      const capability = button.dataset.reportKind === "report" ? "report.full.request" : "report.card.request";
      const permitted = button.dataset.permitted !== "false";
      button.disabled = !hasTask || !permitted;
      button.hidden = !permitted;
      if (!permitted) button.removeAttribute("aria-describedby");
      else button.setAttribute("aria-describedby", "report-feedback");
      button.title = !hasTask
        ? "请先选择任务"
        : capability === "report.full.request" ? "生成详细Report" : "生成简洁Card";
    });
  };

  const transientKey = (taskId = currentTask?.task_id || "new") => `${transientPrefix}.${taskId}`;
  const readTransient = (taskId) => {
    try { return JSON.parse(sessionStorage.getItem(transientKey(taskId)) || "{}"); }
    catch { return {}; }
  };
  const migrateTransient = (fromTaskId, taskId) => {
    const state = readTransient(fromTaskId);
    if (!Object.keys(state).length) return;
    try {
      sessionStorage.setItem(transientKey(taskId), JSON.stringify(state));
      sessionStorage.removeItem(transientKey(fromTaskId));
    } catch { /* Tab state is optional. */ }
  };
  const saveTransient = () => {
    const previous = readTransient();
    const maxScroll = Math.max(0, stream.scrollHeight - stream.clientHeight);
    const scrollRatio = maxScroll > 0 ? stream.scrollTop / maxScroll : 0;
    const atBottom = maxScroll > 0 && maxScroll - stream.scrollTop <= 2;
    const state = {
      ...previous,
      draft: input.value,
      chatScrollTop: currentMode === "chat" ? stream.scrollTop : (previous.chatScrollTop || 0),
      deskScrollTop: currentMode === "desk" ? stream.scrollTop : (previous.deskScrollTop || 0),
      chatScrollRatio: currentMode === "chat" ? scrollRatio : (previous.chatScrollRatio || 0),
      deskScrollRatio: currentMode === "desk" ? scrollRatio : (previous.deskScrollRatio || 0),
      chatAtBottom: currentMode === "chat" ? atBottom : Boolean(previous.chatAtBottom),
      deskAtBottom: currentMode === "desk" ? atBottom : Boolean(previous.deskAtBottom),
      assistantOpen,
    };
    try { sessionStorage.setItem(transientKey(), JSON.stringify(state)); } catch { /* Tab state is optional. */ }
    return state;
  };

  const setAssistantOpen = (open, { focus = false, persist = true } = {}) => {
    assistantOpen = Boolean(open);
    const visibleInDesk = currentMode === "desk" && assistantOpen;
    assistant.classList.toggle("is-open", visibleInDesk);
    assistantToggle.setAttribute("aria-expanded", String(visibleInDesk));
    assistantPanel.setAttribute("aria-hidden", String(currentMode === "desk" && !assistantOpen));
    assistantPanel.inert = currentMode === "desk" && !assistantOpen;
    if (focus && currentMode === "desk" && assistantOpen) input.focus();
    if (persist) saveTransient();
  };

  const restoreTransient = () => {
    const state = readTransient();
    input.value = state.draft || "";
    assistantOpen = Boolean(state.assistantOpen);
    setAssistantOpen(assistantOpen, { persist: false });
    requestAnimationFrame(() => {
      stream.scrollTop = Number(currentMode === "desk" ? state.deskScrollTop : state.chatScrollTop) || 0;
    });
  };

  const placeConversation = () => {
    input.placeholder = currentMode === "desk" ? "输入研究任务" : "输入需求，按Enter发送，Shift加Enter换行";
    if (currentMode === "desk") {
      if (stream.parentElement !== assistantTranscript) assistantTranscript.append(stream);
    } else {
      if (stream.parentElement !== chatScrollStage) chatScrollStage.append(stream);
    }
    setAssistantOpen(assistantOpen, { persist: false });
  };

  const modePath = (mode) => mode === "desk" ? "/optdesk" : "/optchat";
  const applyMode = async (mode, { updateHistory = true } = {}) => {
    activeModeTransition?.dispose();
    const nextMode = mode === "desk" ? "desk" : "chat";
    const state = restoringScroll ? readTransient() : saveTransient();
    const revision = ++modeSwitchRevision;
    const transition = createTransitionScope();
    activeModeTransition = transition;
    const isCurrentTransition = () => (
      activeModeTransition === transition && transition.isActive() && revision === modeSwitchRevision
    );
    const releaseTransition = () => {
      transition.dispose();
      if (activeModeTransition === transition) activeModeTransition = null;
    };
    restoringScroll = true;
    const previousMode = currentMode;
    currentMode = nextMode;
    if (previousMode !== currentMode) clearWorkspaceStatus();
    shell.dataset.switching = "true";
    shell.dataset.mode = currentMode;
    const deskSurface = document.querySelector("#desk-surface");
    chatSurface.inert = currentMode !== "chat";
    deskSurface.inert = currentMode !== "desk";
    chatSurface.setAttribute("aria-hidden", String(chatSurface.inert));
    deskSurface.setAttribute("aria-hidden", String(deskSurface.inert));
    document.querySelectorAll("button[data-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === currentMode)));
    placeConversation();
    if (updateHistory) {
      const next = new URL(modePath(currentMode), location.origin);
      if (currentTask?.task_id) next.searchParams.set("task", currentTask.task_id);
      if (currentMode === "desk" && currentModule) next.searchParams.set("module", currentModule);
      history.pushState({ mode: currentMode, task: currentTask?.task_id || "" }, "", `${next.pathname}${next.search}`);
    }
    if (nextMode === "desk" && currentTask) await mountModule(currentModule, false);
    if (!isCurrentTransition()) return;
    const desiredScrollTop = Number(currentMode === "desk" ? state.deskScrollTop : state.chatScrollTop) || 0;
    const desiredScrollRatio = Number(currentMode === "desk" ? state.deskScrollRatio : state.chatScrollRatio);
    const desiredAtBottom = Boolean(currentMode === "desk" ? state.deskAtBottom : state.chatAtBottom);
    const targetSurface = currentMode === "desk" ? document.querySelector("#desk-surface") : chatSurface;
    const restoreModeScroll = () => {
      const maxScroll = Math.max(0, stream.scrollHeight - stream.clientHeight);
      stream.scrollTop = desiredAtBottom
        ? maxScroll
        : Number.isFinite(desiredScrollRatio) ? desiredScrollRatio * maxScroll : desiredScrollTop;
    };
    const finishModeScroll = () => {
      if (!isCurrentTransition()) {
        releaseTransition();
        return;
      }
      releaseTransition();
      restoreModeScroll();
      restoringScroll = false;
      saveTransient();
      shell.dataset.switching = "false";
    };
    transition.frame(() => {
      if (!isCurrentTransition()) return;
      restoreModeScroll();
      transition.frame(() => {
        if (!isCurrentTransition()) return;
        restoreModeScroll();
        const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
        if (previousMode === currentMode || reducedMotion) {
          finishModeScroll();
          return;
        }
        const onTransitionEnd = (event) => {
          if (event.target !== targetSurface || event.propertyName !== "transform") return;
          finishModeScroll();
        };
        transition.listen(targetSurface, "transitionend", onTransitionEnd);
        // WebKit can suppress transitionend when a surface becomes inert while
        // its sibling is mounting an iframe.  The fallback closes the same
        // transition state without changing geometry or forcing a repaint.
        transition.delay(finishModeScroll, 640);
      });
    });
  };

  async function createTask(subject = "新建研究任务") {
    const { task } = await request("/api/tasks", { method: "POST", body: safeJson({ subject }) });
    return task;
  }

  async function loadTasks() {
    const { tasks } = await request("/api/tasks");
    renderTaskList(rail, tasks, currentTask?.task_id, (id) => selectTask(id).catch((error) => message(status, error.message, true)));
    return tasks;
  }

  async function selectTask(taskId, updateLocation = true) {
    if (currentTask) saveTransient();
    const { task } = await request(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (currentTask?.task_id && currentTask.task_id !== task.task_id) disposeModuleFrames();
    currentTask = task;
    clearReportFeedback();
    syncReportActions();
    title.textContent = task.subject;
    conversationTitle.textContent = task.subject;
    taskState.textContent = "当前任务会保留对话、模块运行记录和关联报告。";
    renderMessages(stream, task.messages || [], "可直接输入任务要求，或选择一个研究起点。");
    chatSurface.classList.toggle("chat-surface--empty", !task.messages?.length);
    const { reports: reportRuns } = await request(`/api/tasks/${encodeURIComponent(taskId)}/reports`).catch(() => ({ reports: [] }));
    renderReports(reports, reportRuns);
    if (updateLocation) setTaskLocation(task.task_id, { module: currentMode === "desk" ? currentModule : "" });
    await loadTasks();
    restoreTransient();
    if (currentMode === "desk") await mountModule(currentModule, false);
  }

  function disposeModuleFrames() {
    for (const frame of moduleFrames.values()) {
      frame.src = "about:blank";
      frame.remove();
    }
    moduleFrames.clear();
    moduleContexts.clear();
  }

  function syncModuleTabIndicator(animate = true) {
    window.cancelAnimationFrame(moduleIndicatorFrame);
    moduleIndicatorFrame = window.requestAnimationFrame(() => {
      const active = tabs?.querySelector('[data-module][aria-selected="true"]');
      if (!active || !moduleTabIndicator) return;
      const tabsRect = tabs.getBoundingClientRect();
      const activeRect = active.getBoundingClientRect();
      if (activeRect.width <= 0 || tabsRect.width <= 0) return;
      if (!animate) moduleTabIndicator.style.transition = "none";
      moduleTabIndicator.style.width = `${activeRect.width}px`;
      moduleTabIndicator.style.transform = `translate3d(${activeRect.left - tabsRect.left + tabs.scrollLeft}px, 0, 0)`;
      moduleTabIndicator.dataset.ready = "true";
      if (!animate) {
        void moduleTabIndicator.offsetWidth;
        moduleTabIndicator.style.removeProperty("transition");
      }
    });
  }

  function setActiveModule(moduleName, animate = true) {
    tabs.querySelectorAll("[data-module]").forEach((button) => {
      const active = button.dataset.module === moduleName;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    syncModuleTabIndicator(animate);
  }

  function activateFrame(moduleName) {
    for (const [name, frame] of moduleFrames) {
      const active = name === moduleName;
      frame.classList.toggle("is-active", active);
      frame.setAttribute("aria-hidden", String(!active));
      frame.tabIndex = active ? 0 : -1;
    }
  }

  function applyHostedModulePresentation(frame) {
    const doc = frame.contentDocument;
    if (!doc?.head || doc.documentElement.dataset.optionhelperAppHosted === "true") return;
    doc.documentElement.dataset.optionhelperAppHosted = "true";
    const link = doc.createElement("link");
    link.rel = "stylesheet";
    link.href = "/app/frontend/shared/module-host.css";
    doc.head.append(link);
  }

  function deliverContext(moduleName) {
    const frame = moduleFrames.get(moduleName);
    const context = moduleContexts.get(moduleName);
    const bridgeNonce = frame?.dataset.bridgeNonce;
    if (frame?.dataset.ready === "true" && context && bridgeNonce) {
      frame.contentWindow?.postMessage({ type: "optionhelper.module-host-context", context, bridge_nonce: bridgeNonce }, location.origin);
      frame.contentWindow?.postMessage({ type: "optionhelper.desk-panel-layout", layout: sharedPanelLayout, bridge_nonce: bridgeNonce }, location.origin);
    }
  }

  async function mountModule(moduleName, updateLocation = true) {
    if (!modules.has(moduleName) || !currentTask) return;
    const previousModule = currentModule;
    try {
      const task = `?task_id=${encodeURIComponent(currentTask.task_id)}`;
      const { context } = await request(`/api/module-host/${encodeURIComponent(moduleName)}${task}`);
      let frame = moduleFrames.get(moduleName);
      if (!frame) {
        const bridgeNonce = createModuleBridgeNonce();
        frame = document.createElement("iframe");
        frame.title = modules.get(moduleName);
        frame.src = `/capability/assets/pages/${encodeURIComponent(moduleName)}/${encodeURIComponent(moduleName)}.html?host=optdesk&bridge_nonce=${encodeURIComponent(bridgeNonce)}`;
        frame.className = "module-frame";
        frame.dataset.bridgeNonce = bridgeNonce;
        frame.setAttribute("aria-hidden", "true");
        frame.addEventListener("load", () => applyHostedModulePresentation(frame));
        frame.addEventListener("error", () => showWorkspaceStatus(`${modules.get(moduleName)}页面未能载入。`, true, 7000), { once: true });
        moduleFrames.set(moduleName, frame);
        mount.querySelector(".conversation-start")?.remove();
        mount.append(frame);
      }
      moduleContexts.set(moduleName, context);
      currentModule = moduleName;
      setActiveModule(moduleName);
      if (updateLocation) setTaskLocation(currentTask.task_id, { module: moduleName });
      activateFrame(moduleName);
      deliverContext(moduleName);
    } catch (error) {
      currentModule = previousModule;
      setActiveModule(previousModule, false);
      const staleTask = error.status === 409 && error.body?.error === "stale_task_contract";
      showWorkspaceStatus(
        staleTask ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。") : "模块暂未就绪，请稍后重试。",
        true,
        7000,
      );
    }
  }

  window.addEventListener("message", (event) => {
    const moduleName = event.data?.module;
    const frame = moduleFrames.get(moduleName);
    if (event.origin !== location.origin || event.source !== frame?.contentWindow) return;
    if (event.data?.bridge_nonce !== frame?.dataset.bridgeNonce) return;
    if (event.data?.type === "optionhelper.desk-panel-layout-change") {
      sharedPanelLayout = normalizePanelLayout(event.data.layout);
      try { localStorage.setItem(panelLayoutKey, JSON.stringify(sharedPanelLayout)); } catch { /* optional */ }
      for (const target of moduleFrames.values()) {
        target.contentWindow?.postMessage({
          type: "optionhelper.desk-panel-layout",
          layout: sharedPanelLayout,
          bridge_nonce: target.dataset.bridgeNonce,
        }, location.origin);
      }
      return;
    }
    if (event.data?.type !== "optionhelper.module-host-ready") return;
    frame.dataset.ready = "true";
    deliverContext(moduleName);
  });

  tabs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-module]");
    if (button) mountModule(button.dataset.module).catch(() => showWorkspaceStatus("模块暂未就绪，请稍后重试。", true, 7000));
  });
  tabs.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const moduleTabs = Array.from(tabs.querySelectorAll("[data-module]"));
    const current = moduleTabs.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const nextIndex = event.key === "Home" ? 0
      : event.key === "End" ? moduleTabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + moduleTabs.length) % moduleTabs.length;
    const next = moduleTabs[nextIndex];
    next.focus();
    mountModule(next.dataset.module).catch(() => showWorkspaceStatus("模块暂未就绪，请稍后重试。", true, 7000));
  });
  const moduleTabsResizeObserver = new ResizeObserver(() => syncModuleTabIndicator(false));
  moduleTabsResizeObserver.observe(tabs);
  tabs.addEventListener("scroll", () => syncModuleTabIndicator(false), { passive: true });
  window.addEventListener("resize", () => syncModuleTabIndicator(false), { passive: true });
  assistantToggle.addEventListener("click", () => setAssistantOpen(!assistantOpen, { focus: true }));
  assistantClose.addEventListener("click", () => { setAssistantOpen(false); assistantToggle.focus(); });
  stream.addEventListener("scroll", () => {
    if (!restoringScroll) saveTransient();
  }, { passive: true });
  input.addEventListener("input", () => {
    saveTransient();
    syncComposerAvailability();
  });
  stream.addEventListener("click", (event) => {
    const starter = event.target.closest("[data-starter-prompt]");
    if (!starter) return;
    input.value = starter.dataset.starterPrompt || "";
    saveTransient();
    input.focus();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && currentMode === "desk" && assistantOpen) {
      setAssistantOpen(false);
      assistantToggle.focus();
    }
  });
  document.querySelector("[data-new-task]").addEventListener("click", async () => {
    try { await selectTask((await createTask()).task_id); } catch (error) { showWorkspaceStatus("新建任务未完成，请稍后重试。", true); }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submittedContent = input.value.trim();
    if (!submittedContent) return;
    let pendingMessage;
    let thinkingMessage;
    setComposerSending(submit, true);
    clearWorkspaceStatus();
    try {
      if (!currentTask) {
        const task = await createTask();
        migrateTransient("new", task.task_id);
        await selectTask(task.task_id);
      }
      pendingMessage = appendPendingMessage(submittedContent);
      thinkingMessage = appendThinkingIndicator();
      dissolveComposerInput(submittedContent);
      input.value = "";
      saveTransient();
      const requestId = globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const response = await request(`/api/tasks/${encodeURIComponent(currentTask.task_id)}/messages`, {
        method: "POST", body: safeJson({ content: submittedContent }), headers: { "X-Request-Id": requestId },
      });
      markComposerSent(submit);
      input.value = "";
      saveTransient();
      applyConversationStatus(response, status, taskState);
      await selectTask(currentTask.task_id, false);
    } catch (error) {
      pendingMessage?.remove();
      let messagePersisted = false;
      if (currentTask) {
        await selectTask(currentTask.task_id, false).catch(() => {});
        const submittedMessagePersisted = currentTask?.messages
          ?.slice?.(-2)
          .some((entry) => entry.role === "user" && entry.content === submittedContent);
        if (submittedMessagePersisted) {
          messagePersisted = true;
          input.value = "";
          saveTransient();
        }
      }
      if (!messagePersisted) {
        input.value = submittedContent;
        saveTransient();
      }
      const recovery = error.body?.error?.next_step || error.body?.next_step;
      const failureMessage = error.status === 409 && error.body?.error === "stale_task_contract"
        ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。")
        : error.status === 503
        ? `${recovery || "模型暂不可用，请在设置中心检查模型服务。"}任务内容已保留。`
        : error.message;
      showWorkspaceStatus(failureMessage, true);
    } finally {
      thinkingMessage?.disposeThinking?.();
      thinkingMessage?.remove();
      setComposerSending(submit, false);
    }
  });
  bindComposerKeyboard(form);
  syncComposerAvailability();

  document.querySelectorAll("[data-report-kind]").forEach((button) => button.addEventListener("click", async () => {
    if (!currentTask) { showReportFeedback("请先新建或选择任务。", true); return; }
    button.disabled = true;
    try {
      const response = await request(`/api/tasks/${encodeURIComponent(currentTask.task_id)}/reports`, { method: "POST", body: safeJson({ kind: button.dataset.reportKind }) });
      const result = response?.result || {};
      if (result.status === "needs_input") {
        showReportFeedback("当前任务还没有可用于生成报告的正式结果。请先完成收益结构、估值定价或历史回测中的至少一项分析。", true);
      } else if (result.status === "completed") {
        showReportFeedback(button.dataset.reportKind === "card" ? "Card已生成，可在报告列表中预览或下载。" : "详细Report已生成，可在报告列表中预览或下载。");
      } else {
        showReportFeedback("报告暂未生成。请先完成当前任务的正式分析结果。", true);
      }
      if (result.status === "completed") await selectTask(currentTask.task_id, false);
    } catch {
      showReportFeedback("当前任务还没有可用于生成报告的正式结果。请先完成收益结构、估值定价或历史回测中的至少一项分析。", true);
    } finally {
      syncReportActions();
    }
  }));

  window.addEventListener("pagehide", saveTransient);
  window.addEventListener("popstate", async () => {
    const nextMode = location.pathname.startsWith("/optdesk") ? "desk" : "chat";
    if (nextMode === "desk" && shell.dataset.hasDesk !== "true") {
      location.replace(`/optchat${location.search}`);
      return;
    }
    await applyMode(nextMode, { updateHistory: false });
    const requested = taskIdFromLocation();
    if (requested && requested !== currentTask?.task_id) await selectTask(requested, false).catch(() => {});
  });

  const session = await initializeWorkspace(initialMode, { onModeChange: async (mode) => {
    if (!currentTask) {
      const task = await createTask();
      migrateTransient("new", task.task_id);
      await selectTask(task.task_id, false);
    }
    await applyMode(mode);
  } });
  if (!session) return;
  document.querySelectorAll("[data-report-kind]").forEach((button) => {
    const capability = button.dataset.reportKind === "report" ? "report.full.request" : "report.card.request";
    button.dataset.permitted = String(session.capabilities.includes(capability));
  });
  syncReportActions();
  const [, tasks] = await Promise.all([
    configureModelPicker(document.querySelector("#workspace-model-picker")).catch(() => {}),
    loadTasks(),
  ]);
  const requested = taskIdFromLocation();
  if (requested) await selectTask(requested, false).catch(() => {});
  if (!currentTask && tasks.length) await selectTask(tasks[0].task_id, false);
  if (!currentTask && initialMode === "desk") await selectTask((await createTask()).task_id, false);
  if (!currentTask) renderMessages(stream, [], "新建任务后即可开始对话，并按需生成Card或Report。");
  await applyMode(initialMode, { updateHistory: false });
  restoreTransient();
  syncComposerAvailability();
  if (initialMode === "desk" && currentTask) await mountModule(currentModule, false);
}

function applyConversationStatus(response, status, taskState) {
  clearMessage(status);
  const state = String(response?.status || "").trim().toLowerCase();
  const taskCopy = {
    completed: "任务助手已更新当前任务对话。",
    needs_input: "等待补充信息后继续处理。",
    pending_approval: "等待确认后继续处理。",
    partial: "本次处理已保留，可以继续补充需求。",
    stopped_duplicate: "本次处理已安全停止，可以调整需求后继续。",
    max_rounds: "本次处理步骤较多，可以继续发送需求。",
    timed_out: "本次处理超时，任务内容已保留。",
    blocked: "当前操作受到限制，任务内容已保留。",
    cancelled: "本次任务已取消。",
    unavailable: "相关服务暂不可用，任务内容已保留。",
  };
  taskState.textContent = taskCopy[state] || "任务状态已更新。";
  if (state === "unavailable") {
    message(status, response?.error?.next_step || "相关服务暂不可用，请检查设置后重试。", true);
  } else if (state === "timed_out") {
    message(status, "本次处理超时，任务内容已保留；请稍后重试。", false);
  } else if (state === "blocked") {
    message(status, "当前操作受到限制，请调整需求后继续。", true);
  }
}

if (location.pathname.startsWith("/optchat")) await startWorkspace("chat");
