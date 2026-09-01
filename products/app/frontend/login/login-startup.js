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

  const mountReferenceAnimation = () => {
    const stage = splash?.querySelector?.(".login-splash__stage");
    const canEnhance = stage
      && typeof stage.append === "function"
      && typeof document.createElement === "function"
      && typeof window.DOMParser === "function"
      && typeof window.Path2D === "function"
      && typeof window.AbortController === "function"
      && typeof window.requestAnimationFrame === "function"
      && typeof window.fetch === "function";
    if (!canEnhance) {
      splash?.classList.add("is-fallback");
      return { stop() {} };
    }

    const logoAsset = "/app/assets/icons/optionhelper-logo.svg";
    const startMs = 516;
    const flyMs = 960;
    const stagger = .5;
    const particleStep = 2;
    const canvas = document.createElement("canvas");
    const loadController = new AbortController();
    let logo = null;
    canvas.id = "login-splash-sparks";
    canvas.className = "login-splash__sparks";
    canvas.setAttribute("aria-hidden", "true");

    let stopped = false;
    let active = false;
    let assembled = false;
    let frameId = 0;

    const assemble = () => {
      if (stopped || assembled) return;
      assembled = true;
      splash.classList.add("is-assembled");
      canvas.classList.add("is-settled");
    };

    const prepareLogo = (svgText) => {
      const parsed = new DOMParser().parseFromString(svgText, "image/svg+xml");
      if (parsed.querySelector("parsererror")) throw new Error("Invalid packaged logo");
      const imported = document.importNode(parsed.documentElement, true);
      imported.removeAttribute("width");
      imported.removeAttribute("height");
      imported.removeAttribute("role");
      imported.removeAttribute("aria-labelledby");
      imported.setAttribute("class", "login-splash__reference-logo");
      imported.setAttribute("aria-hidden", "true");

      const disc = imported.querySelector("#optionhelper-disc");
      const payoff = imported.querySelector("#optionhelper-payoff");
      const dotStart = imported.querySelector("#optionhelper-dot-start");
      const dotEnd = imported.querySelector("#optionhelper-dot-end");
      const arc = imported.querySelector("#optionhelper-arc");
      const wordOption = imported.querySelector("#optionhelper-word-option");
      const wordHelper = imported.querySelector("#optionhelper-word-helper");
      const rule = imported.querySelector("#optionhelper-rule rect");
      if (![disc, payoff, dotStart, dotEnd, arc, wordOption, wordHelper, rule].every(Boolean)) {
        throw new Error("Incomplete packaged logo");
      }
      disc.setAttribute("class", "login-splash__reference-disc");
      payoff.setAttribute("class", "login-splash__reference-ink");
      dotStart.setAttribute("class", "login-splash__reference-ink");
      dotEnd.setAttribute("class", "login-splash__reference-ink");
      arc.setAttribute("class", "login-splash__reference-arc");
      wordOption.id = "login-reference-word-option";
      wordHelper.id = "login-reference-word-helper";
      wordOption.setAttribute("class", "login-splash__reference-word");
      wordHelper.setAttribute("class", "login-splash__reference-word");
      const wordColor = root.dataset.theme === "dark" ? "#E8EAED" : "#252525";
      wordOption.setAttribute("fill", wordColor);
      wordHelper.setAttribute("fill", wordColor);
      rule.setAttribute("class", "login-splash__reference-rule");
      rule.setAttribute("width", "675.4");
      return imported;
    };

    const drawSparks = () => {
      if (stopped || !active || !logo) return;
      const context = canvas.getContext?.("2d");
      const stageRect = stage.getBoundingClientRect?.();
      const wordElements = [
        logo.querySelector("#login-reference-word-option"),
        logo.querySelector("#login-reference-word-helper"),
      ];
      const wordRects = wordElements.map((word) => word?.getBoundingClientRect?.()).filter(Boolean);
      const discRect = logo.querySelector(".login-splash__reference-disc")?.getBoundingClientRect?.();
      if (!context || !stageRect || wordRects.length !== 2 || !discRect) {
        fallback();
        return;
      }

      try {
        const left = Math.min(...wordRects.map((rect) => rect.left)) - 2;
        const top = Math.min(...wordRects.map((rect) => rect.top)) - 2;
        const right = Math.max(...wordRects.map((rect) => rect.right)) + 2;
        const bottom = Math.max(...wordRects.map((rect) => rect.bottom)) + 2;
        const sampleCanvas = document.createElement("canvas");
        sampleCanvas.width = Math.max(1, Math.round(right - left));
        sampleCanvas.height = Math.max(1, Math.round(bottom - top));
        const sampleContext = sampleCanvas.getContext?.("2d", { willReadFrequently: true });
        if (!sampleContext) {
          fallback();
          return;
        }
        const viewBox = logo.viewBox.baseVal;
        const logoRect = logo.getBoundingClientRect();
        const scale = logoRect.width / viewBox.width;
        sampleContext.fillStyle = "#000";
        wordElements.forEach((word) => {
          const matrix = word.transform.baseVal.consolidate().matrix;
          const path = word.querySelector("path");
          sampleContext.save();
          sampleContext.translate(logoRect.left - left, logoRect.top - top);
          sampleContext.scale(scale, scale);
          sampleContext.translate(-viewBox.x, -viewBox.y);
          sampleContext.transform(matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f);
          sampleContext.fill(new Path2D(path.getAttribute("d")));
          sampleContext.restore();
        });
        const pixels = sampleContext.getImageData(0, 0, sampleCanvas.width, sampleCanvas.height).data;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.max(1, Math.round(stageRect.width * dpr));
        canvas.height = Math.max(1, Math.round(stageRect.height * dpr));
        canvas.style.width = `${stageRect.width}px`;
        canvas.style.height = `${stageRect.height}px`;
        context.setTransform(dpr, 0, 0, dpr, 0, 0);

        const originX = discRect.left + discRect.width / 2 - stageRect.left;
        const originY = discRect.top + discRect.height / 2 - stageRect.top;
        const particles = [];
        for (let y = 0; y < sampleCanvas.height; y += particleStep) {
          for (let x = 0; x < sampleCanvas.width; x += particleStep) {
            if (pixels[(y * sampleCanvas.width + x) * 4 + 3] <= 60) continue;
            const targetX = left - stageRect.left + x;
            const targetY = top - stageRect.top + y;
            const angle = Math.atan2(targetY - originY, targetX - originX) + (Math.random() - .5) * 1.5;
            const spread = (.45 + Math.random() * 1.25) * discRect.height * .3;
            particles.push({
              targetX,
              targetY,
              startX: originX + Math.cos(angle) * spread,
              startY: originY + Math.sin(angle) * spread,
              delay: ((targetX - (left - stageRect.left)) / (right - left)) * stagger,
              bend: (Math.random() - .5) * .16,
              size: .55 + Math.random() * .85,
              speed: .88 + Math.random() * .26,
            });
          }
        }
        if (!particles.length) {
          assemble();
          return;
        }

        const glowSize = 24;
        const glow = document.createElement("canvas");
        glow.width = glowSize;
        glow.height = glowSize;
        const glowContext = glow.getContext?.("2d");
        if (!glowContext) {
          fallback();
          return;
        }
        const gradient = glowContext.createRadialGradient(
          glowSize / 2,
          glowSize / 2,
          0,
          glowSize / 2,
          glowSize / 2,
          glowSize / 2,
        );
        gradient.addColorStop(0, "#C8102E");
        gradient.addColorStop(.28, "#C8102E");
        gradient.addColorStop(1, "rgba(200,16,46,0)");
        glowContext.fillStyle = gradient;
        glowContext.beginPath();
        glowContext.arc(glowSize / 2, glowSize / 2, glowSize / 2, 0, Math.PI * 2);
        glowContext.fill();

        let firstFrame = 0;
        const frame = (now) => {
          if (stopped) return;
          if (!firstFrame) firstFrame = now;
          const elapsed = (now - firstFrame - startMs) / flyMs;
          if (elapsed < 0) {
            frameId = window.requestAnimationFrame(frame);
            return;
          }
          context.clearRect(0, 0, stageRect.width, stageRect.height);
          let moving = false;
          particles.forEach((particle) => {
            let progress = ((elapsed - particle.delay) / (1 - stagger)) * particle.speed;
            if (progress < 0) {
              progress = 0;
              moving = true;
            } else if (progress < 1) {
              moving = true;
            } else {
              progress = 1;
            }
            const eased = 1 - Math.pow(1 - progress, 5);
            const dx = particle.targetX - particle.startX;
            const dy = particle.targetY - particle.startY;
            const curve = Math.sin(eased * Math.PI) * particle.bend;
            const x = particle.startX + dx * eased - dy * curve;
            const y = particle.startY + dy * eased + dx * curve;
            const born = eased < .22 ? Math.pow(eased / .22, 2) : 1;
            const previousProgress = Math.max(0, progress - .16);
            const previousEased = 1 - Math.pow(1 - previousProgress, 5);
            const tailCurve = Math.sin(previousEased * Math.PI) * particle.bend;
            const tailX = particle.startX + dx * previousEased - dy * tailCurve;
            const tailY = particle.startY + dy * previousEased + dx * tailCurve;
            const velocity = Math.pow(1 - eased, 4);
            context.globalAlpha = (.06 + .46 * velocity) * particle.size * born;
            context.strokeStyle = "#C8102E";
            context.lineWidth = .8 + .7 * particle.size;
            context.beginPath();
            context.moveTo(tailX, tailY);
            context.lineTo(x, y);
            context.stroke();
            const flashProgress = eased > .7 ? (eased - .7) / .3 : 0;
            const flash = flashProgress > 0 ? Math.sin(flashProgress * Math.PI) : 0;
            const glowRadius = (1.7 + .5 * (1 - Math.pow(1 - eased, 2)) + 3.2 * flash) * particle.size;
            context.globalAlpha = (
              .42 + .58 * Math.sin(Math.min(1, eased * 1.15) * Math.PI * .72)
            ) * (.5 + .5 * particle.size) * born;
            context.drawImage(
              glow,
              x - glowRadius,
              y - glowRadius,
              glowRadius * 2,
              glowRadius * 2,
            );
          });
          context.globalAlpha = 1;
          if (moving) frameId = window.requestAnimationFrame(frame);
          else assemble();
        };
        frameId = window.requestAnimationFrame(frame);
      } catch (_error) {
        fallback();
      }
    };

    const activate = (preparedLogo) => {
      if (stopped || active) return;
      active = true;
      logo = preparedLogo;
      stage.append(canvas, logo);
      splash.classList.remove("is-fallback");
      splash.classList.add("has-reference-animation");
      drawSparks();
    };

    const fallback = () => {
      if (stopped) return;
      stopped = true;
      canvas.remove();
      logo?.remove();
      splash.classList.remove("has-reference-animation", "is-assembled");
      splash.classList.add("is-fallback");
    };

    fetch(logoAsset, {
      cache: "force-cache",
      credentials: "same-origin",
      signal: loadController.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error("Packaged logo unavailable");
        return response.text();
      })
      .then((svgText) => activate(prepareLogo(svgText)))
      .catch((error) => {
        if (error?.name !== "AbortError") fallback();
      });

    return {
      stop() {
        stopped = true;
        loadController.abort();
        if (frameId && typeof window.cancelAnimationFrame === "function") {
          window.cancelAnimationFrame(frameId);
        }
      },
    };
  };

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
  const referenceAnimation = mountReferenceAnimation();
  let finished = false;
  let timer = 0;
  const finish = () => {
    if (finished) return;
    finished = true;
    referenceAnimation.stop();
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

  timer = setTimeout(finish, 2200);
  document.addEventListener("keydown", finish);
  splash.addEventListener("click", finish);

  // The launch screen is never allowed to become a permanent application
  // state.  This independent watchdog still releases the login page if a
  // browser timer is throttled while the WebKit window is being activated.
  setTimeout(finish, 3120);
})();
