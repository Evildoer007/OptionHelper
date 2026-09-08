import type { JsonValue } from "../core/types.js"

interface PendingHostRequest {
  readonly resolve: (value: unknown) => void
  readonly reject: (error: Error) => void
  readonly timer?: NodeJS.Timeout
  readonly chunks?: AsyncChunkQueue
}

class AsyncChunkQueue {
  private readonly values: unknown[] = []
  private readonly waiters: Array<{
    readonly resolve: (value: IteratorResult<unknown>) => void
    readonly reject: (error: Error) => void
  }> = []
  private ended = false
  private failure?: Error

  push(value: unknown): void {
    const waiter = this.waiters.shift()
    if (waiter !== undefined) waiter.resolve({ value, done: false })
    else this.values.push(value)
  }

  close(): void {
    this.ended = true
    while (this.waiters.length > 0) this.waiters.shift()?.resolve({ value: undefined, done: true })
  }

  fail(error: Error): void {
    this.failure = error
    this.ended = true
    while (this.waiters.length > 0) this.waiters.shift()?.reject(error)
  }

  async next(): Promise<IteratorResult<unknown>> {
    if (this.values.length > 0) return { value: this.values.shift(), done: false }
    if (this.failure !== undefined) throw this.failure
    if (this.ended) return { value: undefined, done: true }
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }))
  }
}

/** Full-duplex JSON-RPC peer used by the Runtime for Host callbacks. */
export class JsonRpcPeer {
  private readonly pending = new Map<string, PendingHostRequest>()
  private nextId = 0

  constructor(
    private readonly writeLine: (value: object) => void,
    private readonly timeoutMs = 60_000,
  ) {}

  notify(method: string, params: JsonValue): void {
    this.writeLine({ jsonrpc: "2.0", method, params })
  }

  request(method: string, params: JsonValue, signal?: AbortSignal): Promise<unknown> {
    const id = `host_${++this.nextId}`
    return new Promise((resolve, reject) => {
      if (signal?.aborted === true) { reject(new Error(String(signal.reason ?? "cancelled"))); return }
      // Business tools own their cancellation and network timeouts. A pricing
      // or backtest job must not be cut off by the model RPC idle window.
      const timer = method === "host.tool.call" ? undefined : setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`${method} Host请求超时`))
      }, this.timeoutMs)
      this.pending.set(id, { resolve, reject, ...(timer === undefined ? {} : { timer }) })
      signal?.addEventListener("abort", () => {
        const current = this.pending.get(id)
        if (current === undefined) return
        this.pending.delete(id)
        clearTimeout(current.timer)
        this.notify("host.model.cancel", { requestId: id })
        current.reject(new Error(String(signal.reason ?? "cancelled")))
      }, { once: true })
      this.writeLine({ jsonrpc: "2.0", id, method, params })
    })
  }

  stream(method: string, params: JsonValue, signal?: AbortSignal): AsyncIterable<unknown> {
    const id = `host_${++this.nextId}`
    const queue = new AsyncChunkQueue()
    const completion = new Promise<unknown>((resolve, reject) => {
      if (signal?.aborted === true) {
        const error = new Error(String(signal.reason ?? "cancelled"))
        queue.fail(error)
        reject(error)
        return
      }
      const timer = setTimeout(() => {
        this.pending.delete(id)
        this.notify("host.model.cancel", { requestId: id })
        const error = new Error(`${method} Host流空闲超时`)
        queue.fail(error)
        reject(error)
      }, this.timeoutMs)
      this.pending.set(id, { resolve, reject, timer, chunks: queue })
      signal?.addEventListener("abort", () => {
        const current = this.pending.get(id)
        if (current === undefined) return
        this.pending.delete(id)
        clearTimeout(current.timer)
        this.notify("host.model.cancel", { requestId: id })
        const error = new Error(String(signal.reason ?? "cancelled"))
        queue.fail(error)
        current.reject(error)
      }, { once: true })
      this.writeLine({ jsonrpc: "2.0", id, method, params })
    })
    void completion.catch(() => undefined)
    return {
      [Symbol.asyncIterator]: () => ({
        next: () => queue.next(),
      }),
    }
  }

  accept(message: Record<string, unknown>): boolean {
    if (message.method === "host.model.chunk") {
      const params = message.params as Record<string, unknown> | undefined
      const id = String(params?.requestId ?? params?.request_id ?? "")
      const current = this.pending.get(id)
      if (current?.chunks !== undefined) {
        // A live stream may run longer than one idle window. User cancellation remains
        // available; only silence expires this watchdog.
        current.timer?.refresh()
        current.chunks.push(params?.chunk)
      }
      return true
    }
    if (message.id === undefined || typeof message.method === "string") return false
    const id = String(message.id)
    const current = this.pending.get(id)
    if (current === undefined) return true
    this.pending.delete(id)
    clearTimeout(current.timer)
    if (message.error !== undefined) {
      const raw = message.error as Record<string, unknown>
      const error = new Error(String(raw.message ?? "Host请求失败"))
      current.chunks?.fail(error)
      current.reject(error)
    } else {
      current.chunks?.close()
      current.resolve(message.result)
    }
    return true
  }

  close(error = new Error("Runtime正在关闭")): void {
    for (const current of this.pending.values()) {
      clearTimeout(current.timer)
      current.chunks?.fail(error)
      current.reject(error)
    }
    this.pending.clear()
  }
}
