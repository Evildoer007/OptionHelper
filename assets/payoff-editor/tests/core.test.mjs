import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  LEGACY_SCHEMA,
  SCHEMA,
  THRESHOLD_TOKENS,
  buildPublishReview,
  cliRender,
  createConfig,
  inspectNativeSvg,
  listProducts,
  loadStoredConfig,
  migrateConfig,
  productFileBase,
  publish,
  renderPayoffSvg,
  saveDraft,
  validateConfig,
} from '../app/core.mjs';
import {
  DEFAULT_AXIS,
  DEFAULT_SCALE,
  COUPON_COUNT_AXIS,
  OBSERVED_DAYS_AXIS,
  VOLATILITY_AXIS,
  autoFitScale,
  MAX_GUIDES,
  MAX_THRESHOLDS,
  addGuide,
  addThreshold,
  axisRatios,
  blankCurvePoints,
  boundedPointX,
  createBlankGraphic,
  guideExtent,
  materializeGuideExtent,
  guideMeta,
  ratioToX,
  ratioToY,
  resetAllGraphics,
  resetGraphic,
  scaleIssues,
  thresholdMeta,
  xToRatio,
  yToRatio,
} from '../app/public/graphic-model.mjs';
import { formulaCoverage } from '../app/formula-payoff.mjs';

async function sampleProduct() {
  const product = (await listProducts()).find((item) => item.available);
  assert(product, '资料库至少应有一个可用产品。');
  return product;
}

function completeGraphic(graphic) {
  // 单元测试中的手工图统一使用基准坐标系，避免资料库示例的自适应范围干扰边界断言。
  graphic.scale = structuredClone(DEFAULT_SCALE);
  graphic.curve = {
    type: 'polyline',
    strokeWidth: 3.2,
    points: [{ x: graphic.scale.xMin, y: 0 }, { x: 100, y: 20 }, { x: graphic.scale.xMax, y: -10 }],
  };
  return graphic;
}

async function completeConfig(id = null) {
  const config = await createConfig(id || (await sampleProduct()).id);
  config.graphics.forEach(completeGraphic);
  return config;
}

function legacyFrom(config) {
  return {
    ...structuredClone(config),
    $schema: LEGACY_SCHEMA,
    schemaVersion: 1,
    graphics: config.graphics.map((graphic) => ({
      scenarioId: graphic.scenarioId,
      axis: { left: 0.5, bottom: 0.5 },
      curve: { type: graphic.curve.type, strokeWidth: graphic.curve.strokeWidth, points: [{ x: 0.35, y: 0.45 }, { x: 0.6, y: 0.4 }] },
      thresholds: [{ token: 'K', x: 0.6 }],
      guides: [{ direction: 'vertical', position: 0.35, style: 'dashed' }, { direction: 'horizontal', position: 0.4, style: 'solid' }],
    })),
  };
}

async function temporaryPaths() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'optionhelper-payoff-v2-'));
  return {
    root,
    paths: {
      payoff: path.join(root, 'assets', 'payoff'),
      source: path.join(root, 'assets', 'payoff-editor', 'state'),
      history: path.join(root, 'assets', 'payoff-editor', 'state', 'history'),
    },
  };
}

test('资料库产品均能逐项读取损益结构情景及.3示例', async () => {
  const products = await listProducts();
  assert.equal(products.length, 65);
  assert(products.every((product) => product.available));
  for (const product of products) {
    assert(product.scenarioCount > 0);
    assert.equal(product.scenarios.length, product.scenarioCount);
    assert(product.scenarios.every((scenario) => scenario.title && scenario.condition));
    assert(product.exampleCount > 0, `${product.id}缺少.3示例数据。`);
  }
});

test('公式规则覆盖全部资料库产品，不能回退为示例行连线', async () => {
  const products = await listProducts();
  assert.deepEqual(new Set(formulaCoverage()), new Set(products.map((product) => product.id)));
});

