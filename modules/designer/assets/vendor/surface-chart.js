/* Frozen report grids use the workbench's Plotly scene without repricing. */
(() => {
  'use strict';
  const mounted = new WeakMap();
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));

  function format(value, percent = false, axisDigits = 0) {
    if (!finite(value)) return '不适用';
    const number = (Object.is(value, -0) ? 0 : value) * (percent ? 100 : 1);
    const digits = number === 0 || Math.abs(number) >= 1 ? 2 : Math.min(10, Math.ceil(-Math.log10(Math.abs(number))) + 2);
    return number.toLocaleString('zh-CN', {maximumFractionDigits: Math.max(digits, axisDigits), useGrouping: false}) + (percent ? '%' : '');
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

  function formatters(spec) {
    const rate = unit => ['decimal', 'decimal_rate'].includes(unit);
    return {
      formatX: value => finite(value) ? format(value, rate(spec.x_axis_unit), 4) : String(value),
      formatY: value => finite(value) ? format(value, rate(spec.y_axis_unit), 4) : String(value),
      formatZ: value => format(value, spec.value_format === 'percent') + (spec.value_format === 'percent_points' ? '%' : '') + (spec.value_suffix || ''),
    };
  }

  function figure(spec, options = {}) {
    const values = grid(spec);
    let current = null;
    if (finite(spec.reference_x) && values.x.length && values.y.length) {
      const i = values.x.reduce((best, value, index) => Math.abs(value - spec.reference_x) < Math.abs(values.x[best] - spec.reference_x) ? index : best, 0);
      const j = values.y.reduce((best, value, index) => value > values.y[best] ? index : best, 0);
      if (finite(values.z[j]?.[i])) current = {x: values.x[i], y: values.y[j], z: values.z[j][i]};
    }
    return window.OptionHelperPlotlyCharts.surfaceFigure({
      ...values, ...formatters(spec), xTitle: spec.x_axis_name, yTitle: spec.y_axis_name,
      zTitle: (spec.z_axis_name || '').replace(/（百分比敏感度）$/, ''),
      current, revision: `surface-${spec.id}`,
      ...options,
    });
  }

  function mount(host, spec, {onError} = {}) {
    const previous = mounted.get(host);
    let camera = previous?.camera() || null;
    previous?.dispose();
    const charts = window.OptionHelperPlotlyCharts;
    if (!charts?.surfaceFigure) throw new Error('plotly_surface_unavailable');
    const doc = host.ownerDocument;
    host.classList.add('chart--surface');
    host.setAttribute('role', 'group');
    host.replaceChildren();
    if (!doc.getElementById('optionhelper-surface-style')) {
      const style = doc.createElement('style'); style.id = 'optionhelper-surface-style';
      style.textContent = `.chart--surface{display:flex;flex-direction:column;min-height:330px;height:clamp(330px,45vw,420px)!important}.oh-surface-tools{display:flex;gap:4px;align-items:center;justify-content:flex-end;min-height:32px;flex:none}.oh-surface-tools button{font:inherit;font-size:12px;line-height:1.4;color:inherit;background:transparent;border:1px solid transparent;border-radius:6px;padding:5px 8px;cursor:pointer}.oh-surface-tools button[aria-pressed=true]{background:var(--color-ground,#f1f2f1);color:var(--color-ink,#252525)}.oh-surface-tools button:hover{border-color:var(--color-rule,#d7dce0)}.oh-surface-tools button:focus-visible{outline:2px solid var(--color-ink,#252525);outline-offset:2px}.oh-surface-tools button:disabled{opacity:.35;cursor:default}.oh-surface-plot,.oh-surface-table{flex:1;min-height:0;width:100%}.oh-surface-table{overflow:auto}.oh-surface-table table{border-collapse:collapse;font-size:12px;white-space:nowrap;width:100%}.oh-surface-table th,.oh-surface-table td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--color-rule,#d7dce0)}.oh-surface-table th{position:sticky;top:0;background:var(--color-paper,#fff);font-weight:600}.oh-surface-plot[hidden],.oh-surface-table[hidden]{display:none!important}@media print{.oh-surface-tools{display:none}}`;
      doc.head.append(style);
    }
    const toolbar = doc.createElement('div'); toolbar.className = 'oh-surface-tools';
    toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', '曲面视图');
    const plot = doc.createElement('div'); plot.className = 'oh-surface-plot';
    const table = doc.createElement('div'); table.className = 'oh-surface-table'; table.hidden = true;
    const values = grid(spec), formats = formatters(spec);
    table.innerHTML = `<table aria-label="${escape(spec.title || '曲面')}数据表"><thead><tr><th>${escape(spec.y_axis_name)}／${escape(spec.x_axis_name)}</th>${values.x.map(v => `<th scope="col">${escape(formats.formatX(v))}</th>`).join('')}</tr></thead><tbody>${values.y.map((v, j) => `<tr><th scope="row">${escape(formats.formatY(v))}</th>${values.x.map((_, i) => `<td>${escape(formats.formatZ(values.z[j][i]))}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
    host.append(toolbar, plot, table);
    plot.addEventListener('wheel', event => {
      if (plot._fullLayout && event.cancelable) event.preventDefault();
    }, {capture: true, passive: false});
    const hasValues = Boolean(charts.surfaceDomain(values.z));
    let disposed = false, revision = 0, view = !hasValues ? 'table' : charts.webglAvailable(doc) ? '3d' : 'heatmap';
    let pending = Promise.resolve(), compact = (host.clientWidth || 800) < 540;
    const buttons = new Map();
    const reset = doc.createElement('button'); reset.type = 'button'; reset.title = '重置视角'; reset.setAttribute('aria-label', '重置视角');
    reset.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 4v6h6M3.7 9a8 8 0 1 1-.1 6"/></svg>';
    reset.addEventListener('click', () => {
      camera = charts.surfaceCamera();
      if (view === '3d') window.Plotly?.relayout(plot, {'scene.camera': camera});
    });
    function sync() {
      host.dataset.surfaceView = view;
      for (const [key, button] of buttons) button.setAttribute('aria-pressed', String(key === view));
      reset.disabled = view !== '3d';
      plot.hidden = view === 'table'; table.hidden = view !== 'table';
    }
    function render() {
      sync();
      const activeRevision = ++revision;
      pending = pending.catch(() => {}).then(async () => {
        if (disposed || activeRevision !== revision) return;
        camera = plot._fullLayout?.scene?.camera || camera;
        charts.purge(plot);
        if (view === 'table') return;
        const draw = () => {
          const picture = figure(spec, {heatmap: view === 'heatmap', width: host.clientWidth || 800,
            document: doc, camera: camera || charts.surfaceCamera()});
          return charts.render(plot, picture.traces, picture.layout);
        };
        try { await draw(); }
        catch (error) {
          if (disposed || activeRevision !== revision) return;
          charts.purge(plot);
          if (view === '3d') {
            view = 'heatmap'; sync();
            try { await draw(); } catch (fallbackError) { view = 'table'; sync(); onError?.(fallbackError); }
          } else { view = 'table'; sync(); onError?.(error); }
        }
        if (disposed || activeRevision !== revision) { charts.purge(plot); return; }
        if (view === '3d') charts.watchWebglLoss(plot, () => {
          if (!disposed) { view = 'heatmap'; render(); }
        });
      });
      return pending;
    }
    for (const [key, label] of [['3d', '3D'], ['heatmap', '热力图'], ['table', '数据表']]) {
      const button = doc.createElement('button'); button.type = 'button'; button.textContent = label;
      if (key !== 'table' && !hasValues) { button.disabled = true; button.title = '当前曲面没有有效数值，请查看数据表'; }
      if (key === '3d' && !charts.webglAvailable(doc)) { button.disabled = true; button.title = '当前环境不支持3D，仍可查看热力图和数据表'; }
      button.addEventListener('click', () => { if (key !== view) { view = key; render(); } });
      buttons.set(key, button); toolbar.append(button);
    }
    toolbar.append(reset);
    const resize = () => {
      if (disposed || view === 'table' || !plot._fullLayout) return;
      const nextCompact = (host.clientWidth || 800) < 540;
      if (nextCompact !== compact) { compact = nextCompact; render(); }
      else charts.resize(plot);
    };
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    observer?.observe(host);
    const controller = {ready: render(), resize, camera: () => plot._fullLayout?.scene?.camera || camera, dispose() {
      if (disposed) return;
      disposed = true; revision++; observer?.disconnect(); charts.purge(plot);
      if (mounted.get(host) === controller) mounted.delete(host);
    }};
    mounted.set(host, controller);
    return controller;
  }
  window.OptionHelperSurface = Object.freeze({figure, grid, mount});
})();
