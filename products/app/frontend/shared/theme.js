const THEME_KEY = "oh-theme";
const ALLOWED = new Set(["light", "dark", "auto"]);
const subscribers = new Set();
const systemTheme = window.matchMedia?.("(prefers-color-scheme: dark)");
const favicon = {
  light: "/app/assets/icons/optionhelper-app-icon-tile-light.svg",
  dark: "/app/assets/icons/optionhelper-app-icon-tile-dark.svg",
};

function preferenceOf(value) {
  return ALLOWED.has(value) ? value : "light";
}

function resolvedTheme(preference) {
  return preference === "auto" ? (systemTheme?.matches ? "dark" : "light") : preference;
}

function pageTheme(preference) {
  return document.documentElement.dataset.themeScope === "fixed-light"
    ? "light"
    : resolvedTheme(preference);
}

function savePreference(preference) {
  try { localStorage.setItem(THEME_KEY, preference); } catch { /* A local cache is optional. */ }
}

function storedPreference() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (ALLOWED.has(saved)) return saved;
  } catch { /* The DOM bootstrap value remains the fallback. */ }
  return preferenceOf(document.documentElement.dataset.themePref);
}

function updateFavicon(theme) {
  const link = document.querySelector('link[rel="icon"]');
  if (link) link.href = favicon[theme];
}

function notifyNativeShell(theme, preference, surfaceTheme) {
  try {
    window.webkit?.messageHandlers?.optionhelperTheme?.postMessage({ theme, preference });
  } catch { /* Browsers outside the macOS shell simply have no native receiver. */ }
  try {
    window.chrome?.webview?.postMessage({ type: "set_theme", theme: surfaceTheme, preference });
  } catch { /* Browsers outside the Windows shell simply have no native receiver. */ }
}

function syncControls(scope = document, preference = currentThemePreference()) {
  scope.querySelectorAll?.("[data-theme-set]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.themeSet === preference));
  });
}

function dispatch(theme, preference, iconTheme = resolvedTheme(preference), { notifyNative = true } = {}) {
  updateFavicon(iconTheme);
  if (notifyNative) notifyNativeShell(iconTheme, preference, theme);
  subscribers.forEach((subscriber) => subscriber(theme, preference));
  document.dispatchEvent(new CustomEvent("optionhelper:themechange", { detail: { theme, preference } }));
}

export function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

export function currentThemePreference() {
  return preferenceOf(document.documentElement.dataset.themePref);
}

export function setThemePreference(value, { persist = true, notifyNative = true } = {}) {
  const preference = preferenceOf(value);
  const iconTheme = resolvedTheme(preference);
  const theme = pageTheme(preference);
  const root = document.documentElement;
  root.dataset.themePref = preference;
  root.dataset.theme = theme;
  if (persist) savePreference(preference);
  syncControls(document, preference);
  dispatch(theme, preference, iconTheme, { notifyNative });
  return theme;
}

export function refreshSystemTheme() {
  if (currentThemePreference() !== "auto") return currentTheme();
  return setThemePreference("auto", { persist: false, notifyNative: false });
}

export function onThemeChange(subscriber) {
  subscribers.add(subscriber);
  return () => subscribers.delete(subscriber);
}

export function installThemeControls(container = document) {
  if (!container || container.dataset.themeControlsInstalled === "true") return;
  syncControls(container);
  container.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-theme-set]");
    if (!button || !container.contains(button) || button.disabled) return;
    setThemePreference(button.dataset.themeSet);
    container.dispatchEvent(new CustomEvent("optionhelper:themecontrol", {
      bubbles: true,
      detail: { preference: currentThemePreference(), theme: currentTheme() },
    }));
  });
  container.dataset.themeControlsInstalled = "true";
}

function installDocumentThemeControls() {
  document.querySelectorAll("[data-theme-controls], .login-theme").forEach((container) => {
    installThemeControls(container);
  });
}

export function initializeTheme() {
  setThemePreference(storedPreference(), { persist: false });
  return currentTheme();
}

function syncStoredTheme() {
  const preference = storedPreference();
  if (preference !== currentThemePreference()) setThemePreference(preference, { persist: false });
}

systemTheme?.addEventListener("change", () => {
  if (currentThemePreference() === "auto") refreshSystemTheme();
});

initializeTheme();
window.addEventListener?.("pageshow", syncStoredTheme);
document.addEventListener?.("visibilitychange", () => {
  if (!document.hidden) syncStoredTheme();
});
// Theme controls remain usable even when a page-specific module cannot load.
// Login still installs its own group idempotently for its local event contract.
installDocumentThemeControls();
window.OptionHelperTheme = Object.freeze({
  currentTheme,
  currentThemePreference,
  setThemePreference,
  refreshSystemTheme,
  onThemeChange,
  installThemeControls,
});
