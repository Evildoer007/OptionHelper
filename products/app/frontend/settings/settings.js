import { clearMessage, enhanceSelects, message, request, safeJson } from "/app/frontend/shared/app.js";
import { currentThemePreference, installThemeControls } from "/app/frontend/shared/theme.js";
import { setScale } from "/app/frontend/shared/ui-scale.js";

const send = (path, payload) => request(path, { method: "POST", body: safeJson(payload) });
const sendCredential = (path, payload) => request(path, { method: "POST", body: JSON.stringify(payload) });
const resultFor = (name) => document.querySelector(`[data-form-result="${name}"]`);
const session = await request("/api/me").catch(() => { location.assign("/"); return null; });
if (!session) throw new Error("未建立本机会话");

const canEditModel = session.capabilities.includes("settings.model.local.write") || session.capabilities.includes("settings.model.write");
const canManageData = session.capabilities.includes("settings.data.write");
const dataForm = document.querySelector("#data-form");
const storageForm = document.querySelector("#storage-form");
const preferenceForm = document.querySelector("#preferences-form");
const themePreference = preferenceForm.elements.theme;
const themeControls = preferenceForm.querySelector("[data-theme-controls]");
const uiScale = document.querySelector("#preference-ui-scale");
const dataState = document.querySelector("#data-credential-state");
const dataStatus = document.querySelector("[data-data-status]");
const modelStatus = document.querySelector("[data-model-status]");
const modelRoot = document.querySelector("#model-provider-root");
const multiAgentRoot = document.querySelector("#multi-agent-preset-root");
const reviewPolicyRoot = document.querySelector("#review-policy-root");
let dataConfigured = false;
let providerState = { providers: [], builtins: [], default_model_selection: null, openProviderId: null, addMode: false };
let multiAgentState = {
  presets: [], selected_preset_id: "sequential-deliberation", role_models: {},
  review_policies: [], selected_review_policy_id: "standard-review", review_policy_role_models: {}, available_models: [],
  runtime_status: null, runtime_mode: "", runtime_version: "", runtime_reason: "", runtime_available: false,
};
if (canManageData) {
  document.querySelector("#data").hidden = false;
  document.querySelector("[data-settings-data-link]").hidden = false;
}
const storagePath = document.querySelector("[data-default-storage-path]");
if (storagePath && /Win/i.test(navigator.platform)) storagePath.textContent = "Windows：用户目录/AppData/Local/OptionHelper/local-state";

const settingsClose = document.querySelector(".settings-close");
settingsClose?.addEventListener("click", (event) => {
  const referrer = document.referrer ? new URL(document.referrer) : null;
  const returnsToWorkspace = referrer?.origin === location.origin
    && ["/optchat", "/optdesk"].includes(referrer.pathname);
  if (!returnsToWorkspace) return;
  event.preventDefault();
  history.back();
});

installThemeControls(themeControls);
let preferenceSaveQueue = Promise.resolve();

function preferencePayload() {
  return {
    theme: themePreference.value,
  };
}

function queuePreferenceSave() {
  const payload = preferencePayload();
  preferenceSaveQueue = preferenceSaveQueue
    .catch(() => undefined)
    .then(async () => {
      try {
        await send("/api/settings/preferences", payload);
        clearMessage(resultFor("preferences"));
      } catch (error) {
        const current = await request("/api/settings").catch(() => null);
        if (current?.settings) applySettings(current.settings);
        message(resultFor("preferences"), error.message, true);
      }
    });
  return preferenceSaveQueue;
}

themeControls?.addEventListener("optionhelper:themecontrol", (event) => {
  themePreference.value = event.detail.preference;
  void queuePreferenceSave();
});

uiScale?.addEventListener("change", () => setScale(uiScale.value));
document.addEventListener("optionhelper:uiscalechange", (event) => {
  if (uiScale) uiScale.value = String(event.detail.scale);
});

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>\"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

function setActiveSection(id) {
  const links = Array.from(document.querySelectorAll(".settings-nav a:not([hidden])"));
  const activeId = links.some((link) => link.getAttribute("href") === `#${id}`) ? id : "preferences";
  links.forEach((link) => link.toggleAttribute("aria-current", link.getAttribute("href") === `#${activeId}`));
  document.querySelectorAll(".setting-section").forEach((section) => { section.hidden = section.id !== activeId; });
}

function connectionState(target, configured, kind = "model") {
  if (!target) return;
  target.textContent = configured
    ? "已保存至OptionHelper本机数据目录。留空会保留当前凭据。"
    : kind === "model" ? "尚未配置本机凭据。" : "尚未配置。保存Refresh Token后，App会自动获取可用访问凭据。";
}

function applySettings(settings) {
  const data = settings.data_interface || {};
  dataForm.elements.provider_name.value = data.provider_name === "unconfigured" ? "ifind-http" : (data.provider_name || "ifind-http");
  dataConfigured = Boolean(data.credential_configured);
  dataForm.elements.refresh_token.value = "";
  dataForm.elements.refresh_token.placeholder = dataConfigured ? "••••••••••••（已保存）" : "粘贴Refresh Token";
  connectionState(dataState, dataConfigured, "data");
  dataStatus.textContent = dataConfigured ? "已连接" : "未配置";
  dataStatus.classList.toggle("is-ready", dataConfigured);
  storageForm.elements.export_location_ref.value = settings.storage_export?.export_location_ref || "";
  storageForm.elements.allow_user_selected_directory.value = String(settings.storage_export?.allow_user_selected_directory !== false);
  themePreference.value = currentThemePreference();
  if (uiScale) uiScale.value = String(window.OptionHelperUIScale?.current?.() || 1);
  enhanceSelects(document);
}

