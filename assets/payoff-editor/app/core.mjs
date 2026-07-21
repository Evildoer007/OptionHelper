import { createHash } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 编辑器位于 assets/payoff-editor；资料库与正式产物仍以项目根目录为锚点。
export const ROOT = path.resolve(HERE, '../../..');
export const PATHS = Object.freeze({
  optionList: path.join(ROOT, 'references', 'optionlist.md'),
  optionLib: path.join(ROOT, 'references', 'optionlib.md'),
  payoff: path.join(ROOT, 'assets', 'payoff'),
  source: path.join(ROOT, 'assets', 'payoff-editor', 'state'),
  history: path.join(ROOT, 'assets', 'payoff-editor', 'state', 'history'),
});

export const SCHEMA = 'optionhelper-payoff/v1';
export const THRESHOLD_TOKENS = Object.freeze([
  'S₀', 'K', 'K₁', 'K₂', 'K₃', 'K₄', 'K_p', 'K_c', 'K_u', 'K_d',
  'H', 'H_in', 'H_out', 'H_floor', 'H_reset', 'H_u', 'H_d', 'H_{out,t}',
]);
export const GUIDE_DIRECTIONS = ['horizontal', 'vertical'];
export const GUIDE_STYLES = ['solid', 'dashed'];
export const PAYOFF_SCENARIO_HEADER = '情景';
export const PAYOFF_CONDITION_HEADER = '判断条件';

const CARD = Object.freeze({
  width: 728,
  height: 468,
  rowGap: 12,
  pitch: 480,
  plot: Object.freeze({ left: 42, right: 684, top: 70, bottom: 426 }),
});
const POSITION_DECIMALS = 4;
const LINE_WIDTH_DECIMALS = 2;

const text = (value) => String(value ?? '').trim();
const hash = (value) => createHash('sha256').update(value).digest('hex').slice(0, 16);
const html = (value) => text(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const escapeRegExp = (value) => text(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const rounded = (value, decimals) => Math.round((value + Number.EPSILON) * (10 ** decimals)) / (10 ** decimals);

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
    products.push({
      id: cells[idIndex],
      name: cells[nameIndex],
      category: cells[categoryIndex],
      status: cells[statusIndex],
    });
  }
  return products;
}

