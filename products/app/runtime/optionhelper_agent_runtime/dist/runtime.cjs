"use strict";

// src/entrypoint/runtime.ts
var import_node_readline = require("node:readline");
var import_node_vm = require("node:vm");

// src/core/runtime.ts
var import_node_crypto2 = require("node:crypto");
var import_node_path2 = require("node:path");

// src/core/agent.ts
var import_node_crypto = require("node:crypto");

// src/context/token-meter.ts
function blockText(block) {
  if (block.type === "tool-call") return `${block.name}
${block.arguments}`;
  if (block.type === "image") return `[image:${block.attachmentId}]`;
  if (block.type === "document-ref") return `[document:${block.attachmentId}:${block.name}]`;
  return block.text;
}
var TokenMeter = class {
  estimateMessages(messages) {
    let characters = 0;
    for (const message2 of messages) {
      characters += message2.role.length + 12;
      for (const block of message2.content) characters += blockText(block).length + 8;
    }
    return Math.ceil(characters / 3.2);
  }
  estimateEvents(events) {
    return Math.ceil(JSON.stringify(events).length / 3.2);
  }
  merge(...items) {
    const result = { input: 0, output: 0, reasoning: 0, cached: 0, total: 0 };
    for (const item of items) {
      if (item === void 0) continue;
      result.input += item.input ?? 0;
      result.output += item.output ?? 0;
      result.reasoning += item.reasoning ?? 0;
      result.cached += item.cached ?? 0;
      result.total += item.total ?? (item.input ?? 0) + (item.output ?? 0) + (item.reasoning ?? 0);
    }
    return result;
  }
};

// src/context/context-maintainer.ts
var FINANCIAL_KEYS = /* @__PURE__ */ new Set([
  "contract",
  "candidate_version",
  "candidateversion",
  "market_snapshot",
  "marketsnapshot",
  "greeks",
  "valuation",
  "backtest",
  "ledger",
  "fact_ref",
  "factrefs"
]);
function safeToolProjection(message2, maxChars) {
  if (message2.role !== "tool") return message2;
  const raw = message2.content.map((block) => {
    if (block.type === "tool-call") return block.arguments;
    if (block.type === "image") return `[image:${block.attachmentId}]`;
    if (block.type === "document-ref") return `[document:${block.attachmentId}]`;
    return block.text;
  }).join("\n");
  if (raw.length <= maxChars) return message2;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = void 0;
  }
  const facts = {};
  if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
    for (const [key, value] of Object.entries(parsed)) {
      const normalized = key.toLowerCase().replaceAll("-", "_");
      if (FINANCIAL_KEYS.has(normalized) || normalized.includes("fact_ref") || key === "status") {
        facts[key] = JSON.parse(JSON.stringify(value));
      }
    }
  }
  return {
    ...message2,
    content: [{ type: "text", text: JSON.stringify({ ...facts, pruned: true }) }]
  };
}
async function maintainContext(session, policy, summarize, signal) {
  const meter = new TokenMeter();
  let messages = session.deriveMessages();
  let estimatedTokens = meter.estimateMessages(messages);
  let pruned = false;
  let compacted = false;
  if (estimatedTokens >= policy.pruneAtTokens) {
    const next = messages.map((message2) => safeToolProjection(message2, policy.prunedToolResultChars));
    if (JSON.stringify(next) !== JSON.stringify(messages)) {
      session.append("context/tool-results-pruned", {
        messageCount: next.length
      });
      messages = next;
      estimatedTokens = meter.estimateMessages(messages);
      pruned = true;
    }
  }
  if (estimatedTokens >= policy.compactAtTokens) {
    const keep = Math.max(2, policy.preservedRecentMessages);
    const source = messages.slice(0, Math.max(0, messages.length - keep));
    const recent = messages.slice(-keep);
    if (source.length > 0) {
      session.append("compaction/start", { estimatedTokens });
      try {
        const summary = await summarize(source, 8e3, signal);
        if (!summary.trim()) throw new Error("context summary is empty");
        const sourceSeqs = session.surfaceEvents().filter((event) => {
          if (event.type === "compaction/summary") return true;
          const data = event.data;
          return typeof data.role === "string" && Array.isArray(data.content);
        }).slice(0, source.length).map((event) => event.seq);
        session.append("compaction/summary", { summary, sourceSeqs });
        messages = [{ id: `summary-${session.events.length}`, role: "system", content: [{ type: "text", text: summary }] }, ...recent];
        estimatedTokens = meter.estimateMessages(messages);
        compacted = true;
        session.append("compaction/end", { status: "completed", estimatedTokens });
      } catch (error) {
        session.append("compaction/end", { status: "failed", error: String(error) });
        throw error;
      }
    }
  }
  if (estimatedTokens > policy.maxContextTokens) throw new Error("context exceeds configured token limit");
  return { messages, estimatedTokens, pruned, compacted };
}

// src/llm/assembler.ts
function assertNever(value) {
  throw new Error(`unsupported stream chunk: ${JSON.stringify(value)}`);
}
var BlockAssembler = class {
  partials = /* @__PURE__ */ new Map();
  order = [];
  currentUsage;
  currentFinish;
  push(chunk) {
    switch (chunk.type) {
      case "block-start":
        if (!this.partials.has(chunk.index)) {
          this.order.push(chunk.index);
          this.partials.set(chunk.index, {
            blockType: chunk.blockType,
            text: "",
            toolCallArguments: ""
          });
        }
        return;
      case "text-delta":
      case "reasoning-delta": {
        const partial = this.ensure(chunk.index, chunk.type === "text-delta" ? "text" : "reasoning");
        if (partial.block === void 0) partial.text += chunk.text;
        return;
      }
      case "tool-call-delta": {
        const partial = this.ensure(chunk.index, "tool-call");
        if (partial.block !== void 0) return;
        partial.toolCallId = chunk.id;
        if (chunk.name !== void 0) partial.toolCallName = chunk.name;
        partial.toolCallArguments += chunk.argumentsDelta;
        return;
      }
      case "block-end": {
        const partial = this.ensure(chunk.index, chunk.block.type);
        partial.block ??= chunk.block;
        return;
      }
      case "usage":
        this.currentUsage = Object.freeze({ ...chunk.usage });
        return;
      case "finish":
        this.currentFinish = Object.freeze({ ...chunk.reason });
        return;
      default:
        return assertNever(chunk);
    }
  }
  ensure(index, blockType) {
    const current = this.partials.get(index);
    if (current !== void 0) return current;
    const created = { blockType, text: "", toolCallArguments: "" };
    this.partials.set(index, created);
    this.order.push(index);
    return created;
  }
  assemble(partial, index) {
    if (partial.block !== void 0) return partial.block;
    switch (partial.blockType) {
      case "text":
        return { type: "text", text: partial.text };
      case "reasoning":
        return { type: "reasoning", text: partial.text };
      case "tool-call":
        return {
          type: "tool-call",
          id: partial.toolCallId ?? `call-${index}`,
          name: partial.toolCallName ?? "",
          arguments: partial.toolCallArguments
        };
      case "image":
      case "document-ref":
        throw new Error("\u6A21\u578B\u8F93\u51FA\u6D41\u4E0D\u80FD\u521B\u5EFA\u9644\u4EF6\u5F15\u7528");
      default:
        return assertNever(partial.blockType);
    }
  }
  mustGet(index) {
    const partial = this.partials.get(index);
    if (partial === void 0) throw new Error(`missing stream block ${index}`);
    return partial;
  }
  blocks() {
    const all = this.order.map((index) => this.assemble(this.mustGet(index), index));
    return this.finish.kind === "max-tokens" ? all.filter((block) => block.type !== "tool-call") : all;
  }
  interruptedBlocks() {
    return this.order.map((index) => {
      const partial = this.mustGet(index);
      const type = partial.block?.type ?? partial.blockType;
      return type === "text" || type === "reasoning" ? this.assemble(partial, index) : void 0;
    }).filter((block) => block !== void 0 && (block.type === "text" || block.type === "reasoning") && block.text.trim().length > 0);
  }
  get usage() {
    return this.currentUsage;
  }
  get finish() {
    return this.currentFinish ?? { kind: "stop" };
  }
};

// src/llm/retry-policy.ts
var DEFAULT_RETRY_POLICY = Object.freeze({
  maxRetries: 5,
  initialDelayMs: 500,
  maxDelayMs: 1e4,
  jitterRatio: 0.1,
  retryableCodes: Object.freeze(["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"])
});
function retryDelay(policy, attempt, retryAfterMs) {
  const exponential = Math.min(policy.maxDelayMs, policy.initialDelayMs * 2 ** Math.max(0, attempt - 1));
  const proposed = retryAfterMs === void 0 ? exponential : Math.min(policy.maxDelayMs, retryAfterMs);
  const jitter = proposed * policy.jitterRatio;
  return Math.max(0, Math.round(proposed - jitter + Math.random() * jitter * 2));
}
function shouldRetry(policy, code, attempt, committedOutput) {
  return !committedOutput && attempt <= policy.maxRetries && policy.retryableCodes.includes(code);
}
async function waitForRetry(milliseconds, signal) {
  if (signal.aborted) throw signal.reason;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds);
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