function providerSummary(provider) {
  const enabled = provider.models.filter((model) => model.enabled);
  const selection = providerState.default_model_selection;
  const active = selection?.provider_id === provider.provider_id;
  return `${enabled.length}个已启用模型${active ? " · 默认" : ""}`;
}

function modelRow(model, selected, index) {
  const safeId = escapeHtml(model.model_id || "");
  const safeName = escapeHtml(model.display_name || model.model_id || "");
  const editable = !model.model_id;
  const identity = editable
    ? `<span class="model-row-editors"><input name="model_id-${index}" value="" autocomplete="off" spellcheck="false" placeholder="模型ID" aria-label="模型ID"><input name="model_name-${index}" value="" autocomplete="off" spellcheck="false" placeholder="显示名称" aria-label="模型显示名称"></span>`
    : `<span class="model-row-copy"><strong>${safeName}</strong><small>${safeId}</small><input type="hidden" name="model_id-${index}" value="${safeId}"><input type="hidden" name="model_name-${index}" value="${safeName}"></span>`;
  return `<div class="model-catalog-row" data-model-row>
    <label class="model-enable"><input type="checkbox" name="enabled-${index}" ${model.enabled ? "checked" : ""} aria-label="启用${safeName || "此模型"}"><span class="model-checkmark" aria-hidden="true"></span></label>
    ${identity}
    <label class="model-default"><input type="radio" name="default_model" value="${safeId}" ${selected === model.model_id ? "checked" : ""} aria-label="设为默认模型"><span>默认</span></label>
    <button type="button" class="model-remove" data-model-action="remove-model" aria-label="删除${safeName || "此模型"}"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 4.5h9M6 4.5V3h4v1.5M5 6.5v5M8 6.5v5M11 6.5v5M4.5 4.5l.5 9h6l.5-9"/></svg></button>
  </div>`;
}

function providerEditor(provider) {
  const configured = Boolean(provider.credential_configured);
  const builtIn = providerState.builtins.some((item) => item.provider_id === provider.provider_id);
  const providerIdLocked = builtIn || provider.draft !== true;
  const defaultId = providerState.default_model_selection?.provider_id === provider.provider_id
    ? providerState.default_model_selection.model_id
    : provider.models.find((model) => model.enabled)?.model_id || "";
  const rows = provider.models.map((model, index) => modelRow(model, defaultId, index)).join("");
  return `<form class="model-provider-editor" data-provider-form data-provider-id="${escapeHtml(provider.provider_id)}" novalidate>
    <div class="model-provider-editor__header"><strong>${escapeHtml(provider.display_name)}</strong><span>${escapeHtml(provider.provider_id)}</span></div>
    <label class="provider-key-field"><span>API密钥</span><input name="api_key" type="password" autocomplete="new-password" placeholder="${configured ? "••••••••••••（已保存）" : "输入API密钥"}"><small>${configured ? "已保存。留空将保留当前密钥。" : "保存后仅写入OptionHelper本机数据目录。"}</small></label>
    <details class="provider-advanced"><summary>自定义设置</summary><div class="provider-advanced__content">
      <label>Provider名称<input name="display_name" value="${escapeHtml(provider.display_name)}" autocomplete="off"></label>
      <label>API地址<input name="endpoint" value="${escapeHtml(provider.endpoint)}" inputmode="url" autocomplete="off"></label>
      <label>Provider ID<input name="provider_id" value="${escapeHtml(provider.provider_id)}" autocomplete="off" ${providerIdLocked ? "readonly" : ""}><small>${providerIdLocked ? "保存后用于绑定本机凭据，不可修改。" : "首次保存前可自定义；保存后不可修改。"}</small></label>
      <div class="model-catalog-head"><div><strong>模型目录</strong><small>仅启用的模型会显示在对话选择器中。</small></div><div><button class="model-link-button" type="button" data-model-action="discover-models">获取模型</button><button class="model-link-button" type="button" data-model-action="add-model">添加模型</button></div></div>
      <div class="model-catalog" data-model-catalog>${rows}</div>
      <button class="model-test-button" type="button" data-model-action="test-provider">测试连接</button>
    </div></details>
    <p class="form-result" data-provider-result role="status" aria-live="polite"></p>
    <div class="model-provider-actions"><button class="model-secondary-button" type="button" data-model-action="close-provider">取消</button><button class="model-primary-button" type="submit">保存</button></div>
  </form>`;
}

function renderProvider(provider) {
  const open = providerState.openProviderId === provider.provider_id;
  const configured = provider.credential_configured;
  if (open) return `<article class="model-provider-card is-open" data-provider-card="${escapeHtml(provider.provider_id)}">${providerEditor(provider)}</article>`;
  return `<article class="model-provider-card" data-provider-card="${escapeHtml(provider.provider_id)}">
    <div class="model-provider-card__head"><span class="provider-state ${configured ? "is-ready" : ""}" aria-hidden="true"></span><span class="model-provider-card__identity"><strong>${escapeHtml(provider.display_name)}</strong><small>${escapeHtml(provider.provider_id)}</small></span><span class="model-provider-card__meta">${providerSummary(provider)}</span><button type="button" class="model-row-action" data-model-action="toggle-provider">编辑</button><button class="model-row-action model-row-action--danger" type="button" data-model-action="delete-provider" aria-label="删除${escapeHtml(provider.display_name)}">删除</button></div>
  </article>`;
}

