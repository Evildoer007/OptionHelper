import { autoFitScale, createBlankGraphic, MAX_THRESHOLDS, round } from './public/graphic-model.mjs';

// The library's .2 payoff table defines the geometry.  .3 examples provide
// parameter values and are used for numeric calibration only.  Keeping the
// product rules here makes the initial SVG deterministic for both the editor
// and an LLM that writes the same JSON.

const point = (x, y, extra = {}) => ({ x: round(x), y: round(y), ...extra });
const line = (...items) => items.map(([x, y, extra]) => point(x, y, extra));
const dot = (x, y) => [point(x, y, { endpoint: 'closed' }), point(x, y, { breakBefore: true, endpoint: 'closed' })];

function join(...segments) {
  const points = [];
  for (const segment of segments.filter((item) => item?.length)) {
    const next = segment.map((item) => ({ ...item }));
    if (points.length) next[0].breakBefore = true;
    points.push(...next);
  }
  return points;
}

function graphic(scenarioId, points, thresholds, axis) {
  const result = createBlankGraphic(scenarioId);
  result.curve = { type: 'polyline', strokeWidth: 4.4, points };
  result.thresholds = thresholds;
  result.scale = autoFitScale(result, axis);
  return result;
}

function value(parameters, token, fallback) {
  return Number.isFinite(parameters?.get(token)) ? parameters.get(token) : fallback;
}

function prices(parameters, definitions) {
  const seen = new Set();
  return definitions
    .map(([token, fallback]) => ({ token, value: value(parameters, token, fallback) }))
    .filter((item) => Number.isFinite(item.value) && !seen.has(item.token) && seen.add(item.token))
    .slice(0, MAX_THRESHOLDS);
}

function make(id, scenarios, parameters, axis, definition) {
  const graphics = definition(parameters, axis);
  if (!Array.isArray(graphics) || graphics.length !== scenarios.length) {
    throw new Error(`${id}的公式Payoff定义与资料库情景数不一致。`);
  }
  return graphics.map((item, index) => graphic(scenarios[index].id, item.points || [], item.thresholds || [], axis));
}

const P = (points, thresholds = []) => ({ points, thresholds });

