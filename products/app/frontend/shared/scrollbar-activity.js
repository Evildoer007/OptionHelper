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
    if (element.style.getPropertyValue(property) !== String(value)) {
      element.style.setProperty(property, String(value), "important");
    }
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


function insideClosedDisclosure(element) {
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === "DETAILS" && (!ancestor.open || ancestor.dataset.motionTarget === "closed")) {
      const summary = [...ancestor.children].find(child => child.tagName === "SUMMARY");
      if (!summary?.contains(element)) return true;
    }
  }
  return false;
}

export function scrollbarTrackVisible(element, axis, rect) {
  if (!element.isConnected || insideClosedDisclosure(element) || !element.getClientRects().length) return false;
  const style = getComputedStyle(element);
  if (style.visibility === "hidden" || style.visibility === "collapse") return false;
  const bounds = element.getBoundingClientRect();
  // A clipped-away edge has no visible track. Never relocate its thumb into
  // the middle of the ancestor that clipped it.
  return axis === "y" ? bounds.right <= rect.right + 1 : bounds.bottom <= rect.bottom + 1;
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
  let disposed = false;
  const pendingRoots = new Set();
  let discoveryWalker = null;
  // Bound discovery per frame so a large restored conversation yields to input.
  const discoveryBatchSize = 160;

  const setActive = (state, active) => {
    state.active = active;
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
    states.delete(state.element);
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
      "box-shadow": "none",
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
    if (state.active) setActive(state, true);
    return thumb;
  };

  // Geometry reads are completed for every surface before any overlay write.
  const measureAxis = (state, axis, rect, style, scrollable, otherScrollable) => {
    const element = state.element;
    if (!scrollable || !scrollbarTrackVisible(element, axis, rect)) return null;
    const size = scrollbarSize(element);
    const rounded = element.matches(".choice-menu, .oh-choice__menu");
    const radius = rounded ? Math.max(...[
      style.borderTopLeftRadius, style.borderTopRightRadius,
      style.borderBottomLeftRadius, style.borderBottomRightRadius,
    ].map(value => Number.parseFloat(value) || 0)) : 0;
    const edge = rounded ? Math.max(6, radius) : 2;
    const crossInset = rounded ? 5 : 1;
    const clientLength = axis === "y" ? element.clientHeight : element.clientWidth;
    const scrollLength = axis === "y" ? element.scrollHeight : element.scrollWidth;
    const position = axis === "y" ? element.scrollTop : element.scrollLeft;
    const visibleLength = axis === "y" ? rect.bottom - rect.top : rect.right - rect.left;
    const trackLength = Math.max(0, visibleLength - edge * 2 - (otherScrollable ? size : 0));
    if (!trackLength) return null;
    const thumbLength = Math.min(trackLength, Math.max(28, trackLength * clientLength / scrollLength));
    const maxThumbOffset = Math.max(0, trackLength - thumbLength);
    const scrollRange = Math.max(1, scrollLength - clientLength);
    const offset = maxThumbOffset * Math.min(1, Math.max(0, position / scrollRange));
    return { metrics: { maxThumbOffset, scrollRange }, styles: {
      display: "block", background: `color-mix(in srgb, ${scrollbarInk(element)} 34%, transparent)`,
      left: `${Math.round(axis === "y" ? rect.right - size - crossInset : rect.left + edge + offset)}px`,
      top: `${Math.round(axis === "y" ? rect.top + edge + offset : rect.bottom - size - crossInset)}px`,
      width: `${axis === "y" ? size : Math.round(thumbLength)}px`,
      height: `${axis === "y" ? Math.round(thumbLength) : size}px`,
    }};
  };

  const measureState = (state) => {
    const element = state.element;
    if (!element.isConnected) return {state, detached: true};
    if (insideClosedDisclosure(element) || !element.getClientRects().length) return {state, x: null, y: null};
    const rect = visibleRect(element);
    if (rect.right <= rect.left || rect.bottom <= rect.top) return {state, x: null, y: null};
    const style = getComputedStyle(element);
    const scrollY = (element === documentScroller || /auto|scroll|overlay/.test(style.overflowY))
      && element.scrollHeight > element.clientHeight + 1;
    const scrollX = (element === documentScroller || /auto|scroll|overlay/.test(style.overflowX))
      && element.scrollWidth > element.clientWidth + 1;
    return {state,
      y: measureAxis(state, "y", rect, style, scrollY, scrollX),
      x: measureAxis(state, "x", rect, style, scrollX, scrollY),
    };
  };

  const applyState = (measurement) => {
    const {state} = measurement;
    if (measurement.detached) { removeState(state); return; }
    for (const axis of ["x", "y"]) {
      const next = measurement[axis];
      state.metrics[axis] = next?.metrics || null;
      if (next) setStyle(state.thumbs.get(axis) || createThumb(state, axis), next.styles);
      else if (state.thumbs.has(axis)) setStyle(state.thumbs.get(axis), {display: "none"});
    }
    if (state.element.dataset.ohScrollbarOverlayReady !== "true") state.element.dataset.ohScrollbarOverlayReady = "true";
  };

  const renderAll = () => {
    renderFrame = null;
    if (disposed) return;
    discover();
    const measurements = [...tracked].map(element => measureState(states.get(element)));
    measurements.forEach(applyState);
    if (pendingRoots.size || discoveryWalker) queueRender();
  };

  const queueRender = () => {
    if (disposed || renderFrame !== null) return;
    renderFrame = window.requestAnimationFrame(renderAll);
  };

  const onDisclosureToggle = (event) => {
    if (event.target?.tagName !== "DETAILS") return;
    enqueueScan(event.target);
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
      state.controller.wake();
      queueRender();
    };
    element.addEventListener("scroll", state.onScroll, { passive: true });
    // The native pseudo-element is not reliably composited in WKWebView
    // if we wait for a first paint measurement. Once a real scroll surface
    // has a controller, its overlay owns the visual state from the outset.
    states.set(element, state);
    tracked.add(element);
    resizeObserver?.observe(element);
    return state;
  };

  const ignored = element => element === overlayLayer || overlayLayer.contains(element);
  const registerIfScrollable = (element) => {
    if (!(element instanceof Element) || states.has(element) || ignored(element)
      || element === document.body || element === document.documentElement) return;
    if (selectorText && element.matches(selectorText)) { register(element); return; }
    // Discover scroll surfaces from CSS; ordinary prose never needs layout dimensions.
    const style = getComputedStyle(element);
    if (/auto|scroll|overlay/.test(style.overflowY) || /auto|scroll|overlay/.test(style.overflowX)) register(element);
  };

  const enqueueScan = root => {
    if (!(root instanceof Element) || ignored(root)) return;
    // Coalesce nested mutations before visiting their subtrees.
    for (const pending of pendingRoots) {
      if (pending.contains(root)) { queueRender(); return; }
      if (root.contains(pending)) pendingRoots.delete(pending);
    }
    pendingRoots.add(root);
    queueRender();
  };

  const discover = () => {
    const started = performance.now();
    let visited = 0;
    while (visited < discoveryBatchSize && performance.now() - started < 5) {
      if (!discoveryWalker) {
        const root = pendingRoots.values().next().value;
        if (!root) break;
        pendingRoots.delete(root);
        if (!root.isConnected || ignored(root)) continue;
        registerIfScrollable(root);
        visited += 1;
        discoveryWalker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, {
          acceptNode: element => ignored(element) || element.hidden
            ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
        });
      }
      if (!discoveryWalker.root.isConnected) { discoveryWalker = null; continue; }
      const element = discoveryWalker.nextNode();
      if (!element) { discoveryWalker = null; continue; }
      registerIfScrollable(element);
      visited += 1;
    }
  };

  // A surface can become scrollable after a class/style change or lazy insertion.
  const onCapturedScroll = event => {
    const element = event.target;
    if (!(element instanceof Element) || ignored(element)) return;
    const state = states.get(element) || register(element);
    state?.controller.wake();
    queueRender();
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
    disposed = true;
    pendingRoots.clear();
    discoveryWalker = null;
    document.removeEventListener("scroll", onCapturedScroll, true);
    document.removeEventListener("pointermove", onPointerMove, true);
    document.removeEventListener("pointerup", onPointerUp, true);
    document.removeEventListener("pointercancel", onPointerUp, true);
    document.removeEventListener("optionhelper:themechange", queueRender);
    document.removeEventListener("toggle", onDisclosureToggle, true);
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
    // No geometry reads or DOM writes in the mutation microtask.
    let relevant = false;
    for (const record of records) {
      if (ignored(record.target)) continue;
      relevant = true;
      if (record.type === "attributes") enqueueScan(record.target);
      else for (const node of record.addedNodes) enqueueScan(node);
    }
    if (relevant) queueRender();
  });
  mutationObserver.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["open", "hidden", "class", "data-motion-target"],
  });
  document.addEventListener("scroll", onCapturedScroll, {capture: true, passive: true});

  document.documentElement.dataset.ohScrollbarOverlay = "enabled";
  document.addEventListener("pointermove", onPointerMove, { capture: true, passive: true });
  document.addEventListener("pointerup", onPointerUp, true);
  document.addEventListener("pointercancel", onPointerUp, true);
  document.addEventListener("optionhelper:themechange", queueRender);
  document.addEventListener("toggle", onDisclosureToggle, true);
  window.addEventListener("resize", queueRender, { passive: true });
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) teardown();
  }, { once: true });

  if (includeDocument && documentScroller) register(documentScroller);
  enqueueScan(document.body);
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