function addProviderPanel() {
  const plus = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>`;
  if (!providerState.addMode) return `<div class="model-add-actions"><button class="model-add-provider" type="button" data-model-action="show-add-provider">${plus}添加Provider</button><button class="model-add-provider" type="button" data-model-action="add-custom">${plus}添加自定义Provider</button></div>`;
  const choices = providerState.builtins.map((provider) => `<button type="button" class="model-provider-choice" data-model-action="add-builtin" data-provider-id="${escapeHtml(provider.provider_id)}"><strong>${escapeHtml(provider.display_name)}</strong><span>${escapeHtml(provider.endpoint)}</span></button>`).join("");
  return `<section class="model-provider-add"><div><h3>添加Provider</h3><p>选择一个内置Provider。</p></div><div class="model-provider-choices">${choices}</div><div class="model-provider-add__actions"><button class="model-secondary-button" type="button" data-model-action="hide-add-provider">取消</button></div></section>`;
}

function renderProviders() {
  const providers = providerState.providers;
  const active = providers.some((provider) => provider.credential_configured && provider.models.some((model) => model.enabled));
  modelStatus.textContent = active ? "已配置" : "未配置";
  modelStatus.classList.toggle("is-ready", active);
  if (!canEditModel) {
    modelRoot.innerHTML = `<div class="model-empty"><strong>模型服务由管理员维护</strong><p>当前账户没有本机模型配置权限。</p></div>`;
    return;
  }
  const empty = !providers.length ? `<div class="model-empty"><strong>尚未配置模型服务</strong><p>添加任一Provider，启用至少一个模型并保存API Key后即可开始对话。</p></div>` : "";
  modelRoot.innerHTML = `${empty}${providers.map(renderProvider).join("")}${addProviderPanel()}`;
  enhanceSelects(modelRoot);
}

async function refreshProviders() {
  const response = await request("/api/settings/model-providers");
  providerState = { ...providerState, ...response, openProviderId: providerState.openProviderId };
  renderProviders();
}

const rolePresentation = {
  "sequential-deliberation": {
    Interpreter: { name: "Interpreter", description: "解析目标与约束" },
    Selector: { name: "Selector", description: "生成并比较候选" },
    Reviewer: { name: "Reviewer", description: "复核规则与证据" },
    // Compatibility for settings saved before the neutral role migration.
    Intent: { name: "Interpreter", description: "解析目标与约束" },
    Research: { name: "Selector", description: "生成并比较候选" },
    Critic: { name: "Reviewer", description: "复核规则与证据" },
  },
  "independent-council": {
    Framer: { name: "Framer", description: "明确需求边界和比较框架" },
    Matcher: { name: "Matcher", description: "独立匹配候选与约束" },
    Hedger: { name: "Hedger", description: "识别风险暴露与对冲条件" },
    Moderator: { name: "Moderator", description: "汇总独立意见并形成结论" },
  },
  "product-trader-loop": {
    Structurer: { name: "Structurer", description: "提出候选、条款调整和验证计划" },
    Trader: { name: "Trader", description: "基于Host事实接受或退回候选" },
    Reviewer: { name: "Reviewer", description: "复核最终候选版本与事实引用" },
  },
  "constraint-ranking": {
    Specifier: { name: "Specifier", description: "将用户要求转换为硬约束与排序规则" },
    Generator: { name: "Generator", description: "从受控证据生成候选" },
    Evaluator: { name: "Evaluator", description: "逐个验证候选并返回事实引用" },
    Reviewer: { name: "Reviewer", description: "只批准或拒绝确定性排序结果" },
  },
};

const presetPresentation = {
  "sequential-deliberation": {
    summary: "顺序筛选",
    description: "Interpreter依次交给Selector和Reviewer，形成受控候选。",
  },
  "product-trader-loop": {
    summary: "产品交易闭环",
    description: "Structurer与Trader基于同一CandidateVersion迭代，变更版本后才重新评估。",
  },
  "independent-council": {
    summary: "异构评审",
    description: "Framer先定边界，Matcher与Hedger独立判断，再由Moderator汇总。",
  },
  "constraint-ranking": {
    summary: "约束排序",
    description: "Specifier定义约束，Generator生成候选，确定性引擎排序，Reviewer终审。",
  },
};

function presetDiagram(preset) {
  const presentation = presetPresentation[preset.preset_id] || {
    summary: "推荐预设",
    description: "该预设尚未提供可视化说明。",
  };
  const id = `preset-${preset.preset_id.replace(/[^a-z0-9-]/g, "")}`;
  const arrow = `${id}-arrow`;
  const viewBox = preset.preset_id === "constraint-ranking" ? "0 0 560 220" : "0 0 560 160";
  const node = (x, y, width, label, accent = false) => `<g class="preset-diagram__node ${accent ? "is-accent" : ""}"><rect x="${x}" y="${y}" width="${width}" height="48" rx="8"/><text x="${x + width / 2}" y="${y + 29}" text-anchor="middle">${label}</text></g>`;
  let body = "";
  if (preset.preset_id === "sequential-deliberation") {
    body = `<g class="preset-diagram__links"><path d="M148 80H204" marker-end="url(#${arrow})"/><path d="M356 80H412" marker-end="url(#${arrow})"/></g>${node(12, 56, 136, "Interpreter", true)}${node(220, 56, 136, "Selector")}${node(428, 56, 120, "Reviewer")}`;
  } else if (preset.preset_id === "product-trader-loop") {
    body = `<g class="preset-diagram__links"><path d="M132 44H176" marker-end="url(#${arrow})"/><path d="M384 44H428" marker-end="url(#${arrow})"/><path d="M488 68V100" marker-end="url(#${arrow})"/><path d="M428 84H84Q72 84 72 72V68" fill="none" stroke-dasharray="5 4" marker-end="url(#${arrow})"/></g>${node(12, 20, 120, "Structurer", true)}${node(176, 20, 208, "Host Modules")}${node(428, 20, 120, "Trader")}${node(428, 100, 120, "Reviewer")}`;
  } else if (preset.preset_id === "independent-council") {
    body = `<g class="preset-diagram__links"><path d="M124 80H152"/><path d="M152 80V44H176" marker-end="url(#${arrow})"/><path d="M152 80V124H176" marker-end="url(#${arrow})"/><path d="M288 44H348V80"/><path d="M288 124H348V80"/><path d="M348 80H428" marker-end="url(#${arrow})"/></g>${node(12, 56, 112, "Framer", true)}${node(176, 20, 112, "Matcher")}${node(176, 100, 112, "Hedger")}${node(428, 56, 116, "Moderator", true)}`;
  } else if (preset.preset_id === "constraint-ranking") {
    body = `<g class="preset-diagram__links"><path d="M124 110H148" marker-end="url(#${arrow})"/><path d="M260 110H272"/><path d="M272 110V44H284" marker-end="url(#${arrow})"/><path d="M272 110V124H284" marker-end="url(#${arrow})"/><path d="M396 44H412V110"/><path d="M396 124H412V110"/><path d="M412 110H428" marker-end="url(#${arrow})"/><path d="M484 134V156" marker-end="url(#${arrow})"/></g>${node(12, 86, 112, "Specifier", true)}${node(148, 86, 112, "Generator")}${node(284, 20, 112, "Candidate A")}${node(284, 100, 112, "Candidate B")}${node(428, 86, 112, "Ranker", true)}${node(428, 156, 112, "Reviewer")}`;
  } else {
    body = `<text class="preset-diagram__empty" x="280" y="84" text-anchor="middle">暂未提供可视化说明</text>`;
  }
  return `<figure class="multi-agent-preset-diagram" data-preset-diagram role="button" tabindex="0" aria-label="放大查看${escapeHtml(preset.display_name)}模式图"><svg viewBox="${viewBox}" role="img" aria-labelledby="${id}-title ${id}-desc"><title id="${id}-title">${escapeHtml(preset.display_name)}：${escapeHtml(presentation.summary)}</title><desc id="${id}-desc">${escapeHtml(presentation.description)}</desc><defs><marker id="${arrow}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8Z" fill="var(--color-muted)"/></marker></defs>${body}</svg><figcaption>${escapeHtml(presentation.summary)}<span>${escapeHtml(presentation.description)}</span></figcaption></figure>`;
}

let presetDiagramDialog = null;

function closePresetDiagram() {
  if (presetDiagramDialog?.open) presetDiagramDialog.close();
}

function openPresetDiagram(source) {
  closePresetDiagram();
  const dialog = document.createElement("dialog");
  const title = source.querySelector("figcaption")?.firstChild?.textContent?.trim() || "模式图";
  const heading = document.createElement("h2");
  const content = document.createElement("div");
  const close = document.createElement("button");
  const diagram = source.querySelector("svg")?.cloneNode(true);
  dialog.className = "preset-diagram-dialog";
  dialog.setAttribute("aria-label", `放大查看${title}`);
  heading.textContent = title;
  content.className = "preset-diagram-dialog__content";
  close.type = "button";
  close.className = "preset-diagram-dialog__close";
  close.setAttribute("aria-label", "关闭模式图");
  close.textContent = "×";
  if (diagram) {
    diagram.removeAttribute("aria-labelledby");
    content.append(diagram);
  }
  dialog.append(close, heading, content);
  document.body.append(dialog);
  presetDiagramDialog = dialog;
  close.addEventListener("click", closePresetDiagram);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) closePresetDiagram(); });
  dialog.addEventListener("close", () => {
    const trigger = source.isConnected ? source : null;
    dialog.remove();
    if (presetDiagramDialog === dialog) presetDiagramDialog = null;
    trigger?.focus();
  }, { once: true });
  dialog.showModal();
}

function roleModelValue(selection) {
  return selection ? `${selection.provider_id}\u001f${selection.model_id}` : "";
}

function roleModelOptions(selectedValue) {
  const inherited = `<option value="" ${selectedValue ? "" : "selected"}>继承本轮会话模型</option>`;
  const options = multiAgentState.available_models.map((model) => {
    const value = `${model.provider_id}\u001f${model.model_id}`;
    const label = `${model.provider_name} · ${model.model_name || model.model_id}`;
    return `<option value="${escapeHtml(value)}" ${value === selectedValue ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  return inherited + options;
}

function roleDisplay(presetId, role) {
  return rolePresentation[presetId]?.[role] || { name: role, description: "该Agent的模型配置" };
}

function roleRows(roles, configuredRoles, displayFor, fieldName, disabled) {
  return roles.map((role) => {
    const selectedValue = roleModelValue(configuredRoles[role]);
    const display = displayFor(role);
    if (!multiAgentState.available_models.length) {
      return `<div class="multi-agent-role-row is-empty"><span><strong>${escapeHtml(display.name)}</strong><small>${escapeHtml(display.description)}</small></span><p>请先在“模型配置”中启用至少一个模型。</p></div>`;
    }
    return `<label class="multi-agent-role-row"><span><strong>${escapeHtml(display.name)}</strong><small>${escapeHtml(display.description)}</small></span><select name="${escapeHtml(fieldName)}-${escapeHtml(role)}" data-role="${escapeHtml(role)}" data-choice ${canEditModel && !disabled ? "" : "disabled"}>${roleModelOptions(selectedValue)}</select></label>`;
  }).join("");
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function textValue(value) {
  return value === null || value === undefined ? "" : String(value).trim();
}

function readRuntimeState(source = multiAgentState) {
  const rawStatus = source.runtime_status;
  const statusObject = isRecord(rawStatus) ? rawStatus : {};
  const statusValue = typeof rawStatus === "boolean"
    ? (rawStatus ? "available" : "unavailable")
    : statusObject.status ?? statusObject.state ?? rawStatus;
  const status = textValue(statusValue).toLowerCase() || "unknown";
  const explicitAvailable = typeof source.runtime_available === "boolean"
    ? source.runtime_available
    : statusObject.available;
  const mode = textValue(source.runtime_mode || statusObject.mode).toLowerCase();
  const statusAvailable = typeof explicitAvailable === "boolean"
    ? explicitAvailable
    : ["available", "ready", "ok", "running", "enabled", "active", "shadow"].includes(status);
  return {
    status,
    available: statusAvailable && mode !== "disabled",
    mode,
    version: textValue(source.runtime_version || statusObject.version),
    reason: textValue(source.runtime_reason || statusObject.reason || statusObject.message),
  };
}

function normalizeMultiAgentState(payload) {
  const response = isRecord(payload) ? payload : {};
  const state = {
    ...response,
    presets: Array.isArray(response.presets) ? response.presets : [],
    selected_preset_id: textValue(response.selected_preset_id) || "sequential-deliberation",
    role_models: isRecord(response.role_models) ? response.role_models : {},
    review_policies: Array.isArray(response.review_policies) ? response.review_policies : [],
    selected_review_policy_id: textValue(response.selected_review_policy_id) || "standard-review",
    review_policy_role_models: isRecord(response.review_policy_role_models) ? response.review_policy_role_models : {},
    available_models: Array.isArray(response.available_models) ? response.available_models : [],
  };
  return { ...state, runtime_available: readRuntimeState(state).available };
}

function modeIsAvailable(preset, runtime = readRuntimeState()) {
  return Boolean(preset?.enabled && runtime.available);
}

function renderMultiAgentPresets() {
  const runtime = readRuntimeState();
  const selected = multiAgentState.presets.find((preset) => preset.preset_id === multiAgentState.selected_preset_id);
  const presetRows = multiAgentState.presets.map((preset) => {
    const presentation = presetPresentation[preset.preset_id] || { summary: "推荐预设" };
    const selectedClass = preset.preset_id === multiAgentState.selected_preset_id ? "is-selected" : "";
    const modeAvailable = modeIsAvailable(preset, runtime);
    return `<article class="multi-agent-preset-card ${modeAvailable ? "" : "is-disabled"} ${selectedClass}" aria-disabled="${modeAvailable ? "false" : "true"}">
      <label class="multi-agent-preset-card__head">
        <input type="radio" name="multi-agent-preset" value="${escapeHtml(preset.preset_id)}" aria-label="选择${escapeHtml(preset.display_name)}：${escapeHtml(presentation.summary)}" ${preset.preset_id === multiAgentState.selected_preset_id ? "checked" : ""} ${modeAvailable && canEditModel ? "" : "disabled"}>
        <span class="multi-agent-preset-copy"><strong>${escapeHtml(preset.display_name)}</strong><small>${escapeHtml(presentation.summary)}</small></span>
      </label>
      ${presetDiagram(preset)}
    </article>`;
  }).join("");
  if (!selected) {
    multiAgentRoot.innerHTML = `<div class="model-empty"><strong>未找到可用预设</strong><p>请刷新设置或检查App版本。</p></div>`;
    return;
  }
  const configuredRoles = multiAgentState.role_models[selected.preset_id] || {};
  const presetRoleRows = roleRows(
    selected.roles, configuredRoles, (role) => roleDisplay(selected.preset_id, role), "role", !modeIsAvailable(selected, runtime),
  );
  const hasModels = multiAgentState.available_models.length > 0;
  multiAgentRoot.innerHTML = `<div class="multi-agent-preset-list" role="radiogroup" aria-label="Recommender多智能体预设">${presetRows}</div>
    <form class="multi-agent-role-form" data-multi-agent-role-form novalidate>
      <div class="multi-agent-role-head"><div><strong>${escapeHtml(selected.display_name)}的Agent模型</strong><small>${modeIsAvailable(selected, runtime) ? "仅配置本预设当前实际运行的Agent。未指定时继承本轮会话模型；本轮显式模型会优先覆盖角色槽。" : "当前Runtime不可用，暂不能配置或运行此Mode。"}</small></div></div>
      <div class="multi-agent-role-list">${presetRoleRows}</div>
      <div class="multi-agent-role-actions"><p class="form-result" data-form-result="multi-agent" role="status" aria-live="polite"></p>${hasModels ? `<button class="model-primary-button" type="submit" ${canEditModel && modeIsAvailable(selected, runtime) ? "" : "disabled"}>保存Agent模型</button>` : `<a class="model-secondary-button" href="#model">前往配置模型</a>`}</div>
    </form>`;
  renderReviewPolicies();
  enhanceSelects(multiAgentRoot);
}

function renderReviewPolicies() {
  const selected = multiAgentState.review_policies.find((policy) => policy.policy_id === multiAgentState.selected_review_policy_id);
  if (!selected) {
    reviewPolicyRoot.innerHTML = `<div class="model-empty"><strong>未找到复核策略</strong><p>请刷新设置或检查App版本。</p></div>`;
    return;
  }
  const policyRows = multiAgentState.review_policies.map((policy) => {
    const status = policy.enabled ? "可用" : "规划中";
    const detail = policy.enabled ? `${escapeHtml(policy.version)} · ${status}` : escapeHtml(policy.disabled_reason || status);
    return `<article class="review-policy-card ${policy.enabled ? "" : "is-disabled"} ${policy.policy_id === selected.policy_id ? "is-selected" : ""}">
      <label class="review-policy-card__head"><input type="radio" name="review-policy" value="${escapeHtml(policy.policy_id)}" ${policy.policy_id === selected.policy_id ? "checked" : ""} ${policy.enabled && canEditModel ? "" : "disabled"}><span><strong>${escapeHtml(policy.display_name)}</strong><small>独立于推荐Mode的复核协议</small></span><em>${status}</em></label><p>${detail}</p>
    </article>`;
  }).join("");
  reviewPolicyRoot.innerHTML = `<div class="review-policy-head"><div><h3>复核策略</h3><p>标准复核已可用；严格复核将在协议发布后开放。</p></div></div><div class="review-policy-list" role="radiogroup" aria-label="复核策略">${policyRows}</div>
    <div class="multi-agent-role-form"><div class="multi-agent-role-head"><div><strong>使用当前Mode的终审角色</strong><small>标准复核不增加独立AgentRun，也不新增模型槽。Mode1、Mode2和Mode4由Reviewer终审，Mode3由Moderator终审。</small></div></div></div>`;
}

async function refreshMultiAgentPresets() {
  multiAgentState = normalizeMultiAgentState(await request("/api/settings/multi-agent-presets"));
  renderMultiAgentPresets();
}

multiAgentRoot.addEventListener("change", async (event) => {
  const control = event.target.closest('input[name="multi-agent-preset"]');
  if (!control) return;
  try {
    await send("/api/settings/multi-agent-preset/default", { preset_id: control.value });
    multiAgentState.selected_preset_id = control.value;
    renderMultiAgentPresets();
  } catch (error) {
    await refreshMultiAgentPresets();
    message(resultFor("multi-agent"), error.message, true);
  }
});

multiAgentRoot.addEventListener("click", (event) => {
  const diagram = event.target.closest("[data-preset-diagram]");
  if (!diagram) return;
  openPresetDiagram(diagram);
});

multiAgentRoot.addEventListener("keydown", (event) => {
  const diagram = event.target.closest("[data-preset-diagram]");
  if (!diagram || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  openPresetDiagram(diagram);
});

reviewPolicyRoot.addEventListener("change", async (event) => {
  const control = event.target.closest('input[name="review-policy"]');
  if (!control) return;
  try {
    await send("/api/settings/multi-agent-review-policy/default", { review_policy_id: control.value });
    multiAgentState.selected_review_policy_id = control.value;
    renderReviewPolicies();
  } catch (error) {
    await refreshMultiAgentPresets();
    message(resultFor("review-policy"), error.message, true);
  }
});

multiAgentRoot.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-multi-agent-role-form]");
  if (!form) return;
  event.preventDefault();
  const roleModels = {};
  form.querySelectorAll("select[data-role]").forEach((select) => {
    if (!select.value) return;
    const [providerId, modelId] = select.value.split("\u001f");
    roleModels[select.dataset.role] = { provider_id: providerId, model_id: modelId };
  });
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await send("/api/settings/multi-agent-role-models", {
      preset_id: multiAgentState.selected_preset_id,
      role_models: roleModels,
    });
    await refreshMultiAgentPresets();
    message(resultFor("multi-agent"), "Agent模型已保存。", false);
  } catch (error) { message(resultFor("multi-agent"), error.message, true); }
  finally { button.disabled = false; }
});

