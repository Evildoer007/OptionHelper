import type { AgentMessage, JsonValue, SessionEvent, SessionHeader, SessionKind } from "./types.js"
import { SqliteSessionStore } from "../persistence/sqlite-store.js"

function clone<T>(value: T): T {
  return structuredClone(value)
}

export interface SessionCreateOptions {
  readonly id: string
  readonly kind: SessionKind
  readonly parentId?: string
  readonly label: string
  readonly importedLegacyHistory?: boolean
}

/** Append-only live session whose durable write commits before memory changes. */
export class AgentSession {
  readonly header: SessionHeader
  private readonly mutableEvents: SessionEvent[]

  private constructor(
    private readonly store: SqliteSessionStore,
    header: SessionHeader,
    events: SessionEvent[],
  ) {
    this.header = Object.freeze(clone(header))
    this.mutableEvents = events.map((event) => Object.freeze(clone(event)))
  }

  static create(store: SqliteSessionStore, options: SessionCreateOptions): AgentSession {
    const header: SessionHeader = {
      id: options.id,
      kind: options.kind,
      ...(options.parentId === undefined ? {} : { parentId: options.parentId }),
      label: options.label,
      createdAt: new Date().toISOString(),
      importedLegacyHistory: options.importedLegacyHistory ?? false,
    }
    store.createSession(header)
    return new AgentSession(store, header, [])
  }

  static load(store: SqliteSessionStore, sessionId: string): AgentSession | undefined {
    const stored = store.loadSession(sessionId)
    return stored === undefined ? undefined : new AgentSession(store, stored.header, stored.events)
  }

  get id(): string {
    return this.header.id
  }

  get events(): readonly SessionEvent[] {
    return this.mutableEvents
  }

  append(type: string, data: JsonValue, position: { turn?: number; step?: number } = {}): SessionEvent {
    const event: SessionEvent = Object.freeze({
      sessionId: this.id,
      seq: this.mutableEvents.length,
      type,
      ...(position.turn === undefined ? {} : { turn: position.turn }),
      ...(position.step === undefined ? {} : { step: position.step }),
      timestamp: new Date().toISOString(),
      data: clone(data),
    })
    this.store.appendEvent(event)
    this.mutableEvents.push(event)
    return event
  }

  importHistory(messages: readonly AgentMessage[]): void {
    if (this.mutableEvents.length > 0) throw new Error("legacy history can only be imported into an empty session")
    this.append("legacy/history-imported", { count: messages.length })
    for (const message of messages) this.append("conversation/message", message as unknown as JsonValue)
  }

  surfaceEvents(): SessionEvent[] {
    const replacement = new Map<number, SessionEvent>()
    const shadowed = new Set<number>()
    for (const event of this.mutableEvents) {
      const data = event.data as Record<string, unknown>
      const sourceSeqs = Array.isArray(data.sourceSeqs) ? data.sourceSeqs : []
      for (const value of sourceSeqs) if (Number.isSafeInteger(value)) shadowed.add(Number(value))
      const replacementSeq = data.replacesSeq
      if (Number.isSafeInteger(replacementSeq)) replacement.set(Number(replacementSeq), event)
    }
    return this.mutableEvents
      .filter((event) => isModelVisible(event.type) && !shadowed.has(event.seq))
      .map((event) => replacement.get(event.seq) ?? event)
  }

  deriveMessages(): AgentMessage[] {
    const messages: AgentMessage[] = []
    for (const event of this.surfaceEvents()) {
      if (event.type === "conversation/message" || event.type === "assistant/message" || event.type === "tool/result") {
        const candidate = event.data as unknown as Partial<AgentMessage>
        if (typeof candidate.id === "string"
          && typeof candidate.role === "string"
          && Array.isArray(candidate.content)) {
          messages.push(clone(candidate as AgentMessage))
        }
      } else if (event.type === "compaction/summary") {
        const data = event.data as Record<string, unknown>
        messages.push({
          id: `summary-${event.seq}`,
          role: "system",
          content: [{ type: "text", text: String(data.summary ?? "") }],
        })
      }
    }
    return messages
  }
}

function isModelVisible(type: string): boolean {
  return type === "conversation/message"
    || type === "assistant/message"
    || type === "tool/result"
    || type === "compaction/summary"
}