// src/core/session.ts
function clone(value) {
  return structuredClone(value);
}
var AgentSession = class _AgentSession {
  constructor(store, header, events) {
    this.store = store;
    this.header = Object.freeze(clone(header));
    this.mutableEvents = events.map((event) => Object.freeze(clone(event)));
  }
  header;
  mutableEvents;
  static create(store, options) {
    const header = {
      id: options.id,
      kind: options.kind,
      ...options.parentId === void 0 ? {} : { parentId: options.parentId },
      label: options.label,
      createdAt: (/* @__PURE__ */ new Date()).toISOString(),
      importedLegacyHistory: options.importedLegacyHistory ?? false
    };
    store.createSession(header);
    return new _AgentSession(store, header, []);
  }
  static load(store, sessionId) {
    const stored = store.loadSession(sessionId);
    return stored === void 0 ? void 0 : new _AgentSession(store, stored.header, stored.events);
  }
  get id() {
    return this.header.id;
  }
  get events() {
    return this.mutableEvents;
  }
  append(type, data, position = {}) {
    const event = Object.freeze({
      sessionId: this.id,
      seq: this.mutableEvents.length,
      type,
      ...position.turn === void 0 ? {} : { turn: position.turn },
      ...position.step === void 0 ? {} : { step: position.step },
      timestamp: (/* @__PURE__ */ new Date()).toISOString(),
      data: clone(data)
    });
    this.store.appendEvent(event);
    this.mutableEvents.push(event);
    return event;
  }
  importHistory(messages) {
    if (this.mutableEvents.length > 0) throw new Error("legacy history can only be imported into an empty session");
    this.append("legacy/history-imported", { count: messages.length });
    for (const message2 of messages) this.append("conversation/message", message2);
  }
  surfaceEvents() {
    const replacement = /* @__PURE__ */ new Map();
    const shadowed = /* @__PURE__ */ new Set();
    for (const event of this.mutableEvents) {
      const data = event.data;
      const sourceSeqs = Array.isArray(data.sourceSeqs) ? data.sourceSeqs : [];
      for (const value of sourceSeqs) if (Number.isSafeInteger(value)) shadowed.add(Number(value));
      const replacementSeq = data.replacesSeq;
      if (Number.isSafeInteger(replacementSeq)) replacement.set(Number(replacementSeq), event);
    }
    return this.mutableEvents.filter((event) => isModelVisible(event.type) && !shadowed.has(event.seq)).map((event) => replacement.get(event.seq) ?? event);
  }
  deriveMessages() {
    const messages = [];
    for (const event of this.surfaceEvents()) {
      if (event.type === "conversation/message" || event.type === "assistant/message" || event.type === "tool/result") {
        const candidate = event.data;
        if (typeof candidate.id === "string" && typeof candidate.role === "string" && Array.isArray(candidate.content)) {
          messages.push(clone(candidate));
        }
      } else if (event.type === "compaction/summary") {
        const data = event.data;
        messages.push({
          id: `summary-${event.seq}`,
          role: "system",
          content: [{ type: "text", text: String(data.summary ?? "") }]
        });
      }
    }
    return messages;
  }
};
function isModelVisible(type) {
  return type === "conversation/message" || type === "assistant/message" || type === "tool/result" || type === "compaction/summary";
}

// src/core/tool-scheduler.ts
async function executeToolCalls(calls, createRequest, execute, signal, parallel) {
  const run = async (call) => {
    let parsed;
    try {
      parsed = call.arguments.trim() ? JSON.parse(call.arguments) : {};
    } catch {
      return { call, result: { status: "failed", error: "\u5DE5\u5177\u53C2\u6570\u4E0D\u662F\u6709\u6548JSON" } };
    }
    const request = createRequest({ ...call, arguments: JSON.stringify(parsed) });
    return { call, result: await execute(request, signal) };
  };
  if (parallel) return Promise.all(calls.map(run));
  const results = [];
  for (const call of calls) results.push(await run(call));
  return results;
}

// src/core/inbox.ts
var Inbox = class {
  constructor(session) {
    this.session = session;
    for (const event of session.events) {
      if (event.type === "agent/inbox-spliced") this.apply(event.data);
    }
  }
  state = { "next-turn": [], "next-step": [] };
  get nextTurn() {
    return this.state["next-turn"];
  }
  get nextStep() {
    return this.state["next-step"];
  }
  get hasPending() {
    return this.nextTurn.length > 0 || this.nextStep.length > 0;
  }
  append(target, message2) {
    this.splice(target, this.state[target].length, 0, [message2]);
  }
  prepend(target, message2) {
    this.splice(target, 0, 0, [message2]);
  }
  clear() {
    this.splice("next-step", 0, this.nextStep.length, []);
    this.splice("next-turn", 0, this.nextTurn.length, []);
  }
  claim(target, turn) {
    const claimed = this.mutate("next-step", 0, this.nextStep.length, [], false);
    if (target === "next-turn") claimed.push(...this.mutate("next-turn", 0, 1, [], false));
    for (const message2 of claimed) this.session.append("agent/inbox-claimed", { messageId: message2.id }, { turn });
    return claimed;
  }
  splice(target, start, deleteCount, inserted) {
    return this.mutate(target, start, deleteCount, inserted, true);
  }
  mutate(target, start, deleteCount, inserted, cancelled) {
    const inbox = this.state[target];
    const offset = Number.isNaN(Math.trunc(start)) ? 0 : Math.trunc(start);
    const actualStart = offset < 0 ? Math.max(inbox.length + offset, 0) : Math.min(offset, inbox.length);
    const count = Number.isNaN(Math.trunc(deleteCount)) ? 0 : Math.trunc(deleteCount);
    const actualDeleteCount = Math.min(Math.max(count, 0), inbox.length - actualStart);
    if (actualDeleteCount === 0 && inserted.length === 0) return [];
    const splice = {
      target,
      start: actualStart,
      removedCount: actualDeleteCount,
      inserted: structuredClone(inserted),
      cancelled
    };
    this.validate(splice);
    const event = this.session.append("agent/inbox-spliced", splice);
    const durable = event.data;
    return inbox.splice(durable.start, durable.removedCount, ...durable.inserted);
  }
  apply(splice) {
    this.validate(splice);
    this.state[splice.target].splice(splice.start, splice.removedCount, ...structuredClone(splice.inserted));
  }
  validate(splice) {
    const inbox = this.state[splice.target];
    if (!Number.isSafeInteger(splice.start) || splice.start < 0 || splice.start > inbox.length || !Number.isSafeInteger(splice.removedCount) || splice.removedCount < 0 || splice.start + splice.removedCount > inbox.length) {
      throw new Error("invalid inbox splice");
    }
    const candidate = inbox.toSpliced(splice.start, splice.removedCount, ...splice.inserted);
    const ids = /* @__PURE__ */ new Set();
    for (const message2 of splice.target === "next-turn" ? [...candidate, ...this.nextStep] : [...this.nextTurn, ...candidate]) {
      if (ids.has(message2.id)) throw new Error(`message ${message2.id} is already pending`);
      ids.add(message2.id);
    }
  }
};

