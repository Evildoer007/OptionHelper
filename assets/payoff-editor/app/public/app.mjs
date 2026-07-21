const state = {
  products: [],
  product: null,
  productDraft: null,
  draftLookupPending: false,
  draftLookupVersion: 0,
  draftOrigin: null,
  config: null,
  validation: null,
  editing: false,
  dirty: false,
  externalSvg: null,
  selectedScenario: 0,
  renderTimer: null,
  renderVersion: 0,
  dragging: null,
  livePreviewFrame: null,
  panelResize: null,
  zoom: 0.72,
};

const $ = (id) => document.getElementById(id);
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const round = (value, decimals) => Math.round((value + Number.EPSILON) * (10 ** decimals)) / (10 ** decimals);
const normaliseRatio = (value) => round(clamp(value, 0, 1), 4);
const normaliseWidth = (value) => round(clamp(value, 0.1, 20), 2);
const displaySection = (value) => String(value || '').replace(/^#{1,6}\s*/, '');
const tokenOptions = [
  'S₀', 'K', 'K₁', 'K₂', 'K₃', 'K₄', 'K_p', 'K_c', 'K_u', 'K_d',
  'H', 'H_in', 'H_out', 'H_floor', 'H_reset', 'H_u', 'H_d', 'H_{out,t}',
];
const numericSpecs = Object.freeze({
  position: { min: 0, max: 100, decimals: 2, unit: '%' },
  width: { min: 0.1, max: 20, decimals: 2, unit: 'px' },
});
const chartLayout = Object.freeze({ left: 42, right: 684, top: 70, bottom: 426 });

for (const token of tokenOptions) $('thresholdToken').append(new Option(token, token));

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'content-type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  return { response, body: await response.json() };
}

function element(tag, textValue = '', className = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textValue !== '') node.textContent = textValue;
  return node;
}

function clearDocument() {
  state.config = null;
  state.externalSvg = null;
  state.validation = null;
  state.editing = false;
  state.draftOrigin = null;
  state.selectedScenario = 0;
  state.dirty = false;
  $('canvasViewport').innerHTML = emptyCanvas();
}

function confirmDiscardChanges(action) {
  if (!state.dirty) return true;
  return window.confirm(`当前图有未保存修改。${action}会放弃浏览器中的修改，是否继续？`);
}

function markDirty() {
  state.dirty = true;
}

