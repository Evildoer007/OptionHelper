import { clearMessage, enhanceSelects, message, notifyModelConfigurationChanged, request, safeJson } from "/app/frontend/shared/app.js";
import { applyServerThemePreference, currentThemePreference, installThemeControls } from "/app/frontend/shared/theme.js";
import { setScale } from "/app/frontend/shared/ui-scale.js";

async function sendSettings(path, body) {
  const response = await request(path, { method: "POST", body });
  if (path.startsWith("/api/settings/model-provider") || path === "/api/settings/test/model") {
    notifyModelConfigurationChanged();
  }
  return response;
}
const send = (path, payload) => sendSettings(path, safeJson(payload));
const sendCredential = (path, payload) => sendSettings(path, JSON.stringify(payload));
const resultFor = (name) => document.querySelector(`[data-form-result="${name}"]`);
const session = await request("/api/me").catch(() => { location.assign("/"); return null; });
if (!session) throw new Error("登录状态无效");

const canEditModel = session.capabilities.includes("settings.model.local.write") || session.capabilities.includes("settings.model.write");
const canManageData = session.capabilities.includes("settings.data.write") || session.capabilities.includes("settings.data.local.write");
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
const runtimeHost = document.querySelector("[data-runtime-host]");
const runtimeHostDetail = document.querySelector("[data-runtime-host-detail]");
const runtimeCapability = document.querySelector("[data-runtime-capability]");
const runtimeCapabilityDetail = document.querySelector("[data-runtime-capability-detail]");
const runtimeModel = document.querySelector("[data-runtime-model]");
const runtimeModelDetail = document.querySelector("[data-runtime-model-detail]");
const runtimeData = document.querySelector("[data-runtime-data]");
const runtimeDataDetail = document.querySelector("[data-runtime-data-detail]");
let dataConfigured = false;
let dataProbe = null;
let persistedDataVerification = null;
let modelProbe = null;
let configurationGeneration = 0;
let providerState = { providers: [], builtins: [], default_model_selection: null, openProviderId: null, addMode: false };
let multiAgentState = {
  presets: [], selected_preset_id: "sequential-deliberation", role_models: {},
  agent_files: {}, default_agent_files: {},
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
const requestedReturnTo = new URLSearchParams(location.search).get("return_to");
const returnCandidate = requestedReturnTo ? new URL(requestedReturnTo, location.origin) : null;
const returnToWorkspace = returnCandidate?.origin === location.origin
  && ["/optchat", "/optdesk"].includes(returnCandidate.pathname)
  ? `${returnCandidate.pathname}${returnCandidate.search}${returnCandidate.hash}`
  : "/optchat";
if (settingsClose) settingsClose.href = returnToWorkspace;

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
    ? "已保存至当前设备的OptionHelper数据目录。留空会保留当前凭据。"
    : kind === "model" ? "尚未在当前设备配置凭据。" : "尚未配置。保存Refresh Token后，App会自动获取可用访问凭据。";
}

function verificationTimestamp(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}

function dataVerificationProjection(data) {
  const state = String(data.verification_state || "").trim().toLowerCase();
  const revisionMatch = data.current_revision_match === true;
  if (!state) return null;
  const verified = state === "verified" && revisionMatch;
  const stale = state === "verified" && !revisionMatch;
  const verifiedAt = verificationTimestamp(data.verified_at);
  const statusLabel = verified ? "已验证"
    : state === "temporarily_unavailable" ? "暂时不可用"
      : state === "not_configured" ? "未配置"
        : state === "configured_unverified" || stale ? "已声明，未验证"
          : "验证状态未知";
  return {
    state,
    statusLabel,
    verified,
    failed: !verified && ["failed", "revoked", "expired"].includes(state),
    detail: verified
      ? `当前凭据版本已验证${verifiedAt ? `，验证时间${verifiedAt}` : ""}。`
      : stale
        ? "已保存的连接验证属于旧凭据版本，请重新测试当前连接。"
        : state === "temporarily_unavailable"
          ? "当前凭据版本匹配，但iFind连接暂时不可用。已保存配置未丢失，请稍后重新测试。"
          : state === "not_configured"
            ? "尚未保存iFind Refresh Token。"
            : state === "configured_unverified"
              ? "当前凭据尚未完成连接验证，请测试已保存连接。"
        : state === "pending"
          ? "当前连接正在验证，完成前不会用于正式取数。"
          : "当前验证状态无法确认，请重新读取设置或测试连接。",
  };
}

