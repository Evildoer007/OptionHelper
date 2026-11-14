import { RAYON, DEMI_VIEWBOX } from "./bloub-engine.js";

const SVG = "http://www.w3.org/2000/svg";
const ARC = .55228475;
const MIN_PROGRESS = .0002;

function attributes(element, values) {
  for (const [name, value] of Object.entries(values)) element.setAttribute(name, value);
  return element;
}

function svg(name, values = {}) {
  return attributes(document.createElementNS(SVG, name), values);
}

function mix(from, to, fraction) {
  return from + (to - from) * fraction;
}

function clamp(value) {
  return Math.max(0, Math.min(1, value));
}

function ease(start, end, progress) {
  const t = clamp((progress - start) / (end - start));
  return t * t * t * (10 + t * (-15 + 6 * t));
}

function glide(start, end, progress, ramp) {
  const t = clamp((progress - start) / (end - start));
  const q = Math.min(t, 1 - t, ramp) / ramp;
  const distance = ramp * q ** 3 * (1 - q / 2);
  if (t < ramp) return distance / (1 - ramp);
  if (t > 1 - ramp) return 1 - distance / (1 - ramp);
  return (t - ramp / 2) / (1 - ramp);
}

function composerOutline({ x, y, width, height, radius, fold }) {
  const right = x + width;
  const bottom = y + height;
  const room = clamp((width - height) / (height * 1.4));
  const flex = ease(.08, .46, fold) * (1 - ease(.62, .98, fold)) * room;
  const top = y + Math.min(15, height * .22) * flex;
  const lower = bottom - Math.min(4, height * .07) * flex;
  const frontHeight = lower - top;
  const backRadius = Math.min(width / 2, height / 2, mix(radius, height / 2, flex * .84));
  const frontRadius = Math.min(width / 2, frontHeight / 2, mix(radius, frontHeight / 2, flex));
  const back = x + backRadius;
  const front = right - frontRadius;
  const shoulder = front - back;
  const backArc = backRadius * ARC;
  const frontArc = frontRadius * ARC;
  return [
    "M", back, y,
    "C", back + shoulder * .46, y, back + shoulder * .8, top, front, top,
    "C", front + frontArc, top, right, top + frontRadius - frontArc, right, top + frontRadius,
    "V", lower - frontRadius,
    "C", right, lower - frontRadius + frontArc, front + frontArc, lower, front, lower,
    "C", back + shoulder * .8, lower, back + shoulder * .46, bottom, back, bottom,
    "C", back - backArc, bottom, x, bottom - backRadius + backArc, x, bottom - backRadius,
    "V", y + backRadius,
    "C", x, y + backRadius - backArc, back - backArc, y, back, y,
    "Z",
  ].join(" ");
}

function liquidShape(progress, chat, desk) {
  const frontPhase = glide(.215, .84, progress, .29);
  const backPhase = glide(.447, .986, progress, .24);
  const distance = Math.abs(desk.x - chat.x);
  const front = distance * frontPhase;
  const back = distance * backPhase;
  const span = front - back;
  const stretch = ease(0, .38, frontPhase - backPhase);
  const received = ease(.43, .88, progress);
  const radius = mix(chat.radius, desk.radius, (frontPhase + backPhase) / 2);
  const backRadius = radius * (1 - stretch * mix(.29, .61, received));
  const frontRadius = radius * (1 - stretch * mix(.48, .07, received));
  const neckRadius = mix(radius, 4.1, stretch);
  const neck = mix(back, front, mix(.61, .38, received));
  const backArc = backRadius * ARC;
  const frontArc = frontRadius * ARC;
  const backEnd = back - backRadius;
  const frontEnd = front + frontRadius;
  const shoulder = span * .16;
  const neckArc = span * .11;
  return {
    front, back, frontRadius, stretch,
    outline: [
      "M", back, -backRadius,
      "C", back + shoulder, -backRadius, neck - neckArc, -neckRadius, neck, -neckRadius,
      "C", neck + neckArc, -neckRadius, front - shoulder, -frontRadius, front, -frontRadius,
      "C", front + frontArc, -frontRadius, frontEnd, -frontArc, frontEnd, 0,
      "C", frontEnd, frontArc, front + frontArc, frontRadius, front, frontRadius,
      "C", front - shoulder, frontRadius, neck + neckArc, neckRadius, neck, neckRadius,
      "C", neck - neckArc, neckRadius, back + shoulder, backRadius, back, backRadius,
      "C", back - backArc, backRadius, backEnd, backArc, backEnd, 0,
      "C", backEnd, -backArc, back - backArc, -backRadius, back, -backRadius,
      "Z",
    ].join(" "),
  };
}

