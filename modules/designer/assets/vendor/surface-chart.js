/* Frozen report grids use the workbench's Plotly scene without repricing. */
(() => {
  'use strict';
  const mounted = new WeakMap();
  const numberFormats = new Map();
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));

  function format(value, percent = false, axisDigits = 0) {
    if (!finite(value)) return '不适用';
    const number = (Object.is(value, -0) ? 0 : value) * (percent ? 100 : 1);
    const digits = number === 0 || Math.abs(number) >= 1 ? 2 : Math.min(10, Math.ceil(-Math.log10(Math.abs(number))) + 2);
    const precision = Math.max(digits, axisDigits);
    if (!numberFormats.has(precision)) {
      numberFormats.set(precision, new Intl.NumberFormat('zh-CN', {maximumFractionDigits: precision, useGrouping: false}));
    }
    return numberFormats.get(precision).format(number) + (percent ? '%' : '');
  }

  function grid(spec) {
    const x = spec.x || [], y = spec.y || [];
    const z = y.map(() => x.map(() => null));
    for (const [i, j, value] of spec.data || []) {
      if (Number.isInteger(i) && Number.isInteger(j) && i >= 0 && j >= 0 && i < x.length && j < y.length)
        z[j][i] = finite(value) ? value : null;
    }
    return {x, y, z};
  }

  function formatters(spec, presentation) {
    const rate = unit => ['decimal', 'decimal_rate'].includes(unit);
    function axis(values, name, unit) {
      const percent = rate(unit);
      if (!percent && presentation?.integerPriceLabels(values || [], name)) {
        return value => presentation.displayCategory(value, name, 0);
      }
      return value => finite(value) ? format(value, percent, 4) : String(value);
    }
    return {
      formatX: axis(spec.x, spec.x_axis_name, spec.x_axis_unit),
      formatY: axis(spec.y, spec.y_axis_name, spec.y_axis_unit),
      formatZ: value => format(value, spec.value_format === 'percent') + (spec.value_format === 'percent_points' ? '%' : '') + (spec.value_suffix || ''),
    };
  }

  function figure(spec, options = {}) {
    return figureFromGrid(spec, grid(spec), options);
  }

  function figureFromGrid(spec, values, options) {
    let current = null;
    if (finite(spec.reference_x) && values.x.length && values.y.length) {
      const i = values.x.reduce((best, value, index) => Math.abs(value - spec.reference_x) < Math.abs(values.x[best] - spec.reference_x) ? index : best, 0);
      const j = values.y.reduce((best, value, index) => value > values.y[best] ? index : best, 0);
      if (finite(values.z[j]?.[i])) current = {x: values.x[i], y: values.y[j], z: values.z[j][i]};
    }
    return window.OptionHelperPlotlyCharts.surfaceFigure({
      ...values, ...formatters(spec, window.OptionHelperChartPresentation), xTitle: spec.x_axis_name, yTitle: spec.y_axis_name,
      zTitle: (spec.z_axis_name || '').replace(/（百分比敏感度）$/, ''),
      current, revision: `surface-${spec.id}`,
      ...options,
    });
  }

  function surfaceLegend(doc, domain, colorscale, formatValue) {
    const legend = doc.createElement('div');
    legend.className = 'oh-surface-scale';
    legend.setAttribute('role', 'group');
    legend.setAttribute('aria-label', '颜色刻度');
    const bar = doc.createElement('div');
    bar.className = 'oh-surface-scale-bar';
    bar.style.setProperty('--surface-colors', colorscale.map(([value, color]) => `${color} ${value * 100}%`).join(', '));
    legend.append(bar);
    const flat = domain.values.every(value => value === domain.values[0]);
    const ticks = flat ? [[domain.values[0], .5]] : [[domain.min, 0], [domain.max, 1]];
    if (!flat && domain.min < 0 && domain.max > 0) {
      const zero = -domain.min / (domain.max - domain.min);
      if (zero >= .2 && zero <= .8) ticks.splice(1, 0, [0, zero]);
    }
    for (const [value, place] of ticks) {
      const label = doc.createElement('span');
      label.textContent = formatValue(value);
      label.style.setProperty('--tick-position', `${place * 100}%`);
      if (place === 0) label.className = 'oh-surface-scale-label--min';
      if (place === 1) label.className = 'oh-surface-scale-label--max';
      legend.append(label);
    }
    return legend;
  }

  function mount(host, spec, {onError} = {}) {
    const previous = mounted.get(host);
    let camera = previous?.camera() || null;
    previous?.dispose();
    const charts = window.OptionHelperPlotlyCharts;
    if (!charts?.surfaceFigure) throw new Error('plotly_surface_unavailable');
    const doc = host.ownerDocument;
    const outerDoc = document;
    const frame = doc !== outerDoc ? doc.defaultView?.frameElement : null;
    const rootWindow = outerDoc.defaultView || window;
    host.classList.add('chart--surface');
    host.setAttribute('role', 'group');
    host.replaceChildren();
    if (!doc.getElementById('optionhelper-surface-style')) {
      const style = doc.createElement('style'); style.id = 'optionhelper-surface-style';
      style.textContent = `
        .chart--surface{display:grid;grid-template-rows:auto minmax(0,1fr);min-width:0;height:clamp(330px,45vw,420px)!important}
        .oh-surface-tools{display:flex;flex-wrap:wrap;gap:4px;align-items:center;justify-content:flex-end;min-height:32px}
        .oh-surface-tools button{font:inherit;font-size:12px;line-height:1.4;color:inherit;background:transparent;border:1px solid transparent;border-radius:6px;padding:5px 8px;cursor:pointer}
        .oh-surface-tools button[aria-pressed=true]{background:var(--color-ground,#f1f2f1);color:var(--color-ink,#252525)}
        .oh-surface-tools button:hover{border-color:var(--color-rule,#d7dce0)}
        .oh-surface-tools button:focus-visible{outline:none;box-shadow:inset 0 0 0 100vmax color-mix(in srgb,var(--color-ink,#252525) 16%,transparent)}
        .oh-surface-tools button:disabled{opacity:.35;cursor:default}
        .oh-surface-content{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:minmax(0,1fr);min-width:0;min-height:0}
        .oh-surface-content[data-compact=true]{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr) auto}
        .oh-surface-content[data-table=true]{grid-template-columns:minmax(0,1fr);grid-template-rows:minmax(0,1fr)}
        .oh-surface-plot{position:relative;overflow:hidden;contain:layout paint;isolation:isolate;min-width:0;min-height:0}
        .oh-surface-plot .main-svg{position:absolute;inset:0;pointer-events:none}
        .oh-surface-plot .main-svg .draglayer{pointer-events:all}
        .oh-surface-plot .gl-container{position:absolute;inset:0}
        .oh-surface-scale{display:grid;grid-template-columns:9px auto;grid-template-rows:1fr;align-self:center;height:64%;column-gap:7px;margin:0 6px 0 4px;font-size:11px;line-height:1.2;color:var(--muted,#626a73)}
        .oh-surface-scale-bar{grid-column:1;background:linear-gradient(to top,var(--surface-colors))}
        .oh-surface-scale span{grid-row:1;grid-column:2;position:relative;align-self:start;top:calc(100% - var(--tick-position));transform:translateY(-50%);white-space:nowrap}
        .oh-surface-content[data-compact=true] .oh-surface-scale{grid-template-columns:1fr;grid-template-rows:7px 16px;row-gap:4px;margin:4px 14%;height:auto}
        .oh-surface-content[data-compact=true] .oh-surface-scale-bar{grid-row:1;background:linear-gradient(to right,var(--surface-colors))}
        .oh-surface-content[data-compact=true] .oh-surface-scale span{grid-row:2;grid-column:1;justify-self:start;top:0;left:var(--tick-position);transform:translateX(-50%)}
        .oh-surface-content[data-compact=true] .oh-surface-scale-label--min{transform:none}
        .oh-surface-content[data-compact=true] .oh-surface-scale-label--max{transform:translateX(-100%)}
        .oh-surface-table{min-width:0;min-height:0;overflow:auto}
        .oh-surface-table table{border-collapse:collapse;font-size:12px;white-space:nowrap;width:100%}
        .oh-surface-table th,.oh-surface-table td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--color-rule,#d7dce0)}
        .oh-surface-table th{position:sticky;top:0;background:var(--color-paper,#fff);font-weight:600}
        .oh-surface-content>[hidden]{display:none!important}
        @media print{.oh-surface-tools{display:none}}
      `;
      doc.head.append(style);
    }
    const toolbar = doc.createElement('div'); toolbar.className = 'oh-surface-tools';
    toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', '曲面视图');
    const content = doc.createElement('div'); content.className = 'oh-surface-content';
    const plot = doc.createElement('div'); plot.className = 'oh-surface-plot';
    const table = doc.createElement('div'); table.className = 'oh-surface-table'; table.hidden = true;
    const values = grid(spec), formats = formatters(spec);
    const domain = charts.surfaceDomain(values.z);
    const legend = domain && surfaceLegend(doc, domain, charts.valueColorscale(domain.min, domain.max, charts.colors(doc)), formats.formatZ);
    content.append(plot);
    if (legend) content.append(legend);
    content.append(table);
    host.append(toolbar, content);
    plot.addEventListener('wheel', event => {
      if (plot._fullLayout && event.cancelable) event.preventDefault();
    }, {capture: true, passive: false});
    const webgl = Boolean(domain) && charts.webglAvailable(outerDoc);
    let disposed = false, revision = 0, view = !domain ? 'table' : webgl ? '3d' : 'heatmap';
    let pending = Promise.resolve(), requested = '', drawn = null, onScreen = false;
    let unwatchLoss = null;
    const buttons = new Map();
    const reset = doc.createElement('button'); reset.type = 'button'; reset.title = '重置视角'; reset.setAttribute('aria-label', '重置视角');
    reset.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 4v6h6M3.7 9a8 8 0 1 1-.1 6"/></svg>';
    reset.addEventListener('click', () => {
      camera = charts.surfaceCamera();
      if (view === '3d') window.Plotly?.relayout(plot, {'scene.camera': camera});
    });
    function sync(compact) {
      if (view === 'table' && !table.firstChild) {
        table.innerHTML = `<table aria-label="${escape(spec.title || '曲面')}数据表"><thead><tr><th>${escape(spec.y_axis_name)}／${escape(spec.x_axis_name)}</th>${values.x.map(v => `<th scope="col">${escape(formats.formatX(v))}</th>`).join('')}</tr></thead><tbody>${values.y.map((v, j) => `<tr><th scope="row">${escape(formats.formatY(v))}</th>${values.z[j].map(value => `<td>${escape(formats.formatZ(value))}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
      }
      host.dataset.surfaceView = view;
      for (const [key, button] of buttons) button.setAttribute('aria-pressed', String(key === view));
      reset.disabled = view !== '3d';
      plot.hidden = view === 'table'; table.hidden = view !== 'table';
      if (legend) legend.hidden = view === 'table';
      content.dataset.compact = String(compact);
      content.dataset.table = String(view === 'table');
    }
    function discardPlot() {
      unwatchLoss?.();
      unwatchLoss = null;
      if (drawn?.view === '3d') camera = plot._fullLayout?.scene?.camera || camera;
      charts.purge(plot);
      drawn = null;
    }
    function displayed() {
      if (!onScreen || outerDoc.hidden || outerDoc.documentElement.dataset.optionhelperModuleActive === 'false') return false;
      if (!frame) return true;
      const bounds = host.getBoundingClientRect();
      const parent = frame.getBoundingClientRect();
      return parent.top + bounds.bottom > 0 && parent.top + bounds.top < rootWindow.innerHeight;
    }
    function render() {
      if (disposed) return pending;
      const compact = host.clientWidth < 540;
      sync(compact);
      const visible = displayed();
      const width = visible ? plot.clientWidth : 0, height = visible ? plot.clientHeight : 0;
      const layoutKey = `${view}/${compact}/${width}/${height}`;
      if (layoutKey === requested) return pending;
      requested = layoutKey;
      const activeRevision = ++revision;
      const kind = view;
      pending = pending.catch(() => {}).then(async () => {
        if (disposed || activeRevision !== revision) return;
        if (view !== drawn?.view || !width || !height) discardPlot();
        if (view === 'table' || !width || !height) return;
        const draw = () => {
          const picture = figureFromGrid(spec, values, {heatmap: kind === 'heatmap', width: host.clientWidth,
            document: doc, camera: camera || charts.surfaceCamera(),
            showColorbar: false});
          return charts.render(plot, picture.traces, {...picture.layout, autosize: false, width, height});
        };
        try {
          if (drawn) await window.Plotly.relayout(plot, {width, height});
          else {
            await draw();
            if (!plot._fullLayout || (kind === '3d' && !plot.querySelector('.gl-container canvas'))) {
              throw new Error('surface_plot_unavailable');
            }
            drawn = {view: kind};
          }
        }
        catch (error) {
          if (disposed || activeRevision !== revision) return;
          discardPlot();
          if (view === '3d') {
            view = 'heatmap';
          } else {
            view = 'table';
            onError?.(error);
          }
          // The fallback may have different available dimensions and grid
          // tracks. Queue it through the same render path.
          render();
          return;
        }
        if (disposed) { discardPlot(); return; }
        if (activeRevision !== revision || !drawn) return;
        unwatchLoss?.();
        unwatchLoss = drawn.view === '3d' ? charts.watchWebglLoss(plot, () => {
          if (disposed || view !== '3d') return;
          discardPlot();
          view = 'heatmap';
          render();
        }) : null;
      });
      return pending;
    }
    for (const [key, label] of [['3d', '3D'], ['heatmap', '热力图'], ['table', '数据表']]) {
      const button = doc.createElement('button'); button.type = 'button'; button.textContent = label;
      if (key !== 'table' && !domain) { button.disabled = true; button.title = '当前曲面没有有效数值，请查看数据表'; }
      else if (key === '3d' && !webgl) { button.disabled = true; button.title = '当前环境不支持3D，仍可查看热力图和数据表'; }
      button.addEventListener('click', () => { if (key !== view) { view = key; render(); } });
      buttons.set(key, button); toolbar.append(button);
    }
    toolbar.append(reset);
    const resize = () => { if (!disposed) return render(); };
    let resizeFrame = 0;
    const scheduleResize = () => {
      if (resizeFrame || disposed) return;
      resizeFrame = rootWindow.requestAnimationFrame(() => {
        resizeFrame = 0;
        resize();
      });
    };
    const Resize = doc.defaultView?.ResizeObserver;
    const observer = Resize ? new Resize(scheduleResize) : null;
    observer?.observe(host);
    observer?.observe(plot);
    const ObserveVisibility = doc.defaultView?.IntersectionObserver;
    const visibility = ObserveVisibility ? new ObserveVisibility(([entry]) => {
      onScreen = entry.isIntersecting;
      scheduleResize();
    }) : null;
    if (visibility) visibility.observe(host);
    else onScreen = true;
    outerDoc.addEventListener('visibilitychange', resize);
    outerDoc.addEventListener('optionhelper:modulevisibility', resize);
    if (frame) outerDoc.addEventListener('scroll', scheduleResize, true);
    const controller = {ready: render(), resize, camera: () => plot._fullLayout?.scene?.camera || camera, dispose() {
      if (disposed) return;
      disposed = true;
      revision++;
      if (resizeFrame) rootWindow.cancelAnimationFrame(resizeFrame);
      observer?.disconnect();
      visibility?.disconnect();
      outerDoc.removeEventListener('visibilitychange', resize);
      outerDoc.removeEventListener('optionhelper:modulevisibility', resize);
      if (frame) outerDoc.removeEventListener('scroll', scheduleResize, true);
      discardPlot();
      if (mounted.get(host) === controller) mounted.delete(host);
    }};
    mounted.set(host, controller);
    return controller;
  }
  window.OptionHelperSurface = Object.freeze({figure, grid, mount});
})();
