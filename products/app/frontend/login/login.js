import { request, safeJson } from "/app/frontend/shared/app.js";
import { installThemeControls } from "/app/frontend/shared/theme.js";
import { initializeVolSurface } from "/app/frontend/shared/vol-surface.js";

const form = document.querySelector("#login-form");
const account = document.querySelector("#login-account");
const password = document.querySelector("#login-password");
const remember = document.querySelector("#login-remember");
const submit = document.querySelector("#login-submit");
const eye = document.querySelector("#login-eye");
const status = document.querySelector("#login-status");
const accountField = document.querySelector("#login-account-field");
const passwordField = document.querySelector("#login-password-field");
const accountTip = document.querySelector("#login-account-tip");
const passwordTip = document.querySelector("#login-password-tip");
const themeControls = document.querySelector(".login-theme");
const volSurface = initializeVolSurface(document.querySelector("#login-vol-surface"));
window.__volIn = () => volSurface?.reveal();
if (window.__optionhelperVolInRequested) window.__volIn();
installThemeControls(themeControls);

function saveSessionValue(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Startup animation and workspace handoff remain usable when WebKit has
    // disabled session storage for this local page.
  }
}

function setFieldMessage(field, tip, text, kind = "bad") {
  const invalid = Boolean(text) && kind === "bad";
  const input = field.querySelector("input");
  field.classList.toggle("is-invalid", invalid);
  input?.setAttribute("aria-invalid", String(invalid));
  tip.className = `login-tip login-tip--${kind}`;
  tip.textContent = text || "";
}

function clearStatus() {
  status.textContent = "";
}

function enterWorkspace() {
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  saveSessionValue("optionhelper-workspace-enter", "1");
  document.documentElement.classList.add("login-leaving");
  window.setTimeout(() => location.assign("/optchat"), reducedMotion ? 0 : 180);
}

function installCardGlow() {
  const card = document.querySelector(".login-card");
  if (!card || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let queued = false;
  let x = 50;
  let y = 0;

  card.addEventListener("pointermove", (event) => {
    const bounds = card.getBoundingClientRect();
    x = ((event.clientX - bounds.left) / bounds.width) * 100;
    y = ((event.clientY - bounds.top) / bounds.height) * 100;
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      card.style.setProperty("--login-pointer-x", `${x.toFixed(1)}%`);
      card.style.setProperty("--login-pointer-y", `${y.toFixed(1)}%`);
      queued = false;
    });
  }, { passive: true });
}

account.addEventListener("input", () => {
  setFieldMessage(accountField, accountTip, "");
  clearStatus();
});

password.addEventListener("input", () => {
  setFieldMessage(passwordField, passwordTip, "");
  clearStatus();
});

eye.addEventListener("click", () => {
  const wasVisible = eye.getAttribute("aria-pressed") === "true";
  eye.setAttribute("aria-pressed", String(!wasVisible));
  eye.setAttribute("aria-label", wasVisible ? "显示密码" : "隐藏密码");
  eye.title = eye.getAttribute("aria-label");
  password.type = wasVisible ? "password" : "text";
  password.focus({ preventScroll: true });
});

function updateCapsLock(event) {
  if (passwordField.classList.contains("is-invalid")) return;
  const enabled = Boolean(event.getModifierState?.("CapsLock"));
  setFieldMessage(passwordField, passwordTip, enabled ? "大写锁定已开启" : "", "warn");
}

password.addEventListener("keydown", updateCapsLock);
password.addEventListener("keyup", updateCapsLock);
password.addEventListener("blur", () => {
  if (passwordTip.classList.contains("login-tip--warn")) setFieldMessage(passwordField, passwordTip, "");
});

document.querySelector("#login-forgot").addEventListener("click", (event) => {
  event.preventDefault();
  status.textContent = "账号由管理员统一发放，请联系管理员重置。";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearStatus();
  setFieldMessage(accountField, accountTip, "");
  setFieldMessage(passwordField, passwordTip, "");
  const hasAccount = Boolean(account.value.trim());
  const hasPassword = Boolean(password.value);
  if (hasAccount !== hasPassword) {
    const missingField = hasAccount ? password : account;
    const missingContainer = hasAccount ? passwordField : accountField;
    const missingTip = hasAccount ? passwordTip : accountTip;
    setFieldMessage(missingContainer, missingTip, hasAccount ? "请输入密码" : "请输入账号");
    missingField.focus({ preventScroll: true });
    return;
  }
  submit.disabled = true;
  submit.textContent = "登录中";

  try {
    await request("/api/auth/login", {
      method: "POST",
      body: safeJson({
        account: account.value.trim(),
        password: password.value,
        remember: remember.checked,
      }, { allowSecrets: new Set(["password"]) }),
    });
    enterWorkspace();
    return;
  } catch (error) {
    if (error?.status === 401) {
      setFieldMessage(accountField, accountTip, "账号或密码不正确");
      setFieldMessage(passwordField, passwordTip, "账号或密码不正确");
      account.focus({ preventScroll: true });
    }
    if (error?.status === 401) status.textContent = "账号或密码不正确";
    else if (Number.isInteger(error?.status)) status.textContent = `登录服务返回${error.status}，请稍后重试。`;
    else status.textContent = "无法连接本机服务，请确认AppServer已启动。";
  } finally {
    if (!document.documentElement.classList.contains("login-leaving")) {
      submit.disabled = false;
      submit.textContent = "登录";
    }
  }
});

installCardGlow();
