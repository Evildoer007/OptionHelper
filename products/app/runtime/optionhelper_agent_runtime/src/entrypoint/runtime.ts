import { createInterface } from "node:readline"

import { OptionHelperAgentRuntime } from "../core/runtime.js"
import type { JsonValue } from "../core/types.js"
import { HostRuntimePorts } from "../host/host-ports.js"
import { JsonRpcPeer } from "../host/json-rpc-peer.js"

const write = (value: object): void => { process.stdout.write(`${JSON.stringify(value)}\n`) }
const peer = new JsonRpcPeer(write)
const runtime = new OptionHelperAgentRuntime(new HostRuntimePorts(peer), (event) => {
  write({ jsonrpc: "2.0", method: "runtime.event", params: { event } })
})

function params(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

async function dispatch(method: string, raw: Record<string, unknown>): Promise<unknown> {
  switch (method.replaceAll("/", ".")) {
    case "runtime.initialize": return runtime.initialize(raw)
    case "runtime.capabilities": return { runtimeId: "optionhelper-agent-runtime", protocolVersion: "2.0", capabilities: runtime.capabilities() }
    case "runtime.shutdown": return runtime.shutdown()
    case "agent.activate": return runtime.activate(raw).snapshot()
    case "agent.turn": return runtime.turn(raw)
    case "agent.status": return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).snapshot()
    case "agent.cancel": return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).cancel(String(raw.reason ?? "cancelled"))
    case "subagent.start": return runtime.startSubagent(raw)
    case "subagent.followup": return runtime.followup(raw)
    case "subagent.interrupt": return runtime.getRun(String(raw.runId ?? raw.run_id ?? "")).cancel(String(raw.reason ?? "cancelled"))
    case "workflow.start": return runtime.startWorkflow(raw)
    case "workflow.status": return runtime.workflowStatus(raw)
    case "workflow.cancel": return runtime.cancelWorkflow(raw)
    case "session.resume": return runtime.sessionResume(raw)
    case "session.history":
    case "session.events": return runtime.sessionHistory(raw)
    case "session.tree": return runtime.sessionTree(raw)
    default: throw new Error(`未知Runtime方法：${method}`)
  }
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
input.on("line", (line) => {
  let request: Record<string, unknown>
  try { request = JSON.parse(line) as Record<string, unknown> } catch { return }
  if (peer.accept(request)) return
  if (request.id === undefined || typeof request.method !== "string") return
  void dispatch(request.method, params(request.params)).then(
    (result) => write({ jsonrpc: "2.0", id: request.id, result: result as JsonValue }),
    (error) => write({ jsonrpc: "2.0", id: request.id, error: { code: "RUNTIME_ERROR", message: String(error instanceof Error ? error.message : error).slice(0, 1_000) } }),
  )
})

input.on("close", () => {
  peer.close()
  try { runtime.shutdown() } catch {}
})
