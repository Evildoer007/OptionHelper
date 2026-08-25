import { bindComposerKeyboard, clearMessage, configureModelPicker, initializeWorkspace, message, renderMessages, renderReports, renderTaskList, request, safeJson, setTaskLocation, taskIdFromLocation } from "/app/frontend/shared/app.js";
import { createThinkingOrb } from "/app/frontend/shared/thinking-orb.js";
import { createTransitionScope } from "/app/frontend/shared/transition-scope.js";
import { currentTheme, currentThemePreference, onThemeChange } from "/app/frontend/shared/theme.js";

const modules = new Map([["datafetcher", "数据获取"], ["payoffer", "收益结构"], ["pricer", "估值定价"], ["backtester", "历史回测"], ["reporter", "研究报告"]]);
const processOrbStates = Object.freeze({
  request: "listening",
  routing: "searching",
  agent_run: "solving",
  host_module: "connecting",
  candidate_cycle: "weaving",
  answer: "composing",
  terminal: "shaping",
});
const runtimeEventStatuses = new Set([
  "queued", "starting", "running", "waiting_tool", "waiting_parent", "started", "pending",
  "reselecting", "completed", "succeeded", "failed", "cancelled", "interrupted", "recovered", "stopped",
]);
const runtimeTerminalStatuses = new Set(["completed", "succeeded", "failed", "cancelled", "interrupted", "stopped"]);
const runtimeSensitiveText = /(?:system[ _-]?prompt|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|bearer|secret|private[ _-]?key|chain[ _-]?of[ _-]?thought|hidden[ _-]?reasoning|internal[ _-]?reasoning|tool[ _-]?arguments?|arguments?|run[ _-]?ref|module[ _-]?run[ _-]?ref|contract[ _-]?fingerprint|task[ _-]?id|request[ _-]?id|tenant[ _-]?id|principal[ _-]?id)/i;
const runtimeEventDeltaLimit = 64_000;
const runtimeAssistantTextLimit = 64_000;
const runtimeReasoningTextLimit = 512_000;
const runtimeSummaryLimit = 180;
const runtimeToolLabels = Object.freeze({
  "payoffer": "收益结构",
  "payoffer.run": "收益结构",
  "pricer": "估值定价",
  "pricer.run": "估值定价",
  "backtester": "历史回测",
  "backtester.run": "历史回测",
  "datafetcher": "数据获取",
  "datafetcher.run": "数据获取",
  "reporter": "交付材料",
  "reporter.run": "交付材料",
});

const isRecord = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
const clampRuntimeText = (value, limit = runtimeSummaryLimit) => {
  if (value === null || value === undefined || typeof value === "object" || typeof value === "function") return "";
  const text = String(value ?? "").trim();
  if (!text || runtimeSensitiveText.test(text)) return "";
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
};
const runtimeDeltaText = (value, limit = runtimeEventDeltaLimit) => {
  if (value === null || value === undefined || typeof value === "object" || typeof value === "function") return "";
  const text = String(value);
  if (!text || runtimeSensitiveText.test(text)) return "";
  return text.length > limit ? text.slice(0, limit) : text;
};
export function appendRuntimeDelta(current, chunk, limit) {
  const combined = `${String(current ?? "")}${String(chunk ?? "")}`;
  return combined.length > limit ? `${combined.slice(0, Math.max(0, limit - 1))}…` : combined;
}
const safeRuntimeKey = (value) => String(value ?? "").trim().slice(0, 160);
const runtimeStatusFromType = (type) => ({
  started: "started",
  requested: "queued",
  queued: "queued",
  starting: "starting",
  running: "running",
  completed: "completed",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "cancelled",
  canceled: "cancelled",
  interrupted: "interrupted",
  recovered: "recovered",
  stopped: "stopped",
}[String(type || "").split(/[./]/).pop().toLowerCase()] || "started");
const runtimeStatus = (value, type = "") => {
  const normalized = String(value ?? "").trim().toLowerCase();
  return runtimeEventStatuses.has(normalized) ? normalized : runtimeStatusFromType(type);
};
const runtimeFamily = (type) => {
  const normalized = String(type || "").trim().toLowerCase();
  if (["request", "routing", "candidate_cycle"].includes(normalized) || normalized.startsWith("workflow")) return "workflow";
  if (normalized === "agent_run" || normalized === "agent" || normalized.startsWith("agent.")) return "agent";
  if (normalized === "turn" || normalized.startsWith("turn.")) return "turn";
  if (normalized === "step" || normalized.startsWith("step.")) return "step";
  if (normalized === "assistant.text_delta") return "assistant";
  if (normalized === "assistant.reasoning_delta") return "reasoning";
  if (normalized === "assistant.message" || normalized === "answer") return "assistant";
  if (normalized === "host_module" || normalized === "tool" || normalized.startsWith("tool.")) return "tool";
  if (normalized === "compaction" || normalized.startsWith("compaction.")) return "compaction";
  if (normalized === "usage" || normalized.startsWith("usage.")) return "usage";
  if (normalized === "terminal" || normalized === "recovery" || normalized === "runtime.recovered" || normalized === "runtime.closed" || normalized === "runtime.error" || normalized.startsWith("session.")) return "recovery";
  return "workflow";
};
const runtimeToolLabel = (value, summary = "") => {
  const key = String(value ?? "").trim().toLowerCase();
  if (runtimeToolLabels[key]) return runtimeToolLabels[key];
  const source = String(summary || "");
  return Object.entries(runtimeToolLabels).find(([module]) => module.endsWith(".run") && source.includes(runtimeToolLabels[module]))?.[1] || "研究模块";
};
const runtimeRoleLabel = (value) => {
  const label = clampRuntimeText(value, 48);
  return label && !/[{}<>\[\]]/.test(label) ? label : "Agent";
};
const runtimePayloadText = (row, payload, keys, limit = runtimeSummaryLimit) => {
  for (const key of keys) {
    const value = row?.[key] ?? payload?.[key];
    const text = clampRuntimeText(value, limit);
    if (text) return text;
  }
  return "";
};
const normalizeRuntimeUsage = (value) => {
  const usage = isRecord(value) ? value : {};
  const number = (...keys) => {
    for (const key of keys) {
      const candidate = Number(usage[key]);
      if (Number.isFinite(candidate) && candidate >= 0) return Math.floor(candidate);
    }
    return null;
  };
  const inputTokens = number("input_tokens", "inputTokens", "prompt_tokens", "input");
  const outputTokens = number("output_tokens", "outputTokens", "completion_tokens", "output");
  const reasoningTokens = number("reasoning_tokens", "reasoningTokens", "reasoning");
  const toolTokens = number("tool_tokens", "toolTokens", "tool");
  const totalTokens = number("total_tokens", "totalTokens")
    ?? ((inputTokens !== null || outputTokens !== null || reasoningTokens !== null || toolTokens !== null)
      ? (inputTokens || 0) + (outputTokens || 0) + (reasoningTokens || 0) + (toolTokens || 0)
      : number("total"));
  const cachedTokens = number("cached_tokens", "cachedTokens", "cache_read_input_tokens");
  const calls = number("calls", "call_count");
  if (inputTokens === null && outputTokens === null && reasoningTokens === null && toolTokens === null && totalTokens === null && cachedTokens === null && calls === null) return null;
  return { inputTokens, outputTokens, reasoningTokens, toolTokens, totalTokens, cachedTokens, calls };
};
const defaultRuntimeSummary = (family, status, role, toolLabel) => {
  if (family === "agent") return `${role || "Agent"}${status === "completed" ? "已完成本轮处理。" : "正在处理本轮分工。"}`;
  if (family === "tool") return `${toolLabel || "研究模块"}${status === "completed" || status === "succeeded" ? "已返回结果。" : "正在运行。"}`;
  if (family === "compaction") return status === "completed" ? "上下文整理已完成。" : "正在整理上下文。";
  if (family === "usage") return "Token使用量已更新。";
  if (family === "recovery") return status === "recovered" ? "本轮进度已恢复。" : "正在恢复本轮进度。";
  if (family === "assistant") return status === "completed" ? "答复已整理完成。" : "正在生成答复。";
  if (family === "reasoning") return "正在生成深度思考。";
  if (family === "turn") return status === "completed" ? "当前对话轮次已完成。" : "正在处理当前对话轮次。";
  if (family === "step") return status === "completed" ? "当前步骤已完成。" : "正在执行当前步骤。";
  return status === "completed" ? "研究流程已完成。" : "正在组织本轮研究流程。";
};

