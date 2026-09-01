/* Shared Plotly presentation contract for the Pricer and Backtester pages. */
(() => {
  "use strict";

  const cssColor = (name, fallback = "") =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

  const colors = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    return {
      ink: cssColor("--color-ink", cssColor("--ink", dark ? "#f1f3f4" : "#22272c")),
      muted: cssColor("--color-muted-soft", cssColor("--muted", dark ? "#a7b0b7" : "#6f7880")),
      rule: dark ? "#414950" : "#d7dce0",
      paper: cssColor("--color-surface", cssColor("--paper", dark ? "#202326" : "#ffffff")),
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

  const font = (size = 12, color = colors().muted) => ({
    family: getComputedStyle(document.body).fontFamily,
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

  const valueColorscale = (minimum, maximum) => {
    const palette = colors();
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

  const plotly = () => {
    if (!window.Plotly?.newPlot || !window.Plotly?.react) {
      throw new Error("plotly_unavailable");
    }
    return window.Plotly;
  };

  const render = (node, traces, layout, overrides = {}) => {
    try {
      const runtime = plotly();
      const operation = node._fullLayout ? runtime.react : runtime.newPlot;
      return Promise.resolve(operation.call(runtime, node, traces, layout, { ...config, ...overrides }));
    } catch (error) {
      return Promise.reject(error);
    }
  };

  const purge = (node) => {
    if (!node) return;
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

  const webglAvailable = (documentRef = document) => {
    try {
      const canvas = documentRef.createElement("canvas");
      return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl") || canvas.getContext("experimental-webgl"));
    } catch (_error) {
      return false;
    }
  };

  const watchWebglLoss = (node, onLost) => {
    for (const canvas of node.querySelectorAll("canvas")) {
      if (canvas.dataset.optionhelperWebglWatch === "true") continue;
      canvas.dataset.optionhelperWebglWatch = "true";
      canvas.addEventListener("webglcontextlost", (event) => {
        event.preventDefault();
        onLost?.();
      }, { once: true });
    }
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
    watchWebglLoss,
    webglAvailable,
  });
})();