test('新建配置以损益结构公式生成v2默认Payoff曲线', async () => {
  const config = await createConfig('2.1');
  assert.equal(config.$schema, SCHEMA);
  assert.equal(config.schemaVersion, 2);
  assert.deepEqual(config.graphics.map((item) => item.scenarioId), config.library.scenarios.map((item) => item.id));
  for (const graphic of config.graphics) {
    assert(graphic.curve.points.length >= 2);
    assert(graphic.scale.xMin < graphic.scale.xMax);
    assert(graphic.scale.yMin < graphic.scale.yMax);
    assert.deepEqual(graphic.guides, []);
  }
  assert.deepEqual(config.graphics[0].curve.points, [{ x: 80, y: -5 }, { x: 100, y: -5 }]);
  assert.deepEqual(config.graphics[1].curve.points, [{ x: 100, y: -5 }, { x: 105, y: 0 }, { x: 120, y: 15 }]);
  assert.deepEqual(config.graphics[0].thresholds, [{ token: 'K', value: 100 }]);
  const validation = await validateConfig(config);
  assert.equal(validation.errors.length, 0);
  assert.equal(validation.warnings.filter((item) => item.code === 'CURVE_PENDING').length, 0);
});

test('资料库专属关键价格主键完整保留并可迁移旧草稿', async () => {
  const gecko = await createConfig('9.15');
  assert(gecko.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_{hedge,1}' && item.value === 80)));
  assert(gecko.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_{hedge,2}' && item.value === 75)));
  assert(gecko.graphics.every((graphic) => graphic.thresholds.length === 5));

  const legacyGecko = structuredClone(gecko);
  legacyGecko.graphics.forEach((graphic) => {
    graphic.thresholds = graphic.thresholds
      .filter((item) => item.token !== 'H_{hedge,2}')
      .map((item) => item.token === 'H_{hedge,1}' ? { ...item, token: 'H_buffer' } : item);
  });
  const migrated = migrateConfig(legacyGecko);
  assert(migrated.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_{hedge,1}' && item.value === 80)));
  assert(migrated.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_{hedge,2}' && item.value === 75)));

  const buffer = await createConfig('9.27');
  const limited = await createConfig('9.28');
  const booster = await createConfig('9.29');
  assert(buffer.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'B')));
  assert(limited.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'L')));
  assert(booster.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'L')));
});