function selectedGraphic() { return state.config?.graphics?.[state.selectedScenario]; }
function selectedSource() { return state.config?.library?.scenarios?.[state.selectedScenario]; }
function editable() { return Boolean(state.config && state.editing && !state.externalSvg && state.validation?.errors?.length === 0); }
function formatNumber(value, decimals = 2) { return String(round(Number(value), decimals)).replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1'); }
function formatPercent(value) { return formatNumber(value * 100); }

function prepareConfig(config) {
  for (const graphic of config?.graphics || []) {
    if (!Array.isArray(graphic.guides)) graphic.guides = [];
    delete graphic.axis?.zeroY;
  }
  return config;
}

function emptyCanvas() {
  return '<div class="empty-canvas" id="emptyCanvas"><h2>选择产品后开始绘制</h2><p>产品和情景将由OptionList与OptionLib自动导入。</p></div>';
}

function setNotice(validation = null, extra = '') {
  const errors = validation?.errors || [];
  const warnings = validation?.warnings || [];
  const message = errors[0]?.message || extra || warnings[0]?.message || '';
  const target = $('noticeArea');
  if (!message) { target.replaceChildren(); return; }
  const hint = errors[0]?.section ? `定位：references/optionlib.md · ${errors[0].section}` : '';
  const notice = element('div', '', `notice${errors.length ? ' error' : ''}`);
  notice.append(element('strong', errors.length ? '资料库或图形校验未通过' : '待完成'));
  const detail = element('span', message);
  if (hint) {
    detail.append(document.createElement('br'), element('code', hint));
  }
  notice.append(detail);
  target.replaceChildren(notice);
}

function renderProducts() {
  const select = $('productSelect');
  select.innerHTML = '<option value="">选择产品</option>';
  for (const product of state.products) {
    const option = new Option(`${product.id} · ${product.name}${product.available ? ` · ${product.scenarioCount}个情景` : ' · 待补资料库'}`, product.id);
    option.disabled = !product.available;
    select.add(option);
  }
}

function renderBinding() {
  const card = $('bindingCard');
  if (!state.product) { card.replaceChildren(element('span', '尚未选择产品')); return; }
  if (!state.product.available) {
    card.replaceChildren(element('strong', '资料库不可用'), element('span', state.product.issue?.message || '请补充optionlib.md。'));
    return;
  }
  const draftLabel = state.draftLookupPending
    ? '正在检测已有草稿…'
    : state.productDraft ? '已找到可打开的本地草稿' : '尚无已保存草稿，可从模板新建';
  card.replaceChildren(
    element('strong', state.product.name),
    element('span', `编号${state.product.id} · ${displaySection(state.product.section)} · ${state.product.status}`),
    element('span', `${state.product.scenarioCount}个情景已由资料库导入`),
    element('span', draftLabel),
  );
}

function selectScenario(index) {
  state.selectedScenario = index;
  updateUi();
  scheduleRender(0);
}

function renderScenarioList() {
  const scenarios = state.product?.scenarios || state.config?.library?.scenarios || [];
  $('scenarioCount').textContent = scenarios.length;
  const list = $('scenarioList');
  const active = Boolean(state.config) && !state.externalSvg;
  list.replaceChildren(...scenarios.map((scenario, index) => {
    const item = element('li', scenario.title, active && index === state.selectedScenario ? 'selected' : 'preview');
    if (active) {
      item.dataset.scenarioSelect = String(index);
      item.addEventListener('click', () => selectScenario(index));
    } else {
      item.setAttribute('aria-disabled', 'true');
    }
    return item;
  }));
}

function numericSpec(input) { return numericSpecs[input.dataset.numeric]; }

function readNumericInput(input, { format = false } = {}) {
  const spec = numericSpec(input);
  if (!spec) return null;
  const raw = input.value.trim();
  const decimalPattern = new RegExp(`^\\d+(?:\\.\\d{0,${spec.decimals}})?$`);
  const value = Number(raw);
  let message = '';
  if (!raw || !decimalPattern.test(raw) || !Number.isFinite(value)) message = `请输入最多${spec.decimals}位小数的数字。`;
  else if (value < spec.min || value > spec.max) message = `请输入${spec.min}至${spec.max}${spec.unit}。`;
  if (message) {
    input.setCustomValidity(message);
    input.setAttribute('aria-invalid', 'true');
    return null;
  }
  input.setCustomValidity('');
  input.removeAttribute('aria-invalid');
  const normalised = round(value, spec.decimals);
  if (format) input.value = formatNumber(normalised, spec.decimals);
  return normalised;
}

function ensureNumericControlsValid() {
  const invalid = [...document.querySelectorAll('[data-numeric]')].find((input) => readNumericInput(input) === null);
  if (!invalid) return true;
  invalid.focus();
  setNotice(null, invalid.validationMessage || '请修正图形控制中的数值。');
  return false;
}

function bindNumericInput(input, callback) {
  input.addEventListener('input', () => {
    const value = readNumericInput(input);
    if (value !== null) callback(value);
  });
  input.addEventListener('change', () => { readNumericInput(input, { format: true }); });
  input.addEventListener('blur', () => { readNumericInput(input, { format: true }); });
}

function renderPointTable(enabled) {
  const target = $('pointTable');
  const points = selectedGraphic()?.curve?.points || [];
  if (!points.length) {
    target.innerHTML = '<div class="parameter-empty">尚未设置曲线节点。点击“生成起点”后可直接填写X%、Y%。</div>';
    return;
  }
  target.innerHTML = `<div class="point-head"><span>节点</span><span>X%</span><span>Y%</span><span></span></div>${points.map((point, index) => `<div class="point-row"><span>${String(index + 1).padStart(2, '0')}</span><input data-point-x="${index}" data-numeric="position" type="number" min="0" max="100" step="0.01" value="${formatPercent(point.x)}" ${enabled ? '' : 'disabled'} /><input data-point-y="${index}" data-numeric="position" type="number" min="0" max="100" step="0.01" value="${formatPercent(point.y)}" ${enabled ? '' : 'disabled'} /><button class="row-delete" data-delete-point="${index}" title="删除节点" ${enabled && points.length > 2 ? '' : 'disabled'}>×</button></div>`).join('')}`;
  target.querySelectorAll('[data-point-x], [data-point-y]').forEach((input) => bindNumericInput(input, (value) => {
    const index = Number(input.dataset.pointX ?? input.dataset.pointY);
    setPoint(index, input.dataset.pointX === undefined ? 'y' : 'x', value / 100);
  }));
  target.querySelectorAll('[data-delete-point]').forEach((button) => button.addEventListener('click', () => {
    const pointsForGraphic = selectedGraphic().curve.points;
    if (pointsForGraphic.length <= 2) return;
    pointsForGraphic.splice(Number(button.dataset.deletePoint), 1);
    markDirty();
    updateUi();
    scheduleRender(0);
  }));
}

function guideOptions(selected) {
  return `<option value="horizontal" ${selected === 'horizontal' ? 'selected' : ''}>横线</option><option value="vertical" ${selected === 'vertical' ? 'selected' : ''}>纵线</option>`;
}

function guideStyleOptions(selected) {
  return `<option value="solid" ${selected === 'solid' ? 'selected' : ''}>实线</option><option value="dashed" ${selected === 'dashed' ? 'selected' : ''}>虚线</option>`;
}

function renderGuideList(enabled) {
  const guides = selectedGraphic()?.guides || [];
  $('guideList').innerHTML = guides.length ? guides.map((guide, index) => `<div class="line-row guide-row"><select data-guide-direction="${index}" ${enabled ? '' : 'disabled'}>${guideOptions(guide.direction)}</select><input data-guide-position="${index}" data-numeric="position" type="number" min="0" max="100" step="0.01" value="${formatPercent(guide.position)}" ${enabled ? '' : 'disabled'} /><select data-guide-style="${index}" ${enabled ? '' : 'disabled'}>${guideStyleOptions(guide.style)}</select><button class="row-delete" data-delete-guide="${index}" title="删除辅助线" ${enabled ? '' : 'disabled'}>×</button></div>`).join('') : '<div class="parameter-empty">未添加辅助线。</div>';
  $('guideList').querySelectorAll('[data-guide-position]').forEach((input) => bindNumericInput(input, (value) => updateGraphic((graphic) => { graphic.guides[Number(input.dataset.guidePosition)].position = normaliseRatio(value / 100); })));
  $('guideList').querySelectorAll('[data-guide-direction]').forEach((select) => select.addEventListener('change', () => {
    updateGraphic((graphic) => { graphic.guides[Number(select.dataset.guideDirection)].direction = select.value; }, 0);
    updateUi();
  }));
  $('guideList').querySelectorAll('[data-guide-style]').forEach((select) => select.addEventListener('change', () => updateGraphic((graphic) => { graphic.guides[Number(select.dataset.guideStyle)].style = select.value; })));
  $('guideList').querySelectorAll('[data-delete-guide]').forEach((button) => button.addEventListener('click', () => {
    selectedGraphic().guides.splice(Number(button.dataset.deleteGuide), 1);
    markDirty();
    updateUi();
    scheduleRender(0);
  }));
}

function thresholdOptions(selected, thresholds, currentIndex) {
  return tokenOptions.map((token) => `<option value="${token}" ${token === selected ? 'selected' : ''} ${thresholds.some((threshold, index) => index !== currentIndex && threshold.token === token) ? 'disabled' : ''}>${token}</option>`).join('');
}

function renderThresholdList(enabled) {
  const thresholds = selectedGraphic()?.thresholds || [];
  $('thresholdList').innerHTML = thresholds.length ? thresholds.map((threshold, index) => `<div class="line-row threshold-row"><select data-threshold-token="${index}" ${enabled ? '' : 'disabled'}>${thresholdOptions(threshold.token, thresholds, index)}</select><input data-threshold-position="${index}" data-numeric="position" type="number" min="0" max="100" step="0.01" value="${formatPercent(threshold.x)}" ${enabled ? '' : 'disabled'} /><button class="row-delete" data-delete-threshold="${index}" title="删除关键价格线" ${enabled ? '' : 'disabled'}>×</button></div>`).join('') : '<div class="parameter-empty">未添加关键价格线。</div>';
  $('thresholdList').querySelectorAll('[data-threshold-position]').forEach((input) => bindNumericInput(input, (value) => updateGraphic((graphic) => { graphic.thresholds[Number(input.dataset.thresholdPosition)].x = normaliseRatio(value / 100); })));
  $('thresholdList').querySelectorAll('[data-threshold-token]').forEach((select) => select.addEventListener('change', () => {
    const index = Number(select.dataset.thresholdToken);
    if (selectedGraphic().thresholds.some((threshold, thresholdIndex) => thresholdIndex !== index && threshold.token === select.value)) return;
    updateGraphic((graphic) => { graphic.thresholds[index].token = select.value; }, 0);
    updateUi();
  }));
  $('thresholdList').querySelectorAll('[data-delete-threshold]').forEach((button) => button.addEventListener('click', () => {
    selectedGraphic().thresholds.splice(Number(button.dataset.deleteThreshold), 1);
    markDirty();
    updateUi();
    scheduleRender(0);
  }));
}

function setEditingControls() {
  const graphic = selectedGraphic();
  const enabled = editable() && Boolean(graphic);
  const controls = ['curveType', 'strokeWidth', 'axisLeft', 'axisBottom', 'addCurveButton', 'addNodeButton', 'guideDirection', 'guidePosition', 'guideStyle', 'addGuideButton', 'thresholdToken', 'thresholdPosition', 'addThresholdButton'];
  controls.forEach((id) => { $(id).disabled = !enabled; });
  $('editButton').disabled = !state.config || Boolean(state.externalSvg) || state.validation?.errors?.length > 0;
  $('saveButton').disabled = !state.config || Boolean(state.externalSvg) || state.validation?.errors?.length > 0;
  const curvesComplete = !state.validation?.warnings?.some((item) => item.code === 'CURVE_PENDING');
  const publishable = state.product?.status === '已录入';
  $('publishButton').disabled = !state.config || Boolean(state.externalSvg) || state.validation?.errors?.length > 0 || !curvesComplete || !publishable;
  if (!graphic) {
    renderPointTable(false);
    renderGuideList(false);
    renderThresholdList(false);
    return;
  }
  $('curveType').value = graphic.curve.type;
  $('strokeWidth').value = formatNumber(graphic.curve.strokeWidth);
  $('axisLeft').value = formatPercent(graphic.axis.left);
  $('axisBottom').value = formatPercent(graphic.axis.bottom);
  renderPointTable(enabled);
  renderGuideList(enabled);
  renderThresholdList(enabled);
}

function updateUi() {
  const source = selectedSource();
  const external = Boolean(state.externalSvg);
  $('modeBadge').textContent = external
    ? '外部SVG · 只读'
    : state.config ? `${state.config.library.id} · ${state.config.library.name}${state.dirty ? ' · 未保存' : ''}` : '选择产品后开始';
  const canvasMode = state.editing ? '编辑模式：拖动或参数设置' : '预览锁定';
  $('canvasState').textContent = external ? '外部SVG只读' : state.config ? `${canvasMode}${state.dirty ? ' · 未保存修改' : ''}` : '预览锁定';
  $('stateDot').className = `state-dot ${state.editing ? 'edit' : state.dirty ? 'dirty' : state.validation?.errors?.length ? '' : state.config ? 'valid' : ''}`;
  document.body.classList.toggle('editing', state.editing);
  document.body.classList.toggle('readonly', external);
  $('newTemplateButton').disabled = !state.product?.available;
  $('openDraftButton').disabled = !state.product?.available || !state.productDraft || state.draftLookupPending;
  $('openDraftButton').textContent = state.draftLookupPending ? '检测现有草稿…' : '打开现有草稿';
  $('reimportButton').disabled = !state.product?.available || !state.validation?.errors?.some((item) => item.action === 'update_optionlib');
  $('inspectorTitle').textContent = source ? `情景${state.selectedScenario + 1}：${source.title}` : '未选择情景';
  $('sourceDetail').textContent = source ? `来源：${displaySection(state.config.library.section)} · “情景”列。标题、条件和净损益文字均锁定。${source.condition ? ` 条件：${source.condition}` : ''}` : '选择产品后显示资料库来源。';
  renderBinding();
  renderScenarioList();
  setEditingControls();
  const publicationNotice = state.config && state.product?.status !== '已录入'
    ? '当前产品为“待录入”，可保存草稿，补齐资源并改为“已录入”后才能发布正式SVG。'
    : '';
  setNotice(state.validation, publicationNotice);
  applyZoom();
}

function applyZoom() {
  const stage = $('canvasStage');
  if (stage) stage.style.setProperty('--zoom', state.zoom.toFixed(2));
  $('zoomInput').value = String(Math.round(state.zoom * 100));
}

function setZoom(value) {
  state.zoom = clamp(round(value, 2), 0.01, 4);
  applyZoom();
}

function setZoomFromInput({ format = false } = {}) {
  const input = $('zoomInput');
  const value = Number(input.value);
  if (!Number.isInteger(value) || value < 1 || value > 400) {
    input.setCustomValidity('缩放比例必须是1%至400%的整数。');
    input.setAttribute('aria-invalid', 'true');
    return false;
  }
  input.setCustomValidity('');
  input.removeAttribute('aria-invalid');
  if (format) input.value = String(value);
  setZoom(value / 100);
  return true;
}

function fitZoom() {
  const width = $('canvasViewport').clientWidth - 48;
  setZoom(clamp(Math.round((width / 1500) * 100) / 100, 0.01, 1));
}

function attachCanvasInteractions() {
  const svg = $('canvasViewport').querySelector('svg');
  if (!svg || !state.editing) return;
  svg.querySelectorAll('[data-scenario-card]').forEach((card) => card.addEventListener('click', () => {
    if (!state.dragging) selectScenario(Number(card.dataset.scenarioCard));
  }));
  svg.querySelectorAll('[data-role]').forEach((handle) => handle.addEventListener('pointerdown', (event) => beginDrag(event, svg, handle)));
}

function injectSvg(svg) {
  $('canvasViewport').innerHTML = `<div class="canvas-stage" id="canvasStage">${svg}</div>`;
  applyZoom();
  attachCanvasInteractions();
}

function injectReadOnlySvg(svg) {
  $('canvasViewport').innerHTML = `<div class="canvas-stage" id="canvasStage"><img alt="外部SVG只读预览" src="data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}" /></div>`;
  applyZoom();
}

async function refreshPreview() {
  if (state.externalSvg || !state.config) return;
  const version = ++state.renderVersion;
  const { body } = await request('/api/render', { method: 'POST', body: { config: state.config, editing: state.editing } });
  if (version !== state.renderVersion) return;
  state.validation = body.validation || state.validation;
  if (body.ok) injectSvg(body.svg);
  updateUi();
}

function scheduleRender(wait = 140) {
  state.renderVersion += 1;
  clearTimeout(state.renderTimer);
  state.renderTimer = setTimeout(() => refreshPreview().catch((error) => setNotice(null, error.message)), wait);
}

function chartGeometry(card, axis) {
  const cardX = Number(card.dataset.cardX);
  const cardY = Number(card.dataset.cardY);
  const left = cardX + chartLayout.left;
  const right = cardX + chartLayout.right;
  const top = cardY + chartLayout.top;
  const bottom = cardY + chartLayout.bottom;
  const width = right - left;
  const height = bottom - top;
  return { left, right, top, bottom, width, height, yAxisX: left + axis.left * width, xAxisY: bottom - axis.bottom * height };
}

function pointToSvg(point, geometry) { return { x: geometry.left + point.x * geometry.width, y: geometry.top + point.y * geometry.height }; }

function clientCurvePath(points, type, geometry) {
  if (!points.length) return '';
  const converted = points.map((point) => pointToSvg(point, geometry));
  let path = `M${converted[0].x.toFixed(2)} ${converted[0].y.toFixed(2)}`;
  for (let index = 1; index < converted.length; index += 1) path += type === 'step' ? ` H${converted[index].x.toFixed(2)} V${converted[index].y.toFixed(2)}` : ` L${converted[index].x.toFixed(2)} ${converted[index].y.toFixed(2)}`;
  return path;
}

function setAttrs(element, values) {
  if (!element) return;
  for (const [name, value] of Object.entries(values)) element.setAttribute(name, String(value));
}

function updateLivePreview() {
  const svg = $('canvasViewport').querySelector('svg');
  const graphic = selectedGraphic();
  const card = svg?.querySelector(`[data-scenario-card="${state.selectedScenario}"]`);
  if (!card || !graphic) return;
  const geometry = chartGeometry(card, graphic.axis);
  const xLabelY = Math.min(geometry.bottom + 24, geometry.xAxisY + 24);
  const xReferenceLabelY = Math.min(geometry.bottom + 23, geometry.xAxisY + 23);
  const xReferenceAnchor = graphic.axis.left >= 0.88 ? 'end' : 'start';
  const xReferenceX = geometry.yAxisX + (xReferenceAnchor === 'end' ? -10 : 10);
  const yLabelAnchor = graphic.axis.left >= 0.9 ? 'end' : 'start';
  setAttrs(card.querySelector('[data-element="x-axis"]'), { x1: geometry.left, y1: geometry.xAxisY, x2: geometry.right, y2: geometry.xAxisY });
  setAttrs(card.querySelector('[data-element="y-axis"]'), { x1: geometry.yAxisX, y1: geometry.top, x2: geometry.yAxisX, y2: geometry.bottom });
  setAttrs(card.querySelector('[data-element="y-label"]'), { x: geometry.yAxisX, y: geometry.top - 12, 'text-anchor': yLabelAnchor });
  setAttrs(card.querySelector('[data-element="x-reference"]'), { x: xReferenceX, y: xReferenceLabelY, 'text-anchor': xReferenceAnchor });
  setAttrs(card.querySelector('[data-element="x-label"]'), { x: geometry.right, y: xLabelY });
  setAttrs(card.querySelector('[data-element="axis-y-arrow"]'), { d: `M${geometry.yAxisX} ${geometry.top - 7} L${geometry.yAxisX - 4.5} ${geometry.top + 2} L${geometry.yAxisX + 4.5} ${geometry.top + 2} Z` });
  setAttrs(card.querySelector('[data-element="axis-x-arrow"]'), { d: `M${geometry.right + 7} ${geometry.xAxisY} L${geometry.right - 2} ${geometry.xAxisY - 4.5} L${geometry.right - 2} ${geometry.xAxisY + 4.5} Z` });
  setAttrs(card.querySelector('[data-role="axis-y"]'), { cx: geometry.yAxisX, cy: geometry.top + 13 });
  setAttrs(card.querySelector('[data-role="axis-x"]'), { cx: geometry.right - 13, cy: geometry.xAxisY });
  const curve = card.querySelector('[data-element="curve"]');
  setAttrs(curve, { d: clientCurvePath(graphic.curve.points, graphic.curve.type, geometry), 'stroke-width': graphic.curve.strokeWidth });
  graphic.curve.points.forEach((point, index) => {
    const converted = pointToSvg(point, geometry);
    setAttrs(card.querySelector(`[data-role="node"][data-point="${index}"]`), { cx: converted.x, cy: converted.y });
  });
  graphic.thresholds.forEach((threshold, index) => {
    const group = card.querySelector(`[data-element="threshold"][data-index="${index}"]`);
    const x = geometry.left + threshold.x * geometry.width;
    setAttrs(group?.querySelector('line'), { x1: x, y1: geometry.top, x2: x, y2: geometry.bottom });
    setAttrs(group?.querySelector('text'), { x, y: geometry.bottom + 24 });
    setAttrs(group?.querySelector('[data-role="threshold"]'), { cx: x, cy: geometry.bottom });
  });
  graphic.guides.forEach((guide, index) => {
    const group = card.querySelector(`[data-element="guide"][data-index="${index}"]`);
    const horizontal = guide.direction === 'horizontal';
    const position = horizontal ? geometry.top + guide.position * geometry.height : geometry.left + guide.position * geometry.width;
    setAttrs(group?.querySelector('line'), horizontal ? { x1: geometry.left, y1: position, x2: geometry.right, y2: position } : { x1: position, y1: geometry.top, x2: position, y2: geometry.bottom });
    setAttrs(group?.querySelector('[data-role="guide"]'), { cx: horizontal ? geometry.right : position, cy: horizontal ? position : geometry.top });
  });
}

function queueLivePreview() {
  if (state.livePreviewFrame) return;
  state.livePreviewFrame = requestAnimationFrame(() => {
    state.livePreviewFrame = null;
    updateLivePreview();
  });
}

function flushLivePreview() {
  if (state.livePreviewFrame) {
    cancelAnimationFrame(state.livePreviewFrame);
    state.livePreviewFrame = null;
  }
  updateLivePreview();
}

function svgPoint(event, svg) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}