function painted(element) {
  const style = getComputedStyle(element);
  return element.getClientRects().length > 0 && style.visibility === "visible" && Number(style.opacity) > 0;
}

function copyIcon(original) {
  const bounds = original.getBoundingClientRect();
  const icon = svg("svg", {
    viewBox: original.getAttribute("viewBox"),
    width: bounds.width,
    height: bounds.height,
  });
  for (const child of original.children) {
    const style = getComputedStyle(child);
    if (Number(style.opacity) === 0) continue;
    const shape = child.cloneNode();
    shape.removeAttribute("class");
    attributes(shape, {
      fill: style.fill, stroke: style.stroke, "stroke-width": style.strokeWidth,
      "stroke-linecap": style.strokeLinecap, "stroke-linejoin": style.strokeLinejoin,
      opacity: style.opacity,
    });
    if (style.transform !== "none") {
      shape.style.transform = style.transform;
      shape.style.transformOrigin = style.transformOrigin;
      shape.style.transformBox = style.transformBox;
    }
    icon.append(shape);
  }
  return icon;
}

function copyButtonText(button, group, bounds, defs, id) {
  const label = button.querySelector(".choice-label, .composer-attach-label") || button;
  if (!painted(label)) return;
  const value = label.textContent.trim();
  if (!value) return;
  const range = document.createRange();
  range.selectNodeContents(label);
  const textBounds = range.getBoundingClientRect();
  const clipBounds = label.getBoundingClientRect();
  if (textBounds.width <= 0 || clipBounds.width <= 0) return;
  const style = getComputedStyle(label);
  const clip = svg("clipPath", { id, clipPathUnits: "userSpaceOnUse" });
  clip.append(svg("rect", {
    x: clipBounds.left - bounds.left, y: clipBounds.top - bounds.top,
    width: clipBounds.width, height: clipBounds.height,
  }));
  defs.append(clip);
  const item = svg("text", {
    x: textBounds.left - bounds.left,
    y: textBounds.top + textBounds.height / 2 - bounds.top,
    "dominant-baseline": "central", fill: style.color, opacity: label === button ? 1 : style.opacity,
    "font-family": style.fontFamily, "font-size": style.fontSize, "font-weight": style.fontWeight,
    "clip-path": "url(#" + id + ")",
  });
  item.textContent = value;
  group.append(item);
}