test('未完成收益曲线仍可发布正式SVG', async () => {
  const config = await createConfig((await sampleProduct()).id);
  config.graphics.forEach((graphic) => { graphic.curve.points = []; });
  const { root, paths } = await temporaryPaths();
  try {
    const published = await publish(config, { paths });
    assert.equal(published.published, true);
    assert.equal(published.errors.length, 0);
    assert(published.warnings.some((item) => item.code === 'CURVE_PENDING'));
    assert.equal((await fs.stat(published.locations.formalSvg)).isFile(), true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('全部资料库产品可由公式规则生成，路径信息不足时明确待绘制', async () => {
  const products = await listProducts();
  const pending = new Map();
  for (const product of products) {
    const config = await createConfig(product.id);
    const validation = await validateConfig(config);
    assert.equal(validation.errors.length, 0, `${product.id}：${validation.errors.map((item) => item.message).join('；')}`);
    assert.equal(validation.warnings.filter((item) => item.code === 'CURVE_PENDING').length, pending.get(product.id) || 0, `${product.id}待补充曲线数不正确。`);
    assert.deepEqual(config.library.axis, product.id === '10.4' ? VOLATILITY_AXIS : product.id === '10.8' ? OBSERVED_DAYS_AXIS : ['9.24', '9.25'].includes(product.id) ? COUPON_COUNT_AXIS : DEFAULT_AXIS, `${product.id}横轴配置错误。`);
  }
});

test('示例仅用于参数与金额校验，公式决定曲线边界与路径断线', async () => {
  const accumulator = await createConfig('8.1');
  assert.deepEqual(accumulator.graphics.map((graphic) => graphic.curve.points), [
    [{ x: 90, y: 0 }, { x: 110, y: 46000 }],
    [{ x: 90, y: 0 }, { x: 110, y: 122000 }],
    [
      { x: 85, y: -210000, endpoint: 'closed' }, { x: 85, y: -210000, breakBefore: true, endpoint: 'closed' },
      { x: 95, y: 121500, breakBefore: true, endpoint: 'closed' }, { x: 95, y: 121500, breakBefore: true, endpoint: 'closed' },
    ],
  ]);

  const knockout = await createConfig('5.1');
  const knockin = await createConfig('5.3');
  assert(knockout.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_out')));
  assert(knockin.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_in')));

  const variance = await createConfig('10.4');
  assert.deepEqual(variance.library.axis, VOLATILITY_AXIS);
  assert.deepEqual(variance.graphics[0].curve.points, [{ x: 13, y: 0 }, { x: 14, y: 6.75 }, { x: 15, y: 14 }, { x: 20, y: 57.75 }]);
  assert.deepEqual(variance.graphics[1].curve.points, [{ x: 13, y: 0, endpoint: 'closed' }, { x: 13, y: 0, breakBefore: true, endpoint: 'closed' }]);
  assert.deepEqual(variance.graphics[2].curve.points, [{ x: 9, y: -22 }, { x: 10, y: -17.25 }, { x: 11, y: -12 }, { x: 12, y: -6.25 }, { x: 13, y: 0 }]);

  const descendingSnowball = await createConfig('9.9');
  assert.deepEqual(descendingSnowball.graphics[0].curve.points, [{ x: 103, y: 126.5, endpoint: 'closed' }, { x: 103, y: 126.5, breakBefore: true, endpoint: 'closed' }]);
  assert(descendingSnowball.graphics.every((graphic) => graphic.thresholds.some((item) => item.token === 'H_{out,t}')));

  const rangeAccrual = await createConfig('10.8');
  assert.deepEqual(rangeAccrual.library.axis, OBSERVED_DAYS_AXIS);
  assert.deepEqual(rangeAccrual.graphics[1].curve.points, [{ x: 0, y: -1.21, endpoint: 'open' }, { x: 22, y: 0.24, endpoint: 'open' }]);
  assert.equal(rangeAccrual.graphics[1].scale.xMin, 0);
  assert.equal(rangeAccrual.graphics[1].scale.xMax, 22);

  const dcn = await createConfig('9.24');
  const floorDcn = await createConfig('9.25');
  assert.deepEqual(dcn.library.axis, COUPON_COUNT_AXIS);
  assert.deepEqual(dcn.graphics[1].curve.points, [{ x: 0, y: 0 }, { x: 3, y: 30 }, { x: 9, y: 90 }, { x: 12, y: 120 }]);
  assert.deepEqual(floorDcn.graphics[1].curve.points, [{ x: 0, y: 30 }, { x: 9, y: 88.5 }, { x: 12, y: 108 }]);
});

test('同一价格的不同路径支付以断开的离散点保留，不伪造竖直收益线', async () => {
  const config = await createConfig('9.1');
  const graphic = config.graphics[0];
  assert.equal(graphic.curve.type, 'polyline');
  assert(graphic.curve.points.every((point) => point.endpoint === 'closed'));
  assert(graphic.curve.points.slice(1).every((point) => point.breakBefore));
  assert.equal((await validateConfig(config)).errors.length, 0);
});

test('网页编辑与模型直写同一JSON生成字节完全一致的SVG', async () => {
  const manualConfig = await completeConfig('2.1');
  manualConfig.graphics[0].thresholds = [{ token: 'K', value: 100 }];
  manualConfig.graphics[0].guides = [{ direction: 'horizontal', value: 0, style: 'dashed' }];
  const modelConfig = JSON.parse(JSON.stringify(manualConfig));
  const { root, paths } = await temporaryPaths();
  try {
    const saved = await saveDraft(manualConfig, { paths });
    assert.equal(saved.saved, true);
    const loaded = await loadStoredConfig(manualConfig.library.id, { paths });
    assert.deepEqual(loaded.config, modelConfig);

    const manualSvg = renderPayoffSvg(loaded.config);
    const modelSvg = renderPayoffSvg(modelConfig);
    assert.equal(manualSvg, modelSvg);

    const published = await publish(modelConfig, { paths });
    assert.equal(published.published, true);
    assert.equal(await fs.readFile(published.locations.formalSvg, 'utf8'), manualSvg);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('资料库校验提示不阻断正式SVG发布', async () => {
  const config = await createConfig((await sampleProduct()).id);
  config.library.sourceFingerprint = 'stale-reference';
  const { root, paths } = await temporaryPaths();
  try {
    const published = await publish(config, { paths });
    assert.equal(published.published, true);
    assert(published.errors.some((item) => item.code === 'LIBRARY_SCENARIO_MISMATCH'));
    assert.equal((await fs.stat(published.locations.formalSvg)).isFile(), true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('发布拒绝非法图形，且失败时保留原正式SVG', async () => {
  const config = await completeConfig('2.1');
  const { root, paths } = await temporaryPaths();
  try {
    const first = await publish(config, { paths });
    assert.equal(first.published, true);
    const original = await fs.readFile(first.locations.formalSvg, 'utf8');

    const missingGraphics = structuredClone(config);
    missingGraphics.graphics = [];
    const missingResult = await publish(missingGraphics, { paths });
    assert.equal(missingResult.published, false);
    assert(missingResult.errors.some((item) => item.code === 'GRAPHICS_SCENARIO_MISMATCH'));
    assert.equal(await fs.readFile(first.locations.formalSvg, 'utf8'), original);

    const unsafeWidth = structuredClone(config);
    unsafeWidth.graphics[0].curve.strokeWidth = '4.4" data-audit="unsafe';
    const unsafeResult = await publish(unsafeWidth, { paths });
    assert.equal(unsafeResult.published, false);
    assert(unsafeResult.errors.some((item) => item.code === 'GRAPH_VALUE_INVALID'));
    assert.equal(await fs.readFile(first.locations.formalSvg, 'utf8'), original);
    assert.doesNotMatch(original, /data-audit/);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('共享坐标模型把价格、净损益、坐标轴和拖拽换算保持一致', () => {
  assert.deepEqual(axisRatios(DEFAULT_SCALE), { x: 0.5, y: 0.5 });
  assert.equal(xToRatio(70, DEFAULT_SCALE), 0.35);
  assert.equal(xToRatio(120, DEFAULT_SCALE), 0.6);
  assert.equal(yToRatio(10, DEFAULT_SCALE), 0.45);
  assert.equal(yToRatio(20, DEFAULT_SCALE), 0.4);
  assert.equal(yToRatio(50, DEFAULT_SCALE), 0.25);
  assert.equal(yToRatio(100, DEFAULT_SCALE), 0);
  assert.equal(ratioToX(0.6, DEFAULT_SCALE), 120);
  assert.equal(ratioToY(0.4, DEFAULT_SCALE), 20);
  assert.deepEqual(blankCurvePoints(DEFAULT_SCALE), [{ x: 0, y: 0 }, { x: 200, y: 0 }]);
  assert.equal(boundedPointX(200, [{ x: 0 }, { x: 120 }, { x: 200 }], 1, 'polyline', DEFAULT_SCALE), 199.99);
  assert.equal(boundedPointX(0, [{ x: 0 }, { x: 120 }], 1, 'polyline', DEFAULT_SCALE), 0.01);
});

test('范围、节点、关键线和辅助线的边界校验拒绝无效状态', async () => {
  const config = await completeConfig();
  const graphic = config.graphics[0];
  graphic.scale = { xMin: 100, xMax: 200, yMin: 0, yMax: 100 };
  assert.equal(scaleIssues(graphic.scale).length, 2);
  let validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'SCALE_INVALID'));

  graphic.scale = { xMin: 0, xMax: 200, yMin: -100, yMax: 100 };
  graphic.curve.points[1].x = 220;
  graphic.thresholds = [{ token: 'K', value: 220 }];
  graphic.guides = [{ direction: 'horizontal', value: 101, style: 'dashed' }];
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'POINT_RANGE_INVALID'));
  assert(validation.errors.some((item) => item.code === 'THRESHOLD_RANGE_INVALID'));
  assert(validation.errors.some((item) => item.code === 'GUIDE_RANGE_INVALID'));

  graphic.curve.points[1].x = 100.123;
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_PRECISION_INVALID'));

  graphic.curve.points[1].x = 100;
  graphic.guides = [{ direction: 'horizontal', value: 10, start: 80, end: 80, style: 'dashed' }];
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GUIDE_EXTENT_ORDER_INVALID'));
  graphic.guides = [{ direction: 'vertical', value: 120, start: -20, style: 'dashed' }];
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GUIDE_EXTENT_INVALID'));
  graphic.guides = [{ direction: 'vertical', value: 120, start: -100.001, end: 80, style: 'dashed' }];
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_PRECISION_INVALID'));
});

test('曲线节点增删改、曲线类型与线宽均受v2规则约束', async () => {
  const config = await completeConfig();
  const graphic = config.graphics[0];
  graphic.curve.points = blankCurvePoints(graphic.scale);
  graphic.curve.points.splice(1, 0, { x: 100, y: 10 });
  graphic.curve.points[1].y = 20;
  assert.equal(graphic.curve.points.length, 3);
  assert.equal((await validateConfig(config)).errors.length, 0);

  graphic.curve.points.splice(1, 1);
  assert.equal(graphic.curve.points.length, 2);
  assert.equal((await validateConfig(config)).errors.length, 0);

  graphic.curve.type = 'step';
  graphic.curve.points = [{ x: 50, y: 5 }, { x: 50, y: 20 }, { x: 150, y: -10 }];
  graphic.curve.strokeWidth = 20;
  assert.equal((await validateConfig(config)).errors.length, 0);

  graphic.curve.type = 'polyline';
  let validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'POINT_ORDER_INVALID'));
  graphic.curve.type = 'step';
  graphic.curve.strokeWidth = 20.01;
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'GRAPH_VALUE_INVALID'));
  graphic.curve.strokeWidth = 3;
  graphic.curve.points = Array.from({ length: 13 }, (_, index) => ({ x: index * 10, y: index }));
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'POINT_COUNT_INVALID'));
});

test('跳变曲线以断线和开闭端点准确表达，且规则可校验', async () => {
  const config = await completeConfig();
  const graphic = config.graphics[0];
  graphic.scale = { xMin: 60, xMax: 120, yMin: -60, yMax: 60 };
  graphic.curve = {
    type: 'polyline',
    strokeWidth: 4.4,
    points: [
      { x: 60, y: 10 },
      { x: 100, y: 50, endpoint: 'open' },
      { x: 100, y: -50, breakBefore: true, endpoint: 'closed' },
      { x: 120, y: -50 },
    ],
  };
  assert.equal((await validateConfig(config)).errors.length, 0);
  const svg = renderPayoffSvg(config);
  assert.match(svg, /M58\.00 310\.33 L486\.00 191\.67 M486\.00 488\.33 L700\.00 488\.33/);
  assert.match(svg, /class="curve-endpoint open" data-element="curve-endpoint" data-point="1"/);
  assert.match(svg, /class="curve-endpoint closed" data-element="curve-endpoint" data-point="2"/);

  delete graphic.curve.points[2].breakBefore;
  let validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'POINT_ORDER_INVALID'));
  graphic.curve.points[2].breakBefore = true;
  graphic.curve.points[2].endpoint = 'filled';
  validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'POINT_ENDPOINT_INVALID'));
});

test('自动取景以100%和0为中心并覆盖全部图形元素', () => {
  const graphic = createBlankGraphic('test.2-03');
  graphic.scale = { xMin: 70, xMax: 130, yMin: -20, yMax: 20 };
  graphic.curve.points = [{ x: 70, y: -12 }, { x: 130, y: 24 }];
  graphic.thresholds = [{ token: 'H_out', value: 135 }];
  graphic.guides = [{ direction: 'horizontal', value: 30, start: 80, end: 120, style: 'dashed' }];
  const scale = autoFitScale(graphic);
  assert.equal((scale.xMin + scale.xMax) / 2, 100);
  assert.equal((scale.yMin + scale.yMax) / 2, 0);
  assert(scale.xMin <= 70 && scale.xMax >= 135);
  assert(scale.yMin <= -12 && scale.yMax >= 30);
  assert.deepEqual(autoFitScale(createBlankGraphic('test.2-04')), DEFAULT_SCALE);
});

test('数量上限、重复关键线和无效输入都不改变共享图形状态', () => {
  const graphic = createBlankGraphic('test.2-01');
  for (let index = 0; index < MAX_GUIDES; index += 1) assert.equal(addGuide(graphic, { direction: 'vertical', value: index * 10, style: 'dashed' }).changed, true);
  const beforeGuides = structuredClone(graphic);
  assert.deepEqual(addGuide(graphic, { direction: 'horizontal', value: 10, style: 'dashed' }), { changed: false, message: `每个情景最多${MAX_GUIDES}条辅助线。` });
  assert.deepEqual(graphic, beforeGuides);

  for (let index = 0; index < MAX_THRESHOLDS; index += 1) assert.equal(addThreshold(graphic, { token: THRESHOLD_TOKENS[index], value: 80 + index * 5 }).changed, true);
  const beforeThresholds = structuredClone(graphic);
  assert.equal(addThreshold(graphic, { token: THRESHOLD_TOKENS[0], value: 120 }).changed, false);
  assert.deepEqual(graphic, beforeThresholds);

  const fresh = createBlankGraphic('test.2-02');
  assert.equal(addThreshold(fresh, { token: 'K', value: 120 }).changed, true);
  const beforeDuplicate = structuredClone(fresh);
  assert.equal(addThreshold(fresh, { token: 'K', value: 130 }).changed, false);
  assert.deepEqual(fresh, beforeDuplicate);
  assert.equal(addGuide(fresh, { direction: 'vertical', value: 201, style: 'dashed' }).changed, false);
  assert.deepEqual(fresh, beforeDuplicate);

  assert.equal(addGuide(fresh, { direction: 'horizontal', value: 10, start: 60, end: 140, style: 'dashed' }).changed, true);
  assert.deepEqual(fresh.guides[0], { direction: 'horizontal', value: 10, start: 60, end: 140, style: 'dashed' });
  assert.equal(addGuide(fresh, { direction: 'vertical', value: 110, start: 20, end: 20, style: 'dashed' }).changed, false);
});

test('单图和整图重置恢复默认范围并保留情景绑定', () => {
  const dirty = createBlankGraphic('1.2-01');
  dirty.scale.xMin = 50;
  dirty.curve.points = [{ x: 50, y: 1 }, { x: 150, y: 2 }];
  dirty.guides = [{ direction: 'horizontal', value: 1, style: 'dashed' }];
  const reset = resetGraphic(dirty.scenarioId);
  assert.equal(reset.scenarioId, dirty.scenarioId);
  assert.deepEqual(reset.scale, DEFAULT_SCALE);
  assert.deepEqual(reset.curve.points, []);
  const all = resetAllGraphics(['1.2-01', '1.2-02']);
  assert.deepEqual(all.map((item) => item.scenarioId), ['1.2-01', '1.2-02']);
  assert(all.every((item) => item.scale.xMin === 0 && item.scale.xMax === 200 && item.guides.length === 0));
});

test('SVG以真实坐标绘制语义关键价格线与局部辅助线段', async () => {
  const config = await completeConfig();
  const graphic = config.graphics[0];
  graphic.curve = { type: 'step', strokeWidth: 3.2, points: [{ x: 70, y: 10 }, { x: 120, y: 20 }] };
  graphic.thresholds = [{ token: 'S₀', value: 100 }, { token: 'K', value: 120 }, { token: 'H_out', value: 140 }];
  graphic.guides = [
    { direction: 'vertical', value: 70, start: -40, end: 50, style: 'dashed' },
    { direction: 'horizontal', value: 10, start: 80, end: 160, style: 'dashed' },
  ];
  const svg = renderPayoffSvg(config, { editing: true });
  assert.match(svg, /data-element="x-axis" x1="58" y1="340\.00" x2="700" y2="340\.00"/);
  assert.match(svg, /data-element="y-axis" x1="379\.00" y1="162" x2="379\.00" y2="518"/);
  assert.match(svg, /M282\.70 322\.20 H443\.20 V304\.40/);
  assert.match(svg, /x1="282\.70" y1="411\.20" x2="282\.70" y2="251\.00"/);
  assert.match(svg, /x1="314\.80" y1="322\.20" x2="571\.60" y2="322\.20"/);
  assert.match(svg, /class="threshold spot"[^>]*x1="379\.00" y1="329\.00" x2="379\.00" y2="351\.00"/);
  assert.match(svg, /class="threshold strike"[^>]*x1="443\.20" y1="329\.00" x2="443\.20" y2="351\.00"/);
  assert.match(svg, /class="threshold knockout"[^>]*x1="507\.40" y1="329\.00" x2="507\.40" y2="351\.00"/);
  const visibleSvg = svg.slice(svg.indexOf('</metadata>'));
  assert.match(visibleSvg, /class="cn threshold-label"[^>]*>期初价 S₀<\/text>/);
  assert.match(visibleSvg, /class="cn threshold-label"[^>]*>执行价 K<\/text>/);
  assert.match(visibleSvg, /敲出价 H<tspan class="token-sub" baseline-shift="sub">out<\/tspan>/);
  assert.match(visibleSvg, /class="cn threshold-value"[^>]*>120%<\/text>/);
  assert.match(visibleSvg, /class="threshold knockout" style="stroke:#D89B27;stroke-dasharray:12 5"/);
  assert.match(visibleSvg, /class="threshold strike" style="stroke:#1F5FA8;stroke-dasharray:9 4"/);
  assert.match(visibleSvg, /class="guide" style="stroke:#8A949E;stroke-dasharray:4 4" x1="314\.80" y1="322\.20"/);
  assert.match(visibleSvg, /class="cn guide-label"[^>]*>10<\/text>/);
  assert.doesNotMatch(visibleSvg, /edit-boundary|Y:|·|solid/);
  assert.match(svg, /data-element="x-label"[^>]*>标的价格\(%\)<\/text>/);
  assert.doesNotMatch(svg, /标的价格S_T/);
  assert.match(svg, /data-role="node"/);
  assert.doesNotMatch(svg, /data-role="axis/);
});

test('关键价格语义和旧辅助线全跨度兼容规则稳定', () => {
  assert.deepEqual(thresholdMeta('S₀'), { family: 'spot', label: '期初价', color: '#8A8A8A', dash: '4 4' });
  assert.deepEqual(thresholdMeta('K_u'), { family: 'strike', label: '上执行价', color: '#1F5FA8', dash: '5 4' });
  assert.deepEqual(thresholdMeta('H_out'), { family: 'knockout', label: '敲出价', color: '#D89B27', dash: '12 5' });
  assert.deepEqual(thresholdMeta('H_in'), { family: 'knockin', label: '敲入价', color: '#D89B27', dash: '12 5' });
  assert.deepEqual(thresholdMeta('H_{out,2}'), { family: 'knockout', label: '第二敲出价', color: '#D89B27', dash: '8 4' });
  assert.deepEqual(thresholdMeta('H_{in,2}'), { family: 'knockin', label: '第二敲入价', color: '#D89B27', dash: '8 4' });
  assert.deepEqual(thresholdMeta('H_{hedge,2}'), { family: 'barrier', label: '第二避险线', color: '#D89B27', dash: '6 3' });
  assert.deepEqual(thresholdMeta('B'), { family: 'barrier', label: '缓冲价', color: '#D89B27', dash: '8 3' });
  assert.deepEqual(thresholdMeta('L'), { family: 'barrier', label: '限损价', color: '#D89B27', dash: '7 3' });
  assert.deepEqual(guideExtent({ direction: 'horizontal', value: 10, style: 'solid' }, DEFAULT_SCALE), { start: 0, end: 200 });
  assert.deepEqual(guideExtent({ direction: 'vertical', value: 120, start: -20, end: 80, style: 'solid' }, DEFAULT_SCALE), { start: -20, end: 80 });
  assert.deepEqual(materializeGuideExtent({ direction: 'horizontal', value: 10, style: 'dashed' }, DEFAULT_SCALE), { direction: 'horizontal', value: 10, style: 'dashed', start: 0, end: 200 });
});

test('局部辅助线在新旧JSON中统一规范为中性灰虚线', async () => {
  const config = await completeConfig();
  config.graphics[0].guides = [{ direction: 'horizontal', value: 0, start: 80, end: 120, style: 'solid' }];
  const migrated = migrateConfig(config);
  assert.equal(migrated.graphics[0].guides[0].style, 'dashed');
  const svg = renderPayoffSvg(config);
  assert.match(svg, /class="guide" style="stroke:#8A949E;stroke-dasharray:4 4"/);
  assert.doesNotMatch(svg.slice(svg.indexOf('</metadata>')), /Y:|edit-boundary/);
});

test('关键线按语义家族分色，JSON不接受颜色覆写', async () => {
  const strikeTokens = ['K', 'K₁', 'K₂', 'K₃', 'K₄', 'K_p', 'K_c', 'K_u', 'K_d'];
  const barrierTokens = ['H', 'H_out', 'H_{out,2}', 'H_{out,t}', 'H_in', 'H_{in,2}', 'H_c', 'H_floor', 'H_reset', 'H_u', 'H_d', 'H_{hedge,1}', 'H_{hedge,2}', 'B', 'L'];
  assert(strikeTokens.every((token) => thresholdMeta(token).color === '#1F5FA8'));
  assert(barrierTokens.every((token) => thresholdMeta(token).color === '#D89B27'));
  const guideColors = Array.from({ length: MAX_GUIDES }, (_, index) => guideMeta(index).color);
  assert.deepEqual(guideColors, Array(MAX_GUIDES).fill('#8A949E'));

  const config = await completeConfig();
  config.graphics[0].thresholds = [{ token: 'H_out', value: 120, color: '#000000' }];
  const validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'LOCKED_FIELD'));
});

test('唯一的默认执行价K=100%保留在JSON但不占图线或图例', async () => {
  const config = await completeConfig();
  config.graphics[0].thresholds = [{ token: 'K', value: 100 }];
  const svg = renderPayoffSvg(config);
  const visibleSvg = svg.slice(svg.indexOf('</metadata>'));
  assert.doesNotMatch(visibleSvg, /threshold-group/);
  assert.doesNotMatch(visibleSvg, />执行价 <tspan class="legend-token">K<\/tspan> = 100%<\/text>/);
});

test('方差互换以真实波动率作为横轴且默认执行价不重复出图', async () => {
  const config = await createConfig('10.4');
  const svg = renderPayoffSvg(config);
  const visibleSvg = svg.slice(svg.indexOf('</metadata>'));
  assert.match(visibleSvg, /data-element="x-label"[^>]*>已实现波动率σ<\/text>/);
  assert.match(visibleSvg, /data-element="x-reference"[^>]*>13<\/text>/);
  assert.doesNotMatch(visibleSvg, /data-token="K"/);
  assert.doesNotMatch(visibleSvg, />执行价 <tspan class="legend-token">K<\/tspan> = 13<\/text>/);
});

test('v1草稿和原生SVG在加载时迁移为v2，保存及发布写入v2', async () => {
  const config = await completeConfig();
  const legacy = legacyFrom(config);
  const migrated = migrateConfig(legacy);
  assert.equal(migrated.$schema, SCHEMA);
  assert.equal(migrated.schemaVersion, 2);
  assert.deepEqual(migrated.graphics[0].scale, DEFAULT_SCALE);
  assert.deepEqual(migrated.graphics[0].curve.points, [{ x: 70, y: 10 }, { x: 120, y: 20 }]);
  assert.deepEqual(migrated.graphics[0].thresholds, [{ token: 'K', value: 120 }]);
  assert.deepEqual(migrated.graphics[0].guides, [{ direction: 'vertical', value: 70, style: 'dashed' }, { direction: 'horizontal', value: 20, style: 'dashed' }]);
  assert.equal((await validateConfig(migrated)).errors.length, 0);

  const { root, paths } = await temporaryPaths();
  try {
    const base = productFileBase(legacy.library.id, legacy.library.name);
    await fs.mkdir(paths.source, { recursive: true });
    await fs.writeFile(path.join(paths.source, `${base}.payoff.json`), JSON.stringify(legacy), 'utf8');
    const loaded = await loadStoredConfig(legacy.library.id, { paths });
    assert.equal(loaded.migrated, true);
    assert.equal(loaded.config.schemaVersion, 2);

    const saved = await saveDraft(legacy, { paths });
    assert.equal(saved.saved, true);
    const savedRaw = JSON.parse(await fs.readFile(path.join(paths.source, `${base}.payoff.json`), 'utf8'));
    assert.equal(savedRaw.schemaVersion, 2);

    const inspected = await inspectNativeSvg(renderPayoffSvg(saved.config), { paths });
    assert.equal(inspected.editable, true);

    const published = await publish(saved.config, { paths });
    assert.equal(published.published, true);
    assert.equal((await fs.stat(paths.payoff)).isDirectory(), true);
    assert.equal((await fs.stat(published.locations.formalSvg)).isFile(), true);

    const rendered = path.join(root, 'preview.svg');
    const cli = await cliRender(legacy, rendered);
    assert.equal(cli.errors.length, 0);
    assert.equal((await fs.stat(rendered)).isFile(), true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('发布前审阅明确展示覆盖范围、待绘制情景和图形改动', async () => {
  const config = await completeConfig('2.1');
  const { root, paths } = await temporaryPaths();
  try {
    let review = await buildPublishReview(config, { paths });
    assert.equal(review.formal.kind, 'new');
    assert.equal(review.summary.complete, config.graphics.length);
    assert.equal(review.summary.added, config.graphics.length);

    await publish(config, { paths });
    config.graphics[0].curve.points[1].y = 42;
    config.graphics[1].curve.points = [];
    review = await buildPublishReview(config, { paths });
    assert.equal(review.formal.kind, 'replace');
    assert.equal(review.summary.changed, 2);
    assert.equal(review.summary.pending, 1);
    assert.equal(review.scenarios[1].curve, 'pending');
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('资料库情景和子图顺序仍严格绑定，外部非原生SVG保持只读', async () => {
  const config = await completeConfig();
  config.library.scenarios[0].title = '人工改写';
  config.graphics.reverse();
  const validation = await validateConfig(config);
  assert(validation.errors.some((item) => item.code === 'LIBRARY_SCENARIO_MISMATCH'));
  assert(validation.errors.some((item) => item.code === 'GRAPHICS_SCENARIO_MISMATCH'));
  const inspected = await inspectNativeSvg('<svg xmlns="http://www.w3.org/2000/svg"><text>external</text></svg>');
  assert.equal(inspected.editable, false);
  assert.match(inspected.reason, /只读/);
});