function applySettings(settings) {
  const data = settings.data_interface || {};
  dataForm.elements.provider_name.value = data.provider_name === "unconfigured" ? "ifind-http" : (data.provider_name || "ifind-http");
  dataConfigured = Boolean(data.credential_configured);
  persistedDataVerification = dataVerificationProjection(data);
  if (!dataProbe) dataProbe = persistedDataVerification;
  dataForm.elements.refresh_token.value = "";
  dataForm.elements.refresh_token.placeholder = dataConfigured ? "••••••••••••（已保存）" : "粘贴Refresh Token";
  connectionState(dataState, dataConfigured, "data");
  dataStatus.textContent = dataConfigured ? "已声明" : "未配置";
  dataStatus.classList.toggle("is-ready", Boolean(dataProbe?.verified));
  renderDataProbe();
  storageForm.elements.export_location_ref.value = settings.storage_export?.export_location_ref || "";
  storageForm.elements.allow_user_selected_directory.value = String(settings.storage_export?.allow_user_selected_directory !== false);
  const savedTheme = settings.preferences?.theme;
  if (["light", "dark", "auto"].includes(savedTheme)) applyServerThemePreference(savedTheme);
  themePreference.value = currentThemePreference();
  if (uiScale) uiScale.value = String(window.OptionHelperUIScale?.current?.() || 1);
  enhanceSelects(document);
}

function providerSummary(provider) {
  const enabled = provider.models.filter((model) => model.enabled);
  const verified = enabled.filter((model) => model.verification_state === "verified" && model.current_revision_match === true);
  const selection = providerState.default_model_selection;
  const active = selection?.provider_id === provider.provider_id;
  return `${enabled.length}个已启用模型，${verified.length}个已验证${active ? " · 默认" : ""}`;
}

function modelRow(model, selected, index, { imageEditable = false } = {}) {
  const safeId = escapeHtml(model.model_id || "");
  const safeName = escapeHtml(model.display_name || model.model_id || "");
  const editable = !model.model_id;
  const inputModalities = Array.isArray(model.input_modalities) && model.input_modalities.includes("image")
    ? ["text", "image"]
    : ["text"];
  const identityCore = editable
    ? `<span class="model-row-editors"><input name="model_id-${index}" value="" autocomplete="off" spellcheck="false" placeholder="模型ID" aria-label="模型ID"><input name="model_name-${index}" value="" autocomplete="off" spellcheck="false" placeholder="显示名称" aria-label="模型显示名称"></span>`
    : `<span class="model-row-copy"><strong>${safeName}</strong><small>${safeId}</small><input type="hidden" name="model_id-${index}" value="${safeId}"><input type="hidden" name="model_name-${index}" value="${safeName}"></span>`;
  const imageControl = imageEditable
    ? `<label class="model-capability-control"><input type="checkbox" name="image_input-${index}" ${inputModalities.includes("image") ? "checked" : ""}><span>支持图片输入</span></label>`
    : `<small class="model-capability-note">${inputModalities.includes("image") ? "支持图片输入" : "仅文本输入"}</small>`;
  return `<div class="model-catalog-row" data-model-row data-input-modalities="${inputModalities.join(",")}">
    <label class="model-enable"><input type="checkbox" name="enabled-${index}" ${model.enabled ? "checked" : ""} aria-label="启用${safeName || "此模型"}"><span class="model-checkmark" aria-hidden="true"></span></label>
    <span class="model-row-identity">${identityCore}${imageControl}</span>
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
  const rows = provider.models.map((model, index) => modelRow(model, defaultId, index, { imageEditable: !builtIn })).join("");
  return `<form class="model-provider-editor" data-provider-form data-provider-id="${escapeHtml(provider.provider_id)}" novalidate>
    <div class="model-provider-editor__header"><strong>${escapeHtml(provider.display_name)}</strong><span>${escapeHtml(provider.provider_id)}</span></div>
    <label class="provider-key-field"><span>API密钥</span><input name="api_key" type="password" autocomplete="new-password" placeholder="${configured ? "••••••••••••（已保存）" : "输入API密钥"}"><small>${configured ? "已保存。留空将保留当前密钥。" : "保存后仅写入当前设备的OptionHelper数据目录。"}</small></label>
    <details class="provider-advanced"><summary>自定义设置</summary><div class="provider-advanced__content">
      <label>Provider名称<input name="display_name" value="${escapeHtml(provider.display_name)}" autocomplete="off"></label>
      <label>API地址<input name="endpoint" value="${escapeHtml(provider.endpoint)}" inputmode="url" autocomplete="off"></label>
      <label>Provider ID<input name="provider_id" value="${escapeHtml(provider.provider_id)}" autocomplete="off" ${providerIdLocked ? "readonly" : ""}><small>${providerIdLocked ? "保存后用于绑定当前设备上的凭据，不可修改。" : "首次保存前可自定义；保存后不可修改。"}</small></label>
      <div class="model-catalog-head"><div><strong>模型目录</strong><small>仅启用的模型会显示在对话选择器中。</small></div><div><button class="model-link-button" type="button" data-model-action="discover-models">获取模型</button><button class="model-link-button" type="button" data-model-action="add-model">添加模型</button></div></div>
      <div class="model-catalog" data-model-catalog>${rows}</div>
      <button class="model-test-button" type="button" data-model-action="test-provider">重新测试</button>
    </div></details>
    <p class="form-result" data-provider-result role="status" aria-live="polite"></p>
    <div class="model-provider-actions"><button class="model-secondary-button" type="button" data-model-action="close-provider">取消</button><button class="model-primary-button" type="submit" ${providerSaveInProgress ? "disabled" : ""}>${providerSaveInProgress ? "正在保存并测试…" : "保存并测试"}</button></div>
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
  const verified = providers.some((provider) => provider.models.some(
    (model) => model.enabled && model.verification_state === "verified" && model.current_revision_match === true,
  ));
  const connected = verified || providers.some((provider) => provider.models.some((model) => model.enabled && model.verification_state === "connected" && model.current_revision_match === true));
  modelStatus.textContent = connected ? "已连接" : active ? "已配置" : "未配置";
  modelStatus.classList.toggle("is-ready", connected);
  renderModelProbe(active);
  if (!canEditModel) {
    modelRoot.innerHTML = `<div class="model-empty"><strong>模型服务由管理员维护</strong><p>当前账户没有模型配置权限。</p></div>`;
    return;
  }
  const empty = !providers.length ? `<div class="model-empty"><strong>尚未配置模型服务</strong><p>添加任一Provider，启用至少一个模型并保存API Key后即可开始对话。</p></div>` : "";
  modelRoot.innerHTML = `${empty}${providers.map(renderProvider).join("")}${addProviderPanel()}`;
  enhanceSelects(modelRoot);
}

