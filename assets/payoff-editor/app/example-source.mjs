import { round } from './public/graphic-model.mjs';

const text = (value) => String(value ?? '').trim();
const escapeRegExp = (value) => text(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

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
  return { headers, rows, start, end: cursor };
}

function normalized(value) {
  return text(value)
    .replaceAll('−', '-')
    .replaceAll('≤', '<=')
    .replaceAll('≥', '>=')
    .replaceAll('\\le', '<=')
    .replaceAll('\\ge', '>=')
    .replaceAll('\\cdot', '*')
    .replaceAll('\\times', '*')
    .replaceAll('\\%', '%')
    .replaceAll('$', '')
    .replace(/\\text\{([^}]*)\}/g, '$1')
    .replace(/[{}]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

const NUMBER_PATTERN = /([+-]?)\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)/g;

function readNumber(match) {
  const sign = match[1] === '-' ? -1 : 1;
  const number = sign * Number(String(match[2]).replaceAll(',', ''));
  return Number.isFinite(number) ? round(number) : null;
}

function numbers(value) {
  const source = normalized(value);
  return [...source.matchAll(NUMBER_PATTERN)].map(readNumber).filter(Number.isFinite);
}

function finalNumber(value) {
  const source = normalized(value);
  const tail = source.includes('=') ? source.slice(source.lastIndexOf('=') + 1) : source;
  const values = numbers(tail);
  return values.length ? values.at(-1) : null;
}

const PARAMETER_PATTERNS = Object.freeze([
  ['K₁', /K_?(?:\{?1\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['K₂', /K_?(?:\{?2\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['K₃', /K_?(?:\{?3\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['K₄', /K_?(?:\{?4\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['K_p', /K_?(?:\{?p\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['K_c', /K_?(?:\{?c\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['K_u', /K_?(?:\{?u\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['K_d', /K_?(?:\{?d\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['K', /(?:^|[^A-Za-z0-9_])K(?![_A-Za-z0-9])\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['H_u', /H_?(?:\{?u\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['H_d', /H_?(?:\{?d\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['H_c', /H_?(?:\{?c\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['H_out', /H_?(?:\{?out\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['H_in', /H_?(?:\{?in\}?)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/gi],
  ['H', /(?:^|[^A-Za-z0-9_])H(?![_A-Za-z0-9])\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
  ['H_in', /(?:^|[^A-Za-z0-9_])B(?![_A-Za-z0-9])\s*=\s*([+-]?\d+(?:\.\d+)?)\s*%?/g],
]);

const NAMED_BARRIER_PATTERNS = Object.freeze([
  ['H_{out,t}', /(?:月度|每月|观察日)?(?:止盈线)[^。\n]*?(?:从|为|在)\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*(?:\*|·)?\s*S_0/gi],
  ['H_out', /(?:敲出线|敲出价|敲出障碍)[^。\n]*?(?:从|为|在)\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*(?:\*|·)?\s*S_0/gi],
  ['H_in', /(?:敲入线|敲入价|敲入障碍)[^。\n]*?(?:从|为|在)\s*([+-]?\d+(?:\.\d+)?)\s*%?\s*(?:\*|·)?\s*S_0/gi],
]);

function classifyGenericBarrier(parameters, product, productText) {
  if (!parameters.has('H')) return parameters;
  const context = normalized(`${product.name} ${productText}`);
  const value = parameters.get('H');
  if (product.name.includes('敲入')) {
    if (!parameters.has('H_in')) parameters.set('H_in', value);
    parameters.delete('H');
  } else if (product.name.includes('敲出') || context.includes('累购') || context.includes('鲨鱼鳍') || context.includes('敲出')) {
    if (!parameters.has('H_out')) parameters.set('H_out', value);
    parameters.delete('H');
  }
  return parameters;
}

function extractParameters(source, product) {
  const parameters = new Map();
  const plain = normalized(source).replace(/K₁/g, 'K_1').replace(/K₂/g, 'K_2').replace(/K₃/g, 'K_3').replace(/K₄/g, 'K_4');
  for (const [token, pattern] of PARAMETER_PATTERNS) {
    pattern.lastIndex = 0;
    const match = pattern.exec(plain);
    if (match && Number.isFinite(Number(match[1])) && !parameters.has(token)) parameters.set(token, round(Number(match[1])));
  }
  for (const [token, pattern] of NAMED_BARRIER_PATTERNS) {
    pattern.lastIndex = 0;
    const match = pattern.exec(plain);
    if (match && Number.isFinite(Number(match[1])) && !parameters.has(token)) parameters.set(token, round(Number(match[1])));
  }
  return classifyGenericBarrier(parameters, product, source);
}

export function axisForProduct(product, productText) {
  if (product.id === '10.4' || normalized(productText).includes('方差互换')) return { kind: 'realized_volatility', label: '已实现波动率σ', unit: '', referenceValue: 13 };
  if (product.id === '10.8' || normalized(productText).includes('Range Accrual')) return { kind: 'observed_days', label: '区间内观察日数P', unit: '天', referenceValue: 11 };
  if (product.id === '9.24' || product.id === '9.25') return { kind: 'coupon_count', label: '满足计息条件次数', unit: '次', referenceValue: 6 };
  return { kind: 'underlying_price', label: '标的价格(%)', unit: '%', referenceValue: 100 };
}

export function parseExampleSource(productText, product) {
  const heading = new RegExp(`^####\\s+${escapeRegExp(product.id)}\\.3\\s+示例\\s*$`, 'm').exec(productText);
  const section = `#### ${product.id}.3 示例`;
  if (!heading) return { issue: { code: 'EXAMPLE_SECTION_MISSING', message: `未找到“${section}”。默认Payoff图需要该示例表。`, section } };
  const start = heading.index + heading[0].length;
  const next = productText.slice(start).search(/^####\s+/m);
  const body = productText.slice(start, next < 0 ? undefined : start + next);
  const lines = body.split(/\r?\n/);
  let table = null;
  for (let index = 0; index < lines.length; index += 1) {
    table = parseTable(lines, index);
    if (table) break;
  }
  if (!table) return { issue: { code: 'EXAMPLE_TABLE_MISSING', message: `“${section}”中没有有效示例表。`, section } };
  const payoffColumn = table.headers.find((header) => header.includes('持有方净损益'));
  if (table.headers[0] !== '情景' || !payoffColumn) return { issue: { code: 'EXAMPLE_TABLE_HEADERS_INVALID', message: `“${section}”示例表必须包含“情景”和“持有方净损益”列。`, section } };
  const axis = axisForProduct(product, productText);
  const parameters = extractParameters(productText, product);
  const rows = table.rows.map((row) => ({ value: finalNumber(row[payoffColumn]) }));
  if (!rows.length || rows.some((row) => !Number.isFinite(row.value))) return { issue: { code: 'EXAMPLE_VALUE_INVALID', message: `“${section}”存在无法读取的持有方净损益数值。`, section } };
  return { section, raw: body, rows, parameters, axis };
}