export function normalizeRuntimeEvent(row) {
  if (!isRecord(row) || !Number.isInteger(row.seq) || row.seq <= 0) return null;
  const type = String(row.type || row.event_type || row.event || "").trim().toLowerCase();
  if (!type) return null;
  const payload = isRecord(row.payload) ? row.payload : (isRecord(row.data) ? row.data : {});
  const family = runtimeFamily(type);
  const status = runtimeStatus(row.status || payload.status, type);
  const role = runtimeRoleLabel(row.role || row.role_id || payload.role || payload.role_id || payload.agent_role);
  const rawTool = row.tool || row.tool_name || row.module || payload.tool || payload.tool_name || payload.module;
  const summary = runtimePayloadText(row, payload, ["summary", "display_message", "label", "message"]);
  const toolLabel = runtimeToolLabel(rawTool, summary);
  const delta = family === "assistant" || family === "reasoning"
    ? runtimeDeltaText(row.delta ?? row.text ?? payload.delta ?? payload.text)
    : "";
  const usage = family === "usage"
    ? normalizeRuntimeUsage(payload.usage || row.usage || payload || row)
    : normalizeRuntimeUsage(payload.usage || row.usage);
  return Object.freeze({
    seq: row.seq,
    type,
    family,
    status,
    summary: summary || defaultRuntimeSummary(family, status, role, toolLabel),
    createdAt: String(row.created_at || row.createdAt || row.timestamp || row.time || payload.created_at || payload.timestamp || "").trim(),
    role,
    agentKey: safeRuntimeKey(row.agent_run_id || row.agentId || payload.agent_run_id || payload.agentId || (type === "agent_run" ? "legacy-agent" : "")),
    toolKey: safeRuntimeKey(row.call_id || row.tool_call_id || payload.call_id || payload.tool_call_id || `${toolLabel}:${row.agent_run_id || payload.agent_run_id || "main"}`),
    toolLabel,
    delta,
    reasoningAvailable: family === "reasoning" && (Boolean(delta) || payload.available === true || payload.reasoning_available === true || row.reasoning_available === true || payload.truncated === true || row.truncated === true),
    reasoningChars: Number.isInteger(payload.chars) && payload.chars >= 0 ? payload.chars : null,
    truncated: family === "reasoning" && (payload.truncated === true || row.truncated === true),
    providerChars: Number.isInteger(payload.provider_chars) && payload.provider_chars >= 0
      ? payload.provider_chars
      : (Number.isInteger(row.provider_chars) && row.provider_chars >= 0 ? row.provider_chars : null),
    usage,
  });
}

export function projectRuntimeEvent(event) {
  if (!event) return null;
  const isDelta = event.type === "assistant.text_delta" || event.type === "assistant.reasoning_delta";
  return Object.freeze({
    ...event,
    timelineLabel: event.summary,
    showTimeline: !isDelta && event.family !== "usage",
    showReasoning: event.family === "reasoning" && event.reasoningAvailable === true,
    assistantDelta: event.family === "assistant" && event.type === "assistant.text_delta" ? event.delta : "",
    reasoningDelta: event.family === "reasoning" ? event.delta : "",
    reasoningNotice: event.family === "reasoning" && !event.delta
      ? `模型已返回深度思考过程${event.reasoningChars === null ? "。" : `（${event.reasoningChars}字）。`}`
      : "",
    reasoningTruncationNotice: event.family === "reasoning" && event.truncated
      ? "模型思考过程已按展示上限截断"
      : "",
    isTerminal: runtimeTerminalStatuses.has(event.status),
  });
}

export function consumeRuntimeEventRows(cursor, rows) {
  const pending = cursor?.pending instanceof Map ? new Map(cursor.pending) : new Map();
  let nextSeq = Number.isInteger(cursor?.nextSeq) && cursor.nextSeq >= 0 ? cursor.nextSeq : 0;
  for (const row of rows || []) {
    const event = normalizeRuntimeEvent(row);
    if (!event || event.seq <= nextSeq || pending.has(event.seq)) continue;
    pending.set(event.seq, event);
  }
  const emitted = [];
  while (pending.has(nextSeq + 1)) {
    const event = pending.get(nextSeq + 1);
    pending.delete(nextSeq + 1);
    emitted.push(event);
    nextSeq += 1;
  }
  return { nextSeq, pending, emitted };
}

const formatRuntimeTokens = (value) => Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
const transientPrefix = "optionhelper.workspace.state";

