import { enhanceSelects, message, request, safeJson } from "/app/frontend/shared/app.js";
import { currentThemePreference, installThemeControls } from "/app/frontend/shared/theme.js";

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
const dataState = document.querySelector("#data-credential-state");
const dataStatus = document.querySelector("[data-data-status]");
const modelStatus = document.querySelector("[data-model-status]");
const modelRoot = document.querySelector("#model-provider-root");
let dataConfigured = false;
let providerState = { providers: [], builtins: [], default_model_selection: null, openProviderId: null, addMode: false };
if (canManageData) {
  document.querySelector("#data").hidden = false;
  document.querySelector("[data-settings-data-link]").hidden = false;
}
const storagePath = document.querySelector("[data-default-storage-path]");
if (storagePath && /Win/i.test(navigator.platform)) storagePath.textContent = "Windows：用户目录/AppData/Local/OptionHelper/local-state";

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
        message(resultFor("preferences"), "已保存。", false);
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
    ? "已保存至OptionHelper本机凭据存储。留空会保留当前凭据。"
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
  const defaultId = providerState.default_model_selection?.provider_id === provider.provider_id
    ? providerState.default_model_selection.model_id
    : provider.models.find((model) => model.enabled)?.model_id || "";
  const rows = provider.models.map((model, index) => modelRow(model, defaultId, index)).join("");
  return `<form class="model-provider-editor" data-provider-form data-provider-id="${escapeHtml(provider.provider_id)}" novalidate>
    <div class="model-provider-editor__header"><strong>${escapeHtml(provider.display_name)}</strong><span>${escapeHtml(provider.provider_id)}</span></div>
    <label class="provider-key-field"><span>API密钥</span><input name="api_key" type="password" autocomplete="new-password" placeholder="${configured ? "••••••••••••（已保存）" : "输入API密钥"}"><small>${configured ? "已保存。留空将保留当前密钥。" : "保存后仅写入当前设备的凭据存储。"}</small></label>
    <details class="provider-advanced"><summary>自定义设置</summary><div class="provider-advanced__content">
      <label>提供方名称<input name="display_name" value="${escapeHtml(provider.display_name)}" autocomplete="off"></label>
      <label>API地址<input name="endpoint" value="${escapeHtml(provider.endpoint)}" inputmode="url" autocomplete="off"></label>
      <label>提供方ID<input name="provider_id" value="${escapeHtml(provider.provider_id)}" autocomplete="off" ${builtIn ? "readonly" : ""}><small>${builtIn ? "内置提供方标识不可修改。" : "仅用于本机识别。"}</small></label>
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
  if (!providerState.addMode) return `<div class="model-add-actions"><button class="model-add-provider" type="button" data-model-action="show-add-provider">${plus}添加提供方</button><button class="model-add-provider" type="button" data-model-action="add-custom">${plus}添加自定义提供方</button></div>`;
  const choices = providerState.builtins.map((provider) => `<button type="button" class="model-provider-choice" data-model-action="add-builtin" data-provider-id="${escapeHtml(provider.provider_id)}"><strong>${escapeHtml(provider.display_name)}</strong><span>${escapeHtml(provider.endpoint)}</span></button>`).join("");
  return `<section class="model-provider-add"><div><h3>添加提供方</h3><p>选择一个内置提供方。</p></div><div class="model-provider-choices">${choices}</div><div class="model-provider-add__actions"><button class="model-secondary-button" type="button" data-model-action="hide-add-provider">取消</button></div></section>`;
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
  const empty = !providers.length ? `<div class="model-empty"><strong>尚未配置模型服务</strong><p>添加任一提供方，启用至少一个模型并保存API Key后即可开始对话。</p></div>` : "";
  modelRoot.innerHTML = `${empty}${providers.map(renderProvider).join("")}${addProviderPanel()}`;
  enhanceSelects(modelRoot);
}

async function refreshProviders() {
  const response = await request("/api/settings/model-providers");
  providerState = { ...providerState, ...response, openProviderId: providerState.openProviderId };
  renderProviders();
}

function newCustomProvider() {
  const providerId = `provider-${Date.now().toString(36)}`;
  return { provider_id: providerId, display_name: "自定义提供方", endpoint: "", protocol: "openai-chat-completions", credential_configured: false, models: [{ model_id: "", display_name: "", enabled: true }] };
}

function addBuiltin(providerId) {
  const source = providerState.builtins.find((provider) => provider.provider_id === providerId);
  if (!source) return;
  const existing = providerState.providers.some((provider) => provider.provider_id === source.provider_id);
  const provider = { ...source, models: source.models.map((model, index) => ({ ...model, enabled: index === 0 })), credential_configured: false };
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
  const defaultModelId = form.querySelector('input[name="default_model"]:checked')?.value || models.find((model) => model.enabled)?.model_id || "";
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
  const apiKey = form.elements.api_key.value.trim();
  const current = providerState.providers.find((provider) => provider.provider_id === form.dataset.providerId);
  if (!payload.provider_id || !payload.endpoint || !payload.models.some((model) => model.enabled)) {
    showProviderResult(form, "请填写提供方ID、Base URL，并至少启用一个模型。", true);
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
      ? await sendCredential("/api/settings/model-provider/credential", { ...payload, api_key: apiKey })
      : await send("/api/settings/model-provider", payload);
    providerState.openProviderId = payload.provider_id;
    applySettings(response.settings);
    await refreshProviders();
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
    showProviderResult(form, "请先填写提供方ID和Base URL。", true);
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
    try { await send("/api/settings/model-provider/delete", { provider_id: providerId }); providerState.openProviderId = null; await refreshProviders(); }
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
