/* Offline Plotly presentation shared by workbenches and saved reports. */
(() => {
  "use strict";

  const cssColor = (name, fallback = "", documentRef = document) =>
    (documentRef.defaultView?.getComputedStyle || getComputedStyle)(documentRef.documentElement).getPropertyValue(name).trim() || fallback;

  const colors = (documentRef = document) => {
    const dark = documentRef.documentElement.dataset.theme === "dark";
    const css = (name, fallback) => cssColor(name, fallback, documentRef);
    return {
      ink: css("--color-ink", css("--ink", dark ? "#f1f3f4" : "#22272c")),
      muted: css("--color-muted-soft", css("--muted", dark ? "#a7b0b7" : "#6f7880")),
      rule: dark ? "#414950" : "#d7dce0",
      paper: css("--color-surface", css("--paper", dark ? "#202326" : "#ffffff")),
      ground: dark ? "#2a3035" : "#f1f2f1",
      primary: dark ? "#91aec1" : "#3f6078",
      primaryMid: dark ? "#6f8fa5" : "#7892a5",
      primarySoft: dark ? "#344650" : "#dbe4e9",
      secondary: dark ? "#7f8c95" : "#9aa6ae",
      tertiary: dark ? "#66737c" : "#b2bbc1",
      quaternary: dark ? "#505a61" : "#cbd0d4",
      risk: dark ? "#e36f82" : "#ad2940",
      riskMid: dark ? "#b94f62" : "#d07a89",
      riskSoft: dark ? "#503039" : "#f0dadd",
    };
  };

  const config = Object.freeze({
    displayModeBar: false,
    displaylogo: false,
    responsive: false,
    scrollZoom: true,
    plotGlPixelRatio: 1.25,
    doubleClick: "reset",
    showTips: false,
  });

  const font = (size = 12, color = colors().muted, documentRef = document) => ({
    family: (documentRef.defaultView?.getComputedStyle || getComputedStyle)(documentRef.body).fontFamily,
    size,
    color,
  });

  const axis = ({ title = "", tickformat, range, autorange, categoryorder } = {}) => {
    const palette = colors();
    return {
      title: title ? { text: title, font: font(12), standoff: 12 } : undefined,
      tickfont: font(12),
      tickformat,
      range,
      autorange,
      categoryorder,
      showgrid: false,
      zeroline: false,
      showline: true,
      linecolor: palette.rule,
      linewidth: 1,
      ticks: "outside",
      tickcolor: palette.rule,
      fixedrange: false,
      automargin: true,
    };
  };

  const cartesianLayout = ({
    margin = { l: 58, r: 24, t: 20, b: 52 },
    showlegend = false,
    hovermode = "closest",
    barmode,
    bargap,
    xaxis,
    yaxis,
    shapes,
    annotations,
    grid,
    uirevision,
  } = {}) => {
    const palette = colors();
    return {
      autosize: true,
      margin,
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: font(12, palette.ink),
      hoverlabel: {
        bgcolor: palette.paper,
        bordercolor: palette.rule,
        font: font(12, palette.ink),
        align: "left",
        namelength: -1,
      },
      hovermode,
      showlegend,
      legend: {
        orientation: "h",
        x: 0,
        y: 1.08,
        font: font(12),
        bgcolor: "rgba(0,0,0,0)",
      },
      barmode,
      bargap,
      xaxis: { ...axis(), ...xaxis },
      yaxis: { ...axis(), ...yaxis },
      shapes,
      annotations,
      grid,
      uirevision,
    };
  };

  const valueColorscale = (minimum, maximum, palette = colors()) => {
    if (Number.isFinite(minimum) && Number.isFinite(maximum) && minimum < 0 && maximum > 0) {
      const zero = Math.max(0, Math.min(1, (0 - minimum) / (maximum - minimum)));
      return [
        [0, palette.risk],
        [zero * .56, palette.riskMid],
        [zero, palette.ground],
        [zero + (1 - zero) * .5, palette.primaryMid],
        [1, palette.primary],
      ];
    }
    if (Number.isFinite(maximum) && maximum <= 0) {
      return [[0, palette.risk], [.52, palette.riskMid], [.82, palette.riskSoft], [1, palette.ground]];
    }
    return [[0, palette.ground], [.34, palette.primarySoft], [.68, palette.primaryMid], [1, palette.primary]];
  };

  const surfaceCamera = () => ({
    eye: { x: 1.45, y: -1.55, z: 1.08 },
    center: { x: 0, y: 0, z: -.08 },
    up: { x: 0, y: 0, z: 1 },
    projection: { type: 'orthographic' },
  });
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));

  function surfaceDomain(matrix) {
    const values = matrix.flat().filter(finite);
    if (!values.length) return null;
    const minimum = Math.min(...values), maximum = Math.max(...values);
    const padding = minimum === maximum ? Math.max(Math.abs(minimum) * .02, 1e-8) : 0;
    return { min: minimum - padding, max: maximum + padding, values };
  }

  function surfaceAxisTicks(values, limit) {
    if (values.length <= limit) return values;
    const minimum = Math.min(...values), maximum = Math.max(...values);
    const target = (maximum - minimum) / (limit - 1);
    const power = 10 ** Math.floor(Math.log10(target));
    for (const factor of [1, 2, 2.5, 5, 10]) {
      const step = factor * power;
      const first = Math.ceil(minimum / step), last = Math.floor(maximum / step);
      const count = last - first + 1;
      if (Number.isSafeInteger(first) && count >= 2 && count <= limit) {
        return Array.from({length: count}, (_, index) => Number(((first + index) * step).toPrecision(14)));
      }
    }
    return [...new Set([minimum, maximum].filter(finite))];
  }

  function surfaceFigure({ x, y, z, xTitle = '', yTitle = '', zTitle = '',
    formatX = String, formatY = String, formatZ = String, current = null,
    revision = 'surface', heatmap = false, width = 800, document: documentRef = document,
    camera = surfaceCamera(), showColorbar = true } = {}) {
    const domain = surfaceDomain(z);
    if (!domain) throw new Error('surface_has_no_values');
    const palette = colors(documentRef), narrow = width < 540;
    const textFont = (size = 12, color = palette.muted) => font(size, color, documentRef);
    const flat = domain.values.every(value => value === domain.values[0]);
    const ticks = flat ? [domain.values[0]] : [...new Set([domain.min, ...(domain.min < 0 && domain.max > 0 ? [0] : []), domain.max])];
    const colorbar = {
      thickness: 9, len: narrow ? .68 : .64, outlinewidth: 0,
      orientation: narrow ? 'h' : 'v', x: narrow ? .5 : 1.01, y: narrow ? -.12 : .5,
      xanchor: narrow ? 'center' : 'left', yanchor: narrow ? 'top' : 'middle',
      tickmode: 'array', tickvals: ticks, ticktext: ticks.map(formatZ), tickfont: textFont(11),
    };
    const axis = (title, values, formatter) => ({
      title: { text: escape(title), font: textFont(narrow ? 11 : 12) }, tickfont: textFont(narrow ? 10 : 11),
      showgrid: false, zeroline: false, showbackground: false, showspikes: false,
      showline: true, linecolor: palette.rule, ticks: 'outside', tickcolor: palette.rule,
      ...(values ? { tickmode: 'array', tickvals: values, ticktext: values.map(formatter) } : {}),
    });
    const xTicks = surfaceAxisTicks(x, narrow ? 3 : 5), yTicks = surfaceAxisTicks(y, narrow ? 3 : 5);
    const customdata = y.map((yv, j) => x.map((xv, i) => [formatX(xv), formatY(yv), finite(z[j]?.[i]) ? formatZ(z[j][i]) : '不适用']));
    const hovertemplate = `${escape(xTitle)}：%{customdata[0]}<br>${escape(yTitle)}：%{customdata[1]}<br>${escape(zTitle)}：%{customdata[2]}<extra></extra>`;
    const trace = {
      type: heatmap ? 'heatmap' : 'surface', x, y, z, customdata, hovertemplate,
      colorscale: valueColorscale(domain.min, domain.max, palette), colorbar, showscale: showColorbar,
      connectgaps: false,
      ...(heatmap ? { zmin: domain.min, zmax: domain.max, zsmooth: false, hoverongaps: false } : {
        cmin: domain.min, cmax: domain.max,
        contours: { x: { show: false }, y: { show: false }, z: { show: false } },
        lighting: { ambient: .86, diffuse: .58, specular: 0, roughness: 1, fresnel: 0 },
      }),
    };
    const traces = [trace];
    if (!heatmap && (x.length < 2 || y.length < 2)) {
      const points = y.flatMap((yv, j) => x.flatMap((xv, i) => finite(z[j]?.[i]) ? [{ x: xv, y: yv, z: z[j][i], custom: customdata[j][i] }] : []));
      traces[0] = { type: 'scatter3d', mode: 'markers', x: points.map(p => p.x), y: points.map(p => p.y), z: points.map(p => p.z),
        customdata: points.map(p => p.custom), hovertemplate,
        marker: { size: 4, color: points.map(p => p.z), cmin: domain.min, cmax: domain.max,
          colorscale: trace.colorscale, showscale: showColorbar, colorbar }, showlegend: false };
    }
    if (current && [current.x, current.y, current.z].every(finite)) traces.push({
      type: heatmap ? 'scatter' : 'scatter3d', mode: 'markers', name: '当前参考点',
      x: [current.x], y: [current.y], ...(heatmap ? {} : { z: [current.z] }),
      customdata: [[formatX(current.x), formatY(current.y), formatZ(current.z)]],
      marker: { color: palette.ink, size: heatmap ? 8 : 5, line: { color: palette.paper, width: 1.5 } },
      hovertemplate: '<b>当前参考点</b><br>' + hovertemplate, showlegend: false,
    });
    const layout = {
      autosize: true,
      margin: { l: heatmap ? 60 : 0, r: narrow || !showColorbar ? 12 : 94, t: 12,
        b: narrow && showColorbar ? 72 : (heatmap ? 48 : 12) },
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', font: textFont(12, palette.ink),
      showlegend: false, hoverlabel: { bgcolor: palette.paper, bordercolor: palette.rule, font: textFont(12, palette.ink), align: 'left' },
      uirevision: revision,
      ...(heatmap ? {
        xaxis: axis(xTitle, xTicks, formatX), yaxis: axis(yTitle, yTicks, formatY),
      } : { scene: {
        bgcolor: 'rgba(0,0,0,0)', dragmode: 'orbit', aspectmode: 'manual',
        aspectratio: { x: 1.5, y: 1, z: .68 }, camera, uirevision: revision,
        xaxis: axis(xTitle, xTicks, formatX), yaxis: axis(yTitle, yTicks, formatY),
        zaxis: axis(zTitle, ticks, formatZ),
      } }),
    };
    return { traces, layout };
  }

  const plotly = () => window.Plotly?.newPlot && window.Plotly?.react ? window.Plotly : null;
  const deferredScript = globalThis.document?.getElementById?.("plotly-runtime")?.content?.querySelector("script");
  let plotlyLoad;
  const pendingRenders = new WeakMap();
  const webglWatchers = new WeakMap();

  const loadPlotly = () => {
    if (!deferredScript) return Promise.reject(new Error("plotly_unavailable"));
    if (!plotlyLoad) {
      plotlyLoad = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        const src = deferredScript.getAttribute("src");
        const finish = () => {
          const runtime = plotly();
          if (runtime) resolve(runtime);
          else {
            script.remove();
            reject(new Error("plotly_unavailable"));
          }
        };
        if (src) {
          script.src = src;
          script.onload = finish;
          script.onerror = finish;
        } else {
          // The offline demo inlines the deferred asset inside the same template.
          script.textContent = deferredScript.textContent;
        }
        document.head.append(script);
        if (!src) finish();
      }).catch(error => {
        plotlyLoad = null;
        throw error;
      });
    }
    return plotlyLoad;
  };

  const render = (node, traces, layout, overrides = {}) => {
    const draw = (runtime) => {
      const operation = node._fullLayout ? runtime.react : runtime.newPlot;
      return operation.call(runtime, node, traces, layout, { ...config, ...overrides });
    };
    try {
      pendingRenders.delete(node);
      const runtime = plotly();
      if (runtime) return Promise.resolve(draw(runtime));
      const token = {};
      pendingRenders.set(node, token);
      const isCurrent = () => pendingRenders.get(node) === token;
      return loadPlotly().then(ready => {
        if (isCurrent()) return draw(ready);
      }, error => {
        if (isCurrent()) throw error;
      }).finally(() => {
        if (isCurrent()) pendingRenders.delete(node);
      });
    } catch (error) {
      return Promise.reject(error);
    }
  };

  const purge = (node) => {
    if (!node) return;
    pendingRenders.delete(node);
    webglWatchers.get(node)?.();
    try {
      if (window.Plotly?.purge && node._fullLayout) window.Plotly.purge(node);
    } catch (_error) {
      // A failed WebGL context may already have torn down the graph.
    }
    node.replaceChildren();
  };

  const resize = (node) => {
    if (!node || node.hidden || !node.isConnected || !node._fullLayout) return;
    window.Plotly?.Plots?.resize(node);
  };

  const webglSupport = new WeakMap();
  const webglAvailable = (documentRef = document) => {
    if (webglSupport.has(documentRef)) return webglSupport.get(documentRef);
    try {
      const canvas = documentRef.createElement("canvas");
      const context = canvas.getContext("webgl2") || canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      const available = Boolean(context);
      context?.getExtension?.('WEBGL_lose_context')?.loseContext();
      webglSupport.set(documentRef, available);
      return available;
    } catch (_error) {
      return false;
    }
  };

  const watchWebglLoss = (node, onLost) => {
    webglWatchers.get(node)?.();
    const handlers = Array.from(node.querySelectorAll("canvas"), canvas => {
      const lost = (event) => {
        if (!node.contains(canvas)) return;
        event.preventDefault();
        unwatch();
        onLost?.();
      };
      canvas.addEventListener("webglcontextlost", lost, { once: true });
      return () => canvas.removeEventListener("webglcontextlost", lost);
    });
    const unwatch = () => {
      handlers.forEach(remove => remove());
      if (webglWatchers.get(node) === unwatch) webglWatchers.delete(node);
    };
    webglWatchers.set(node, unwatch);
    return unwatch;
  };

  window.OptionHelperPlotlyCharts = Object.freeze({
    axis,
    cartesianLayout,
    colors,
    config,
    font,
    purge,
    render,
    resize,
    valueColorscale,
    surfaceCamera,
    surfaceDomain,
    surfaceFigure,
    watchWebglLoss,
    webglAvailable,
  });
})();
