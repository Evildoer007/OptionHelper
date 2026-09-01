export interface RetryPolicy {
  readonly maxRetries: number
  readonly initialDelayMs: number
  readonly maxDelayMs: number
  readonly jitterRatio: number
  readonly retryableCodes: readonly string[]
}

export const DEFAULT_RETRY_POLICY: RetryPolicy = Object.freeze({
  maxRetries: 5,
  initialDelayMs: 500,
  maxDelayMs: 10_000,
  jitterRatio: 0.1,
  retryableCodes: Object.freeze(["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"]),
})

export function retryDelay(policy: RetryPolicy, attempt: number, retryAfterMs?: number): number {
  const exponential = Math.min(policy.maxDelayMs, policy.initialDelayMs * (2 ** Math.max(0, attempt - 1)))
  const proposed = retryAfterMs === undefined ? exponential : Math.min(policy.maxDelayMs, retryAfterMs)
  const jitter = proposed * policy.jitterRatio
  return Math.max(0, Math.round(proposed - jitter + Math.random() * jitter * 2))
}

export function shouldRetry(policy: RetryPolicy, code: string, attempt: number, committedOutput: boolean): boolean {
  return !committedOutput && attempt <= policy.maxRetries && policy.retryableCodes.includes(code)
}

export async function waitForRetry(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) throw signal.reason
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, milliseconds)
    const abort = (): void => {
      clearTimeout(timer)
      reject(signal.reason)
    }
    signal.addEventListener("abort", abort, { once: true })
    if (signal.aborted) abort()
  })
}
