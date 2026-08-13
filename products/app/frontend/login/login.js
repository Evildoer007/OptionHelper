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
const themeControls = document.querySelector(".login-theme");
const volSurface = initializeVolSurface(document.querySelector("#login-vol-surface"));

window.__volIn = () => volSurface?.reveal();
if (window.__optionhelperVolInRequested) window.__volIn();
installThemeControls(themeControls);

function saveSessionValue(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // The workspace handoff remains usable when WebKit disables session storage.
  }
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

eye.addEventListener("click", () => {
  const wasVisible = eye.getAttribute("aria-pressed") === "true";
  eye.setAttribute("aria-pressed", String(!wasVisible));
  eye.setAttribute("aria-label", wasVisible ? "显示密码" : "隐藏密码");
  eye.title = eye.getAttribute("aria-label");
  password.type = wasVisible ? "password" : "text";
  password.focus({ preventScroll: true });
});

document.querySelector("#login-forgot").addEventListener("click", (event) => {
  event.preventDefault();
  status.textContent = "开发阶段无需重置密码，任意填写后即可进入。";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.textContent = "";
  submit.disabled = true;
  submit.textContent = "登录中";
  try {
    // Development-only presentation login: typed values are never read,
    // persisted or sent. The loopback AppServer creates its fixed local identity.
    await request("/api/auth/login", {
      method: "POST",
      body: safeJson({ account: "", password: "", remember: remember.checked }, { allowSecrets: new Set(["password"]) }),
    });
    enterWorkspace();
  } catch (error) {
    status.textContent = Number.isInteger(error?.status)
      ? `本机服务暂不可用，返回${error.status}。`
      : "无法连接本机服务，请确认AppServer已启动。";
    submit.disabled = false;
    submit.textContent = "登录";
  }
});

installCardGlow();