function newCustomProvider() {
  const providerId = `provider-${Date.now().toString(36)}`;
  return { provider_id: providerId, display_name: "自定义Provider", endpoint: "", protocol: "openai-chat-completions", credential_configured: false, draft: true, models: [{ model_id: "", display_name: "", enabled: true }] };
}

function addBuiltin(providerId) {
  const source = providerState.builtins.find((provider) => provider.provider_id === providerId);
  if (!source) return;
  const existing = providerState.providers.some((provider) => provider.provider_id === source.provider_id);
  const provider = { ...source, models: source.models.map((model, index) => ({ ...model, enabled: index === 0 })), credential_configured: false, draft: true };
  if (!existing) providerState.providers = [...providerState.providers, provider];
  providerState.openProviderId = provider.provider_id;
  providerState.addMode = false;
  renderProviders();
}

function catalogPayload(form) {
  const rows = Array.from(form.querySelectorAll("[data-model-row]"));
  const models = rows.map((row) => ({
    model_id: row.querySelector('[name^="model_id-"]').value.trim(),
    display_name: row.querySelector('[name^="model_name-"]').value.trim(),
    enabled: row.querySelector('[name^="enabled-"]').checked,
  })).filter((model) => model.model_id);
  const selectedRow = form.querySelector('input[name="default_model"]:checked')?.closest("[data-model-row]");
  const selectedModelId = selectedRow?.querySelector('[name^="model_id-"]')?.value.trim() || "";
  const selectedIsEnabled = Boolean(selectedRow?.querySelector('[name^="enabled-"]')?.checked);
  const defaultModelId = selectedIsEnabled && selectedModelId
    ? selectedModelId
    : models.find((model) => model.enabled)?.model_id || "";
  return {
    provider_id: form.elements.provider_id.value.trim().toLowerCase(),
    display_name: form.elements.display_name.value.trim(),
    endpoint: form.elements.endpoint.value.trim(),
    protocol: "openai-chat-completions",
    models,
    default_model_id: defaultModelId,
  };
}