export async function startWorkspace(initialMode) {
  const shell = document.querySelector("[data-workspace-shell]");
  const workArea = shell.querySelector(".work-area");
  let workspaceRevealTimer = 0;
  let workspaceRevealed = false;
  const revealWorkspace = () => {
    if (workspaceRevealed) return;
    workspaceRevealed = true;
    window.clearTimeout(workspaceRevealTimer);
    shell.dataset.initializing = "false";
    shell.setAttribute("aria-busy", "false");
    document.body.classList.remove("workspace-body--initializing");
  };
  // Never leave the native window behind a loading surface if a local service
  // responds slowly or a module fails during initial restoration.
  workspaceRevealTimer = window.setTimeout(revealWorkspace, 2800);
  const rail = document.querySelector("#rail-task-list");
  const stream = document.querySelector("#conversation-stream");
  const chatSurface = document.querySelector("#chat-surface");
  const chatScrollStage = document.querySelector("#chat-scroll-stage");
  const runtimeLiveStatus = document.querySelector("#runtime-live-status");
  const conversationTitle = document.querySelector("#conversation-title");
  const assistant = document.querySelector(".assistant-region");
  const assistantPanel = assistant.querySelector(".assistant-panel");
  const assistantTranscript = document.querySelector("#assistant-transcript");
  const assistantToggle = document.querySelector("[data-assistant-toggle]");
  const assistantClose = document.querySelector("[data-assistant-close]");
  const form = document.querySelector("#workspace-form");
  const input = form.elements.content;
  const submit = form.querySelector("button[type=submit]");
  const modelPicker = document.querySelector("#workspace-model-picker");
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
  let taskSelectionRevision = 0;
  let moduleMountRevision = 0;
  let moduleNavigationController = null;
  let activeModeTransition = null;
  let workspaceStatusTimer = 0;
  let operationRefreshTimer = 0;
  let pendingConversationRequest = null;
  let pendingReportRequest = null;
  let activeProcessPlayback = null;
  let activeConversation = null;
  const moduleFrames = new Map();
  const moduleContexts = new Map();
  const moduleContextVersions = new Map();
  const moduleContextRefreshes = new Map();
  const moduleFrameLoadTimers = new Map();
  let moduleIndicatorFrame = 0;
  let composerClearanceFrame = 0;
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

  const syncChatComposerClearance = () => {
    window.cancelAnimationFrame(composerClearanceFrame);
    composerClearanceFrame = window.requestAnimationFrame(() => {
      composerClearanceFrame = 0;
      const usesFloatingComposer = currentMode === "chat"
        && window.matchMedia("(min-width: 701px)").matches;
      if (!usesFloatingComposer) {
        workArea.style.removeProperty("--chat-composer-clearance");
        return;
      }
      const workAreaBounds = workArea.getBoundingClientRect();
      const composerBounds = form.getBoundingClientRect();
      if (composerBounds.height <= 0) return;
      const clearance = Math.max(0, Math.ceil(workAreaBounds.bottom - composerBounds.top + 12));
      workArea.style.setProperty("--chat-composer-clearance", `${clearance}px`);
    });
  };

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
  const reportFailureMessage = (error) => {
    if (error?.status === 401 || error?.status === 403) return "当前账户没有生成该报告的权限。";
    if (error?.status === 404) return "当前任务或报告服务不存在，请重新选择任务后再试。";
    if (error?.status === 409) return error.body?.message || error.message || "当前任务状态不允许生成报告。";
    if (error?.status >= 500) return "报告服务暂时不可用，请稍后重试。";
    if (!error?.status) return "无法连接报告服务，请检查连接后重试。";
    return error.body?.message || error.message || "报告生成失败，请稍后重试。";
  };
  const setComposerSending = (button, sending) => {
    if (sending) button.classList.remove("is-sent");
    button.dataset.sending = String(sending);
    if (!sending) button.dataset.cancelRequested = "false";
    button.disabled = button.dataset.cancelRequested === "true"
      || (!sending && (modelPicker.disabled || !modelPicker.value || !input.value.trim()));
    button.classList.toggle("is-generating", sending);
    form.classList.toggle("is-generating", sending);
    button.setAttribute("aria-busy", String(sending));
    button.setAttribute("aria-label", sending
      ? (button.dataset.cancelRequested === "true" ? "正在取消本次处理" : "取消本次处理")
      : "发送");
  };
  const syncComposerAvailability = () => {
    const sending = submit.dataset.sending === "true";
    submit.disabled = submit.dataset.cancelRequested === "true"
      || (!sending && (modelPicker.disabled || !modelPicker.value || !input.value.trim()));
    submit.setAttribute("aria-label", sending
      ? (submit.dataset.cancelRequested === "true" ? "正在取消本次处理" : "取消本次处理")
      : "发送");
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
  const processEventsPath = (taskId, requestId, afterSeq = 0) => (
    `/api/tasks/${encodeURIComponent(taskId)}/conversation-requests/${encodeURIComponent(requestId)}/events?after_seq=${afterSeq}`
  );
  const runtimeStatusLabel = (status) => ({
    queued: "排队中",
    starting: "正在启动",
    running: "运行中",
    waiting_tool: "等待工具结果",
    waiting_parent: "等待主Agent汇总",
    started: "已开始",
    pending: "等待处理",
    reselecting: "重新筛选中",
    completed: "已完成",
    succeeded: "已完成",
    failed: "未完成",
    cancelled: "已取消",
    interrupted: "已中断",
    recovered: "已恢复",
    stopped: "已停止",
  }[status] || "处理中");
  const runtimeOrbState = (event) => {
    if (processOrbStates[event.type]) return processOrbStates[event.type];
    if (event.family === "workflow") return "searching";
    if (event.family === "agent" || event.family === "turn" || event.family === "step") return "solving";
    if (event.family === "tool") return "connecting";
    if (event.family === "compaction") return "weaving";
    if (event.family === "assistant") return "composing";
    if (event.family === "reasoning") return "solving";
    if (event.family === "recovery") return "breathing";
    return "working";
  };
  const setRuntimeLiveStatus = (text) => {
    if (runtimeLiveStatus) runtimeLiveStatus.textContent = clampRuntimeText(text, 140);
  };
  const appendProcessPanel = ({ onCancel = null } = {}) => {
    const panel = document.createElement("article");
    panel.className = "message message--assistant message--process";
    panel.dataset.runtimeProcess = "true";
    const details = document.createElement("details");
    details.open = true;
    const summary = document.createElement("summary");
    const orb = createThinkingOrb({ state: "working", size: 20 });
    const summaryLabel = document.createElement("span");
    summaryLabel.textContent = "运行过程";
    summary.append(orb.element, summaryLabel);
    const state = document.createElement("span");
    state.className = "process-state";
    state.textContent = "正在获取进度";
    const events = document.createElement("ol");
    events.className = "process-events";
    events.dataset.processTimeline = "true";
    const actions = document.createElement("div");
    actions.className = "process-actions";
    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "button-secondary";
    cancelButton.textContent = "取消本轮";
    cancelButton.setAttribute("aria-label", "取消本轮处理");
    cancelButton.hidden = typeof onCancel !== "function";
    cancelButton.addEventListener("click", () => onCancel?.());
    actions.append(cancelButton);

    const createSection = (label, className, child) => {
      const section = document.createElement("section");
      section.className = className;
      section.hidden = true;
      const heading = document.createElement("p");
      heading.className = "process-section-label";
      heading.textContent = label;
      section.append(heading, child);
      return section;
    };
    const agentCards = document.createElement("div");
    agentCards.className = "process-agent-cards";
    agentCards.dataset.processAgents = "true";
    const toolCards = document.createElement("div");
    toolCards.className = "process-tool-cards";
    toolCards.dataset.processTools = "true";
    const usageValue = document.createElement("span");
    usageValue.dataset.processUsageValue = "true";
    usageValue.textContent = "暂无数据";
    const usageSection = createSection("Token usage", "process-token-usage", usageValue);
    usageSection.dataset.processUsage = "true";

    const reasoning = document.createElement("details");
    reasoning.className = "process-reasoning";
    reasoning.hidden = true;
    const reasoningSummary = document.createElement("summary");
    reasoningSummary.textContent = "深度思考";
    const reasoningBody = document.createElement("p");
    reasoningBody.dataset.processReasoningBody = "true";
    const reasoningLimitNotice = document.createElement("p");
    reasoningLimitNotice.className = "process-reasoning-notice";
    reasoningLimitNotice.dataset.processReasoningNotice = "true";
    reasoningLimitNotice.hidden = true;
    reasoning.append(reasoningSummary, reasoningBody, reasoningLimitNotice);

    const assistantOutput = document.createElement("details");
    assistantOutput.className = "process-assistant-output";
    assistantOutput.hidden = true;
    const assistantSummary = document.createElement("summary");
    assistantSummary.textContent = "答复生成";
    const assistantBody = document.createElement("p");
    assistantBody.dataset.processAssistantBody = "true";
    assistantOutput.append(assistantSummary, assistantBody);

    details.append(
      summary,
      state,
      actions,
      events,
      createSection("Agent", "process-agent-section", agentCards),
      createSection("工具调用", "process-tool-section", toolCards),
      usageSection,
      reasoning,
      assistantOutput,
    );
    panel.append(details);
    stream.append(panel);
    stream.scrollTop = stream.scrollHeight;
    return {
      panel,
      details,
      state,
      events,
      orb,
      cancelButton,
      agentCards,
      toolCards,
      usageSection,
      usageValue,
      reasoning,
      reasoningBody,
      reasoningLimitNotice,
      assistantOutput,
      assistantBody,
      agentCardMap: new Map(),
      toolCardMap: new Map(),
      pendingRows: new Map(),
      nextSeq: 0,
      answerText: "",
      reasoningText: "",
      usage: null,
      failures: 0,
      outcome: "",
    };
  };
  const updateProcessCard = (map, parent, key, title, event, kind) => {
    const cardKey = key || `${kind}:main`;
    let card = map.get(cardKey);
    if (!card) {
      card = document.createElement("article");
      card.className = `process-${kind}-card`;
      card.dataset.processCard = kind;
      const heading = document.createElement("strong");
      heading.dataset.processCardTitle = "true";
      const status = document.createElement("span");
      status.dataset.processCardStatus = "true";
      const detail = document.createElement("p");
      detail.dataset.processCardDetail = "true";
      card.append(heading, status, detail);
      parent.append(card);
      map.set(cardKey, card);
    }
    card.dataset.status = event.status;
    card.querySelector("[data-process-card-title]").textContent = title;
    card.querySelector("[data-process-card-status]").textContent = runtimeStatusLabel(event.status);
    card.querySelector("[data-process-card-detail]").textContent = event.summary;
    parent.parentElement.hidden = false;
  };
  const appendProcessTimeline = (playback, projection) => {
    if (!projection.showTimeline) return;
    const item = document.createElement("li");
    item.dataset.status = projection.status;
    item.dataset.runtimeFamily = projection.family;
    item.textContent = projection.timelineLabel;
    playback.events.append(item);
  };
  const appendRuntimeProjection = (playback, event) => {
    const projection = projectRuntimeEvent(event);
    if (!projection) return;
    appendProcessTimeline(playback, projection);
    if (projection.family === "agent") {
      updateProcessCard(
        playback.agentCardMap,
        playback.agentCards,
        projection.agentKey || projection.role,
        projection.role || "Agent",
        projection,
        "agent",
      );
    }
    if (projection.family === "tool") {
      updateProcessCard(
        playback.toolCardMap,
        playback.toolCards,
        projection.toolKey,
        projection.toolLabel,
        projection,
        "tool",
      );
    }
    if (projection.assistantDelta) {
      playback.answerText = appendRuntimeDelta(playback.answerText, projection.assistantDelta, runtimeAssistantTextLimit);
      playback.assistantBody.textContent = playback.answerText;
      playback.assistantOutput.hidden = !playback.answerText;
    }
    if (projection.showReasoning) {
      const reasoningChunk = projection.reasoningDelta || (!playback.reasoningText ? projection.reasoningNotice : "");
      playback.reasoningText = appendRuntimeDelta(playback.reasoningText, reasoningChunk, runtimeReasoningTextLimit);
      playback.reasoningBody.textContent = playback.reasoningText;
      if (projection.reasoningTruncationNotice) {
        playback.reasoningLimitNotice.textContent = projection.reasoningTruncationNotice;
        playback.reasoningLimitNotice.hidden = false;
      }
      playback.reasoning.hidden = !playback.reasoningText && playback.reasoningLimitNotice.hidden;
    }
    if (projection.usage) {
      playback.usage = projection.usage;
      const usage = projection.usage;
      const parts = [];
      if (usage.inputTokens !== null) parts.push(`输入${formatRuntimeTokens(usage.inputTokens)}`);
      if (usage.outputTokens !== null) parts.push(`输出${formatRuntimeTokens(usage.outputTokens)}`);
      if (usage.reasoningTokens !== null) parts.push(`思考${formatRuntimeTokens(usage.reasoningTokens)}`);
      if (usage.toolTokens !== null) parts.push(`工具${formatRuntimeTokens(usage.toolTokens)}`);
      if (usage.totalTokens !== null) parts.push(`合计${formatRuntimeTokens(usage.totalTokens)}`);
      if (usage.cachedTokens !== null) parts.push(`缓存${formatRuntimeTokens(usage.cachedTokens)}`);
      if (usage.calls !== null) parts.push(`调用${formatRuntimeTokens(usage.calls)}`);
      playback.usageValue.textContent = parts.join("，") || "暂无数据";
      playback.usageSection.hidden = false;
    }
    playback.orb.setState(runtimeOrbState(projection));
    if ((projection.family === "workflow" || projection.family === "recovery" || ["failed", "cancelled", "interrupted", "stopped"].includes(projection.status)) && runtimeTerminalStatuses.has(projection.status)) {
      playback.outcome = projection.status;
    }
    if (projection.family === "recovery" && projection.status === "recovered") {
      playback.state.textContent = "进度已恢复，正在继续读取";
      setRuntimeLiveStatus("本轮进度已恢复，正在继续读取。");
    } else if (projection.status === "failed") {
      playback.state.textContent = "本轮处理未完成";
      setRuntimeLiveStatus(projection.summary);
    } else if (projection.status === "cancelled" || projection.status === "interrupted") {
      playback.state.textContent = "本轮处理已取消";
      setRuntimeLiveStatus(projection.summary);
    } else if (projection.status === "running" || projection.status === "started") {
      playback.state.textContent = projection.summary;
      setRuntimeLiveStatus(projection.summary);
    }
  };
  const finishProcessPlayback = (playback) => {
    const outcome = playback.outcome || "completed";
    const terminalCopy = {
      failed: "本轮处理未完成",
      cancelled: "本轮处理已取消",
      interrupted: "本轮处理已中断",
      stopped: "本轮处理已停止",
    }[outcome] || "本轮处理已结束";
    playback.state.textContent = terminalCopy;
    playback.panel.dataset.terminal = "true";
    playback.panel.dataset.outcome = outcome;
    playback.cancelButton.hidden = true;
    playback.orb.setState("shaping");
    playback.orb.setPaused(true);
    setRuntimeLiveStatus(terminalCopy);
  };
  const appendProcessEvents = (playback, rows, terminal = false) => {
    const drained = consumeRuntimeEventRows(playback, rows);
    playback.nextSeq = drained.nextSeq;
    playback.pendingRows = drained.pending;
    for (const event of drained.emitted) appendRuntimeProjection(playback, event);
    if (terminal && playback.pendingRows.size === 0) finishProcessPlayback(playback);
    else if (playback.nextSeq > 0 && !playback.panel.dataset.terminal) playback.state.textContent = "正在运行";
    stream.scrollTop = stream.scrollHeight;
  };
  const startProcessPlayback = (taskId, requestId, { restore = false, onCancel = null } = {}) => {
    const playback = appendProcessPanel({ onCancel });
    let stopped = false;
    let timer = 0;
    const poll = async () => {
      if (stopped) return;
      try {
        const payload = await request(processEventsPath(taskId, requestId, playback.nextSeq));
        if (playback.failures > 0) {
          playback.state.textContent = "进度连接已恢复，正在同步";
          setRuntimeLiveStatus("进度连接已恢复，正在同步。");
          playback.failures = 0;
        }
        appendProcessEvents(playback, payload.events, payload.terminal === true);
        if (payload.terminal === true && playback.pendingRows.size === 0) { stopped = true; return; }
      } catch (error) {
        playback.failures += 1;
        playback.state.textContent = error.status === 404
          ? "正在等待本轮进度"
          : "进度连接暂时中断，正在恢复";
        playback.orb.setState("breathing");
        setRuntimeLiveStatus(playback.state.textContent);
      }
      if (!stopped) timer = window.setTimeout(poll, 350);
    };
    void poll();
    return {
      ...playback,
      taskId,
      requestId,
      stop() {
        stopped = true;
        window.clearTimeout(timer);
        playback.orb.destroy();
      },
      restore,
    };
  };
  const cancelTaskRequest = async (taskId, requestId) => {
    if (!taskId || !requestId) return;
    if (activeConversation?.taskId === taskId && activeConversation.requestId === requestId) {
      submit.dataset.cancelRequested = "true";
      submit.disabled = true;
      submit.setAttribute("aria-label", "正在取消本次处理");
    }
    if (activeProcessPlayback?.taskId === taskId && activeProcessPlayback.requestId === requestId) {
      activeProcessPlayback.state.textContent = "正在取消本轮处理";
      activeProcessPlayback.cancelButton.disabled = true;
      activeProcessPlayback.cancelButton.textContent = "正在取消";
    }
    try {
      await request(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
        method: "POST",
        body: safeJson({}),
      });
      showWorkspaceStatus("正在取消本轮处理。", false);
      setRuntimeLiveStatus("正在取消本轮处理。");
    } catch (error) {
      if (activeConversation?.taskId === taskId && activeConversation.requestId === requestId) {
        submit.dataset.cancelRequested = "false";
        syncComposerAvailability();
      }
      if (activeProcessPlayback?.taskId === taskId && activeProcessPlayback.requestId === requestId) {
        activeProcessPlayback.cancelButton.disabled = false;
        activeProcessPlayback.cancelButton.textContent = "取消本轮";
      }
      showWorkspaceStatus(error.message || "取消请求未完成，请稍后重试。", true);
    }
  };
  const lastConversationRequestId = (messages) => {
    const last = [...(messages || [])].reverse().find((entry) => entry?.role === "assistant");
    const match = String(last?.message_id || "").match(/^request-(.+)-assistant$/);
    return match?.[1] || "";
  };
  const restoreLatestProcess = (task) => {
    const requestId = lastConversationRequestId(task?.messages);
    if (!requestId || currentTask?.task_id !== task.task_id) return;
    activeProcessPlayback?.stop();
    activeProcessPlayback = startProcessPlayback(task.task_id, requestId, {
      restore: true,
      onCancel: () => cancelTaskRequest(task.task_id, requestId),
    });
  };
  const newWorkspaceRequestId = () => globalThis.crypto?.randomUUID?.()
    || `chat-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const conversationResponsePath = (taskId, requestId) => (
    `/api/tasks/${encodeURIComponent(taskId)}/conversation-requests/${encodeURIComponent(requestId)}`
  );
  const selectedModel = () => {
    const raw = document.querySelector("#workspace-model-picker")?.value || "";
    if (!raw) return null;
    try { return JSON.parse(raw); } catch { return null; }
  };
  const explicitSelectedModel = () => (
    modelPicker?.dataset.userExplicitSelection === "true" ? selectedModel() : null
  );
  const sendConversation = (taskId, content, requestId, modelSelection) => request(
    `/api/tasks/${encodeURIComponent(taskId)}/messages`,
    { method: "POST", body: safeJson({ content, model_selection: modelSelection, background: true }), headers: { "X-Request-Id": requestId } },
  );
  const waitForOperation = async (taskId, operationId) => {
    while (true) {
      const { operation } = await request(`/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}`);
      if (operation.state === "succeeded") return operation.result || {};
      if (["failed", "cancelled", "interrupted"].includes(operation.state)) {
        const error = new Error(operation.message || "后台运行未完成。");
        error.status = operation.state === "failed" ? 500 : 409;
        error.body = operation.result || {};
        error.operationTerminal = true;
        throw error;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 350));
    }
  };
  const lookupConversationResponse = async (taskId, requestId) => {
    try {
      return (await request(conversationResponsePath(taskId, requestId))).response || null;
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  };
  const submitConversation = async (taskId, content, requestId, modelSelection) => {
    const submitted = await sendConversation(taskId, content, requestId, modelSelection);
    const operationId = submitted.operation?.operation_id;
    if (!operationId) throw new Error("后台对话未返回操作标识。");
    return waitForOperation(taskId, operationId);
  };
  const syncReportActions = () => {
    const hasTask = Boolean(currentTask?.task_id);
    reportActions?.querySelectorAll("[data-report-kind]").forEach((button) => {
      const kind = button.dataset.reportKind;
      const permitted = button.dataset.permitted !== "false";
      button.disabled = !hasTask || !permitted;
      button.hidden = !permitted;
      if (!permitted) button.removeAttribute("aria-describedby");
      else button.setAttribute("aria-describedby", "report-feedback");
      button.title = !hasTask
        ? "请先选择任务"
        : kind === "report" ? "生成详细报告" : kind === "quote" ? "生成参考报价" : "生成简单报告";
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
  const updateTransient = (taskId, updates) => {
    const state = { ...readTransient(taskId), ...updates };
    try { sessionStorage.setItem(transientKey(taskId), JSON.stringify(state)); } catch { /* Tab state is optional. */ }
    return state;
  };
  const settleConversationRequest = (taskId, requestId, { draft, keepPending = false } = {}) => {
    const state = readTransient(taskId);
    const pending = state.pendingConversation;
    const matches = pending?.taskId === taskId && pending?.requestId === requestId;
    updateTransient(taskId, {
      draft: draft ?? state.draft ?? "",
      pendingConversation: matches ? (keepPending ? pending : null) : (pending || null),
    });
    if (currentTask?.task_id === taskId && pendingConversationRequest?.requestId === requestId) {
      pendingConversationRequest = keepPending ? pendingConversationRequest : null;
    }
  };
  const settleReportRequest = (taskId, requestId, { keepPending = false } = {}) => {
    const state = readTransient(taskId);
    const pending = state.pendingReport;
    const matches = pending?.taskId === taskId && pending?.requestId === requestId;
    updateTransient(taskId, {
      pendingReport: matches ? (keepPending ? pending : null) : (pending || null),
    });
    if (currentTask?.task_id === taskId && pendingReportRequest?.requestId === requestId) {
      pendingReportRequest = keepPending ? pendingReportRequest : null;
    }
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
      pendingConversation: pendingConversationRequest?.taskId === currentTask?.task_id
        ? pendingConversationRequest
        : null,
      pendingReport: pendingReportRequest?.taskId === currentTask?.task_id
        ? pendingReportRequest
        : null,
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
    const pending = state.pendingConversation;
    pendingConversationRequest = pending
      && pending.taskId === currentTask?.task_id
      && typeof pending.requestId === "string"
      && typeof pending.content === "string"
      ? pending
      : null;
    if (pendingConversationRequest) {
      activeProcessPlayback?.stop();
      activeProcessPlayback = startProcessPlayback(
        pendingConversationRequest.taskId,
        pendingConversationRequest.requestId,
        { restore: true },
      );
    }
    const pendingReport = state.pendingReport;
    pendingReportRequest = pendingReport
      && pendingReport.taskId === currentTask?.task_id
      && typeof pendingReport.requestId === "string"
      && typeof pendingReport.kind === "string"
      ? pendingReport
      : null;
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
    syncChatComposerClearance();
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
    const enteringDeskFromChat = previousMode === "chat" && nextMode === "desk";
    shell.dataset.assistantSwitching = "false";
    if (enteringDeskFromChat) {
      // Chat renders this same region as its in-flow composer. Desk turns it
      // into a floating assistant panel, so never carry a hidden Chat state
      // into an open Desk drawer or let the in-flow composer repaint mid-swap.
      setAssistantOpen(false, { persist: false });
      shell.dataset.assistantSwitching = "true";
    }
    currentMode = nextMode;
    if (previousMode !== currentMode) clearWorkspaceStatus();
    shell.dataset.switching = "true";
    shell.dataset.mode = currentMode;
    const deskSurface = document.querySelector("#desk-surface");
    chatSurface.inert = currentMode !== "chat";
    deskSurface.inert = currentMode !== "desk";
    chatSurface.setAttribute("aria-hidden", String(chatSurface.inert));
    deskSurface.setAttribute("aria-hidden", String(deskSurface.inert));
    if (nextMode !== "desk") activateFrame(null);
    document.querySelectorAll("button[data-mode]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.mode === currentMode)));
    placeConversation();
    if (updateHistory) {
      const next = new URL(modePath(currentMode), location.origin);
      if (currentTask?.task_id) next.searchParams.set("task", currentTask.task_id);
      if (currentMode === "desk" && currentModule) next.searchParams.set("module", currentModule);
      history.replaceState({ mode: currentMode, task: currentTask?.task_id || "" }, "", `${next.pathname}${next.search}`);
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
      shell.dataset.assistantSwitching = "false";
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
    const activeCount = tasks.reduce((total, task) => total + (task.active_operations?.length || 0), 0);
    window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type: "active_operations", count: activeCount});
    window.clearTimeout(operationRefreshTimer);
    if (activeCount > 0) {
      operationRefreshTimer = window.setTimeout(() => {
        loadTasks().catch(() => {});
      }, 900);
    }
    renderTaskList(
      rail,
      tasks,
      currentTask?.task_id,
      (id) => selectTask(id).catch((error) => showWorkspaceStatus(error.message, true)),
      { onRename: openTaskRenameDialog, onDelete: openTaskDeleteDialog },
    );
    return tasks;
  }

  function resetTaskSelection() {
    currentTask = null;
    pendingConversationRequest = null;
    pendingReportRequest = null;
    clearReportFeedback();
    disposeModuleFrames();
    syncReportActions();
    title.textContent = "未选择任务";
    conversationTitle.textContent = "开始研究";
    taskState.textContent = "请新建或选择任务后，再运行研究模块。";
    renderMessages(stream, [], "新建任务后即可开始对话，并按需生成简单报告、详细报告或参考报价。");
    renderReports(reports, []);
    chatSurface.classList.add("chat-surface--empty");
    mount?.replaceChildren(createModuleStart());
    const next = new URL(location.href);
    next.searchParams.delete("task");
    next.searchParams.delete("module");
    history.replaceState(null, "", next);
  }

  function createModuleStart() {
    const start = document.createElement("div");
    start.className = "conversation-start";
    const content = document.createElement("div");
    const heading = document.createElement("h2");
    heading.textContent = "选择研究模块";
    const copy = document.createElement("p");
    copy.className = "conversation-start__copy";
    copy.textContent = "各模块页面保持独立运行能力，并在OptDesk中承载同一任务上下文。";
    content.append(heading, copy);
    start.append(content);
    return start;
  }

  function moduleLoadingSurface(moduleName) {
    return Array.from(mount?.querySelectorAll("[data-module-loading]") || [])
      .find((surface) => surface.dataset.module === moduleName) || null;
  }

  function syncModuleStageState(moduleName = currentModule) {
    if (!mount) return;
    let activeSurface = null;
    mount.querySelectorAll("[data-module-loading]").forEach((surface) => {
      const active = surface.dataset.module === moduleName;
      surface.hidden = !active;
      if (active) activeSurface = surface;
    });
    const state = activeSurface?.dataset.state || "ready";
    mount.dataset.loading = activeSurface ? state : "false";
    mount.setAttribute("aria-busy", String(state === "loading"));
  }

  function showModuleLoading(moduleName, { replace = false } = {}) {
    if (!mount) return;
    if (replace) mount.replaceChildren();
    let loading = moduleLoadingSurface(moduleName);
    if (!loading) {
      loading = document.createElement("div");
      loading.className = "module-stage__loading";
      loading.dataset.moduleLoading = "true";
      mount.append(loading);
    }
    loading.dataset.module = moduleName;
    loading.dataset.state = "loading";
    loading.classList.remove("module-stage__loading--error");
    loading.setAttribute("role", "status");
    loading.setAttribute("aria-live", "polite");
    loading.textContent = `正在打开${modules.get(moduleName) || "研究模块"}…`;
    syncModuleStageState();
  }

  function clearModuleLoading(moduleName, taskId) {
    if (!mount || currentTask?.task_id !== taskId) return;
    moduleLoadingSurface(moduleName)?.remove();
    syncModuleStageState();
  }

  function showModuleLoadFailure(moduleName, taskId, detail = "") {
    if (!mount || currentTask?.task_id !== taskId) return;
    let loading = moduleLoadingSurface(moduleName);
    if (!loading) {
      loading = document.createElement("div");
      loading.className = "module-stage__loading";
      loading.dataset.moduleLoading = "true";
      loading.dataset.module = moduleName;
      mount.append(loading);
    }
    const message = document.createElement("p");
    message.textContent = detail || `${modules.get(moduleName) || "研究模块"}暂未就绪。`;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "module-stage__retry";
    retry.textContent = "重新打开";
    retry.addEventListener("click", () => {
      void mountModule(moduleName, true, { forceRetry: true });
    });
    loading.replaceChildren(message, retry);
    loading.dataset.state = "error";
    loading.classList.add("module-stage__loading--error");
    loading.setAttribute("role", "alert");
    loading.removeAttribute("aria-live");
    syncModuleStageState();
  }

  function openTaskDialog({ title: dialogTitle, description, confirmLabel, danger = false, initialSubject = "", onConfirm }) {
    const dialog = document.createElement("dialog");
    dialog.className = "task-dialog";
    dialog.setAttribute("aria-labelledby", "task-dialog-title");
    const form = document.createElement("form");
    form.className = "task-dialog__form";
    const heading = document.createElement("h2");
    heading.id = "task-dialog-title";
    heading.textContent = dialogTitle;
    const copy = document.createElement("p");
    copy.className = "task-dialog__copy";
    copy.textContent = description;
    const feedback = document.createElement("p");
    feedback.className = "task-dialog__feedback";
    feedback.hidden = true;
    feedback.setAttribute("role", "alert");
    let subjectInput = null;
    if (initialSubject) {
      const label = document.createElement("label");
      label.className = "task-dialog__field";
      label.textContent = "任务名称";
      subjectInput = document.createElement("input");
      subjectInput.className = "task-rename-input";
      subjectInput.type = "text";
      subjectInput.name = "subject";
      subjectInput.value = initialSubject;
      subjectInput.maxLength = 160;
      subjectInput.required = true;
      label.append(subjectInput);
      form.append(heading, copy, label, feedback);
    } else {
      form.append(heading, copy, feedback);
    }
    const actions = document.createElement("div");
    actions.className = "task-dialog__actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button-secondary";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => dialog.close());
    const confirm = document.createElement("button");
    confirm.type = "submit";
    confirm.className = danger ? "button-danger" : "button-primary";
    confirm.textContent = confirmLabel;
    actions.append(cancel, confirm);
    form.append(actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const subject = subjectInput?.value.trim();
      if (subjectInput && !subject) {
        feedback.textContent = "请输入任务名称。";
        feedback.hidden = false;
        subjectInput.focus();
        return;
      }
      confirm.disabled = true;
      cancel.disabled = true;
      try {
        await onConfirm(subject);
        dialog.close();
      } catch (error) {
        feedback.textContent = error.message || "操作未完成，请稍后重试。";
        feedback.hidden = false;
        confirm.disabled = false;
        cancel.disabled = false;
      }
    });
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.append(form);
    document.body.append(dialog);
    dialog.showModal();
    requestAnimationFrame(() => (subjectInput || confirm).focus());
  }

  function openTaskRenameDialog(task) {
    openTaskDialog({
      title: "重命名任务",
      description: "仅更新任务在任务栏中的名称，不影响对话和模块结果。",
      confirmLabel: "保存名称",
      initialSubject: String(task.subject || ""),
      onConfirm: async (subject) => {
        const { task: updated } = await request(`/api/tasks/${encodeURIComponent(task.task_id)}/rename`, {
          method: "POST",
          body: safeJson({ subject }),
        });
        if (currentTask?.task_id === updated.task_id) {
          currentTask = { ...currentTask, ...updated };
          title.textContent = updated.subject;
          conversationTitle.textContent = updated.subject;
        }
        await loadTasks();
        showWorkspaceStatus("任务名称已更新。", false, 3500);
      },
    });
  }

  function openTaskDeleteDialog(task) {
    openTaskDialog({
      title: "删除任务？",
      description: "将删除该任务及其对话记录。模块的原始结果、报告和数据资产不会被删除。",
      confirmLabel: "删除任务",
      danger: true,
      onConfirm: async () => {
        await request(`/api/tasks/${encodeURIComponent(task.task_id)}/delete`, {
          method: "POST",
          body: safeJson({}),
        });
        if (currentTask?.task_id === task.task_id) resetTaskSelection();
        await loadTasks();
        showWorkspaceStatus("任务已删除。", false, 3500);
      },
    });
  }

  async function selectTask(taskId, updateLocation = true) {
    const selectionRevision = ++taskSelectionRevision;
    moduleMountRevision += 1;
    const isCurrentSelection = () => selectionRevision === taskSelectionRevision;
    if (currentTask) saveTransient();
    activeProcessPlayback?.stop();
    activeProcessPlayback = null;
    const { task } = await request(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (!isCurrentSelection()) return null;
    if (currentTask?.task_id && currentTask.task_id !== task.task_id) {
      disposeModuleFrames();
      if (currentMode === "desk") showModuleLoading(currentModule, { replace: true });
    }
    currentTask = task;
    pendingConversationRequest = null;
    pendingReportRequest = null;
    clearReportFeedback();
    syncReportActions();
    title.textContent = task.subject;
    conversationTitle.textContent = task.subject;
    taskState.textContent = "当前任务会保留对话、模块运行记录和关联报告。";
    renderMessages(stream, task.messages || [], "可直接输入任务要求，或选择一个研究起点。");
    restoreLatestProcess(task);
    chatSurface.classList.toggle("chat-surface--empty", !task.messages?.length);
    const { reports: reportRuns } = await request(`/api/tasks/${encodeURIComponent(taskId)}/reports`).catch(() => ({ reports: [] }));
    if (!isCurrentSelection()) return null;
    renderReports(reports, reportRuns);
    if (updateLocation) setTaskLocation(task.task_id, { module: currentMode === "desk" ? currentModule : "" });
    await loadTasks();
    if (!isCurrentSelection()) return null;
    restoreTransient();
    if (currentMode === "desk") {
      await mountModule(currentModule, false);
    }
    return task;
  }

  function disposeModuleFrames() {
    moduleMountRevision += 1;
    moduleNavigationController?.abort();
    moduleNavigationController = null;
    for (const timer of moduleFrameLoadTimers.values()) window.clearTimeout(timer);
    moduleFrameLoadTimers.clear();
    for (const frame of moduleFrames.values()) {
      frame.src = "about:blank";
      frame.remove();
    }
    moduleFrames.clear();
    moduleContexts.clear();
    moduleContextVersions.clear();
    moduleContextRefreshes.clear();
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

  function notifyModuleVisibility(frame, active) {
    if (!frame) return;
    frame.contentWindow?.postMessage({
      type: "optionhelper.module-visibility",
      active,
      bridge_nonce: frame.dataset.bridgeNonce,
    }, location.origin);
  }

  function activateFrame(moduleName) {
    for (const [name, frame] of moduleFrames) {
      const active = name === moduleName;
      frame.classList.toggle("is-active", active);
      frame.setAttribute("aria-hidden", String(!active));
      frame.tabIndex = active ? 0 : -1;
      notifyModuleVisibility(frame, active);
    }
    syncModuleStageState(moduleName);
  }

  function deliverContext(moduleName) {
    const frame = moduleFrames.get(moduleName);
    const context = moduleContexts.get(moduleName);
    const bridgeNonce = frame?.dataset.bridgeNonce;
    if (frame?.dataset.ready === "true" && context && bridgeNonce) {
      frame.contentWindow?.postMessage({
        type: "optionhelper.module-host-context",
        context,
        context_version: moduleContextVersions.get(moduleName) || 1,
        bridge_nonce: bridgeNonce,
      }, location.origin);
      frame.contentWindow?.postMessage({ type: "optionhelper.desk-panel-layout", layout: sharedPanelLayout, bridge_nonce: bridgeNonce }, location.origin);
      frame.contentWindow?.postMessage({
        type: "optionhelper.module-theme",
        theme: currentTheme(),
        preference: currentThemePreference(),
        bridge_nonce: bridgeNonce,
      }, location.origin);
    }
  }

  function broadcastModuleTheme() {
    for (const frame of moduleFrames.values()) {
      frame.contentWindow?.postMessage({
        type: "optionhelper.module-theme",
        theme: currentTheme(),
        preference: currentThemePreference(),
        bridge_nonce: frame.dataset.bridgeNonce,
      }, location.origin);
    }
  }

  function setModuleContext(moduleName, context) {
    moduleContexts.set(moduleName, context);
    moduleContextVersions.set(moduleName, (moduleContextVersions.get(moduleName) || 0) + 1);
  }

  async function refreshModuleContext(moduleName) {
    const existing = moduleContextRefreshes.get(moduleName);
    if (existing) return existing;
    if (!currentTask || !moduleFrames.has(moduleName)) return null;
    const taskId = currentTask.task_id;
    const frame = moduleFrames.get(moduleName);
    const task = `?task_id=${encodeURIComponent(taskId)}`;
    let refresh;
    refresh = request(`/api/module-host/${encodeURIComponent(moduleName)}${task}`).then(({ context }) => {
      // The response is scoped to the frame and task that requested it.  A
      // completed request from a discarded iframe must not overwrite the
      // context of the newly selected task.
      if (currentTask?.task_id !== taskId || moduleFrames.get(moduleName) !== frame) return null;
      setModuleContext(moduleName, context);
      deliverContext(moduleName);
      return context;
    }).finally(() => {
      if (moduleContextRefreshes.get(moduleName) === refresh) moduleContextRefreshes.delete(moduleName);
    });
    moduleContextRefreshes.set(moduleName, refresh);
    return refresh;
  }

  async function refreshMountedModuleContexts() {
    return Promise.all([...moduleFrames.keys()].map((moduleName) => refreshModuleContext(moduleName)));
  }

  function childNeedsFreshModuleContext(moduleName, contextVersion) {
    if (!moduleContexts.has(moduleName)) return true;
    const childVersion = Number(contextVersion);
    const parentVersion = moduleContextVersions.get(moduleName) || 0;
    // A matching version means the child has exhausted the same lease Desk
    // still holds.  Refresh through the App so the new signed context keeps
    // the current task, permission and contract binding.  A lagging child
    // receives the already-issued context without another server request.
    return Number.isSafeInteger(childVersion) && childVersion >= parentVersion;
  }

  function createModuleFrame(moduleName, taskId) {
    const bridgeNonce = createModuleBridgeNonce();
    const frame = document.createElement("iframe");
    frame.title = modules.get(moduleName);
    frame.src = `/capability/assets/pages/${encodeURIComponent(moduleName)}/${encodeURIComponent(moduleName)}.html?host=optdesk&bridge_nonce=${encodeURIComponent(bridgeNonce)}`;
    frame.className = "module-frame";
    frame.dataset.bridgeNonce = bridgeNonce;
    frame.dataset.taskId = taskId;
    frame.setAttribute("aria-hidden", "true");
    frame.addEventListener("load", () => {
      if (moduleFrames.get(moduleName) !== frame || currentTask?.task_id !== taskId) return;
      window.clearTimeout(moduleFrameLoadTimers.get(moduleName));
      moduleFrameLoadTimers.delete(moduleName);
      frame.dataset.ready = "true";
      delete frame.dataset.loadError;
      deliverContext(moduleName);
      if (moduleContexts.has(moduleName)) clearModuleLoading(moduleName, taskId);
    });
    frame.addEventListener("error", () => {
      if (moduleFrames.get(moduleName) !== frame || currentTask?.task_id !== taskId) return;
      window.clearTimeout(moduleFrameLoadTimers.get(moduleName));
      moduleFrameLoadTimers.delete(moduleName);
      frame.dataset.loadError = "true";
      showModuleLoadFailure(moduleName, taskId, `${modules.get(moduleName)}页面未能载入。`);
    });
    moduleFrames.set(moduleName, frame);
    mount.querySelector(".conversation-start")?.remove();
    mount.append(frame);
    moduleFrameLoadTimers.set(moduleName, window.setTimeout(() => {
      if (moduleFrames.get(moduleName) !== frame || frame.dataset.ready === "true" || currentTask?.task_id !== taskId) return;
      frame.dataset.loadError = "true";
      showModuleLoadFailure(moduleName, taskId, `${modules.get(moduleName)}页面打开超时。`);
    }, 12_000));
    return frame;
  }

  async function mountModule(moduleName, updateLocation = true, { forceRetry = false } = {}) {
    if (!modules.has(moduleName) || !currentTask) return;
    const taskId = currentTask.task_id;
    const mountRevision = ++moduleMountRevision;
    const isCurrentMount = () => (
      mountRevision === moduleMountRevision
      && currentTask?.task_id === taskId
      && currentModule === moduleName
    );
    moduleNavigationController?.abort();
    moduleNavigationController = null;
    currentModule = moduleName;
    setActiveModule(moduleName);
    if (updateLocation) setTaskLocation(taskId, { module: moduleName });

    let frame = moduleFrames.get(moduleName);
    if (forceRetry && frame?.dataset.loadError === "true") {
      window.clearTimeout(moduleFrameLoadTimers.get(moduleName));
      moduleFrameLoadTimers.delete(moduleName);
      frame.src = "about:blank";
      frame.remove();
      moduleFrames.delete(moduleName);
      moduleContexts.delete(moduleName);
      moduleContextVersions.delete(moduleName);
      frame = null;
    }
    if (!frame) frame = createModuleFrame(moduleName, taskId);
    activateFrame(moduleName);

    if (!forceRetry && frame.dataset.ready === "true" && moduleContexts.has(moduleName)) {
      deliverContext(moduleName);
      clearModuleLoading(moduleName, taskId);
      return frame;
    }

    showModuleLoading(moduleName);
    const controller = new AbortController();
    moduleNavigationController = controller;
    let timedOut = false;
    const contextTimeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 8_000);
    try {
      const task = `?task_id=${encodeURIComponent(taskId)}`;
      const { context } = await request(`/api/module-host/${encodeURIComponent(moduleName)}${task}`, { signal: controller.signal });
      if (!isCurrentMount()) return null;
      if (moduleFrames.get(moduleName) !== frame) return null;
      setModuleContext(moduleName, context);
      deliverContext(moduleName);
      if (frame.dataset.ready === "true") clearModuleLoading(moduleName, taskId);
      return frame;
    } catch (error) {
      if (!isCurrentMount()) return null;
      if (error.name === "AbortError" && !timedOut) return null;
      const staleTask = error.status === 409 && error.body?.error === "stale_task_contract";
      const detail = timedOut
        ? `${modules.get(moduleName)}上下文同步超时。`
        : staleTask
          ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。")
          : `${modules.get(moduleName)}暂未就绪。`;
      showModuleLoadFailure(moduleName, taskId, detail);
      showWorkspaceStatus(
        detail,
        true,
        7000,
      );
      return null;
    } finally {
      window.clearTimeout(contextTimeout);
      if (moduleNavigationController === controller) moduleNavigationController = null;
    }
  }

  window.addEventListener("message", (event) => {
    const moduleName = event.data?.module;
    const frame = moduleFrames.get(moduleName);
    // WKWebView may replace the iframe WindowProxy while a capability page is
    // navigating.  The bridge nonce is created by Desk and is the stable
    // per-frame binding, so do not discard a valid ready message merely
    // because WebKit changed the proxy object identity.
    if (event.origin !== location.origin || !frame) return;
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
    if (event.data?.type === "optionhelper.module-host-context-ack") {
      frame.dataset.contextVersion = String(event.data.context_version || "");
      return;
    }
    if (event.data?.type === "optionhelper.module-host-context-request") {
      frame.dataset.ready = "true";
      const refresh = childNeedsFreshModuleContext(moduleName, event.data.context_version)
        ? refreshModuleContext(moduleName)
        : Promise.resolve(deliverContext(moduleName));
      void refresh.catch(() => {
        showWorkspaceStatus("模块上下文暂未同步。请稍后重试。", true, 7000);
      });
      return;
    }
    if (event.data?.type === "optionhelper.module-contract-updated") {
      void refreshMountedModuleContexts().catch(() => {
        showWorkspaceStatus("产品方案已更新，但模块上下文暂未同步。请稍后重试。", true, 7000);
      });
      return;
    }
    if (event.data?.type !== "optionhelper.module-host-ready") return;
    frame.dataset.ready = "true";
    deliverContext(moduleName);
  });
  onThemeChange(() => broadcastModuleTheme());

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
  const composerResizeObserver = new ResizeObserver(syncChatComposerClearance);
  composerResizeObserver.observe(form);
  composerResizeObserver.observe(assistant);
  composerResizeObserver.observe(workArea);
  syncChatComposerClearance();
  tabs.addEventListener("scroll", () => syncModuleTabIndicator(false), { passive: true });
  window.addEventListener("resize", () => {
    syncModuleTabIndicator(false);
    syncChatComposerClearance();
  }, { passive: true });
  assistantToggle.addEventListener("click", () => setAssistantOpen(!assistantOpen, { focus: true }));
  assistantClose.addEventListener("click", () => { setAssistantOpen(false); assistantToggle.focus(); });
  stream.addEventListener("scroll", () => {
    if (!restoringScroll) saveTransient();
  }, { passive: true });
  input.addEventListener("input", () => {
    saveTransient();
    syncComposerAvailability();
  });
  modelPicker.addEventListener("change", syncComposerAvailability);
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

  submit.addEventListener("click", (event) => {
    if (submit.dataset.sending !== "true") return;
    event.preventDefault();
    if (submit.dataset.cancelRequested === "true") return;
    const taskId = activeConversation?.taskId || currentTask?.task_id;
    const requestId = activeConversation?.requestId || pendingConversationRequest?.requestId;
    void cancelTaskRequest(taskId, requestId);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.dataset.sending === "true") return;
    const submittedContent = input.value.trim();
    if (!submittedContent || !selectedModel()) {
      if (!selectedModel()) showWorkspaceStatus("请先在设置中心添加并保存一个可用模型。", true);
      return;
    }
    let pendingMessage;
    let processPlayback;
    let submittedTaskId = "";
    let requestId = "";
    setComposerSending(submit, true);
    clearWorkspaceStatus();
    try {
      if (!currentTask) {
        const task = await createTask();
        migrateTransient("new", task.task_id);
        await selectTask(task.task_id);
      }
      pendingMessage = appendPendingMessage(submittedContent);
      dissolveComposerInput(submittedContent);
      input.value = "";
      saveTransient();
      requestId = pendingConversationRequest?.taskId === currentTask.task_id
        && pendingConversationRequest.content === submittedContent
        ? pendingConversationRequest.requestId
        : newWorkspaceRequestId();
      const explicitModelSelection = pendingConversationRequest?.taskId === currentTask.task_id
        && pendingConversationRequest.content === submittedContent
        ? pendingConversationRequest.modelSelection
        : explicitSelectedModel();
      pendingConversationRequest = {
        taskId: currentTask.task_id,
        requestId,
        content: submittedContent,
        modelSelection: explicitModelSelection,
      };
      saveTransient();
      submittedTaskId = currentTask.task_id;
      activeConversation = { taskId: submittedTaskId, requestId };
      activeProcessPlayback?.stop();
      processPlayback = startProcessPlayback(submittedTaskId, requestId, {
        onCancel: () => cancelTaskRequest(submittedTaskId, requestId),
      });
      activeProcessPlayback = processPlayback;
      const response = await submitConversation(
        submittedTaskId,
        submittedContent,
        requestId,
        explicitModelSelection,
      );
      settleConversationRequest(submittedTaskId, requestId, { draft: "" });
      if (currentTask?.task_id === submittedTaskId) {
        modelPicker.dataset.userExplicitSelection = "false";
        markComposerSent(submit);
        input.value = "";
        saveTransient();
        applyConversationStatus(response, status, taskState);
        await selectTask(submittedTaskId, false);
      }
    } catch (error) {
      if (!submittedTaskId || !requestId) {
        showWorkspaceStatus(error.message || "新建任务未完成，请稍后重试。", true);
        return;
      }
      // A completed response can be durable even when its HTTP response was
      // interrupted on the way back to the browser. Recover by request id,
      // never by comparing message text or creating another model turn.
      const recoveredResponse = submittedTaskId && requestId
        ? await lookupConversationResponse(submittedTaskId, requestId).catch(() => null)
        : null;
      if (recoveredResponse) {
        pendingMessage?.remove();
        settleConversationRequest(submittedTaskId, requestId, { draft: "" });
        if (currentTask?.task_id === submittedTaskId) {
          modelPicker.dataset.userExplicitSelection = "false";
          input.value = "";
          saveTransient();
          applyConversationStatus(recoveredResponse, status, taskState);
          await selectTask(submittedTaskId, false).catch(() => {});
        }
        return;
      }
      pendingMessage?.remove();
      settleConversationRequest(submittedTaskId, requestId, { draft: submittedContent, keepPending: true });
      const recovery = error.body?.error?.next_step || error.body?.next_step;
      const failureMessage = error.status === 409 && error.body?.error === "stale_task_contract"
        ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。")
        : error.status === 503
        ? `${recovery || "模型暂不可用，请在设置中心检查模型服务。"}任务内容已保留。`
        : error.message;
      if (currentTask?.task_id === submittedTaskId) {
        input.value = submittedContent;
        saveTransient();
        showWorkspaceStatus(failureMessage, true);
      }
    } finally {
      processPlayback?.stop();
      if (activeConversation?.taskId === submittedTaskId && activeConversation.requestId === requestId) {
        activeConversation = null;
      }
      setComposerSending(submit, false);
    }
  });
  bindComposerKeyboard(form);
  syncComposerAvailability();

  document.querySelectorAll("[data-report-kind]").forEach((button) => button.addEventListener("click", async () => {
    if (!currentTask) { showReportFeedback("请先新建或选择任务。", true); return; }
    button.disabled = true;
    const taskId = currentTask.task_id;
    const kind = button.dataset.reportKind;
    const requestId = pendingReportRequest?.taskId === taskId
      && pendingReportRequest.kind === kind
      ? pendingReportRequest.requestId
      : newWorkspaceRequestId();
    pendingReportRequest = { taskId, kind, requestId };
    saveTransient();
    let operationId = "";
    try {
      const response = await request(`/api/tasks/${encodeURIComponent(taskId)}/reports`, {
        method: "POST",
        body: safeJson({ kind, background: true }),
        headers: { "X-Request-Id": requestId },
      });
      operationId = response.operation?.operation_id || "";
      const result = operationId
        ? await waitForOperation(taskId, operationId)
        : response?.result || {};
      settleReportRequest(taskId, requestId);
      if (result.status === "needs_input") {
        showReportFeedback("当前任务还没有可用于生成报告的正式结果。请先完成收益结构、估值定价或历史回测中的至少一项分析。", true);
      } else if (result.status === "completed") {
        const label = ({ card: "简单报告", report: "详细报告", quote: "参考报价" })[kind] || "交付物";
        showReportFeedback(`${label}已生成，可在报告列表中预览或下载。`);
      } else {
        showReportFeedback("报告暂未生成。请先完成当前任务的正式分析结果。", true);
      }
      if (result.status === "completed" && currentTask?.task_id === taskId) await selectTask(taskId, false);
    } catch (error) {
      if (error.operationTerminal) settleReportRequest(taskId, requestId);
      else settleReportRequest(taskId, requestId, { keepPending: true });
      showReportFeedback(reportFailureMessage(error), true);
    } finally {
      syncReportActions();
    }
  }));

  window.addEventListener("pagehide", () => {
    composerResizeObserver.disconnect();
    window.cancelAnimationFrame(composerClearanceFrame);
    saveTransient();
  }, { once: true });
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
    await applyMode(mode);
    if (!currentTask && mode === "desk") {
      showWorkspaceStatus("请先新建或选择任务后再使用研究模块。", true);
    }
  } });
  if (!session) return;
  document.querySelectorAll("[data-report-kind]").forEach((button) => {
    const capability = ({
      card: "report.card.request",
      quote: "report.quote.request",
      report: "report.full.request",
    })[button.dataset.reportKind];
    button.dataset.permitted = String(session.capabilities.includes(capability));
  });
  syncReportActions();
  const [, tasks] = await Promise.all([
    configureModelPicker(modelPicker).catch(() => {}),
    loadTasks(),
  ]);
  const requested = taskIdFromLocation();
  if (requested) await selectTask(requested, false).catch(() => {});
  // A workspace entry never creates or silently resumes a task. A task exists
  // only after an explicit rail action, task URL, or the first submitted chat
  // message; this keeps a fresh OptDesk entry unbound and product-neutral.
  if (!currentTask) {
    title.textContent = "未选择任务";
    conversationTitle.textContent = "开始研究";
    taskState.textContent = "请新建或选择任务后，再运行研究模块。";
    renderMessages(stream, [], "新建任务后即可开始对话，并按需生成简单报告、详细报告或参考报价。");
  }
  await applyMode(initialMode, { updateHistory: false });
  restoreTransient();
  syncComposerAvailability();
  if (initialMode === "desk" && currentTask) await mountModule(currentModule, false);
  requestAnimationFrame(() => requestAnimationFrame(revealWorkspace));
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