// src/core/agent.ts
function textOf(blocks) {
  return blocks.filter((block) => block.type === "text").map((block) => block.text).join("").trim();
}
function message(role, content, id = (0, import_node_crypto.randomUUID)()) {
  return { id, role, content };
}
function normalizeAllowedTools(value) {
  return value.map((name) => ({
    name,
    description: `OptionHelper\u53D7\u63A7\u4E1A\u52A1\u5DE5\u5177\uFF1A${name}`,
    parameters: { type: "object", additionalProperties: true },
    executionMode: "exclusive"
  }));
}
function abortError(reason) {
  const error = new Error(String(reason ?? "cancelled"));
  error.name = "AbortError";
  return error;
}
var AgentRun = class {
  constructor(activation, store, ports, publisher, session) {
    this.activation = activation;
    this.store = store;
    this.ports = ports;
    this.publisher = publisher;
    this.routeIdValue = activation.routeId;
    this.session = session ?? AgentSession.create(store, {
      id: activation.sessionId,
      kind: activation.parentSessionId === void 0 ? "root" : "child",
      ...activation.parentSessionId === void 0 ? {} : { parentId: activation.parentSessionId },
      label: activation.label
    });
    this.inbox = new Inbox(this.session);
    const stored = store.loadRun(activation.runId);
    if (stored !== void 0) {
      this.statusValue = stored.status === "running" || stored.status === "waiting_tool" ? "idle" : String(stored.status);
      this.turnValue = Number(stored.turn ?? 0);
      this.stepValue = Number(stored.step ?? 0);
      const storedResult = stored.result !== null && typeof stored.result === "object" && !Array.isArray(stored.result) ? stored.result : void 0;
      this.resultValue = String(storedResult?.text ?? stored.result ?? "");
      const storedBlocks = storedResult?.blocks;
      this.resultBlocks = Array.isArray(storedBlocks) ? storedBlocks : [];
      this.errorValue = stored.error === void 0 ? void 0 : String(stored.error);
      this.routeIdValue = typeof stored.routeId === "string" ? stored.routeId : activation.routeId;
      if (stored.status === "running" || stored.status === "waiting_tool") {
        this.session.append("session/interrupted", { reason: "runtime_restarted", turn: this.turnValue, step: this.stepValue });
      }
    }
  }
  session;
  inbox;
  statusValue = "idle";
  turnValue = 0;
  stepValue = 0;
  resultValue = "";
  resultBlocks = [];
  errorValue;
  usageValue = {};
  active;
  abortController;
  toolCount = 0;
  routeIdValue;
  activate(history2 = []) {
    if (history2.length > 0 && this.session.events.length === 0) this.session.importHistory(history2);
    this.publisher.publish(this.session, "agent/activated", {
      runId: this.activation.runId,
      sessionId: this.activation.sessionId,
      parentSessionId: this.activation.parentSessionId ?? null,
      parentRunId: this.activation.parentRunId ?? null,
      roleId: this.activation.roleId,
      label: this.activation.label,
      mode: this.activation.mode
    });
    this.persist();
    return this.snapshot();
  }
  turn(prompt, routeId2) {
    if (routeId2 !== void 0 && routeId2 !== this.routeIdValue) {
      if (this.active !== void 0) throw new Error("Agent\u6B63\u5728\u8FD0\u884C\uFF0C\u4E0D\u80FD\u5207\u6362\u5F53\u524DTurn\u6A21\u578B\u8DEF\u7531");
      this.routeIdValue = routeId2;
      this.publisher.publish(this.session, "model/route-bound", {
        runId: this.activation.runId,
        routeId: routeId2,
        nextTurn: this.turnValue + 1
      });
      this.persist();
    }
    if (this.active !== void 0) {
      if (this.activation.mode !== "continuable") throw new Error("one-shot Agent\u6B63\u5728\u8FD0\u884C");
      this.inbox.append("next-turn", message("user", typeof prompt === "string" ? [{ type: "text", text: prompt }] : prompt));
      return this.active;
    }
    const queued = message("user", typeof prompt === "string" ? [{ type: "text", text: prompt }] : prompt);
    this.inbox.append("next-turn", queued);
    this.active = this.drainTurns().finally(() => {
      this.active = void 0;
    });
    return this.active;
  }
  followup(prompt, routeId2) {
    if (this.activation.mode !== "continuable") throw new Error("one-shot Child Agent\u4E0D\u63A5\u53D7\u540E\u7EED\u6D88\u606F");
    return this.turn(prompt, routeId2);
  }
  cancel(reason = "cancelled") {
    this.abortController?.abort(reason);
    this.statusValue = "cancelled";
    this.errorValue = reason;
    this.publisher.publish(this.session, "agent/status", { runId: this.activation.runId, roleId: this.activation.roleId, status: "cancelled", reason });
    this.persist();
    return this.snapshot();
  }
  snapshot() {
    return {
      runId: this.activation.runId,
      workflowId: this.activation.workflowId,
      sessionId: this.activation.sessionId,
      ...this.activation.parentSessionId === void 0 ? {} : { parentSessionId: this.activation.parentSessionId },
      ...this.activation.parentRunId === void 0 ? {} : { parentRunId: this.activation.parentRunId },
      roleId: this.activation.roleId,
      label: this.activation.label,
      mode: this.activation.mode,
      status: this.statusValue,
      turn: this.turnValue,
      step: this.stepValue,
      result: { text: this.resultValue, blocks: this.resultBlocks, usage: this.usageValue, turn: this.turnValue },
      ...this.errorValue === void 0 ? {} : { error: this.errorValue },
      usage: this.usageValue,
      routeId: this.routeIdValue,
      activation: { ...this.activation, routeId: this.routeIdValue }
    };
  }
  async drainTurns() {
    while (this.inbox.nextTurn.length > 0 && this.statusValue !== "cancelled") {
      const claimed = this.inbox.claim("next-turn", this.turnValue + 1);
      if (claimed.length === 0) break;
      for (const item of claimed) this.session.append("conversation/message", item);
      await this.executeTurn();
      if (this.activation.mode === "one-shot") break;
    }
    return this.snapshot();
  }
  async executeTurn() {
    this.turnValue += 1;
    this.stepValue = 0;
    this.toolCount = 0;
    this.resultValue = "";
    this.resultBlocks = [];
    this.errorValue = void 0;
    this.abortController = new AbortController();
    const signal = this.abortController.signal;
    this.setStatus("running");
    this.publisher.publish(this.session, "turn/start", { runId: this.activation.runId, turn: this.turnValue }, { turn: this.turnValue });
    try {
      for (let step = 1; this.activation.budget.maxSteps === 0 || step <= this.activation.budget.maxSteps; step += 1) {
        if (signal.aborted) throw abortError(signal.reason);
        this.stepValue = step;
        this.publisher.publish(this.session, "step/start", { runId: this.activation.runId, turn: this.turnValue, step }, { turn: this.turnValue, step });
        const operationId = `model:${this.activation.runId}:${this.turnValue}:${step}`;
        const checkpointPayload = { turn: this.turnValue, step, routeId: this.routeIdValue };
        this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "model", operationId, "not_started", checkpointPayload);
        let assembler;
        try {
          const maintained = await maintainContext(
            this.session,
            this.activation.contextPolicy,
            this.ports.summarize,
            signal
          );
          const requestId = (0, import_node_crypto.randomUUID)();
          this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "model", operationId, "started", {
            ...checkpointPayload,
            requestId
          });
          assembler = await this.streamWithRetry({
            requestId,
            routeId: this.routeIdValue,
            messages: maintained.messages,
            tools: normalizeAllowedTools(this.activation.allowedTools),
            sessionId: this.session.id,
            agentRunId: this.activation.runId,
            roleId: this.activation.roleId,
            turn: this.turnValue,
            step
          }, signal);
          this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "model", operationId, "completed", {
            ...checkpointPayload,
            finishReason: assembler.finish.kind,
            usage: assembler.usage
          });
        } catch (error) {
          this.store.checkpoint(
            (0, import_node_crypto.randomUUID)(),
            this.session.id,
            "model",
            operationId,
            signal.aborted || error instanceof Error && error.name === "AbortError" ? "cancelled" : "failed",
            { ...checkpointPayload, error: String(error) }
          );
          throw error;
        }
        this.usageValue = new TokenMeter().merge(this.usageValue, assembler.usage);
        const blocks = assembler.blocks();
        const assistant = message("assistant", blocks);
        this.session.append("assistant/message", assistant, { turn: this.turnValue, step });
        this.publisher.publish(this.session, "assistant/message", {
          runId: this.activation.runId,
          text: textOf(blocks),
          blocks,
          finishReason: assembler.finish.kind
        }, { turn: this.turnValue, step });
        const calls = blocks.filter((block) => block.type === "tool-call");
        if (calls.length === 0) {
          if (assembler.finish.kind === "error") throw new Error(assembler.finish.message ?? "model request failed");
          this.resultValue = textOf(blocks);
          this.resultBlocks = blocks;
          this.publisher.publish(this.session, "step/end", { runId: this.activation.runId, reason: "completed" }, { turn: this.turnValue, step });
          this.publisher.publish(this.session, "turn/end", { runId: this.activation.runId, reason: "completed" }, { turn: this.turnValue, step });
          this.setStatus("completed");
          return;
        }
        if (this.activation.budget.maxTools > 0 && this.toolCount + calls.length > this.activation.budget.maxTools) throw new Error("Agent\u5DE5\u5177\u8C03\u7528\u6B21\u6570\u8D85\u8FC7\u9884\u7B97");
        this.toolCount += calls.length;
        this.setStatus("waiting_tool");
        const results = await executeToolCalls(
          calls,
          (call) => ({
            callId: call.id,
            name: call.name,
            arguments: JSON.parse(call.arguments || "{}"),
            sessionId: this.session.id,
            agentRunId: this.activation.runId,
            turn: this.turnValue,
            step
          }),
          (request, currentSignal) => this.executeTool(request, currentSignal),
          signal,
          this.activation.parallelTools
        );
        for (const item of results) {
          const toolMessage = message("tool", [{ type: "text", text: JSON.stringify(item.result) }], `tool-result-${item.call.id}`);
          const withCall = { ...toolMessage, toolCallId: item.call.id };
          this.session.append("tool/result", withCall, { turn: this.turnValue, step });
        }
        this.setStatus("running");
        this.publisher.publish(this.session, "step/end", { runId: this.activation.runId, reason: "tool-results-available" }, { turn: this.turnValue, step });
      }
      throw new Error("Agent\u8FBE\u5230\u6700\u5927Step\u4ECD\u672A\u5F62\u6210\u6700\u7EC8\u7B54\u590D");
    } catch (error) {
      const cancelled = signal.aborted || error instanceof Error && error.name === "AbortError";
      const partialBlocks = error instanceof Error ? error.partialBlocks ?? [] : [];
      if (partialBlocks.length > 0) {
        const interrupted = message("assistant", partialBlocks);
        this.session.append("assistant/message", interrupted, {
          turn: this.turnValue,
          step: this.stepValue
        });
        this.resultBlocks = partialBlocks;
        this.resultValue = textOf(partialBlocks);
        this.publisher.publish(this.session, "assistant/message", {
          runId: this.activation.runId,
          text: this.resultValue,
          blocks: partialBlocks,
          finishReason: cancelled ? "cancelled" : "error",
          interrupted: true
        }, { turn: this.turnValue, step: this.stepValue });
      }
      this.errorValue = String(error);
      this.publisher.publish(this.session, "turn/end", {
        runId: this.activation.runId,
        reason: cancelled ? "cancelled" : "failed",
        error: this.errorValue
      }, { turn: this.turnValue, step: this.stepValue });
      if (cancelled) this.session.append("session/interrupted", { reason: String(signal.reason ?? "cancelled") });
      this.setStatus(cancelled ? "cancelled" : "failed");
      if (!cancelled) throw error;
    } finally {
      this.abortController = void 0;
    }
  }
  async streamWithRetry(request, signal) {
    let attempt = 0;
    while (true) {
      const assembler = new BlockAssembler();
      try {
        for await (const chunk of this.ports.streamModel(request, signal)) {
          assembler.push(chunk);
          this.publishChunk(chunk);
        }
        return assembler;
      } catch (error) {
        const code = error instanceof Error && error.name === "AbortError" ? "ABORT" : "TRANSPORT";
        if (!shouldRetry(DEFAULT_RETRY_POLICY, code, attempt + 1, assembler.blocks().length > 0) || signal.aborted) {
          if (error instanceof Error) error.partialBlocks = assembler.interruptedBlocks();
          throw error;
        }
        await waitForRetry(retryDelay(DEFAULT_RETRY_POLICY, attempt + 1), signal);
        attempt += 1;
      }
    }
  }
  publishChunk(chunk) {
    const position = { turn: this.turnValue, step: this.stepValue };
    if (chunk.type === "block-start") this.publisher.publish(this.session, "assistant/block-start", {
      index: chunk.index,
      blockType: chunk.blockType
    }, position);
    else if (chunk.type === "block-end") this.publisher.publish(this.session, "assistant/block-end", {
      index: chunk.index,
      block: chunk.block
    }, position);
    else if (chunk.type === "text-delta") this.publisher.publish(this.session, "assistant.text_delta", { index: chunk.index, text: chunk.text }, position);
    else if (chunk.type === "reasoning-delta") this.publisher.publish(this.session, "assistant.reasoning_delta", {
      index: chunk.index,
      text: chunk.text,
      available: chunk.text.length > 0
    }, position);
    else if (chunk.type === "usage") this.publisher.publish(this.session, "usage/updated", { usage: { ...chunk.usage } }, position);
  }
  async executeTool(request, signal) {
    this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "tool", request.callId, "not_started", { toolName: request.name });
    this.publisher.publish(this.session, "tool/requested", { toolCallId: request.callId, toolName: request.name, arguments: request.arguments }, { turn: request.turn, step: request.step });
    this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "tool", request.callId, "started", { toolName: request.name });
    this.publisher.publish(this.session, "tool/started", { toolCallId: request.callId, toolName: request.name }, { turn: request.turn, step: request.step });
    try {
      const result = await this.ports.executeTool(request, signal);
      const status = result.status === "unknown" ? "outcome_unknown" : result.status;
      this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "tool", request.callId, status, { toolName: request.name, factRefs: result.factRefs ?? [] });
      this.publisher.publish(this.session, result.status === "completed" ? "tool/completed" : `tool/${result.status}`, {
        toolCallId: request.callId,
        toolName: request.name,
        status: result.status,
        factRefs: [...result.factRefs ?? []],
        error: result.error ?? null
      }, { turn: request.turn, step: request.step });
      return result;
    } catch (error) {
      const status = signal.aborted ? "cancelled" : "outcome_unknown";
      this.store.checkpoint((0, import_node_crypto.randomUUID)(), this.session.id, "tool", request.callId, status, { toolName: request.name, error: String(error) });
      this.publisher.publish(this.session, signal.aborted ? "tool/cancelled" : "tool/failed", {
        toolCallId: request.callId,
        toolName: request.name,
        status,
        error: String(error)
      }, { turn: request.turn, step: request.step });
      if (!signal.aborted) return { status: "unknown", error: String(error) };
      throw error;
    }
  }
  setStatus(status) {
    this.statusValue = status;
    this.publisher.publish(this.session, "agent/status", { runId: this.activation.runId, roleId: this.activation.roleId, status });
    this.persist();
  }
  persist() {
    this.store.saveRun(this.activation.runId, this.session.id, this.snapshot());
  }
};