function beginDrag(event, svg, handle) {
  event.preventDefault();
  event.stopPropagation();
  state.selectedScenario = Number(handle.dataset.scenario);
  state.renderVersion += 1;
  state.dragging = { svg, role: handle.dataset.role, data: handle.dataset };
  handle.setPointerCapture?.(event.pointerId);
}

function boundedNodeX(value, points, index, type) {
  const previous = points[index - 1];
  const next = points[index + 1];
  const gap = type === 'polyline' ? 0.0001 : 0;
  const min = previous ? previous.x + gap : 0;
  const max = next ? next.x - gap : 1;
  return normaliseRatio(clamp(value, min, Math.max(min, max)));
}

function dragMove(event) {
  if (!state.dragging) return;
  const { svg, role, data } = state.dragging;
  const graphic = selectedGraphic();
  const card = svg.querySelector(`[data-scenario-card="${state.selectedScenario}"]`);
  if (!graphic || !card) return;
  const point = svgPoint(event, svg);
  const geometry = chartGeometry(card, graphic.axis);
  if (role === 'node') {
    const index = Number(data.point);
    graphic.curve.points[index].x = boundedNodeX((point.x - geometry.left) / geometry.width, graphic.curve.points, index, graphic.curve.type);
    graphic.curve.points[index].y = normaliseRatio((point.y - geometry.top) / geometry.height);
  } else if (role === 'axis-y') {
    graphic.axis.left = normaliseRatio((point.x - geometry.left) / geometry.width);
  } else if (role === 'axis-x') {
    graphic.axis.bottom = normaliseRatio((geometry.bottom - point.y) / geometry.height);
  } else if (role === 'threshold') {
    graphic.thresholds[Number(data.threshold)].x = normaliseRatio((point.x - geometry.left) / geometry.width);
  } else if (role === 'guide') {
    const guide = graphic.guides[Number(data.guide)];
    guide.position = normaliseRatio(guide.direction === 'horizontal'
      ? (point.y - geometry.top) / geometry.height
      : (point.x - geometry.left) / geometry.width);
  }
  markDirty();
  queueLivePreview();
}

