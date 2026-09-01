import type { AgentMessage, InboxTarget, JsonValue } from "./types.js"
import { AgentSession } from "./session.js"

type InboxState = Record<InboxTarget, AgentMessage[]>

/** Durable two-queue inbox for next-turn and next-step input. */
export class Inbox {
  private readonly state: InboxState = { "next-turn": [], "next-step": [] }

  constructor(private readonly session: AgentSession) {
    for (const event of session.events) {
      if (event.type === "agent/inbox-spliced") this.apply(event.data as unknown as InboxSplice)
    }
  }

  get nextTurn(): readonly AgentMessage[] {
    return this.state["next-turn"]
  }

  get nextStep(): readonly AgentMessage[] {
    return this.state["next-step"]
  }

  get hasPending(): boolean {
    return this.nextTurn.length > 0 || this.nextStep.length > 0
  }

  append(target: InboxTarget, message: AgentMessage): void {
    this.splice(target, this.state[target].length, 0, [message])
  }

  prepend(target: InboxTarget, message: AgentMessage): void {
    this.splice(target, 0, 0, [message])
  }

  clear(): void {
    this.splice("next-step", 0, this.nextStep.length, [])
    this.splice("next-turn", 0, this.nextTurn.length, [])
  }

  claim(target: InboxTarget, turn: number): AgentMessage[] {
    const claimed = this.mutate("next-step", 0, this.nextStep.length, [], false)
    if (target === "next-turn") claimed.push(...this.mutate("next-turn", 0, 1, [], false))
    for (const message of claimed) this.session.append("agent/inbox-claimed", { messageId: message.id }, { turn })
    return claimed
  }

  splice(target: InboxTarget, start: number, deleteCount: number, inserted: AgentMessage[]): AgentMessage[] {
    return this.mutate(target, start, deleteCount, inserted, true)
  }

  private mutate(
    target: InboxTarget,
    start: number,
    deleteCount: number,
    inserted: AgentMessage[],
    cancelled: boolean,
  ): AgentMessage[] {
    const inbox = this.state[target]
    const offset = Number.isNaN(Math.trunc(start)) ? 0 : Math.trunc(start)
    const actualStart = offset < 0 ? Math.max(inbox.length + offset, 0) : Math.min(offset, inbox.length)
    const count = Number.isNaN(Math.trunc(deleteCount)) ? 0 : Math.trunc(deleteCount)
    const actualDeleteCount = Math.min(Math.max(count, 0), inbox.length - actualStart)
    if (actualDeleteCount === 0 && inserted.length === 0) return []
    const splice: InboxSplice = {
      target,
      start: actualStart,
      removedCount: actualDeleteCount,
      inserted: structuredClone(inserted),
      cancelled,
    }
    this.validate(splice)
    const event = this.session.append("agent/inbox-spliced", splice as unknown as JsonValue)
    const durable = event.data as unknown as InboxSplice
    return inbox.splice(durable.start, durable.removedCount, ...durable.inserted)
  }

  private apply(splice: InboxSplice): void {
    this.validate(splice)
    this.state[splice.target].splice(splice.start, splice.removedCount, ...structuredClone(splice.inserted))
  }

  private validate(splice: InboxSplice): void {
    const inbox = this.state[splice.target]
    if (!Number.isSafeInteger(splice.start) || splice.start < 0 || splice.start > inbox.length
      || !Number.isSafeInteger(splice.removedCount) || splice.removedCount < 0
      || splice.start + splice.removedCount > inbox.length) {
      throw new Error("invalid inbox splice")
    }
    const candidate = inbox.toSpliced(splice.start, splice.removedCount, ...splice.inserted)
    const ids = new Set<string>()
    for (const message of splice.target === "next-turn"
      ? [...candidate, ...this.nextStep]
      : [...this.nextTurn, ...candidate]) {
      if (ids.has(message.id)) throw new Error(`message ${message.id} is already pending`)
      ids.add(message.id)
    }
  }
}

interface InboxSplice {
  readonly target: InboxTarget
  readonly start: number
  readonly removedCount: number
  readonly inserted: AgentMessage[]
  readonly cancelled: boolean
}
