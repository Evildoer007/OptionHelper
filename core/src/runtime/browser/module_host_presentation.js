/* Capability-owned presentation switch for the five embedded module pages. */
(() => {
  "use strict";

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

  const greekSymbols = new Map([
    ["alpha", "α"], ["beta", "β"], ["gamma", "γ"], ["delta", "δ"],
    ["theta", "θ"], ["lambda", "λ"], ["rho", "ρ"], ["sigma", "σ"],
    ["tau", "τ"], ["phi", "φ"], ["omega", "ω"],
  ]);

  const normalizeMathText = (value) => String(value ?? "")
    .replaceAll(">=", "≥")
    .replaceAll("<=", "≤")
    .replaceAll("==", "=")
    .replaceAll("*", "×");

  const normalizeFieldIdentifier = (value) => String(value ?? "")
    .trim()
    .replace(/[\s-]+/g, "_")
    .replace(/_+/g, "_")
    .toLowerCase();

  const fieldSymbol = (field) => {
    const symbol = String(field?.symbol ?? "").trim();
    if (!symbol) return "";
    const key = normalizeFieldIdentifier(field?.key);
    return key && normalizeFieldIdentifier(symbol) === key ? "" : symbol;
  };

  const termValueLabels = new Map([
    ["true", "是"], ["false", "否"],
    ["European", "欧式"], ["American", "美式"], ["Bermudan", "百慕大式"],
    ["cash", "现金结算"], ["physical", "实物交割"],
    ["close", "收盘价"], ["open", "开盘价"], ["high", "最高价"], ["low", "最低价"],
    ["daily", "每个交易日"], ["monthly_last", "每月最后一个交易日"],
    ["KO_over_KI", "同日先敲出后敲入"],
    ["include_hedge_date", "包含避险日"], ["exclude_hedge_date", "不包含避险日"],
    ["gross_before_premium", "期权费前收益"], ["net_after_premium", "期权费后收益"],
  ]);

  const termValueLabel = (value) => {
    const text = String(value ?? "");
    const registered = termValueLabels.get(text);
    if (registered) return registered;
    const monthly = /^monthly_(\d+)(?:st|nd|rd|th)$/.exec(text);
    return monthly ? `每月第${Number(monthly[1])}个交易日` : text;
  };

  const readScript = (source, start) => {
    if (source[start] === "{") {
      const end = source.indexOf("}", start + 1);
      if (end !== -1) return { value: source.slice(start + 1, end), end: end + 1 };
    }
    const match = source.slice(start).match(/^[A-Za-z0-9]+/);
    if (match) return { value: match[0], end: start + match[0].length };
    return null;
  };

  const formatMath = (value) => {
    const source = normalizeMathText(value);
    let output = "";
    for (let index = 0; index < source.length;) {
      const character = source[index];
      if (character === "_" || character === "^") {
        const scripts = [];
        const marker = character;
        let cursor = index;
        while (source[cursor] === marker) {
          const script = readScript(source, cursor + 1);
          if (!script) break;
          scripts.push(script.value);
          cursor = script.end;
        }
        if (scripts.length) {
          const tag = marker === "_" ? "sub" : "sup";
          output += `<${tag} class="formula-${tag}">${escapeHtml(scripts.join(","))}</${tag}>`;
          index = cursor;
          continue;
        }
      }
      const word = source.slice(index).match(/^[A-Za-z]+/);
      if (word) {
        output += escapeHtml(greekSymbols.get(word[0].toLowerCase()) || word[0]);
        index += word[0].length;
        continue;
      }
      output += escapeHtml(character);
      index += 1;
    }
    return `<span class="formula" aria-label="${escapeHtml(source)}">${output}</span>`;
  };

  window.OptionHelperModulePresentation = Object.freeze({
    fieldSymbol,
    formatMath,
    normalizeMathText,
    termValueLabel,
  });

  const query = new URLSearchParams(location.search);
  if (query.get("host") !== "optdesk" || window.parent === window) return;
  document.documentElement.dataset.optionhelperAppHosted = "true";

  const moduleScrollContainers = [
    ".library-panel",
    ".source-panel",
    ".inspector-panel",
    ".settings-panel",
    ".canvas-viewport",
    ".selection-panel",
    ".json-block",
    ".preview-table-wrap",
    ".table-wrap",
    ".audit-details pre",
    ".trade-inspector",
    ".ledger",
    "textarea",
  ];

  const initializeHostedPresentation = async () => {
    document.body?.classList.add("optionhelper-embedded");
    try {
      const { installScrollbarActivity } = await import("/app/frontend/shared/scrollbar-activity.js");
      installScrollbarActivity({ selectors: moduleScrollContainers, includeDocument: false });
    } catch (error) {
      console.warn("OptionHelper overlay scrollbars are unavailable; native scrolling remains enabled.", error);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeHostedPresentation, { once: true });
  } else {
    initializeHostedPresentation();
  }
})();