async function refreshProviders() {
  const response = await request("/api/settings/model-providers");
  providerState = { ...providerState, ...response, openProviderId: providerState.openProviderId };
  const selection = providerState.default_model_selection || {};
  const selectedProvider = providerState.providers.find((provider) => provider.provider_id === selection.provider_id);
  const selectedModel = selectedProvider?.models.find((model) => model.model_id === selection.model_id);
  if (selectedModel?.verification_state === "verified" && selectedModel.current_revision_match === true) {
    modelProbe = {
      verified: true,
      generation: configurationGeneration,
      model: { provider_id: selection.provider_id, model_id: selection.model_id },
      effective: selectedModel.verified_capabilities || {},
    };
  } else {
    modelProbe = selectedModel?.verification_state === "connected" && selectedModel.current_revision_match === true
      ? { connected: true, verified: false, generation: configurationGeneration } : null;
  }
  renderProviders();
}

function invalidateConnectionProbes() {
  configurationGeneration += 1;
  modelProbe = null;
  dataProbe = null;
  renderProviders();
  renderDataProbe();
}

function renderModelProbe(configured = providerState.providers.some((provider) => provider.credential_configured && provider.models.some((model) => model.enabled))) {
  if (!runtimeModel || !runtimeModelDetail) return;
  if (modelProbe?.verified) {
    const labels = {
      streaming: "流式输出",
      tool_calling: "工具调用",
      tool_result_continuation: "工具续接",
      reasoning: "推理",
      cancellation: "取消",
      bounded_timeout: "超时边界",
    };
    const effective = Object.entries(modelProbe.effective || {}).filter(([, value]) => value).map(([key]) => labels[key] || key);
    runtimeModel.textContent = "已验证";
    runtimeModelDetail.textContent = `${modelProbe.model?.provider_id || "默认Provider"}/${modelProbe.model?.model_id || "默认模型"}；有效能力：${effective.join("、") || "未返回"}。`;
    return;
  }
  if (modelProbe?.connected) {
    runtimeModel.textContent = "已连接";
    runtimeModelDetail.textContent = "可用于对话；部分工具能力未确认。";
    return;
  }
  runtimeModel.textContent = configured ? "已配置" : "未配置";
  runtimeModelDetail.textContent = configured
    ? "保存配置时会自动验证连接与模型能力。"
    : "请先在模型配置中保存Provider、模型和凭据。";
}

