/*
 * Runs synchronously before the shared stylesheet.  The full, subscribable
 * theme controller lives in theme.js; this tiny bootstrap only prevents a
 * wrong-colour first paint while that module is fetched.
 */
(() => {
  const root = document.documentElement;
  // The embedded settings card must have its final geometry before CSS paints.
  if (location.pathname === "/settings" && window.parent !== window
      && new URLSearchParams(location.search).get("embedded") === "1") {
    root.dataset.settingsEmbedded = "true";
  }
  const allowed = new Set(["light", "dark", "auto"]);
  let preference = "light";
  try {
    const saved = localStorage.getItem("oh-theme");
    if (allowed.has(saved)) preference = saved;
  } catch { /* Storage is optional during the unauthenticated first paint. */ }
  const systemDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const preferredTheme = preference === "auto" ? (systemDark ? "dark" : "light") : preference;
  root.dataset.themePref = preference;
  root.dataset.theme = root.dataset.themeScope === "fixed-light" ? "light" : preferredTheme;
})();
