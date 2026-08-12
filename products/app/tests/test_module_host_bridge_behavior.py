"""Executable browser-bridge race regression without a real browser dependency."""

from __future__ import annotations

import subprocess
from pathlib import Path
import unittest


PROJECT = Path(__file__).resolve().parents[3]
BRIDGE = PROJECT / "core" / "src" / "runtime" / "browser" / "module_host_bridge.js"


HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");

class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, listener) {
    const values = this.listeners.get(type) || [];
    values.push(listener);
    this.listeners.set(type, values);
  }
  removeEventListener(type, listener) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((value) => value !== listener));
  }
  dispatchEvent(event) {
    for (const listener of this.listeners.get(event.type) || []) listener(event);
    return true;
  }
}
class StubMutationObserver { constructor() {} observe() {} }
class StubElement {}
class StubCustomEvent { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }

function createRuntime({ embedded, module = "payoffer" }) {
  const calls = [];
  const downloaded = [];
  const timers = new Map();
  let nextTimer = 1;
  const document = new Target();
  document.readyState = "complete";
  document.documentElement = { classList: { add() {} } };
  document.body = { classList: { add() {} }, append() {} };
  document.head = { append() {} };
  document.getElementById = () => null;
  document.createElement = (tag) => tag === "a"
    ? { style: {}, dataset: {}, click() { downloaded.push({ href: this.href, download: this.download }); }, remove() {} }
    : { style: {} };
  document.querySelectorAll = () => [];
  const window = new Target();
  const parent = embedded ? { postMessage() {} } : window;
  window.parent = parent;
  window.location = {
    origin: "http://127.0.0.1:4321",
    href: `http://127.0.0.1:4321/capability/assets/pages/${module}/${module}.html?host=optdesk`,
    pathname: `/capability/assets/pages/${module}/${module}.html`,
    search: embedded ? "?host=optdesk" : "",
  };
  window.setTimeout = (callback) => {
    const id = nextTimer++;
    timers.set(id, callback);
    return id;
  };
  window.clearTimeout = (id) => timers.delete(id);
  window.fetch = async (input, init = {}) => {
    calls.push({ input: typeof input === "string" ? input : input.url, init });
    return new Response(JSON.stringify({ ok: true, native: true }), { status: 200 });
  };
  const sandbox = {
    window, document, location: window.location, URL, URLSearchParams, Response, Request, DOMException,
    AbortController, MutationObserver: StubMutationObserver, Element: StubElement,
    CustomEvent: StubCustomEvent, crypto: { randomUUID: () => "request-id" }, console,
  };
  vm.runInNewContext(source, sandbox, { filename: "module_host_bridge.js" });
  return { window, parent, document, calls, downloaded, timers };
}

function context(module = "payoffer") {
  return {
    module, host_kind: "app", context_id: "context-1",
    capability_token: `v1.9999999999.${"a".repeat(64)}`,
    page_hash: "b".repeat(64), task_id: null, analysis_case_id: null,
    candidate_id: null, catalog_version: null, contract_fingerprint: null,
  };
}
function deliver(runtime, value = context()) {
  runtime.window.dispatchEvent({
    type: "message", origin: runtime.window.location.origin, source: runtime.parent,
    data: { type: "optionhelper.module-host-context", context: value },
  });
}
function assert(condition, message) { if (!condition) throw new Error(message); }
async function tick() { await Promise.resolve(); await Promise.resolve(); }