function showProviderResult(form, text, error = false) { message(form.querySelector("[data-provider-result]"), text, error); }

async function saveProvider(form) {
  const payload = catalogPayload(form);
  const originalProviderId = form.dataset.providerId;
  const apiKey = form.elements.api_key.value.trim();
  const current = providerState.providers.find((provider) => provider.provider_id === form.dataset.providerId);
  if (!payload.provider_id || !payload.endpoint || !payload.models.some((model) => model.enabled)) {
    showProviderResult(form, "请填写Provider ID、Base URL，并至少启用一个模型。", true);
    return;
  }
  if (!apiKey && !current?.credential_configured) {
    showProviderResult(form, "请粘贴API Key后保存。", true);
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const response = apiKey
      ? await sendCredential("/api/settings/model-provider/credential", { ...payload, original_provider_id: originalProviderId, api_key: apiKey })
      : await send("/api/settings/model-provider", { ...payload, original_provider_id: originalProviderId });
    providerState.openProviderId = payload.provider_id;
    applySettings(response.settings);
    await refreshProviders();
    await refreshMultiAgentPresets();
  } catch (error) { showProviderResult(form, error.message, true); }
  finally { button.disabled = false; }
}

async function testProvider(form) {
  const payload = catalogPayload(form);
  const apiKey = form.elements.api_key.value.trim();
  if (!payload.endpoint || !payload.default_model_id) {
    showProviderResult(form, "请先填写Base URL并选择一个启用模型。", true);
    return;
  }
  const button = form.querySelector('[data-model-action="test-provider"]');
  button.disabled = true;
  try {
    const value = await sendCredential("/api/settings/model-provider/test", { provider_id: payload.provider_id, endpoint: payload.endpoint, model_id: payload.default_model_id, api_key: apiKey });
    showProviderResult(form, value.connection?.detail || "模型服务连接可用。");
  } catch (error) { showProviderResult(form, error.message, true); }
  finally { button.disabled = false; }
}