function endDrag() {
  if (!state.dragging) return;
  state.dragging = null;
  flushLivePreview();
  updateUi();
  scheduleRender(0);
}

function updateGraphic(mutator, wait = 180) {
  const graphic = selectedGraphic();
  if (!graphic || !editable()) return;
  mutator(graphic);
  markDirty();
  queueLivePreview();
  scheduleRender(wait);
}

function setPoint(index, field, value) {
  updateGraphic((graphic) => {
    const points = graphic.curve.points;
    if (field === 'x') points[index].x = boundedNodeX(value, points, index, graphic.curve.type);
    else points[index].y = normaliseRatio(value);
  });
}

function panelWidth(side) {
  const variable = side === 'library' ? '--library-width' : '--inspector-width';
  return Number.parseFloat(getComputedStyle($('workbench')).getPropertyValue(variable)) || (side === 'library' ? 267 : 310);
}

function setPanelWidth(side, value) {
  const workbench = $('workbench');
  const other = panelWidth(side === 'library' ? 'inspector' : 'library');
  const naturalMax = side === 'library' ? 390 : 460;
  const naturalMin = side === 'library' ? 220 : 270;
  const centerSafeMax = workbench.clientWidth - other - 580;
  const width = Math.round(clamp(value, naturalMin, Math.max(naturalMin, Math.min(naturalMax, centerSafeMax))));
  const variable = side === 'library' ? '--library-width' : '--inspector-width';
  workbench.style.setProperty(variable, `${width}px`);
  const resizer = side === 'library' ? $('libraryResizer') : $('inspectorResizer');
  resizer.setAttribute('aria-valuemin', String(naturalMin));
  resizer.setAttribute('aria-valuemax', String(Math.max(naturalMin, Math.min(naturalMax, centerSafeMax))));
  resizer.setAttribute('aria-valuenow', String(width));
}

