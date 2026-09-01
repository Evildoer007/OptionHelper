/*
 * Vanilla ES module adapter for thinking-orbs 0.3.1.
 *
 * The canvas sizing, shared performance clock, preset resolution, DPR cap,
 * reduced-motion frame, IntersectionObserver pause and visibility pause are
 * adapted directly from the upstream ThinkingOrb React component.
 * Copyright (c) 2026 Jakub Antalik, MIT license.
 */
import { MODE_DRAWS, resolvePreset } from "/app/frontend/shared/thinking-orbs-engine.js";

const STATES = new Set([
  "working",
  "searching",
  "solving",
  "listening",
  "connecting",
  "weaving",
  "composing",
  "breathing",
  "shaping",
]);

const LABELS = Object.freeze({
  working: "正在处理",
  searching: "正在检索",
  solving: "正在分析",
  listening: "正在接收任务",
  connecting: "正在连接研究模块",
  weaving: "正在整合候选结果",
  composing: "正在整理答复",
  breathing: "正在等待下一步",
  shaping: "正在完成本轮处理",
});

function normalizeState(value) {
  return STATES.has(value) ? value : "working";
}

function ancestorTheme(element) {
  let node = element;
  while (node) {
    const theme = node.getAttribute?.("data-theme");
    if (theme === "dark") return true;
    if (theme === "light") return false;
    if (node.classList?.contains("dark")) return true;
    if (node.classList?.contains("light")) return false;
    node = node.parentElement;
  }
  return null;
}

function systemDark() {
  return typeof matchMedia === "undefined" || matchMedia("(prefers-color-scheme: dark)").matches;
}

export function createThinkingOrb({
  state = "working",
  size = 20,
  theme = "auto",
  speed = 1,
  paused = false,
  className = "thinking-orbs",
} = {}) {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const pixelSize = size === 64 ? 64 : 20;
  const pixelRatio = Math.min(2, globalThis.devicePixelRatio || 1);
  const themeMedia = globalThis.matchMedia?.("(prefers-color-scheme: dark)") || null;
  const motionMedia = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)") || null;

  let currentState = normalizeState(state);
  let currentPreset = resolvePreset(currentState, pixelSize);
  let dark = true;
  let reduced = Boolean(motionMedia?.matches);
  let visible = true;
  let suspended = Boolean(paused);
  let destroyed = false;
  let running = false;
  let animationFrame = 0;

  canvas.className = className;
  canvas.width = Math.round(pixelSize * pixelRatio);
  canvas.height = Math.round(pixelSize * pixelRatio);
  canvas.style.width = `${pixelSize}px`;
  canvas.style.height = `${pixelSize}px`;
  canvas.dataset.thinkingState = currentState;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", LABELS[currentState]);

  const resolveDark = () => {
    if (theme === "dark") return true;
    if (theme === "light") return false;
    return ancestorTheme(canvas) ?? systemDark();
  };

  const draw = (time = performance.now()) => {
    if (!context || destroyed) return;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, pixelSize, pixelSize);
    const { mode, speed: presetSpeed, opts } = currentPreset;
    MODE_DRAWS[mode](context, pixelSize, (time / 1000) * presetSpeed * speed, dark, opts);
  };

  const stop = () => {
    running = false;
    cancelAnimationFrame(animationFrame);
  };

  const loop = () => {
    draw();
    if (running) animationFrame = requestAnimationFrame(loop);
  };

  const canRun = () => (
    !destroyed
    && !suspended
    && !reduced
    && visible
    && document.visibilityState !== "hidden"
  );

  const start = () => {
    if (running || !canRun()) return;
    running = true;
    animationFrame = requestAnimationFrame(loop);
  };

  const syncPlayback = () => {
    stop();
    draw(reduced ? 600 : performance.now());
    start();
  };

  const syncTheme = () => {
    const next = resolveDark();
    if (next === dark) return;
    dark = next;
    draw();
  };

  const onThemeMediaChange = () => syncTheme();
  const onMotionChange = (event) => {
    reduced = event.matches;
    syncPlayback();
  };
  const onVisibilityChange = () => {
    if (document.visibilityState === "hidden") stop();
    else start();
  };

  themeMedia?.addEventListener("change", onThemeMediaChange);
  motionMedia?.addEventListener("change", onMotionChange);
  document.addEventListener("visibilitychange", onVisibilityChange);

  const themeObserver = typeof MutationObserver === "undefined"
    ? null
    : new MutationObserver(syncTheme);
  themeObserver?.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class", "data-theme"],
    subtree: true,
  });

  const intersectionObserver = typeof IntersectionObserver === "undefined"
    ? null
    : new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) start();
      else stop();
    });
  intersectionObserver?.observe(canvas);

  dark = resolveDark();
  draw(reduced ? 600 : performance.now());
  if (!intersectionObserver) start();

  return Object.freeze({
    element: canvas,
    setState(nextState) {
      const normalized = normalizeState(nextState);
      if (normalized === currentState) return;
      currentState = normalized;
      currentPreset = resolvePreset(currentState, pixelSize);
      canvas.dataset.thinkingState = currentState;
      canvas.setAttribute("aria-label", LABELS[currentState]);
      draw();
    },
    setPaused(nextPaused) {
      suspended = Boolean(nextPaused);
      syncPlayback();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stop();
      themeMedia?.removeEventListener("change", onThemeMediaChange);
      motionMedia?.removeEventListener("change", onMotionChange);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      themeObserver?.disconnect();
      intersectionObserver?.disconnect();
    },
  });
}
