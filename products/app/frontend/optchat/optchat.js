import { bindComposerKeyboard, clearMessage, configureModelPicker, createReasoningDisclosure, initializeWorkspace, message, renderMessages, renderReports, renderTaskList, request, safeJson, setTaskLocation, settingsURLFor, taskIdFromLocation } from "/app/frontend/shared/app.js";
import { attachmentMediaType, validateAttachments } from "/app/frontend/optchat/attachment-utils.js";
import { createThinkingOrb } from "/app/frontend/shared/thinking-orb.js";
import { createTransitionScope } from "/app/frontend/shared/transition-scope.js";
import { currentTheme, currentThemePreference, onThemeChange } from "/app/frontend/shared/theme.js";

const modules = new Map([["datafetcher", "数据获取"], ["payoffer", "收益结构"], ["pricer", "估值定价"], ["backtester", "历史回测"], ["reporter", "研究报告"]]);
const operationStates = new Map([
  ["queued", "等待运行"], ["running", "运行中"], ["recovering", "正在恢复"],
  ["cancel_requested", "正在取消"], ["succeeded", "已完成"], ["failed", "失败"],
  ["cancelled", "已取消"], ["interrupted", "已中断"],
]);
const operationOutcomeStates = new Map([
  ["needs_input", "等待输入"], ["pending_approval", "等待确认"], ["partial", "部分完成"],
  ["stopped_duplicate", "已安全停止"], ["max_rounds", "等待继续"], ["timed_out", "已超时"],
  ["blocked", "受限"], ["cancelled", "已取消"], ["unavailable", "暂不可用"],
]);
const activeOperationStates = new Set(["queued", "running", "recovering", "cancel_requested"]);
const terminalOperationStates = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

const prioritizedOperationRows = (operations = [], limit = 8) => [...operations]
  .sort((left, right) => Number(activeOperationStates.has(right.state)) - Number(activeOperationStates.has(left.state))
    || String(right.updated_at || "").localeCompare(String(left.updated_at || "")))
  .slice(0, limit);

const loadOperationResultWithin = (loadResult, taskId, operationId, { signal, timeoutMs }) => new Promise((resolve, reject) => {
  const controller = new AbortController();
  let settled = false;
  let timer = 0;
  const finish = (callback, value) => {
    if (settled) return;
    settled = true;
    globalThis.clearTimeout(timer);
    signal?.removeEventListener?.("abort", abort);
    callback(value);
  };
  const abort = () => {
    controller.abort();
    const error = new Error("Operation result hydration aborted");
    error.name = "AbortError";
    finish(reject, error);
  };
  if (signal?.aborted) {
    abort();
    return;
  }
  signal?.addEventListener?.("abort", abort, { once: true });
  timer = globalThis.setTimeout(abort, Math.max(250, Number(timeoutMs) || 6_000));
  Promise.resolve()
    .then(() => loadResult(taskId, operationId, { signal: controller.signal }))
    .then((value) => finish(resolve, value), (error) => finish(reject, error));
});

export async function hydrateOperationHistory(taskId, operations = [], loadResult, { signal, timeoutMs = 6_000 } = {}) {
  if (!taskId || typeof loadResult !== "function") return operations;
  const visibleIds = new Set(prioritizedOperationRows(operations)
    .filter((operation) => terminalOperationStates.has(operation?.state))
    .map((operation) => operation.operation_id));
  const hydrated = await Promise.all(operations.map(async (operation) => {
    if (!visibleIds.has(operation?.operation_id)) return operation;
    let result;
    try {
      result = await loadOperationResultWithin(loadResult, taskId, operation.operation_id, { signal, timeoutMs });
    } catch (error) {
      const status = Number(error?.status);
      if (!Number.isInteger(status) || status < 400 || [401, 403, 404].includes(status)) return operation;
      result = error?.body && typeof error.body === "object" ? error.body : {};
    }
    const detail = result && typeof result === "object" ? result : {};
    return {
      ...operation,
      result: detail,
      failure_code: detail.failure_code || operation.failure_code,
      stage: detail.stage || operation.stage,
      next_step: detail.next_step || operation.next_step,
      diagnostic_id: detail.diagnostic_id || operation.diagnostic_id,
    };
  }));
  return hydrated;
}