async function discoverModels(form) {
  const payload = catalogPayload(form);
  if (!payload.provider_id || !payload.endpoint) {
    showProviderResult(form, "请先填写Provider ID和Base URL。", true);
    return;
  }
  const button = form.querySelector('[data-model-action="discover-models"]');
  button.disabled = true;
  try {
    const response = await sendCredential("/api/settings/model-provider/discover", { provider_id: payload.provider_id, endpoint: payload.endpoint, api_key: form.elements.api_key.value.trim() });
    const catalog = form.querySelector("[data-model-catalog]");
    const existing = new Set(Array.from(catalog.querySelectorAll('[name^="model_id-"]')).map((input) => input.value.trim()));
    response.models.filter((model) => !existing.has(model.model_id)).forEach((model, offset) => {
      catalog.insertAdjacentHTML("beforeend", modelRow({ ...model, enabled: false }, "", catalog.children.length + offset));
    });
    showProviderResult(form, response.models.length ? "已读取模型目录，请勾选要启用的模型。" : "该服务未返回可识别模型，可手动添加。", !response.models.length);
  } catch (error) { showProviderResult(form, `${error.message}，你仍可手动添加模型。`, true); }
  finally { button.disabled = false; }
}

modelRoot.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-model-action]")?.dataset.modelAction;
  if (!action) return;
  const card = event.target.closest("[data-provider-card]");
  const providerId = card?.dataset.providerCard;
  const form = event.target.closest("[data-provider-form]");
  if (action === "show-add-provider") { providerState.addMode = true; renderProviders(); return; }
  if (action === "hide-add-provider") { providerState.addMode = false; renderProviders(); return; }
  if (action === "add-builtin") { addBuiltin(event.target.closest("[data-provider-id]").dataset.providerId); return; }
  if (action === "add-custom") {
    const provider = newCustomProvider();
    providerState.providers = [...providerState.providers, provider];
    providerState.openProviderId = provider.provider_id;
    providerState.addMode = false;
    renderProviders();
    return;
  }
  if (action === "toggle-provider") { providerState.openProviderId = providerState.openProviderId === providerId ? null : providerId; renderProviders(); return; }
  if (action === "close-provider") { providerState.openProviderId = null; await refreshProviders(); return; }
  if (action === "delete-provider") {
    const provider = providerState.providers.find((item) => item.provider_id === providerId);
    if (!provider || !confirm(`删除“${provider.display_name}”及其本机凭据？`)) return;
    try { await send("/api/settings/model-provider/delete", { provider_id: providerId }); providerState.openProviderId = null; await refreshProviders(); await refreshMultiAgentPresets(); }
    catch (error) { window.alert(error.message); }
    return;
  }
  if (action === "add-model" && form) {
    const catalog = form.querySelector("[data-model-catalog]");
    catalog.insertAdjacentHTML("beforeend", modelRow({ model_id: "", display_name: "", enabled: true }, "", catalog.children.length));
    catalog.lastElementChild.querySelector('[name^="model_id-"]').focus();
    return;
  }
  if (action === "remove-model") { event.target.closest("[data-model-row]")?.remove(); return; }
  if (action === "discover-models" && form) { await discoverModels(form); return; }
  if (action === "test-provider" && form) await testProvider(form);
});

