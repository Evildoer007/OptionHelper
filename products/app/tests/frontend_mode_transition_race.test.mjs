import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createTransitionScope } from "../frontend/shared/transition-scope.js";

function createScheduler() {
  let nextId = 1;
  const frames = new Map();
  const timers = new Map();
  return {
    frame(callback) {
      const id = nextId++;
      frames.set(id, callback);
      return id;
    },
    cancelFrame(id) { frames.delete(id); },
    delay(callback) {
      const id = nextId++;
      timers.set(id, callback);
      return id;
    },
    clearDelay(id) { timers.delete(id); },
    flushFrames() {
      for (const [id, callback] of [...frames]) {
        frames.delete(id);
        callback();
      }
    },
    flushTimers() {
      for (const [id, callback] of [...timers]) {
        timers.delete(id);
        callback();
      }
    },
    pending() { return { frames: frames.size, timers: timers.size }; },
  };
}

function createTransitionTarget() {
  const listeners = new Set();
  return {
    addEventListener(type, listener) {
      if (type === "transitionend") listeners.add(listener);
    },
    removeEventListener(type, listener) {
      if (type === "transitionend") listeners.delete(listener);
    },
    listenerCount() { return listeners.size; },
  };
}

function transitionOptions(scheduler) {
  return {
    requestFrame: scheduler.frame,
    cancelFrame: scheduler.cancelFrame,
    setDelay: scheduler.delay,
    clearDelay: scheduler.clearDelay,
  };
}

test("superseded Chat or Desk transition cancels a queued inner frame", () => {
  const scheduler = createScheduler();
  const first = createTransitionScope(transitionOptions(scheduler));

  first.frame(() => first.frame(() => {}));
  scheduler.flushFrames();
  assert.deepEqual(scheduler.pending(), { frames: 1, timers: 0 });
  first.dispose();
  assert.deepEqual(scheduler.pending(), { frames: 0, timers: 0 });
});

test("superseded Chat or Desk transition removes an active listener and timer", () => {
  const scheduler = createScheduler();
  const target = createTransitionTarget();
  const first = createTransitionScope(transitionOptions(scheduler));

  first.frame(() => first.frame(() => {
    first.listen(target, "transitionend", () => {});
    first.delay(() => {}, 640);
  }));
  scheduler.flushFrames();
  scheduler.flushFrames();
  assert.deepEqual(scheduler.pending(), { frames: 0, timers: 1 });
  assert.equal(target.listenerCount(), 1);
  first.dispose();
  assert.deepEqual(scheduler.pending(), { frames: 0, timers: 0 });
  assert.equal(target.listenerCount(), 0);

  const second = createTransitionScope(transitionOptions(scheduler));
  second.listen(target, "transitionend", () => {});
  second.delay(() => {}, 640);
  assert.deepEqual(scheduler.pending(), { frames: 0, timers: 1 });
  assert.equal(target.listenerCount(), 1);

  second.dispose();
  scheduler.flushTimers();
  assert.deepEqual(scheduler.pending(), { frames: 0, timers: 0 });
  assert.equal(target.listenerCount(), 0);
});

test("transition cleanup does not own navigation", () => {
  const workspace = readFileSync(new URL("../frontend/optchat/optchat.js", import.meta.url), "utf8");
  const scope = readFileSync(new URL("../frontend/shared/transition-scope.js", import.meta.url), "utf8");
  assert.equal((workspace.match(/history\.pushState/g) || []).length, 1);
  assert.match(workspace, /if \(updateHistory\) \{/);
  assert.doesNotMatch(scope, /history\.|location\./);
});