function parsePayoffTable(optionLib, product) {
  const productHeading = new RegExp(`^###\\s+${escapeRegExp(product.id)}\\s+${escapeRegExp(product.name)}\\s*$`, 'm');
  const headingMatch = productHeading.exec(optionLib);
  if (!headingMatch) {
    return { issue: { code: 'OPTIONLIB_PRODUCT_MISSING', message: `未找到“${product.id} ${product.name}”产品章节。请在optionlib.md补充同编号、同名称的正文。` } };
  }
  const productStart = headingMatch.index;
  const nextProduct = optionLib.slice(productStart + headingMatch[0].length).search(/^###\s+/m);
  const productText = optionLib.slice(productStart, nextProduct < 0 ? undefined : productStart + headingMatch[0].length + nextProduct);
  const sectionPattern = new RegExp(`^####\\s+${escapeRegExp(product.id)}\\.2\\s+损益结构\\s*$`, 'm');
  const sectionMatch = sectionPattern.exec(productText);
  const hint = `#### ${product.id}.2 损益结构`;
  if (!sectionMatch) {
    return { issue: { code: 'PAYOFF_SECTION_MISSING', message: `未找到“${hint}”。请在optionlib.md补充损益结构表及“情景”列。`, hint } };
  }
  const sectionStart = sectionMatch.index + sectionMatch[0].length;
  const sectionEndOffset = productText.slice(sectionStart).search(/^####\s+/m);
  const section = productText.slice(sectionStart, sectionEndOffset < 0 ? undefined : sectionStart + sectionEndOffset);
  const lines = section.split(/\r?\n/);
  let table = null;
  for (let index = 0; index < lines.length; index += 1) {
    table = parseTable(lines, index);
    if (table) break;
  }
  if (!table) {
    return { issue: { code: 'PAYOFF_TABLE_MISSING', message: `“${hint}”中没有有效Markdown表。请补充包含“情景”列的损益结构表。`, hint } };
  }
  if (table.headers[0] !== PAYOFF_SCENARIO_HEADER || table.headers[1] !== PAYOFF_CONDITION_HEADER) {
    return { issue: { code: 'PAYOFF_TABLE_HEADERS_INVALID', message: `“${hint}”的表头必须依次以“${PAYOFF_SCENARIO_HEADER}”“${PAYOFF_CONDITION_HEADER}”开头，且不接受同义名称。请修改optionlib.md后重新检测。`, hint } };
  }
  const payoffColumn = table.headers.find((header) => header.includes('净损益'));
  if (!payoffColumn || table.rows.length === 0 || table.rows.some((row) => !text(row[PAYOFF_SCENARIO_HEADER]) || !text(row[PAYOFF_CONDITION_HEADER]))) {
    return { issue: { code: 'SCENARIO_ROWS_INVALID', message: `“${hint}”必须包含非空的“情景”“判断条件”行和净损益列。请修改optionlib.md后重新检测。`, hint } };
  }
  const scenarios = table.rows.map((row, index) => ({
    id: `${product.id}.2-${String(index + 1).padStart(2, '0')}`,
    title: row[PAYOFF_SCENARIO_HEADER],
    condition: row[PAYOFF_CONDITION_HEADER],
    payoff: row[payoffColumn],
  }));
  return { scenarios, section: hint };
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
  const fingerprint = hash(JSON.stringify({ id: product.id, name: product.name, scenarios: parsed.scenarios }));
  return { product, scenarios: parsed.scenarios, section: parsed.section, fingerprint };
}

export async function listProducts() {
  const { products, optionLib } = await loadLibrary();
  return products.map((product) => {
    const parsed = parsePayoffTable(optionLib, product);
    if (parsed.issue) return { ...product, available: false, issue: parsed.issue };
    return {
      ...product,
      available: true,
      scenarioCount: parsed.scenarios.length,
      scenarios: parsed.scenarios,
      section: parsed.section,
    };
  });
}

// 轴位置仅决定绘图区内的排版；默认交点是X=100%、Y=0的中心位置。
const defaultAxis = () => ({ left: 0.5, bottom: 0.5 });

export async function createConfig(idOrName) {
  const resolved = await resolveProduct(idOrName);
  if (resolved.issue) throw new Error(resolved.issue.message);
  return {
    $schema: SCHEMA,
    schemaVersion: 1,
    library: {
      id: resolved.product.id,
      name: resolved.product.name,
      sourceFingerprint: resolved.fingerprint,
      section: resolved.section,
      scenarios: resolved.scenarios,
    },
    graphics: resolved.scenarios.map((scenario) => ({
      scenarioId: scenario.id,
      axis: defaultAxis(),
      curve: { type: 'polyline', strokeWidth: 4.4, points: [] },
      thresholds: [],
      guides: [],
    })),
  };
}

function validationError(code, message, extra = {}) {
  return { code, message, ...extra };
}

function keyCheck(value, allowed, label, errors) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    errors.push(validationError('INVALID_OBJECT', `${label}必须是对象。`));
    return;
  }
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) errors.push(validationError('LOCKED_FIELD', `${label}不允许字段“${key}”。文字与资料库绑定字段不可编辑。`));
  }
}

function numberCheck(value, min, max, label, errors, { decimals = null } = {}) {
  if (!Number.isFinite(value) || value < min || value > max) {
    errors.push(validationError('GRAPH_VALUE_INVALID', `${label}必须在${min}至${max}之间。`));
    return;
  }
  if (decimals !== null && Math.abs(value - rounded(value, decimals)) > 1e-9) {
    errors.push(validationError('GRAPH_PRECISION_INVALID', `${label}最多保留${decimals}位小数。`));
  }
}