// src/persistence/sqlite-store.ts
var import_node_fs = require("node:fs");
var import_node_path = require("node:path");
var import_node_sqlite = require("node:sqlite");
var SCHEMA_VERSION = 2;
function parseObject(value, label) {
  if (typeof value !== "string") throw new Error(`${label} is not stored as JSON text`);
  const parsed = JSON.parse(value);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must decode to an object`);
  }
  return parsed;
}
var SqliteSessionStore = class {
  constructor(path) {
    this.path = path;
    (0, import_node_fs.mkdirSync)((0, import_node_path.dirname)(path), { recursive: true, mode: 448 });
    this.database = new import_node_sqlite.DatabaseSync(path);
    this.database.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;");
    this.migrate();
  }
  database;
  migrate() {
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS runtime_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        header_json TEXT NOT NULL,
        next_seq INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS session_events (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        PRIMARY KEY (session_id, seq),
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL DEFAULT '',
        role_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (session_id, operation_id),
        FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS workflows (
        workflow_id TEXT PRIMARY KEY,
        root_session_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (root_session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
      );
    `);
    this.ensureColumn("agent_runs", "workflow_id", "TEXT NOT NULL DEFAULT ''");
    this.ensureColumn("agent_runs", "role_id", "TEXT NOT NULL DEFAULT ''");
    const row = this.database.prepare("SELECT value FROM runtime_meta WHERE key='schema_version'").get();
    if (row === void 0) {
      this.database.prepare("INSERT INTO runtime_meta(key,value) VALUES('schema_version',?)").run(String(SCHEMA_VERSION));
    } else if (Number(row.value) === 1) {
      this.database.prepare("UPDATE runtime_meta SET value=? WHERE key='schema_version'").run(String(SCHEMA_VERSION));
    } else if (Number(row.value) !== SCHEMA_VERSION) {
      throw new Error(`unsupported Runtime schema version ${String(row.value)}`);
    }
  }
  ensureColumn(table, column, declaration) {
    const columns = this.database.prepare(`PRAGMA table_info(${table})`).all();
    if (!columns.some((item) => item.name === column)) this.database.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${declaration}`);
  }
  createSession(header) {
    const now = (/* @__PURE__ */ new Date()).toISOString();
    this.database.prepare(
      "INSERT INTO sessions(session_id,header_json,next_seq,updated_at) VALUES(?,?,0,?)"
    ).run(header.id, JSON.stringify(header), now);
  }
  hasSession(sessionId) {
    return this.database.prepare("SELECT 1 AS present FROM sessions WHERE session_id=?").get(sessionId) !== void 0;
  }
  loadSession(sessionId) {
    const row = this.database.prepare("SELECT header_json,next_seq FROM sessions WHERE session_id=?").get(sessionId);
    if (row === void 0) return void 0;
    const events = this.database.prepare(
      "SELECT event_json FROM session_events WHERE session_id=? ORDER BY seq"
    ).all(sessionId);
    const parsed = events.map((item, index) => {
      const event = parseObject(item.event_json, `session event ${index}`);
      if (event.sessionId !== sessionId || event.seq !== index) throw new Error(`non-contiguous Runtime session ${sessionId}`);
      return event;
    });
    if (Number(row.next_seq) !== parsed.length) throw new Error(`Runtime session ${sessionId} next_seq mismatch`);
    return { header: parseObject(row.header_json, "session header"), events: parsed };
  }
  appendEvent(event) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const row = this.database.prepare("SELECT next_seq FROM sessions WHERE session_id=?").get(event.sessionId);
      if (row === void 0) throw new Error(`Runtime session ${event.sessionId} does not exist`);
      if (Number(row.next_seq) !== event.seq) throw new Error(`Runtime session ${event.sessionId} expected seq ${String(row.next_seq)}`);
      this.database.prepare(
        "INSERT INTO session_events(session_id,seq,event_json) VALUES(?,?,?)"
      ).run(event.sessionId, event.seq, JSON.stringify(event));
      this.database.prepare(
        "UPDATE sessions SET next_seq=next_seq+1,updated_at=? WHERE session_id=?"
      ).run(event.timestamp, event.sessionId);
      this.database.exec("COMMIT");
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }
  saveRun(runId, sessionId, snapshot) {
    const now = (/* @__PURE__ */ new Date()).toISOString();
    const value = snapshot;
    const workflowId = String(value.workflowId ?? "");
    const roleId = String(value.roleId ?? "");
    this.database.prepare(`
      INSERT INTO agent_runs(run_id,workflow_id,role_id,session_id,snapshot_json,updated_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(run_id) DO UPDATE SET
        workflow_id=excluded.workflow_id,role_id=excluded.role_id,
        session_id=excluded.session_id,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
    `).run(runId, workflowId, roleId, sessionId, JSON.stringify(snapshot), now);
  }
  loadRun(runId) {
    const row = this.database.prepare("SELECT snapshot_json FROM agent_runs WHERE run_id=?").get(runId);
    return row === void 0 ? void 0 : parseObject(row.snapshot_json, "agent run");
  }
  listRuns() {
    const rows = this.database.prepare("SELECT snapshot_json FROM agent_runs ORDER BY updated_at").all();
    return rows.map((row) => parseObject(row.snapshot_json, "agent run"));
  }
  saveWorkflow(workflowId, rootSessionId, snapshot) {
    this.database.prepare(`
      INSERT INTO workflows(workflow_id,root_session_id,snapshot_json,updated_at) VALUES(?,?,?,?)
      ON CONFLICT(workflow_id) DO UPDATE SET
        root_session_id=excluded.root_session_id,snapshot_json=excluded.snapshot_json,updated_at=excluded.updated_at
    `).run(workflowId, rootSessionId, JSON.stringify(snapshot), (/* @__PURE__ */ new Date()).toISOString());
  }
  loadWorkflow(workflowId) {
    const row = this.database.prepare("SELECT snapshot_json FROM workflows WHERE workflow_id=?").get(workflowId);
    return row === void 0 ? void 0 : parseObject(row.snapshot_json, "workflow");
  }
  listWorkflows(rootSessionId) {
    const rows = rootSessionId === void 0 ? this.database.prepare("SELECT snapshot_json FROM workflows ORDER BY updated_at").all() : this.database.prepare("SELECT snapshot_json FROM workflows WHERE root_session_id=? ORDER BY updated_at").all(rootSessionId);
    return rows.map(
      (row) => parseObject(row.snapshot_json, "workflow")
    );
  }
  checkpoint(checkpointId, sessionId, operationKind, operationId, state, payload) {
    this.database.prepare(`
      INSERT INTO checkpoints(checkpoint_id,session_id,operation_kind,operation_id,state,payload_json,updated_at)
      VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(session_id,operation_id) DO UPDATE SET
        state=excluded.state,payload_json=excluded.payload_json,updated_at=excluded.updated_at
    `).run(checkpointId, sessionId, operationKind, operationId, state, JSON.stringify(payload), (/* @__PURE__ */ new Date()).toISOString());
  }
  unresolvedCheckpoints(sessionId) {
    const rows = this.database.prepare(`
      SELECT checkpoint_id,operation_kind,operation_id,state,payload_json,updated_at
      FROM checkpoints WHERE session_id=? AND state NOT IN ('completed','failed','cancelled') ORDER BY updated_at
    `).all(sessionId);
    return rows.map((row) => ({
      checkpointId: row.checkpoint_id,
      operationKind: row.operation_kind,
      operationId: row.operation_id,
      state: row.state,
      payload: parseObject(row.payload_json, "checkpoint payload"),
      updatedAt: row.updated_at
    }));
  }
  listSessions() {
    const rows = this.database.prepare("SELECT header_json FROM sessions ORDER BY updated_at DESC").all();
    return rows.map((row) => parseObject(row.header_json, "session header"));
  }
  close() {
    this.database.close();
  }
};

// src/core/runtime.ts
var DEFAULT_CONTEXT = {
  maxContextTokens: 12e4,
  pruneAtTokens: 72e3,
  compactAtTokens: 9e4,
  preservedRecentMessages: 12,
  prunedToolResultChars: 6e3
};
function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}
function pick(value, ...names) {
  for (const name of names) if (value[name] !== void 0) return value[name];
  return void 0;
}
function userContent(params2) {
  const raw = params2.content;
  if (!Array.isArray(raw)) {
    const text = String(params2.prompt ?? "");
    if (text.trim() === "") throw new Error("Agent Turn\u7F3A\u5C11\u7528\u6237\u5185\u5BB9");
    return [{ type: "text", text }];
  }
  const blocks = [];
  for (const item of raw) {
    const value = record(item);
    const type = String(value.type ?? "").replaceAll("_", "-");
    if (type === "text") {
      const text = String(value.text ?? "");
      if (text !== "") blocks.push({ type: "text", text });
      continue;
    }
    if (type === "image") {
      blocks.push({
        type: "image",
        attachmentId: identifier(pick(value, "attachmentId", "attachment_id"), "attachmentId"),
        mediaType: String(pick(value, "mediaType", "media_type") ?? ""),
        ...String(value.name ?? "").trim() === "" ? {} : { name: String(value.name).slice(0, 160) }
      });
      continue;
    }
    if (type === "document-ref") {
      blocks.push({
        type: "document-ref",
        attachmentId: identifier(pick(value, "attachmentId", "attachment_id"), "attachmentId"),
        mediaType: String(pick(value, "mediaType", "media_type") ?? ""),
        name: String(value.name ?? "document").slice(0, 160),
        ...String(pick(value, "extractionStatus", "extraction_status") ?? "").trim() === "" ? {} : { extractionStatus: String(pick(value, "extractionStatus", "extraction_status")) }
      });
      continue;
    }
    throw new Error(`\u4E0D\u652F\u6301\u7684\u7528\u6237\u5185\u5BB9\u5757\uFF1A${type}`);
  }
  if (blocks.length === 0) throw new Error("Agent Turn\u7F3A\u5C11\u7528\u6237\u5185\u5BB9");
  return blocks;
}
function identifier(value, name, fallback) {
  const result = String(value ?? fallback ?? "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(result)) throw new Error(`${name}\u683C\u5F0F\u65E0\u6548`);
  return result;
}
function presetRevision(value) {
  const revision = String(value ?? "").trim();
  if (!/^[0-9a-f]{64}$/.test(revision)) throw new Error("presetRevision\u5FC5\u987B\u662F\u9884\u8BBE\u5185\u5BB9\u7684SHA-256\u6807\u8BC6");
  return revision;
}
function workflowRecord(value, fallbackRootSessionId) {
  const spec = record(value);
  return {
    ...spec,
    workflowId: identifier(pick(spec, "workflowId", "workflow_id"), "workflowId"),
    presetId: identifier(pick(spec, "presetId", "preset_id"), "presetId"),
    presetRevision: presetRevision(pick(spec, "presetRevision", "preset_revision")),
    rootSessionId: identifier(pick(spec, "rootSessionId", "root_session_id"), "rootSessionId", fallbackRootSessionId)
  };
}
function executionLimit(value) {
  const number = Number(value ?? 0);
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}
function positiveInteger(value, fallback, maximum) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? Math.min(number, maximum) : fallback;
}
function routeId(value) {
  const route = record(pick(value, "modelRouteRef", "model_route_ref"));
  return identifier(pick(route, "routeId", "route_id"), "routeId", "route-default");
}
function optionalRouteId(value) {
  const route = record(pick(value, "modelRouteRef", "model_route_ref"));
  const raw = pick(route, "routeId", "route_id");
  return raw === void 0 || String(raw).trim() === "" ? void 0 : identifier(raw, "routeId");
}
function history(value) {
  if (!Array.isArray(value)) return [];
  return value.map((raw, index) => {
    const item = record(raw);
    const role = ["system", "user", "assistant", "tool"].includes(String(item.role)) ? String(item.role) : "user";
    const rawContent = item.content;
    const content = Array.isArray(rawContent) ? rawContent.map((block) => record(block)).flatMap((block) => {
      const type = String(block.type ?? "").replaceAll("_", "-");
      if (type === "text" || type === "reasoning") return [{ type, text: String(block.text ?? "") }];
      if (type === "image") return [{
        type: "image",
        attachmentId: String(pick(block, "attachmentId", "attachment_id") ?? ""),
        mediaType: String(pick(block, "mediaType", "media_type") ?? ""),
        name: String(block.name ?? "")
      }];
      if (type === "document-ref") return [{
        type: "document-ref",
        attachmentId: String(pick(block, "attachmentId", "attachment_id") ?? ""),
        mediaType: String(pick(block, "mediaType", "media_type") ?? ""),
        name: String(block.name ?? "document")
      }];
      if (type === "tool-call") return [{
        type: "tool-call",
        id: String(block.id ?? ""),
        name: String(block.name ?? ""),
        arguments: String(block.arguments ?? "{}")
      }];
      return [];
    }) : [{ type: "text", text: String(rawContent ?? "") }];
    return { id: String(item.id ?? `legacy-${index}`), role, content };
  });
}
function contextPolicy(value) {
  const raw = record(pick(value, "contextPolicy", "context_policy"));
  return {
    maxContextTokens: positiveInteger(pick(raw, "maxContextTokens", "max_context_tokens", "maxTokens", "max_tokens"), DEFAULT_CONTEXT.maxContextTokens, 1e6),
    pruneAtTokens: positiveInteger(pick(raw, "pruneAtTokens", "prune_at_tokens"), DEFAULT_CONTEXT.pruneAtTokens, 1e6),
    compactAtTokens: positiveInteger(pick(raw, "compactAtTokens", "compact_at_tokens"), DEFAULT_CONTEXT.compactAtTokens, 1e6),
    preservedRecentMessages: positiveInteger(pick(raw, "preservedRecentMessages", "preserved_recent_messages"), DEFAULT_CONTEXT.preservedRecentMessages, 100),
    prunedToolResultChars: positiveInteger(pick(raw, "prunedToolResultChars", "pruned_tool_result_chars", "toolResultMaxChars", "tool_result_max_chars"), DEFAULT_CONTEXT.prunedToolResultChars, 1e5)
  };
}
var OptionHelperAgentRuntime = class {
  constructor(ports, emit) {
    this.ports = ports;
    this.emit = emit;
  }
  store;
  rootSessionId = "";
  runs = /* @__PURE__ */ new Map();
  workflows = /* @__PURE__ */ new Map();
  initialized = false;
  initialize(params2) {
    const sessionRoot = String(pick(params2, "sessionRoot", "session_root") ?? process.env.OPTIONHELPER_AGENT_SESSION_ROOT ?? ".").trim();
    this.rootSessionId = identifier(pick(params2, "rootSessionId", "root_session_id"), "rootSessionId", "root-session");
    if (!this.initialized) {
      this.store = new SqliteSessionStore((0, import_node_path2.join)(sessionRoot, "agent-sessions.sqlite3"));
      if (!this.store.hasSession(this.rootSessionId)) AgentSession.create(this.store, { id: this.rootSessionId, kind: "root", label: "OptionHelper" });
      for (const workflow of this.store.listWorkflows(this.rootSessionId)) {
        const normalized = workflowRecord(workflow, this.rootSessionId);
        this.workflows.set(String(normalized.workflowId), normalized);
      }
      this.initialized = true;
      this.emitRuntime("runtime/ready", this.rootSessionId, { runtimeId: "optionhelper-agent-runtime", protocolVersion: "2.0" });
    }
    return { runtimeId: "optionhelper-agent-runtime", version: "0.2.0", protocolVersion: "2.0", rootSessionId: this.rootSessionId, database: "agent-sessions.sqlite3" };
  }
  capabilities() {
    return {
      state: { detected: true, initialized: this.initialized, verified: this.initialized && this.store !== void 0 },
      mainAgent: true,
      persistentSessions: true,
      sqlitePersistence: true,
      nativeToolLoop: true,
      streaming: true,
      reasoning: true,
      usage: true,
      contextCompaction: true,
      oneShotSubagent: true,
      continuableSubagent: true,
      coldResume: true,
      workflowPersistence: true,
      workflowScopedCancel: true,
      turnModelRoute: true,
      hostOrchestration: true,
      continuableRolesByPreset: {
        "sequential-deliberation": [],
        "product-trader-loop": ["Structurer", "Trader"],
        "independent-council": ["Matcher", "Hedger"],
        "constraint-ranking": []
      }
    };
  }
  activate(params2, child = false) {
    const store = this.requireStore();
    const sessionId = identifier(pick(params2, "sessionId", "session_id"), "sessionId", child ? `child-${(0, import_node_crypto2.randomUUID)()}` : this.rootSessionId);
    const runId = identifier(pick(params2, "runId", "run_id"), "runId", `run-${(0, import_node_crypto2.randomUUID)()}`);
    const parentSessionId = child ? identifier(pick(params2, "parentSessionId", "parent_session_id"), "parentSessionId", this.rootSessionId) : void 0;
    const rawParentRunId = pick(params2, "parentRunId", "parent_run_id");
    if (this.runs.has(runId)) return this.runs.get(runId);
    if (child && !store.hasSession(parentSessionId)) throw new Error("Parent Session\u4E0D\u5B58\u5728");
    const toolPolicy = record(pick(params2, "toolPolicy", "tool_policy"));
    const budget = record(params2.budget);
    const activation = {
      runId,
      workflowId: identifier(pick(params2, "workflowId", "workflow_id"), "workflowId", `workflow-${this.rootSessionId}`),
      sessionId,
      ...parentSessionId === void 0 ? {} : { parentSessionId },
      ...rawParentRunId === void 0 || rawParentRunId === null || String(rawParentRunId).trim() === "" ? {} : { parentRunId: identifier(rawParentRunId, "parentRunId") },
      roleId: identifier(pick(params2, "roleId", "role_id"), "roleId", child ? "Agent" : "MainAgent"),
      label: String(params2.label ?? (child ? "Agent" : "OptionHelper")).slice(0, 160),
      mode: String(params2.mode ?? (child ? "one-shot" : "continuable")) === "one-shot" ? "one-shot" : "continuable",
      routeId: routeId(params2),
      allowedTools: Array.isArray(pick(toolPolicy, "allowedTools", "allowed_tools")) ? pick(toolPolicy, "allowedTools", "allowed_tools").map(String) : [],
      parallelTools: pick(toolPolicy, "parallelTools", "parallel_tools") === true,
      budget: {
        maxSteps: executionLimit(pick(budget, "maxSteps", "max_steps")),
        maxTools: executionLimit(pick(budget, "maxTools", "max_tools"))
      },
      contextPolicy: contextPolicy(params2)
    };
    const existing = AgentSession.load(store, sessionId);
    const run = new AgentRun(activation, store, this.ports, this, existing);
    this.runs.set(runId, run);
    run.activate(history(params2.history));
    if (child) this.publish(run.session, "subagent/start", {
      runId,
      childSessionId: sessionId,
      parentSessionId,
      roleId: activation.roleId,
      mode: activation.mode
    });
    return run;
  }
  async turn(params2) {
    const run = this.getRun(identifier(pick(params2, "runId", "run_id"), "runId"));
    const result = await run.turn(userContent(params2), optionalRouteId(params2));
    if (run.activation.parentSessionId !== void 0) this.publish(run.session, "subagent/end", {
      runId: run.activation.runId,
      childSessionId: run.activation.sessionId,
      status: result.status,
      result: { text: result.result.text }
    });
    return result;
  }
  async startSubagent(params2) {
    const run = this.activate(params2, true);
    const promise = run.turn(userContent(params2));
    if (params2.wait === true) return this.turnResult(run, await promise);
    void promise.then((result) => this.publish(run.session, "subagent/end", {
      runId: run.activation.runId,
      childSessionId: run.activation.sessionId,
      status: result.status,
      result: { text: result.result.text }
    })).catch(() => void 0);
    return run.snapshot();
  }
  async followup(params2) {
    const run = this.getRun(identifier(pick(params2, "runId", "run_id"), "runId"));
    return this.turnResult(run, await run.followup(userContent(params2), optionalRouteId(params2)));
  }
  startWorkflow(params2) {
    const rawSpec = record(params2.spec ?? params2);
    const spec = workflowRecord(
      { ...rawSpec, workflowId: pick(rawSpec, "workflowId", "workflow_id") ?? `workflow-${(0, import_node_crypto2.randomUUID)()}` },
      this.rootSessionId
    );
    const workflowId = String(spec.workflowId);
    const frozen = structuredClone({ ...spec, status: "queued", autoExecute: false, orchestrationOwner: "optionhelper_host" });
    this.workflows.set(workflowId, frozen);
    this.requireStore().saveWorkflow(workflowId, this.rootSessionId, frozen);
    this.emitRuntime("workflow/start", this.rootSessionId, { workflowId, status: "queued" });
    return frozen;
  }
  workflowStatus(params2) {
    const workflowId = identifier(pick(params2, "workflowId", "workflow_id"), "workflowId");
    const workflow = this.workflows.get(workflowId) ?? this.requireStore().loadWorkflow(workflowId);
    if (workflow === void 0) throw new Error("Workflow\u4E0D\u5B58\u5728");
    return workflow;
  }
  cancelWorkflow(params2) {
    const workflowId = identifier(pick(params2, "workflowId", "workflow_id"), "workflowId");
    for (const run of this.runs.values()) {
      if (run.activation.workflowId === workflowId) run.cancel(String(params2.reason ?? "workflow_cancelled"));
    }
    const workflow = this.workflows.get(workflowId) ?? this.requireStore().loadWorkflow(workflowId) ?? { workflowId };
    const cancelled = { ...workflow, status: "cancelled" };
    this.workflows.set(workflowId, cancelled);
    this.requireStore().saveWorkflow(workflowId, String(workflow.rootSessionId ?? this.rootSessionId), cancelled);
    this.emitRuntime("workflow/end", this.rootSessionId, { workflowId, status: "cancelled" });
    return cancelled;
  }
  sessionResume(params2) {
    const sessionId = identifier(pick(params2, "sessionId", "session_id"), "sessionId");
    const loaded = this.requireStore().loadSession(sessionId);
    if (loaded === void 0) throw new Error("Session\u4E0D\u5B58\u5728");
    const restoredRuns = this.restoreRuns(sessionId);
    const unresolved = this.requireStore().unresolvedCheckpoints(sessionId);
    const recovery = unresolved.map((item) => ({
      ...item,
      recovery: item.operationKind === "tool" && ["started", "outcome_unknown"].includes(String(item.state)) ? "TOOL_OUTCOME_UNKNOWN" : "TOOL_NOT_STARTED"
    }));
    this.emitRuntime("session/recovered", sessionId, { unresolved: recovery.length });
    return {
      sessionId,
      nextSeq: loaded.events.length,
      recovery,
      events: loaded.events,
      restoredRuns: restoredRuns.map((run) => run.snapshot()),
      workflows: this.requireStore().listWorkflows(sessionId)
    };
  }
  sessionHistory(params2) {
    const sessionId = identifier(pick(params2, "sessionId", "session_id"), "sessionId");
    const loaded = this.requireStore().loadSession(sessionId);
    if (loaded === void 0) throw new Error("Session\u4E0D\u5B58\u5728");
    const after = Math.max(0, Number(params2.afterSeq ?? params2.after_seq ?? 0));
    const limit = positiveInteger(params2.limit, 500, 5e3);
    return { sessionId, events: loaded.events.slice(after, after + limit), nextSeq: loaded.events.length };
  }
  sessionTree(params2) {
    const root = String(pick(params2, "sessionId", "session_id") ?? this.rootSessionId);
    const sessions = this.requireStore().listSessions();
    const selected = sessions.filter((session) => session.id === root || this.descendsFrom(session.id, root, sessions));
    return { rootSessionId: root, sessions: selected.map((session) => ({
      sessionId: session.id,
      parentSessionId: session.parentId ?? null,
      kind: session.kind,
      label: session.label
    })) };
  }
  getRun(runId) {
    let run = this.runs.get(runId);
    if (run === void 0) {
      const stored = this.requireStore().loadRun(runId);
      if (stored !== void 0) run = this.restoreRun(stored);
    }
    if (run === void 0) throw new Error("AgentRun\u4E0D\u5B58\u5728\u6216\u5C1A\u672A\u5728\u672C\u8FDB\u7A0B\u6062\u590D");
    return run;
  }
  publish(session, type, data, position = {}) {
    const event = session.append(type, data, position);
    this.emit({
      eventId: `${session.id}:${event.seq}`,
      seq: event.seq,
      sessionId: session.id,
      type,
      timestamp: event.timestamp,
      ...position.turn === void 0 ? {} : { turn: position.turn },
      ...position.step === void 0 ? {} : { step: position.step },
      data
    });
  }
  shutdown() {
    for (const run of this.runs.values()) if (["running", "waiting_tool"].includes(run.snapshot().status)) run.cancel("runtime_shutdown");
    this.store?.close();
    this.store = void 0;
    this.initialized = false;
    return { status: "closed" };
  }
  turnResult(run, result) {
    this.publish(run.session, "subagent/end", {
      runId: run.activation.runId,
      childSessionId: run.activation.sessionId,
      status: result.status,
      result: { text: result.result.text }
    });
    return result;
  }
  emitRuntime(type, sessionId, data) {
    this.emit({ eventId: `${sessionId}:${type}:${Date.now()}`, sessionId, type, timestamp: (/* @__PURE__ */ new Date()).toISOString(), data });
  }
  requireStore() {
    if (!this.initialized || this.store === void 0) throw new Error("Runtime\u5C1A\u672A\u521D\u59CB\u5316");
    return this.store;
  }
  restoreRuns(rootSessionId) {
    const sessions = this.requireStore().listSessions();
    const allowed = new Set(
      sessions.filter((session) => session.id === rootSessionId || this.descendsFrom(session.id, rootSessionId, sessions)).map((session) => session.id)
    );
    const restored = [];
    for (const snapshot of this.requireStore().listRuns()) {
      if (!allowed.has(String(snapshot.sessionId ?? ""))) continue;
      const runId = String(snapshot.runId ?? "");
      const existing = this.runs.get(runId);
      restored.push(existing ?? this.restoreRun(snapshot));
    }
    return restored;
  }
  restoreRun(snapshot) {
    const raw = record(snapshot.activation);
    const runId = identifier(pick(raw, "runId", "run_id") ?? snapshot.runId, "runId");
    const existing = this.runs.get(runId);
    if (existing !== void 0) return existing;
    const sessionId = identifier(pick(raw, "sessionId", "session_id") ?? snapshot.sessionId, "sessionId");
    const parentSession = pick(raw, "parentSessionId", "parent_session_id");
    const parentRun = pick(raw, "parentRunId", "parent_run_id");
    const budget = record(raw.budget);
    const policy = record(pick(raw, "contextPolicy", "context_policy"));
    const activation = {
      runId,
      workflowId: identifier(pick(raw, "workflowId", "workflow_id") ?? snapshot.workflowId, "workflowId"),
      sessionId,
      ...parentSession === void 0 ? {} : { parentSessionId: identifier(parentSession, "parentSessionId") },
      ...parentRun === void 0 ? {} : { parentRunId: identifier(parentRun, "parentRunId") },
      roleId: identifier(pick(raw, "roleId", "role_id") ?? snapshot.roleId, "roleId"),
      label: String(raw.label ?? snapshot.label ?? "Agent").slice(0, 160),
      mode: String(raw.mode ?? snapshot.mode) === "one-shot" ? "one-shot" : "continuable",
      routeId: identifier(raw.routeId ?? snapshot.routeId, "routeId"),
      allowedTools: Array.isArray(raw.allowedTools) ? raw.allowedTools.map(String) : [],
      parallelTools: raw.parallelTools === true,
      budget: {
        maxSteps: executionLimit(budget.maxSteps),
        maxTools: executionLimit(budget.maxTools)
      },
      contextPolicy: contextPolicy({ contextPolicy: policy })
    };
    const session = AgentSession.load(this.requireStore(), sessionId);
    if (session === void 0) throw new Error("AgentRun\u5BF9\u5E94Session\u4E0D\u5B58\u5728");
    const run = new AgentRun(activation, this.requireStore(), this.ports, this, session);
    this.runs.set(runId, run);
    return run;
  }
  descendsFrom(sessionId, root, sessions) {
    let current = sessions.find((session) => session.id === sessionId)?.parentId;
    const seen = /* @__PURE__ */ new Set();
    while (current !== void 0 && !seen.has(current)) {
      if (current === root) return true;
      seen.add(current);
      current = sessions.find((session) => session.id === current)?.parentId;
    }
    return false;
  }
};

// src/host/host-ports.ts
function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}
function finishReason(value) {
  const normalized = String(value ?? "stop").toLowerCase().replaceAll("_", "-");
  if (["tool-call", "tool-calls", "tool-use"].includes(normalized)) return { kind: "tool-calls" };
  if (["max-token", "max-tokens", "length"].includes(normalized)) return { kind: "max-tokens" };
  if (["cancelled", "canceled", "interrupted"].includes(normalized)) return { kind: "cancelled" };
  if (["error", "failed"].includes(normalized)) return { kind: "error" };
  return { kind: "stop" };
}
function usage(value) {
  const raw = object(value);
  const number = (...names) => {
    for (const name of names) if (typeof raw[name] === "number") return raw[name];
    return void 0;
  };
  const input2 = number("input", "input_tokens", "prompt_tokens");
  const output = number("output", "output_tokens", "completion_tokens");
  const reasoning = number("reasoning", "reasoning_tokens");
  const cached = number("cached", "cached_tokens");
  const total = number("total", "total_tokens");
  return {
    ...input2 === void 0 ? {} : { input: input2 },
    ...output === void 0 ? {} : { output },
    ...reasoning === void 0 ? {} : { reasoning },
    ...cached === void 0 ? {} : { cached },
    ...total === void 0 ? {} : { total }
  };
}
function wireMessage(item) {
  const content = item.content.map((block) => {
    if (block.type === "tool-call") return { type: "tool_call", id: block.id, name: block.name, arguments: block.arguments };
    if (block.type === "image") return {
      type: "image",
      attachment_id: block.attachmentId,
      media_type: block.mediaType,
      name: block.name ?? ""
    };
    if (block.type === "document-ref") return {
      type: "document_ref",
      attachment_id: block.attachmentId,
      media_type: block.mediaType,
      name: block.name,
      extraction_status: block.extractionStatus ?? ""
    };
    return { type: block.type, text: block.text };
  });
  return {
    id: item.id,
    role: item.role,
    content,
    ...item.toolCallId === void 0 ? {} : { tool_call_id: item.toolCallId }
  };
}
function modelMessages(messages) {
  const invalidIds = /* @__PURE__ */ new Set();
  for (const message2 of messages) for (const block of message2.content) {
    if (block.type === "tool-call" && !block.name.trim()) invalidIds.add(block.id);
  }
  return messages.filter((message2) => !(message2.role === "tool" && invalidIds.has(message2.toolCallId ?? ""))).map((message2) => ({ ...message2, content: message2.content.filter((block) => block.type !== "tool-call" || !invalidIds.has(block.id)) })).filter((message2) => message2.content.length > 0).map(wireMessage);
}
function latestUserText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message2 = messages[index];
    if (message2?.role !== "user") continue;
    return message2.content.filter((block) => block.type === "text").map((block) => block.text).join("");
  }
  return "";
}
var HostRuntimePorts = class {
  constructor(peer2) {
    this.peer = peer2;
  }
  async *streamModel(request, signal) {
    let nextIndex = 0;
    let activeTextBlock;
    const toolBlocks = /* @__PURE__ */ new Map();
    const toolIndexes = /* @__PURE__ */ new Map();
    const startTextBlock = (blockType) => {
      if (activeTextBlock?.blockType === blockType) return activeTextBlock;
      activeTextBlock = { index: nextIndex++, blockType, text: "", arguments: "" };
      return activeTextBlock;
    };
    const closeTextBlock = function* () {
      if (activeTextBlock === void 0) return;
      const block = activeTextBlock;
      activeTextBlock = void 0;
      yield {
        type: "block-end",
        index: block.index,
        block: block.blockType === "text" ? { type: "text", text: block.text } : { type: "reasoning", text: block.text }
      };
    };
    const closeToolBlocks = function* () {
      for (const block of toolBlocks.values()) {
        yield {
          type: "block-end",
          index: block.index,
          block: {
            type: "tool-call",
            id: block.id ?? `call-${block.index}`,
            name: block.name ?? "",
            arguments: block.arguments
          }
        };
      }
      toolBlocks.clear();
      toolIndexes.clear();
    };
    const stream = this.peer.stream("host.model.stream", {
      requestId: request.requestId,
      routeId: request.routeId,
      modelRouteRef: { routeId: request.routeId },
      sessionId: request.sessionId,
      agentRunId: request.agentRunId,
      roleId: request.roleId,
      prompt: latestUserText(request.messages),
      turn: request.turn,
      step: request.step,
      messages: modelMessages(request.messages),
      tools: request.tools
    }, signal);
    for await (const raw of stream) {
      const item = object(raw);
      const type = String(item.type ?? item.event ?? "text-delta").replaceAll("_", "-");
      if (type === "text-delta" || type === "reasoning-delta") {
        const blockType = type === "text-delta" ? "text" : "reasoning";
        if (activeTextBlock !== void 0 && activeTextBlock.blockType !== blockType) {
          yield* closeTextBlock();
        }
        const block = startTextBlock(blockType);
        if (block.text.length === 0) yield { type: "block-start", index: block.index, blockType };
        const text = String(item.text ?? item.delta ?? (type === "text-delta" ? item.content : "") ?? "");
        block.text += text;
        yield { type, index: block.index, text };
      } else if (type === "tool-call-delta") {
        yield* closeTextBlock();
        const suppliedId = String(item.id ?? item.tool_call_id ?? "");
        const providerIndex = typeof item.index === "number" ? item.index : void 0;
        let block = (suppliedId ? toolBlocks.get(suppliedId) : void 0) ?? (providerIndex === void 0 ? void 0 : toolIndexes.get(providerIndex));
        if (block === void 0) {
          if (!suppliedId && providerIndex === void 0) throw new Error("Tool delta is missing both id and index");
          const id2 = suppliedId || `call-${nextIndex}`;
          block = { index: nextIndex++, blockType: "tool-call", text: "", id: id2, arguments: "" };
          toolBlocks.set(id2, block);
          yield { type: "block-start", index: block.index, blockType: "tool-call" };
        }
        if (providerIndex !== void 0) toolIndexes.set(providerIndex, block);
        if (suppliedId && suppliedId !== block.id) {
          toolBlocks.delete(block.id);
          block.id = suppliedId;
          toolBlocks.set(suppliedId, block);
        }
        const id = block.id;
        if (item.name !== void 0 || item.tool_name !== void 0) block.name = String(item.name ?? item.tool_name);
        const argumentsDelta = String(item.argumentsDelta ?? item.arguments_delta ?? item.arguments ?? item.delta ?? "");
        block.arguments += argumentsDelta;
        yield {
          type,
          index: block.index,
          id,
          ...item.name === void 0 && item.tool_name === void 0 ? {} : { name: String(item.name ?? item.tool_name) },
          argumentsDelta
        };
      } else if (type === "usage") yield { type, usage: usage(item.usage ?? item) };
      else if (type === "finish") {
        yield* closeTextBlock();
        yield* closeToolBlocks();
        if (item.usage !== void 0) yield { type: "usage", usage: usage(item.usage) };
        yield { type, reason: finishReason(item.reason ?? item.finish_reason) };
      }
    }
    yield* closeTextBlock();
    yield* closeToolBlocks();
  }
  async executeTool(request, signal) {
    const raw = object(await this.peer.request("host.tool.call", {
      toolCallId: request.callId,
      toolName: request.name,
      arguments: request.arguments,
      sessionId: request.sessionId,
      agentRunId: request.agentRunId,
      turn: request.turn,
      step: request.step
    }, signal));
    const status = String(raw.status ?? "completed");
    const normalized = ["complete", "succeeded", "success", "available", "pending_question", "needs_input", "pending_approval", "candidate_ready"].includes(status) ? "completed" : ["outcome_unknown", "interrupted"].includes(status) ? "unknown" : status;
    const factRefs = Array.isArray(raw.fact_refs) ? raw.fact_refs.map(String) : Array.isArray(raw.factRefs) ? raw.factRefs.map(String) : raw.fact_ref === void 0 ? [] : [String(raw.fact_ref)];
    return {
      status: ["completed", "failed", "cancelled", "unknown"].includes(normalized) ? normalized : "failed",
      result: raw,
      ...raw.error === void 0 ? {} : { error: String(raw.error) },
      factRefs
    };
  }
  async summarize(messages, maxChars, _signal) {
    const lines = ["\u4EE5\u4E0B\u662F\u6B64\u524D\u5BF9\u8BDD\u7684\u4E0A\u4E0B\u6587\u6458\u8981\uFF1B\u7CBE\u786E\u91D1\u878D\u6570\u503C\u5FC5\u987B\u901A\u8FC7FactRef\u91CD\u65B0\u8BFB\u53D6\uFF1A"];
    for (const item of messages) {
      const raw = item.content.map((block) => {
        if (block.type === "tool-call") return `[\u5DE5\u5177\u8BF7\u6C42:${block.name}]`;
        if (block.type === "image") return `[\u56FE\u7247\u9644\u4EF6:${block.attachmentId}]`;
        if (block.type === "document-ref") return `[\u6587\u6863\u9644\u4EF6:${block.attachmentId}:${block.name}]`;
        return block.text;
      }).join(" ").trim();
      const factRefs = [...raw.matchAll(/fact[_:.-][A-Za-z0-9_.:-]+/g)].map((match) => match[0]);
      const content = item.role === "tool" ? `[\u5DF2\u9A8C\u8BC1\u5DE5\u5177\u7ED3\u679C${factRefs.length > 0 ? `\uFF1BFactRef:${factRefs.join(",")}` : ""}]` : raw.replace(/(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?%?/g, "[\u6570\u503C\u89C1\u4E8B\u5B9E\u5F15\u7528]");
      if (content) lines.push(`${item.role}: ${content}`);
      if (lines.join("\n").length >= maxChars) break;
    }
    return lines.join("\n").slice(0, maxChars);
  }
};

// src/host/json-rpc-peer.ts
var AsyncChunkQueue = class {
  values = [];
  waiters = [];
  ended = false;
  failure;
  push(value) {
    const waiter = this.waiters.shift();
    if (waiter !== void 0) waiter.resolve({ value, done: false });
    else this.values.push(value);
  }
  close() {
    this.ended = true;
    while (this.waiters.length > 0) this.waiters.shift()?.resolve({ value: void 0, done: true });
  }
  fail(error) {
    this.failure = error;
    this.ended = true;
    while (this.waiters.length > 0) this.waiters.shift()?.reject(error);
  }
  async next() {
    if (this.values.length > 0) return { value: this.values.shift(), done: false };
    if (this.failure !== void 0) throw this.failure;
    if (this.ended) return { value: void 0, done: true };
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }
};
var JsonRpcPeer = class {
  constructor(writeLine, timeoutMs = 6e4) {
    this.writeLine = writeLine;
    this.timeoutMs = timeoutMs;
  }
  pending = /* @__PURE__ */ new Map();
  nextId = 0;
  notify(method, params2) {
    this.writeLine({ jsonrpc: "2.0", method, params: params2 });
  }
  request(method, params2, signal) {
    const id = `host_${++this.nextId}`;
    return new Promise((resolve, reject) => {
      if (signal?.aborted === true) {
        reject(new Error(String(signal.reason ?? "cancelled")));
        return;
      }
      const timer = method === "host.tool.call" ? void 0 : setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method} Host\u8BF7\u6C42\u8D85\u65F6`));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, ...timer === void 0 ? {} : { timer } });
      signal?.addEventListener("abort", () => {
        const current = this.pending.get(id);
        if (current === void 0) return;
        this.pending.delete(id);
        clearTimeout(current.timer);
        this.notify("host.model.cancel", { requestId: id });
        current.reject(new Error(String(signal.reason ?? "cancelled")));
      }, { once: true });
      this.writeLine({ jsonrpc: "2.0", id, method, params: params2 });
    });
  }
  stream(method, params2, signal) {
    const id = `host_${++this.nextId}`;
    const queue = new AsyncChunkQueue();
    const completion = new Promise((resolve, reject) => {
      if (signal?.aborted === true) {
        const error = new Error(String(signal.reason ?? "cancelled"));
        queue.fail(error);
        reject(error);
        return;
      }
      const timer = setTimeout(() => {
        this.pending.delete(id);
        this.notify("host.model.cancel", { requestId: id });
        const error = new Error(`${method} Host\u6D41\u7A7A\u95F2\u8D85\u65F6`);
        queue.fail(error);
        reject(error);
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer, chunks: queue });
      signal?.addEventListener("abort", () => {
        const current = this.pending.get(id);
        if (current === void 0) return;
        this.pending.delete(id);
        clearTimeout(current.timer);
        this.notify("host.model.cancel", { requestId: id });
        const error = new Error(String(signal.reason ?? "cancelled"));
        queue.fail(error);
        current.reject(error);
      }, { once: true });
      this.writeLine({ jsonrpc: "2.0", id, method, params: params2 });
    });
    void completion.catch(() => void 0);
    return {
      [Symbol.asyncIterator]: () => ({
        next: () => queue.next()
      })
    };
  }
  accept(message2) {
    if (message2.method === "host.model.chunk") {
      const params2 = message2.params;
      const id2 = String(params2?.requestId ?? params2?.request_id ?? "");
      const current2 = this.pending.get(id2);
      if (current2?.chunks !== void 0) {
        current2.timer?.refresh();
        current2.chunks.push(params2?.chunk);
      }
      return true;
    }
    if (message2.id === void 0 || typeof message2.method === "string") return false;
    const id = String(message2.id);
    const current = this.pending.get(id);
    if (current === void 0) return true;
    this.pending.delete(id);
    clearTimeout(current.timer);
    if (message2.error !== void 0) {
      const raw = message2.error;
      const error = new Error(String(raw.message ?? "Host\u8BF7\u6C42\u5931\u8D25"));
      current.chunks?.fail(error);
      current.reject(error);
    } else {
      current.chunks?.close();
      current.resolve(message2.result);
    }
    return true;
  }
  close(error = new Error("Runtime\u6B63\u5728\u5173\u95ED")) {
    for (const current of this.pending.values()) {
      clearTimeout(current.timer);
      current.chunks?.fail(error);
      current.reject(error);
    }
    this.pending.clear();
  }
};