function beginPanelResize(event, side) {
  event.preventDefault();
  const resizer = event.currentTarget;
  state.panelResize = { side, startX: event.clientX, startWidth: panelWidth(side), resizer };
  resizer.classList.add('active');
  resizer.setPointerCapture?.(event.pointerId);
}

function movePanelResize(event) {
  if (!state.panelResize) return;
  const { side, startX, startWidth } = state.panelResize;
  const delta = event.clientX - startX;
  setPanelWidth(side, startWidth + (side === 'library' ? delta : -delta));
}

function endPanelResize() {
  if (!state.panelResize) return;
  state.panelResize.resizer.classList.remove('active');
  state.panelResize = null;
}

function wirePanelResizers() {
  [['libraryResizer', 'library'], ['inspectorResizer', 'inspector']].forEach(([id, side]) => {
    const resizer = $(id);
    setPanelWidth(side, panelWidth(side));
    resizer.addEventListener('pointerdown', (event) => beginPanelResize(event, side));
    resizer.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const direction = event.key === 'ArrowRight' ? 1 : -1;
      setPanelWidth(side, panelWidth(side) + direction * (side === 'library' ? 10 : -10));
    });
  });
}

async function loadProducts({ preserve = true } = {}) {
  const { body } = await request('/api/products');
  state.products = body.products;
  if (preserve && state.product) state.product = state.products.find((item) => item.id === state.product.id) || null;
  renderProducts();
  if (state.product) $('productSelect').value = state.product.id;
  updateUi();
}