async function normalHandshake() {
  const runtime = createRuntime({ embedded: true });
  const pending = runtime.window.fetch("/api/catalog");
  await tick();
  assert(runtime.calls.length === 0, "catalog escaped before Host context");
  deliver(runtime);
  const response = await pending;
  const body = await response.json();
  assert(response.status === 200 && body.ok, "catalog did not resolve after context");
  assert(runtime.calls.length === 1 && runtime.calls[0].input === "/api/tools/payoffer", "catalog did not use Payoffer tool endpoint");
  const request = JSON.parse(runtime.calls[0].init.body);
  assert(request.action === "catalog", "catalog action was not preserved");
  assert(runtime.calls[0].init.method === "POST", "catalog must be normalized to Host POST");
}
async function timeoutIsExplicit() {
  const runtime = createRuntime({ embedded: true });
  const pending = runtime.window.fetch("/api/catalog");
  await tick();
  assert(runtime.timers.size === 1, "Host wait timeout was not registered");
  for (const callback of [...runtime.timers.values()]) callback();
  const response = await pending;
  const body = await response.json();
  assert(response.status === 503 && body.ok === false, "context timeout must be explicit 503");
  assert(runtime.calls.length === 0, "timed out catalog escaped to native fetch");
}
async function standaloneStaysNative() {
  const runtime = createRuntime({ embedded: false });
  const response = await runtime.window.fetch("/api/catalog");
  assert(response.status === 200 && runtime.calls.length === 1, "standalone page lost local-service fetch behavior");
}
async function nonHostRequestsStayNativeAndAbortWorks() {
  const runtime = createRuntime({ embedded: true });
  await runtime.window.fetch("https://example.invalid/api/catalog");
  const wrongMethod = await runtime.window.fetch("/api/run", { method: "GET" });
  assert(wrongMethod.status === 405, "known Host route with wrong method must be rejected");
  assert(runtime.calls.length === 1, "cross-origin request was rewritten by Host bridge");
  const controller = new AbortController();
  controller.abort();
  await runtime.window.fetch("/api/catalog", { signal: controller.signal })
    .then(() => { throw new Error("aborted request unexpectedly resolved"); })
    .catch((error) => assert(error.name === "AbortError", "abort signal was not propagated"));
  assert(runtime.calls.length === 1, "aborted request reached native fetch");
}
async function dataAssetDownloadUsesTheHostBoundary() {
  const runtime = createRuntime({ embedded: true, module: "datafetcher" });
  deliver(runtime, context("datafetcher"));
  const response = await runtime.window.fetch("/api/assets/data-abc_01/download");
  assert(response.status === 200, "controlled download did not resolve");
  assert(runtime.calls.length === 1, "download did not make exactly one Host request");
  const call = runtime.calls[0];
  assert(call.input === "/api/assets/data-abc_01/download", "download route changed its opaque id");
  assert(call.init.method === "POST", "hosted download must use authenticated Host POST");
  assert(call.init.headers["X-OptionHelper-Module-Context"], "download omitted Host context");
  assert(call.init.headers["X-OptionHelper-Request-Id"] === "request-id", "download omitted replay id");
  const unsafe = await runtime.window.fetch("/api/assets/%2Fprivate%2Finput.csv/download");
  assert(unsafe.status === 400, "path-like download id was not rejected");
  assert(runtime.calls.length === 1, "unsafe download escaped to native fetch");

  const clickRuntime = createRuntime({ embedded: true, module: "datafetcher" });
  deliver(clickRuntime, context("datafetcher"));
  let prevented = false;
  const anchor = {
    href: `${clickRuntime.window.location.origin}/api/assets/data-click/download`, dataset: {},
    closest(selector) { return selector === "a[href]" ? this : null; },
  };
  clickRuntime.document.dispatchEvent({ type: "click", target: anchor, preventDefault() { prevented = true; } });
  for (let index = 0; index < 8; index += 1) await tick();
  assert(prevented, "embedded download anchor was not intercepted");
  assert(clickRuntime.calls.length === 1 && clickRuntime.calls[0].init.method === "POST", "anchor bypassed Host download POST");
  assert(clickRuntime.downloaded.length === 1 && clickRuntime.downloaded[0].download === "data-click.csv", "CSV download was not materialized");
}

(async () => {
  await normalHandshake();
  await timeoutIsExplicit();
  await standaloneStaysNative();
  await nonHostRequestsStayNativeAndAbortWorks();
  await dataAssetDownloadUsesTheHostBoundary();
  process.stdout.write("module-host bridge behavior verified\n");
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
"""


class ModuleHostBridgeBehaviorTests(unittest.TestCase):
    def test_context_race_timeout_and_standalone_paths(self) -> None:
        result = subprocess.run(
            ["node", "-e", HARNESS, str(BRIDGE)],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("module-host bridge behavior verified", result.stdout)


if __name__ == "__main__":
    unittest.main()