const FORMULAS = Object.freeze({
  '2.1': (p) => {
    const k = value(p, 'K', 100); const t = prices(p, [['K', 100]]);
    return [P(line([80, -5], [k, -5]), t), P(line([k, -5], [k + 5, 0], [120, 15]), t)];
  },
  '2.2': (p) => {
    const k = value(p, 'K', 100); const t = prices(p, [['K', 100]]);
    return [P(line([k, -5], [120, -5]), t), P(line([80, 15], [k - 5, 0], [k, -5]), t)];
  },
  '3.1': (p) => {
    const a = value(p, 'K₁', 95), b = value(p, 'K₂', 105), t = prices(p, [['K₁', 95], ['K₂', 105]]);
    return [P(line([80, -4], [a, -4]), t), P(line([a, -4], [b, 6]), t), P(line([b, 6], [120, 6]), t)];
  },
  '3.2': (p) => {
    const a = value(p, 'K₁', 95), b = value(p, 'K₂', 105), t = prices(p, [['K₁', 95], ['K₂', 105]]);
    return [P(line([b, 6], [120, 6]), t), P(line([a, -4], [b, 6]), t), P(line([80, -4], [a, -4]), t)];
  },
  '3.3': (p) => {
    const a = value(p, 'K₁', 95), b = value(p, 'K₂', 105), t = prices(p, [['K₁', 95], ['K₂', 105]]);
    return [P(line([b, -4], [120, -4]), t), P(line([a, 6], [b, -4]), t), P(line([80, 6], [a, 6]), t)];
  },
  '3.4': (p) => {
    const a = value(p, 'K₁', 95), b = value(p, 'K₂', 105), t = prices(p, [['K₁', 95], ['K₂', 105]]);
    return [P(line([80, 6], [a, 6]), t), P(line([a, 6], [b, -4]), t), P(line([b, -4], [120, -4]), t)];
  },
  '4.1': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([80, 6], [k, -14]), t), P(dot(k, -14), t), P(line([k, -14], [120, 6]), t)];
  },
  '4.2': (p) => {
    const a = value(p, 'K_p', 90), b = value(p, 'K_c', 110), t = prices(p, [['K_p', 90], ['K_c', 110]]);
    return [P(line([78, 4], [a, -8]), t), P(line([a, -8], [b, -8]), t), P(line([b, -8], [122, 4]), t)];
  },
  '4.3': (p) => {
    const a = value(p, 'K₁', 90), b = value(p, 'K₂', 100), c = value(p, 'K₃', 110), t = prices(p, [['K₁', 90], ['K₂', 100], ['K₃', 110]]);
    return [P(join(line([80, -2.5], [a, -2.5]), line([c, -2.5], [120, -2.5])), t), P(line([a, -2.5], [b, 7.5]), t), P(line([b, 7.5], [c, -2.5]), t)];
  },
  '4.4': (p) => {
    const a = value(p, 'K₁', 85), b = value(p, 'K₂', 95), c = value(p, 'K₃', 105), d = value(p, 'K₄', 115), t = prices(p, [['K₁', 85], ['K₂', 95], ['K₃', 105], ['K₄', 115]]);
    return [P(join(line([75, -3], [a, -3]), line([d, -3], [125, -3])), t), P(line([a, -3], [88, 0], [b, 7]), t), P(line([b, 7], [c, 7]), t), P(line([c, 7], [112, 0], [d, -3]), t)];
  },
  '5.1': (p) => barrierCall(p, 120, 'H_out', 'upOut'),
  '5.2': (p) => barrierCall(p, 88, 'H_out', 'downOut'),
  '5.3': (p) => barrierCall(p, 120, 'H_in', 'upIn'),
  '5.4': (p) => barrierCall(p, 88, 'H_in', 'downIn'),
  '5.5': (p) => barrierPut(p, 120, 'H_out', 'upOut'),
  '5.6': (p) => barrierPut(p, 82, 'H_out', 'downOut'),
  '5.7': (p) => barrierPut(p, 120, 'H_in', 'upIn'),
  '5.8': (p) => barrierPut(p, 82, 'H_in', 'downIn'),
  '6.1': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([80, -8], [k, -8]), t), P(line([k, 12, { endpoint: 'open' }], [120, 12]), t)];
  },
  '6.2': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([k, -8], [120, -8]), t), P(line([80, 12], [k, 12, { endpoint: 'open' }]), t)];
  },
  '6.3': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([80, -50], [k, -50]), t), P(line([k, 50, { endpoint: 'open' }], [120, 70]), t)];
  },
  '6.4': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([k, -50], [120, -50]), t), P(line([80, 30], [99, 49], [k, 50, { endpoint: 'open' }]), t)];
  },
  '6.5': (p) => {
    const h = value(p, 'H', 115), t = prices(p, [['H', 115]]); return [P(dot(h, 12), t), P(dot(h, -8), t)];
  },
  '6.6': (p) => {
    const h = value(p, 'H', 115), t = prices(p, [['H', 115]]); return [P(dot(h, 12), t), P(dot(h, -8), t)];
  },
  '7.1': (p) => airbag(p, 80, [[line([100, -5], [130, 25])], [line([80, -5, { endpoint: 'open' }], [100, -5])], [line([70, -35], [100, -5])]]),
  '7.2': (p) => airbag(p, 80, [[line([125, 25], [140, 25])], [line([100, 0], [125, 25])], [line([80, 0], [100, 0])], [line([70, -30], [100, 0])]]),
  '7.3': (p) => airbag(p, 80, [[line([100, 0], [140, 29.6])], [line([80, 0], [100, 0])], [line([70, -30], [100, 0])]]),
  '8.1': (p) => {
    const k = value(p, 'K', 90), h = value(p, 'H_out', 105), t = prices(p, [['K', 90], ['H_out', 105]]);
    return [P(line([k, 0], [110, 46000]), t), P(line([k, 0], [110, 122000]), t), P(join(dot(85, -210000), dot(95, 121500)), t)];
  },
  '9.1': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70]], [P(join(dot(103, 99.7), dot(103, 131.5))), P(line([70, 400], [103, 400, { endpoint: 'open' }])), P(line([65, -350], [100, 0]))]),
  '9.2': (p) => snowball(p, [['K', 85], ['H_out', 103], ['H_in', 70]], [P(dot(104, 80)), P(line([70, 240], [103, 240])), P(line([65, -200], [85, 0]))]),
  '9.3': (p) => snowball(p, [['K', 85], ['H_out', 103], ['H_in', 70]], [P(dot(104, 92.1)), P(line([85, 280], [103, 280])), P(line([85, 0], [103, 0])), P(line([65, -235.29], [85, 0]))]),
  '9.4': (p) => snowball(p, [['K', 90], ['H_out', 103], ['H_in', 70]], [P(dot(104, 105.2)), P(line([90, 320], [103, 320])), P(line([60, -333.3], [90, 0], [110, 222.2]))]),
  '9.5': (p) => snowball(p, [['K', 100], ['H_out', 100], ['H_in', 65]], [P(dot(100, 25.3)), P(line([65, 154], [100, 154])), P(line([60, -400], [100, 0]))]),
  '9.6': (p) => snowball(p, [['K', 100], ['H_out', 100], ['H_in', 65]], [P(dot(100, 14.1)), P(line([65, 86], [100, 86])), P(line([60, -350], [65, -350], [70, -300], [100, 0]))]),
  '9.7': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70], ['H_{in,2}', 75]], [P(dot(103, 22)), P(line([75, 440], [103, 440])), P(line([70, -300], [100, 0]))]),
  '9.8': (p) => snowball(p, [['K', 100], ['H_out', 97], ['H_in', 80]], [P(join(dot(80, 53.9), dot(97, 71.9))), P(line([80, 216], [97, 216])), P(line([77, -230], [100, 0]))]),
  '9.9': (p) => snowball(p, [['K', 100], ['H_{out,t}', 103], ['H_in', 70]], [P(dot(103, 126.5)), P(line([70, 380], [103, 380])), P(line([65, -350], [100, 0]))]),
  '9.10': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_{out,2}', 70], ['H_in', 70]], [P(dot(103, 74.8)), P(line([70, 300], [100, 300])), P(line([65, -350], [70, -300]))]),
  '9.11': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70]], [P(join(dot(110, 84.3), dot(110, 134.9))), P(line([70, 300], [103, 300])), P(line([60, -400], [100, 0]))]),
  '9.12': (p) => snowball(p, [['K', 100], ['H_out', 100], ['H_in', 85]], [P(dot(100, 82.2)), P(dot(100, 92.1)), P(line([85, 100], [100, 100])), P(line([80, -200], [100, 0]))]),
  '9.13': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 95], ['H_floor', 95]], [P(dot(103, 51)), P(line([95, 77.3], [103, 77.3])), P(line([90, -50], [95, -50], [100, 0]))]),
  '9.14': (p) => snowball(p, [['K', 100], ['H_out', 100], ['H_in', 80], ['H_reset', 90]], [P(dot(100, 24)), P(dot(90, 48.6)), P(line([80, 146], [100, 146])), P(line([85, -150], [100, 0]))]),
  '9.15': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70], ['H_{hedge,1}', 80], ['H_{hedge,2}', 75]], [P(dot(103, 150)), P(dot(80, 15)), P(line([70, 300], [103, 300])), P(line([85, -150], [100, 0]))]),
  '9.16': (p) => snowball(p, [['K', 100], ['H_out', 103]], [P(dot(103, 32.2)), P(line([70, -100], [103, -100]))]),
  '9.17': (p) => snowball(p, [['K', 100], ['H_out', 100]], [P(join(dot(100, 78.9), dot(103, 213.7))), P(line([70, 55], [100, 55]))]),
  '9.18': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70]], [P(dot(104, 75)), P(line([70, 225], [103, 225])), P(dot(100, 0)), P(line([80, -200], [100, 0]))]),
  '9.19': (p) => snowball(p, [['K', 100], ['H_c', 80], ['H_out', 103], ['H_in', 70]], [P(dot(104, 30)), P(line([70, 100], [103, 100])), P(line([100, 80], [103, 80])), P(line([65, -250], [100, 100]))]),
  '9.20': (p) => snowball(p, [['K', 100], ['H_c', 80], ['H_out', 103], ['H_in', 70]], [P(dot(103, 30)), P(line([70, 67.5], [103, 67.5])), P(line([100, 60], [103, 60])), P(line([60, -90], [85, -90], [90, -40], [100, 60]))]),
  '9.21': (p) => snowball(p, [['K', 90], ['H_out', 103]], [P(dot(104, 36)), P(line([90, 144], [103, 144])), P(line([75, -22.7], [90, 144]))]),
  '9.22': (p) => snowball(p, [['K', 90], ['H_out', 103], ['H_in', 70]], [P(dot(104, 54)), P(line([70, 162], [103, 162])), P(line([90, 162], [103, 162])), P(line([75, -4.7], [90, 162]))]),
  '9.23': (p) => snowball(p, [['K', 90], ['H_out', 103], ['H_in', 70]], [P(dot(103, 45)), P(line([70, 108], [103, 108])), P(line([90, 108], [103, 108])), P(line([60, -42], [76.5, -42], [90, 108]))]),
  '9.24': () => [P(dot(4, 40)), P(line([0, 0], [3, 30], [9, 90], [12, 120]))],
  '9.25': () => [P(dot(4, 36)), P(line([0, 30], [9, 88.5], [12, 108]))],
  '9.26': (p) => snowball(p, [['K', 100], ['H_out', 103], ['H_in', 70]], [P(dot(104, 120)), P(line([70, 40], [103, 40])), P(dot(100, 0)), P(line([65, -350], [100, 0]))]),
  '9.27': (p) => snowball(p, [['K', 100], ['H_out', 103], ['B', 80]], [P(dot(103, 65.8)), P(line([80, 0], [105, 250])), P(line([75, -250], [80, -200]))]),
  '9.28': (p) => snowball(p, [['K', 100], ['H_out', 103], ['L', 95]], [P(dot(103, 38.5)), P(line([100, 0], [120, 0])), P(line([95, -50], [100, 0])), P(line([70, -50], [95, -50]))]),
  '9.29': (p) => snowball(p, [['K', 100], ['H_out', 103], ['L', 80]], [P(dot(104, 45)), P(line([100, 0], [102, 30])), P(line([70, -100], [80, -100], [90, -50], [100, 0]))]),
  '10.1': (p) => {
    const k = value(p, 'K', 100), t = prices(p, [['K', 100]]);
    return [P(line([k, -1.59], [110, 8.41]), t), P(line([80, -1.59], [k, -1.59]), t)];
  },
  '10.2': (p) => discountCall(p),
  '10.3': (p) => discountCall(p),
  '10.4': (p) => {
    const k = value(p, 'K', 13), t = prices(p, [['K', 13]]);
    return [P(line([k, 0], [14, 6.75], [15, 14], [20, 57.75]), t), P(dot(k, 0), t), P(line([9, -22], [10, -17.25], [11, -12], [12, -6.25], [k, 0]), t)];
  },
  '10.5': (p) => {
    const k = value(p, 'K', 100), h = value(p, 'H_out', 110), t = prices(p, [['K', 100], ['H_out', 110]]);
    return [P(dot(h, 24), t), P(line([k, -26], [h, 74, { endpoint: 'open' }]), t), P(line([80, -26], [k, -26]), t)];
  },
  '10.6': (p) => {
    const k = value(p, 'K', 100), h = value(p, 'H_out', 90), t = prices(p, [['K', 100], ['H_out', 90]]);
    return [P(dot(h, 20), t), P(line([h, 70, { endpoint: 'open' }], [k, -30]), t), P(line([k, -30], [120, -30]), t)];
  },
  '10.7': (p) => {
    const ku = value(p, 'K_u', 105), kd = value(p, 'K_d', 95), hu = value(p, 'H_u', 115), hd = value(p, 'H_d', 85);
    const t = prices(p, [['K_u', 105], ['K_d', 95], ['H_u', 115], ['H_d', 85]]);
    return [P(dot(hu, 21.2), t), P(dot(hd, 31.2), t), P(line([ku, -8.8], [hu, 41.2, { endpoint: 'open' }]), t), P(line([hd, 41.2, { endpoint: 'open' }], [kd, -8.8]), t), P(line([kd, -8.8], [ku, -8.8]), t)];
  },
  '10.8': () => [P(dot(22, 0.24)), P(line([0, -1.21, { endpoint: 'open' }], [22, 0.24, { endpoint: 'open' }])), P(dot(0, -1.21))],
});

