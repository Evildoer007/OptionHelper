(() => {
  const root = document.documentElement;
  const splash = document.querySelector("#login-splash");
  const account = document.querySelector("#login-account");
  const launchUrl = new URL(location.href);
  const startupToken = launchUrl.searchParams.get("app_startup");
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const shielded = [
    document.querySelector(".login-wrap"),
    document.querySelector(".login-theme"),
  ].filter(Boolean);

  const setInteractive = (interactive) => {
    shielded.forEach((element) => {
      element.toggleAttribute("inert", !interactive);
      if (interactive) element.removeAttribute("aria-hidden");
      else element.setAttribute("aria-hidden", "true");
    });
  };

  const requestSurface = () => {
    window.__optionhelperVolInRequested = true;
    window.__volIn?.();
  };

  const revealLogin = () => {
    root.classList.remove("login-boot");
    setInteractive(true);
    requestSurface();
    document.querySelector(".login-wrap")?.classList.add("is-revealing");
  };

  if (startupToken) {
    launchUrl.searchParams.delete("app_startup");
    history.replaceState({}, "", `${launchUrl.pathname}${launchUrl.search}`);
  }

  if (!startupToken || reducedMotion || !splash) {
    splash?.remove();
    revealLogin();
    account?.focus({ preventScroll: true });
    return;
  }

  setInteractive(false);
  root.classList.add("login-boot");
  let finished = false;
  let timer = 0;
  const finish = () => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    document.removeEventListener("keydown", finish);
    splash.removeEventListener("click", finish);
    splash.classList.add("is-leaving");
    revealLogin();
    setTimeout(() => {
      splash.remove();
      account?.focus({ preventScroll: true });
    }, 470);
  };

  timer = setTimeout(finish, 1833);
  document.addEventListener("keydown", finish);
  splash.addEventListener("click", finish);

  // The launch screen is never allowed to become a permanent application
  // state.  This independent watchdog still releases the login page if a
  // browser timer is throttled while the WebKit window is being activated.
  setTimeout(finish, 2600);
})();