export async function validateConfig(config, { requireComplete = false, requireRecorded = false } = {}) {
  const errors = [];
  const warnings = [];
  if (!config || typeof config !== 'object' || Array.isArray(config)) return { errors: [validationError('CONFIG_INVALID', 'JSON根节点必须是对象。')], warnings: [] };
  keyCheck(config, ['$schema', 'schemaVersion', 'library', 'graphics'], '根对象', errors);
  if (config.$schema !== SCHEMA || config.schemaVersion !== 1) errors.push(validationError('SCHEMA_INVALID', `仅支持${SCHEMA}。`));
  keyCheck(config.library, ['id', 'name', 'sourceFingerprint', 'section', 'scenarios'], 'library', errors);
  if (!Array.isArray(config.graphics)) errors.push(validationError('GRAPHICS_INVALID', 'graphics必须是数组。'));
  if (!config.library?.id || !config.library?.name) return { errors, warnings };

  const resolved = await resolveProduct(config.library.id);
  if (resolved.issue) {
    errors.push(validationError('LIBRARY_PRODUCT_MISMATCH', resolved.issue.message, { action: 'update_optionlib' }));
    return { errors, warnings };
  }
  if (resolved.product.name !== config.library.name) {
    errors.push(validationError('LIBRARY_PRODUCT_MISMATCH', `产品名称已与optionlist.md不一致。请先修改optionlist.md或重新创建该产品图。`, { action: 'update_optionlib' }));
  }
  const expectedLibrary = {
    id: resolved.product.id,
    name: resolved.product.name,
    sourceFingerprint: resolved.fingerprint,
    section: resolved.section,
    scenarios: resolved.scenarios,
  };
  const providedLibrary = {
    id: config.library.id,
    name: config.library.name,
    sourceFingerprint: config.library.sourceFingerprint,
    section: config.library.section,
    scenarios: config.library.scenarios,
  };
  if (!equal(providedLibrary, expectedLibrary)) {
    errors.push(validationError(
      'LIBRARY_SCENARIO_MISMATCH',
      `当前图的情景与optionlib.md不一致。请修改${resolved.section}中的“情景”列后重新导入情景。`,
      { action: 'update_optionlib', section: resolved.section },
    ));
  }
  if (requireRecorded && resolved.product.status !== '已录入') {
    errors.push(validationError(
      'FORMAL_PUBLISH_STATUS_INVALID',
      `产品“${resolved.product.name}”当前为“${resolved.product.status}”，只能保存草稿；补齐损益图和定价资源并将optionlist.md改为“已录入”后才能正式发布。`,
      { action: 'update_optionlist' },
    ));
  }
  if (!Array.isArray(config.graphics) || !Array.isArray(config.library.scenarios)) return { errors, warnings };
  const expectedIds = resolved.scenarios.map((item) => item.id);
  const actualIds = config.graphics.map((item) => item?.scenarioId);
  if (!equal(actualIds, expectedIds)) errors.push(validationError('GRAPHICS_SCENARIO_MISMATCH', '子图必须与损益结构表的情景逐项、一一对应，不能新增、删除或重排。', { action: 'update_optionlib' }));

  for (const [index, graphic] of config.graphics.entries()) {
    keyCheck(graphic, ['scenarioId', 'axis', 'curve', 'thresholds', 'guides'], `graphics[${index}]`, errors);
    keyCheck(graphic?.axis, ['left', 'bottom', 'zeroY'], `graphics[${index}].axis`, errors);
    numberCheck(graphic?.axis?.left, 0, 1, `graphics[${index}].axis.left`, errors, { decimals: POSITION_DECIMALS });
    numberCheck(graphic?.axis?.bottom, 0, 1, `graphics[${index}].axis.bottom`, errors, { decimals: POSITION_DECIMALS });
    if (graphic?.axis?.zeroY !== undefined) numberCheck(graphic.axis.zeroY, 0, 1, `graphics[${index}].axis.zeroY`, errors, { decimals: POSITION_DECIMALS });
    keyCheck(graphic?.curve, ['type', 'strokeWidth', 'points'], `graphics[${index}].curve`, errors);
    if (!['polyline', 'step'].includes(graphic?.curve?.type)) errors.push(validationError('CURVE_TYPE_INVALID', `graphics[${index}]仅支持polyline或step。`));
    numberCheck(graphic?.curve?.strokeWidth, 0.1, 20, `graphics[${index}].curve.strokeWidth`, errors, { decimals: LINE_WIDTH_DECIMALS });
    const points = graphic?.curve?.points;
    if (!Array.isArray(points)) {
      errors.push(validationError('POINTS_INVALID', `graphics[${index}].curve.points必须是数组。`));
    } else if (points.length === 0) {
      warnings.push(validationError('CURVE_PENDING', `情景${index + 1}尚未绘制收益曲线。`));
    } else {
      if (points.length < 2 || points.length > 12) errors.push(validationError('POINT_COUNT_INVALID', `情景${index + 1}的曲线节点数必须为2至12个。`));
      for (const [pointIndex, point] of points.entries()) {
        keyCheck(point, ['x', 'y'], `graphics[${index}].curve.points[${pointIndex}]`, errors);
        numberCheck(point?.x, 0, 1, `情景${index + 1}节点${pointIndex + 1}的x`, errors, { decimals: POSITION_DECIMALS });
        numberCheck(point?.y, 0, 1, `情景${index + 1}节点${pointIndex + 1}的y`, errors, { decimals: POSITION_DECIMALS });
        if (pointIndex > 0) {
          const previous = points[pointIndex - 1];
          const comparison = graphic.curve.type === 'step' ? point.x < previous.x : point.x <= previous.x;
          if (comparison) errors.push(validationError('POINT_ORDER_INVALID', `情景${index + 1}的曲线横坐标必须${graphic.curve.type === 'step' ? '非递减' : '递增'}。`));
        }
      }
    }
    if (!Array.isArray(graphic?.thresholds)) {
      errors.push(validationError('THRESHOLD_INVALID', `graphics[${index}].thresholds必须是数组。`));
    } else {
      if (graphic.thresholds.length > 4) errors.push(validationError('THRESHOLD_COUNT_INVALID', `情景${index + 1}最多4条关键线。`));
      const tokens = new Set();
      for (const [thresholdIndex, threshold] of graphic.thresholds.entries()) {
        keyCheck(threshold, ['token', 'x'], `graphics[${index}].thresholds[${thresholdIndex}]`, errors);
        if (!THRESHOLD_TOKENS.includes(threshold?.token)) errors.push(validationError('THRESHOLD_TOKEN_INVALID', `关键线只能使用标准标记：${THRESHOLD_TOKENS.join('、')}。`));
        if (tokens.has(threshold?.token)) errors.push(validationError('THRESHOLD_DUPLICATE', `情景${index + 1}不能重复使用${threshold?.token}。`));
        tokens.add(threshold?.token);
        numberCheck(threshold?.x, 0, 1, `情景${index + 1}关键线位置`, errors, { decimals: POSITION_DECIMALS });
      }
    }
    const guides = graphic?.guides ?? [];
    if (!Array.isArray(guides)) {
      errors.push(validationError('GUIDE_INVALID', `graphics[${index}].guides必须是数组。`));
    } else {
      if (guides.length > 6) errors.push(validationError('GUIDE_COUNT_INVALID', `情景${index + 1}最多6条辅助线。`));
      for (const [guideIndex, guide] of guides.entries()) {
        keyCheck(guide, ['direction', 'position', 'style'], `graphics[${index}].guides[${guideIndex}]`, errors);
        if (!GUIDE_DIRECTIONS.includes(guide?.direction)) errors.push(validationError('GUIDE_DIRECTION_INVALID', `辅助线只能为horizontal或vertical。`));
        if (!GUIDE_STYLES.includes(guide?.style)) errors.push(validationError('GUIDE_STYLE_INVALID', `辅助线只能为solid或dashed。`));
        numberCheck(guide?.position, 0, 1, `情景${index + 1}辅助线位置`, errors, { decimals: POSITION_DECIMALS });
      }
    }
  }
  if (requireComplete && warnings.some((item) => item.code === 'CURVE_PENDING')) errors.push(validationError('PUBLISH_CURVE_PENDING', '所有情景均需完成收益曲线后才能发布。'));
  return { errors, warnings, resolved };
}

