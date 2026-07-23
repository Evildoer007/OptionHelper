export const DEFAULT_SCALE = Object.freeze({ xMin: 0, xMax: 200, yMin: -100, yMax: 100 });
export const VALUE_DECIMALS = 2;
export const MAX_GUIDES = 6;
// 个别结构（例如壁虎型雪球）同时需要执行价、敲出价、敲入价和两条
// 避险线，因此关键价格线的真实上限为5条。
export const MAX_THRESHOLDS = 5;
export const MAX_POINTS = 12;
export const CURVE_ENDPOINTS = Object.freeze(['none', 'open', 'closed']);
export const AXIS_KINDS = Object.freeze(['underlying_price', 'realized_volatility', 'observed_days', 'coupon_count']);
export const DEFAULT_AXIS = Object.freeze({ kind: 'underlying_price', label: '标的价格(%)', unit: '%', referenceValue: 100 });
export const VOLATILITY_AXIS = Object.freeze({ kind: 'realized_volatility', label: '已实现波动率σ', unit: '', referenceValue: 13 });
export const OBSERVED_DAYS_AXIS = Object.freeze({ kind: 'observed_days', label: '区间内观察日数P', unit: '天', referenceValue: 11 });
export const COUPON_COUNT_AXIS = Object.freeze({ kind: 'coupon_count', label: '满足计息条件次数', unit: '次', referenceValue: 6 });

export const THRESHOLD_FAMILIES = Object.freeze({
  spot: Object.freeze({ label: '期初价', color: '#8A8A8A' }),
  strike: Object.freeze({ label: '执行价', color: '#1F5FA8' }),
  knockout: Object.freeze({ label: '敲出价', color: '#D89B27' }),
  knockin: Object.freeze({ label: '敲入价', color: '#D89B27' }),
  barrier: Object.freeze({ label: '障碍价', color: '#D89B27' }),
  other: Object.freeze({ label: '关键价格', color: '#6F7780' }),
});

const THRESHOLD_META = Object.freeze({
  'S₀': Object.freeze({ family: 'spot', label: '期初价', color: '#8A8A8A', dash: '4 4' }),
  K: Object.freeze({ family: 'strike', label: '执行价', color: '#1F5FA8', dash: '9 4' }),
  'K₁': Object.freeze({ family: 'strike', label: '执行价1', color: '#1F5FA8', dash: '9 4' }),
  'K₂': Object.freeze({ family: 'strike', label: '执行价2', color: '#1F5FA8', dash: '7 4' }),
  'K₃': Object.freeze({ family: 'strike', label: '执行价3', color: '#1F5FA8', dash: '5 4' }),
  'K₄': Object.freeze({ family: 'strike', label: '执行价4', color: '#1F5FA8', dash: '3 4' }),
  K_p: Object.freeze({ family: 'strike', label: '看跌执行价', color: '#1F5FA8', dash: '9 4' }),
  K_c: Object.freeze({ family: 'strike', label: '看涨执行价', color: '#1F5FA8', dash: '7 4' }),
  K_u: Object.freeze({ family: 'strike', label: '上执行价', color: '#1F5FA8', dash: '5 4' }),
  K_d: Object.freeze({ family: 'strike', label: '下执行价', color: '#1F5FA8', dash: '3 4' }),
  H: Object.freeze({ family: 'barrier', label: '触发价', color: '#D89B27', dash: '8 5' }),
  H_out: Object.freeze({ family: 'knockout', label: '敲出价', color: '#D89B27', dash: '12 5' }),
  'H_{out,2}': Object.freeze({ family: 'knockout', label: '第二敲出价', color: '#D89B27', dash: '8 4' }),
  'H_{out,t}': Object.freeze({ family: 'knockout', label: '时间敲出价', color: '#D89B27', dash: '5 3' }),
  H_in: Object.freeze({ family: 'knockin', label: '敲入价', color: '#D89B27', dash: '12 5' }),
  'H_{in,2}': Object.freeze({ family: 'knockin', label: '第二敲入价', color: '#D89B27', dash: '8 4' }),
  H_c: Object.freeze({ family: 'barrier', label: '计息价', color: '#D89B27', dash: '6 4' }),
  H_buffer: Object.freeze({ family: 'barrier', label: '缓冲价', color: '#D89B27', dash: '8 3' }),
  H_floor: Object.freeze({ family: 'barrier', label: '保底价', color: '#D89B27', dash: '7 3' }),
  H_reset: Object.freeze({ family: 'barrier', label: '重设价', color: '#D89B27', dash: '4 3' }),
  H_u: Object.freeze({ family: 'barrier', label: '上障碍价', color: '#D89B27', dash: '10 4' }),
  H_d: Object.freeze({ family: 'barrier', label: '下障碍价', color: '#D89B27', dash: '6 3' }),
  'H_{hedge,1}': Object.freeze({ family: 'barrier', label: '第一避险线', color: '#D89B27', dash: '8 3' }),
  'H_{hedge,2}': Object.freeze({ family: 'barrier', label: '第二避险线', color: '#D89B27', dash: '6 3' }),
  B: Object.freeze({ family: 'barrier', label: '缓冲价', color: '#D89B27', dash: '8 3' }),
  L: Object.freeze({ family: 'barrier', label: '限损价', color: '#D89B27', dash: '7 3' }),
});