function barrierCall(parameters, fallbackBarrier, token, mode) {
  const k = value(parameters, 'K', 100), h = value(parameters, token, fallbackBarrier), t = prices(parameters, [['K', 100], [token, fallbackBarrier]]);
  const low = 80;
  const plainCall = line([k, -5], [105, 0], [125, 20]);
  if (mode === 'upOut') return [P(dot(h, -5), t), P(line([k, -5], [h, h - k - 5, { endpoint: 'open' }]), t), P(line([low, -5], [k, -5]), t)];
  if (mode === 'downOut') return [P(dot(h, -5), t), P(plainCall, t), P(line([h, -5, { endpoint: 'open' }], [k, -5]), t)];
  if (mode === 'upIn') return [P(dot(h, -5), t), P(plainCall, t), P(line([low, -5], [k, -5]), t)];
  return [P(dot(h, -5), t), P(line([k, -5], [110, 5]), t), P(line([h, -5], [k, -5]), t)];
}

function barrierPut(parameters, fallbackBarrier, token, mode) {
  const k = value(parameters, 'K', 100), h = value(parameters, token, fallbackBarrier), t = prices(parameters, [['K', 100], [token, fallbackBarrier]]);
  const plainPut = line([80, 15], [95, 0], [k, -5]);
  if (mode === 'upOut') return [P(dot(h, -5), t), P(plainPut, t), P(line([k, -5], [h, -5, { endpoint: 'open' }]), t)];
  if (mode === 'downOut') return [P(dot(h, -5), t), P(line([h, k - h - 5, { endpoint: 'open' }], [95, 0], [k, -5]), t), P(line([k, -5], [120, -5]), t)];
  if (mode === 'upIn') return [P(dot(h, -5), t), P(plainPut, t), P(line([k, -5], [120, -5]), t)];
  return [P(dot(h, -5), t), P(line([75, 20], [90, 5], [k, -5]), t), P(line([k, -5], [120, -5]), t)];
}

function airbag(parameters, fallbackBarrier, groups) {
  const h = value(parameters, 'H_in', fallbackBarrier);
  const t = prices(parameters, [['H_in', fallbackBarrier]]);
  return groups.map(([points]) => P(points, t));
}

function snowball(parameters, definitions, outputs) {
  const t = prices(parameters, definitions);
  return outputs.map((item) => P(item.points, t));
}

function discountCall(parameters) {
  const a = value(parameters, 'K₁', 80), b = value(parameters, 'K₂', 100), t = prices(parameters, [['K₁', 80], ['K₂', 100]]);
  return [P(line([b, 0], [110, 80]), t), P(line([a, -200], [b, 0]), t), P(line([60, -200], [a, -200]), t)];
}

export function createFormulaGraphics(product, scenarios, examples) {
  const definition = FORMULAS[product.id];
  if (!definition) throw new Error(`未配置${product.id}的公式Payoff规则。`);
  return make(product.id, scenarios, examples.parameters, examples.axis, definition);
}

export function formulaCoverage() {
  return Object.keys(FORMULAS).sort((left, right) => left.localeCompare(right, 'zh-Hans-CN', { numeric: true }));
}
