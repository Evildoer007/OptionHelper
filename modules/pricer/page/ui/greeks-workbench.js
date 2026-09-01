(function registerGreeksWorkbench(global) {
  'use strict';

  const GREEKS = {
    delta: { label: 'Delta', surfaceKey: 'delta_surface' },
    gamma: { label: 'Gamma', surfaceKey: 'gamma_surface' },
    theta: { label: 'Theta', surfaceKey: 'theta_surface' },
    vega: { label: 'Vega', surfaceKey: 'vega_surface' },
    rho: { label: 'Rho', surfaceKey: 'rho_surface' },
  };
  const SURFACE_VIEWS = new Set(['3d', 'heatmap', 'table']);
  const DEFAULT_CAMERA = Object.freeze({
    eye: { x: 1.45, y: -1.55, z: 1.08 },
    center: { x: 0, y: 0, z: -.08 },
    up: { x: 0, y: 0, z: 1 },
    projection: { type: 'orthographic' },
  });

  function finite(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function riskPercentDigits(percentValue) {
    const absolute = Math.abs(percentValue);
    if (absolute === 0 || absolute >= 1) return 2;
    if (absolute >= .1) return 3;
    if (absolute >= .01) return 4;
    if (absolute >= .001) return 5;
    return Math.min(10, Math.max(6, Math.ceil(-Math.log10(absolute)) + 2));
  }

  function formatRiskPercent(value) {
    if (!finite(value)) return '不适用';
    const percentValue = Object.is(value, -0) ? 0 : value * 100;
    return `${percentValue.toLocaleString('zh-CN', {
      maximumFractionDigits: riskPercentDigits(percentValue),
      useGrouping: false,
    })}%`;
  }

  function formatRiskValue(value, unit, formatUnit) {
    const formatted = formatRiskPercent(value);
    if (formatted === '不适用' || !unit) return formatted;
    const unitLabel = formatUnit(unit);
    if (!unitLabel || unitLabel === '%' || unitLabel === '百分比') return formatted;
    return unitLabel.startsWith('%') ? `${formatted}${unitLabel.slice(1)}` : `${formatted} ${unitLabel}`;
  }

  function formatSensitivity(value, formatUnit) {
    if (value === null || value === undefined) return '不适用';
    if (typeof value === 'object' && value.status === 'not_applicable') {
      return `不适用${value.reason ? `：${value.reason}` : ''}`;
    }
    const number = typeof value === 'object' ? value.value : value;
    const unit = typeof value === 'object' ? value.unit : null;
    return formatRiskValue(number, unit, formatUnit);
  }

  function formatAxisValue(value, unit, formatNumber) {
    if (!finite(value)) return '不适用';
    if (unit === 'decimal' || unit === 'decimal_rate') return formatRiskPercent(value);
    return formatNumber(value, 4);
  }

  function pointGrid(surface) {
    const xs = surface?.x_axis?.values || [];
    const ys = surface?.y_axis?.values || [];
    const matrix = Array.from({ length: ys.length }, () => Array(xs.length).fill(null));
    const points = [];
    for (const item of surface?.data || []) {
      const [xIndex, yIndex, value] = item.value || [];
      if (!Number.isInteger(xIndex) || !Number.isInteger(yIndex)) continue;
      if (yIndex >= matrix.length || xIndex >= xs.length) continue;
      matrix[yIndex][xIndex] = finite(value) ? value : null;
      points.push({
        xIndex,
        yIndex,
        x: xs[xIndex],
        y: ys[yIndex],
        value: finite(value) ? value : null,
        status: item.status || (finite(value) ? 'ok' : 'not_applicable'),
        reason: item.reason || '',
      });
    }
    return { xs, ys, matrix, points };
  }

  function valueDomain(points) {
    const values = points.map((point) => point.value).filter(finite);
    if (!values.length) return null;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = min === max ? Math.max(Math.abs(min) * .02, 1e-8) : 0;
    return { min: min - padding, max: max + padding, values };
  }

  function create(options) {
    const {
      document: documentRef,
      plotly,
      getElement,
      formatNumber,
      formatUnit,
      escapeHtml,
    } = options;
    const state = {
      activeGreek: 'delta',
      activeCurveKey: null,
      surfaceView: '3d',
      pricing: null,
      renderRevision: 0,
      visible: true,
      pendingRender: false,
    };

    const purge = (id) => plotly.purge(getElement(id));
    const wheelBindings = ['greekCurveChart', 'greekSurfaceChart'].map((id) => {
      const node = getElement(id);
      const handler = (event) => {
        if (!node._fullLayout || event.cancelable === false) return;
        // Plotly仍接收同一个wheel事件完成缩放；这里只阻止外层滚动区
        // 同时移动，避免用户缩放风险图时丢失当前分析位置。
        event.preventDefault();
      };
      node.addEventListener('wheel', handler, { passive: false, capture: true });
      return { node, handler };
    });

    function setEmpty(id, message) {
      const node = getElement(id);
      purge(id);
      node.hidden = false;
      node.classList.add('is-empty');
      node.innerHTML = `<div class="chart-empty">${escapeHtml(message)}</div>`;
    }

    function curveCandidates() {
      return (state.pricing?.risk_curves || []).filter((curve) => curve.key?.startsWith(`${state.activeGreek}_`));
    }

    function selectedSurface() {
      const key = GREEKS[state.activeGreek].surfaceKey;
      return (state.pricing?.risk_surfaces || []).find((surface) => surface.key === key) || null;
    }

    function compactRange(values, unit) {
      const numbers = (values || []).filter(finite);
      if (!numbers.length) return '不适用';
      const minimum = Math.min(...numbers);
      const maximum = Math.max(...numbers);
      const suffix = unit && !['decimal', 'decimal_rate'].includes(unit) ? ` ${formatUnit(unit)}` : '';
      return `${formatAxisValue(minimum, unit, formatNumber)}–${formatAxisValue(maximum, unit, formatNumber)}${suffix}，${numbers.length}点`;
    }

    function updateDomainSummary() {
      const summary = getElement('riskDomainSummary');
      const spotCurve = (state.pricing?.risk_curves || []).find((curve) => curve.key?.endsWith('_spot'));
      const volatilityCurve = (state.pricing?.risk_curves || []).find((curve) => curve.key?.endsWith('_volatility'));
      const rateCurve = (state.pricing?.risk_curves || []).find((curve) => curve.key?.endsWith('_rate'));
      const surface = selectedSurface() || (state.pricing?.risk_surfaces || [])[0];
      if (!state.pricing || !spotCurve || !surface) {
        summary.innerHTML = '<span>运行后展示风险范围、节点数和合同关键价格。</span>';
        return;
      }
      const annotations = spotCurve.annotations || surface.annotations || [];
      const scheduleCount = annotations.filter((item) => item.kind === 'schedule').length;
      const primary = annotations.filter((item) => item.kind !== 'schedule').map((item) => `${item.label} ${formatNumber(item.axis_value, 4)}`);
      if (scheduleCount) primary.push(`分期条款 ${scheduleCount}个`);
      const cells = [
        ['Spot曲线', compactRange((spotCurve.points || []).map((point) => point.x), spotCurve.x_axis?.unit)],
        [`${surface.x_axis?.name || '风险因子'}×期限`, `${compactRange(surface.x_axis?.values, surface.x_axis?.unit)}；${compactRange(surface.y_axis?.values, surface.y_axis?.unit)}`],
        ['波动率', compactRange((volatilityCurve?.points || []).map((point) => point.x), volatilityCurve?.x_axis?.unit)],
        ['利率', compactRange((rateCurve?.points || []).map((point) => point.x), rateCurve?.x_axis?.unit)],
        ['关键位置', primary.join('；') || '无合同价格条款'],
      ];
      summary.innerHTML = cells.map(([label, value]) => `<span><b>${escapeHtml(label)}</b>${escapeHtml(value)}</span>`).join('');
    }

    function updateGreekSelection() {
      for (const button of documentRef.querySelectorAll('[data-greek]')) {
        const selected = button.dataset.greek === state.activeGreek;
        button.setAttribute('aria-selected', String(selected));
        button.tabIndex = selected ? 0 : -1;
      }
      const meta = GREEKS[state.activeGreek];
      getElement('activeGreekName').textContent = meta.label;
      getElement('activeGreekValue').textContent = state.pricing
        ? formatSensitivity(state.pricing.greeks?.[state.activeGreek], formatUnit)
        : '未运行';
      getElement('greekWorkbenchCopy').textContent = !state.pricing
        ? `运行定价后展示${meta.label}的真实曲线与曲面。`
        : state.pricing.status === 'priced'
          ? `${meta.label}的曲线与曲面均来自本次定价结果；切换视图不会重新估值。`
          : `${meta.label}未提供可用风险输出，不生成替代数据。`;
    }

    function moveTabFocus(buttons, currentButton, event, onSelect) {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const available = buttons.filter((button) => !button.disabled);
      if (!available.length) return;
      const current = Math.max(0, available.indexOf(currentButton));
      const next = event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? available.length - 1
          : (current + (event.key === 'ArrowRight' ? 1 : -1) + available.length) % available.length;
      onSelect(available[next]);
      available[next].focus();
    }

    function buildCurveTabs(curves) {
      const tabs = getElement('curveFactorTabs');
      tabs.replaceChildren();
      if (!curves.length) return;
      if (!curves.some((curve) => curve.key === state.activeCurveKey)) state.activeCurveKey = curves[0].key;
      for (const curve of curves) {
        const button = documentRef.createElement('button');
        button.type = 'button';
        button.dataset.curveKey = curve.key;
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-controls', 'greekCurveChart');
        button.setAttribute('aria-selected', String(curve.key === state.activeCurveKey));
        button.tabIndex = curve.key === state.activeCurveKey ? 0 : -1;
        button.textContent = curve.x_axis?.name || curve.name;
        const select = () => {
          state.activeCurveKey = curve.key;
          renderCurve();
        };
        button.addEventListener('click', select);
        button.addEventListener('keydown', (event) => {
          moveTabFocus([...tabs.children], button, event, (nextButton) => {
            state.activeCurveKey = nextButton.dataset.curveKey;
            renderCurve();
          });
        });
        tabs.append(button);
      }
    }

    function renderCurve() {
      const curves = curveCandidates();
      buildCurveTabs(curves);
      const curve = curves.find((candidate) => candidate.key === state.activeCurveKey) || curves[0];
      if (!curve) {
        getElement('curveAxisText').textContent = '';
        const message = !state.pricing
          ? `运行定价后展示${GREEKS[state.activeGreek].label}的真实风险曲线。`
          : state.pricing.status === 'priced'
            ? `${GREEKS[state.activeGreek].label}当前结果仅提供点值，没有可用风险曲线。`
            : `${GREEKS[state.activeGreek].label}本次没有可用风险曲线，不生成替代数据。`;
        setEmpty('greekCurveChart', message);
        return;
      }
      const points = (curve.points || []).filter((point) => finite(point.x));
      const usable = points.filter((point) => finite(point.y));
      getElement('curveAxisText').textContent = `X：${curve.x_axis.name}（${formatUnit(curve.x_axis.unit)}）；Y：${curve.y_axis.name}（${formatUnit(curve.y_axis.unit)}）`;
      if (!usable.length) {
        setEmpty('greekCurveChart', points.find((point) => point.reason)?.reason || '当前曲线没有可用真实风险点。');
        return;
      }
      const node = getElement('greekCurveChart');
      node.hidden = false;
      node.classList.remove('is-empty');
      if (!node._fullLayout) node.replaceChildren();
      const palette = plotly.colors();
      const marketAxis = (curve.annotations || []).find((annotation) => annotation.kind === 'market' && finite(annotation.axis_value))?.axis_value;
      const marketPoint = finite(marketAxis)
        ? usable.reduce((closest, point) => !closest || Math.abs(point.x - marketAxis) < Math.abs(closest.x - marketAxis) ? point : closest, null)
        : null;
      const traces = [{
        type: 'scatter',
        mode: 'lines',
        name: curve.name,
        x: points.map((point) => point.x),
        y: points.map((point) => point.y),
        customdata: points.map((point) => [formatAxisValue(point.x, curve.x_axis.unit, formatNumber), formatRiskValue(point.y, curve.y_axis.unit, formatUnit)]),
        line: { color: palette.primary, width: 2.6, shape: 'spline', smoothing: .72, simplify: false },
        connectgaps: false,
        hovertemplate: `<b>${escapeHtml(curve.name)}</b><br>${escapeHtml(curve.x_axis.name)}：%{customdata[0]}<br>${escapeHtml(curve.y_axis.name)}：%{customdata[1]}<extra></extra>`,
      }];
      if (marketPoint) {
        traces.push({
          type: 'scatter',
          mode: 'markers',
          name: '当前市场点',
          x: [marketPoint.x],
          y: [marketPoint.y],
          customdata: [[formatAxisValue(marketPoint.x, curve.x_axis.unit, formatNumber), formatRiskValue(marketPoint.y, curve.y_axis.unit, formatUnit)]],
          marker: { color: palette.risk, size: 9, line: { color: palette.paper, width: 2 } },
          hovertemplate: `<b>当前市场点</b><br>${escapeHtml(curve.x_axis.name)}：%{customdata[0]}<br>${escapeHtml(curve.y_axis.name)}：%{customdata[1]}<extra></extra>`,
        });
      }
      const yValues = usable.map((point) => point.y);
      const crossesZero = Math.min(...yValues) < 0 && Math.max(...yValues) > 0;
      const layout = plotly.cartesianLayout({
        margin: { l: 72, r: 28, t: 18, b: 58 },
        xaxis: plotly.axis({ title: formatUnit(curve.x_axis.unit) }),
        yaxis: plotly.axis({ title: formatUnit(curve.y_axis.unit) }),
        shapes: crossesZero ? [{ type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: 0, y1: 0, line: { color: palette.rule, width: 1 } }] : [],
        uirevision: `curve-${state.activeGreek}-${curve.key}`,
      });
      plotly.render(node, traces, layout).catch((error) => {
        console.error('Pricer Plotly curve render failed.', error);
        setEmpty('greekCurveChart', '风险曲线渲染失败，请切换模块后重试。');
      });
    }

    function syncSurfaceButtons() {
      const available = Boolean(selectedSurface());
      for (const button of documentRef.querySelectorAll('[data-surface-view]')) {
        const selected = button.dataset.surfaceView === state.surfaceView;
        button.disabled = !available;
        button.tabIndex = selected ? 0 : -1;
        button.setAttribute('aria-selected', String(selected));
      }
    }

    function renderSurfaceTable(surface, grid, domain) {
      purge('greekSurfaceChart');
      getElement('greekSurfaceChart').hidden = true;
      const table = getElement('surfaceDataTable');
      table.hidden = false;
      getElement('surfaceTableHead').innerHTML = `<tr><th id="surfaceValueHeading">剩余期限</th>${grid.xs.map((value) => `<th>${escapeHtml(formatAxisValue(value, surface.x_axis.unit, formatNumber))}</th>`).join('')}</tr>`;
      const byCoordinate = new Map(grid.points.map((point) => [`${point.xIndex}:${point.yIndex}`, point]));
      getElement('surfaceTableBody').innerHTML = grid.ys.map((yValue, yIndex) => {
        const cells = grid.xs.map((_xValue, xIndex) => {
          const point = byCoordinate.get(`${xIndex}:${yIndex}`);
          if (!point || point.value === null) {
            const reason = point?.reason ? ` title="${escapeHtml(point.reason)}"` : '';
            return `<td data-status="not_applicable"${reason}>不适用</td>`;
          }
          const scale = Math.max(Math.abs(domain.min), Math.abs(domain.max), Number.EPSILON);
          const magnitude = Math.min(1, Math.abs(point.value) / scale);
          const tone = point.value < 0 ? 'var(--chart-negative)' : 'var(--chart-positive)';
          const alpha = `${Math.round(5 + magnitude * 28)}%`;
          return `<td data-risk-tone style="--risk-tone:${tone};--risk-alpha:${alpha}">${escapeHtml(formatRiskValue(point.value, surface.z_axis.unit, formatUnit))}</td>`;
        }).join('');
        return `<tr><td>${escapeHtml(formatNumber(yValue, 2))}日</td>${cells}</tr>`;
      }).join('') || `<tr><td colspan="${grid.xs.length + 1}" class="empty">当前曲面没有可用数据</td></tr>`;
    }

    const surfaceHover = (surface) => `<b>${escapeHtml(surface.z_axis.name)}</b><br>${escapeHtml(surface.x_axis.name)}：%{x}<br>${escapeHtml(surface.y_axis.name)}：%{y}日<br>数值：%{customdata}<extra></extra>`;

    function renderHeatmap(surface, grid, domain) {
      const node = getElement('greekSurfaceChart');
      const trace = {
        type: 'heatmap',
        x: grid.xs,
        y: grid.ys,
        z: grid.matrix,
        customdata: grid.matrix.map((row) => row.map((value) => formatRiskValue(value, surface.z_axis.unit, formatUnit))),
        zmin: domain.min,
        zmax: domain.max,
        colorscale: plotly.valueColorscale(domain.min, domain.max),
        zsmooth: 'best',
        connectgaps: false,
        hoverongaps: false,
        hovertemplate: surfaceHover(surface),
        colorbar: { title: { text: formatUnit(surface.z_axis.unit), side: 'right', font: plotly.font(12) }, thickness: 10, len: .72, outlinewidth: 0, tickfont: plotly.font(11) },
      };
      const layout = plotly.cartesianLayout({
        margin: { l: 76, r: 92, t: 18, b: 60 },
        xaxis: { ...plotly.axis({ title: formatUnit(surface.x_axis.unit) }), nticks: 7 },
        yaxis: { ...plotly.axis({ title: formatUnit(surface.y_axis.unit) }), nticks: 7 },
        uirevision: `heatmap-${state.activeGreek}-${surface.key}`,
      });
      return plotly.render(node, [trace], layout);
    }

    function currentSurfacePoint(surface, grid) {
      const marketAxis = (surface.annotations || []).find((annotation) => annotation.kind === 'market' && finite(annotation.axis_value))?.axis_value;
      if (!finite(marketAxis) || !grid.xs.length || !grid.ys.length) return null;
      const xIndex = grid.xs.reduce((best, value, index) => Math.abs(value - marketAxis) < Math.abs(grid.xs[best] - marketAxis) ? index : best, 0);
      const yIndex = grid.ys.reduce((best, value, index) => value > grid.ys[best] ? index : best, 0);
      const value = grid.matrix[yIndex]?.[xIndex];
      return finite(value) ? { x: grid.xs[xIndex], y: grid.ys[yIndex], z: value } : null;
    }

    function sceneAxis(title) {
      const palette = plotly.colors();
      return {
        title: { text: title, font: plotly.font(12) },
        tickfont: plotly.font(11),
        showgrid: false,
        zeroline: false,
        showbackground: false,
        showspikes: false,
        showline: true,
        linecolor: palette.rule,
        ticks: 'outside',
        tickcolor: palette.rule,
      };
    }

    function render3d(surface, grid, domain, revision) {
      const node = getElement('greekSurfaceChart');
      const palette = plotly.colors();
      const traces = [{
        type: 'surface',
        x: grid.xs,
        y: grid.ys,
        z: grid.matrix,
        customdata: grid.matrix.map((row) => row.map((value) => formatRiskValue(value, surface.z_axis.unit, formatUnit))),
        cmin: domain.min,
        cmax: domain.max,
        colorscale: plotly.valueColorscale(domain.min, domain.max),
        hovertemplate: surfaceHover(surface),
        connectgaps: false,
        contours: { x: { show: false }, y: { show: false }, z: { show: false } },
        lighting: { ambient: .86, diffuse: .58, specular: 0, roughness: 1, fresnel: 0 },
        colorbar: { title: { text: formatUnit(surface.z_axis.unit), side: 'right', font: plotly.font(12) }, thickness: 10, len: .66, outlinewidth: 0, tickfont: plotly.font(11) },
      }];
      const current = currentSurfacePoint(surface, grid);
      if (current) {
        traces.push({
          type: 'scatter3d',
          mode: 'markers',
          name: '当前市场点',
          x: [current.x],
          y: [current.y],
          z: [current.z],
          customdata: [[formatRiskValue(current.z, surface.z_axis.unit, formatUnit)]],
          marker: { color: palette.risk, size: 5, line: { color: palette.paper, width: 1 } },
          hovertemplate: `<b>当前市场点</b><br>${escapeHtml(surface.x_axis.name)}：%{x}<br>${escapeHtml(surface.y_axis.name)}：%{y}日<br>数值：%{customdata[0]}<extra></extra>`,
          showlegend: false,
        });
      }
      const layout = {
        autosize: true,
        margin: { l: 0, r: 34, t: 8, b: 0 },
        paper_bgcolor: 'rgba(0,0,0,0)',
        font: plotly.font(12, palette.ink),
        showlegend: false,
        hoverlabel: { bgcolor: palette.paper, bordercolor: palette.rule, font: plotly.font(12, palette.ink) },
        uirevision: `surface-${state.activeGreek}-${surface.key}`,
        scene: {
          bgcolor: 'rgba(0,0,0,0)',
          dragmode: 'orbit',
          aspectmode: 'manual',
          aspectratio: { x: 1.5, y: 1, z: .68 },
          camera: DEFAULT_CAMERA,
          xaxis: sceneAxis(surface.x_axis.name),
          yaxis: sceneAxis(`${surface.y_axis.name}（日）`),
          zaxis: sceneAxis(surface.z_axis.name),
        },
      };
      return plotly.render(node, traces, layout).then(() => {
        if (revision === state.renderRevision) plotly.watchWebglLoss(node, () => fallbackSurface('3D上下文已失效，已切换热力图'));
      });
    }

    function fallbackSurface(message) {
      if (state.surfaceView !== '3d') return;
      state.surfaceView = 'heatmap';
      getElement('surfaceRenderStatus').textContent = message;
      syncSurfaceButtons();
      renderSurface(message);
    }

    function renderSurface(statusMessage = '') {
      const revision = ++state.renderRevision;
      const surface = selectedSurface();
      syncSurfaceButtons();
      const chartNode = getElement('greekSurfaceChart');
      const tableNode = getElement('surfaceDataTable');
      const resetButton = getElement('resetSurfaceView');
      tableNode.hidden = true;
      chartNode.hidden = false;
      resetButton.hidden = state.surfaceView !== '3d';
      if (!surface) {
        getElement('surfaceAxisText').textContent = '';
        getElement('surfaceRenderStatus').textContent = '';
        setEmpty('greekSurfaceChart', !state.pricing
          ? `运行定价后展示${GREEKS[state.activeGreek].label}的真实风险曲面。`
          : `${GREEKS[state.activeGreek].label}当前结果没有可用曲面，不生成替代数据。`);
        return;
      }
      const grid = pointGrid(surface);
      const domain = valueDomain(grid.points);
      getElement('surfacePanelTitle').textContent = `${surface.x_axis.name}×剩余期限曲面`;
      getElement('surfaceAxisText').textContent = `X：${surface.x_axis.name}（${formatUnit(surface.x_axis.unit)}）；Y：${surface.y_axis.name}（${formatUnit(surface.y_axis.unit)}）；Z：${surface.z_axis.name}（${formatUnit(surface.z_axis.unit)}）`;
      if (!domain) {
        setEmpty('greekSurfaceChart', '当前曲面没有可用真实风险点。');
        return;
      }
      if (state.surfaceView === '3d' && !plotly.webglAvailable(documentRef)) {
        state.surfaceView = 'heatmap';
        getElement('surfaceRenderStatus').textContent = 'WebGL不可用，已切换热力图';
        syncSurfaceButtons();
        resetButton.hidden = true;
      } else {
        getElement('surfaceRenderStatus').textContent = statusMessage || (state.surfaceView === '3d' ? '固定视角；拖动旋转和平移，滚轮缩放' : '');
      }
      if (state.surfaceView === 'table') {
        renderSurfaceTable(surface, grid, domain);
        return;
      }
      chartNode.classList.remove('is-empty');
      if (!chartNode._fullLayout) chartNode.replaceChildren();
      const renderPromise = state.surfaceView === '3d'
        ? render3d(surface, grid, domain, revision)
        : renderHeatmap(surface, grid, domain);
      renderPromise.catch((error) => {
        if (revision !== state.renderRevision) return;
        console.error(`Pricer Plotly ${state.surfaceView} render failed.`, error);
        if (state.surfaceView === '3d') fallbackSurface('3D渲染失败，已切换热力图');
        else setEmpty('greekSurfaceChart', '热力图渲染失败，请切换模块后重试。');
      });
    }

    function render() {
      if (!state.visible) {
        state.pendingRender = true;
        return;
      }
      state.pendingRender = false;
      updateGreekSelection();
      updateDomainSummary();
      renderCurve();
      renderSurface();
    }

    function selectGreek(greek) {
      if (!Object.prototype.hasOwnProperty.call(GREEKS, greek)) return;
      state.activeGreek = greek;
      state.activeCurveKey = null;
      render();
    }

    for (const button of documentRef.querySelectorAll('[data-greek]')) {
      button.addEventListener('click', () => selectGreek(button.dataset.greek));
      button.addEventListener('keydown', (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const keys = Object.keys(GREEKS);
        const current = keys.indexOf(state.activeGreek);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length;
        selectGreek(keys[next]);
        documentRef.querySelector(`[data-greek="${keys[next]}"]`)?.focus();
      });
    }

    for (const button of documentRef.querySelectorAll('[data-surface-view]')) {
      const select = (nextButton = button) => {
        if (nextButton.disabled) return;
        const view = nextButton.dataset.surfaceView;
        if (!SURFACE_VIEWS.has(view)) return;
        state.surfaceView = view;
        renderSurface();
      };
      button.addEventListener('click', () => select());
      button.addEventListener('keydown', (event) => {
        moveTabFocus([...documentRef.querySelectorAll('[data-surface-view]')], button, event, select);
      });
    }

    getElement('resetSurfaceView').addEventListener('click', () => {
      const node = getElement('greekSurfaceChart');
      if (state.surfaceView === '3d' && node._fullLayout) global.Plotly?.relayout(node, { 'scene.camera': DEFAULT_CAMERA });
    });
    const themeObserver = typeof global.MutationObserver === 'function'
      ? new global.MutationObserver(() => { if (state.pricing) render(); })
      : null;
    themeObserver?.observe(documentRef.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const handleModuleVisibility = (event) => {
      state.visible = event.detail?.active !== false;
      if (!state.visible) return;
      if (state.pendingRender) render();
      else {
        plotly.resize(getElement('greekCurveChart'));
        if (state.surfaceView !== 'table') plotly.resize(getElement('greekSurfaceChart'));
      }
    };
    documentRef.addEventListener?.('optionhelper:modulevisibility', handleModuleVisibility);

    render();

    return {
      setPricing(pricing) {
        state.pricing = pricing || null;
        render();
      },
      resize() {
        if (!state.visible) {
          state.pendingRender = true;
          return;
        }
        plotly.resize(getElement('greekCurveChart'));
        if (state.surfaceView !== 'table') plotly.resize(getElement('greekSurfaceChart'));
      },
      dispose() {
        state.renderRevision += 1;
        purge('greekCurveChart');
        purge('greekSurfaceChart');
        for (const { node, handler } of wheelBindings) {
          node.removeEventListener?.('wheel', handler, { capture: true });
        }
        themeObserver?.disconnect();
        documentRef.removeEventListener?.('optionhelper:modulevisibility', handleModuleVisibility);
      },
      getState() {
        return { activeGreek: state.activeGreek, activeCurveKey: state.activeCurveKey, surfaceView: state.surfaceView };
      },
    };
  }

  global.PricerGreeksWorkbench = { create, formatRiskValue, formatSensitivity };
}(window));
