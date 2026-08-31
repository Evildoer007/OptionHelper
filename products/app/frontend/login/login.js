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
const heading = document.querySelector(".login-head h1");
const themeControls = document.querySelector(".login-theme");
const volSurface = initializeVolSurface(document.querySelector("#login-vol-surface"));
const initializationToken = typeof window.__optionhelperInitializationToken === "string"
  ? window.__optionhelperInitializationToken
  : "";

window.__volIn = () => volSurface?.reveal();
if (window.__optionhelperVolInRequested) window.__volIn();
installThemeControls(themeControls);

function enterWorkspace() {
  location.assign("/optchat");
}

function loginErrorMessage(error) {
  if (error.body?.failure_code === "account_store_unreadable") {
    return "账号数据无法读取，请恢复后重试。";
  }
  return error.body?.message || error.message || "无法连接服务，请重新启动OptionHelper后重试。";
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.textContent = "";
  submit.disabled = true;
  const initializing = form.dataset.mode === "initialize";
  if (!account.value.trim() || !password.value) {
    status.textContent = initializing ? "请填写管理员账号和密码。" : "请填写账号和密码。";
    submit.disabled = false;
    return;
  }
  submit.textContent = initializing ? "正在创建" : "正在登录";
  try {
    if (initializing) {
      await request("/api/auth/initialize", {
        method: "POST",
        headers: { "X-OptionHelper-Initialization-Token": initializationToken },
        body: safeJson({ account: account.value.trim(), password: password.value }, { allowSecrets: new Set(["password"]) }),
      });
      window.__optionhelperInitializationToken = undefined;
      form.dataset.mode = "login";
      heading.textContent = "登录OptionHelper";
      password.value = "";
      password.autocomplete = "current-password";
      remember.closest("label").hidden = false;
      status.textContent = "管理员账号已创建，请登录。";
      submit.disabled = false;
      submit.textContent = "登录";
      password.focus();
      return;
    }
    await request("/api/auth/login", {
      method: "POST",
      body: safeJson({ account: account.value.trim(), password: password.value, remember: remember.checked }, { allowSecrets: new Set(["password"]) }),
    });
    enterWorkspace();
  } catch (error) {
    status.textContent = loginErrorMessage(error);
    submit.disabled = false;
    submit.textContent = initializing ? "创建账号" : "登录";
  }
});

async function bootstrapLogin() {
  submit.disabled = true;
  try {
    await request("/api/me");
    enterWorkspace();
    return;
  } catch (error) {
    if (![401, 403].includes(error.status)) {
      status.textContent = loginErrorMessage(error);
    }
  }

  try {
    const value = await request("/api/auth/initialization");
    if (value.initialization_required === true) {
      form.dataset.mode = "initialize";
      heading.textContent = "创建管理员账号";
      password.autocomplete = "new-password";
      remember.closest("label").hidden = true;
      submit.textContent = "创建账号";
      if (!initializationToken) {
        status.textContent = "账号初始化能力不可用，请重新启动OptionHelper后重试。";
        return;
      }
    }
  } catch (error) {
    status.textContent = error.body?.failure_code === "account_store_unreadable"
      ? loginErrorMessage(error)
      : "无法连接服务，请重新启动OptionHelper后重试。";
  } finally {
    submit.disabled = form.dataset.mode === "initialize" && !initializationToken;
  }
}

bootstrapLogin();

eye.addEventListener("click", () => {
  const visible = eye.getAttribute("aria-pressed") === "true";
  eye.setAttribute("aria-pressed", String(!visible));
  eye.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
  eye.title = eye.getAttribute("aria-label");
  password.type = visible ? "password" : "text";
  password.focus({ preventScroll: true });
});

installCardGlow();
