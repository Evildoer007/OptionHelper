/**
 * Own every asynchronous resource for one visual transition.
 *
 * A new mode switch disposes the prior scope before it can attach an event
 * listener or finish a stale timeout.  This deliberately contains no routing
 * or DOM policy so callers remain responsible for their own final state.
 */
export function createTransitionScope(options = {}) {
  const requestFrame = options.requestFrame || globalThis.requestAnimationFrame.bind(globalThis);
  const cancelFrame = options.cancelFrame || globalThis.cancelAnimationFrame.bind(globalThis);
  const setDelay = options.setDelay || globalThis.setTimeout.bind(globalThis);
  const clearDelay = options.clearDelay || globalThis.clearTimeout.bind(globalThis);
  const frames = new Set();
  let timer = null;
  let removeListener = () => {};
  let disposed = false;

  const frame = (callback) => {
    if (disposed) return null;
    let handle = null;
    handle = requestFrame(() => {
      frames.delete(handle);
      if (!disposed) callback();
    });
    frames.add(handle);
    return handle;
  };

  const delay = (callback, milliseconds) => {
    if (disposed) return null;
    if (timer !== null) clearDelay(timer);
    timer = setDelay(() => {
      timer = null;
      if (!disposed) callback();
    }, milliseconds);
    return timer;
  };

  const listen = (target, type, callback) => {
    if (disposed) return;
    removeListener();
    const listener = (event) => {
      if (!disposed) callback(event);
    };
    target.addEventListener(type, listener);
    removeListener = () => target.removeEventListener(type, listener);
  };

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    for (const handle of frames) cancelFrame(handle);
    frames.clear();
    if (timer !== null) clearDelay(timer);
    timer = null;
    removeListener();
    removeListener = () => {};
  };

  return { frame, delay, listen, dispose, isActive: () => !disposed };
}
