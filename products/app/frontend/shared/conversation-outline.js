let outlineSequence = 0;

/** Lightweight navigation over the current stream. No message text is persisted. */
export function createConversationOutline({ stream, container }) {
  if (!stream || !container || stream === container || stream.contains(container)) {
    throw new TypeError("历史导航容器必须位于消息流外部");
  }
  const doc = stream.ownerDocument;
  const win = doc.defaultView;
  const stylesheet = "/app/frontend/shared/conversation-outline.css";
  if (![...doc.querySelectorAll('link[rel="stylesheet"]')].some(link => link.getAttribute("href") === stylesheet)) {
    const link = doc.createElement("link");
    link.rel = "stylesheet";
    link.href = stylesheet;
    doc.head.append(link);
  }
  const ownsHostClass = !container.classList.contains("conversation-outline-host");
  container.classList.add("conversation-outline-host");
  const nav = doc.createElement("nav");
  nav.className = "conversation-outline";
  nav.setAttribute("aria-label", "对话历史");
  nav.hidden = true;
  const list = doc.createElement("div");
  list.className = "conversation-outline__ticks";
  const preview = doc.createElement("div");
  preview.className = "conversation-outline__preview";
  preview.id = `conversation-outline-preview-${++outlineSequence}`;
  preview.setAttribute("role", "region");
  preview.setAttribute("aria-label", "消息预览");
  preview.hidden = true;
  const count = doc.createElement("span");
  count.className = "conversation-outline__count";
  const question = doc.createElement("strong");
  const answer = doc.createElement("p");
  const jump = doc.createElement("button");
  jump.type = "button";
  jump.textContent = "定位此消息";
  preview.append(count, question, answer, jump);
  const marks = doc.createElement("div");
  marks.className = "conversation-outline__marks";
  marks.setAttribute("aria-hidden", "true");
  list.append(marks);
  nav.append(list, preview);
  container.append(nav);

  let entries = [];
  let selected = null;
  let destroyed = false;
  let refreshTimer = 0;
  let previewTimer = 0;
  let scrollFrame = 0;
  let narrow = false;
  const buttons = new Map();
  let rulerMarks = [];
  let previewPosition = null;
  const targets = new WeakMap();
  const listeners = [];
  const listen = (target, type, handler, options) => {
    target.addEventListener(type, handler, options);
    listeners.push(() => target.removeEventListener(type, handler, options));
  };
  const forbidden = '.assistant-reasoning, .message--process, .message__actions, [hidden], [aria-hidden="true"], [inert], script, style';
  const visible = element => {
    if (element.closest(forbidden)) return false;
    for (let parent = element; parent && parent !== stream; parent = parent.parentElement) {
      const style = win.getComputedStyle(parent);
      if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return false;
    }
    return true;
  };
  // Walk only public body text, with a hard preview limit even for very long replies.
  const bodyText = message => {
    let text = "";
    for (const body of message.querySelectorAll(".message__body")) {
      if (body.closest(".message") !== message || !visible(body)) continue;
      const walker = doc.createTreeWalker(body, win.NodeFilter.SHOW_TEXT, {
        acceptNode: node => visible(node.parentElement) ? win.NodeFilter.FILTER_ACCEPT : win.NodeFilter.FILTER_REJECT,
      });
      let node;
      while (text.length < 240 && (node = walker.nextNode())) text += `${node.textContent.slice(0, 240 - text.length)} `;
      if (text.length >= 240) break;
    }
    return text.replace(/\s+/g, " ").trim();
  };
  const shorten = (text, limit) => text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text;
  const shapeTicks = (focused = null, expand = false, position = null) => {
    const index = entries.indexOf(focused);
    const center = position ?? (index < 0 ? -100 : Number.parseFloat(buttons.get(focused)?.style.getPropertyValue("--tick-top")) || 0);
    rulerMarks.forEach((mark, row) => {
      const distance = Math.abs(row * 10 - center) / 10;
      const strength = expand ? Math.exp(-(distance * distance) / 5) : 0;
      mark.style.setProperty("--mark-scale", String((6 + 20 * strength) / 26));
      mark.style.setProperty("--mark-color", distance < .6 && index >= 0
        ? "var(--ink, #242424)" : "color-mix(in srgb, var(--muted, #666) 35%, transparent)");
    });
  };
  const closePreview = () => {
    shapeTicks();
    buttons.get(selected)?.removeAttribute("aria-describedby");
    selected = null;
    previewPosition = null;
    preview.hidden = true;
    shapeTicks(entries.find(entry => buttons.get(entry)?.hasAttribute("aria-current")) || null);
  };
  const updateVisibility = () => {
    nav.hidden = entries.length < 3 || narrow;
    if (nav.hidden) closePreview();
  };
  const updatePreview = () => {
    if (!selected || !stream.contains(selected) || nav.hidden) { closePreview(); return; }
    const index = entries.indexOf(selected);
    if (index < 0) { closePreview(); return; }
    const replies = [];
    for (let sibling = selected.nextElementSibling; sibling; sibling = sibling.nextElementSibling) {
      if (sibling.matches(".message--user:not(.message--pending)")) break;
      if (sibling.matches(".message--assistant:not(.message--process)") && visible(sibling)) {
        const text = bodyText(sibling);
        if (text) replies.push(text);
        if (replies.join(" ").length >= 160) break;
      }
    }
    count.textContent = `第${index + 1}条，共${entries.length}条`;
    question.textContent = shorten(bodyText(selected), 100) || "附件消息";
    answer.textContent = shorten(replies.join(" "), 160) || "暂无回复正文";
    preview.hidden = false;
    const button = buttons.get(selected);
    const top = previewPosition ?? (button.getBoundingClientRect().top - nav.getBoundingClientRect().top);
    preview.style.top = `${Math.max(0, Math.min(top - preview.offsetHeight / 2 + button.offsetHeight / 2, nav.clientHeight - preview.offsetHeight))}px`;
  };
  const showPreview = (button, position = null) => {
    const target = targets.get(button);
    if (!target || !stream.contains(target)) { refresh(); return; }
    buttons.get(selected)?.removeAttribute("aria-describedby");
    selected = target;
    previewPosition = position;
    shapeTicks(target, true, position);
    button.setAttribute("aria-describedby", preview.id);
    updatePreview();
  };
  const locate = target => {
    if (!target || !stream.contains(target)) { refresh(); return; }
    const reduced = win.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const top = stream.scrollTop + target.getBoundingClientRect().top - stream.getBoundingClientRect().top - stream.clientTop - 16;
    stream.scrollTo({ top: Math.max(0, top), behavior: reduced ? "instant" : "smooth" });
  };
  const updateActive = () => {
    scrollFrame = 0;
    if (destroyed || nav.hidden) return;
    const rulerHeight = Math.max(0, nav.clientHeight - 10);
    const markCount = Math.floor(rulerHeight / 10) + 1;
    if (rulerMarks.length !== markCount) {
      rulerMarks = Array.from({ length: markCount }, (_, row) => {
        const mark = doc.createElement("span");
        mark.className = "conversation-outline__mark";
        mark.style.setProperty("--mark-top", `${row * 10}px`);
        return mark;
      });
      marks.replaceChildren(...rulerMarks);
    }
    entries.forEach((entry, index) => {
      const position = index / Math.max(1, entries.length - 1);
      const top = Math.round(position * rulerHeight / 10) * 10;
      buttons.get(entry)?.style.setProperty("--tick-top", `${top}px`);
    });
    const threshold = stream.getBoundingClientRect().top + 48;
    let active = entries[0];
    for (const entry of entries) {
      if (entry.getBoundingClientRect().top > threshold) break;
      active = entry;
    }
    for (const [entry, button] of buttons) {
      if (entry === active) button.setAttribute("aria-current", "location");
      else button.removeAttribute("aria-current");
    }
    if (selected) shapeTicks(selected, true, previewPosition);
    else shapeTicks(active);
  };
  const scheduleActive = () => {
    if (!scrollFrame) scrollFrame = win.requestAnimationFrame(updateActive);
  };
  function refresh() {
    if (destroyed) return;
    win.clearTimeout(refreshTimer);
    refreshTimer = 0;
    entries = [...stream.children].filter(node => node.matches(".message--user:not(.message--pending)") && visible(node));
    const current = new Set(entries);
    for (const [entry, button] of buttons) {
      if (!current.has(entry)) { button.remove(); buttons.delete(entry); }
    }
    entries.forEach((entry, index) => {
      let button = buttons.get(entry);
      if (!button) {
        button = doc.createElement("button");
        button.type = "button";
        button.className = "conversation-outline__tick";
        buttons.set(entry, button);
        targets.set(button, entry);
      }
      const label = `定位第${index + 1}条消息`;
      if (button.getAttribute("aria-label") !== label) button.setAttribute("aria-label", label);
      if (list.children[index + 1] !== button) list.insertBefore(button, list.children[index + 1] || null);
    });
    updateVisibility();
    updatePreview();
    scheduleActive();
  }
  const eventButton = event => event.target.closest?.(".conversation-outline__tick");
  const pointerTarget = event => {
    const height = Math.max(10, nav.clientHeight - 10);
    const position = Math.round(Math.max(0, Math.min(height, event.clientY - nav.getBoundingClientRect().top)) / 10) * 10;
    const index = Math.round(position / height * Math.max(0, entries.length - 1));
    return { button: buttons.get(entries[Math.min(index, entries.length - 1)]), position };
  };
  listen(list, "pointermove", event => {
    if (event.pointerType === "touch") return;
    const { button, position } = pointerTarget(event);
    if (button) showPreview(button, position);
  });
  listen(list, "focusin", event => { const button = eventButton(event); if (button) showPreview(button); });
  listen(list, "click", event => {
    const button = eventButton(event) || pointerTarget(event).button;
    if (button) { showPreview(button); locate(targets.get(button)); }
  });
  listen(jump, "click", () => locate(selected));
  listen(nav, "keydown", event => {
    if (event.key === "Escape") {
      const button = buttons.get(selected);
      button?.focus({ preventScroll: true });
      closePreview();
      event.preventDefault();
      return;
    }
    const button = eventButton(event);
    if (!button) return;
    let index = entries.indexOf(targets.get(button));
    if (event.key === "ArrowDown") index += 1;
    else if (event.key === "ArrowUp") index -= 1;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = entries.length - 1;
    else return;
    event.preventDefault();
    buttons.get(entries[Math.max(0, Math.min(index, entries.length - 1))])?.focus();
  });
  listen(nav, "pointerleave", event => {
    if (event.pointerType !== "touch" && !nav.contains(doc.activeElement)) closePreview();
  });
  listen(nav, "focusout", event => { if (!nav.contains(event.relatedTarget)) closePreview(); });
  listen(doc, "pointerdown", event => { if (!nav.contains(event.target)) closePreview(); });
  listen(stream, "scroll", scheduleActive, { passive: true });
  listen(list, "scroll", closePreview, { passive: true });
  const resize = () => {
    const width = stream.getBoundingClientRect().width;
    narrow = width > 0 && width < 600;
    updateVisibility();
    scheduleActive();
  };
  listen(win, "resize", resize);
  const resizeObserver = win.ResizeObserver ? new win.ResizeObserver(resize) : null;
  resizeObserver?.observe(stream);
  const observer = new win.MutationObserver(records => {
    // Replaced task nodes invalidate previews immediately, before the throttled reconciliation.
    if (selected && !stream.contains(selected)) closePreview();
    if (entries.some(entry => !stream.contains(entry))) nav.hidden = true;
    const structural = records.some(record => record.target === stream ||
      (record.type === "attributes" && record.target.parentElement === stream && record.target.matches(".message--user")));
    if (structural && !refreshTimer) refreshTimer = win.setTimeout(refresh, 120);
    if (selected && !previewTimer) previewTimer = win.setTimeout(() => {
      previewTimer = 0;
      if (!destroyed) updatePreview();
    }, 180);
  });
  observer.observe(stream, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["hidden", "aria-hidden", "class", "style"] });
  resize();
  refresh();
  return {
    refresh,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      observer.disconnect();
      resizeObserver?.disconnect();
      win.clearTimeout(refreshTimer);
      win.clearTimeout(previewTimer);
      win.cancelAnimationFrame(scrollFrame);
      listeners.forEach(remove => remove());
      buttons.clear();
      entries = [];
      selected = null;
      nav.remove();
      if (ownsHostClass) container.classList.remove("conversation-outline-host");
    },
  };
}