modelRoot.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-provider-form]");
  if (!form) return;
  event.preventDefault();
  await saveProvider(form);
});

modelRoot.addEventListener("change", (event) => {
  const control = event.target;
  if (!(control instanceof HTMLInputElement)) return;
  const row = control.closest("[data-model-row]");
  if (control.name === "default_model" && control.checked) {
    const enabled = row?.querySelector('[name^="enabled-"]');
    if (enabled instanceof HTMLInputElement) enabled.checked = true;
    return;
  }
  if (!control.name.startsWith("enabled-") || control.checked) return;
  const defaultControl = row?.querySelector('input[name="default_model"]');
  if (!(defaultControl instanceof HTMLInputElement) || !defaultControl.checked) return;
  defaultControl.checked = false;
  const ownerForm = control.form;
  if (!ownerForm) return;
  const replacement = Array.from(ownerForm.querySelectorAll("[data-model-row]"))
    .find((candidate) => candidate.querySelector('[name^="enabled-"]')?.checked)
    ?.querySelector('input[name="default_model"]');
  if (replacement instanceof HTMLInputElement) replacement.checked = true;
});

modelRoot.addEventListener("input", (event) => {
  const control = event.target;
  if (!(control instanceof HTMLInputElement) || !control.name.startsWith("model_id-")) return;
  const row = control.closest("[data-model-row]");
  const defaultControl = row?.querySelector('input[name="default_model"]');
  if (defaultControl instanceof HTMLInputElement) defaultControl.value = control.value.trim();
});