async function checkSelectedDraft() {
  if (!state.product?.available) return;
  const productId = state.product.id;
  const version = ++state.draftLookupVersion;
  state.productDraft = null;
  state.draftLookupPending = true;
  updateUi();
  const { body } = await request(`/api/config?id=${encodeURIComponent(productId)}`);
  if (version !== state.draftLookupVersion || state.product?.id !== productId) return;
  state.draftLookupPending = false;
  state.productDraft = body.config ? { config: body.config, validation: body.validation } : null;
  updateUi();
}

async function newTemplate() {
  if (!state.product?.available) return;
  if (!confirmDiscardChanges('从模板重新开始')) return;
  const { body } = await request('/api/drafts/new', { method: 'POST', body: { id: state.product.id } });
  if (!body.ok) { state.validation = body.validation || { errors: [{ message: body.message || '无法创建草稿。' }], warnings: [] }; updateUi(); return; }
  state.config = prepareConfig(body.config);
  state.externalSvg = null;
  state.editing = false;
  state.selectedScenario = 0;
  state.validation = body.validation;
  state.dirty = true;
  state.draftOrigin = 'new';
  injectSvg(body.svg);
  updateUi();
  requestAnimationFrame(fitZoom);
}

async function openExistingDraft() {
  if (!state.productDraft?.config) return;
  if (!confirmDiscardChanges('打开已保存草稿')) return;
  state.config = prepareConfig(structuredClone(state.productDraft.config));
  state.externalSvg = null;
  state.editing = false;
  state.selectedScenario = 0;
  state.validation = state.productDraft.validation;
  state.dirty = false;
  state.draftOrigin = 'stored';
  await refreshPreview();
  requestAnimationFrame(fitZoom);
}

