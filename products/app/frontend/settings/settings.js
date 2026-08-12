import { enhanceSelects, message, request, safeJson } from "/app/frontend/shared/app.js";
import { currentThemePreference, installThemeControls, setThemePreference } from "/app/frontend/shared/theme.js";

const resultFor = (name) => document.querySelector(`[data-form-result="${name}"]`);
const send = (path, payload) => request(path, { method: "POST", body: safeJson(payload) });
const sendCredential = (path, payload) => request(path, { method: "POST", body: JSON.stringify(payload) });
const session = await request("/api/me").catch(() => { location.assign("/"); return null; });
if (!session) throw new Error("未建立本机会话");

const roleLabel = { admin: "管理员", sales: "用户" };
const canManageModel = session.capabilities.includes("settings.model.write");
const canManageData = session.capabilities.includes("settings.data.write");
document.querySelector("#identity-list").innerHTML = [["使用身份", roleLabel[session.identity.role] || "用户"], ["租户", session.identity.tenant_id], ["本机身份", session.identity.principal_id]].map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("");
if (canManageData) {
  document.querySelector("#data").hidden = false;
  document.querySelector("[data-settings-data-link]").hidden = false;
}
document.querySelector("[data-setup-intro]").textContent = canManageModel && session.capabilities.includes("optdesk")
  ? "首次使用只需选择模型服务，保存API Key并测试连接；随后即可在OptChat和OptDesk发起任务。其余项目已有可用默认值，可按需要调整。"
  : canManageModel
    ? "首次使用只需选择模型服务，保存API Key并测试连接；随后即可在OptChat发起任务。其余项目已有可用默认值，可按需要调整。"
    : "模型服务和数据接口由管理员统一配置。你可以查看当前状态，并调整自己的结果导出与显示偏好。";
const storagePath = document.querySelector("[data-default-storage-path]");
if (storagePath && /Win/i.test(navigator.platform)) {
  storagePath.textContent = "Windows：用户目录/AppData/Local/OptionHelper/local-state";
}

const modelForm = document.querySelector("#model-form");
const dataForm = document.querySelector("#data-form");
const storageForm = document.querySelector("#storage-form");
const preferenceForm = document.querySelector("#preferences-form");
const themePreference = preferenceForm.elements.theme;
const themeControls = preferenceForm.querySelector("[data-theme-controls]");
const modelState = document.querySelector("#model-credential-state");
const dataState = document.querySelector("#data-credential-state");
const modelStep = document.querySelector("[data-model-step]");
const workspaceStep = document.querySelector("[data-workspace-step]");
const modelPreset = modelForm.elements.provider_preset;
const modelAdvanced = document.querySelector("#model-advanced-options");
let modelConfigured = false;
let dataConfigured = false;
let modelCredentialOrigin = "";

installThemeControls(themeControls);
themeControls?.addEventListener("optionhelper:themecontrol", (event) => {
  themePreference.value = event.detail.preference;
});

const MODEL_PRESETS = Object.freeze({
  deepseek: { endpoint: "https://api.deepseek.com/v1", model_name: "deepseek-chat" },
  kimi: { endpoint: "https://api.moonshot.cn/v1", model_name: "moonshot-v1-8k" },
  custom: { endpoint: "", model_name: "" },
});

function fieldFor(control) {
  return control.closest("[data-field-spec]") || control.closest(".field") || control.closest(".advanced-options");
}

function clearInvalid(control) {
  const field = fieldFor(control);
  const error = field?.querySelector("[data-field-error]");
  const errorId = error?.id;
  if (errorId) {
    const describedBy = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== errorId);
    if (describedBy.length) control.setAttribute("aria-describedby", describedBy.join(" "));
    else control.removeAttribute("aria-describedby");
  }
  field?.classList.remove("is-invalid");
  control.removeAttribute("aria-invalid");
  error?.remove();
}