function validateData() {
  if (dataForm.elements.refresh_token.value.trim() || dataConfigured) return true;
  message(resultFor("data"), "请粘贴iFind Refresh Token后保存。", true);
  return false;
}

function setSaving(form, saving) {
  const button = form.querySelector('button[type="submit"]');
  if (!button) return;
  button.disabled = saving;
  if (saving) button.dataset.originalText = button.textContent;
  button.textContent = saving ? "正在保存…" : (button.dataset.originalText || button.textContent);
}

document.querySelectorAll(".settings-nav a").forEach((link) => link.addEventListener("click", (event) => {
  event.preventDefault();
  const id = link.getAttribute("href").slice(1);
  setActiveSection(id);
  history.replaceState(null, "", `#${id}`);
  document.querySelector(".settings-options")?.scrollTo({ top: 0, behavior: "auto" });
}));
setActiveSection(location.hash.slice(1) || "preferences");

preferenceForm.addEventListener("submit", (event) => { event.preventDefault(); });

storageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSaving(storageForm, true);
  try {
    await send("/api/settings/storage", { export_location_ref: storageForm.elements.export_location_ref.value.trim() || null, allow_user_selected_directory: storageForm.elements.allow_user_selected_directory.value === "true" });
    message(resultFor("storage"), "导出设置已保存。已有结果的位置不会改变。");
  } catch (error) { message(resultFor("storage"), error.message, true); }
  finally { setSaving(storageForm, false); }
});

if (canManageData) {
  dataForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!validateData()) return;
    setSaving(dataForm, true);
    try {
      const refresh = dataForm.elements.refresh_token.value;
      const response = refresh ? await sendCredential("/api/settings/data/credential", { provider_name: "ifind-http", refresh_token: refresh }) : await send("/api/settings/data", { provider_name: "ifind-http" });
      applySettings(response.settings);
      message(resultFor("data"), "iFind凭据已保存。现在可以测试连接。");
    } catch (error) { message(resultFor("data"), error.message, true); }
    finally { setSaving(dataForm, false); }
  });
  document.querySelector("#test-data").addEventListener("click", async () => {
    if (!dataConfigured) { message(resultFor("data"), "请先保存Refresh Token，再测试连接。", true); return; }
    try { const value = await send("/api/settings/test/ifind", {}); message(resultFor("data"), value.connection?.detail || "iFind连接测试未返回说明。", value.connection?.status !== "available"); }
    catch (error) { message(resultFor("data"), error.message, true); }
  });
}

if (!canManageData) dataForm.querySelectorAll("input, select, button").forEach((control) => { control.disabled = true; });
const settingsResponse = await request("/api/settings");
applySettings(settingsResponse.settings);
await refreshProviders();
await refreshMultiAgentPresets();