function plotGeometry(cardX, cardY, axis) {
  const left = cardX + CARD.plot.left;
  const right = cardX + CARD.plot.right;
  const top = cardY + CARD.plot.top;
  const bottom = cardY + CARD.plot.bottom;
  const width = right - left;
  const height = bottom - top;
  return {
    left,
    right,
    top,
    bottom,
    width,
    height,
    yAxisX: left + axis.left * width,
    xAxisY: bottom - axis.bottom * height,
  };
}

function pointToSvg(point, geometry) {
  return { x: geometry.left + point.x * geometry.width, y: geometry.top + point.y * geometry.height };
}

function curvePath(points, type, geometry) {
  const converted = points.map((point) => pointToSvg(point, geometry));
  if (!converted.length) return '';
  let d = `M${converted[0].x.toFixed(2)} ${converted[0].y.toFixed(2)}`;
  for (let index = 1; index < converted.length; index += 1) {
    const point = converted[index];
    if (type === 'step') d += ` H${point.x.toFixed(2)} V${point.y.toFixed(2)}`;
    else d += ` L${point.x.toFixed(2)} ${point.y.toFixed(2)}`;
  }
  return d;
}

function deriveXAxis(scenarios) {
  return scenarios.some((item) => /观察|触及|敲入|敲出|路径/.test(`${item.title}${item.condition}`)) ? '观察路径' : '到期价格S_T';
}