function invalidate(control, text) {
  const field = fieldFor(control);
  if (!field) return;
  field.classList.add("is-invalid");
  control.setAttribute("aria-invalid", "true");
  let error = field.querySelector("[data-field-error]");
  if (!error) {
    error = document.createElement("small");
    error.className = "field-note";
    error.dataset.fieldError = "true";
    error.id = `${control.id || control.name}-error`;
    error.setAttribute("role", "alert");
    field.append(error);
  }
  error.textContent = text;
  const describedBy = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
  describedBy.add(error.id);
  control.setAttribute("aria-describedby", Array.from(describedBy).join(" "));
}

function resetValidation(form) {
  form.querySelectorAll("[aria-invalid=true]").forEach(clearInvalid);
}

function focusFirstInvalid(form) {
  const control = form.querySelector("[aria-invalid=true]");
  if (!control) return;
  const details = control.closest("details");
  if (details) details.open = true;
  const target = control.closest("[data-choice-root]")?.querySelector(".choice-trigger") || control;
  requestAnimationFrame(() => target.focus());
}

function credentialState(target, configured, { kind = "model" } = {}) {
  if (!target) return;
  target.textContent = configured
    ? canManageModel || kind === "data"
      ? "已保存至OptionHelper本机凭据存储。留空会保留当前凭据；请点击“测试连接”确认可用。"
      : "管理员已配置此租户的模型服务，可以直接开始任务。"
    : kind === "model"
      ? "尚未配置。粘贴API Key后即可开始对话。"
      : "尚未配置。保存Refresh Token后，App会自动获取可用的访问凭据。";
}

function showStoredCredential(control, configured, emptyPlaceholder) {
  control.value = "";
  control.placeholder = configured ? "••••••••••••（已保存）" : emptyPlaceholder;
  control.dataset.credentialSaved = String(configured);
}

function updateProgress() {
  const configured = modelConfigured;
  modelStep?.classList.toggle("is-current", !configured);
  modelStep?.classList.toggle("is-complete", configured);
  workspaceStep?.classList.toggle("is-current", configured);
  modelStep?.querySelector("span").replaceChildren(document.createTextNode(configured ? "✓" : "1"));
  modelStep?.querySelector("small").replaceChildren(document.createTextNode(configured ? "已保存凭据，建议测试连接" : "选择服务并保存API Key"));
  workspaceStep?.querySelector("small").replaceChildren(document.createTextNode(configured ? "返回工作台发送需求" : "完成第一步后可开始"));
}

function presetFor(model) {
  const provider = String(model.provider_name || "").trim();
  const endpoint = String(model.endpoint || "").trim().replace(/\/$/, "");
  if (provider === "deepseek-compatible" || endpoint === MODEL_PRESETS.deepseek.endpoint) return "deepseek";
  if (provider === "kimi-compatible" || endpoint === MODEL_PRESETS.kimi.endpoint) return "kimi";
  return "custom";
}

function applyPreset(presetName, { overwrite = false } = {}) {
  const preset = MODEL_PRESETS[presetName] || MODEL_PRESETS.custom;
  modelForm.elements.provider_name.value = "openai-compatible";
  if (overwrite || !modelForm.elements.endpoint.value.trim()) modelForm.elements.endpoint.value = preset.endpoint;
  if (overwrite || !modelForm.elements.model_name.value.trim()) modelForm.elements.model_name.value = preset.model_name;
  if (modelAdvanced) modelAdvanced.open = presetName === "custom";
}

function setActiveSection(id) {
  document.querySelectorAll(".settings-nav a").forEach((link) => {
    const active = link.getAttribute("href") === `#${id}`;
    link.toggleAttribute("aria-current", active);
  });
}

