import assert from 'node:assert/strict';
import test from 'node:test';
import { createConfig, createTemplateDraft, inspectNativeSvg, listProducts, publish, renderPayoffSvg, THRESHOLD_TOKENS, validateConfig } from '../app/core.mjs';

async function sampleProduct() {
  const product = (await listProducts()).find((item) => item.available);
  assert(product, '资料库至少应有一个可用产品。');
  return product;
}

test('资料库仅将表头以情景、判断条件开头且具备损益结构表的产品标记为可用', async () => {
  const products = await listProducts();
  const available = products.filter((product) => product.available);
  assert(available.length > 0);
  assert.equal(available.length, products.length);
  for (const product of available) {
    assert(product.scenarioCount > 0);
    assert.equal(product.scenarios.length, product.scenarioCount);
    assert(product.scenarios.every((scenario) => scenario.title && scenario.condition));
  }
  for (const product of products.filter((item) => !item.available)) assert.match(product.issue.message, /optionlib\.md|损益结构|情景/);
});

test('从模板新建只返回待保存配置，不隐式写入草稿', async () => {
  const product = await sampleProduct();
  const draft = await createTemplateDraft(product.id);
  assert.equal(draft.source, 'new');
  assert.equal(draft.saved, false);
  assert.equal(draft.errors.length, 0);
  assert.equal(draft.config.library.id, product.id);
  assert.equal(draft.config.graphics.length, product.scenarioCount);
});

test('新配置将损益结构表的情景逐项绑定为子图', async () => {
  const product = await sampleProduct();
  const config = await createConfig(product.id);
  assert(config.library.scenarios.length > 0);
  assert.deepEqual(config.graphics.map((item) => item.scenarioId), config.library.scenarios.map((item) => item.id));
  assert(config.graphics.every((item) => Array.isArray(item.guides) && item.guides.length === 0));
  assert(config.graphics.every((item) => item.axis.left === 0.5 && item.axis.bottom === 0.5 && !('zeroY' in item.axis)));
  const validation = await validateConfig(config);
  assert.equal(validation.errors.length, 0);
  assert.equal(validation.warnings.filter((item) => item.code === 'CURVE_PENDING').length, config.library.scenarios.length);
  const publishValidation = await validateConfig(config, { requireComplete: true });
  assert(publishValidation.errors.some((item) => item.code === 'PUBLISH_CURVE_PENDING'));
});

test('禁止改写资料库情景文字或改变子图数量', async () => {
  const product = await sampleProduct();
  const config = await createConfig(product.id);
  config.library.scenarios[0].title = '人工改写';
  config.graphics.pop();
  const validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'LIBRARY_SCENARIO_MISMATCH'));
  assert(validation.errors.some((item) => item.code === 'GRAPHICS_SCENARIO_MISMATCH'));
});

