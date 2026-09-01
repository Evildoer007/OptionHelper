# OptionHelper Agent Runtime source mapping

## 1. Fixed source revision

- Repository: `https://github.com/deepseek-ai/deepseek-harness`
- Fixed commit: `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`
- License: MIT, recorded in `OptionHelper-Agent-Runtime-LICENSE.txt`
- Review rule: this mapping is provenance only; it is not an instruction to
  copy the upstream repository or to add its product surface to OptionHelper.

## 2. Migrated source modules and local destinations

| Upstream module at the fixed commit | OptionHelper surface | Adapted boundary |
|---|---|---|
| `packages/core/session` | `products/app/runtime/optionhelper_agent_runtime/src/core/session.ts`; `src/persistence/sqlite-store.ts` | Append-only events, model-surface projection, event replay and SQLite persistence. |
| `packages/core/agent` | `products/app/runtime/optionhelper_agent_runtime/src/core/inbox.ts`; `src/core/agent.ts` | Durable Inbox, run lifecycle and bounded turn ownership. |
| `packages/core/agent-loop` | `products/app/runtime/optionhelper_agent_runtime/src/core/agent.ts`; `src/core/tool-scheduler.ts` | Turn and Step lifecycle, native tool continuation, cancellation and result pairing. |
| `packages/core/system-prompt` | `products/app/backend/agent_runtime/conversation_agent.py`; `runtime_recommender.py` | Ordered OptionHelper persona, business context, tool guidance and financial-safety layers. |
| `packages/core/session-persistence-sqlite` | `products/app/runtime/optionhelper_agent_runtime/src/persistence/sqlite-store.ts` | Runtime-owned Session, AgentRun and Checkpoint persistence. |
| `packages/subagent/subagent`; `packages/subagent/in-process-driver` | `products/app/runtime/optionhelper_agent_runtime/src/core/runtime.ts` | One-shot and continuable Child Sessions with explicit parent ownership. |
| `packages/compaction/compaction` | `products/app/runtime/optionhelper_agent_runtime/src/context/context-maintainer.ts` | Checkpointed summary compaction without converting summaries into financial facts. |
| `packages/compaction/compaction-tool-result-pruner` | `products/app/runtime/optionhelper_agent_runtime/src/context/context-maintainer.ts` | Oversized Tool Result pruning before summary compaction. |
| `packages/llm/llm` | `products/app/runtime/optionhelper_agent_runtime/src/llm/assembler.ts`; `src/llm/retry-policy.ts`; `src/host/host-ports.ts` | Ordered stream blocks, bounded retry and Host-controlled model streaming. |
| `packages/llm/token-meter` | `products/app/runtime/optionhelper_agent_runtime/src/context/token-meter.ts` | Usage accounting and context-budget estimation. |
| `packages/client/runtime/src/client/sessions/partial.ts` | `products/app/frontend/optchat/optchat.js` | Turn-, Step- and Block-indexed live Assistant projection without combining independent Reasoning blocks. |
| `packages/client/ui-conversation/src/client/chat/ReasoningRow.tsx`; `ReasoningRow.module.css` | `products/app/frontend/shared/app.js`; `products/app/frontend/shared/refinement.css` | Collapsed Reasoning disclosure, live latest-line preview, settled first-line preview, reduced-motion behavior and OptionHelper visual adaptation. |

The listed modules were migrated at source-mechanism level and then adapted to
OptionHelper-owned interfaces. The product does not import upstream packages
at runtime, does not depend on the upstream plugin host, and does not expose
upstream provider, shell or workspace capabilities.

## 3. Explicitly not migrated

- Upstream application frontend, web server, CLI, desktop shell, and branding.
- Plugin loader, hot reload, external telemetry, feedback, and workspace management.
- Shell, terminal, filesystem, Git, LSP, code execution, and sandbox features.
- External agent-provider adapters and protocol-specific child providers.
- Experimental team/task-board features.
- Skill registration and distribution mechanisms.

## 4. Local modifications

- Replaced the upstream service/plugin graph with a bundled runtime process
  controlled by an OptionHelper JSON-RPC boundary and a locked build-only
  TypeScript toolchain.
- Kept model credentials behind the existing OptionHelper ModelGateway and
  SecretProvider boundary.
- Kept financial facts behind CandidateVersion, ResultStore, and the existing
  business-tool bridge.
- Added task, tenant, principal, and AgentRun binding at the Python control
  boundary.
- Added local OptionHelper runtime statistics for operational diagnosis only;
  they remain on the user's machine and are not an upstream telemetry or
  feedback channel.
- Added a packaging preflight that fails closed when native SEA tooling,
  platform alignment, or signing prerequisites are unavailable.

## 5. Dependency and distribution status

The packaging entry builds a native SEA candidate but does not by itself mark
macOS or Windows support. A platform is distributable only after its native
artifact, code signature, runtime launch, shutdown, and product integration
checks pass. The Windows artifact is not treated as supported merely because
its target name exists in the manifest.