function renderDataProbe() {
  if (!runtimeData || !runtimeDataDetail) return;
  const statusLabel = dataProbe?.statusLabel || (dataProbe?.verified ? "已验证" : dataProbe?.failed ? "验证失败" : dataConfigured ? "已声明，未验证" : "未配置");
  runtimeData.textContent = statusLabel;
  runtimeDataDetail.textContent = dataProbe?.detail || (dataConfigured
    ? "Refresh Token已保存；保存时会自动测试连接，也可重新测试。"
    : "尚未保存iFind Refresh Token。");
  dataStatus.textContent = statusLabel;
  dataStatus.classList.toggle("is-ready", Boolean(dataProbe?.verified));
}

async function refreshRuntimeStatus() {
  const button = document.querySelector("#refresh-runtime");
  if (button) button.disabled = true;
  try {
    const [health, capability] = await Promise.all([request("/api/health"), request("/api/capability/status")]);
    runtimeHost.textContent = health.status === "ok" ? "可用" : "异常";
    runtimeHostDetail.textContent = health.mode === "local" ? "应用服务响应正常。" : `运行模式：${health.mode || "未知"}。`;
    const pages = Array.isArray(capability.capability?.pages) ? capability.capability.pages : [];
    runtimeCapability.textContent = capability.status === "verified" ? "已验证" : "未验证";
    runtimeCapabilityDetail.textContent = capability.status === "verified"
      ? `${capability.capability?.capability_version || "当前版本"}；已校验${pages.length}个模块页面。`
      : "模块资源未通过完整性校验。";
    clearMessage(resultFor("runtime"));
  } catch (error) {
    runtimeHost.textContent = "状态未知";
    runtimeHostDetail.textContent = "本次探测暂时中断，App正在等待下一次状态刷新。";
    runtimeCapability.textContent = "状态未知";
    runtimeCapabilityDetail.textContent = "瞬时连接异常不会清空上一次运行或验证结果。";
    message(resultFor("runtime"), error.message || "运行状态探测暂时中断。", true);
  } finally {
    if (button) button.disabled = false;
  }
}

