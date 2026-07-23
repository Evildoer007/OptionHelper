import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  DEFAULT_AXIS,
  COUPON_COUNT_AXIS,
  OBSERVED_DAYS_AXIS,
  VOLATILITY_AXIS,
  AXIS_KINDS,
  CURVE_ENDPOINTS,
  MAX_GUIDES,
  MAX_POINTS,
  MAX_THRESHOLDS,
  VALUE_DECIMALS,
  axisRatios,
  axisReference,
  axisUnit,
  createBlankGraphic,
  defaultScale,
  guideExtent,
  guideMeta,
  normalizeAxis,
  pointInScale,
  ratioToX,
  ratioToY,
  scaleIssues,
  thresholdMeta,
  valueInScale,
  xToRatio,
  yToRatio,
} from './public/graphic-model.mjs';
import { parseExampleSource } from './example-source.mjs';
import { createFormulaGraphics } from './formula-payoff.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(HERE, '../../..');
export const PATHS = Object.freeze({
  optionList: path.join(ROOT, 'references', 'optionlist.md'),
  optionLib: path.join(ROOT, 'references', 'optionlib.md'),
  payoff: path.join(ROOT, 'assets', 'payoff'),
  source: path.join(ROOT, 'assets', 'payoff-editor', 'state'),
  history: path.join(ROOT, 'assets', 'payoff-editor', 'state', 'history'),
});

export const SCHEMA = 'optionhelper-payoff/v2';
export const LEGACY_SCHEMA = 'optionhelper-payoff/v1';
export const THRESHOLD_TOKENS = Object.freeze([
  'S₀', 'K', 'K₁', 'K₂', 'K₃', 'K₄', 'K_p', 'K_c', 'K_u', 'K_d',
  'H', 'H_in', 'H_{in,2}', 'H_out', 'H_{out,2}', 'H_{out,t}', 'H_c', 'H_buffer', 'H_floor', 'H_reset', 'H_u', 'H_d',
]);
export const GUIDE_DIRECTIONS = ['horizontal', 'vertical'];
export const GUIDE_STYLES = ['solid', 'dashed'];
export const PAYOFF_SCENARIO_HEADER = '情景';
export const PAYOFF_CONDITION_HEADER = '判断条件';

const CARD = Object.freeze({
  width: 728,
  height: 468,
  pitch: 480,
  plot: Object.freeze({ left: 42, right: 684, top: 70, bottom: 426 }),
});
const LINE_WIDTH_DECIMALS = 2;

