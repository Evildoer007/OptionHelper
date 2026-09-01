import type { ToolCallBlock, ToolExecutionRequest, ToolExecutionResult } from "./types.js"

export interface ScheduledToolResult {
  readonly call: ToolCallBlock
  readonly result: ToolExecutionResult
}

export async function executeToolCalls(
  calls: readonly ToolCallBlock[],
  createRequest: (call: ToolCallBlock) => ToolExecutionRequest,
  execute: (request: ToolExecutionRequest, signal: AbortSignal) => Promise<ToolExecutionResult>,
  signal: AbortSignal,
  parallel: boolean,
): Promise<ScheduledToolResult[]> {
  const run = async (call: ToolCallBlock): Promise<ScheduledToolResult> => {
    let parsed: unknown
    try { parsed = call.arguments.trim() ? JSON.parse(call.arguments) : {} } catch {
      return { call, result: { status: "failed", error: "工具参数不是有效JSON" } }
    }
    const request = createRequest({ ...call, arguments: JSON.stringify(parsed) })
    return { call, result: await execute(request, signal) }
  }
  if (parallel) return Promise.all(calls.map(run))
  const results: ScheduledToolResult[] = []
  for (const call of calls) results.push(await run(call))
  return results
}