const rolePresentation = {
  "sequential-deliberation": {
    Interpreter: { name: "Interpreter", description: "解析目标与约束" },
    Selector: { name: "Selector", description: "生成并比较候选" },
    Reviewer: { name: "Reviewer", description: "复核规则与证据" },
  },
  "independent-council": {
    Framer: { name: "Framer", description: "明确需求边界和比较框架" },
    Matcher: { name: "Matcher", description: "独立匹配候选与约束" },
    Hedger: { name: "Hedger", description: "识别风险暴露与对冲条件" },
    Moderator: { name: "Moderator", description: "汇总独立意见并形成结论" },
  },
  "product-trader-loop": {
    Structurer: { name: "Structurer", description: "提出候选、条款调整和验证计划" },
    Trader: { name: "Trader", description: "基于已验证事实接受或退回候选" },
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
    description: "Structurer与Trader基于同一候选方案迭代，条款变化后重新评估。",
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
    body = `<g class="preset-diagram__links"><path d="M132 44H176" marker-end="url(#${arrow})"/><path d="M384 44H428" marker-end="url(#${arrow})"/><path d="M488 68V100" marker-end="url(#${arrow})"/><path d="M428 84H84Q72 84 72 72V68" fill="none" stroke-dasharray="5 4" marker-end="url(#${arrow})"/></g>${node(12, 20, 120, "Structurer", true)}${node(176, 20, 208, "计算模块")}${node(428, 20, 120, "Trader")}${node(428, 100, 120, "Reviewer")}`;
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

function roleRows(roles, configuredRoles, agentFiles, defaultAgentFiles, displayFor, fieldName, disabled) {
  return roles.map((role) => {
    const selectedValue = roleModelValue(configuredRoles[role]);
    const display = displayFor(role);
    const content = agentFiles[role] || defaultAgentFiles[role] || `# ${role}`;
    const editorId = `agent-file-${role.toLowerCase()}`;
    const modelControl = multiAgentState.available_models.length
      ? `<select name="${escapeHtml(fieldName)}-${escapeHtml(role)}" data-role="${escapeHtml(role)}" data-choice ${canEditModel && !disabled ? "" : "disabled"}>${roleModelOptions(selectedValue)}</select>`
      : `<p class="multi-agent-role-model-empty">尚未配置模型，运行时将使用本轮会话模型。</p>`;
    return `<section class="multi-agent-agent-card">
      <div class="multi-agent-role-row"><span class="multi-agent-role-identity"><strong>${escapeHtml(display.name)}</strong><small>${escapeHtml(display.description)}</small></span><div class="multi-agent-role-model"><span>运行模型</span>${modelControl}</div></div>
      <div class="multi-agent-agent-file"><span><label for="${escapeHtml(editorId)}"><strong>AGENT.md</strong><small>角色指令</small></label><button type="button" class="button-quiet" data-reset-agent-role="${escapeHtml(role)}" ${canEditModel && !disabled ? "" : "disabled"}>恢复默认</button></span><textarea id="${escapeHtml(editorId)}" name="agent-${escapeHtml(role)}" data-agent-role="${escapeHtml(role)}" rows="8" maxlength="16000" spellcheck="false" ${canEditModel && !disabled ? "" : "disabled"}>${escapeHtml(content)}</textarea><small>只定义职责和判断重点。工具权限、数据边界和金融事实规则由App强制执行。</small></div>
    </section>`;
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
    execution_mode: response.execution_mode === "multi" ? "multi" : "single",
    selected_preset_id: textValue(response.selected_preset_id) || "sequential-deliberation",
    role_models: isRecord(response.role_models) ? response.role_models : {},
    agent_files: isRecord(response.agent_files) ? response.agent_files : {},
    default_agent_files: isRecord(response.default_agent_files) ? response.default_agent_files : {},
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
    return `<article class="multi-agent-preset-card ${preset.enabled ? "" : "is-disabled"} ${selectedClass}" aria-disabled="${preset.enabled ? "false" : "true"}" data-runtime-available="${modeAvailable ? "true" : "false"}">
      <label class="multi-agent-preset-card__head">
        <input type="radio" name="multi-agent-preset" value="${escapeHtml(preset.preset_id)}" aria-label="选择${escapeHtml(preset.display_name)}：${escapeHtml(presentation.summary)}" ${preset.preset_id === multiAgentState.selected_preset_id ? "checked" : ""} ${preset.enabled && canEditModel ? "" : "disabled"}>
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
  const agentFiles = multiAgentState.agent_files[selected.preset_id] || {};
  const defaultAgentFiles = multiAgentState.default_agent_files[selected.preset_id] || {};
  const presetRoleRows = roleRows(
    selected.roles, configuredRoles, agentFiles, defaultAgentFiles,
    (role) => roleDisplay(selected.preset_id, role), "role", !selected.enabled,
  );
  multiAgentRoot.innerHTML = `<div class="multi-agent-role-head"><div><label for="recommendation-execution-mode">智能体预设</label><select id="recommendation-execution-mode" data-recommendation-mode data-choice ${canEditModel ? "" : "disabled"}><option value="single" ${multiAgentState.execution_mode === "single" ? "selected" : ""}>单Agent</option><option value="multi" ${multiAgentState.execution_mode === "multi" ? "selected" : ""}>多Agent</option></select><p>单Agent使用本轮会话模型完成筛选和复核；多Agent按下方预设分工。聊天中指定的模式仅对本次推荐生效。</p></div></div><div class="multi-agent-configuration" ${multiAgentState.execution_mode === "single" ? "hidden" : ""}><div class="multi-agent-preset-list" role="radiogroup" aria-label="Recommender多智能体预设">${presetRows}</div>
    <form class="multi-agent-role-form" data-multi-agent-role-form novalidate>
      <div class="multi-agent-role-head"><div><span>当前预设</span><strong>${escapeHtml(selected.display_name)}的Agent配置</strong><small>${modeIsAvailable(selected, runtime) ? "修改对下一次推荐生效。" : "当前Runtime不可用；配置仍可保存，并在Runtime恢复后的新任务中生效。"}</small></div></div>
      <div class="multi-agent-role-list">${presetRoleRows}</div>
      <div class="multi-agent-role-actions"><p class="form-result" data-form-result="multi-agent" role="status" aria-live="polite"></p><div>${multiAgentState.available_models.length ? "" : `<a class="model-secondary-button" href="#model">配置模型</a>`}<button class="model-primary-button" type="submit" ${canEditModel && selected.enabled ? "" : "disabled"}>保存Agent配置</button></div></div>
    </form></div>`;
  renderReviewPolicies();
  enhanceSelects(multiAgentRoot);
}

function renderReviewPolicies() {
  const policies = multiAgentState.review_policies.filter((policy) => policy.enabled);
  const visible = multiAgentState.execution_mode === "multi" && policies.length > 1;
  reviewPolicyRoot.hidden = !visible;
  if (!visible) {
    reviewPolicyRoot.replaceChildren();
    return;
  }
  const labels = { "standard-review": "标准复核", "adversarial-review": "交叉复核" };
  const policyRows = policies.map((policy) => {
    const selected = policy.policy_id === multiAgentState.selected_review_policy_id;
    return `<article class="review-policy-card ${selected ? "is-selected" : ""}">
      <label class="review-policy-card__head"><input type="radio" name="review-policy" value="${escapeHtml(policy.policy_id)}" ${selected ? "checked" : ""} ${canEditModel ? "" : "disabled"}><span><strong>${escapeHtml(labels[policy.policy_id] || policy.display_name)}</strong></span></label>
    </article>`;
  }).join("");
  reviewPolicyRoot.innerHTML = `<div class="review-policy-head"><h3>复核方式</h3></div><div class="review-policy-list" role="radiogroup" aria-label="复核方式">${policyRows}</div>`;
}

async function refreshMultiAgentPresets() {
  multiAgentState = normalizeMultiAgentState(await request("/api/settings/multi-agent-presets"));
  renderMultiAgentPresets();
}

multiAgentRoot.addEventListener("change", async (event) => {
  const mode = event.target.closest('[data-recommendation-mode]');
  if (mode) {
    mode.disabled = true;
    try {
      await send("/api/settings/recommendation-execution", { execution_mode: mode.value });
      await refreshMultiAgentPresets();
    } catch (error) {
      await refreshMultiAgentPresets();
      message(resultFor("multi-agent"), error.message, true);
    }
    return;
  }
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
  const reset = event.target.closest("[data-reset-agent-role]");
  if (reset) {
    const role = reset.dataset.resetAgentRole;
    const content = multiAgentState.default_agent_files[multiAgentState.selected_preset_id]?.[role];
    const textarea = Array.from(multiAgentRoot.querySelectorAll("textarea[data-agent-role]"))
      .find((item) => item.dataset.agentRole === role);
    if (textarea && typeof content === "string") textarea.value = content;
    return;
  }
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
  const agentFiles = {};
  form.querySelectorAll("textarea[data-agent-role]").forEach((textarea) => {
    agentFiles[textarea.dataset.agentRole] = textarea.value;
  });
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await send("/api/settings/multi-agent-role-config", {
      preset_id: multiAgentState.selected_preset_id,
      role_models: roleModels,
      agent_files: agentFiles,
    });
    await refreshMultiAgentPresets();
    message(resultFor("multi-agent"), "Agent配置已保存，将从新任务开始生效。", false);
  } catch (error) { message(resultFor("multi-agent"), error.message, true); }
  finally { button.disabled = false; }
});

function newCustomProvider() {
  const providerId = `provider-${Date.now().toString(36)}`;
  return { provider_id: providerId, display_name: "自定义Provider", endpoint: "", protocol: "openai-chat-completions", credential_configured: false, draft: true, models: [{ model_id: "", display_name: "", enabled: true, input_modalities: ["text"] }] };
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
  const models = rows.map((row) => {
    const imageInput = row.querySelector('[name^="image_input-"]');
    const supportsImages = imageInput instanceof HTMLInputElement
      ? imageInput.checked
      : row.dataset.inputModalities?.split(",").includes("image");
    return {
      model_id: row.querySelector('[name^="model_id-"]').value.trim(),
      display_name: row.querySelector('[name^="model_name-"]').value.trim(),
      enabled: row.querySelector('[name^="enabled-"]').checked,
      input_modalities: supportsImages ? ["text", "image"] : ["text"],
    };
  }).filter((model) => model.model_id);
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

function showProviderResult(form, text, error = false) { if (form) message(form.querySelector("[data-provider-result]"), text, error); }

const fieldErrors = new WeakMap();
let fieldErrorSequence = 0;

function clearFieldError(control) {
  const state = fieldErrors.get(control);
  if (!state) return;
  state.error.remove();
  control.removeAttribute("aria-invalid");
  const descriptions = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(id => id && id !== state.error.id);
  if (descriptions.length) control.setAttribute("aria-describedby", descriptions.join(" "));
  else control.removeAttribute("aria-describedby");
  control.removeEventListener("input", state.clear);
  control.removeEventListener("change", state.clear);
  fieldErrors.delete(control);
}

function showFieldError(control, text) {
  clearFieldError(control);
  const error = document.createElement("small");
  error.id = `settings-field-error-${++fieldErrorSequence}`;
  error.dataset.fieldError = "true";
  error.setAttribute("role", "alert");
  error.textContent = text;
  const modelRow = control.closest("[data-model-row]");
  if (modelRow) {
    error.style.gridColumn = "1 / -1";
    modelRow.append(error);
  } else control.after(error);
  control.setAttribute("aria-invalid", "true");
  control.setAttribute("aria-describedby", [control.getAttribute("aria-describedby"), error.id].filter(Boolean).join(" "));
  const clear = () => clearFieldError(control);
  fieldErrors.set(control, {error, clear});
  control.addEventListener("input", clear);
  control.addEventListener("change", clear);
  for (let details = control.closest("details"); details; details = details.parentElement?.closest("details")) details.open = true;
  control.focus();
  return false;
}

let providerSaveInProgress = false;

async function saveProvider(form) {
  if (providerSaveInProgress) return;
  const payload = catalogPayload(form);
  const originalProviderId = form.dataset.providerId;
  const apiKey = form.elements.api_key.value.trim();
  const current = providerState.providers.find((provider) => provider.provider_id === form.dataset.providerId);
  if (!payload.provider_id) return showFieldError(form.elements.provider_id, "请填写Provider ID。");
  if (!payload.endpoint) return showFieldError(form.elements.endpoint, "请填写API地址。");
  if (!payload.models.some((model) => model.enabled)) {
    const enabledRow = Array.from(form.querySelectorAll("[data-model-row]")).find(row => row.querySelector('[name^="enabled-"]')?.checked);
    const control = enabledRow?.querySelector('[name^="model_id-"]')
      || form.querySelector('[name^="enabled-"]') || form.querySelector('[data-model-action="add-model"]');
    return showFieldError(control, enabledRow ? "请填写已启用模型的ID。" : "请至少启用一个模型。");
  }
  if (!apiKey && !current?.credential_configured) {
    return showFieldError(form.elements.api_key, "请粘贴API密钥后保存。");
  }
  const button = form.querySelector('button[type="submit"]');
  providerSaveInProgress = true;
  button.disabled = true;
  button.textContent = "正在保存并测试…";
  try {
    const response = apiKey
      ? await sendCredential("/api/settings/model-provider/credential", { ...payload, original_provider_id: originalProviderId, api_key: apiKey })
      : await send("/api/settings/model-provider", { ...payload, original_provider_id: originalProviderId });
    providerState.openProviderId = payload.provider_id;
    applySettings(response.settings);
    await refreshProviders();
    invalidateConnectionProbes();
    const savedForm = modelRoot.querySelector(`[data-provider-form][data-provider-id="${CSS.escape(payload.provider_id)}"]`);
    await testProvider(savedForm, payload);
    await refreshMultiAgentPresets();
  } catch (error) {
    const activeForm = modelRoot.querySelector(`[data-provider-form][data-provider-id="${CSS.escape(payload.provider_id)}"]`);
    showProviderResult(activeForm || form, error.message, true);
  } finally {
    providerSaveInProgress = false;
    const activeForm = modelRoot.querySelector(`[data-provider-form][data-provider-id="${CSS.escape(payload.provider_id)}"]`);
    const activeButton = activeForm?.querySelector('button[type="submit"]') || button;
    activeButton.disabled = false;
    activeButton.textContent = "保存并测试";
  }
}

async function testProvider(form, savedPayload = null) {
  const payload = savedPayload || catalogPayload(form);
  const apiKey = savedPayload ? "" : form.elements.api_key.value.trim();
  if (!payload.endpoint || !payload.default_model_id) {
    showProviderResult(form, "请先填写Base URL并选择一个启用模型。", true);
    return;
  }
  const current = providerState.providers.find((provider) => provider.provider_id === payload.provider_id);
  if (apiKey || !current?.credential_configured) {
    showProviderResult(form, "请先保存Provider和API Key，再测试当前模型。", true);
    return;
  }
  const button = form?.querySelector('[data-model-action="test-provider"]');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  showProviderResult(form, "配置已保存，正在测试连接…");
  const generation = configurationGeneration;
  try {
    const value = await send("/api/settings/test/model", {
      provider_id: payload.provider_id,
      model_id: payload.default_model_id,
    });
    if (generation !== configurationGeneration) return;
    const probe = value.connection?.capability_probe || {};
    modelProbe = { ...probe, connected: value.connection?.status === "available", verified: value.connection?.status === "available" && probe.verified === true, generation };
    providerState.openProviderId = payload.provider_id;
    await refreshProviders();
    const refreshedForm = modelRoot.querySelector(`[data-provider-form][data-provider-id="${CSS.escape(payload.provider_id)}"]`);
    showProviderResult(refreshedForm || form, `配置已保存。${value.connection?.detail || "模型能力验证已完成。"}`, value.connection?.status !== "available");
  } catch (error) {
    if (generation === configurationGeneration) showProviderResult(form, `配置已保存，连接测试失败：${error.message}`, true);
  }
  finally { if (button) button.disabled = false; }
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
    const builtIn = providerState.builtins.some((provider) => provider.provider_id === form.dataset.providerId);
    response.models.filter((model) => !existing.has(model.model_id)).forEach((model, offset) => {
      catalog.insertAdjacentHTML("beforeend", modelRow(
        { ...model, enabled: false, input_modalities: ["text"] },
        "",
        catalog.children.length + offset,
        { imageEditable: !builtIn },
      ));
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
    if (!provider || !confirm(`删除“${provider.display_name}”及当前设备上的凭据？`)) return;
    try { await send("/api/settings/model-provider/delete", { provider_id: providerId }); providerState.openProviderId = null; await refreshProviders(); await refreshMultiAgentPresets(); invalidateConnectionProbes(); }
    catch (error) { window.alert(error.message); }
    return;
  }
  if (action === "add-model" && form) {
    const catalog = form.querySelector("[data-model-catalog]");
    const builtIn = providerState.builtins.some((provider) => provider.provider_id === form.dataset.providerId);
    catalog.insertAdjacentHTML("beforeend", modelRow(
      { model_id: "", display_name: "", enabled: true, input_modalities: ["text"] },
      "",
      catalog.children.length,
      { imageEditable: !builtIn },
    ));
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
  return showFieldError(dataForm.elements.refresh_token, "请粘贴iFind Refresh Token后保存。");
}

function setSaving(form, saving) {
  const button = form.querySelector('button[type="submit"], button:not([type])');
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

let dataSaveInProgress = false;

async function testDataConnection() {
    if (!dataConfigured) { message(resultFor("data"), "请先保存Refresh Token，再测试连接。", true); return; }
    const generation = configurationGeneration;
    message(resultFor("data"), "凭据已保存，正在测试连接…");
    try {
      const value = await send("/api/settings/test/ifind", {});
      if (generation !== configurationGeneration) return;
      const failed = value.connection?.status !== "available";
      const verified = !failed;
      dataProbe = { verified, failed, detail: value.connection?.detail || "iFind连接测试未返回说明。", generation: configurationGeneration };
      renderDataProbe();
      message(resultFor("data"), dataProbe.detail, !verified);
    } catch (error) {
      if (generation !== configurationGeneration) return;
      dataProbe = { verified: false, failed: true, detail: error.message, generation: configurationGeneration };
      renderDataProbe();
      message(resultFor("data"), `凭据已保存，连接测试失败：${error.message}`, true);
    }
}

if (canManageData) {
  dataForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (dataSaveInProgress || !validateData()) return;
    dataSaveInProgress = true;
    setSaving(dataForm, true);
    try {
      const refresh = dataForm.elements.refresh_token.value.trim();
      const response = refresh ? await sendCredential("/api/settings/data/credential", { provider_name: "ifind-http", refresh_token: refresh }) : await send("/api/settings/data", { provider_name: "ifind-http" });
      dataProbe = null;
      persistedDataVerification = null;
      configurationGeneration += 1;
      applySettings(response.settings);
      await testDataConnection();
    } catch (error) { message(resultFor("data"), error.message, true); }
    finally { dataSaveInProgress = false; setSaving(dataForm, false); }
  });
  document.querySelector("#test-data").addEventListener("click", async () => {
    if (dataSaveInProgress) return;
    dataSaveInProgress = true;
    try { await testDataConnection(); }
    finally { dataSaveInProgress = false; }
  });
}

document.querySelector("#refresh-runtime")?.addEventListener("click", refreshRuntimeStatus);
document.querySelector("#verify-model-capability")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  const generation = configurationGeneration;
  try {
    const value = await send("/api/settings/test/model", {});
    const probe = value.connection?.capability_probe || {};
    if (generation !== configurationGeneration) return;
    modelProbe = { ...probe, connected: value.connection?.status === "available", verified: value.connection?.status === "available" && probe.verified === true, generation };
    renderProviders();
    message(resultFor("runtime"), value.connection?.detail || (modelProbe.connected ? "连接成功。" : "连接失败，请检查模型配置。"), !modelProbe.connected);
  } catch (error) {
    if (generation === configurationGeneration) {
      modelProbe = { verified: false, failed: true, generation };
      renderProviders();
    }
    message(resultFor("runtime"), error.message, true);
  } finally {
    button.disabled = false;
  }
});

if (!canManageData) dataForm.querySelectorAll("input, select, button").forEach((control) => { control.disabled = true; });
const settingsResponse = await request("/api/settings");
applySettings(settingsResponse.settings);
await refreshProviders();
await refreshMultiAgentPresets();
await refreshRuntimeStatus();
