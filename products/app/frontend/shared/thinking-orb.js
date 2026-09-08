/*
 * thinking-orbs 0.3.1, MIT © Jakub Antalik.
 * OH binding of the upstream geometry engine: one scheduler for every orb,
 * continuous state blending, eased settling, and automatic DOM lifecycle.
 * See LICENSES/ThinkingOrbs-SOURCE.md for the pinned source and build recipe.
 */
import { MODE_FRAMES, paintFrame, resolvePreset } from "/app/frontend/shared/thinking-orbs-engine.js";

const LABELS = Object.freeze({
  working: "正在处理", searching: "正在检索", solving: "正在思考",
  listening: "正在接收", connecting: "正在连接", weaving: "正在整合",
  composing: "正在生成答复", breathing: "正在等待", shaping: "正在完成",
});
// Geometry may vary within a real phase. The semantic label never changes
// merely because the presentation rotates to another upstream shape.
const VISUAL_SEQUENCES = Object.freeze({
  working: ['working','solving','weaving','shaping'],
  searching: ['searching','connecting','working'],
  solving: ['solving','working','shaping','weaving'],
  listening: ['listening','breathing'],
  connecting: ['connecting','listening','working'],
  weaving: ['weaving','shaping','composing'],
  composing: ['composing','listening','weaving'],
  breathing: ['breathing'],
  shaping: ['shaping'],
});
const VARIANT_INTERVAL = 3600;
const instances = new Map();
let frameRequest = 0;
let lastFrame = 0;
let observers = null;
const normalizeState = value => Object.hasOwn(LABELS, value) ? value : "working";
const media = query => globalThis.matchMedia?.(query);
const reducedMotion = () => Boolean((observers?.motion || media("(prefers-reduced-motion: reduce)"))?.matches);

function isDark(canvas, preference) {
  if (preference !== "auto") return preference === "dark";
  for (let node = canvas; node; node = node.parentElement) {
    const declared = node.getAttribute("data-theme");
    if (declared === "dark" || declared === "light") return declared === "dark";
    if (node.classList.contains("dark")) return true;
    if (node.classList.contains("light")) return false;
  }
  return Boolean(media("(prefers-color-scheme: dark)")?.matches);
}

function schedule() {
  if (!frameRequest && document.visibilityState !== "hidden") frameRequest = requestAnimationFrame(tick);
}

function tick(now) {
  frameRequest = 0;
  const elapsed = lastFrame ? Math.min(50, now - lastFrame) : 16.67;
  lastFrame = now;
  let more = false;
  for (const instance of instances.values()) more = instance.advance(elapsed) || more;
  if (more) schedule();
  else lastFrame = 0;
}

function observe() {
  if (observers) return;
  const invalidate = () => { for (const item of instances.values()) item.invalidate(); schedule(); };
  const visibility = () => {
    if (document.visibilityState === "hidden") { cancelAnimationFrame(frameRequest); frameRequest = 0; lastFrame = 0; }
    else invalidate();
  };
  const intersection = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
    for (const entry of entries) instances.get(entry.target)?.setVisible(entry.isIntersecting);
    schedule();
  });
  const mutation = new MutationObserver(() => {
    for (const item of instances.values()) {
      if (item.element.isConnected) { item.mounted = true; item.invalidate(); }
      else if (item.mounted) item.destroy();
    }
    schedule();
  });
  mutation.observe(document.documentElement, {childList: true, subtree: true, attributes: true, attributeFilter: ["data-theme", "class"]});
  const theme = media("(prefers-color-scheme: dark)");
  const motion = media("(prefers-reduced-motion: reduce)");
  theme?.addEventListener("change", invalidate);
  motion?.addEventListener("change", invalidate);
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("resize", invalidate);
  observers = {intersection, mutation, motion, cleanup() {
    intersection?.disconnect(); mutation.disconnect();
    theme?.removeEventListener("change", invalidate); motion?.removeEventListener("change", invalidate);
    document.removeEventListener("visibilitychange", visibility); window.removeEventListener("resize", invalidate);
  }};
}

