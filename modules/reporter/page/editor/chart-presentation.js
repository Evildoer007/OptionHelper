(() => {
  'use strict';

  const DEFAULT_PALETTE = Object.freeze(['#C8102E', '#49647D', '#936719', '#6B6B6B', '#404040']);
  const DEFAULT_THEME = Object.freeze({
    color: [...DEFAULT_PALETTE],
    lineTypes: ['solid', 'dashed', 'dotted'],
    symbols: ['circle', 'rect', 'triangle', 'diamond'],
    textStyle: {
      fontFamily: 'Arial, "PingFang SC", "Noto Sans CJK SC", sans-serif',
      color: '#404040',
      fontSize: 11,
    },
    axis: {line: '#C9C9C9', label: '#6B6B6B', split: '#E2E2E2'},
    tooltip: {background: '#252525', border: '#C9C9C9', text: '#FFFFFF'},
    heatmap: {low: '#F4F4F4'},
  });
  const superscript = Object.freeze({
    '-': '⁻', '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
    '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
  });

  const text = value => String(value ?? '');
  const copy = value => JSON.parse(JSON.stringify(value));

  function displayNumber(value, valueFormat = 'number', digits = 2) {
    if (value === null || value === undefined || value === '') return '';
    let number = Number(value);
    if (!Number.isFinite(number)) return text(value);
    const format = text(valueFormat || 'number').toLowerCase();
    if (format === 'percent') number *= 100;
    const percent = format === 'percent' || format === 'percent_points';
    if (number !== 0 && (Math.round(Math.abs(number) * 10 ** digits) === 0 || Math.abs(number) >= 1e12)) {
      const [mantissa, exponent] = number.toExponential(2).split('e');
      const power = [...String(Number(exponent))].map(character => superscript[character] || character).join('');
      return `${mantissa.replace(/0+$/, '').replace(/\.$/, '').replace(/^-/, '−')}×10${power}${percent ? '%' : ''}`;
    }
    return new Intl.NumberFormat('zh-CN', {maximumFractionDigits: digits}).format(number).replace(/^-/, '−') + (percent ? '%' : '');
  }

  function formatValue(value, spec = {}, digits = 2) {
    return `${displayNumber(value, spec.value_format, digits)}${text(spec.value_suffix)}`;
  }

  function unitLabel(spec = {}) {
    const format = text(spec.value_format || 'number').toLowerCase();
    const percent = format === 'percent' || format === 'percent_points' ? '%' : '';
    return `${percent}${text(spec.value_suffix)}`;
  }

  function inputValue(value, spec = {}) {
    if (value === null || value === undefined || value === '') return '';
    const number = Number(value);
    if (!Number.isFinite(number)) return text(value);
    const scaled = text(spec.value_format).toLowerCase() === 'percent' ? number * 100 : number;
    return String(Number(scaled.toPrecision(15)));
  }

  function parseInputValue(value, spec = {}) {
    if (value === null || value === undefined || text(value).trim() === '') return null;
    const number = Number(value);
    if (!Number.isFinite(number)) return value;
    return text(spec.value_format).toLowerCase() === 'percent' ? number / 100 : number;
  }

  function displayCategory(value, axisName, digits = 2) {
    return /年|日期|时间|代码|标识/.test(text(axisName)) ? text(value) : displayNumber(value, 'number', digits);
  }

  function axisPrecision(values, format = 'number') {
    const numbers = [...new Set(values.filter(value => value !== null && value !== '' && Number.isFinite(Number(value))).map(Number))].sort((a,b) => a-b);
    if (numbers.length < 2) return 2;
    let step = Infinity;
    for (let index = 1; index < numbers.length; index++) step = Math.min(step, numbers[index] - numbers[index-1]);
    if (format === 'percent') step *= 100;
    return Math.min(6, Math.max(2, Math.ceil(-Math.log10(step))));
  }

  function presentationDefaults() {
    return {palette: [...DEFAULT_PALETTE], theme: copy(DEFAULT_THEME)};
  }

  function chartOption(spec, presentation = presentationDefaults()) {
    const theme = presentation?.theme || DEFAULT_THEME;
    const palette = Array.isArray(presentation?.palette) && presentation.palette.length
      ? presentation.palette
      : DEFAULT_PALETTE;
    const lineTypes = Array.isArray(theme.lineTypes) && theme.lineTypes.length ? theme.lineTypes : DEFAULT_THEME.lineTypes;
    const symbols = Array.isArray(theme.symbols) && theme.symbols.length ? theme.symbols : DEFAULT_THEME.symbols;
    const tooltipStyle = {
      backgroundColor: theme.tooltip?.background,
      borderColor: theme.tooltip?.border,
      textStyle: {color: theme.tooltip?.text, fontFamily: theme.textStyle?.fontFamily, fontSize: theme.textStyle?.fontSize},
    };
    const type = spec.type === 'bar' ? 'bar' : 'line';
    const xValues = Array.isArray(spec.x) ? spec.x : [];
    const series = Array.isArray(spec.series) ? spec.series : [];
    if (spec.type === 'surface') return window.OptionHelperSurface.option(spec, value => formatValue(value, spec, 4), theme.axis?.label);
    if (spec.type === 'heatmap') {
      const yValues = Array.isArray(spec.y) ? spec.y : [];
      const data = Array.isArray(spec.data) ? spec.data : [];
      const values = data.map(item => Number(item?.[2])).filter(Number.isFinite);
      return {
        animation: false,
        color: [...palette],
        tooltip: {
          ...tooltipStyle,
          position: 'top',
          formatter: item => `${spec.x_axis_name || '横轴'}：${displayCategory(xValues[item.data[0]], spec.x_axis_name)}<br>${spec.y_axis_name || '纵轴'}：${displayCategory(yValues[item.data[1]], spec.y_axis_name)}<br>${spec.z_axis_name || '数值'}：${formatValue(item.data[2], spec)}`,
        },
        grid: {left: 72, right: 24, top: 30, bottom: 94},
        xAxis: {
          type: 'category', name: spec.x_axis_name || '', nameLocation: 'middle', nameGap: 27, data: xValues,
          axisLine: {lineStyle: {color: theme.axis?.line}},
          axisLabel: {color: theme.axis?.label, fontSize: theme.textStyle?.fontSize, formatter: value => displayCategory(value, spec.x_axis_name, axisPrecision(xValues))},
        },
        yAxis: {
          type: 'category', name: spec.y_axis_name || '', nameLocation: 'middle', nameGap: 48, data: yValues,
          axisLine: {lineStyle: {color: theme.axis?.line}},
          axisLabel: {color: theme.axis?.label, fontSize: theme.textStyle?.fontSize, formatter: value => displayCategory(value, spec.y_axis_name, axisPrecision(yValues))},
        },
        visualMap: {
          min: values.length ? Math.min(...values) : 0,
          max: values.length ? Math.max(...values) : 1,
          calculable: false, orient: 'horizontal', left: 'center', bottom: 6,
          formatter: value => formatValue(value, spec),
          textStyle: {color: theme.axis?.label, fontSize: theme.textStyle?.fontSize},
          inRange: {color: [theme.heatmap?.low || DEFAULT_THEME.heatmap.low, palette[0], palette[1]]},
        },
        series: [{name: spec.z_axis_name || '数值', type: 'heatmap', data, label: {show: false}, emphasis: {itemStyle: {shadowBlur: 8}}}],
      };
    }
    const hasLegend = series.length > 1;
    const yValues = series.flatMap(item => item.data || []).filter(value => value !== null && value !== '' && Number.isFinite(Number(value))).map(Number);
    const span = yValues.length ? (Math.max(...yValues) - Math.min(0, ...yValues)) / 4 : 1;
    const yDigits = axisPrecision([0, span], spec.value_format);
    return {
      animation: false,
      color: [...palette],
      tooltip: {...tooltipStyle, trigger: 'axis', valueFormatter: value => formatValue(value, spec, Math.max(4, yDigits))},
      legend: {
        show: hasLegend, top: 2,
        textStyle: {color: theme.textStyle?.color, fontFamily: theme.textStyle?.fontFamily, fontSize: theme.textStyle?.fontSize},
      },
      grid: {left: 58, right: 18, top: hasLegend ? 48 : 28, bottom: 78},
      xAxis: {
        type: 'category', name: spec.x_axis_name || '', nameLocation: 'middle', nameGap: 24, data: xValues,
        axisLine: {lineStyle: {color: theme.axis?.line}},
        axisLabel: {
          color: theme.axis?.label, fontSize: theme.textStyle?.fontSize, interval: 0,
          rotate: xValues.length > 14 ? 42 : 0,
          formatter: value => displayCategory(value, spec.x_axis_name, axisPrecision(xValues)),
        },
      },
      yAxis: {
        type: 'value', name: spec.y_axis_name || '', nameTextStyle: {color: theme.axis?.label},
        axisLabel: {color: theme.axis?.label, fontSize: theme.textStyle?.fontSize, formatter: value => formatValue(value, spec, yDigits)},
        splitLine: {lineStyle: {color: theme.axis?.split, type: 'dashed'}},
      },
      series: series.map((item, seriesIndex) => ({
        name: item.name,
        type,
        smooth: false,
        symbol: type === 'line' ? symbols[seriesIndex % symbols.length] : 'none',
        showSymbol: type === 'line' && xValues.length <= 60,
        symbolSize: 5,
        barMaxWidth: 42,
        data: Array.isArray(item.data) ? item.data : [],
        itemStyle: {color: palette[seriesIndex % palette.length]},
        lineStyle: {width: 2, type: lineTypes[seriesIndex % lineTypes.length]},
      })),
    };
  }

  function renderChart({echarts, host, spec, presentation} = {}) {
    if (!echarts?.init || !host || !spec) throw new Error('图表渲染参数不完整');
    const instance = echarts.init(host, null, {renderer: spec.type === 'surface' ? 'canvas' : 'svg'});
    instance.setOption(chartOption(spec, presentation), true);
    return instance;
  }

  function mountCharts({root = document, echarts = window.echarts, specs = [], presentation, onError} = {}) {
    const values = Array.isArray(specs) ? specs : Object.values(specs || {});
    const instances = [];
    values.forEach(spec => {
      const host = root.getElementById(spec.id);
      if (!host) return;
      try {
        instances.push(renderChart({echarts, host, spec, presentation}));
      } catch (error) {
        if (typeof onError === 'function') onError(spec, error, host);
      }
    });
    return Object.freeze({
      instances,
      resize: () => instances.forEach(instance => instance.resize?.()),
      dispose: () => instances.forEach(instance => instance.dispose?.()),
    });
  }

  window.OptionHelperChartPresentation = Object.freeze({
    chartOption,
    displayCategory,
    displayNumber,
    formatValue,
    inputValue,
    mountCharts,
    parseInputValue,
    presentationDefaults,
    renderChart,
    unitLabel,
  });
})();