export const GUIDE_META = Object.freeze({ label: '辅助线', color: '#8A949E', dash: '4 4' });

export const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
export const round = (value, decimals = VALUE_DECIMALS) => Math.round((Number(value) + Number.EPSILON) * (10 ** decimals)) / (10 ** decimals);
export const equal = (left, right) => Math.abs(Number(left) - Number(right)) < 1e-9;

export function defaultScale() {
  return { ...DEFAULT_SCALE };
}

export function normalizeAxis(axis) {
  if (axis?.kind === 'realized_volatility') return { ...VOLATILITY_AXIS, ...axis, unit: '' };
  if (axis?.kind === 'observed_days') return { ...OBSERVED_DAYS_AXIS, ...axis, unit: '天' };
  if (axis?.kind === 'coupon_count') return { ...COUPON_COUNT_AXIS, ...axis, unit: '次' };
  return { ...DEFAULT_AXIS, ...(axis || {}), kind: 'underlying_price', unit: '%' };
}

export function axisReference(axis) {
  return normalizeAxis(axis).referenceValue;
}

export function axisUnit(axis) {
  return normalizeAxis(axis).unit;
}

export function createBlankGraphic(scenarioId) {
  return {
    scenarioId,
    scale: defaultScale(),
    curve: { type: 'polyline', strokeWidth: 4.4, points: [] },
    thresholds: [],
    guides: [],
  };
}

export function resetGraphic(scenarioId) {
  return createBlankGraphic(scenarioId);
}

export function resetAllGraphics(scenarioIds) {
  return scenarioIds.map((scenarioId) => createBlankGraphic(scenarioId));
}

export function scaleIssues(scale, axis = DEFAULT_AXIS) {
  if (!scale || typeof scale !== 'object') return ['坐标范围必须完整填写。'];
  const { xMin, xMax, yMin, yMax } = scale;
  if (![xMin, xMax, yMin, yMax].every(Number.isFinite)) return ['坐标范围必须是有效数字。'];
  const issues = [];
  if (xMin >= xMax) issues.push('X轴下限必须小于上限。');
  if (yMin >= yMax) issues.push('Y轴下限必须小于上限。');
  const reference = axisReference(axis);
  if (!(xMin < reference && xMax > reference)) issues.push(`X轴范围必须严格包含参考值${reference}${axisUnit(axis)}。`);
  if (!(yMin < 0 && yMax > 0)) issues.push('Y轴范围必须严格包含0。');
  return issues;
}

export function valueInScale(value, min, max) {
  return Number.isFinite(value) && value >= min && value <= max;
}

export function thresholdMeta(token) {
  if (THRESHOLD_META[token]) return THRESHOLD_META[token];
  return { family: 'other', ...THRESHOLD_FAMILIES.other };
}

export function guideMeta(index) {
  return GUIDE_META;
}

// Guides are intentionally non-semantic local drawing aids. Older v2 JSON without
// start/end remains valid and is rendered over the full relevant axis range.
export function guideExtent(guide, scale) {
  const horizontal = guide?.direction === 'horizontal';
  const min = horizontal ? scale.xMin : scale.yMin;
  const max = horizontal ? scale.xMax : scale.yMax;
  return {
    start: Number.isFinite(guide?.start) ? guide.start : min,
    end: Number.isFinite(guide?.end) ? guide.end : max,
  };
}

// v2早期草稿可不带起止点。任何一次局部编辑前都先把它显式化，
// 避免只改一个端点而被校验器视为无效半段线。
export function materializeGuideExtent(guide, scale) {
  const { start, end } = guideExtent(guide, scale);
  return { ...guide, start: round(start), end: round(end) };
}

export function pointInScale(point, scale) {
  return valueInScale(point?.x, scale.xMin, scale.xMax) && valueInScale(point?.y, scale.yMin, scale.yMax);
}

export function xToRatio(value, scale) {
  return (value - scale.xMin) / (scale.xMax - scale.xMin);
}

export function yToRatio(value, scale) {
  return (scale.yMax - value) / (scale.yMax - scale.yMin);
}

export function ratioToX(ratio, scale) {
  return round(scale.xMin + clamp(ratio, 0, 1) * (scale.xMax - scale.xMin));
}

export function ratioToY(ratio, scale) {
  return round(scale.yMax - clamp(ratio, 0, 1) * (scale.yMax - scale.yMin));
}