// src/entrypoint/runtime.ts
var write = (value) => {
  process.stdout.write(`${JSON.stringify(value)}
`);
};
var peer = new JsonRpcPeer(write);
var runtime = new OptionHelperAgentRuntime(new HostRuntimePorts(peer), (event) => {
  write({ jsonrpc: "2.0", method: "runtime.event", params: { event } });
});
function params(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : {};
}
async function dispatch(method, raw) {
  switch (method.replaceAll("/", ".")) {
    case "runtime.initialize":
      return runtime.initialize(raw);
    case "runtime.capabilities":
      return { runtimeId: "optionhelper-agent-runtime", protocolVersion: "2.0", capabilities: runtime.capabilities() };
    case "runtime.shutdown":
      return runtime.shutdown();
    case "runtime.validateJavascript": {
      if (typeof raw.source !== "string") throw new Error("JavaScript source is required");
      new import_node_vm.Script(raw.source, { filename: "report-chart.js" });
      return { valid: true };
    }
    case "agent.activate":
      return runtime.activate(raw).snapshot();
    case "agent.turn":
      return runtime.turn(raw);
    case "agent.status":
      return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).snapshot();
    case "agent.cancel":
      return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).cancel(String(raw.reason ?? "cancelled"));
    case "subagent.start":
      return runtime.startSubagent(raw);
    case "subagent.followup":
      return runtime.followup(raw);
    case "subagent.interrupt":
      return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).cancel(String(raw.reason ?? "cancelled"));
    case "workflow.start":
      return runtime.startWorkflow(raw);
    case "workflow.status":
      return runtime.workflowStatus(raw);
    case "workflow.cancel":
      return runtime.cancelWorkflow(raw);
    case "session.resume":
      return runtime.sessionResume(raw);
    case "session.history":
    case "session.events":
      return runtime.sessionHistory(raw);
    case "session.tree":
      return runtime.sessionTree(raw);
    default:
      throw new Error(`\u672A\u77E5Runtime\u65B9\u6CD5\uFF1A${method}`);
  }
}
var input = (0, import_node_readline.createInterface)({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return;
  }
  if (peer.accept(request)) return;
  if (request.id === void 0 || typeof request.method !== "string") return;
  void dispatch(request.method, params(request.params)).then(
    (result) => write({ jsonrpc: "2.0", id: request.id, result }),
    (error) => write({ jsonrpc: "2.0", id: request.id, error: { code: "RUNTIME_ERROR", message: String(error instanceof Error ? error.message : error).slice(0, 1e3) } })
  );
});
input.on("close", () => {
  peer.close();
  try {
    runtime.shutdown();
  } catch {
  }
});