async function reimportDraft() {
  if (!state.product?.available) return;
  const label = `${state.product.id} ${state.product.name}`;
  const unsaved = state.dirty ? '当前浏览器内未保存的修改也会丢失。' : '';
  if (!window.confirm(`将归档“${label}”当前已保存的草稿JSON，并按最新资料库重建空子图。旧曲线不会自动迁移。${unsaved}是否继续？`)) return;
  const { body } = await request('/api/drafts/reimport', { method: 'POST', body: { id: state.product.id } });
  if (!body.ok) { state.validation = body.validation || { errors: [{ message: body.message || '无法重新导入情景。' }], warnings: [] }; updateUi(); return; }
  state.config = prepareConfig(body.config);
  state.externalSvg = null;
  state.editing = false;
  state.selectedScenario = 0;
  state.validation = body.validation;
  state.productDraft = { config: structuredClone(body.config), validation: body.validation };
  state.dirty = false;
  state.draftOrigin = 'stored';
  injectSvg(body.svg);
  updateUi();
}

async function saveDraft() {
  if (!ensureNumericControlsValid()) return;
  if (state.draftLookupPending) {
    setNotice(null, '正在检测现有草稿，请稍后再保存。');
    return;
  }
  if (state.draftOrigin === 'new' && state.productDraft && !window.confirm('该产品已有已保存草稿。保存此模板图将覆盖现有草稿，是否继续？')) return;
  const { body } = await request('/api/drafts/save', { method: 'POST', body: { config: state.config } });
  state.validation = body.validation;
  if (body.ok) {
    state.editing = false;
    state.dirty = false;
    state.draftOrigin = 'stored';
    state.productDraft = { config: structuredClone(state.config), validation: body.validation };
    injectSvg(body.svg);
  }
  updateUi();
}

async function publishDraft() {
  if (!ensureNumericControlsValid()) return;
  const first = await request('/api/publish', { method: 'POST', body: { config: state.config } });
  if (!first.body.ok || !first.body.confirmationRequired) { state.validation = first.body.validation; updateUi(); return; }
  if (!window.confirm(`${first.body.message}\n\n产品：${state.config.library.name}\n发布后会覆盖正式SVG。`)) return;
  const { body } = await request('/api/publish', { method: 'POST', body: { config: state.config, confirmed: true } });
  state.validation = body.validation;
  if (body.ok) {
    state.editing = false;
    state.dirty = false;
    state.draftOrigin = 'stored';
    state.productDraft = { config: structuredClone(state.config), validation: body.validation };
    injectSvg(body.svg);
  }
  updateUi();
}

function ensureCurve() {
  const curve = selectedGraphic().curve;
  if (!curve.points.length) {
    curve.points = [{ x: 0.06, y: 0.72 }, { x: 0.94, y: 0.28 }];
    markDirty();
  }
}

async function importExternalSvg(svg) {
  if (!confirmDiscardChanges('导入外部SVG')) return false;
  const { body } = await request('/api/external/check', { method: 'POST', body: { svg } });
  state.editing = false;
  if (body.editable) {
    state.config = prepareConfig(body.config);
    state.externalSvg = null;
    state.validation = body.validation;
    state.product = state.products.find((item) => item.id === state.config.library.id) || null;
    state.productDraft = { config: structuredClone(body.config), validation: body.validation };
    state.dirty = false;
    state.draftOrigin = 'stored';
    const rendered = await request('/api/render', { method: 'POST', body: { config: state.config, editing: false } });
    if (!rendered.body.ok) throw new Error(rendered.body.validation?.errors?.[0]?.message || '无法渲染原生SVG。');
    injectSvg(rendered.body.svg);
  } else {
    state.externalSvg = svg;
    state.config = null;
    state.validation = { errors: [{ message: body.reason }], warnings: [] };
    state.product = null;
    state.productDraft = null;
    state.dirty = false;
    state.draftOrigin = null;
    $('productSelect').value = '';
    injectReadOnlySvg(svg);
  }
  updateUi();
}