const attachmentFileKey = (file) => `${String(file?.name || "file").slice(0, 160)}:${Number(file?.size) || 0}:${Number(file?.lastModified) || 0}:${globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
const attachmentKindLabel = (referenceOrItem) => referenceOrItem?.kind === "image" || String(referenceOrItem?.media_type || referenceOrItem?.mediaType || "").startsWith("image/") ? "图片" : "文档";
const attachmentDisplayName = (value) => {
  const name = String(value?.name || "").trim();
  return name || (attachmentKindLabel(value) === "图片" ? "图片附件" : "研究文档");
};
const formatAttachmentBytes = (value) => {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)}MB`;
};
const randomAttachmentKey = () => globalThis.crypto?.randomUUID?.() || `attachment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
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
const runtimeSensitiveText = /(?:system[ _-]?prompt|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|bearer|secret|private[ _-]?key|tool[ _-]?arguments?|arguments?|run[ _-]?ref|module[ _-]?run[ _-]?ref|contract[ _-]?fingerprint|task[ _-]?id|request[ _-]?id|tenant[ _-]?id|principal[ _-]?id)/i;
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
  if (normalized === "assistant.block_started" || normalized === "assistant.block_completed") return "block";
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
  const delta = family === "reasoning"
    ? runtimeDeltaText(row.delta ?? row.text ?? payload.delta ?? payload.text ?? payload.reasoning_delta ?? payload.reasoning)
    : family === "assistant"
      ? runtimeDeltaText(row.delta ?? row.text ?? payload.delta ?? payload.text)
      : "";
  const usage = family === "usage"
    ? normalizeRuntimeUsage(payload.usage || row.usage || payload || row)
    : normalizeRuntimeUsage(payload.usage || row.usage);
  return Object.freeze({
    seq: row.seq,
    type,
    family,
    scope: String(row.scope || payload.scope || "").trim().toLowerCase(),
    status,
    summary: summary || defaultRuntimeSummary(family, status, role, toolLabel),
    createdAt: String(row.created_at || row.createdAt || row.timestamp || row.time || payload.created_at || payload.timestamp || "").trim(),
    role,
    agentKey: safeRuntimeKey(row.agent_run_id || row.agentId || payload.agent_run_id || payload.agentId || (type === "agent_run" ? "legacy-agent" : "")),
    toolKey: safeRuntimeKey(row.call_id || row.tool_call_id || payload.call_id || payload.tool_call_id || `${toolLabel}:${row.agent_run_id || payload.agent_run_id || "main"}`),
    toolLabel,
    delta,
    turn: Number.isInteger(row.turn) && row.turn >= 0 ? row.turn : null,
    step: Number.isInteger(row.step) && row.step >= 0 ? row.step : null,
    blockIndex: Number.isInteger(payload.index) && payload.index >= 0 ? payload.index : null,
    blockType: String(payload.block_type || payload.block?.type || "").replaceAll("_", "-"),
    completedBlock: isRecord(payload.block) ? payload.block : null,
    reasoningAvailable: family === "reasoning" && Boolean(delta),
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
  const isMainAgent = event.scope === "main_agent";
  const mainAgentDetail = event.family === "tool" || event.family === "compaction" || event.family === "recovery";
  return Object.freeze({
    ...event,
    timelineLabel: event.summary,
    showTimeline: !isDelta && !["usage", "block"].includes(event.family) && (!isMainAgent || mainAgentDetail),
    showReasoning: event.family === "reasoning" && event.reasoningAvailable === true && Boolean(event.delta),
    assistantDelta: event.family === "assistant" && event.type === "assistant.text_delta" ? event.delta : "",
    reasoningDelta: event.family === "reasoning" ? event.delta : "",
    reasoningTruncationNotice: event.family === "reasoning" && Boolean(event.delta) && event.truncated
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
  const taskLists = Array.from(document.querySelectorAll("[data-task-list]"));
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
  const operationList = document.querySelector("#operation-list");
  const reportFeedback = document.querySelector("#report-feedback");
  const reportActions = document.querySelector("[data-report-actions]");
  const attachmentDropTarget = document.querySelector("[data-attachment-drop-target]");
  const attachmentTrigger = document.querySelector("[data-attachment-trigger]");
  const attachmentInput = document.querySelector("[data-attachment-input]");
  const attachmentArea = document.querySelector("#composer-attachments");
  const attachmentList = attachmentArea?.querySelector("[data-attachment-list]");
  const attachmentCount = attachmentArea?.querySelector("[data-attachment-count]");
  const attachmentDropHint = document.querySelector("[data-attachment-drop-hint]");
  const lightbox = document.querySelector("#attachment-lightbox");
  const lightboxImage = lightbox?.querySelector("[data-attachment-lightbox-image]");
  const lightboxName = lightbox?.querySelector("[data-attachment-lightbox-name]");
  const lightboxClose = lightbox?.querySelector("[data-attachment-lightbox-close]");
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
  let operationHistoryHydrationController = null;
  let operationHistoryLoadRevision = 0;
  let pendingConversationRequest = null;
  let pendingReportRequest = null;
  let activeProcessPlayback = null;
  let activeConversation = null;
  const reconnectingOperations = new Set();
  let operationRefreshHadActive = false;
  let draftAttachments = [];
  let attachmentDragDepth = 0;
  let attachmentDraftTaskId = "new";
  let attachmentDraftRevision = 0;
  const moduleFrames = new Map();
  const moduleContexts = new Map();
  const moduleContextVersions = new Map();
  const moduleContextRefreshes = new Map();
  const moduleContextRefreshGenerations = new Map();
  const moduleFrameLoadTimers = new Map();
  let moduleIndicatorFrame = 0;
  let composerClearanceFrame = 0;
  const panelLayoutKey = "optionhelper.desk-panel-widths";
  const clampPanelWidth = (value, minimum, maximum, fallback) => {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
  };
  const normalizePanelLayout = (layout) => ({
    left: clampPanelWidth(layout?.left, 200, 420, 250),
    right: clampPanelWidth(layout?.right, 200, 520, 250),
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

  const syncComposerInputHeight = () => {
    input.style.height = "auto";
    const maximum = 120;
    const height = Math.min(maximum, Math.max(30, input.scrollHeight));
    input.style.height = `${height}px`;
    input.dataset.overflow = String(input.scrollHeight > maximum);
    syncChatComposerClearance();
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
    if (error?.status >= 500) {
      return error.body?.next_step
        || error.body?.error?.next_step
        || error.body?.message
        || error.message
        || "报告服务暂时不可用，请稍后重试。";
    }
    if (!error?.status) return "无法连接报告服务，请检查连接后重试。";
    return error.body?.message || error.message || "报告生成失败，请稍后重试。";
  };
  const setComposerSending = (button, sending) => {
    if (sending) button.classList.remove("is-sent");
    button.dataset.sending = String(sending);
    if (!sending) button.dataset.cancelRequested = "false";
    button.disabled = button.dataset.cancelRequested === "true"
      || (!sending && (modelPicker.disabled || !modelPicker.value || (!input.value.trim() && draftAttachments.length === 0)));
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
      || (!sending && (modelPicker.disabled || !modelPicker.value || (!input.value.trim() && draftAttachments.length === 0)));
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
  const enterActiveConversation = () => {
    chatSurface.classList.remove("chat-surface--empty");
    for (const child of Array.from(stream.children)) {
      if (child.classList.contains("conversation-start")) child.remove();
    }
  };
  const appendPendingMessage = (content, attachments = []) => {
    enterActiveConversation();
    const pending = document.createElement("article");
    pending.className = "message message--user message--pending";
    const body = document.createElement("p");
    body.className = "message__body";
    body.textContent = content;
    if (content) pending.append(body);
    if (attachments.length) {
      const list = document.createElement("div");
      list.className = "message-attachments";
      for (const item of attachments) {
        const chip = document.createElement("span");
        chip.className = "message-attachment message-attachment--pending";
        chip.textContent = attachmentDisplayName(item);
        list.append(chip);
      }
      pending.append(list);
    }
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
  const attachmentUrl = (taskId, attachmentId) => (
    `/api/tasks/${encodeURIComponent(taskId)}/attachments/${encodeURIComponent(attachmentId)}`
  );
  const closeAttachmentLightbox = () => {
    if (!lightbox) return;
    if (lightbox.open) lightbox.close();
    if (lightboxImage) lightboxImage.removeAttribute("src");
  };
  const showAttachmentLightbox = (source, name) => {
    if (!lightbox || !lightboxImage) return;
    lightboxImage.src = source;
    lightboxImage.alt = name;
    if (lightboxName) lightboxName.textContent = name;
    lightbox.showModal();
  };
  const openStoredAttachment = (reference, taskId = currentTask?.task_id) => {
    if (!taskId || !reference?.attachment_id) return;
    const source = attachmentUrl(taskId, reference.attachment_id);
    if (reference.kind === "image") {
      showAttachmentLightbox(source, attachmentDisplayName(reference));
      return;
    }
    const link = document.createElement("a");
    link.href = source;
    link.download = attachmentDisplayName(reference);
    document.body.append(link);
    link.click();
    link.remove();
  };
  const revokeDraftPreview = (item) => {
    if (item?.ownedPreviewUrl && item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  };
  const draftTaskId = () => currentTask?.task_id || "new";
  const renderDraftAttachments = () => {
    if (!attachmentArea || !attachmentList) return;
    attachmentList.replaceChildren(...draftAttachments.map((item) => {
      const row = document.createElement("article");
      row.className = `composer-attachment composer-attachment--${attachmentKindLabel(item).toLowerCase()}`;
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "composer-attachment__preview";
      preview.setAttribute("aria-label", `${attachmentKindLabel(item) === "图片" ? "预览" : "查看"}${item.name}`);
      if (attachmentKindLabel(item) === "图片" && item.previewUrl) {
        const image = document.createElement("img");
        image.src = item.previewUrl;
        image.alt = "";
        preview.append(image);
        preview.addEventListener("click", () => showAttachmentLightbox(item.previewUrl, item.name));
      } else {
        preview.textContent = "文";
        preview.disabled = true;
      }
      const text = document.createElement("span");
      text.className = "composer-attachment__text";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const meta = document.createElement("small");
      meta.textContent = `${attachmentKindLabel(item)} ${formatAttachmentBytes(item.size)}`;
      text.append(name, meta);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "composer-attachment__remove";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `移除${item.name}`);
      remove.addEventListener("click", async () => {
        attachmentDraftRevision += 1;
        const target = draftAttachments.find((candidate) => candidate.key === item.key);
        revokeDraftPreview(target);
        draftAttachments = draftAttachments.filter((candidate) => candidate.key !== item.key);
        renderDraftAttachments();
        if (target?.reference?.attachment_id && currentTask?.task_id) {
          await request(`/api/tasks/${encodeURIComponent(currentTask.task_id)}/attachment-drafts/discard`, {
            method: "POST",
            body: safeJson({ attachment_ids: [target.reference.attachment_id] }),
          }).catch(() => showWorkspaceStatus("附件草稿未能从当前任务移除，请稍后重试。", true));
        }
        syncComposerAvailability();
      });
      row.append(preview, text, remove);
      return row;
    }));
    attachmentArea.hidden = draftAttachments.length === 0;
    if (attachmentCount) attachmentCount.textContent = String(draftAttachments.length);
    if (attachmentDropHint) attachmentDropHint.textContent = draftAttachments.length
      ? `已添加${draftAttachments.length}个附件`
      : "可粘贴、拖放或选择文件";
  };
  const addDraftFiles = async (files) => {
    if (submit.dataset.sending === "true") {
      showWorkspaceStatus("当前回复生成完成后再添加附件。", true);
      return false;
    }
    const incoming = Array.from(files || []).filter(Boolean);
    const error = validateAttachments(incoming, {
      existingCount: draftAttachments.length,
      existingBytes: draftAttachments.reduce((total, item) => total + item.size, 0),
    });
    if (error) {
      showWorkspaceStatus(error, true);
      return false;
    }
    if (!currentTask) {
      try {
        await selectTask((await createTask()).task_id);
      } catch {
        showWorkspaceStatus("附件需要绑定研究任务，但新建任务未完成。", true);
        return false;
      }
    }
    const targetTaskId = draftTaskId();
    attachmentDraftRevision += 1;
    const prepared = [];
    for (const file of incoming) {
      if (draftAttachments.some((item) => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) continue;
      const mediaType = attachmentMediaType(file);
      prepared.push({
        key: attachmentFileKey(file),
        file,
        name: String(file.name || "附件").slice(0, 160),
        mediaType,
        media_type: mediaType,
        size: file.size,
        lastModified: file.lastModified,
        kind: mediaType.startsWith("image/") ? "image" : "document",
        previewUrl: mediaType.startsWith("image/") ? URL.createObjectURL(file) : "",
        ownedPreviewUrl: mediaType.startsWith("image/"),
      });
    }
    if (!prepared.length) return true;
    showWorkspaceStatus("正在保存附件草稿。", false);
    let references;
    try {
      references = await uploadDraftAttachments(targetTaskId, prepared);
    } catch (uploadError) {
      prepared.forEach(revokeDraftPreview);
      showWorkspaceStatus(uploadError.message || "附件保存未完成。", true);
      await restoreAttachmentDrafts();
      return false;
    }
    prepared.forEach((item, index) => { item.reference = references[index]; });
    draftAttachments.push(...prepared);
    attachmentDraftTaskId = targetTaskId;
    renderDraftAttachments();
    clearWorkspaceStatus();
    syncComposerAvailability();
    return true;
  };
  const restoreAttachmentDrafts = async () => {
    const targetTaskId = draftTaskId();
    const revision = ++attachmentDraftRevision;
    const references = targetTaskId === "new"
      ? []
      : (await request(`/api/tasks/${encodeURIComponent(targetTaskId)}/attachment-drafts`).catch(() => ({ attachments: [] }))).attachments || [];
    if (revision !== attachmentDraftRevision || targetTaskId !== draftTaskId()) return;
    draftAttachments.forEach(revokeDraftPreview);
    draftAttachments = [];
    for (const reference of references) {
      draftAttachments.push({
        key: randomAttachmentKey(),
        reference,
        name: attachmentDisplayName(reference),
        mediaType: String(reference.media_type || ""),
        media_type: String(reference.media_type || ""),
        size: Number(reference.bytes) || 0,
        lastModified: 0,
        kind: reference.kind,
        previewUrl: reference.kind === "image" ? attachmentUrl(targetTaskId, reference.attachment_id) : "",
        ownedPreviewUrl: false,
      });
    }
    attachmentDraftTaskId = targetTaskId;
    renderDraftAttachments();
    syncComposerAvailability();
  };
  const fileBase64 = async (file) => {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  };
  const uploadDraftAttachments = async (taskId, items) => {
    if (!items.length) return [];
    const references = [];
    for (const item of items) {
      if (item.reference?.attachment_id) {
        references.push(item.reference);
        continue;
      }
      const response = await request(`/api/tasks/${encodeURIComponent(taskId)}/attachments`, {
        method: "POST",
        body: safeJson({ files: [{
          data: await fileBase64(item.file),
          media_type: item.mediaType,
          name: item.name,
        }] }),
      });
      if (!Array.isArray(response.attachments) || response.attachments.length !== 1) {
        throw new Error(`${item.name}上传后未返回有效引用。`);
      }
      references.push(response.attachments[0]);
    }
    return references;
  };
  const clearDraftAttachments = async (taskId = draftTaskId()) => {
    const ownsVisibleDraft = attachmentDraftTaskId === taskId && draftTaskId() === taskId;
    if (ownsVisibleDraft) {
      attachmentDraftRevision += 1;
      draftAttachments.forEach(revokeDraftPreview);
      draftAttachments = [];
      renderDraftAttachments();
    }
    if (ownsVisibleDraft) syncComposerAvailability();
  };
  const createLiveAssistantMessage = () => {
    const article = document.createElement("article");
    article.className = "message message--assistant message--streaming";
    article.hidden = true;
    const content = document.createElement("div");
    content.className = "message__content";
    article.append(content);
    return { article, content, blocks: new Map() };
  };
  const runtimeBlockKey = (projection, fallbackType = "text") => [
    projection.agentKey || "main",
    projection.turn ?? 0,
    projection.step ?? 0,
    projection.blockIndex ?? fallbackType,
  ].join(":");
  const ensureLiveBlock = (live, projection, blockType) => {
    if (!live) return null;
    const key = runtimeBlockKey(projection, blockType);
    let block = live.blocks.get(key);
    if (block) return block;
    if (blockType === "reasoning") {
      const disclosure = createReasoningDisclosure("", { running: true });
      const notice = document.createElement("p");
      notice.className = "assistant-reasoning__notice";
      notice.hidden = true;
      disclosure.element.append(notice);
      block = { type: "reasoning", text: "", element: disclosure.element, disclosure, notice };
    } else if (blockType === "text") {
      const element = document.createElement("p");
      element.className = "message__body";
      block = { type: "text", text: "", element };
    } else {
      return null;
    }
    live.blocks.set(key, block);
    live.content.append(block.element);
    return block;
  };
  const updateLiveBlock = (live, projection, blockType, delta) => {
    const block = ensureLiveBlock(live, projection, blockType);
    if (!block || !delta) return;
    const limit = blockType === "reasoning" ? runtimeReasoningTextLimit : runtimeAssistantTextLimit;
    block.text = appendRuntimeDelta(block.text, delta, limit);
    if (blockType === "reasoning") block.disclosure.update(block.text, true);
    else block.element.textContent = block.text;
    live.article.hidden = false;
  };
  const settleLiveReasoning = (live, projection = null) => {
    if (!live) return;
    for (const [key, block] of live.blocks) {
      if (block.type !== "reasoning") continue;
      if (projection && key !== runtimeBlockKey(projection, "reasoning")) continue;
      block.disclosure.update(block.text, false);
    }
  };
  const appendProcessPanel = ({ onCancel = null } = {}) => {
    const panel = document.createElement("article");
    panel.className = "message message--assistant message--process";
    panel.dataset.runtimeProcess = "true";
    panel.hidden = true;
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

    details.append(
      summary,
      state,
      actions,
      events,
      createSection("Agent", "process-agent-section", agentCards),
      createSection("工具调用", "process-tool-section", toolCards),
      usageSection,
    );
    panel.append(details);
    stream.append(panel);
    stream.scrollTop = stream.scrollHeight;
    return {
      panel,
      details,
      summaryLabel,
      state,
      events,
      orb,
      cancelButton,
      agentCards,
      toolCards,
      usageSection,
      usageValue,
      agentCardMap: new Map(),
      toolCardMap: new Map(),
      pendingRows: new Map(),
      nextSeq: 0,
      usage: null,
      failures: 0,
      outcome: "",
      runtimeScope: "",
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
      const reasoningHost = document.createElement("div");
      reasoningHost.className = "process-agent-reasoning";
      reasoningHost.hidden = true;
      reasoningHost.dataset.processAgentReasoning = "true";
      card.append(heading, status, detail, reasoningHost);
      card.reasoningHost = reasoningHost;
      card.reasoningBlocks = new Map();
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
    if (projection.scope === "main_agent" && playback.runtimeScope !== "main_agent") {
      playback.runtimeScope = "main_agent";
      playback.summaryLabel.textContent = "正在回复";
      playback.panel.hidden = true;
      playback.events.replaceChildren();
      playback.agentCards.replaceChildren();
      playback.agentCardMap.clear();
      playback.agentCards.parentElement.hidden = true;
    }
    const legacyBoilerplate = ["request", "routing", "answer", "terminal"].includes(projection.type);
    const hasProcessDetail = projection.family === "tool"
      || projection.family === "compaction"
      || projection.family === "recovery"
      || (!legacyBoilerplate && playback.runtimeScope !== "main_agent" && ["workflow", "agent", "turn", "step"].includes(projection.family));
    if (hasProcessDetail) playback.panel.hidden = false;
    if (!legacyBoilerplate) appendProcessTimeline(playback, projection);
    if (projection.family === "agent" && projection.role !== "MainAgent" && playback.runtimeScope !== "main_agent") {
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
    if (projection.family === "block" && playback.runtimeScope === "main_agent" && projection.blockType) {
      if (projection.type === "assistant.block_started") {
        ensureLiveBlock(playback.liveAssistant, projection, projection.blockType);
      } else if (projection.type === "assistant.block_completed") {
        const block = ensureLiveBlock(playback.liveAssistant, projection, projection.blockType);
        const completedText = runtimeDeltaText(projection.completedBlock?.text, projection.blockType === "reasoning" ? runtimeReasoningTextLimit : runtimeAssistantTextLimit);
        if (block && completedText && !block.text) {
          block.text = completedText;
          if (block.type === "reasoning") block.disclosure.update(block.text, false);
          else block.element.textContent = block.text;
          playback.liveAssistant.article.hidden = false;
        }
        if (projection.blockType === "reasoning") settleLiveReasoning(playback.liveAssistant, projection);
      }
    }
    if (projection.assistantDelta && playback.runtimeScope === "main_agent") {
      updateLiveBlock(playback.liveAssistant, projection, "text", projection.assistantDelta);
    }
    if (projection.showReasoning && projection.reasoningDelta) {
      if (playback.runtimeScope === "main_agent") {
        updateLiveBlock(playback.liveAssistant, projection, "reasoning", projection.reasoningDelta);
        const block = ensureLiveBlock(playback.liveAssistant, projection, "reasoning");
        if (block?.notice && projection.reasoningTruncationNotice) {
          block.notice.textContent = projection.reasoningTruncationNotice;
          block.notice.hidden = false;
        }
      } else {
        const card = playback.agentCardMap.get(projection.agentKey);
        if (card?.reasoningHost) {
          let roleBlock = card.reasoningBlocks.get(runtimeBlockKey(projection, "reasoning"));
          if (!roleBlock) {
            const disclosure = createReasoningDisclosure("", { running: true });
            roleBlock = { text: "", disclosure };
            card.reasoningBlocks.set(runtimeBlockKey(projection, "reasoning"), roleBlock);
            card.reasoningHost.append(disclosure.element);
            card.reasoningHost.hidden = false;
          }
          roleBlock.text = appendRuntimeDelta(roleBlock.text, projection.reasoningDelta, runtimeReasoningTextLimit);
          roleBlock.disclosure.update(roleBlock.text, true);
        }
      }
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
    settleLiveReasoning(playback.liveAssistant);
    for (const card of playback.agentCardMap.values()) {
      for (const block of card.reasoningBlocks || []) block[1].disclosure.update(block[1].text, false);
    }
    playback.details.open = false;
    if (playback.runtimeScope === "main_agent") {
      playback.summaryLabel.textContent = "本轮答复";
      if (!playback.panel.querySelector('[data-process-card="tool"]') && playback.events.childElementCount === 0) {
        playback.panel.hidden = true;
      }
    }
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
    playback.liveAssistant = restore ? null : createLiveAssistantMessage();
    if (playback.liveAssistant) stream.insertBefore(playback.liveAssistant.article, playback.panel);
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
  const publishModuleOperationCancel = (moduleName, operationId) => {
    const frame = moduleFrames.get(moduleName);
    if (!frame || !operationId) return;
    try {
      const FrameCustomEvent = frame.contentWindow?.CustomEvent;
      if (FrameCustomEvent) {
        frame.contentWindow.dispatchEvent(new FrameCustomEvent("optionhelper.module-operation-cancel", {
          detail: { module: moduleName, operation_id: operationId },
        }));
        return;
      }
    } catch {
      // A not-yet-loaded frame falls through to the controlled bridge message.
    }
    frame.contentWindow?.postMessage({
      type: "optionhelper.module-operation-cancel",
      module: moduleName,
      operation_id: operationId,
      bridge_nonce: frame.dataset.bridgeNonce,
    }, location.origin);
  };
  const cancelOperationRequest = async (taskId, operationId, { requestId = "", module = "" } = {}) => {
    if (!taskId || !operationId) return;
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
      await request(`/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}/cancel`, {
        method: "POST",
        body: safeJson({}),
      });
      if (module) publishModuleOperationCancel(module, operationId);
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
  const cancelConversationRequest = async (taskId, requestId, operationId) => {
    if (!taskId || !requestId) return;
    if (!operationId) {
      if (activeConversation?.taskId === taskId && activeConversation.requestId === requestId) {
        activeConversation.cancelWhenAccepted = true;
        submit.dataset.cancelRequested = "true";
        submit.disabled = true;
        submit.setAttribute("aria-label", "正在等待后台接受取消请求");
      }
      return;
    }
    await cancelOperationRequest(taskId, operationId, { requestId });
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
      onCancel: () => cancelConversationRequest(task.task_id, requestId, activeConversation?.operationId),
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
  const selectedModelSupportsImages = () => {
    const option = modelPicker?.selectedOptions?.[0];
    try {
      const modalities = JSON.parse(option?.dataset.inputModalities || '["text"]');
      return Array.isArray(modalities) && modalities.includes("image");
    } catch {
      return false;
    }
  };
  const explicitSelectedModel = () => (
    modelPicker?.dataset.userExplicitSelection === "true" ? selectedModel() : null
  );
  const sendConversation = (taskId, content, requestId, modelSelection, attachments = []) => request(
    `/api/tasks/${encodeURIComponent(taskId)}/messages`,
    { method: "POST", body: safeJson({ content, attachments, model_selection: modelSelection, background: true }), headers: { "X-Request-Id": requestId } },
  );
  const readOperationResult = async (taskId, operationId, onUpdate) => {
    let failures = 0;
    while (true) {
      try {
        return await request(`/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}/result`);
      } catch (error) {
        if ([401, 403, 404].includes(error.status)) throw error;
        failures += 1;
        onUpdate?.({ state: "recovering", message: "计算已完成，正在恢复结果读取。", recovery_attempts: failures });
        await new Promise((resolve) => window.setTimeout(resolve, Math.min(4_000, 500 * (2 ** Math.min(failures - 1, 3)))));
      }
    }
  };
  const readOperationFailure = async (taskId, operationId, operation) => {
    try {
      await request(`/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}/result`);
    } catch (source) {
      const sourceStatus = Number(source.status);
      if (!Number.isInteger(sourceStatus) || sourceStatus < 400) throw source;
      if ([401, 403, 404].includes(sourceStatus)) throw source;
      const body = source.body && typeof source.body === "object" ? source.body : {};
      const detail = [body.message || operation.message, body.next_step, body.error?.next_step]
        .filter(Boolean)
        .join(" ");
      const error = new Error(detail || "后台运行未完成。");
      error.status = sourceStatus;
      error.body = body;
      error.operationTerminal = true;
      throw error;
    }
    const error = new Error(operation.message || "后台运行未完成。");
    error.status = operation.state === "failed" ? 500 : 409;
    error.body = {};
    error.operationTerminal = true;
    throw error;
  };
  const waitForOperation = async (taskId, operationId, onUpdate = null) => {
    let failures = 0;
    while (true) {
      try {
        const { operation } = await request(`/api/tasks/${encodeURIComponent(taskId)}/operations/${encodeURIComponent(operationId)}?include_result=false`);
        failures = 0;
        onUpdate?.(operation);
        if (operation.state === "succeeded") return readOperationResult(taskId, operationId, onUpdate);
        if (["failed", "cancelled", "interrupted"].includes(operation.state)) {
          return readOperationFailure(taskId, operationId, operation);
        }
      } catch (error) {
        if (error.operationTerminal || [401, 403, 404].includes(error.status)) throw error;
        failures += 1;
        onUpdate?.({ state: "recovering", message: "状态连接暂时中断，正在恢复。", recovery_attempts: failures });
        await new Promise((resolve) => window.setTimeout(resolve, Math.min(4_000, 500 * (2 ** Math.min(failures - 1, 3)))));
        continue;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 750));
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
  const submitConversation = async (taskId, content, requestId, modelSelection, attachments = [], onAccepted = null) => {
    const submitted = await sendConversation(taskId, content, requestId, modelSelection, attachments);
    const operationId = submitted.operation?.operation_id;
    if (!operationId) throw new Error("后台对话未返回操作标识。");
    await onAccepted?.(operationId);
    void loadTasks()
      .then(() => currentTask?.task_id === taskId ? loadCurrentTaskOperations(taskId) : null)
      .catch(() => {});
    return waitForOperation(taskId, operationId, (operation) => {
      if (activeConversation?.taskId === taskId && activeConversation.requestId === requestId) {
        activeConversation.operationId = operationId;
        activeConversation.state = operation.state;
      }
      if (currentTask?.task_id === taskId) {
        setRuntimeLiveStatus(operation.message || operationStates.get(operation.state) || "正在处理当前请求。");
      }
    });
  };
  const operationDetailText = (operation) => {
    const result = operation?.result && typeof operation.result === "object" ? operation.result : {};
    const resultStatus = String(result.status || "").trim().toLowerCase();
    const primary = String(
      (operation?.state === "succeeded" && resultStatus && resultStatus !== "completed" ? result.message : "")
      || operation?.message
      || (activeOperationStates.has(operation?.state) ? "正在处理当前请求。" : "本次运行已结束。"),
    );
    const recovery = Number(operation?.recovery_attempts) > 0
      ? `已恢复${Number(operation.recovery_attempts)}次。`
      : "";
    const nextStep = operation?.state === "failed" ? String(result.next_step || operation?.next_step || "") : "";
    return [primary, recovery, nextStep].filter(Boolean).join(" ");
  };
  const renderTaskOperations = (operations = [], taskId = currentTask?.task_id) => {
    if (!operationList) return;
    if (!taskId) {
      operationList.innerHTML = '<p class="operation-empty">选择任务后查看运行状态。</p>';
      return;
    }
    const rows = prioritizedOperationRows(operations);
    if (!rows.length) {
      operationList.innerHTML = '<p class="operation-empty">当前任务暂无后台运行。</p>';
      return;
    }
    operationList.replaceChildren(...rows.map((operation) => {
      const card = document.createElement("article");
      card.className = "operation-item";
      card.dataset.state = String(operation.state || "");
      const heading = document.createElement("div");
      heading.className = "operation-item__heading";
      const name = document.createElement("strong");
      name.textContent = modules.get(operation.module) || (operation.kind === "conversation" ? "任务对话" : "后台任务");
      const state = document.createElement("span");
      state.className = "operation-item__state";
      const resultStatus = String(operation?.result?.status || "").trim().toLowerCase();
      state.textContent = operation.state === "succeeded" && operationOutcomeStates.has(resultStatus)
        ? operationOutcomeStates.get(resultStatus)
        : operationStates.get(operation.state) || "状态未知";
      heading.append(name, state);
      const detail = document.createElement("p");
      detail.textContent = operationDetailText(operation);
      card.append(heading, detail);
      if (operation.diagnostic_id) {
        const diagnostic = document.createElement("small");
        diagnostic.className = "operation-item__diagnostic";
        diagnostic.textContent = `诊断编号：${operation.diagnostic_id}`;
        card.append(diagnostic);
      }
      if (activeOperationStates.has(operation.state)) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "operation-item__cancel";
        cancel.textContent = operation.state === "cancel_requested" ? "正在取消" : "取消";
        cancel.disabled = operation.state === "cancel_requested";
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await cancelOperationRequest(taskId, operation.operation_id, { module: operation.module });
            await loadCurrentTaskOperations(taskId);
          } catch (error) {
            showWorkspaceStatus(error.message || "取消请求未完成。", true);
            cancel.disabled = false;
          }
        });
        card.append(cancel);
      }
      return card;
    }));
  };
  const loadCurrentTaskOperations = async (taskId) => {
    const loadRevision = ++operationHistoryLoadRevision;
    operationHistoryHydrationController?.abort();
    operationHistoryHydrationController = null;
    if (!taskId) { renderTaskOperations([], ""); return []; }
    const { operations = [] } = await request(`/api/tasks/${encodeURIComponent(taskId)}/operations?include_result=false`);
    if (loadRevision !== operationHistoryLoadRevision) return operations;
    if (currentTask?.task_id === taskId) renderTaskOperations(operations, taskId);
    const controller = new AbortController();
    operationHistoryHydrationController = controller;
    void hydrateOperationHistory(taskId, operations, (ownedTaskId, operationId, options) => request(
      `/api/tasks/${encodeURIComponent(ownedTaskId)}/operations/${encodeURIComponent(operationId)}/result`,
      options,
    ), { signal: controller.signal }).then((hydrated) => {
      if (!controller.signal.aborted && currentTask?.task_id === taskId) renderTaskOperations(hydrated, taskId);
    }).finally(() => {
      if (operationHistoryHydrationController === controller) operationHistoryHydrationController = null;
    });
    return operations;
  };
  const resumePendingConversation = async (pending) => {
    if (!pending?.operationId || pending.taskId !== currentTask?.task_id) return;
    const key = `conversation:${pending.operationId}`;
    if (reconnectingOperations.has(key)) return;
    reconnectingOperations.add(key);
    activeConversation = {
      taskId: pending.taskId,
      requestId: pending.requestId,
      operationId: pending.operationId,
      cancelWhenAccepted: false,
    };
    setComposerSending(submit, true);
    try {
      const response = await waitForOperation(pending.taskId, pending.operationId, (operation) => {
        if (activeConversation?.operationId === pending.operationId) activeConversation.state = operation.state;
        if (currentTask?.task_id === pending.taskId) {
          setRuntimeLiveStatus(operation.message || operationStates.get(operation.state) || "正在恢复后台对话。");
        }
      });
      settleConversationRequest(pending.taskId, pending.requestId, { draft: "" });
      if (currentTask?.task_id === pending.taskId) {
        applyConversationStatus(response, status, taskState);
        await selectTask(pending.taskId, false);
      }
    } catch (error) {
      const recovered = await lookupConversationResponse(pending.taskId, pending.requestId).catch(() => null);
      if (recovered) {
        settleConversationRequest(pending.taskId, pending.requestId, { draft: "" });
        if (currentTask?.task_id === pending.taskId) {
          applyConversationStatus(recovered, status, taskState);
          await selectTask(pending.taskId, false).catch(() => {});
        }
      } else {
        settleConversationRequest(pending.taskId, pending.requestId);
        if (currentTask?.task_id === pending.taskId) showWorkspaceStatus(error.message || "后台对话未完成。", true);
      }
    } finally {
      if (activeConversation?.operationId === pending.operationId) activeConversation = null;
      reconnectingOperations.delete(key);
      setComposerSending(submit, false);
    }
  };
  const resumePendingReport = async (pending) => {
    if (!pending?.operationId || pending.taskId !== currentTask?.task_id) return;
    const key = `report:${pending.operationId}`;
    if (reconnectingOperations.has(key)) return;
    reconnectingOperations.add(key);
    try {
      const result = await waitForOperation(pending.taskId, pending.operationId, (operation) => {
        if (pendingReportRequest?.operationId === pending.operationId) pendingReportRequest.status = operation.state;
        if (currentTask?.task_id === pending.taskId) {
          showReportFeedback(operation.message || operationStates.get(operation.state) || "正在恢复报告生成。", false);
        }
      });
      settleReportRequest(pending.taskId, pending.requestId);
      if (currentTask?.task_id === pending.taskId) {
        if (result.status === "completed") {
          showReportFeedback("交付物处理完成。报告列表已更新。", false);
          autoPreviewGeneratedHtml(result);
        } else {
          showReportFeedback("报告暂未生成。请先完成当前任务的正式分析结果。", true);
        }
        await selectTask(pending.taskId, false);
      }
    } catch (error) {
      settleReportRequest(pending.taskId, pending.requestId);
      if (currentTask?.task_id === pending.taskId) showReportFeedback(reportFailureMessage(error), true);
    } finally {
      reconnectingOperations.delete(key);
      syncReportActions();
    }
  };
  const resumeTransientOperations = () => {
    if (pendingConversationRequest?.operationId) void resumePendingConversation({ ...pendingConversationRequest });
    if (pendingReportRequest?.operationId) void resumePendingReport({ ...pendingReportRequest });
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
  const autoPreviewGeneratedHtml = (result) => {
    const raw = String(result?.preview_url || "").trim();
    if (!raw) return false;
    let target;
    try { target = new URL(raw, location.origin); } catch { return false; }
    if (target.origin !== location.origin
      || !target.pathname.startsWith("/api/reports/")
      || !target.pathname.includes("/artifacts/")
      || !target.pathname.toLowerCase().endsWith(".html")) return false;
    return Boolean(window.open(target.href, "_blank", "noopener"));
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
    const livePending = pendingConversationRequest?.taskId === taskId
      && pendingConversationRequest.requestId === requestId
      ? pendingConversationRequest
      : pending;
    updateTransient(taskId, {
      draft: draft ?? state.draft ?? "",
      pendingConversation: matches ? (keepPending ? livePending : null) : (pending || null),
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
    syncComposerInputHeight();
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
    resumeTransientOperations();
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
  const syncReportSurface = (mode) => {
    const desk = mode === "desk";
    const reportToggle = shell.querySelector("[data-report-toggle]");
    const panel = shell.querySelector("#task-context");
    const openLabel = desk ? "打开报告库" : "打开任务与交付";
    const closeLabel = desk ? "收起报告库" : "收起任务与交付";
    if (reportToggle) {
      reportToggle.dataset.openLabel = openLabel;
      reportToggle.dataset.closeLabel = closeLabel;
      const label = shell.dataset.reportOpen === "true" ? closeLabel : openLabel;
      reportToggle.setAttribute("aria-label", label);
      reportToggle.setAttribute("title", label);
      const accessibleText = reportToggle.querySelector(".sr-only");
      if (accessibleText) accessibleText.textContent = label;
    }
    if (panel) panel.setAttribute("aria-label", desk ? "报告库" : "任务与交付");
    const kicker = panel?.querySelector("[data-report-panel-kicker]");
    const heading = panel?.querySelector("[data-report-panel-title]");
    const close = panel?.querySelector("[data-report-close]");
    const operations = panel?.querySelector("[data-report-task-operations]");
    const copy = panel?.querySelector("[data-report-panel-copy]");
    if (kicker) kicker.textContent = desk ? "报告" : "当前任务";
    if (heading) heading.textContent = desk ? "报告库" : "任务与交付";
    if (close) close.setAttribute("aria-label", closeLabel);
    if (operations) {
      operations.hidden = desk;
      operations.inert = desk;
    }
    if (copy) {
      copy.textContent = desk
        ? "交付保存在当前设备，可按需预览、下载或切换格式。"
        : "运行状态与交付物均绑定当前任务。结果状态未知时不会自动重算。";
    }
  };
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
    syncReportSurface(currentMode);
    document.title = currentMode === "desk" ? "OptDesk | OptionHelper" : "OptChat | OptionHelper";
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
    const selectedTask = tasks.find((task) => task.task_id === currentTask?.task_id);
    if (selectedTask?.active_operations?.length) {
      renderTaskOperations(selectedTask.active_operations, selectedTask.task_id);
    } else if (operationRefreshHadActive && activeCount === 0 && currentTask?.task_id) {
      void loadCurrentTaskOperations(currentTask.task_id).catch(() => {});
    }
    operationRefreshHadActive = activeCount > 0;
    window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type: "active_operations", state: "ready", count: activeCount});
    window.clearTimeout(operationRefreshTimer);
    if (activeCount > 0) {
      operationRefreshTimer = window.setTimeout(() => {
        loadTasks().catch(() => {});
      }, 900);
    }
    taskLists.forEach((taskList) => {
      renderTaskList(
        taskList,
        tasks,
        currentTask?.task_id,
        (id) => selectTask(id)
          .then(() => {
            if (taskList.matches("[data-mobile-task-list]")) document.querySelector("[data-task-history-close]")?.click();
          })
          .catch((error) => showWorkspaceStatus(error.message, true)),
        { onRename: openTaskRenameDialog, onDelete: openTaskDeleteDialog },
      );
    });
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
    renderTaskOperations([], "");
    chatSurface.classList.add("chat-surface--empty");
    mount?.replaceChildren(createModuleStart());
    const next = new URL(location.href);
    next.searchParams.delete("task");
    next.searchParams.delete("module");
    history.replaceState(null, "", next);
    void restoreAttachmentDrafts();
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

  function showModuleRecovering(moduleName, taskId) {
    if (!mount || currentTask?.task_id !== taskId) return;
    const loading = moduleLoadingSurface(moduleName);
    if (!loading || loading.dataset.state !== "loading") return;
    loading.textContent = `正在恢复${modules.get(moduleName) || "研究模块"}连接…`;
    syncModuleStageState(moduleName);
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

  function renderTaskConversation(task) {
    renderMessages(stream, task?.messages || [], "可直接输入任务要求，或选择一个研究起点。", {
      onAttachmentOpen: (reference) => openStoredAttachment(reference, task.task_id),
      onQuestionAnswer: (answer) => {
        if (submit.dataset.sending === "true" || currentTask?.task_id !== task.task_id) return;
        input.value = String(answer || "").trim();
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.focus();
        if (input.value) form.requestSubmit();
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
    renderTaskConversation(task);
    restoreLatestProcess(task);
    const { reports: reportRuns } = await request(`/api/tasks/${encodeURIComponent(taskId)}/reports`).catch(() => ({ reports: [] }));
    if (!isCurrentSelection()) return null;
    renderReports(reports, reportRuns);
    await loadCurrentTaskOperations(taskId).catch(() => renderTaskOperations([], taskId));
    if (updateLocation) setTaskLocation(task.task_id, { module: currentMode === "desk" ? currentModule : "" });
    await loadTasks();
    if (!isCurrentSelection()) return null;
    restoreTransient();
    await restoreAttachmentDrafts();
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
    moduleContextRefreshGenerations.clear();
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

  async function refreshModuleContext(moduleName, { force = false } = {}) {
    if (force) {
      moduleContextRefreshGenerations.set(moduleName, (moduleContextRefreshGenerations.get(moduleName) || 0) + 1);
    }
    const generation = moduleContextRefreshGenerations.get(moduleName) || 0;
    const existing = moduleContextRefreshes.get(moduleName);
    if (existing && !force) return existing;
    if (!currentTask || !moduleFrames.has(moduleName)) return null;
    const taskId = currentTask.task_id;
    const frame = moduleFrames.get(moduleName);
    const task = `?task_id=${encodeURIComponent(taskId)}`;
    let refresh;
    refresh = (async () => {
      let failures = 0;
      const started = Date.now();
      while (
        currentTask?.task_id === taskId
        && moduleFrames.get(moduleName) === frame
        && (moduleContextRefreshGenerations.get(moduleName) || 0) === generation
      ) {
        try {
          const { context } = await request(`/api/module-host/${encodeURIComponent(moduleName)}${task}`);
          if (
            currentTask?.task_id !== taskId
            || moduleFrames.get(moduleName) !== frame
            || (moduleContextRefreshGenerations.get(moduleName) || 0) !== generation
          ) return null;
          setModuleContext(moduleName, context);
          deliverContext(moduleName);
          if (frame.dataset.ready === "true") clearModuleLoading(moduleName, taskId);
          return context;
        } catch (error) {
          const terminal = [401, 403, 404].includes(error.status)
            || (error.status === 409 && error.body?.error === "stale_task_contract");
          if (terminal) throw error;
          failures += 1;
          if (Date.now() - started >= 8_000) showModuleRecovering(moduleName, taskId);
          const delay = Math.min(4_000, 500 * (2 ** Math.min(failures - 1, 3)));
          await new Promise((resolve) => window.setTimeout(resolve, delay));
        }
      }
      return null;
    })().finally(() => {
      if (moduleContextRefreshes.get(moduleName) === refresh) moduleContextRefreshes.delete(moduleName);
    });
    moduleContextRefreshes.set(moduleName, refresh);
    return refresh;
  }

  async function refreshMountedModuleContexts() {
    return Promise.all([...moduleFrames.keys()].map((moduleName) => refreshModuleContext(moduleName, { force: true })));
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
      window.OptionHelperUIScale?.syncFrame?.(frame);
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
    moduleMountRevision += 1;
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
    void refreshModuleContext(moduleName).catch((error) => {
      if (currentTask?.task_id !== taskId || moduleFrames.get(moduleName) !== frame) return;
      const staleTask = error.status === 409 && error.body?.error === "stale_task_contract";
      const detail = staleTask
        ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。")
        : `${modules.get(moduleName)}上下文不可用。`;
      showModuleLoadFailure(moduleName, taskId, detail);
      showWorkspaceStatus(detail, true, 7000);
    });
    return frame;
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
    if (event.data?.type === "optionhelper.open-settings") {
      const section = event.data?.section === "data" ? "data" : "";
      location.assign(settingsURLFor(location, section));
      return;
    }
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
      void loadTasks()
        .then(() => currentTask?.task_id ? loadCurrentTaskOperations(currentTask.task_id) : null)
        .catch(() => {});
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
    if (pendingConversationRequest?.status === "uncertain") {
      pendingConversationRequest = null;
    }
    saveTransient();
    syncComposerInputHeight();
    syncComposerAvailability();
  });
  attachmentTrigger?.addEventListener("click", () => {
    if (submit.dataset.sending === "true") {
      showWorkspaceStatus("当前回复生成完成后再添加附件。", true);
      return;
    }
    attachmentInput?.click();
  });
  attachmentInput?.addEventListener("change", () => {
    void addDraftFiles(attachmentInput.files).finally(() => { attachmentInput.value = ""; });
  });
  input.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) return;
    event.preventDefault();
    void addDraftFiles(files);
  });
  attachmentDropTarget?.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    event.preventDefault();
    attachmentDragDepth += 1;
    attachmentDropTarget.dataset.dragging = "true";
  });
  attachmentDropTarget?.addEventListener("dragover", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  attachmentDropTarget?.addEventListener("dragleave", () => {
    attachmentDragDepth = Math.max(0, attachmentDragDepth - 1);
    if (attachmentDragDepth === 0) attachmentDropTarget.dataset.dragging = "false";
  });
  attachmentDropTarget?.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) return;
    event.preventDefault();
    attachmentDragDepth = 0;
    attachmentDropTarget.dataset.dragging = "false";
    void addDraftFiles(event.dataTransfer.files);
  });
  lightboxClose?.addEventListener("click", closeAttachmentLightbox);
  lightbox?.addEventListener("click", (event) => {
    if (event.target === lightbox) closeAttachmentLightbox();
  });
  modelPicker.addEventListener("change", syncComposerAvailability);
  stream.addEventListener("click", (event) => {
    const starter = event.target.closest("[data-starter-prompt]");
    if (!starter) return;
    input.value = starter.dataset.starterPrompt || "";
    syncComposerInputHeight();
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
    const operationId = activeConversation?.operationId || pendingConversationRequest?.operationId;
    void cancelConversationRequest(taskId, requestId, operationId);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.dataset.sending === "true") return;
    const submittedContent = input.value.trim();
    const submittedDrafts = [...draftAttachments];
    if ((!submittedContent && submittedDrafts.length === 0) || !selectedModel()) {
      if (!selectedModel()) showWorkspaceStatus("请先在设置中心添加并保存一个可用模型。", true);
      return;
    }
    if (submittedDrafts.some((item) => item.kind === "image") && !selectedModelSupportsImages()) {
      showWorkspaceStatus("当前模型未声明图片输入能力，请切换支持图片的模型后发送。", true);
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
      submittedTaskId = currentTask.task_id;
      const resumeUncertain = pendingConversationRequest?.taskId === currentTask.task_id
        && pendingConversationRequest.status === "uncertain";
      const effectiveContent = resumeUncertain ? pendingConversationRequest.content : submittedContent;
      const attachmentReferences = resumeUncertain
        ? (pendingConversationRequest.attachments || [])
        : await uploadDraftAttachments(submittedTaskId, submittedDrafts);
      pendingMessage = appendPendingMessage(effectiveContent, attachmentReferences);
      dissolveComposerInput(submittedContent);
      input.value = "";
      syncComposerInputHeight();
      saveTransient();
      requestId = resumeUncertain
        ? pendingConversationRequest.requestId
        : newWorkspaceRequestId();
      const explicitModelSelection = resumeUncertain
        ? pendingConversationRequest.modelSelection
        : explicitSelectedModel();
      pendingConversationRequest = {
        taskId: currentTask.task_id,
        requestId,
        content: effectiveContent,
        modelSelection: explicitModelSelection,
        attachments: attachmentReferences,
        operationId: resumeUncertain ? pendingConversationRequest.operationId || "" : "",
        status: "submitting",
      };
      saveTransient();
      activeConversation = {
        taskId: submittedTaskId,
        requestId,
        operationId: pendingConversationRequest.operationId,
        cancelWhenAccepted: false,
      };
      activeProcessPlayback?.stop();
      processPlayback = startProcessPlayback(submittedTaskId, requestId, {
        onCancel: () => cancelConversationRequest(submittedTaskId, requestId, activeConversation?.operationId),
      });
      activeProcessPlayback = processPlayback;
      const response = await submitConversation(
        submittedTaskId,
        effectiveContent,
        requestId,
        explicitModelSelection,
        attachmentReferences,
        async (operationId) => {
          if (pendingConversationRequest?.requestId === requestId) {
            pendingConversationRequest.operationId = operationId;
            pendingConversationRequest.status = "accepted";
            saveTransient();
          }
          if (activeConversation?.requestId === requestId) {
            activeConversation.operationId = operationId;
            if (activeConversation.cancelWhenAccepted) {
              await cancelConversationRequest(submittedTaskId, requestId, operationId);
            }
          }
          await clearDraftAttachments(submittedTaskId);
        },
      );
      settleConversationRequest(submittedTaskId, requestId, { draft: "" });
      if (currentTask?.task_id === submittedTaskId) {
        modelPicker.dataset.userExplicitSelection = "false";
        markComposerSent(submit);
        input.value = "";
        syncComposerInputHeight();
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
        await clearDraftAttachments(submittedTaskId);
        settleConversationRequest(submittedTaskId, requestId, { draft: "" });
        if (currentTask?.task_id === submittedTaskId) {
          modelPicker.dataset.userExplicitSelection = "false";
          input.value = "";
          syncComposerInputHeight();
          saveTransient();
          applyConversationStatus(recoveredResponse, status, taskState);
          await selectTask(submittedTaskId, false).catch(() => {});
        }
        return;
      }
      pendingMessage?.remove();
      if (currentTask?.task_id === submittedTaskId) renderTaskConversation(currentTask);
      const retryableTransport = !error.operationTerminal && ![401, 403, 404].includes(error.status);
      if (pendingConversationRequest?.requestId === requestId) {
        pendingConversationRequest.status = retryableTransport ? "uncertain" : "failed";
      }
      settleConversationRequest(submittedTaskId, requestId, {
        draft: pendingConversationRequest?.content || submittedContent,
        keepPending: retryableTransport,
      });
      const recovery = error.body?.error?.next_step || error.body?.next_step;
      const failureMessage = error.status === 409 && error.body?.error === "stale_task_contract"
        ? (error.body.message || "当前任务的合同来自旧产品目录。请新建研究任务后继续。")
        : error.status === 503
        ? `${recovery || "模型暂不可用，请在设置中心检查模型服务。"}任务内容已保留。`
        : error.message;
      if (currentTask?.task_id === submittedTaskId) {
        input.value = pendingConversationRequest?.content || submittedContent;
        syncComposerInputHeight();
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
  syncComposerInputHeight();
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
      if (operationId) {
        if (pendingReportRequest?.requestId === requestId) {
          pendingReportRequest.operationId = operationId;
          pendingReportRequest.status = "accepted";
          saveTransient();
        }
        void loadTasks()
          .then(() => currentTask?.task_id === taskId ? loadCurrentTaskOperations(taskId) : null)
          .catch(() => {});
      }
      const result = operationId
        ? await waitForOperation(taskId, operationId, (operation) => {
          if (pendingReportRequest?.requestId === requestId) pendingReportRequest.status = operation.state;
          showReportFeedback(operation.message || operationStates.get(operation.state) || "正在生成交付物。");
        })
        : response?.result || {};
      settleReportRequest(taskId, requestId);
      if (result.status === "needs_input") {
        showReportFeedback("当前任务还没有可用于生成报告的正式结果。请先完成收益结构、估值定价或历史回测中的至少一项分析。", true);
      } else if (result.status === "completed") {
        const label = ({ card: "简单报告", report: "详细报告", quote: "参考报价" })[kind] || "交付物";
        showReportFeedback(`${label}处理完成。报告列表会明确显示完整完成或部分完成状态。`);
        autoPreviewGeneratedHtml(result);
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
    renderTaskOperations([], "");
  }
  await applyMode(initialMode, { updateHistory: false });
  restoreTransient();
  await restoreAttachmentDrafts();
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

export function showWorkspaceStartupFailure(error) {
  const shell = document.querySelector("[data-workspace-shell]");
  shell?.setAttribute("aria-busy", "false");
  if (shell) shell.dataset.initializing = "false";
  document.body.classList.remove("workspace-body--initializing");
  const status = document.querySelector("#workspace-status");
  const signedOut = error?.status === 401 || error?.status === 403;
  message(
    status,
    signedOut
      ? "登录状态已失效，请重新登录。"
      : `工作台初始化未完成：${error?.message || "请确认OptionHelper服务可用后重试。"}`,
    true,
  );
}

if (location.pathname.startsWith("/optchat")) {
  void startWorkspace("chat").catch(showWorkspaceStartupFailure);
}