function graphicCard(scenario, graphic, index, cardX, cardY, editing, xLabel) {
  const axis = graphic.axis;
  const geometry = plotGeometry(cardX, cardY, axis);
  const xAxisLabelY = Math.min(geometry.bottom + 24, geometry.xAxisY + 24);
  const xReferenceLabelY = Math.min(geometry.bottom + 23, geometry.xAxisY + 23);
  const xReferenceAnchor = axis.left >= 0.88 ? 'end' : 'start';
  const xReferenceX = geometry.yAxisX + (xReferenceAnchor === 'end' ? -10 : 10);
  const yLabelAnchor = axis.left >= 0.9 ? 'end' : 'start';
  const curve = graphic.curve.points.length ? `<path class="payoff-line" data-element="curve" stroke-width="${graphic.curve.strokeWidth}" d="${curvePath(graphic.curve.points, graphic.curve.type, geometry)}"/>` : `<text class="pending" data-element="pending" x="${((geometry.left + geometry.right) / 2).toFixed(2)}" y="${((geometry.top + geometry.bottom) / 2).toFixed(2)}" text-anchor="middle">待绘制</text>`;
  const thresholdLines = graphic.thresholds.map((threshold) => {
    const x = geometry.left + threshold.x * geometry.width;
    const thresholdIndex = graphic.thresholds.indexOf(threshold);
    return `<g data-element="threshold" data-index="${thresholdIndex}"><line class="threshold" x1="${x.toFixed(2)}" y1="${geometry.top}" x2="${x.toFixed(2)}" y2="${geometry.bottom}"/><text class="cn key-label" x="${x.toFixed(2)}" y="${(geometry.bottom + 24).toFixed(2)}" text-anchor="middle">${html(threshold.token)}</text>${editing ? `<circle class="threshold-handle" data-role="threshold" data-scenario="${index}" data-threshold="${thresholdIndex}" cx="${x.toFixed(2)}" cy="${geometry.bottom}" r="8"/>` : ''}</g>`;
  }).join('');
  const guideLines = (graphic.guides || []).map((guide, guideIndex) => {
    const isHorizontal = guide.direction === 'horizontal';
    const position = isHorizontal ? geometry.top + guide.position * geometry.height : geometry.left + guide.position * geometry.width;
    const line = isHorizontal
      ? `<line class="guide ${guide.style}" x1="${geometry.left}" y1="${position.toFixed(2)}" x2="${geometry.right}" y2="${position.toFixed(2)}"/>`
      : `<line class="guide ${guide.style}" x1="${position.toFixed(2)}" y1="${geometry.top}" x2="${position.toFixed(2)}" y2="${geometry.bottom}"/>`;
    const handle = editing
      ? `<circle class="guide-handle" data-role="guide" data-scenario="${index}" data-guide="${guideIndex}" data-direction="${guide.direction}" cx="${(isHorizontal ? geometry.right : position).toFixed(2)}" cy="${(isHorizontal ? position : geometry.top).toFixed(2)}" r="7"/>`
      : '';
    return `<g data-element="guide" data-index="${guideIndex}">${line}${handle}</g>`;
  }).join('');
  const pointHandles = editing ? graphic.curve.points.map((point, pointIndex) => {
    const converted = pointToSvg(point, geometry);
    return `<circle class="node-handle" data-role="node" data-scenario="${index}" data-point="${pointIndex}" cx="${converted.x.toFixed(2)}" cy="${converted.y.toFixed(2)}" r="8"/>`;
  }).join('') : '';
  const editHandles = editing ? `<rect class="edit-boundary" x="${geometry.left}" y="${geometry.top}" width="${geometry.width}" height="${geometry.height}"/><circle class="axis-y-handle" data-role="axis-y" data-scenario="${index}" cx="${geometry.yAxisX.toFixed(2)}" cy="${geometry.top + 13}" r="8"/><circle class="axis-x-handle" data-role="axis-x" data-scenario="${index}" cx="${geometry.right - 13}" cy="${geometry.xAxisY.toFixed(2)}" r="8"/>` : '';
  const titleWidth = 592;
  const titleUnits = [...text(scenario.title)].reduce((total, character) => total + (character.charCodeAt(0) > 255 ? 1 : 0.56), 0);
  const titleFit = titleUnits * 17 > titleWidth ? ` textLength="${titleWidth}" lengthAdjust="spacingAndGlyphs"` : '';
  return `<g data-scenario-card="${index}" data-card-x="${cardX}" data-card-y="${cardY}">
    <rect class="card" x="${cardX}" y="${cardY}" width="${CARD.width}" height="${CARD.height}"/><line class="card-accent" x1="${cardX}" y1="${cardY + 2}" x2="${cardX + CARD.width}" y2="${cardY + 2}"/>
    <text class="cn scenario-no" x="${cardX + 24}" y="${cardY + 39}">情景${index + 1}</text><text class="cn scenario-name" x="${cardX + 85}" y="${cardY + 39}"${titleFit}>${html(scenario.title)}</text>
    <line class="axis" data-element="x-axis" x1="${geometry.left}" y1="${geometry.xAxisY.toFixed(2)}" x2="${geometry.right}" y2="${geometry.xAxisY.toFixed(2)}"/><line class="axis" data-element="y-axis" x1="${geometry.yAxisX.toFixed(2)}" y1="${geometry.top}" x2="${geometry.yAxisX.toFixed(2)}" y2="${geometry.bottom}"/>
    <path class="axis-arrow" data-element="axis-y-arrow" d="M${geometry.yAxisX.toFixed(2)} ${(geometry.top - 7).toFixed(2)} L${(geometry.yAxisX - 4.5).toFixed(2)} ${(geometry.top + 2).toFixed(2)} L${(geometry.yAxisX + 4.5).toFixed(2)} ${(geometry.top + 2).toFixed(2)} Z"/><path class="axis-arrow" data-element="axis-x-arrow" d="M${(geometry.right + 7).toFixed(2)} ${geometry.xAxisY.toFixed(2)} L${(geometry.right - 2).toFixed(2)} ${(geometry.xAxisY - 4.5).toFixed(2)} L${(geometry.right - 2).toFixed(2)} ${(geometry.xAxisY + 4.5).toFixed(2)} Z"/>
    ${guideLines}${thresholdLines}${curve}${pointHandles}${editHandles}
    <text class="cn axis-title" data-element="y-label" x="${geometry.yAxisX.toFixed(2)}" y="${geometry.top - 12}" text-anchor="${yLabelAnchor}">净收益</text><text class="cn reference-label" data-element="x-reference" x="${xReferenceX.toFixed(2)}" y="${xReferenceLabelY.toFixed(2)}" text-anchor="${xReferenceAnchor}">100%</text><text class="cn axis-title" data-element="x-label" x="${geometry.right}" y="${xAxisLabelY.toFixed(2)}" text-anchor="end">${xLabel}</text>
  </g>`;
}