test('SVG输出保留固定画布、内嵌JSON、单行情景标题及大绘图区', async () => {
  const product = await sampleProduct();
  const config = await createConfig(product.id);
  config.graphics[0].curve = { type: 'polyline', strokeWidth: 3.2, points: [{ x: 0.05, y: 0.75 }, { x: 0.95, y: 0.25 }] };
  const svg = renderPayoffSvg(config);
  const expectedHeight = Math.max(636, 156 + 480 * Math.ceil(config.library.scenarios.length / 2));
  assert.match(svg, /width="1500"/);
  assert.match(svg, new RegExp(`height="${expectedHeight}"`));
  assert.match(svg, /id="optionhelper-payoff-config"/);
  assert.match(svg, /stroke-width="3.2"/);
  assert.match(svg, /<rect class="card" x="16" y="92" width="728" height="468"/);
  assert(svg.includes(`情景1</text><text class="cn scenario-name" x="101" y="131">${config.library.scenarios[0].title}`));
  assert.match(svg, /data-element="x-axis" x1="58" y1="340\.00" x2="700" y2="340\.00"/);
  assert.match(svg, /data-element="y-axis" x1="379\.00" y1="162" x2="379\.00" y2="518"/);
  assert.match(svg, /data-element="x-reference" x="389\.00" y="363\.00" text-anchor="start">100%/);
  assert.match(svg, /data-element="axis-y-arrow" d="M379\.00 155\.00 L374\.50 164\.00 L383\.50 164\.00 Z"/);
  assert.match(svg, /data-element="axis-x-arrow" d="M707\.00 340\.00 L698\.00 335\.50 L698\.00 344\.50 Z"/);
  assert.doesNotMatch(svg, /zero-line|zero-label|y-reference|零收益|class="zero/);
  const editableSvg = renderPayoffSvg(config, { editing: true });
  assert.match(editableSvg, /<rect class="edit-boundary" x="58" y="162" width="642" height="356"/);
  config.graphics[0].guides.push({ direction: 'horizontal', position: 0.4, style: 'dashed' });
  const guideValidation = await validateConfig(config);
  assert.equal(guideValidation.errors.length, 0);
  const svgWithGuide = renderPayoffSvg(config);
  assert.match(svgWithGuide, /class="guide dashed"/);
});

test('图形参数支持完整位置范围与规定小数精度', async () => {
  const product = await sampleProduct();
  const config = await createConfig(product.id);
  config.graphics[0] = {
    ...config.graphics[0],
    axis: { left: 0, bottom: 1 },
    curve: { type: 'polyline', strokeWidth: 0.1, points: [{ x: 0, y: 1 }, { x: 1, y: 0 }] },
    thresholds: [{ token: 'K', x: 1 }],
    guides: [{ direction: 'vertical', position: 0, style: 'solid' }],
  };
  let validation = await validateConfig(config);
  assert.equal(validation.errors.length, 0);

  config.graphics[0].axis.left = 0.12345;
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_PRECISION_INVALID'));

  config.graphics[0].axis.left = 0.1234;
  config.graphics[0].curve.strokeWidth = 0.123;
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_PRECISION_INVALID'));

  config.graphics[0].curve.strokeWidth = 20.01;
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_VALUE_INVALID'));
});

test('关键线标签覆盖资料库中的异名行权价和障碍价', async () => {
  const config = await createConfig('10.7');
  const extraTokens = ['K_p', 'K_c', 'K_u', 'K_d', 'H', 'H_u', 'H_d', 'H_{out,t}'];
  assert(extraTokens.every((token) => THRESHOLD_TOKENS.includes(token)));
  config.graphics.forEach((graphic, index) => {
    graphic.thresholds = extraTokens.slice(index * 4, index * 4 + 4).map((token, tokenIndex) => ({ token, x: 0.15 + tokenIndex * 0.2 }));
  });
  const validation = await validateConfig(config);
  assert.equal(validation.errors.length, 0);
});

test('待录入产品可以保存草稿，但不能正式发布', async () => {
  const config = await createConfig('2.1');
  config.graphics.forEach((graphic) => {
    graphic.curve = { type: 'polyline', strokeWidth: 3, points: [{ x: 0.1, y: 0.8 }, { x: 0.9, y: 0.2 }] };
  });
  const draftValidation = await validateConfig(config, { requireComplete: true });
  assert.equal(draftValidation.errors.length, 0);
  const publicationValidation = await validateConfig(config, { requireComplete: true, requireRecorded: true });
  assert(publicationValidation.errors.some((item) => item.code === 'FORMAL_PUBLISH_STATUS_INVALID'));
  const result = await publish(config);
  assert.equal(result.published, false);
  assert(result.errors.some((item) => item.code === 'FORMAL_PUBLISH_STATUS_INVALID'));
});

test('路径结构的未敲出情景显式排除提前终止路径', async () => {
  const products = await listProducts();
  const byId = new Map(products.map((product) => [product.id, product]));
  const checks = [
    ['9.3', [2, 3], /全部敲出观察日/],
    ['9.7', [2], /全部敲出观察日/],
    ['9.10', [2], /全部非最后敲出观察日/],
    ['9.19', [2, 3], /未触发敲出/],
    ['9.20', [1, 2, 3], /未触发敲出/],
    ['9.22', [1, 2, 3], /全部敲出观察日/],
    ['9.23', [1, 2, 3], /全部敲出观察日/],
    ['9.26', [2, 3], /全部观察日未达/],
  ];
  for (const [id, indices, pattern] of checks) {
    const product = byId.get(id);
    assert(product?.available, `${id}应可被资料库解析。`);
    for (const index of indices) assert.match(product.scenarios[index].condition, pattern, `${id}情景${index + 1}缺少未敲出前提。`);
  }
});

test('无来源绑定的外部SVG只能只读预览', async () => {
  const inspected = await inspectNativeSvg('<svg xmlns="http://www.w3.org/2000/svg"><text>external</text></svg>');
  assert.equal(inspected.editable, false);
  assert.match(inspected.reason, /只读/);
});

test('内嵌JSON含CDATA结束符时仍可解析为受校验的原生SVG', async () => {
  const product = await sampleProduct();
  const config = await createConfig(product.id);
  config.library.scenarios[0].title = '测试]]>标题';
  const inspected = await inspectNativeSvg(renderPayoffSvg(config));
  assert.equal(inspected.editable, false);
  assert.doesNotMatch(inspected.reason, /无法解析/);
});