export function createThinkingOrb({state = "working", size = 20, theme = "auto", speed = 1, paused = false, className = "thinking-orbs"} = {}) {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const pixels = Math.max(20, Math.min(64, Number(size) || 20));
  // Full sphere geometry at chat size, purpose-tuned sparse geometry only inline.
  const presetSize = pixels <= 20 ? 20 : 64;
  const multiplier = Math.max(.1, Math.min(3, Number(speed) || 1));
  let currentState = normalizeState(state);
  let currentVisual = currentState;
  let visualIndex = 0;
  let visualElapsed = 0;
  let suspended = Boolean(paused);
  let visible = true;
  let disposed = false;
  let dirty = true;
  let velocity = suspended ? 0 : 1;
  let time = performance.now() / 1000;
  let weights = new Map([[currentState, 1]]);
  canvas.className = className;
  canvas.style.width = `${pixels}px`;
  canvas.style.height = `${pixels}px`;
  canvas.dataset.thinkingState = currentState;
  canvas.dataset.thinkingVisual = currentVisual;
  canvas.dataset.renderMode = context ? "canvas" : "static";
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", LABELS[currentState]);

  function selectVisual(next) {
    currentVisual = next;
    if (!weights.has(next)) weights.set(next, 0);
    canvas.dataset.thinkingVisual = next;
    dirty = true;
  }

  function paint(reduced) {
    if (!context) return;
    const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    const resolution = Math.round(pixels * dpr);
    if (canvas.width !== resolution || canvas.height !== resolution) { canvas.width = resolution; canvas.height = resolution; }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, pixels, pixels);
    const dark = isDark(canvas, theme);
    for (const [name, weight] of weights) {
      if (weight < .001) continue;
      const {mode, speed: baseSpeed, opts} = resolvePreset(name, presetSize);
      // Keep the upstream depth sorting, ink weights, projection and nine geometries.
      const frame = MODE_FRAMES[mode](pixels, (reduced ? .6 : time * baseSpeed * multiplier), opts);
      context.globalAlpha = weight;
      paintFrame(context, frame, dark);
    }
    context.globalAlpha = 1;
    dirty = false;
  }

  const instance = {
    element: canvas, mounted: false,
    invalidate() { dirty = true; },
    setVisible(value) { visible = value; dirty = true; },
    advance(elapsed) {
      if (disposed || !canvas.isConnected || !visible || !context) return false;
      instance.mounted = true;
      const reduced = reducedMotion();
      if (reduced) {
        currentVisual = currentState;
        visualIndex = 0; visualElapsed = 0;
        canvas.dataset.thinkingVisual = currentState;
        weights = new Map([[currentState, 1]]);
        if (dirty) paint(true);
        canvas.dataset.playback = "reduced"; return false;
      }
      if (!suspended) {
        visualElapsed += elapsed;
        const sequence = VISUAL_SEQUENCES[currentState];
        if (sequence.length > 1 && visualElapsed >= VARIANT_INTERVAL) {
          visualElapsed %= VARIANT_INTERVAL;
          visualIndex = (visualIndex + 1) % sequence.length;
          selectVisual(sequence[visualIndex]);
        }
      }
      const targetVelocity = suspended ? 0 : 1;
      velocity += (targetVelocity - velocity) * (1 - Math.exp(-elapsed / 100));
      if (Math.abs(targetVelocity - velocity) < .001) velocity = targetVelocity;
      time += elapsed / 1000 * velocity;
      let transitioning = false;
      const blend = 1 - Math.exp(-elapsed / 85);
      for (const [name, weight] of weights) {
        const target = name === currentVisual ? 1 : 0;
        const next = weight + (target - weight) * blend;
        if (Math.abs(target - next) < .001) { if (target) weights.set(name, 1); else weights.delete(name); }
        else { weights.set(name, next); transitioning = true; }
      }
      const moving = velocity > 0 || transitioning;
      if (dirty || moving) paint(false);
      canvas.dataset.playback = moving ? "running" : "paused";
      return moving;
    },
    destroy() {
      if (disposed) return;
      disposed = true;
      observers?.intersection?.unobserve(canvas);
      instances.delete(canvas);
      canvas.dataset.playback = "disposed";
      if (!instances.size) {
        observers?.cleanup(); observers = null;
        cancelAnimationFrame(frameRequest); frameRequest = 0; lastFrame = 0;
      }
    },
  };
  instances.set(canvas, instance);
  observe();
  observers.intersection?.observe(canvas);
  paint(reducedMotion());
  schedule();
  return Object.freeze({element: canvas,
    setState(next) {
      if (disposed) return;
      next = normalizeState(next);
      if (next === currentState) return;
      currentState = next;
      visualIndex = 0; visualElapsed = 0;
      selectVisual(next);
      canvas.dataset.thinkingState = next;
      canvas.setAttribute("aria-label", LABELS[next]);
      dirty = true;
      schedule();
    },
    setPaused(next) { if (!disposed && suspended !== Boolean(next)) { suspended = Boolean(next); dirty = true; schedule(); } },
    destroy: instance.destroy,
  });
}