export function renderPayoffSvg(config, { editing = false } = {}) {
  const count = config.library.scenarios.length;
  const rows = Math.ceil(count / 2);
  const height = Math.max(636, 156 + CARD.pitch * rows);
  const xLabel = deriveXAxis(config.library.scenarios);
  const cards = config.library.scenarios.map((scenario, index) => {
    const column = index % 2;
    const row = Math.floor(index / 2);
    return graphicCard(scenario, config.graphics[index], index, 16 + column * 740, 92 + row * CARD.pitch, editing, xLabel);
  }).join('');
  const metadata = JSON.stringify(config).replaceAll(']]>', ']]]]><![CDATA[>');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1500" height="${height}" viewBox="0 0 1500 ${height}" role="img" aria-labelledby="title desc">
  <title id="title">${html(config.library.name)}情景Payoff图</title><desc id="desc">由OptionHelper资料库绑定的情景Payoff图。图中文字由optionlist.md和optionlib.md生成。</desc>
  <metadata id="optionhelper-payoff-config" data-schema="${SCHEMA}"><![CDATA[${metadata}]]></metadata>
  <defs><linearGradient id="everbright-red-gold" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#C8102E"/><stop offset="1" stop-color="#D89B27"/></linearGradient><style>
  .cn{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif}.card{fill:#fff;stroke:#CFCFCF;stroke-width:1.1}.card-accent{stroke:#C8102E;stroke-width:4.5}.scenario-no{font-size:15px;font-weight:700;fill:#C8102E}.scenario-name{font-size:17px;font-weight:700;fill:#252525}.axis-title{font-size:12px;font-weight:600;fill:#252525}.reference-label{font-size:12px;fill:#4F4F4F}.key-label{font-size:12px;fill:#B16D00}.axis{stroke:#252525;stroke-width:1.9}.axis-arrow{fill:#252525}.threshold{stroke:#D89B27;stroke-width:2.4;stroke-dasharray:8 5}.guide{stroke:#a58e5f;stroke-width:1.25;opacity:.88}.guide.dashed{stroke-dasharray:5 5}.payoff-line{fill:none;stroke:#C8102E;stroke-linecap:round;stroke-linejoin:round}.pending{font-family:"PingFang SC","Microsoft YaHei",sans-serif;font-size:18px;font-weight:700;fill:#CFCFCF;letter-spacing:3px}.legend{font-size:12px;fill:#777}.node-handle{fill:#fff;stroke:#C8102E;stroke-width:3;cursor:grab}.threshold-handle{fill:#fff;stroke:#D89B27;stroke-width:3;cursor:ew-resize}.guide-handle{fill:#fff;stroke:#a58e5f;stroke-width:3;cursor:move}.axis-y-handle{fill:#fff;stroke:#252525;stroke-width:2.4;cursor:ew-resize}.axis-x-handle{fill:#fff;stroke:#252525;stroke-width:2.4;cursor:ns-resize}.edit-boundary{fill:none;stroke:#D89B27;stroke-width:1;stroke-dasharray:3 5;opacity:.65;pointer-events:none}
  </style></defs>
  <rect width="1500" height="${height}" fill="#fff"/><rect x="16" y="16" width="1468" height="64" fill="url(#everbright-red-gold)"/><rect x="16" y="16" width="9" height="64" fill="#A80F28"/><text class="cn" x="40" y="56" fill="#fff" font-size="30" font-weight="700">${html(config.library.name)}</text>
  ${cards}
  <line x1="16" y1="${height - 50}" x2="1484" y2="${height - 50}" stroke="#C8102E" stroke-width="2.4"/><g class="cn"><line x1="16" y1="${height - 20}" x2="58" y2="${height - 20}" stroke="#C8102E" stroke-width="4.4"/><text class="legend" x="71" y="${height - 15}">Payoff</text><line x1="170" y1="${height - 20}" x2="212" y2="${height - 20}" stroke="#D89B27" stroke-width="2.4" stroke-dasharray="8 5"/><text class="legend" x="225" y="${height - 15}">关键价格/障碍</text></g>
</svg>`;
}

async function exists(file) {
  try { await fs.access(file); return true; } catch { return false; }
}

export async function verifyWorkspace() {
  const required = [PATHS.optionList, PATHS.optionLib, PATHS.payoff];
  const checks = await Promise.all(required.map(async (file) => ({ file, exists: await exists(file) })));
  const missing = checks.filter((item) => !item.exists).map((item) => path.relative(ROOT, item.file));
  if (missing.length) {
    throw new Error(`项目结构不完整，缺少：${missing.join('、')}。请保留完整项目中的references/、assets/payoff/和assets/payoff-editor/。`);
  }
}

export async function ensureStorage() {
  await Promise.all([PATHS.source, PATHS.history].map((directory) => fs.mkdir(directory, { recursive: true })));
}

export function storagePaths(config) {
  const base = productFileBase(config.library.id, config.library.name);
  return {
    base,
    source: path.join(PATHS.source, `${base}.payoff.json`),
    formalSvg: path.join(PATHS.payoff, `${base}_payoff.svg`),
  };
}

export async function loadStoredConfig(idOrName) {
  const resolved = await resolveProduct(idOrName);
  if (resolved.issue) return { issue: resolved.issue };
  const base = productFileBase(resolved.product.id, resolved.product.name);
  const source = path.join(PATHS.source, `${base}.payoff.json`);
  if (!await exists(source)) return { product: resolved.product, config: null, source: null };
  try {
    return { product: resolved.product, config: JSON.parse(await fs.readFile(source, 'utf8')), source: 'draft' };
  } catch {
    return { product: resolved.product, issue: { code: 'CONFIG_READ_ERROR', message: `无法读取${path.basename(source)}。请检查JSON格式。` } };
  }
}

export async function saveDraft(config) {
  const validation = await validateConfig(config);
  if (validation.errors.length) return { ...validation, saved: false };
  await ensureStorage();
  const locations = storagePaths(config);
  await fs.writeFile(locations.source, `${JSON.stringify(config, null, 2)}\n`, 'utf8');
  return { ...validation, saved: true, locations };
}

// 从模板创建只生成内存配置；必须由用户点击“保存草稿”才写入state/。
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
  return { config, source: 'reimported', ...saved };
}

export async function publish(config) {
  const validation = await validateConfig(config, { requireComplete: true, requireRecorded: true });
  if (validation.errors.length) return { ...validation, published: false };
  await ensureStorage();
  const locations = storagePaths(config);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  if (await exists(locations.formalSvg)) await fs.rename(locations.formalSvg, path.join(PATHS.history, `${locations.base}_payoff.${stamp}.svg`));
  const svg = renderPayoffSvg(config);
  await Promise.all([
    fs.writeFile(locations.source, `${JSON.stringify(config, null, 2)}\n`, 'utf8'),
    fs.writeFile(locations.formalSvg, svg, 'utf8'),
  ]);
  return { ...validation, published: true, locations };
}

export async function inspectNativeSvg(svg) {
  const match = /<metadata\b[^>]*id=["']optionhelper-payoff-config["'][^>]*>\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*<\/metadata>/.exec(svg);
  if (!match) return { editable: false, reason: '该文件不是本工具生成的原生SVG，只能只读预览。' };
  let config;
  try { config = JSON.parse(match[1].replaceAll(']]]]><![CDATA[>', ']]>')); } catch { return { editable: false, reason: 'SVG内嵌配置无法解析，只能只读预览。' }; }
  const validation = await validateConfig(config);
  if (validation.errors.length) return { editable: false, reason: validation.errors[0].message, validation };
  const locations = storagePaths(config);
  if (!await exists(locations.source)) return { editable: false, reason: '未找到匹配的本地草稿JSON，只能只读预览。' };
  const local = JSON.parse(await fs.readFile(locations.source, 'utf8'));
  if (!equal(local, config)) return { editable: false, reason: 'SVG与本地草稿JSON不一致，只能只读预览。' };
  return { editable: true, config, validation };
}

export async function cliRender(config, output) {
  const validation = await validateConfig(config);
  if (validation.errors.length) return validation;
  const formalDir = `${PATHS.payoff}${path.sep}`;
  if (path.resolve(output).startsWith(formalDir)) return { errors: [validationError('FORMAL_WRITE_FORBIDDEN', '命令行不能写入正式SVG目录。请使用网页确认发布。')], warnings: [] };
  await fs.mkdir(path.dirname(output), { recursive: true });
  await fs.writeFile(output, renderPayoffSvg(config), 'utf8');
  return validation;
}
