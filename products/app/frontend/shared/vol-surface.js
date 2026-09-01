import { currentTheme, onThemeChange } from "/app/frontend/shared/theme.js";

/**
 * A deliberately quiet implied-volatility surface for the login page.
 * It starts only after the native startup animation releases the login form.
 */
export function initializeVolSurface(canvas) {
  if (!canvas || matchMedia("(prefers-reduced-motion: reduce)").matches) return null;

  const context = canvas.getContext("2d");
  if (!context) return null;

  const rowCount = 34;
  const columnCount = 58;
  const gridWidth = columnCount + 1;
  const gridHeight = rowCount + 1;
  const gridSize = gridWidth * gridHeight;
  const gridX = new Float32Array(gridSize);
  const gridY = new Float32Array(gridSize);
  const gridZ = new Float32Array(gridSize);
  const gridDepth = new Float32Array(gridSize);
  const gridLift = new Float32Array(gridSize);
  const statistics = new Float32Array(3);
  const qualityLevels = [
    { rows: 1, columns: 2 },
    { rows: 1, columns: 3 },
    { rows: 2, columns: 3 },
    { rows: 2, columns: 4 },
  ];
  const strikeStep = 2 / columnCount;
  const maturityStep = 2.63 / rowCount;
  const heightToWorld = 0.44 / 2.35;
  const light = normalize(-0.42, -0.55, 0.72);
  const halfVector = normalize(light.x, light.y, light.z + 1);

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

  function normalize(x, y, z) {
    const length = Math.hypot(x, y, z);
    return { x: x / length, y: y / length, z: z / length };
  }

  function clamp01(value) {
    return Math.min(1, Math.max(0, value));
  }

  function resize() {
    pixelRatio = Math.min(window.devicePixelRatio || 1, 1.25);
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

  function buildGrid(clock) {
    const cosineYaw = Math.cos(yaw);
    const sineYaw = Math.sin(yaw);
    const cosinePitch = Math.cos(pitch);
    let index = 0;

    for (let row = 0; row < gridHeight; row += 1) {
      const maturityValue = maturity(row);
      for (let column = 0; column < gridWidth; column += 1, index += 1) {
        const strike = -1 + (2 * column / columnCount);
        const z = volatility(strike, maturityValue, clock) * span * 0.44;
        const x = strike * span * 2.35;
        const y = (maturityValue - 0.5) * span * 2.35;
        const rotatedX = x * cosineYaw - y * sineYaw;
        const rotatedY = x * sineYaw + y * cosineYaw;
        const depth = Math.min(2.6, Math.max(0.12, 1 / (1 + rotatedY / (span * 3.2))));
        const screenX = width / 2 + rotatedX * depth;
        let screenY = height * 0.46 + (rotatedY * cosinePitch * 0.58 - z) * depth;
        let lift = 0;

        if (pointerPull > 0.01) {
          const deltaX = screenX - pointerX;
          const deltaY = screenY - pointerY;
          lift = Math.exp(-((deltaX * deltaX + deltaY * deltaY) / (2 * 245 * 245))) * pointerPull;
          screenY -= 150 * lift * depth;
        }

        gridX[index] = screenX;
        gridY[index] = screenY;
        gridZ[index] = z / (span * 0.44);
        gridDepth[index] = depth;
        gridLift[index] = lift;
      }
    }
  }

  function paintQuads(quality) {
    const rowStep = quality.rows;
    const columnStep = quality.columns > 2 ? 2 : 1;
    const direction = yaw < 0 ? 1 : -1;

    for (let row = rowCount - rowStep; row >= 0; row -= rowStep) {
      const firstColumn = direction > 0 ? 0 : columnCount - columnStep;
      const lastColumn = direction > 0 ? columnCount - columnStep : 0;
      for (let column = firstColumn; direction > 0 ? column <= lastColumn : column >= lastColumn; column += direction * columnStep) {
        const topLeft = row * gridWidth + column;
        const topRight = topLeft + columnStep;
        const bottomLeft = topLeft + rowStep * gridWidth;
        const bottomRight = bottomLeft + columnStep;
        const edgeX = gridX[topRight] - gridX[topLeft];
        const edgeY = gridY[topRight] - gridY[topLeft];
        const rowX = gridX[bottomLeft] - gridX[topLeft];
        const rowY = gridY[bottomLeft] - gridY[topLeft];

        if (Math.abs(edgeX * rowY - edgeY * rowX) < 0.5) continue;

        const left = column > 0 ? topLeft - 1 : topLeft;
        const right = column < columnCount - columnStep ? topRight + 1 : topRight;
        const up = row > 0 ? topLeft - gridWidth : topLeft;
        const down = row + rowStep < rowCount ? bottomLeft + gridWidth : bottomLeft;
        const normal = normalize(
          -((gridZ[right] - gridZ[left]) / (strikeStep * (columnStep + 1))) * heightToWorld,
          -((gridZ[down] - gridZ[up]) / (maturityStep * (rowStep + 1))) * heightToWorld,
          1,
        );
        const diffuse = Math.max(0, normal.x * light.x + normal.y * light.y + normal.z * light.z);
        let specular = Math.max(0, normal.x * halfVector.x + normal.y * halfVector.y + normal.z * halfVector.z);
        specular *= specular;
        specular *= specular;
        specular *= specular;

        const normalizedHeight = (gridZ[topLeft] + gridZ[topRight] + gridZ[bottomLeft] + gridZ[bottomRight]) * 0.25;
        const lift = (gridLift[topLeft] + gridLift[topRight] + gridLift[bottomLeft] + gridLift[bottomRight]) * 0.25;
        const depth = (gridDepth[topLeft] + gridDepth[topRight] + gridDepth[bottomLeft] + gridDepth[bottomRight]) * 0.25;
        const highlight = clamp01((normalizedHeight - 1.45) * 1.05 + lift * 0.75 + specular * 0.55);
        const shade = 0.34 + 0.66 * diffuse + 0.9 * specular;
        const fog = 0.45 + 0.55 * Math.min(1, depth);
        const alpha = (0.046 + 0.052 * depth) * shade * fog * (1 + 2.2 * lift);

        context.fillStyle = tone(highlight, alpha);
        context.beginPath();
        context.moveTo(gridX[topLeft], gridY[topLeft]);
        context.lineTo(gridX[topRight], gridY[topRight]);
        context.lineTo(gridX[bottomRight], gridY[bottomRight]);
        context.lineTo(gridX[bottomLeft], gridY[bottomLeft]);
        context.closePath();
        context.fill();
      }
    }
  }

  function traceGridLine(isRow, fixed) {
    const pointCount = isRow ? columnCount : rowCount;
    const midpoint = pointCount >> 1;
    let maximumLift = 0;

    context.beginPath();
    for (let point = 0; point <= pointCount; point += 1) {
      const index = isRow ? fixed * gridWidth + point : point * gridWidth + fixed;
      if (point === 0) context.moveTo(gridX[index], gridY[index]);
      else context.lineTo(gridX[index], gridY[index]);
      maximumLift = Math.max(maximumLift, gridLift[index]);
      if (point === midpoint) {
        statistics[0] = gridDepth[index];
        statistics[1] = gridZ[index];
      }
    }
    statistics[2] = maximumLift;
    return clamp01((statistics[1] - 1.45) * 1.05 + statistics[2] * 0.75);
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
    if (!startedAt) startedAt = now;
    if (!previousFrame) {
      previousFrame = now;
      previousMeasure = now;
    }

    const elapsed = (now - startedAt) / 1000;
    const deltaSeconds = Math.min(0.05, (now - previousFrame) / 1000);
    previousFrame = now;
    averageFrame += ((now - previousMeasure) - averageFrame) * 0.02;
    previousMeasure = now;

    if (qualityHold > 0) qualityHold -= 1;
    else if (averageFrame > 21 && qualityIndex < qualityLevels.length - 1) {
      qualityIndex += 1;
      qualityHold = 300;
    } else if (averageFrame < 9.5 && qualityIndex > 0) {
      qualityIndex -= 1;
      qualityHold = 300;
    }

    const cameraDamping = 1 - Math.exp(-2.8 * deltaSeconds);
    const pullDamping = 1 - Math.exp(-5.6 * deltaSeconds);
    yaw += (targetYaw - yaw) * cameraDamping;
    pitch += (targetPitch - pitch) * cameraDamping;
    pointerPull += (((pointerX < 0) ? 0 : 1) - pointerPull) * pullDamping;

    context.clearRect(0, 0, width, height);
    context.lineWidth = 0.72;
    context.lineJoin = "round";
    const quality = qualityLevels[qualityIndex];
    const clock = elapsed * 0.252;
    buildGrid(clock);
    paintQuads(quality);

    for (let row = 0; row <= rowCount; row += quality.rows) {
      const highlight = traceGridLine(true, row);
      const opacity = (0.044 + 0.074 * statistics[0]) * (0.42 + 0.58 * (1 - row / rowCount))
        * (1 + 2.6 * statistics[2]) * (1 + 0.28 * (quality.rows - 1));
      context.strokeStyle = tone(highlight, opacity);
      context.stroke();
    }

    for (let column = 0; column <= columnCount; column += quality.columns) {
      const highlight = traceGridLine(false, column);
      const opacity = (0.021 + 0.034 * statistics[0]) * (1 + 2.6 * statistics[2])
        * (1 + 0.22 * (quality.columns - 2));
      context.strokeStyle = tone(highlight, opacity);
      context.stroke();
    }
  }

  function start() {
    if (animationFrame) return;
    previousFrame = 0;
    previousMeasure = 0;
    animationFrame = requestAnimationFrame(frame);
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
  window.addEventListener("pointerleave", () => {
    pointerX = -1;
    pointerY = -1;
    targetYaw = -0.46;
    targetPitch = 0.92;
  }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (canvas.classList.contains("is-visible")) start();
  });
  onThemeChange((theme) => { dark = theme === "dark"; });

  resize();
  return { reveal };
}
