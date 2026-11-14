// WebKit accepts at most 100 history writes in 10 seconds. Leave room for
// startup writes and keep the latest route when workspace actions arrive faster.
const WRITE_INTERVAL_MS = 125;
const QUOTA_WINDOW_MS = 10_000;

export function createWorkspaceHistory({
  location = window.location,
  history = window.history,
  now = () => performance.now(),
  setDelay = (callback, delay) => window.setTimeout(callback, delay),
  clearDelay = (timer) => window.clearTimeout(timer),
} = {}) {
  let pending = null;
  let timer = null;
  let nextWriteAt = -Infinity;

  function flush() {
    timer = null;
    if (!pending) return;
    if (pending.url.href === location.href) {
      pending = null;
      return;
    }
    const timestamp = now();
    if (timestamp < nextWriteAt) {
      timer = setDelay(flush, nextWriteAt - timestamp);
      return;
    }
    const route = pending;
    pending = null;
    nextWriteAt = timestamp + WRITE_INTERVAL_MS;
    try {
      history.replaceState(route.state, "", route.url);
    } catch (error) {
      // The address bar reflects the workspace. A browser failure must never
      // interrupt task selection, module mounting, or transition cleanup.
      if (error?.name === "SecurityError" && !route.retried) {
        pending = { ...route, retried: true };
        nextWriteAt = timestamp + QUOTA_WINDOW_MS;
        timer = setDelay(flush, QUOTA_WINDOW_MS);
      } else {
        console.warn("Could not update the OptionHelper workspace URL", error);
      }
    }
  }

  function replace({ mode, taskId, module }) {
    const pathname = mode === "desk" ? "/optdesk" : "/optchat";
    const url = new URL(location.pathname === pathname ? location.href : pathname, location.origin);
    if (taskId) url.searchParams.set("task", taskId);
    else url.searchParams.delete("task");
    if (mode === "desk" && module) url.searchParams.set("module", module);
    else url.searchParams.delete("module");
    pending = { url, state: { mode, task: taskId || "" }, retried: false };
    if (timer === null) flush();
  }

  function cancelPending() {
    if (timer !== null) clearDelay(timer);
    timer = null;
    pending = null;
  }

  return { replace, cancelPending };
}