function copyButtonMask(button, group, defs, id, x, y, rect) {
  const style = getComputedStyle(button, "::before");
  const source = [style.maskImage, style.webkitMaskImage].find(value => value && value !== "none");
  const href = source?.match(/^url\((.*)\)$/)?.[1].replace(/^["']|["']$/g, "");
  const width = parseFloat(style.width);
  const height = parseFloat(style.height);
  if (!href?.startsWith("data:image/svg+xml,") || !(width > 0 && height > 0)) return;
  const box = { x: x + (rect.width - width) / 2, y: y + (rect.height - height) / 2, width, height };
  const mask = svg("mask", { id, maskUnits: "userSpaceOnUse", ...box, "mask-type": "alpha" });
  mask.append(svg("image", { ...box, href }));
  defs.append(mask);
  group.append(svg("rect", { ...box, fill: style.backgroundColor, mask: "url(#" + id + ")" }));
}

function copyButtonChevron(button, group, bounds) {
  const chevron = button.querySelector(".choice-trigger__chevron");
  if (!chevron || !painted(chevron)) return;
  const rect = chevron.getBoundingClientRect();
  const style = getComputedStyle(chevron);
  const left = rect.left - bounds.left + 1;
  const right = rect.right - bounds.left - 1;
  const middle = (left + right) / 2;
  const center = rect.top + rect.height / 2 - bounds.top;
  const offset = button.getAttribute("aria-expanded") === "true" ? -1 : 1;
  group.append(svg("path", {
    d: "M " + left + " " + center + " L " + middle + " " + (center + offset * (middle - left)) + " L " + right + " " + center,
    fill: "none", stroke: style.color, "stroke-width": style.borderRightWidth,
  }));
}

function copyComposerContent(form, send, bounds) {
  const content = svg("g");
  const defs = svg("defs");
  content.append(defs);
  const input = form.querySelector("textarea");
  const inputBounds = input.getBoundingClientRect();
  const inputStyle = getComputedStyle(input);
  const placeholder = !input.value;
  const text = (input.value || input.placeholder).split("\n", 1)[0].slice(0, 1000);
  const placeholderStyle = getComputedStyle(input, "::placeholder");
  const inputText = svg("text", {
    x: inputBounds.left - bounds.left + parseFloat(inputStyle.paddingLeft),
    y: inputBounds.top - bounds.top + parseFloat(inputStyle.paddingTop) + parseFloat(inputStyle.lineHeight) * .78,
    fill: placeholder ? placeholderStyle.color : inputStyle.color,
    "font-family": inputStyle.fontFamily, "font-size": inputStyle.fontSize,
    "font-weight": inputStyle.fontWeight, "xml:space": "preserve",
  });
  inputText.textContent = text;
  content.append(inputText);
  let control = 0;
  for (const button of form.querySelectorAll(".composer-actions button")) {
    if (button === send || !painted(button) || button.closest("[hidden], .choice-menu")) continue;
    const rect = button.getBoundingClientRect();
    if (rect.width < 15 || rect.height < 15) continue;
    const style = getComputedStyle(button);
    const x = rect.left - bounds.left;
    const y = rect.top - bounds.top;
    const radius = Math.min(parseFloat(style.borderTopLeftRadius) || 0, rect.height / 2);
    const group = svg("g", { opacity: style.opacity });
    group.append(svg("rect", {
      x, y, width: rect.width, height: rect.height, rx: radius, fill: style.backgroundColor,
    }));
    for (const original of button.querySelectorAll("svg")) {
      if (!painted(original)) continue;
      const icon = copyIcon(original);
      const iconBounds = original.getBoundingClientRect();
      attributes(icon, { x: iconBounds.left - bounds.left, y: iconBounds.top - bounds.top });
      group.append(icon);
    }
    control += 1;
    copyButtonText(button, group, bounds, defs, "workspace-mode-label-" + control);
    copyButtonMask(button, group, defs, "workspace-mode-icon-" + control, x, y, rect);
    copyButtonChevron(button, group, bounds);
    content.append(group);
  }
  return content;
}

/**
 * Draw the shared composer-to-assistant motion over the live workspace.
 * Mode, task, iframe, and composer ownership remain with the workspace.
 */
export function createWorkspaceModeMotion({ shell, workArea, form, send, toggle, avatar }) {
  const desktop = matchMedia("(min-width: 701px)");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let active = null;
  let request = 0;
  reduced.addEventListener("change", stop);

  function stop() {
    cancelAnimationFrame(request);
    if (!active) return;
    active.overlay.remove();
    active = null;
    delete shell.dataset.modeMotion;
    workArea.style.removeProperty("--workspace-mode-veil");
  }

  const layout = new ResizeObserver(() => {
    if (!active) return;
    const area = workArea.getBoundingClientRect();
    if (Math.abs(area.width - active.area.width) > 1 || Math.abs(area.height - active.area.height) > 1) stop();
  });
  layout.observe(workArea);

  function capture(mode) {
    if (!desktop.matches || reduced.matches || document.hidden) return null;
    if (active) return active;
    const button = mode === "chat" ? send : toggle;
    if (!painted(button)) return null;
    const bounds = button.getBoundingClientRect();
    const style = getComputedStyle(button);
    if (bounds.width <= 0 || bounds.height <= 0 || Number(style.opacity) === 0) return null;
    if (mode === "desk") return { mode, bounds, avatarMarkup: new XMLSerializer().serializeToString(avatar) };
    const composer = form.getBoundingClientRect();
    const formStyle = getComputedStyle(form);
    const scale = composer.width / form.offsetWidth;
    return {
      mode, composer, bounds, icon: copyIcon(send.querySelector("svg")), red: style.backgroundColor,
      fill: formStyle.backgroundColor, line: formStyle.borderTopColor,
      corner: parseFloat(formStyle.borderTopLeftRadius) * scale,
      content: copyComposerContent(form, send, composer),
    };
  }

  function createLayers(chat, desk, area, appearance, avatarMarkup) {
    const top = Math.max(0, Math.min(chat.composer.y, desk.artY) - 6);
    const overlay = svg("svg", {
      class: "workspace-mode-motion", "aria-hidden": "true", focusable: "false",
      viewBox: [0, top, area.width, area.height - top].join(" "),
    });
    overlay.style.height = (area.height - top) / area.height * 100 + "%";
    const gradient = svg("linearGradient", { id: "workspace-mode-light", gradientUnits: "userSpaceOnUse" });
    for (const [offset, opacity] of [[0, 0], [.38, .07], [.76, .32], [1, 0]]) {
      gradient.append(svg("stop", { offset, "stop-color": "#fff", "stop-opacity": opacity }));
    }
    const defs = svg("defs");
    defs.append(gradient);
    const paper = svg("path", { fill: appearance.fill, stroke: appearance.line, "stroke-width": 1 });
    const ink = svg("path", { fill: appearance.red });
    const light = svg("path", { fill: "url(#workspace-mode-light)" });
    const liquid = svg("g", { transform: "translate(" + chat.x + " " + chat.y + ") scale(" + desk.direction + " 1)" });
    liquid.append(ink, light);
    const image = svg("image", {
      width: desk.artSize, height: desk.artSize, opacity: 0,
      href: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(avatarMarkup),
    });
    overlay.append(defs, paper, liquid, appearance.icon, image);
    workArea.append(overlay);
    const content = appearance.content;
    const clip = svg("clipPath", { id: "workspace-mode-composer-clip", clipPathUnits: "userSpaceOnUse" });
    const contentMask = svg("path");
    clip.append(contentMask);
    defs.append(clip);
    const cropped = svg("g", { "clip-path": "url(#workspace-mode-composer-clip)" });
    cropped.append(content);
    paper.after(cropped);
    return { overlay, paper, ink, light, gradient, icon: appearance.icon, image, content, contentMask };
  }

  function paint(motion) {
    const { progress, chat, desk, paper, ink, light, gradient, icon, image, content, contentMask } = motion;
    // Uncover the live workspace once. A reversal continues from the current
    // opacity and reaches a clear workspace before the liquid settles.
    const fade = motion.veil;
    const travelled = (progress - fade.from) * fade.direction;
    fade.opacity = Math.min(fade.opacity, fade.initial * (1 - ease(fade.delay, fade.end, travelled)));
    workArea.style.setProperty("--workspace-mode-veil", String(fade.opacity));
    const shape = liquidShape(progress, chat, desk);
    const front = chat.x + shape.front * desk.direction;
    const back = chat.x + shape.back * desk.direction;
    const launcherOpacity = ease(.925, .99, progress);
    attributes(ink, { d: shape.outline, opacity: 1 - ease(.974, 1, progress) });
    attributes(light, { d: shape.outline, opacity: shape.stretch * (1 - ease(.86, .95, progress)) });
    attributes(gradient, {
      x1: shape.back, y1: 9,
      x2: shape.front + shape.frontRadius * .42, y2: -shape.frontRadius * .8,
    });
    attributes(icon, {
      x: back - Number(icon.getAttribute("width")) / 2,
      y: chat.y - Number(icon.getAttribute("height")) / 2,
      opacity: 1 - ease(.25, .43, progress),
    });
    attributes(image, {
      x: front - desk.artSize / 2, y: chat.y - desk.artSize / 2, opacity: launcherOpacity,
    });
    const gather = ease(0, .46, progress);
    const { composer } = chat;
    const width = mix(composer.width, chat.radius * 2, gather);
    const height = mix(composer.height, chat.radius * 2, ease(.04, .46, progress));
    const right = mix(composer.x + composer.width, chat.x + chat.radius, ease(.08, .46, progress));
    const bottom = mix(composer.y + composer.height, chat.y + chat.radius, ease(.10, .46, progress));
    const paperOpacity = 1 - ease(.31, .46, progress);
    paper.setAttribute("opacity", paperOpacity);
    const contentOpacity = 1 - ease(.04, .17, progress);
    content.setAttribute("opacity", contentOpacity);
    if (paperOpacity > 0) {
      const shape = {
        x: right - width, y: bottom - height, width, height,
        radius: mix(composer.radius, chat.radius, gather), fold: gather * .82,
      };
      const outline = composerOutline(shape);
      paper.setAttribute("d", outline);
      if (contentOpacity > 0) {
        content.setAttribute("transform", "translate(" + shape.x + " " + composer.y + ")");
        contentMask.setAttribute("d", outline);
      }
    }
  }

  function tick(now) {
    const motion = active;
    const elapsed = Math.max(0, (now - motion.lastFrame) / 1000);
    motion.lastFrame = now;
    const response = -Math.expm1(-elapsed / .020);
    const difference = motion.velocity - motion.rate;
    const position = motion.progress + motion.rate * elapsed + difference * .020 * response;
    motion.progress = clamp(position);
    motion.velocity = position === motion.progress ? motion.rate + difference * (1 - response) : 0;
    if (Math.abs(motion.progress - motion.target) <= MIN_PROGRESS) {
      stop();
      return;
    }
    paint(motion);
    request = requestAnimationFrame(tick);
  }

  function play(previous, next) {
    if (!previous || !desktop.matches || reduced.matches || document.hidden) {
      stop();
      return false;
    }
    const target = Number(next === "desk");
    const rate = target ? 1 / .52 : -1 / .48;
    if (previous === active) {
      active.veil = {
        from: active.progress, direction: target ? 1 : -1, delay: 0,
        end: Math.max(MIN_PROGRESS, Math.min(.38, Math.abs(target - active.progress))),
        initial: active.veil.opacity, opacity: active.veil.opacity,
      };
      active.target = target;
      active.rate = rate;
      return true;
    }
    const appearance = previous.mode === "chat" ? previous : capture("chat");
    if (!appearance) return false;
    const area = workArea.getBoundingClientRect();
    const { bounds, composer } = appearance;
    const chat = {
      x: bounds.left + bounds.width / 2 - area.left,
      y: bounds.top + bounds.height / 2 - area.top,
      radius: bounds.width / 2,
      composer: {
        x: composer.left - area.left, y: composer.top - area.top,
        width: composer.width, height: composer.height, radius: appearance.corner,
      },
    };
    const scale = area.width / workArea.offsetWidth;
    const actionCenter = parseFloat(getComputedStyle(shell).getPropertyValue("--workspace-action-center")) * scale;
    const launcher = next === "desk" ? toggle.getBoundingClientRect() : previous.bounds;
    if (!launcher || !launcher.width || Math.abs(launcher.top + launcher.height / 2 - area.top - chat.y) > 1) return false;
    const deskX = launcher.left + launcher.width / 2 - area.left;
    const desk = {
      x: deskX, radius: RAYON * launcher.width / (DEMI_VIEWBOX * 2),
      artSize: launcher.width, artY: chat.y - launcher.width / 2,
      direction: Math.sign(deskX - chat.x) || 1,
    };
    if (Math.abs(chat.y - (area.height - actionCenter)) > 1) return false;
    active = {
      ...createLayers(chat, desk, area, appearance, previous.avatarMarkup || new XMLSerializer().serializeToString(avatar)),
      chat, desk, area, target, rate,
      veil: { from: 1 - target, direction: target ? 1 : -1, delay: .31, end: .65, initial: 1, opacity: 1 },
      progress: 1 - target, velocity: 0, lastFrame: performance.now(),
    };
    shell.dataset.modeMotion = "";
    paint(active);
    request = requestAnimationFrame(tick);
    return true;
  }

  return {
    capture, play, stop,
    destroy() {
      stop();
      layout.disconnect();
      reduced.removeEventListener("change", stop);
    },
  };
}