const text = (value) => String(value ?? '').trim();
const hash = (value) => createHash('sha256').update(value).digest('hex').slice(0, 16);
const html = (value) => text(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const escapeRegExp = (value) => text(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const rounded = (value, decimals) => Math.round((value + Number.EPSILON) * (10 ** decimals)) / (10 ** decimals);
const clone = (value) => structuredClone(value);

function defaultAxisForProduct(id) {
  if (id === '10.4') return { ...VOLATILITY_AXIS };
  if (id === '10.8') return { ...OBSERVED_DAYS_AXIS };
  if (id === '9.24' || id === '9.25') return { ...COUPON_COUNT_AXIS };
  return { ...DEFAULT_AXIS };
}

function libraryAxis(library) {
  return normalizeAxis(library?.axis || defaultAxisForProduct(library?.id));
}

export function productFileBase(id, name) {
  const safeId = text(id).replace(/[^0-9.]/g, '');
  const safeName = text(name).replace(/[<>:"/\\|?*\u0000-\u001f]/g, '');
  return `${safeId}_${safeName}`;
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function parseTable(lines, start) {
  if (!lines[start]?.trim().startsWith('|') || !lines[start + 1]?.trim().startsWith('|')) return null;
  const headers = tableCells(lines[start]);
  const divider = tableCells(lines[start + 1]);
  if (headers.length !== divider.length || !divider.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
  const rows = [];
  let cursor = start + 2;
  while (cursor < lines.length && lines[cursor].trim().startsWith('|')) {
    const cells = tableCells(lines[cursor]);
    if (cells.length !== headers.length) break;
    rows.push(Object.fromEntries(headers.map((header, index) => [header, cells[index]])));
    cursor += 1;
  }
  return { headers, rows, end: cursor };
}

export function parseOptionList(markdown) {
  const lines = markdown.split(/\r?\n/);
  const headerIndex = lines.findIndex((line) => line.includes('| 中文唯一名称 |') && (line.includes('| 分类编号 |') || line.includes('| 标题编号 |')));
  if (headerIndex < 0) throw new Error('未找到optionlist.md的产品表头。');
  const headers = tableCells(lines[headerIndex]);
  const nameIndex = headers.indexOf('中文唯一名称');
  const idIndex = headers.indexOf('分类编号') >= 0 ? headers.indexOf('分类编号') : headers.indexOf('标题编号');
  const categoryIndex = headers.indexOf('所属类别');
  const statusIndex = headers.indexOf('入库情况');
  const products = [];
  for (let index = headerIndex + 2; index < lines.length && lines[index].trim().startsWith('|'); index += 1) {
    const cells = tableCells(lines[index]);
    if (cells.length !== headers.length) continue;
    products.push({ id: cells[idIndex], name: cells[nameIndex], category: cells[categoryIndex], status: cells[statusIndex] });
  }
  return products;
}

function parsePayoffTable(optionLib, product) {
  const productHeading = new RegExp(`^###\\s+${escapeRegExp(product.id)}\\s+${escapeRegExp(product.name)}\\s*$`, 'm');
  const headingMatch = productHeading.exec(optionLib);
  if (!headingMatch) return { issue: { code: 'OPTIONLIB_PRODUCT_MISSING', message: `未找到“${product.id} ${product.name}”产品章节。请在optionlib.md补充同编号、同名称的正文。` } };
  const productStart = headingMatch.index;
  const nextProduct = optionLib.slice(productStart + headingMatch[0].length).search(/^###\s+/m);
  const productText = optionLib.slice(productStart, nextProduct < 0 ? undefined : productStart + headingMatch[0].length + nextProduct);
  const sectionPattern = new RegExp(`^####\\s+${escapeRegExp(product.id)}\\.2\\s+损益结构\\s*$`, 'm');
  const sectionMatch = sectionPattern.exec(productText);
  const hint = `#### ${product.id}.2 损益结构`;
  if (!sectionMatch) return { issue: { code: 'PAYOFF_SECTION_MISSING', message: `未找到“${hint}”。请在optionlib.md补充损益结构表及“情景”列。`, hint } };
  const sectionStart = sectionMatch.index + sectionMatch[0].length;
  const sectionEndOffset = productText.slice(sectionStart).search(/^####\s+/m);
  const section = productText.slice(sectionStart, sectionEndOffset < 0 ? undefined : sectionStart + sectionEndOffset);
  const lines = section.split(/\r?\n/);
  let table = null;
  for (let index = 0; index < lines.length; index += 1) {
    table = parseTable(lines, index);
    if (table) break;
  }
  if (!table) return { issue: { code: 'PAYOFF_TABLE_MISSING', message: `“${hint}”中没有有效Markdown表。请补充包含“情景”列的损益结构表。`, hint } };
  if (table.headers[0] !== PAYOFF_SCENARIO_HEADER || table.headers[1] !== PAYOFF_CONDITION_HEADER) return { issue: { code: 'PAYOFF_TABLE_HEADERS_INVALID', message: `“${hint}”的表头必须依次以“${PAYOFF_SCENARIO_HEADER}”“${PAYOFF_CONDITION_HEADER}”开头，且不接受同义名称。请修改optionlib.md后重新检测。`, hint } };
  const payoffColumn = table.headers.find((header) => header.includes('净损益'));
  if (!payoffColumn || table.rows.length === 0 || table.rows.some((row) => !text(row[PAYOFF_SCENARIO_HEADER]) || !text(row[PAYOFF_CONDITION_HEADER]))) return { issue: { code: 'SCENARIO_ROWS_INVALID', message: `“${hint}”必须包含非空的“情景”“判断条件”行和净损益列。请修改optionlib.md后重新检测。`, hint } };
  const scenarios = table.rows.map((row, index) => ({
    id: `${product.id}.2-${String(index + 1).padStart(2, '0')}`,
    title: row[PAYOFF_SCENARIO_HEADER],
    condition: row[PAYOFF_CONDITION_HEADER],
    payoff: row[payoffColumn],
  }));
  return { scenarios, section: hint, productText };
}

export async function loadLibrary() {
  const [optionList, optionLib] = await Promise.all([fs.readFile(PATHS.optionList, 'utf8'), fs.readFile(PATHS.optionLib, 'utf8')]);
  const products = parseOptionList(optionList);
  const seenIds = new Set();
  const seenNames = new Set();
  for (const product of products) {
    if (seenIds.has(product.id) || seenNames.has(product.name)) throw new Error(`optionlist.md存在重复产品：${product.id} ${product.name}`);
    seenIds.add(product.id);
    seenNames.add(product.name);
  }
  return { products, optionLib };
}

export async function resolveProduct(idOrName) {
  const library = await loadLibrary();
  const product = library.products.find((item) => item.id === idOrName || item.name === idOrName);
  if (!product) return { issue: { code: 'OPTIONLIST_PRODUCT_MISSING', message: `产品“${idOrName}”不在optionlist.md中，不能创建Payoff图。` } };
  const parsed = parsePayoffTable(library.optionLib, product);
  if (parsed.issue) return { product, issue: parsed.issue };
  const examples = parseExampleSource(parsed.productText, product);
  if (examples.issue) return { product, issue: examples.issue };
  const axis = libraryAxis({ id: product.id, axis: examples.axis });
  const fingerprint = hash(JSON.stringify({ id: product.id, name: product.name, scenarios: parsed.scenarios, examples: examples.raw, axis }));
  return { product, scenarios: parsed.scenarios, section: parsed.section, examples, axis, fingerprint };
}

export async function listProducts() {
  const { products, optionLib } = await loadLibrary();
  return products.map((product) => {
    const parsed = parsePayoffTable(optionLib, product);
    if (parsed.issue) return { ...product, available: false, issue: parsed.issue };
    const examples = parseExampleSource(parsed.productText, product);
    if (examples.issue) return { ...product, available: false, issue: examples.issue };
    return { ...product, available: true, scenarioCount: parsed.scenarios.length, exampleCount: examples.rows.length, scenarios: parsed.scenarios, section: parsed.section, exampleSection: examples.section, axis: libraryAxis({ id: product.id, axis: examples.axis }) };
  });
}

export async function createConfig(idOrName) {
  const resolved = await resolveProduct(idOrName);
  if (resolved.issue) throw new Error(resolved.issue.message);
  return {
    $schema: SCHEMA,
    schemaVersion: 2,
    library: { id: resolved.product.id, name: resolved.product.name, sourceFingerprint: resolved.fingerprint, section: resolved.section, scenarios: resolved.scenarios, axis: resolved.axis },
    graphics: createFormulaGraphics(resolved.product, resolved.scenarios, resolved.examples),
  };
}

function isLegacyConfig(config) {
  return config?.$schema === LEGACY_SCHEMA && config?.schemaVersion === 1;
}

export function migrateConfig(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return input;
  if (input.$schema === SCHEMA && input.schemaVersion === 2) {
    const config = clone(input);
    config.library = { ...config.library, axis: libraryAxis(config.library) };
    config.graphics = (config.graphics || []).map((graphic) => ({
      ...graphic,
      guides: Array.isArray(graphic?.guides) ? graphic.guides.map((guide) => ({ ...guide, style: 'dashed' })) : graphic?.guides,
    }));
    return config;
  }
  if (!isLegacyConfig(input)) return clone(input);
  const config = clone(input);
  config.$schema = SCHEMA;
  config.schemaVersion = 2;
  config.library = { ...config.library, axis: libraryAxis(config.library) };
  config.graphics = (config.graphics || []).map((legacyGraphic) => {
    const scale = defaultScale();
    const oldCurve = legacyGraphic?.curve || {};
    return {
      scenarioId: legacyGraphic?.scenarioId,
      scale,
      curve: {
        type: oldCurve.type || 'polyline',
        strokeWidth: oldCurve.strokeWidth ?? 4.4,
        points: Array.isArray(oldCurve.points) ? oldCurve.points.map((point) => ({ x: ratioToX(point.x, scale), y: ratioToY(point.y, scale) })) : [],
      },
      thresholds: Array.isArray(legacyGraphic?.thresholds) ? legacyGraphic.thresholds.map((threshold) => ({ token: threshold.token, value: ratioToX(threshold.x, scale) })) : [],
      guides: Array.isArray(legacyGraphic?.guides) ? legacyGraphic.guides.map((guide) => ({
        direction: guide.direction,
        value: guide.direction === 'horizontal' ? ratioToY(guide.position, scale) : ratioToX(guide.position, scale),
        style: 'dashed',
      })) : [],
    };
  });
  return config;
}

function validationError(code, message, extra = {}) {
  return { code, message, ...extra };
}

function keyCheck(value, allowed, label, errors) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    errors.push(validationError('INVALID_OBJECT', `${label}必须是对象。`));
    return;
  }
  for (const key of Object.keys(value)) if (!allowed.includes(key)) errors.push(validationError('LOCKED_FIELD', `${label}不允许字段“${key}”。文字与资料库绑定字段不可编辑。`));
}

function numberCheck(value, label, errors, { min = -Infinity, max = Infinity, decimals = VALUE_DECIMALS } = {}) {
  if (!Number.isFinite(value) || value < min || value > max) {
    errors.push(validationError('GRAPH_VALUE_INVALID', `${label}不是有效范围内的数值。`));
    return;
  }
  if (Math.abs(value - rounded(value, decimals)) > 1e-9) errors.push(validationError('GRAPH_PRECISION_INVALID', `${label}最多保留${decimals}位小数。`));
}

export async function validateConfig(config, { requireComplete = false } = {}) {
  const errors = [];
  const warnings = [];
  if (!config || typeof config !== 'object' || Array.isArray(config)) return { errors: [validationError('CONFIG_INVALID', 'JSON根节点必须是对象。')], warnings: [] };
  keyCheck(config, ['$schema', 'schemaVersion', 'library', 'graphics'], '根对象', errors);
  if (config.$schema !== SCHEMA || config.schemaVersion !== 2) errors.push(validationError('SCHEMA_INVALID', `仅支持${SCHEMA}。`));
  keyCheck(config.library, ['id', 'name', 'sourceFingerprint', 'section', 'scenarios', 'axis'], 'library', errors);
  keyCheck(config.library?.axis, ['kind', 'label', 'unit', 'referenceValue'], 'library.axis', errors);
  const axis = libraryAxis(config.library);
  if (!AXIS_KINDS.includes(axis.kind) || !Number.isFinite(axis.referenceValue) || !text(axis.label)) errors.push(validationError('AXIS_INVALID', '资料库横轴定义无效。'));
  if (!Array.isArray(config.graphics)) errors.push(validationError('GRAPHICS_INVALID', 'graphics必须是数组。'));
  if (!config.library?.id || !config.library?.name) return { errors, warnings };

  const resolved = await resolveProduct(config.library.id);
  if (resolved.issue) return { errors: [...errors, validationError('LIBRARY_PRODUCT_MISMATCH', resolved.issue.message, { action: 'update_optionlib' })], warnings };
  if (resolved.product.name !== config.library.name) errors.push(validationError('LIBRARY_PRODUCT_MISMATCH', '产品名称已与optionlist.md不一致。请先修改optionlist.md或重新创建该产品图。', { action: 'update_optionlib' }));
  const expectedLibrary = { id: resolved.product.id, name: resolved.product.name, sourceFingerprint: resolved.fingerprint, section: resolved.section, scenarios: resolved.scenarios, axis: resolved.axis };
  const providedLibrary = { id: config.library.id, name: config.library.name, sourceFingerprint: config.library.sourceFingerprint, section: config.library.section, scenarios: config.library.scenarios, axis: config.library.axis };
  if (!equal(providedLibrary, expectedLibrary)) errors.push(validationError('LIBRARY_SCENARIO_MISMATCH', `当前图的情景或示例与optionlib.md不一致。请检查${resolved.section}及${resolved.examples.section}后重新导入情景。`, { action: 'update_optionlib', section: resolved.section }));
  if (!Array.isArray(config.graphics) || !Array.isArray(config.library.scenarios)) return { errors, warnings, resolved };
  const expectedIds = resolved.scenarios.map((item) => item.id);
  const actualIds = config.graphics.map((item) => item?.scenarioId);
  if (!equal(actualIds, expectedIds)) errors.push(validationError('GRAPHICS_SCENARIO_MISMATCH', '子图必须与损益结构表的情景逐项、一一对应，不能新增、删除或重排。', { action: 'update_optionlib' }));

  for (const [index, graphic] of config.graphics.entries()) {
    const label = `情景${index + 1}`;
    keyCheck(graphic, ['scenarioId', 'scale', 'curve', 'thresholds', 'guides'], `graphics[${index}]`, errors);
    keyCheck(graphic?.scale, ['xMin', 'xMax', 'yMin', 'yMax'], `graphics[${index}].scale`, errors);
    for (const field of ['xMin', 'xMax', 'yMin', 'yMax']) numberCheck(graphic?.scale?.[field], `${label}坐标范围${field}`, errors);
    for (const issue of scaleIssues(graphic?.scale, axis)) errors.push(validationError('SCALE_INVALID', `${label}${issue}`));
    const scale = graphic?.scale;
    keyCheck(graphic?.curve, ['type', 'strokeWidth', 'points'], `graphics[${index}].curve`, errors);
    if (!['polyline', 'step'].includes(graphic?.curve?.type)) errors.push(validationError('CURVE_TYPE_INVALID', `graphics[${index}]仅支持polyline或step。`));
    numberCheck(graphic?.curve?.strokeWidth, `${label}曲线线宽`, errors, { min: 0.1, max: 20, decimals: LINE_WIDTH_DECIMALS });
    const points = graphic?.curve?.points;
    if (!Array.isArray(points)) errors.push(validationError('POINTS_INVALID', `graphics[${index}].curve.points必须是数组。`));
    else if (points.length === 0) warnings.push(validationError('CURVE_PENDING', `${label}尚未绘制收益曲线。`));
    else {
      if (points.length < 2 || points.length > MAX_POINTS) errors.push(validationError('POINT_COUNT_INVALID', `${label}的曲线节点数必须是2至${MAX_POINTS}个。`));
      for (const [pointIndex, point] of points.entries()) {
        keyCheck(point, ['x', 'y', 'breakBefore', 'endpoint'], `graphics[${index}].curve.points[${pointIndex}]`, errors);
        numberCheck(point?.x, `${label}节点${pointIndex + 1}的${axis.label}`, errors);
        numberCheck(point?.y, `${label}节点${pointIndex + 1}的净损益`, errors);
        if (Object.prototype.hasOwnProperty.call(point || {}, 'breakBefore') && typeof point.breakBefore !== 'boolean') errors.push(validationError('POINT_BREAK_INVALID', `${label}节点${pointIndex + 1}的断线标记必须为true或false。`));
        if (Object.prototype.hasOwnProperty.call(point || {}, 'endpoint') && !CURVE_ENDPOINTS.includes(point.endpoint)) errors.push(validationError('POINT_ENDPOINT_INVALID', `${label}节点${pointIndex + 1}的端点样式只能为none、open或closed。`));
        if (scale && !pointInScale(point, scale)) errors.push(validationError('POINT_RANGE_INVALID', `${label}节点${pointIndex + 1}必须落在当前坐标范围内。`));
        if (pointIndex > 0) {
          const previous = points[pointIndex - 1];
          const invalidOrder = graphic.curve.type === 'step'
            ? point.x < previous.x
            : point.x < previous.x || (point.x === previous.x && !point.breakBefore);
          if (invalidOrder) errors.push(validationError('POINT_ORDER_INVALID', `${label}的${axis.label}必须${graphic.curve.type === 'step' ? '非递减' : '递增；同价跳变须在后一节点勾选断线'}。`));
        }
      }
    }
    if (!Array.isArray(graphic?.thresholds)) errors.push(validationError('THRESHOLD_INVALID', `graphics[${index}].thresholds必须是数组。`));
    else {
      if (graphic.thresholds.length > MAX_THRESHOLDS) errors.push(validationError('THRESHOLD_COUNT_INVALID', `${label}最多${MAX_THRESHOLDS}条关键价格线。`));
      const tokens = new Set();
      for (const [thresholdIndex, threshold] of graphic.thresholds.entries()) {
        keyCheck(threshold, ['token', 'value'], `graphics[${index}].thresholds[${thresholdIndex}]`, errors);
        if (!THRESHOLD_TOKENS.includes(threshold?.token)) errors.push(validationError('THRESHOLD_TOKEN_INVALID', `关键线只能使用标准标记：${THRESHOLD_TOKENS.join('、')}。`));
        if (tokens.has(threshold?.token)) errors.push(validationError('THRESHOLD_DUPLICATE', `${label}不能重复使用${threshold?.token}。`));
        tokens.add(threshold?.token);
        numberCheck(threshold?.value, `${label}关键价格`, errors);
        if (scale && !valueInScale(threshold?.value, scale.xMin, scale.xMax)) errors.push(validationError('THRESHOLD_RANGE_INVALID', `${label}关键价格必须落在当前X轴范围内。`));
      }
    }
    const guides = graphic?.guides;
    if (!Array.isArray(guides)) errors.push(validationError('GUIDE_INVALID', `graphics[${index}].guides必须是数组。`));
    else {
      if (guides.length > MAX_GUIDES) errors.push(validationError('GUIDE_COUNT_INVALID', `${label}最多${MAX_GUIDES}条辅助线。`));
      for (const [guideIndex, guide] of guides.entries()) {
        keyCheck(guide, ['direction', 'value', 'start', 'end', 'style'], `graphics[${index}].guides[${guideIndex}]`, errors);
        if (!GUIDE_DIRECTIONS.includes(guide?.direction)) errors.push(validationError('GUIDE_DIRECTION_INVALID', '辅助线只能为horizontal或vertical。'));
        if (!GUIDE_STYLES.includes(guide?.style)) errors.push(validationError('GUIDE_STYLE_INVALID', '辅助线只能为solid或dashed。'));
        numberCheck(guide?.value, `${label}辅助线数值`, errors);
        const hasStart = Object.prototype.hasOwnProperty.call(guide || {}, 'start');
        const hasEnd = Object.prototype.hasOwnProperty.call(guide || {}, 'end');
        if (hasStart !== hasEnd) errors.push(validationError('GUIDE_EXTENT_INVALID', `${label}辅助线的起点和终点必须同时填写。`));
        if (scale) {
          const [valueMin, valueMax] = guide?.direction === 'horizontal' ? [scale.yMin, scale.yMax] : [scale.xMin, scale.xMax];
          if (!valueInScale(guide?.value, valueMin, valueMax)) errors.push(validationError('GUIDE_RANGE_INVALID', `${label}辅助线必须落在当前${guide?.direction === 'horizontal' ? 'Y轴' : 'X轴'}范围内。`));
          if (hasStart && hasEnd) {
            const [extentMin, extentMax] = guide?.direction === 'horizontal' ? [scale.xMin, scale.xMax] : [scale.yMin, scale.yMax];
            numberCheck(guide?.start, `${label}辅助线起点`, errors);
            numberCheck(guide?.end, `${label}辅助线终点`, errors);
            if (!valueInScale(guide?.start, extentMin, extentMax) || !valueInScale(guide?.end, extentMin, extentMax)) errors.push(validationError('GUIDE_EXTENT_RANGE_INVALID', `${label}辅助线起止位置必须落在当前${guide?.direction === 'horizontal' ? 'X轴' : 'Y轴'}范围内。`));
            if (!(guide?.start < guide?.end)) errors.push(validationError('GUIDE_EXTENT_ORDER_INVALID', `${label}辅助线起点必须小于终点。`));
          }
        }
      }
    }
  }
  if (requireComplete && warnings.some((item) => item.code === 'CURVE_PENDING')) errors.push(validationError('PUBLISH_CURVE_PENDING', '所有情景均需完成收益曲线后才能发布。'));
  return { errors, warnings, resolved };
}

function plotGeometry(cardX, cardY, scale, axis) {
  const left = cardX + CARD.plot.left;
  const right = cardX + CARD.plot.right;
  const top = cardY + CARD.plot.top;
  const bottom = cardY + CARD.plot.bottom;
  const width = right - left;
  const height = bottom - top;
  const ratio = axisRatios(scale, axis);
  return { left, right, top, bottom, width, height, yAxisX: left + ratio.x * width, xAxisY: top + ratio.y * height };
}

function pointToSvg(point, geometry, scale) {
  return { x: geometry.left + xToRatio(point.x, scale) * geometry.width, y: geometry.top + yToRatio(point.y, scale) * geometry.height };
}

function curvePath(points, type, geometry, scale) {
  const converted = points.map((point) => pointToSvg(point, geometry, scale));
  if (!converted.length) return '';
  let d = `M${converted[0].x.toFixed(2)} ${converted[0].y.toFixed(2)}`;
  for (let index = 1; index < converted.length; index += 1) {
    if (points[index].breakBefore) d += ` M${converted[index].x.toFixed(2)} ${converted[index].y.toFixed(2)}`;
    else d += type === 'step' ? ` H${converted[index].x.toFixed(2)} V${converted[index].y.toFixed(2)}` : ` L${converted[index].x.toFixed(2)} ${converted[index].y.toFixed(2)}`;
  }
  return d;
}

function formatValue(value) {
  return String(rounded(value, VALUE_DECIMALS)).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

function svgToken(token) {
  const source = text(token);
  const match = /^([A-Za-z])_(?:\{(.+)\}|(.+))$/.exec(source);
  if (!match) return html(source);
  const suffix = match[2] || match[3];
  return `${html(match[1])}<tspan class="token-sub" baseline-shift="sub">${html(suffix)}</tspan>`;
}

function isImplicitDefaultStrike(thresholds, threshold, axis) {
  const strikeCount = thresholds.filter((item) => String(item?.token || '').startsWith('K')).length;
  return strikeCount === 1 && threshold?.token === 'K' && threshold?.value === axisReference(axis);
}

function graphicCard(scenario, graphic, index, cardX, cardY, editing, axis) {
  const { scale } = graphic;
  const geometry = plotGeometry(cardX, cardY, scale, axis);
  const xAxisLabelY = Math.min(geometry.bottom + 24, geometry.xAxisY + 24);
  const xReferenceAnchor = geometry.yAxisX >= geometry.right - 74 ? 'end' : 'start';
  const xReferenceX = geometry.yAxisX + (xReferenceAnchor === 'end' ? -10 : 10);
  const yLabelAnchor = geometry.yAxisX >= geometry.right - 54 ? 'end' : 'start';
  const curve = graphic.curve.points.length ? `<path class="payoff-line" data-element="curve" stroke-width="${graphic.curve.strokeWidth}" d="${curvePath(graphic.curve.points, graphic.curve.type, geometry, scale)}"/>` : `<text class="pending" data-element="pending" x="${((geometry.left + geometry.right) / 2).toFixed(2)}" y="${((geometry.top + geometry.bottom) / 2).toFixed(2)}" text-anchor="middle">待绘制</text>`;
  const curveMarkers = graphic.curve.points.filter((point) => point.endpoint && point.endpoint !== 'none').map((point, pointIndex) => {
    const originalIndex = graphic.curve.points.indexOf(point, pointIndex);
    const converted = pointToSvg(point, geometry, scale);
    return `<circle class="curve-endpoint ${point.endpoint}" data-element="curve-endpoint" data-point="${originalIndex}" cx="${converted.x.toFixed(2)}" cy="${converted.y.toFixed(2)}" r="5.4"/>`;
  }).join('');
  const thresholdLines = graphic.thresholds.filter((threshold) => !isImplicitDefaultStrike(graphic.thresholds, threshold, axis)).map((threshold) => {
    const thresholdIndex = graphic.thresholds.indexOf(threshold);
    const x = geometry.left + xToRatio(threshold.value, scale) * geometry.width;
    const meta = thresholdMeta(threshold.token);
    return `<g class="threshold-group ${meta.family}" data-element="threshold" data-index="${thresholdIndex}" data-token="${html(threshold.token)}"><line class="threshold ${meta.family}" style="stroke:${meta.color};stroke-dasharray:${meta.dash}" x1="${x.toFixed(2)}" y1="${geometry.top}" x2="${x.toFixed(2)}" y2="${geometry.bottom}"/>${editing ? `<circle class="threshold-handle ${meta.family}" style="stroke:${meta.color}" data-role="threshold" data-scenario="${index}" data-threshold="${thresholdIndex}" cx="${x.toFixed(2)}" cy="${geometry.bottom}" r="8"/>` : ''}</g>`;
  }).join('');
  const guideLines = graphic.guides.map((guide, guideIndex) => {
    const horizontal = guide.direction === 'horizontal';
    const position = horizontal ? geometry.top + yToRatio(guide.value, scale) * geometry.height : geometry.left + xToRatio(guide.value, scale) * geometry.width;
    const extent = guideExtent(guide, scale);
    const start = horizontal
      ? geometry.left + xToRatio(extent.start, scale) * geometry.width
      : geometry.top + yToRatio(extent.start, scale) * geometry.height;
    const end = horizontal
      ? geometry.left + xToRatio(extent.end, scale) * geometry.width
      : geometry.top + yToRatio(extent.end, scale) * geometry.height;
    const handlePosition = (start + end) / 2;
    const meta = guideMeta(guideIndex);
    const line = horizontal
      ? `<line class="guide" style="stroke:${meta.color};stroke-dasharray:${meta.dash}" x1="${start.toFixed(2)}" y1="${position.toFixed(2)}" x2="${end.toFixed(2)}" y2="${position.toFixed(2)}"/>`
      : `<line class="guide" style="stroke:${meta.color};stroke-dasharray:${meta.dash}" x1="${position.toFixed(2)}" y1="${start.toFixed(2)}" x2="${position.toFixed(2)}" y2="${end.toFixed(2)}"/>`;
    const handle = editing ? `<circle class="guide-handle" style="stroke:${meta.color}" data-role="guide" data-scenario="${index}" data-guide="${guideIndex}" cx="${(horizontal ? handlePosition : position).toFixed(2)}" cy="${(horizontal ? position : handlePosition).toFixed(2)}" r="7"/><circle class="guide-endpoint ${horizontal ? 'horizontal' : 'vertical'}" style="stroke:${meta.color}" data-role="guide-start" data-scenario="${index}" data-guide="${guideIndex}" cx="${(horizontal ? start : position).toFixed(2)}" cy="${(horizontal ? position : start).toFixed(2)}" r="5"/><circle class="guide-endpoint ${horizontal ? 'horizontal' : 'vertical'}" style="stroke:${meta.color}" data-role="guide-end" data-scenario="${index}" data-guide="${guideIndex}" cx="${(horizontal ? end : position).toFixed(2)}" cy="${(horizontal ? position : end).toFixed(2)}" r="5"/>` : '';
    return `<g data-element="guide" data-index="${guideIndex}">${line}${handle}</g>`;
  }).join('');
  const pointHandles = editing ? graphic.curve.points.map((point, pointIndex) => {
    const converted = pointToSvg(point, geometry, scale);
    return `<circle class="node-handle" data-role="node" data-scenario="${index}" data-point="${pointIndex}" cx="${converted.x.toFixed(2)}" cy="${converted.y.toFixed(2)}" r="8"/>`;
  }).join('') : '';
  const titleWidth = 592;
  const titleUnits = [...text(scenario.title)].reduce((total, character) => total + (character.charCodeAt(0) > 255 ? 1 : 0.56), 0);
  const titleFit = titleUnits * 17 > titleWidth ? ` textLength="${titleWidth}" lengthAdjust="spacingAndGlyphs"` : '';
  return `<g data-scenario-card="${index}" data-card-x="${cardX}" data-card-y="${cardY}">
    <rect class="card" x="${cardX}" y="${cardY}" width="${CARD.width}" height="${CARD.height}"/><line class="card-accent" x1="${cardX}" y1="${cardY + 2}" x2="${cardX + CARD.width}" y2="${cardY + 2}"/>
    <text class="cn scenario-no" x="${cardX + 24}" y="${cardY + 39}">情景${index + 1}</text><text class="cn scenario-name" x="${cardX + 85}" y="${cardY + 39}"${titleFit}>${html(scenario.title)}</text>
    <line class="axis" data-element="x-axis" x1="${geometry.left}" y1="${geometry.xAxisY.toFixed(2)}" x2="${geometry.right}" y2="${geometry.xAxisY.toFixed(2)}"/><line class="axis" data-element="y-axis" x1="${geometry.yAxisX.toFixed(2)}" y1="${geometry.top}" x2="${geometry.yAxisX.toFixed(2)}" y2="${geometry.bottom}"/>
    <path class="axis-arrow" data-element="axis-y-arrow" d="M${geometry.yAxisX.toFixed(2)} ${(geometry.top - 7).toFixed(2)} L${(geometry.yAxisX - 4.5).toFixed(2)} ${(geometry.top + 2).toFixed(2)} L${(geometry.yAxisX + 4.5).toFixed(2)} ${(geometry.top + 2).toFixed(2)} Z"/><path class="axis-arrow" data-element="axis-x-arrow" d="M${(geometry.right + 7).toFixed(2)} ${geometry.xAxisY.toFixed(2)} L${(geometry.right - 2).toFixed(2)} ${(geometry.xAxisY - 4.5).toFixed(2)} L${(geometry.right - 2).toFixed(2)} ${(geometry.xAxisY + 4.5).toFixed(2)} Z"/>
    ${guideLines}${thresholdLines}${curve}${curveMarkers}${pointHandles}
    <text class="cn axis-title" data-element="y-label" x="${geometry.yAxisX.toFixed(2)}" y="${geometry.top - 12}" text-anchor="${yLabelAnchor}">净收益</text><text class="cn reference-label" data-element="x-reference" x="${xReferenceX.toFixed(2)}" y="${xAxisLabelY.toFixed(2)}" text-anchor="${xReferenceAnchor}">${formatValue(axisReference(axis))}${axisUnit(axis)}</text><text class="cn axis-title" data-element="x-label" x="${geometry.right}" y="${xAxisLabelY.toFixed(2)}" text-anchor="end">${html(axis.label)}</text><text class="cn range-label" x="${geometry.left}" y="${(geometry.bottom + 42).toFixed(2)}">${formatValue(scale.xMin)}${axisUnit(axis)}</text><text class="cn range-label" x="${geometry.right}" y="${(geometry.bottom + 42).toFixed(2)}" text-anchor="end">${formatValue(scale.xMax)}${axisUnit(axis)}</text>
  </g>`;
}

function legendItems(config) {
  const axis = libraryAxis(config.library);
  const items = [{ kind: 'payoff', label: 'Payoff', width: 142 }];
  const guideIndices = new Set();
  for (const graphic of config.graphics) graphic.guides.forEach((_, index) => guideIndices.add(index));
  for (const index of [...guideIndices].sort((left, right) => left - right)) {
    const meta = guideMeta(index);
    items.push({ kind: 'guide', index, meta, label: meta.label, width: 132 });
  }
  const seen = new Set();
  for (const graphic of config.graphics) {
    for (const threshold of graphic.thresholds) {
      if (isImplicitDefaultStrike(graphic.thresholds, threshold, axis)) continue;
      const key = `${threshold.token}\u0000${threshold.value}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const meta = thresholdMeta(threshold.token);
      const label = `${meta.label} ${threshold.token} = ${formatValue(threshold.value)}${axisUnit(axis)}`;
      items.push({ kind: 'threshold', token: threshold.token, value: threshold.value, meta, axisUnit: axisUnit(axis), label, width: Math.max(184, 98 + [...label].length * 8) });
    }
  }
  return items;
}

function layoutLegend(items) {
  const rows = [[]];
  let width = 0;
  for (const item of items) {
    if (width && width + item.width > 1458) {
      rows.push([]);
      width = 0;
    }
    rows.at(-1).push({ ...item, x: 16 + width });
    width += item.width;
  }
  return rows;
}

function renderLegendItem(item, y) {
  if (item.kind === 'payoff') return `<line x1="${item.x}" y1="${y}" x2="${item.x + 42}" y2="${y}" stroke="#C8102E" stroke-width="4.4"/><text class="legend" x="${item.x + 55}" y="${y + 5}">Payoff</text>`;
  if (item.kind === 'guide') return `<line x1="${item.x}" y1="${y}" x2="${item.x + 42}" y2="${y}" stroke="${item.meta.color}" stroke-width="1.25" stroke-dasharray="${item.meta.dash}"/><text class="legend" x="${item.x + 55}" y="${y + 5}">${item.meta.label}</text>`;
  const { meta } = item;
  return `<line x1="${item.x}" y1="${y}" x2="${item.x + 42}" y2="${y}" stroke="${meta.color}" stroke-width="2.35" stroke-dasharray="${meta.dash}"/><text class="legend" x="${item.x + 55}" y="${y + 5}">${html(meta.label)} <tspan class="legend-token">${svgToken(item.token)}</tspan> = ${formatValue(item.value)}${item.axisUnit}</text>`;
}

export function renderPayoffSvg(config, { editing = false } = {}) {
  const axis = libraryAxis(config.library);
  const count = config.library.scenarios.length;
  const rows = Math.ceil(count / 2);
  const legendRows = layoutLegend(legendItems(config));
  const height = Math.max(636, 156 + CARD.pitch * rows + (legendRows.length - 1) * 28);
  const cards = config.library.scenarios.map((scenario, index) => graphicCard(scenario, config.graphics[index], index, 16 + (index % 2) * 740, 92 + Math.floor(index / 2) * CARD.pitch, editing, axis)).join('');
  const legend = legendRows.map((row, rowIndex) => row.map((item) => renderLegendItem(item, height - 20 - (legendRows.length - rowIndex - 1) * 28)).join('')).join('');
  const metadata = JSON.stringify(config).replaceAll(']]>', ']]]]><![CDATA[>');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="${height}" viewBox="0 0 1500 ${height}" role="img" aria-labelledby="title desc">
  <title id="title">${html(config.library.name)}情景Payoff图</title><desc id="desc">由OptionHelper资料库绑定的真实坐标情景Payoff图。图中文字由optionlist.md和optionlib.md生成。</desc>
  <metadata id="optionhelper-payoff-config" data-schema="${SCHEMA}"><![CDATA[${metadata}]]></metadata>
  <defs><linearGradient id="everbright-red-gold" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#C8102E"/><stop offset="1" stop-color="#D89B27"/></linearGradient><style>
  .cn{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif}.card{fill:#fff;stroke:#CFCFCF;stroke-width:1.1}.card-accent{stroke:#C8102E;stroke-width:4.5}.scenario-no{font-size:15px;font-weight:700;fill:#C8102E}.scenario-name{font-size:17px;font-weight:700;fill:#252525}.axis-title{font-size:12px;font-weight:600;fill:#252525}.reference-label,.range-label{font-size:12px;fill:#4F4F4F}.axis{stroke:#252525;stroke-width:1.9}.axis-arrow{fill:#252525}.threshold{stroke-width:2.35}.guide{stroke-width:1.25;opacity:.88}.payoff-line{fill:none;stroke:#C8102E;stroke-linecap:round;stroke-linejoin:round}.curve-endpoint{stroke:#C8102E;stroke-width:2.25}.curve-endpoint.open{fill:#fff}.curve-endpoint.closed{fill:#C8102E}.pending{font-family:"PingFang SC","Microsoft YaHei",sans-serif;font-size:18px;font-weight:700;fill:#CFCFCF;letter-spacing:3px}.legend{font-size:12px;fill:#777}.legend .token-sub{font-size:9px}.node-handle{fill:#fff;stroke:#C8102E;stroke-width:3;cursor:grab}.threshold-handle{fill:#fff;stroke-width:3;cursor:ew-resize}.guide-handle{fill:#fff;stroke-width:3;cursor:move}.guide-endpoint{fill:#fff;stroke-width:2.2}.guide-endpoint.horizontal{cursor:ew-resize}.guide-endpoint.vertical{cursor:ns-resize}
  </style></defs>
  <rect width="1500" height="${height}" fill="#fff"/><rect x="16" y="16" width="1468" height="64" fill="url(#everbright-red-gold)"/><rect x="16" y="16" width="9" height="64" fill="#A80F28"/><text class="cn" x="40" y="56" fill="#fff" font-size="30" font-weight="700">${html(config.library.name)}</text>
  ${cards}
  <line x1="16" y1="${height - 50 - (legendRows.length - 1) * 28}" x2="1484" y2="${height - 50 - (legendRows.length - 1) * 28}" stroke="#C8102E" stroke-width="2.4"/><g class="cn">${legend}</g>
</svg>`;
}

async function exists(file) {
  try { await fs.access(file); return true; } catch { return false; }
}

export async function verifyWorkspace() {
  const required = [PATHS.optionList, PATHS.optionLib];
  const checks = await Promise.all(required.map(async (file) => ({ file, exists: await exists(file) })));
  const missing = checks.filter((item) => !item.exists).map((item) => path.relative(ROOT, item.file));
  if (missing.length) throw new Error(`项目结构不完整，缺少：${missing.join('、')}。请保留完整项目中的references/和assets/payoff-editor/。`);
}

export async function ensureStorage(paths = PATHS, { includePayoff = false } = {}) {
  const directories = [paths.source, paths.history];
  if (includePayoff) directories.push(paths.payoff);
  await Promise.all(directories.map((directory) => fs.mkdir(directory, { recursive: true })));
}

export function storagePaths(config, paths = PATHS) {
  const base = productFileBase(config.library.id, config.library.name);
  return { base, source: path.join(paths.source, `${base}.payoff.json`), formalSvg: path.join(paths.payoff, `${base}_payoff.svg`) };
}

function nativeSvgPayload(svg) {
  const match = /<metadata\b[^>]*id=["']optionhelper-payoff-config["'][^>]*>\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*<\/metadata>/.exec(svg);
  if (!match) return null;
  try {
    const raw = JSON.parse(match[1].replaceAll(']]]]><![CDATA[>', ']]>'));
    return { raw, config: migrateConfig(raw) };
  } catch { return null; }
}

function nativeSvgConfig(svg) {
  return nativeSvgPayload(svg)?.config || null;
}

export async function buildPublishReview(input, { paths = PATHS } = {}) {
  const config = migrateConfig(input);
  const validation = await validateConfig(config);
  const hasIdentity = Boolean(config?.library?.id && config?.library?.name && Array.isArray(config?.graphics));
  const locations = hasIdentity ? storagePaths(config, paths) : null;
  let previous = null;
  let formalKind = 'new';
  if (locations && await exists(locations.formalSvg)) {
    const rawSvg = await fs.readFile(locations.formalSvg, 'utf8');
    previous = nativeSvgConfig(rawSvg);
    formalKind = previous ? 'replace' : 'replace-external';
  }
  const previousByScenario = new Map((previous?.graphics || []).map((graphic) => [graphic.scenarioId, graphic]));
  const scenarios = (config?.graphics || []).map((graphic, index) => {
    const previousGraphic = previousByScenario.get(graphic?.scenarioId);
    const state = !previousGraphic ? 'new' : equal(previousGraphic, graphic) ? 'unchanged' : 'changed';
    const source = config?.library?.scenarios?.[index];
    return {
      index: index + 1,
      title: source?.title || `情景${index + 1}`,
      curve: (graphic?.curve?.points || []).length >= 2 ? 'complete' : 'pending',
      points: graphic?.curve?.points?.length || 0,
      thresholds: graphic?.thresholds?.length || 0,
      guides: graphic?.guides?.length || 0,
      state,
    };
  });
  return {
    config,
    validation,
    formal: { kind: formalKind, exists: Boolean(previous || (locations && await exists(locations.formalSvg))) },
    summary: {
      scenarios: scenarios.length,
      complete: scenarios.filter((item) => item.curve === 'complete').length,
      pending: scenarios.filter((item) => item.curve === 'pending').length,
      changed: scenarios.filter((item) => item.state === 'changed').length,
      unchanged: scenarios.filter((item) => item.state === 'unchanged').length,
      added: scenarios.filter((item) => item.state === 'new').length,
    },
    scenarios,
  };
}

export async function loadStoredConfig(idOrName, { paths = PATHS } = {}) {
  const resolved = await resolveProduct(idOrName);
  if (resolved.issue) return { issue: resolved.issue };
  const base = productFileBase(resolved.product.id, resolved.product.name);
  const source = path.join(paths.source, `${base}.payoff.json`);
  if (!await exists(source)) return { product: resolved.product, config: null, source: null };
  try {
    const raw = JSON.parse(await fs.readFile(source, 'utf8'));
    const config = migrateConfig(raw);
    return { product: resolved.product, config, source: 'draft', migrated: !equal(raw, config) };
  } catch {
    return { product: resolved.product, issue: { code: 'CONFIG_READ_ERROR', message: `无法读取${path.basename(source)}。请检查JSON格式。` } };
  }
}

export async function saveDraft(input, { paths = PATHS } = {}) {
  const config = migrateConfig(input);
  const validation = await validateConfig(config);
  if (validation.errors.length) return { ...validation, saved: false, config };
  await ensureStorage(paths);
  const locations = storagePaths(config, paths);
  await fs.writeFile(locations.source, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
  return { ...validation, saved: true, locations, config };
}

export async function createTemplateDraft(idOrName) {
  const config = await createConfig(idOrName);
  const validation = await validateConfig(config);
  return { config, source: 'new', saved: false, ...validation };
}

export async function reimportDraft(idOrName) {
  const config = await createConfig(idOrName);
  await ensureStorage();
  const locations = storagePaths(config);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  if (await exists(locations.source)) await fs.rename(locations.source, path.join(PATHS.history, `${locations.base}_payoff.${stamp}.draft.json`));
  const saved = await saveDraft(config);
  return { config: saved.config, source: 'reimported', ...saved };
}

export async function publish(input, { paths = PATHS } = {}) {
  const config = migrateConfig(input);
  const validation = await validateConfig(config);
  // A formal SVG is a snapshot, not an approval workflow.  Validation remains
  // available to the editor as feedback, but does not block publishing a
  // renderable draft at any stage.
  if (!config?.library?.id || !config?.library?.name || !Array.isArray(config?.library?.scenarios) || !Array.isArray(config?.graphics)) {
    return { ...validation, published: false, config };
  }
  await ensureStorage(paths, { includePayoff: true });
  const locations = storagePaths(config, paths);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  if (await exists(locations.formalSvg)) await fs.rename(locations.formalSvg, path.join(paths.history, `${locations.base}_payoff.${stamp}.svg`));
  try {
    const svg = renderPayoffSvg(config);
    await Promise.all([fs.writeFile(locations.source, `${JSON.stringify(config, null, 2)}\n`, 'utf8'), fs.writeFile(locations.formalSvg, svg, 'utf8')]);
    return { ...validation, published: true, locations, config };
  } catch (error) {
    return {
      ...validation,
      errors: [...validation.errors, validationError('SVG_RENDER_FAILED', `无法生成SVG：${error.message || '图形数据不完整。'}`)],
      published: false,
      config,
    };
  }
}

export async function inspectNativeSvg(svg, { paths = PATHS } = {}) {
  const payload = nativeSvgPayload(svg);
  if (!payload) return { editable: false, reason: 'SVG内嵌配置无法解析或不是原生SVG，只能只读预览。' };
  const { raw, config } = payload;
  const validation = await validateConfig(config);
  if (validation.errors.length) return { editable: false, reason: validation.errors[0].message, validation };
  const locations = storagePaths(config, paths);
  if (!await exists(locations.source)) return { editable: false, reason: '未找到匹配的本地草稿JSON，只能只读预览。' };
  const local = migrateConfig(JSON.parse(await fs.readFile(locations.source, 'utf8')));
  if (!equal(local, config)) return { editable: false, reason: 'SVG与本地草稿JSON不一致，只能只读预览。' };
  return { editable: true, config, validation, migrated: !equal(raw, config) };
}

export async function cliRender(input, output) {
  const config = migrateConfig(input);
  const validation = await validateConfig(config);
  if (validation.errors.length) return { ...validation, config };
  const formalDir = `${PATHS.payoff}${path.sep}`;
  if (path.resolve(output).startsWith(formalDir)) return { errors: [validationError('FORMAL_WRITE_FORBIDDEN', '命令行不能写入正式SVG目录。请使用网页确认发布。')], warnings: [], config };
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.writeFile(output, renderPayoffSvg(config), 'utf8');
  return { ...validation, config };
}