function applySettings(settings) {
  const model = settings.model_service || {};
  const preset = presetFor(model);
  modelPreset.value = preset;
  modelForm.elements.provider_name.value = "openai-compatible";
  modelForm.elements.endpoint.value = model.endpoint || MODEL_PRESETS[preset].endpoint;
  modelForm.elements.model_name.value = model.model_name || MODEL_PRESETS[preset].model_name;
  modelConfigured = Boolean(model.credential_configured);
  modelCredentialOrigin = modelConfigured ? urlOrigin(model.endpoint) : "";
  showStoredCredential(modelForm.elements.api_key, modelConfigured, "粘贴所选模型服务的API Key");
  if (modelAdvanced) modelAdvanced.open = preset === "custom";
  credentialState(modelState, modelConfigured);
  const sharedState = document.querySelector("[data-user-model-state]");
  if (sharedState) sharedState.textContent = modelConfigured ? "管理员已配置模型服务，可以直接开始任务。" : "管理员尚未配置模型服务。";

  const data = settings.data_interface || {};
  dataForm.elements.provider_name.value = data.provider_name === "unconfigured" ? "ifind-http" : (data.provider_name || "ifind-http");
  dataConfigured = Boolean(data.credential_configured);
  showStoredCredential(dataForm.elements.refresh_token, dataConfigured, "粘贴iFind Refresh Token");
  credentialState(dataState, dataConfigured, { kind: "data" });

  storageForm.elements.export_location_ref.value = settings.storage_export?.export_location_ref || "";
  storageForm.elements.allow_user_selected_directory.value = String(settings.storage_export?.allow_user_selected_directory !== false);
  preferenceForm.elements.language.value = settings.preferences?.language || "zh-CN";
  themePreference.value = settings.preferences?.theme || currentThemePreference();
  setThemePreference(themePreference.value, { persist: false });
  updateProgress();
  enhanceSelects(document);
}

function validUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && Boolean(url.hostname) && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}

function urlOrigin(value) {
  try { return new URL(value).origin; } catch { return ""; }
}

function validateModel() {
  resetValidation(modelForm);
  const apiKey = modelForm.elements.api_key;
  const endpoint = modelForm.elements.endpoint;
  let valid = true;
  if (!apiKey.value.trim() && !modelConfigured) {
    invalidate(apiKey, "请粘贴所选模型服务的API Key后保存。");
    valid = false;
  }
  if (!apiKey.value.trim() && modelConfigured && urlOrigin(endpoint.value.trim()) !== modelCredentialOrigin) {
    invalidate(apiKey, "更换模型服务后，请同时粘贴该服务对应的API Key。");
    valid = false;
  }
  if (!endpoint.value.trim() || !validUrl(endpoint.value.trim())) {
    invalidate(endpoint, "请输入完整的https Base URL，例如https://api.example.com/v1。");
    valid = false;
  }
  if (!modelForm.elements.model_name.value.trim()) {
    invalidate(modelForm.elements.model_name, "请输入服务商文档中的模型名。");
    valid = false;
  }
  return valid;
}

[...modelForm.querySelectorAll("input"), ...dataForm.querySelectorAll("input")].forEach((control) => {
  control.addEventListener("input", () => clearInvalid(control));
});
modelPreset.addEventListener("change", () => {
  applyPreset(modelPreset.value, { overwrite: true });
  [modelForm.elements.endpoint, modelForm.elements.model_name].forEach(clearInvalid);
  enhanceSelects(modelForm);
});

function validateData() {
  resetValidation(dataForm);
  const refresh = dataForm.elements.refresh_token;
  if (refresh.value.trim() || dataConfigured) return true;
  invalidate(refresh, "请粘贴iFind Refresh Token后保存。");
  return false;
}

function setSaving(form, saving) {
  const button = form.querySelector("button[type=submit]");
  if (!button) return;
  button.disabled = saving;
  if (saving) button.dataset.originalText = button.textContent;
  button.textContent = saving ? "正在保存…" : (button.dataset.originalText || button.textContent);
}

document.querySelectorAll(".settings-nav a").forEach((link) => link.addEventListener("click", (event) => {
  const id = link.getAttribute("href").slice(1);
  setActiveSection(id);
  const target = document.getElementById(id);
  if (!target) return;
  event.preventDefault();
  history.replaceState(null, "", `#${id}`);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) target.scrollIntoView({ block: "start" });
  else target.scrollIntoView({ behavior: "smooth", block: "start" });
}));
setActiveSection(location.hash.slice(1) || "model");

preferenceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSaving(preferenceForm, true);
  try {
    await send("/api/settings/preferences", Object.fromEntries(new FormData(preferenceForm)));
    message(resultFor("preferences"), "显示偏好已保存，将用于后续页面和HTML报告。");
  } catch (error) { message(resultFor("preferences"), error.message, true); }
  finally { setSaving(preferenceForm, false); }
});

storageForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setSaving(storageForm, true);
  try {
    await send("/api/settings/storage", {
      export_location_ref: storageForm.elements.export_location_ref.value.trim() || null,
      allow_user_selected_directory: storageForm.elements.allow_user_selected_directory.value === "true",
    });
    message(resultFor("storage"), "导出设置已保存。已有结果的位置不会改变。");
  } catch (error) { message(resultFor("storage"), error.message, true); }
  finally { setSaving(storageForm, false); }
});

if (canManageModel) modelForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!validateModel()) {
    focusFirstInvalid(modelForm);
    return;
  }
  const provider_name = modelForm.elements.provider_name.value.trim();
  const endpoint = modelForm.elements.endpoint.value.trim();
  const model_name = modelForm.elements.model_name.value.trim();
  const api_key = modelForm.elements.api_key.value;
  setSaving(modelForm, true);
  try {
    const response = api_key
      ? await sendCredential("/api/settings/model/credential", { provider_name, endpoint, model_name, api_key })
      : await send("/api/settings/model", { provider_name, endpoint, model_name });
    applySettings(response.settings);
    message(resultFor("model"), api_key ? "模型设置和凭据已保存。请点击“测试连接”确认可用。" : "模型服务地址和模型名已保存。请点击“测试连接”确认可用。");
  } catch (error) { message(resultFor("model"), error.message, true); }
  finally { setSaving(modelForm, false); }
});

document.querySelector("#test-model").addEventListener("click", async () => {
  if (!modelConfigured) {
    message(resultFor("model"), "请先保存API Key，再测试连接。", true);
    return;
  }
  if (!validateModel()) {
    focusFirstInvalid(modelForm);
    return;
  }
  const button = document.querySelector("#test-model");
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "正在测试…";
  try {
    const value = await send("/api/settings/test/model", {});
    message(resultFor("model"), value.connection.detail || "模型服务连接可用。");
  } catch (error) { message(resultFor("model"), error.message, true); }
  finally { button.disabled = false; button.textContent = original; }
});

if (canManageData) {
  dataForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!validateData()) {
      focusFirstInvalid(dataForm);
      return;
    }
    const provider_name = dataForm.elements.provider_name.value.trim();
    const refresh_token = dataForm.elements.refresh_token.value;
    setSaving(dataForm, true);
    try {
      const response = refresh_token
        ? await sendCredential("/api/settings/data/credential", { provider_name, refresh_token })
        : await send("/api/settings/data", { provider_name });
      applySettings(response.settings);
      message(resultFor("data"), refresh_token ? "iFind凭据已保存。App会自动获取访问凭据，现在可以测试连接。" : "iFind设置已保存。");
    } catch (error) { message(resultFor("data"), error.message, true); }
    finally { setSaving(dataForm, false); }
  });
  document.querySelector("#test-data").addEventListener("click", async () => {
    if (!dataConfigured) {
      message(resultFor("data"), "请先保存Refresh Token，再测试连接。", true);
      return;
    }
    const button = document.querySelector("#test-data");
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "正在测试…";
    try {
      const value = await send("/api/settings/test/ifind", {});
      message(resultFor("data"), value.connection.detail || "iFind连接测试未返回说明。", value.connection.status !== "available");
    } catch (error) { message(resultFor("data"), error.message, true); }
    finally { button.disabled = false; button.textContent = original; }
  });
}

if (!canManageModel) {
  modelForm.querySelectorAll("input, select, button").forEach((control) => { control.disabled = true; });
  modelForm.querySelectorAll("[data-admin-only]").forEach((node) => { node.hidden = true; });
  const sharedState = document.querySelector("[data-user-model-state]");
  if (sharedState) sharedState.hidden = false;
  const description = document.querySelector("#model > p:not(.setting-section__eyebrow)");
  if (description) description.textContent = "当前租户的模型服务由管理员统一维护。配置完成后，所有用户均可在OptChat中直接使用。";
  modelAdvanced.open = false;
  enhanceSelects(modelForm);
}

applySettings((await request("/api/settings")).settings);
