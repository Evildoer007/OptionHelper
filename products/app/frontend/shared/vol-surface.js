import { currentTheme, onThemeChange } from "/app/frontend/shared/theme.js";

/**
 * A deliberately quiet implied-volatility surface for the login page.
 * It starts only after the native startup animation releases the login form.
 */
export function initializeVolSurface(canvas) {
  if (!canvas || matchMedia("(prefers-reduced-motion: reduce)").matches) return null;

  const context = canvas.getContext("2d");
  if (!context) return null;

  const rowCount = 28;
  const columnCount = 46;
  const xBuffer = new Float32Array(Math.max(rowCount, columnCount) + 1);
  const yBuffer = new Float32Array(Math.max(rowCount, columnCount) + 1);
  const statistics = new Float32Array(3);
  const qualityLevels = [
    { rows: 1, columns: 2, pixelRatio: 1.25 },
    { rows: 1, columns: 2, pixelRatio: 1 },
    { rows: 2, columns: 3, pixelRatio: 1 },
    { rows: 2, columns: 4, pixelRatio: 1 },
  ];

  let width = 0;
  let height = 0;
  let pixelRatio = 1;
  let span = 1;
  let animationFrame = 0;
  let startedAt = 0;
  let previousFrame = 0;
  let previousMeasure = 0;
  let averageFrame = 16.7;
  let qualityIndex = 0;
  let qualityHold = 0;
  let yaw = -0.46;
  let targetYaw = yaw;
  let pitch = 0.92;
  let targetPitch = pitch;
  let pointerX = -1;
  let pointerY = -1;
  let pointerPull = 0;
  let dark = currentTheme() === "dark";

  function resize() {
    const displayPixelRatio = Math.min(window.devicePixelRatio || 1, 1.25);
    pixelRatio = Math.min(displayPixelRatio, qualityLevels[qualityIndex].pixelRatio);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = Math.round(width * pixelRatio);
    canvas.height = Math.round(height * pixelRatio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    span = Math.min(width * 0.98, height * 1.75) * 0.58;
  }

  function maturity(index) {
    return -0.18 + 2.63 * (index / rowCount);
  }

  function volatility(strike, maturityValue, clock) {
    const smile = 0.78 * strike * strike;
    const skew = -0.34 * strike;
    const term = 0.52 * Math.sqrt(maturityValue + 0.3);
    const swell = 0.21 * Math.sin(strike * 1.7 - maturityValue * 1.15 + clock * 0.42);
    const wave = (0.15 * Math.sin(strike * 3.1 + clock * 0.66) * Math.cos(maturityValue * 1.9 - clock * 0.47))
      + (0.08 * Math.sin((strike * 1.8 + maturityValue * 1.3) * 2.2 - clock * 0.33));
    return smile + skew + term + swell + wave;
  }

  function buildLine(isRow, fixed, pointCount, clock) {
    const cosineYaw = Math.cos(yaw);
    const sineYaw = Math.sin(yaw);
    const cosinePitch = Math.cos(pitch);
    const midpoint = pointCount >> 1;
    let maximumLift = 0;

    for (let index = 0; index <= pointCount; index += 1) {
      const strike = isRow ? -1 + (2 * index / columnCount) : fixed;
      const maturityValue = isRow ? fixed : maturity(index);
      const z = volatility(strike, maturityValue, clock) * span * 0.44;
      const x = strike * span * 2.35;
      const y = (maturityValue - 0.5) * span * 2.35;
      const rotatedX = x * cosineYaw - y * sineYaw;
      const rotatedY = x * sineYaw + y * cosineYaw;
      const depth = Math.min(2.6, Math.max(0.12, 1 / (1 + rotatedY / (span * 3.2))));
      const screenX = width / 2 + rotatedX * depth;
      let screenY = height * 0.46 + (rotatedY * cosinePitch * 0.58 - z) * depth;

      if (pointerPull > 0.01) {
        const deltaX = screenX - pointerX;
        const deltaY = screenY - pointerY;
        const lift = Math.exp(-((deltaX * deltaX + deltaY * deltaY) / (2 * 245 * 245))) * pointerPull;
        screenY -= 150 * lift * depth;
        maximumLift = Math.max(maximumLift, lift);
      }

      xBuffer[index] = screenX;
      yBuffer[index] = screenY;
      if (index === midpoint) {
        statistics[0] = depth;
        statistics[1] = z / (span * 0.44);
      }
    }

    statistics[2] = maximumLift;
  }

  function strokeLine(pointCount) {
    context.beginPath();
    context.moveTo(xBuffer[0], yBuffer[0]);
    for (let index = 1; index <= pointCount; index += 1) context.lineTo(xBuffer[index], yBuffer[index]);
    context.stroke();
  }

  function tone(highlight, alpha) {
    const safeAlpha = alpha < 0.001 ? 0 : alpha;
    const red = (dark ? 150 + 75 * highlight : 126 + 74 * highlight) | 0;
    const green = (dark ? 158 - 130 * highlight : 136 - 118 * highlight) | 0;
    const blue = (dark ? 172 - 118 * highlight : 150 - 106 * highlight) | 0;
    return `rgba(${red},${green},${blue},${(dark ? safeAlpha * 1.12 : safeAlpha).toFixed(3)})`;
  }

  function frame(now) {
    animationFrame = requestAnimationFrame(frame);
    if (!startedAt) {
      startedAt = now;
      previousFrame = now;
      previousMeasure = now;
    }

    const elapsed = (now - startedAt) / 1000;
    const deltaSeconds = Math.min(0.05, (now - previousFrame) / 1000);
    previousFrame = now;
    averageFrame += ((now - previousMeasure) - averageFrame) * 0.06;
    previousMeasure = now;

    if (qualityHold > 0) qualityHold -= 1;
    else if (averageFrame > 18.5 && qualityIndex < qualityLevels.length - 1) {
      const previousRatio = qualityLevels[qualityIndex].pixelRatio;
      qualityIndex += 1;
      qualityHold = 150;
      if (qualityLevels[qualityIndex].pixelRatio !== previousRatio) resize();
    }

    const cameraDamping = 1 - Math.exp(-2.8 * deltaSeconds);
    const pullDamping = 1 - Math.exp(-5.6 * deltaSeconds);
    yaw += (targetYaw - yaw) * cameraDamping;
    pitch += (targetPitch - pitch) * cameraDamping;
    pointerPull += (((pointerX < 0) ? 0 : 1) - pointerPull) * pullDamping;

    context.clearRect(0, 0, width, height);
    context.lineWidth = 1;
    context.lineJoin = "round";
    const quality = qualityLevels[qualityIndex];
    const clock = elapsed * 0.252;

    for (let row = 0; row <= rowCount; row += quality.rows) {
      buildLine(true, maturity(row), columnCount, clock);
      const highlight = Math.min(1, Math.max(0, (statistics[1] - 1.45) * 1.05 + statistics[2] * 0.75));
      const opacity = (0.095 + 0.155 * statistics[0]) * (0.42 + 0.58 * (1 - row / rowCount))
        * (1 + 2.6 * statistics[2]) * (1 + 0.28 * (quality.rows - 1));
      context.strokeStyle = tone(highlight, opacity);
      strokeLine(columnCount);
    }

    for (let column = 0; column <= columnCount; column += quality.columns) {
      buildLine(false, -1 + 2 * column / columnCount, rowCount, clock);
      const highlight = Math.min(1, Math.max(0, (statistics[1] - 1.45) * 1.05 + statistics[2] * 0.75));
      const opacity = (0.042 + 0.068 * statistics[0]) * (1 + 2.6 * statistics[2])
        * (1 + 0.22 * (quality.columns - 2));
      context.strokeStyle = tone(highlight, opacity);
      strokeLine(rowCount);
    }
  }

  function start() {
    if (!animationFrame) animationFrame = requestAnimationFrame(frame);
  }

  function stop() {
    if (!animationFrame) return;
    cancelAnimationFrame(animationFrame);
    animationFrame = 0;
  }

  function reveal() {
    canvas.classList.add("is-visible");
    start();
  }

  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("pointermove", (event) => {
    pointerX = event.clientX;
    pointerY = event.clientY;
    targetYaw = -0.46 + (event.clientX / window.innerWidth - 0.5) * 0.1;
    targetPitch = 0.92 - (event.clientY / window.innerHeight - 0.5) * 0.08;
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (canvas.classList.contains("is-visible")) start();
  });
  onThemeChange((theme) => { dark = theme === "dark"; });

  resize();
  return { reveal };
}