function wireControls() {
  $('productSelect').addEventListener('change', () => {
    const nextProduct = state.products.find((item) => item.id === $('productSelect').value) || null;
    if (nextProduct?.id !== state.product?.id && !confirmDiscardChanges('切换产品')) {
      $('productSelect').value = state.product?.id || '';
      return;
    }
    state.product = nextProduct;
    state.productDraft = null;
    state.draftLookupPending = false;
    clearDocument();
    updateUi();
    checkSelectedDraft().catch((error) => setNotice(null, error.message));
  });
  $('newTemplateButton').addEventListener('click', () => newTemplate().catch((error) => setNotice(null, error.message)));
  $('openDraftButton').addEventListener('click', () => openExistingDraft().catch((error) => setNotice(null, error.message)));
  $('reimportButton').addEventListener('click', () => reimportDraft().catch((error) => setNotice(null, error.message)));
  $('editButton').addEventListener('click', () => { state.editing = !state.editing; scheduleRender(0); updateUi(); });
  $('saveButton').addEventListener('click', () => saveDraft().catch((error) => setNotice(null, error.message)));
  $('publishButton').addEventListener('click', () => publishDraft().catch((error) => setNotice(null, error.message)));
  $('recheckButton').addEventListener('click', async () => {
    await loadProducts();
    if (!state.config) return;
    const { body } = await request('/api/render', { method: 'POST', body: { config: state.config, editing: state.editing } });
    state.validation = body.validation || { errors: [{ message: body.issue?.message || body.message || '资料库检测失败。' }], warnings: [] };
    if (body.ok) injectSvg(body.svg);
    updateUi();
  });
  $('zoomOutButton').addEventListener('click', () => setZoom(state.zoom - 0.01));
  $('zoomInButton').addEventListener('click', () => setZoom(state.zoom + 0.01));
  $('zoomInput').addEventListener('input', () => setZoomFromInput());
  $('zoomInput').addEventListener('change', () => setZoomFromInput({ format: true }));
  $('zoomInput').addEventListener('blur', () => setZoomFromInput({ format: true }));
  $('zoomFitButton').addEventListener('click', fitZoom);
  $('canvasViewport').addEventListener('wheel', (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setZoom(state.zoom + (event.deltaY < 0 ? 0.01 : -0.01));
  }, { passive: false });
  $('curveType').addEventListener('change', () => updateGraphic((graphic) => { graphic.curve.type = $('curveType').value; }));
  bindNumericInput($('strokeWidth'), (value) => updateGraphic((graphic) => { graphic.curve.strokeWidth = normaliseWidth(value); }));
  bindNumericInput($('axisLeft'), (value) => updateGraphic((graphic) => { graphic.axis.left = normaliseRatio(value / 100); }));
  bindNumericInput($('axisBottom'), (value) => updateGraphic((graphic) => { graphic.axis.bottom = normaliseRatio(value / 100); }));
  $('addCurveButton').addEventListener('click', () => { ensureCurve(); updateUi(); scheduleRender(0); });
  $('addNodeButton').addEventListener('click', () => {
    const curve = selectedGraphic().curve;
    ensureCurve();
    if (curve.points.length >= 12) return;
    const gaps = curve.points.slice(1).map((point, index) => ({ index, width: point.x - curve.points[index].x }));
    const widest = gaps.reduce((current, candidate) => candidate.width > current.width ? candidate : current, gaps[0]);
    if (!widest || widest.width < 0.0002) {
      setNotice(null, '没有足够的横向空间添加节点；请先拉开相邻节点。');
      return;
    }
    const left = curve.points[widest.index];
    const right = curve.points[widest.index + 1];
    curve.points.splice(widest.index + 1, 0, { x: normaliseRatio((left.x + right.x) / 2), y: normaliseRatio((left.y + right.y) / 2) });
    markDirty();
    updateUi();
    scheduleRender(0);
  });
  $('addGuideButton').addEventListener('click', () => {
    const position = readNumericInput($('guidePosition'), { format: true });
    if (position === null) return;
    updateGraphic((graphic) => {
      if (graphic.guides.length < 6) graphic.guides.push({ direction: $('guideDirection').value, position: normaliseRatio(position / 100), style: $('guideStyle').value });
    }, 0);
    updateUi();
  });
  $('addThresholdButton').addEventListener('click', () => {
    const position = readNumericInput($('thresholdPosition'), { format: true });
    if (position === null) return;
    updateGraphic((graphic) => {
      const token = $('thresholdToken').value;
      if (graphic.thresholds.length < 4 && !graphic.thresholds.some((item) => item.token === token)) graphic.thresholds.push({ token, x: normaliseRatio(position / 100) });
    }, 0);
    updateUi();
  });
  $('externalFile').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    try {
      if (file) await importExternalSvg(await file.text());
    } catch (error) {
      setNotice(null, error instanceof Error ? error.message : '导入SVG失败。');
    } finally {
      event.target.value = '';
    }
  });
  document.addEventListener('pointermove', dragMove);
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
  document.addEventListener('pointermove', movePanelResize);
  document.addEventListener('pointerup', endPanelResize);
  document.addEventListener('pointercancel', endPanelResize);
  document.addEventListener('dragover', (event) => event.preventDefault());
  document.addEventListener('drop', async (event) => {
    event.preventDefault();
    const file = [...event.dataTransfer.files].find((item) => item.name.toLowerCase().endsWith('.svg'));
    try {
      if (file) await importExternalSvg(await file.text());
    } catch (error) {
      setNotice(null, error instanceof Error ? error.message : '导入SVG失败。');
    }
  });
  window.addEventListener('resize', () => {
    setPanelWidth('library', panelWidth('library'));
    setPanelWidth('inspector', panelWidth('inspector'));
  });
  window.addEventListener('beforeunload', (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
}

wireControls();
wirePanelResizers();
updateUi();
loadProducts({ preserve: false }).catch((error) => setNotice(null, error.message));
