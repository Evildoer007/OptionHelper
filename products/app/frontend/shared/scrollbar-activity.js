export const SCROLLBAR_TIMING = Object.freeze({ holdMs: 1500, fadeMs: 450, wakeMs: 90 });

const SHELL_SCROLL_CONTAINERS = Object.freeze([
  ".task-rail",
  ".message-stream",
  ".task-context",
  ".assistant-panel__transcript",
  ".choice-menu",
  ".composer textarea",
  ".connection-editor",
  ".settings-options",
  ".desk-module-tabs",
]);

const INSTANCE_BY_DOCUMENT = new WeakMap();

export function createScrollbarVisibilityController({
  onShow,
  onHide,
  holdMs = SCROLLBAR_TIMING.holdMs,
  schedule = window.setTimeout.bind(window),
  cancel = window.clearTimeout.bind(window),
}) {
  let holdTimer = null;

  const clear = () => {
    if (holdTimer !== null) cancel(holdTimer);
    holdTimer = null;
  };

  return Object.freeze({
    wake() {
      clear();
      onShow();
      holdTimer = schedule(() => {
        holdTimer = null;
        onHide();
      }, holdMs);
    },
    hide() {
      clear();
      onHide();
    },
    dispose: clear,
  });
}

function createOverlayLayer(documentRoot) {
  const host = documentRoot.createElement("div");
  host.id = "optionhelper-scrollbar-layer";
  const hostStyle = {
    position: "fixed",
    top: "0",
    right: "0",
    bottom: "0",
    left: "0",
    display: "block",
    margin: "0",
    padding: "0",
    border: "0",
    width: "auto",
    height: "auto",
    overflow: "visible",
    pointerEvents: "none",
    zIndex: "2147483000",
  };
  host.style.setProperty("all", "initial", "important");
  Object.entries(hostStyle).forEach(([property, value]) => {
    host.style.setProperty(property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`), value, "important");
  });
  host.setAttribute("aria-hidden", "true");
  documentRoot.body.appendChild(host);
  return host;
}

function setStyle(element, values) {
  Object.entries(values).forEach(([property, value]) => {
    element.style.setProperty(property, String(value), "important");
  });
}

function intersectRect(rect, boundary, clipX, clipY) {
  return {
    top: clipY ? Math.max(rect.top, boundary.top) : rect.top,
    right: clipX ? Math.min(rect.right, boundary.right) : rect.right,
    bottom: clipY ? Math.min(rect.bottom, boundary.bottom) : rect.bottom,
    left: clipX ? Math.max(rect.left, boundary.left) : rect.left,
  };
}

function visibleRect(element) {
  let rect = intersectRect(
    element.getBoundingClientRect(),
    { top: 0, right: window.innerWidth, bottom: window.innerHeight, left: 0 },
    true,
    true,
  );

  for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
    const style = getComputedStyle(parent);
    const clipX = /auto|scroll|hidden|clip/.test(style.overflowX);
    const clipY = /auto|scroll|hidden|clip/.test(style.overflowY);
    if (clipX || clipY) rect = intersectRect(rect, parent.getBoundingClientRect(), clipX, clipY);
  }
  return rect;
}

function scrollableOnAxis(element, axis, documentScroller) {
  const overflow = getComputedStyle(element)[axis === "y" ? "overflowY" : "overflowX"];
  const allowed = element === documentScroller || /auto|scroll|overlay/.test(overflow);
  const client = axis === "y" ? element.clientHeight : element.clientWidth;
  const scroll = axis === "y" ? element.scrollHeight : element.scrollWidth;
  return allowed && scroll > client + 1;
}

function scrollbarSize(element) {
  const sizeSource = element === document.scrollingElement && document.body ? document.body : element;
  const style = getComputedStyle(sizeSource);
  return Number.parseFloat(style.getPropertyValue("--scrollbar-size"))
    || Number.parseFloat(style.getPropertyValue("--oh-scrollbar-size"))
    || 6;
}

function scrollbarInk(element) {
  const colorSource = element === document.scrollingElement && document.body ? document.body : element;
  return getComputedStyle(colorSource).color || "rgb(37, 40, 43)";
}

export function installScrollbarActivity({ selectors = SHELL_SCROLL_CONTAINERS, includeDocument = true } = {}) {
  if (typeof document === "undefined") return null;
  const existing = INSTANCE_BY_DOCUMENT.get(document);
  if (existing) return existing;

  const overlayLayer = createOverlayLayer(document);
  const selectorList = [...new Set(selectors.filter(Boolean))];
  const selectorText = selectorList.join(",");
  const documentScroller = document.scrollingElement;
  const states = new WeakMap();
  const tracked = new Set();
  let renderFrame = null;
  let mutationObserver = null;
  let resizeObserver = null;
  let drag = null;

  const setActive = (state, active) => {
    state.thumbs.forEach((thumb) => {
      if (!thumb) return;
      setStyle(thumb, {
        opacity: active ? "1" : "0",
        "pointer-events": active ? "auto" : "none",
        transition: `opacity ${active ? SCROLLBAR_TIMING.wakeMs : SCROLLBAR_TIMING.fadeMs}ms ${active ? "ease-out" : "ease"}`,
      });
    });
  };

  const removeState = (state) => {
    if (!state) return;
    state.controller.dispose();
    state.element.removeEventListener("scroll", state.onScroll);
    delete state.element.dataset.ohScrollbarOverlayReady;
    resizeObserver?.unobserve(state.element);
    state.thumbs.forEach((thumb) => thumb?.remove());
    tracked.delete(state.element);
  };

  const createThumb = (state, axis) => {
    const thumb = document.createElement("div");
    thumb.className = "oh-scrollbar-thumb";
    thumb.dataset.ohScrollbarAxis = axis;
    thumb.setAttribute("aria-hidden", "true");
    thumb.addEventListener("pointerdown", (event) => {
      const metrics = state.metrics[axis];
      if (!metrics || metrics.maxThumbOffset <= 0) return;
      event.preventDefault();
      event.stopPropagation();
      thumb.style.setProperty("cursor", "default", "important");
      thumb.setPointerCapture?.(event.pointerId);
      drag = {
        axis,
        element: state.element,
        pointerId: event.pointerId,
        startPosition: axis === "y" ? event.clientY : event.clientX,
        startScroll: axis === "y" ? state.element.scrollTop : state.element.scrollLeft,
        scrollRange: metrics.scrollRange,
        maxThumbOffset: metrics.maxThumbOffset,
        thumb,
      };
      state.controller.wake();
    });
    setStyle(thumb, {
      position: "fixed",
      "z-index": "2147483001",
      display: "none",
      "box-sizing": "border-box",
      "border-radius": "999px",
      background: "rgba(81, 77, 73, .34)",
      "box-shadow": "inset 0 0 0 1px rgba(255, 255, 255, .22)",
      "backdrop-filter": "blur(5px) saturate(112%)",
      "-webkit-backdrop-filter": "blur(5px) saturate(112%)",
      opacity: "0",
      "pointer-events": "none",
      "touch-action": "none",
      "user-select": "none",
      transition: `opacity ${SCROLLBAR_TIMING.fadeMs}ms ease`,
    });
    overlayLayer.appendChild(thumb);
    state.thumbs.set(axis, thumb);
    return thumb;
  };

  const renderAxis = (state, axis, rect, otherAxisScrollable) => {
    const element = state.element;
    const isScrollable = scrollableOnAxis(element, axis, documentScroller);
    let thumb = state.thumbs.get(axis);
    if (!isScrollable || rect.right <= rect.left || rect.bottom <= rect.top) {
      if (thumb) thumb.style.display = "none";
      state.metrics[axis] = null;
      return;
    }

    thumb ||= createThumb(state, axis);
    const size = scrollbarSize(element);
    const edge = 2;
    const clientLength = axis === "y" ? element.clientHeight : element.clientWidth;
    const scrollLength = axis === "y" ? element.scrollHeight : element.scrollWidth;
    const scrollPosition = axis === "y" ? element.scrollTop : element.scrollLeft;
    const visibleLength = axis === "y" ? rect.bottom - rect.top : rect.right - rect.left;
    const trackLength = Math.max(0, visibleLength - edge * 2 - (otherAxisScrollable ? size : 0));
    const thumbLength = Math.min(trackLength, Math.max(28, trackLength * (clientLength / scrollLength)));
    const maxThumbOffset = Math.max(0, trackLength - thumbLength);
    const scrollRange = Math.max(1, scrollLength - clientLength);
    const thumbOffset = maxThumbOffset * Math.min(1, Math.max(0, scrollPosition / scrollRange));
    const ink = scrollbarInk(element);

    thumb.style.setProperty("display", trackLength > 0 ? "block" : "none", "important");
    thumb.style.setProperty("background", `color-mix(in srgb, ${ink} 34%, transparent)`, "important");
    if (axis === "y") {
      setStyle(thumb, {
        left: `${Math.round(rect.right - size - 1)}px`,
        top: `${Math.round(rect.top + edge + thumbOffset)}px`,
        width: `${size}px`,
        height: `${Math.round(thumbLength)}px`,
      });
    } else {
      setStyle(thumb, {
        left: `${Math.round(rect.left + edge + thumbOffset)}px`,
        top: `${Math.round(rect.bottom - size - 1)}px`,
        width: `${Math.round(thumbLength)}px`,
        height: `${size}px`,
      });
    }
    if (thumb.isConnected && trackLength > 0) {
      element.dataset.ohScrollbarOverlayReady = "true";
    }
    state.metrics[axis] = { maxThumbOffset, scrollRange };
  };

  const renderState = (state) => {
    if (!state.element.isConnected) {
      removeState(state);
      return;
    }
    const rect = visibleRect(state.element);
    const scrollY = scrollableOnAxis(state.element, "y", documentScroller);
    const scrollX = scrollableOnAxis(state.element, "x", documentScroller);
    renderAxis(state, "y", rect, scrollX);
    renderAxis(state, "x", rect, scrollY);
  };

  const renderAll = () => {
    renderFrame = null;
    [...tracked].forEach((element) => {
      const state = states.get(element);
      if (state) renderState(state);
    });
  };

  const queueRender = () => {
    if (renderFrame !== null) return;
    renderFrame = window.requestAnimationFrame(renderAll);
  };

  const register = (element) => {
    if (!(element instanceof Element) || states.has(element)) return states.get(element);
    const state = {
      element,
      metrics: { x: null, y: null },
      thumbs: new Map(),
      controller: null,
      onScroll: null,
    };
    state.controller = createScrollbarVisibilityController({
      onShow: () => setActive(state, true),
      onHide: () => setActive(state, false),
    });
    state.onScroll = () => {
      renderState(state);
      state.controller.wake();
      queueRender();
    };
    element.addEventListener("scroll", state.onScroll, { passive: true });
    // The native pseudo-element is not reliably composited in WKWebView
    // if we wait for a first paint measurement. Once a real scroll surface
    // has a controller, its overlay owns the visual state from the outset.
    element.dataset.ohScrollbarOverlayReady = "true";
    states.set(element, state);
    tracked.add(element);
    resizeObserver?.observe(element);
    return state;
  };

  const registerIfScrollable = (element) => {
    if (!(element instanceof Element) || element === document.body || element === document.documentElement) return;
    const isKnownSurface = Boolean(selectorText) && element.matches(selectorText);
    if (isKnownSurface
      || scrollableOnAxis(element, "y", documentScroller)
      || scrollableOnAxis(element, "x", documentScroller)) {
      register(element);
    }
  };

  const scan = (root) => {
    if (!(root instanceof Element)) return;
    registerIfScrollable(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) registerIfScrollable(walker.currentNode);
  };

  const scanScrollParents = (node) => {
    for (let parent = node instanceof Element ? node : node.parentElement;
      parent && parent !== document.documentElement;
      parent = parent.parentElement) {
      registerIfScrollable(parent);
    }
  };

  const onPointerMove = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const position = drag.axis === "y" ? event.clientY : event.clientX;
    const delta = position - drag.startPosition;
    const next = drag.startScroll + (delta / drag.maxThumbOffset) * drag.scrollRange;
    if (drag.axis === "y") drag.element.scrollTop = next;
    else drag.element.scrollLeft = next;
  };

  const onPointerUp = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const state = states.get(drag.element);
    drag.thumb.style.removeProperty("cursor");
    drag = null;
    state?.controller.wake();
  };

  const teardown = () => {
    document.removeEventListener("pointermove", onPointerMove, true);
    document.removeEventListener("pointerup", onPointerUp, true);
    document.removeEventListener("pointercancel", onPointerUp, true);
    document.removeEventListener("optionhelper:themechange", queueRender);
    window.removeEventListener("resize", queueRender);
    mutationObserver?.disconnect();
    resizeObserver?.disconnect();
    if (renderFrame !== null) window.cancelAnimationFrame(renderFrame);
    [...tracked].forEach((element) => removeState(states.get(element)));
    overlayLayer.remove();
    document.documentElement.removeAttribute("data-oh-scrollbar-overlay");
    INSTANCE_BY_DOCUMENT.delete(document);
  };

  resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(queueRender) : null;
  mutationObserver = new MutationObserver((records) => {
    records.forEach((record) => {
      scanScrollParents(record.target);
      record.addedNodes.forEach((node) => scan(node));
    });
    queueRender();
  });
  mutationObserver.observe(document.documentElement, { childList: true, subtree: true });

  document.documentElement.dataset.ohScrollbarOverlay = "enabled";
  document.addEventListener("pointermove", onPointerMove, { capture: true, passive: true });
  document.addEventListener("pointerup", onPointerUp, true);
  document.addEventListener("pointercancel", onPointerUp, true);
  document.addEventListener("optionhelper:themechange", queueRender);
  window.addEventListener("resize", queueRender, { passive: true });
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) teardown();
  }, { once: true });

  if (includeDocument && documentScroller) register(documentScroller);
  scan(document.body);
  queueRender();

  const instance = Object.freeze({ teardown });
  INSTANCE_BY_DOCUMENT.set(document, instance);
  return instance;
}

function autoInstall() {
  if (document.documentElement.dataset.optionhelperAppHosted === "true") return;
  installScrollbarActivity({ includeDocument: !document.body?.classList.contains("workspace-body") });
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoInstall, { once: true });
  } else {
    autoInstall();
  }
}
