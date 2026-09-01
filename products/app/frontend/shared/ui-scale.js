const SCALE_STEPS = Object.freeze([0.8, 0.9, 1, 1.1, 1.25, 1.4]);
const STORAGE_KEY = "optionhelper.ui-scale";
const root = document.documentElement;

let currentScale = 1;
let resizeFrame = 0;

function supportedScale(value) {
  const numeric = Number(value);
  return SCALE_STEPS.find((step) => Math.abs(step - numeric) < 0.0001) ?? null;
}

function nativeShell() {
  return root.dataset.nativeShell === "macos" || root.dataset.nativeShell === "windows";
}

function storedScale() {
  try { return supportedScale(localStorage.getItem(STORAGE_KEY)); }
  catch { return null; }
}

function persistBrowserScale(scale) {
  try { localStorage.setItem(STORAGE_KEY, String(scale)); }
  catch { /* Private previews may not expose local storage. */ }
}

function syncNativeTitlebarClearance(scale) {
  if (root.dataset.nativeShell !== "macos") return;
  root.style.setProperty("--native-titlebar-height", `${36 / scale}px`);
  root.style.setProperty("--native-titlebar-collapsed-leading-safe-area", `${166 / scale}px`);
}

function syncFrame(frame, scale = currentScale) {
  const accepted = supportedScale(scale);
  if (!frame?.contentWindow || accepted === null) return false;
  frame.contentWindow.postMessage({
    type: "optionhelper.ui-scale",
    scale: accepted,
    bridge_nonce: frame.dataset.bridgeNonce || undefined,
  }, location.origin);
  return true;
}

function broadcastScale(scale) {
  document.querySelectorAll("iframe").forEach((frame) => syncFrame(frame, scale));
}

function queueResize(scale) {
  cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
      broadcastScale(scale);
    });
  });
}

function applyScale(value, {persist = false, announce = true} = {}) {
  const scale = supportedScale(value) ?? 1;
  const changed = currentScale !== scale;
  currentScale = scale;
  root.dataset.uiScale = String(scale);
  root.dataset.uiScalePercent = String(Math.round(scale * 100));
  syncNativeTitlebarClearance(scale);

  if (nativeShell()) root.style.removeProperty("zoom");
  else {
    root.style.zoom = String(scale);
    if (persist) persistBrowserScale(scale);
  }

  if (changed || announce) {
    document.dispatchEvent(new CustomEvent("optionhelper:uiscalechange", {detail: {scale}}));
    queueResize(scale);
  }
  return scale;
}

function postNativeScale(scale) {
  const payload = {type: "set_ui_scale", scale};
  if (root.dataset.nativeShell === "macos") {
    window.webkit?.messageHandlers?.optionhelperUIScale?.postMessage(payload);
    return true;
  }
  if (root.dataset.nativeShell === "windows") {
    window.chrome?.webview?.postMessage(payload);
    return true;
  }
  return false;
}

function setScale(value) {
  const scale = supportedScale(value);
  if (scale === null) return currentScale;
  if (postNativeScale(scale)) return scale;
  return applyScale(scale, {persist: true});
}

function stepScale(direction) {
  const index = Math.max(0, SCALE_STEPS.indexOf(currentScale));
  const next = Math.max(0, Math.min(SCALE_STEPS.length - 1, index + direction));
  return setScale(SCALE_STEPS[next]);
}

const initialScale = supportedScale(root.dataset.uiScale) ?? storedScale() ?? 1;
applyScale(initialScale, {announce: false});

window.OptionHelperUIScale = Object.freeze({
  steps: SCALE_STEPS,
  current: () => currentScale,
  set: setScale,
  increase: () => stepScale(1),
  decrease: () => stepScale(-1),
  reset: () => setScale(1),
  syncFromNative: (scale) => applyScale(scale),
  syncFrame,
});

export { SCALE_STEPS, applyScale, setScale, supportedScale };