export function axisRatios(scale, axis = DEFAULT_AXIS) {
  return { x: xToRatio(axisReference(axis), scale), y: yToRatio(0, scale) };
}

export function blankCurvePoints(scale) {
  return [{ x: scale.xMin, y: 0 }, { x: scale.xMax, y: 0 }];
}

function centeredRange(values, anchor, minimumHalfSpan) {
  const finite = values.filter(Number.isFinite);
  const furthest = finite.length ? Math.max(...finite.map((value) => Math.abs(value - anchor))) : 0;
  const halfSpan = Math.max(minimumHalfSpan, furthest * 1.15);
  return [round(anchor - halfSpan), round(anchor + halfSpan)];
}

// Keep the semantic cross at X=100 / Y=0 while automatically leaving equal
// visual space on both sides. Manual scale edits remain fully supported.
export function autoFitScale(graphic, axis = DEFAULT_AXIS) {
  const points = graphic?.curve?.points || [];
  const thresholds = graphic?.thresholds || [];
  const guides = graphic?.guides || [];
  if (!points.length && !thresholds.length && !guides.length) return defaultScale();
  const current = graphic?.scale || defaultScale();
  const reference = axisReference(axis);
  const xValues = [reference, ...points.map((point) => point.x), ...thresholds.map((item) => item.value)];
  const yValues = [0, ...points.map((point) => point.y)];
  for (const guide of guides) {
    const extent = guideExtent(guide, current);
    if (guide.direction === 'horizontal') {
      yValues.push(guide.value);
      xValues.push(extent.start, extent.end);
    } else {
      xValues.push(guide.value);
      yValues.push(extent.start, extent.end);
    }
  }
  const axisKind = normalizeAxis(axis).kind;
  if (axisKind === 'observed_days' || axisKind === 'coupon_count') {
    // 观察日数与计息次数的定义域从0开始。以参考值为中心时，默认完整展示
    // 一个完整观察期，不为真实不存在的负数量留白。
    const xMax = round(Math.max(reference * 2, ...xValues.filter(Number.isFinite)));
    const [yMin, yMax] = centeredRange(yValues, 0, 10);
    return { xMin: 0, xMax, yMin, yMax };
  }
  const [xMin, xMax] = centeredRange(xValues, reference, axisKind === 'realized_volatility' ? 4 : axisKind === 'observed_days' ? 11 : 20);
  const [yMin, yMax] = centeredRange(yValues, 0, 10);
  return { xMin, xMax, yMin, yMax };
}

export function addGuide(graphic, guide) {
  if (graphic.guides.length >= MAX_GUIDES) return { changed: false, message: `每个情景最多${MAX_GUIDES}条辅助线。` };
  const valueRange = guide.direction === 'horizontal'
    ? [graphic.scale.yMin, graphic.scale.yMax]
    : [graphic.scale.xMin, graphic.scale.xMax];
  if (!valueInScale(guide.value, valueRange[0], valueRange[1])) return { changed: false, message: '辅助线数值必须落在当前坐标范围内。' };
  const extent = guideExtent(guide, graphic.scale);
  const extentRange = guide.direction === 'horizontal'
    ? [graphic.scale.xMin, graphic.scale.xMax]
    : [graphic.scale.yMin, graphic.scale.yMax];
  if (!valueInScale(extent.start, extentRange[0], extentRange[1]) || !valueInScale(extent.end, extentRange[0], extentRange[1]) || extent.start >= extent.end) {
    return { changed: false, message: '辅助线起点和终点必须在对应坐标范围内，且起点小于终点。' };
  }
  graphic.guides.push({ direction: guide.direction, value: round(guide.value), start: round(extent.start), end: round(extent.end), style: 'dashed' });
  return { changed: true };
}

export function addThreshold(graphic, threshold) {
  if (graphic.thresholds.length >= MAX_THRESHOLDS) return { changed: false, message: `每个情景最多${MAX_THRESHOLDS}条关键价格线。` };
  if (graphic.thresholds.some((item) => item.token === threshold.token)) return { changed: false, message: `当前情景已添加${threshold.token}。` };
  if (!valueInScale(threshold.value, graphic.scale.xMin, graphic.scale.xMax)) return { changed: false, message: '关键价格必须落在当前X轴范围内。' };
  graphic.thresholds.push({ token: threshold.token, value: round(threshold.value) });
  return { changed: true };
}

export function boundedPointX(value, points, index, type, scale) {
  const previous = points[index - 1];
  const next = points[index + 1];
  const point = points[index];
  const minGap = type === 'polyline' && !point?.breakBefore ? 0.01 : 0;
  const maxGap = type === 'polyline' && !next?.breakBefore ? 0.01 : 0;
  const min = previous ? previous.x + minGap : scale.xMin;
  const max = next ? next.x - maxGap : scale.xMax;
  return round(clamp(value, min, Math.max(min, max)));
}
