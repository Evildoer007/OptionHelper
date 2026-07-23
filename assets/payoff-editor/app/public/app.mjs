import {
  CURVE_ENDPOINTS,
  MAX_GUIDES,
  MAX_POINTS,
  MAX_THRESHOLDS,
  addGuide,
  addThreshold,
  autoFitScale,
  axisReference,
  axisUnit,
  blankCurvePoints,
  boundedPointX,
  clamp,
  guideExtent,
  guideMeta,
  normalizeAxis,
  ratioToX,
  ratioToY,
  resetAllGraphics as createResetGraphics,
  resetGraphic,
  round,
  scaleIssues,
  thresholdMeta,
  valueInScale,
  xToRatio,
  yToRatio,
} from './graphic-model.mjs';
import { appendFormulaContent } from './formula-renderer.mjs';

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
  canvasPan: null,
  spacePanActive: false,
  suppressCanvasClickUntil: 0,
  livePreviewFrame: null,
  panelResize: null,
  zoom: 0.72,
};

const $ = (id) => document.getElementById(id);
const normaliseWidth = (value) => round(clamp(value, 0.1, 20), 2);
const displaySection = (value) => String(value || '').replace(/^#{1,6}\s*/, '');
const tokenOptions = [
  'S₀', 'K', 'K₁', 'K₂', 'K₃', 'K₄', 'K_p', 'K_c', 'K_u', 'K_d',
  'H', 'H_in', 'H_{in,2}', 'H_out', 'H_{out,2}', 'H_{out,t}', 'H_c', 'H_buffer', 'H_floor', 'H_reset', 'H_u', 'H_d',
];

function currentAxis() {
  return normalizeAxis(state.config?.library?.axis);
}
const numericSpecs = Object.freeze({
  value: { min: -1_000_000, max: 1_000_000, decimals: 2, unit: '' },
  width: { min: 0.1, max: 20, decimals: 2, unit: 'px' },
});
const chartLayout = Object.freeze({ left: 42, right: 684, top: 70, bottom: 426 });

function guideExtentRange(scale, direction) {
  return direction === 'horizontal' ? [scale.xMin, scale.xMax] : [scale.yMin, scale.yMax];
}

function readGuideExtentValue(input, scale, direction) {
  const value = readNumericInput(input);
  if (value === null) return null;
  const [min, max] = guideExtentRange(scale, direction);
  if (!valueInScale(value, min, max)) {
    input.setCustomValidity(`请输入${formatNumber(min)}至${formatNumber(max)}之间的数值。`);
    input.setAttribute('aria-invalid', 'true');
    return null;
  }
  input.setCustomValidity('');
  input.removeAttribute('aria-invalid');
  return value;
}

function applyGuideExtentDefaults(scale, direction, { reset = false } = {}) {
  const [start, end] = guideExtentRange(scale, direction);
  if (reset || !$('guideStart').value.trim()) $('guideStart').value = formatNumber(start);
  if (reset || !$('guideEnd').value.trim()) $('guideEnd').value = formatNumber(end);
}

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
    const item = element('li', '', active && index === state.selectedScenario ? 'selected' : 'preview');
    item.append(element('span', scenario.title, 'scenario-title'));
    if (scenario.condition) {
      const condition = element('span', '', 'scenario-condition');
      appendFormulaContent(condition, scenario.condition);
      item.append(condition);
    }
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
  const decimalPattern = new RegExp(`^-?\\d+(?:\\.\\d{0,${spec.decimals}})?$`);
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
  const invalid = [...document.querySelectorAll('[data-numeric]:not([data-optional]):not(:disabled)')].find((input) => readNumericInput(input) === null);
  if (!invalid) return true;
  invalid.focus();
  setNotice(null, invalid.validationMessage || '请修正图形控制中的数值。');
  return false;
}

function readControlValue(input, scale, direction) {
  const value = readNumericInput(input);
  if (value === null) return null;
  const [min, max] = direction === 'horizontal' ? [scale.yMin, scale.yMax] : [scale.xMin, scale.xMax];
  if (!valueInScale(value, min, max)) {
    input.setCustomValidity(`请输入${formatNumber(min)}至${formatNumber(max)}之间的数值。`);
    input.setAttribute('aria-invalid', 'true');
    return null;
  }
  input.setCustomValidity('');
  input.removeAttribute('aria-invalid');
  return value;
}

function scaleInputs() {
  return ['scaleXMin', 'scaleXMax', 'scaleYMin', 'scaleYMax'].map((id) => $(id));
}

function readScaleInputs({ format = false } = {}) {
  const values = {};
  for (const input of scaleInputs()) {
    const value = readNumericInput(input, { format });
    if (value === null) return null;
    values[input.dataset.scale] = value;
  }
  const issues = scaleIssues(values, currentAxis());
  if (issues.length) {
    const input = scaleInputs().find((item) => item.dataset.scale.endsWith('Min')) || scaleInputs()[0];
    input.setCustomValidity(issues[0]);
    input.setAttribute('aria-invalid', 'true');
    return null;
  }
  scaleInputs().forEach((input) => { input.setCustomValidity(''); input.removeAttribute('aria-invalid'); });
  return values;
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
    target.innerHTML = `<div class="parameter-empty">尚未设置曲线节点。点击“生成起点”后填写${currentAxis().label}和净损益。</div>`;
    return;
  }
  const endpointOptions = (selected) => CURVE_ENDPOINTS.map((value) => {
    const label = ({ none: '—', open: '○', closed: '●' })[value];
    return `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`;
  }).join('');
  target.innerHTML = `<div class="point-head"><span>节点</span><span>${currentAxis().unit ? '价格%' : '变量值'}</span><span>净损益</span><span>断</span><span>端</span><span></span></div>${points.map((point, index) => `<div class="point-row"><span>${String(index + 1).padStart(2, '0')}</span><input data-point-x="${index}" data-numeric="value" type="number" step="0.01" value="${formatNumber(point.x)}" ${enabled ? '' : 'disabled'} /><input data-point-y="${index}" data-numeric="value" type="number" step="0.01" value="${formatNumber(point.y)}" ${enabled ? '' : 'disabled'} /><label class="point-break" title="从此节点断开前一段曲线"><input data-point-break="${index}" type="checkbox" ${point.breakBefore ? 'checked' : ''} ${enabled && index > 0 ? '' : 'disabled'} /><span>断</span></label><select class="point-endpoint" data-point-endpoint="${index}" title="端点样式：—无标记、○不含、●包含" ${enabled ? '' : 'disabled'}>${endpointOptions(point.endpoint || 'none')}</select><button class="row-delete" data-delete-point="${index}" title="删除节点" ${enabled && points.length > 2 ? '' : 'disabled'}>×</button></div>`).join('')}<p class="point-hint">断：从该点重新起笔。端：○不含，●包含；同价跳变请在后一节点勾选断。</p>`;
  target.querySelectorAll('[data-point-x], [data-point-y]').forEach((input) => bindNumericInput(input, (value) => {
    const index = Number(input.dataset.pointX ?? input.dataset.pointY);
    setPoint(index, input.dataset.pointX === undefined ? 'y' : 'x', value);
  }));
  target.querySelectorAll('[data-point-break]').forEach((input) => input.addEventListener('change', () => {
    updateGraphic((graphic) => {
      const point = graphic.curve.points[Number(input.dataset.pointBreak)];
      const next = input.checked;
      if (point.breakBefore === next) return { changed: false };
      if (next) point.breakBefore = true;
      else delete point.breakBefore;
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
  target.querySelectorAll('[data-point-endpoint]').forEach((select) => select.addEventListener('change', () => {
    updateGraphic((graphic) => {
      const point = graphic.curve.points[Number(select.dataset.pointEndpoint)];
      const next = select.value;
      if ((point.endpoint || 'none') === next) return { changed: false };
      if (next === 'none') delete point.endpoint;
      else point.endpoint = next;
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
  target.querySelectorAll('[data-delete-point]').forEach((button) => button.addEventListener('click', () => {
    const pointsForGraphic = selectedGraphic().curve.points;
    if (pointsForGraphic.length <= 2) return;
    updateGraphic((graphic) => {
      graphic.curve.points.splice(Number(button.dataset.deletePoint), 1);
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
}

function guideOptions(selected) {
  return `<option value="horizontal" ${selected === 'horizontal' ? 'selected' : ''}>横线</option><option value="vertical" ${selected === 'vertical' ? 'selected' : ''}>纵线</option>`;
}

function renderGuideList(enabled) {
  const guides = selectedGraphic()?.guides || [];
  $('guideList').innerHTML = guides.length ? guides.map((guide, index) => {
    const extent = guideExtent(guide, selectedGraphic().scale);
    const valueLabel = guide.direction === 'horizontal' ? 'Y位置' : 'X位置';
    const startLabel = guide.direction === 'horizontal' ? 'Xmin' : 'Ymin';
    const endLabel = guide.direction === 'horizontal' ? 'Xmax' : 'Ymax';
    const valueUnit = guide.direction === 'vertical' ? '%' : '';
    const extentUnit = guide.direction === 'horizontal' ? '%' : '';
    return `<div class="line-row guide-row"><select data-guide-direction="${index}" ${enabled ? '' : 'disabled'}>${guideOptions(guide.direction)}</select><label class="guide-row-control">${valueLabel}<input data-guide-field="${index}" data-guide-property="value" data-numeric="value" type="number" step="0.01" value="${formatNumber(guide.value)}" ${enabled ? '' : 'disabled'} /><em>${valueUnit}</em></label><label class="guide-row-control">${startLabel}<input data-guide-field="${index}" data-guide-property="start" data-numeric="value" type="number" step="0.01" value="${formatNumber(extent.start)}" ${enabled ? '' : 'disabled'} /><em>${extentUnit}</em></label><label class="guide-row-control">${endLabel}<input data-guide-field="${index}" data-guide-property="end" data-numeric="value" type="number" step="0.01" value="${formatNumber(extent.end)}" ${enabled ? '' : 'disabled'} /><em>${extentUnit}</em></label><button class="row-delete" data-delete-guide="${index}" title="删除局部辅助线" ${enabled ? '' : 'disabled'}>×</button></div>`;
  }).join('') : '<div class="parameter-empty">未添加局部辅助线。</div>';
  $('guideList').querySelectorAll('[data-guide-field]').forEach((input) => bindNumericInput(input, (value) => updateGraphic((graphic) => {
    const guide = graphic.guides[Number(input.dataset.guideField)];
    const property = input.dataset.guideProperty;
    const range = property === 'value'
      ? (guide.direction === 'horizontal' ? [graphic.scale.yMin, graphic.scale.yMax] : [graphic.scale.xMin, graphic.scale.xMax])
      : guideExtentRange(graphic.scale, guide.direction);
    if (!valueInScale(value, range[0], range[1])) return { changed: false, message: '局部辅助线数值必须落在当前对应坐标范围内。' };
    const extent = guideExtent(guide, graphic.scale);
    const nextStart = property === 'start' ? round(value) : extent.start;
    const nextEnd = property === 'end' ? round(value) : extent.end;
    if (property !== 'value' && nextStart >= nextEnd) return { changed: false, message: '局部辅助线起点必须小于终点。' };
    const next = round(value);
    if (guide[property] === next) return { changed: false };
    guide[property] = next;
    return { changed: true };
  })));
  $('guideList').querySelectorAll('[data-guide-direction]').forEach((select) => select.addEventListener('change', () => {
    updateGraphic((graphic) => {
      const guide = graphic.guides[Number(select.dataset.guideDirection)];
      const [min, max] = select.value === 'horizontal' ? [graphic.scale.yMin, graphic.scale.yMax] : [graphic.scale.xMin, graphic.scale.xMax];
      if (!valueInScale(guide.value, min, max)) return { changed: false, message: `切换后数值必须落在${select.value === 'horizontal' ? 'Y轴' : 'X轴'}范围内。` };
      if (guide.direction === select.value) return { changed: false };
      guide.direction = select.value;
      const [start, end] = guideExtentRange(graphic.scale, select.value);
      guide.start = round(start);
      guide.end = round(end);
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
  $('guideList').querySelectorAll('[data-delete-guide]').forEach((button) => button.addEventListener('click', () => {
    updateGraphic((graphic) => {
      graphic.guides.splice(Number(button.dataset.deleteGuide), 1);
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
}

function thresholdOptions(selected, thresholds, currentIndex) {
  return tokenOptions.map((token) => {
    const meta = thresholdMeta(token);
    return `<option value="${token}" ${token === selected ? 'selected' : ''} ${thresholds.some((threshold, index) => index !== currentIndex && threshold.token === token) ? 'disabled' : ''}>${meta.label} ${token}</option>`;
  }).join('');
}

function renderThresholdAddOptions() {
  const select = $('thresholdToken');
  const used = new Set(selectedGraphic()?.thresholds?.map((item) => item.token) || []);
  const current = select.value;
  select.replaceChildren(...tokenOptions.map((token) => {
    const option = new Option(`${thresholdMeta(token).label} ${token}`, token);
    option.disabled = used.has(token);
    return option;
  }));
  const available = [...select.options].find((option) => !option.disabled);
  if ([...select.options].some((option) => option.value === current && !option.disabled)) select.value = current;
  else if (available) select.value = available.value;
}

function renderThresholdList(enabled) {
  const thresholds = selectedGraphic()?.thresholds || [];
  const axis = currentAxis();
  $('thresholdList').innerHTML = thresholds.length ? thresholds.map((threshold, index) => {
    const meta = thresholdMeta(threshold.token);
    return `<div class="line-row threshold-row"><span class="line-swatch" style="background:${meta.color}" title="${meta.label}"></span><select data-threshold-token="${index}" ${enabled ? '' : 'disabled'}>${thresholdOptions(threshold.token, thresholds, index)}</select><input data-threshold-value="${index}" data-numeric="value" type="number" step="0.01" value="${formatNumber(threshold.value)}" ${enabled ? '' : 'disabled'} /><span class="line-unit">${axis.unit}</span><button class="row-delete" data-delete-threshold="${index}" title="删除关键价格线" ${enabled ? '' : 'disabled'}>×</button></div>`;
  }).join('') : '<div class="parameter-empty">未添加关键价格线。</div>';
  $('thresholdList').querySelectorAll('[data-threshold-value]').forEach((input) => bindNumericInput(input, (value) => updateGraphic((graphic) => {
    if (!valueInScale(value, graphic.scale.xMin, graphic.scale.xMax)) return { changed: false, message: '关键价格必须落在当前X轴范围内。' };
    const threshold = graphic.thresholds[Number(input.dataset.thresholdValue)];
    if (threshold.value === round(value)) return { changed: false };
    threshold.value = round(value);
    return { changed: true };
  })));
  $('thresholdList').querySelectorAll('[data-threshold-token]').forEach((select) => select.addEventListener('change', () => {
    const index = Number(select.dataset.thresholdToken);
    if (selectedGraphic().thresholds.some((threshold, thresholdIndex) => thresholdIndex !== index && threshold.token === select.value)) return;
    updateGraphic((graphic) => {
      if (graphic.thresholds[index].token === select.value) return { changed: false };
      graphic.thresholds[index].token = select.value;
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
  $('thresholdList').querySelectorAll('[data-delete-threshold]').forEach((button) => button.addEventListener('click', () => {
    updateGraphic((graphic) => {
      graphic.thresholds.splice(Number(button.dataset.deleteThreshold), 1);
      return { changed: true };
    }, { structural: true });
    updateUi();
  }));
}

function setEditingControls() {
  const graphic = selectedGraphic();
  const enabled = editable() && Boolean(graphic);
  const controls = ['curveType', 'strokeWidth', 'scaleXMin', 'scaleXMax', 'scaleYMin', 'scaleYMax', 'autoScaleButton', 'addCurveButton', 'addNodeButton', 'guideDirection', 'guideValue', 'guideStart', 'guideEnd', 'addGuideButton', 'thresholdToken', 'thresholdValue', 'addThresholdButton', 'resetScenarioButton', 'resetAllButton'];
  controls.forEach((id) => { $(id).disabled = !enabled; });
  $('editButton').disabled = !state.config || Boolean(state.externalSvg) || state.validation?.errors?.length > 0;
  $('editButton').textContent = state.editing ? '退出编辑' : '进入编辑';
  $('saveButton').disabled = !state.config || Boolean(state.externalSvg) || state.validation?.errors?.length > 0;
  $('publishButton').disabled = !state.config || Boolean(state.externalSvg);
  if (!graphic) {
    $('guideActionHint').textContent = '';
    $('thresholdActionHint').textContent = '';
    renderPointTable(false);
    renderGuideList(false);
    renderThresholdList(false);
    return;
  }
  $('curveType').value = graphic.curve.type;
  $('strokeWidth').value = formatNumber(graphic.curve.strokeWidth);
  $('scaleXMin').value = formatNumber(graphic.scale.xMin);
  $('scaleXMax').value = formatNumber(graphic.scale.xMax);
  $('scaleYMin').value = formatNumber(graphic.scale.yMin);
  $('scaleYMax').value = formatNumber(graphic.scale.yMax);
  const axis = currentAxis();
  document.querySelectorAll('[data-axis-unit]').forEach((element) => { element.textContent = axis.unit; });
  $('axisRule').textContent = `纵轴固定在${axisReference(axis)}${axis.unit}，横轴固定在Y=0；X轴范围须严格覆盖该参考值。`;
  const guideIsVertical = $('guideDirection').value === 'vertical';
  $('guideValueLabel').textContent = guideIsVertical ? 'X位置' : 'Y位置';
  $('guideValueUnit').textContent = guideIsVertical ? axis.unit : '';
  $('guideValue').placeholder = guideIsVertical ? axis.label : '净损益';
  $('guideStartLabel').textContent = guideIsVertical ? 'Y起点' : 'X起点';
  $('guideEndLabel').textContent = guideIsVertical ? 'Y终点' : 'X终点';
  $('guideStartUnit').textContent = guideIsVertical ? '' : axis.unit;
  $('guideEndUnit').textContent = guideIsVertical ? '' : axis.unit;
  applyGuideExtentDefaults(graphic.scale, $('guideDirection').value);
  $('addNodeButton').disabled = !enabled || graphic.curve.points.length >= MAX_POINTS;
  const guideDirection = $('guideDirection').value;
  const guideValue = readControlValue($('guideValue'), graphic.scale, guideDirection);
  const guideStart = readGuideExtentValue($('guideStart'), graphic.scale, guideDirection);
  const guideEnd = readGuideExtentValue($('guideEnd'), graphic.scale, guideDirection);
  const guideReason = !enabled
    ? '进入编辑模式后可添加辅助线。'
    : graphic.guides.length >= MAX_GUIDES
      ? `已达每图${MAX_GUIDES}条辅助线上限。`
      : guideValue === null
        ? `请输入当前${guideDirection === 'horizontal' ? 'Y轴净损益' : axis.label}范围内的数值。`
        : guideStart === null || guideEnd === null
          ? `请输入当前${guideDirection === 'horizontal' ? 'X轴' : 'Y轴'}范围内的起点和终点。`
          : guideStart >= guideEnd
            ? '局部辅助线起点必须小于终点。'
        : '';
  $('addGuideButton').disabled = Boolean(guideReason);
  $('guideActionHint').textContent = guideReason;
  renderThresholdAddOptions();
  const thresholdTokenAvailable = [...$('thresholdToken').options].some((option) => !option.disabled);
  $('thresholdToken').disabled = !enabled || !thresholdTokenAvailable;
  $('thresholdValue').disabled = !enabled;
  const thresholdValue = readControlValue($('thresholdValue'), graphic.scale, 'vertical');
  const thresholdReason = !enabled
    ? '进入编辑模式后可添加关键价格线。'
    : graphic.thresholds.length >= MAX_THRESHOLDS
      ? `已达每图${MAX_THRESHOLDS}条关键价格线上限。`
      : !thresholdTokenAvailable
        ? '当前情景没有可用的标准关键标记。'
        : thresholdValue === null
          ? `请输入当前${axis.label}范围内的数值。`
          : '';
  $('addThresholdButton').disabled = Boolean(thresholdReason);
  $('thresholdActionHint').textContent = thresholdReason;
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
  const sourceDetail = $('sourceDetail');
  if (!source) sourceDetail.textContent = '选择产品后显示资料库来源。';
  else {
    sourceDetail.replaceChildren(document.createTextNode(`来源：${displaySection(state.config.library.section)} · “情景”列。标题、条件和净损益文字均锁定。`));
    if (source.condition) {
      sourceDetail.append(document.createTextNode(' 条件：'));
      appendFormulaContent(sourceDetail, source.condition);
    }
  }
  renderBinding();
  renderScenarioList();
  setEditingControls();
  setNotice(state.validation);
  applyZoom();
}

function applyZoom() {
  const stage = $('canvasStage');
  if (stage) stage.style.setProperty('--zoom', state.zoom.toFixed(2));
  $('zoomInput').value = String(Math.round(state.zoom * 100));
}

function zoomAnchor(anchor = null) {
  const viewport = $('canvasViewport');
  const rect = viewport.getBoundingClientRect();
  if (anchor) return { x: anchor.clientX - rect.left, y: anchor.clientY - rect.top };
  return { x: viewport.clientWidth / 2, y: viewport.clientHeight / 2 };
}

function setZoom(value, anchor = null) {
  const next = clamp(round(value, 2), 0.01, 4);
  if (next === state.zoom) return;
  const viewport = $('canvasViewport');
  const point = zoomAnchor(anchor);
  const sourceX = (viewport.scrollLeft + point.x) / state.zoom;
  const sourceY = (viewport.scrollTop + point.y) / state.zoom;
  state.zoom = next;
  applyZoom();
  viewport.scrollLeft = Math.max(0, sourceX * next - point.x);
  viewport.scrollTop = Math.max(0, sourceY * next - point.y);
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
    if (!state.dragging && performance.now() >= state.suppressCanvasClickUntil) selectScenario(Number(card.dataset.scenarioCard));
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
  if (body.ok) {
    state.config = body.config || state.config;
    injectSvg(body.svg);
  }
  updateUi();
}

function scheduleRender(wait = 140) {
  state.renderVersion += 1;
  clearTimeout(state.renderTimer);
  state.renderTimer = setTimeout(() => refreshPreview().catch((error) => setNotice(null, error.message)), wait);
}

function chartGeometry(card, scale, axis = currentAxis()) {
  const cardX = Number(card.dataset.cardX);
  const cardY = Number(card.dataset.cardY);
  const left = cardX + chartLayout.left;
  const right = cardX + chartLayout.right;
  const top = cardY + chartLayout.top;
  const bottom = cardY + chartLayout.bottom;
  const width = right - left;
  const height = bottom - top;
  return {
    left,
    right,
    top,
    bottom,
    width,
    height,
    yAxisX: left + xToRatio(axisReference(axis), scale) * width,
    xAxisY: top + yToRatio(0, scale) * height,
  };
}

function pointToSvg(point, geometry, scale) {
  return { x: geometry.left + xToRatio(point.x, scale) * geometry.width, y: geometry.top + yToRatio(point.y, scale) * geometry.height };
}

function clientCurvePath(points, type, geometry, scale) {
  if (!points.length) return '';
  const converted = points.map((point) => pointToSvg(point, geometry, scale));
  let path = `M${converted[0].x.toFixed(2)} ${converted[0].y.toFixed(2)}`;
  for (let index = 1; index < converted.length; index += 1) {
    if (points[index].breakBefore) path += ` M${converted[index].x.toFixed(2)} ${converted[index].y.toFixed(2)}`;
    else path += type === 'step' ? ` H${converted[index].x.toFixed(2)} V${converted[index].y.toFixed(2)}` : ` L${converted[index].x.toFixed(2)} ${converted[index].y.toFixed(2)}`;
  }
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
  const geometry = chartGeometry(card, graphic.scale, currentAxis());
  const curve = card.querySelector('[data-element="curve"]');
  setAttrs(curve, { d: clientCurvePath(graphic.curve.points, graphic.curve.type, geometry, graphic.scale), 'stroke-width': graphic.curve.strokeWidth });
  graphic.curve.points.forEach((point, index) => {
    const converted = pointToSvg(point, geometry, graphic.scale);
    setAttrs(card.querySelector(`[data-role="node"][data-point="${index}"]`), { cx: converted.x, cy: converted.y });
    setAttrs(card.querySelector(`[data-element="curve-endpoint"][data-point="${index}"]`), { cx: converted.x, cy: converted.y });
  });
  graphic.thresholds.forEach((threshold, index) => {
    const group = card.querySelector(`[data-element="threshold"][data-index="${index}"]`);
    const x = geometry.left + xToRatio(threshold.value, graphic.scale) * geometry.width;
    setAttrs(group?.querySelector('line'), { x1: x, y1: geometry.top, x2: x, y2: geometry.bottom });
    setAttrs(group?.querySelector('[data-role="threshold"]'), { cx: x, cy: geometry.bottom });
  });
  graphic.guides.forEach((guide, index) => {
    const group = card.querySelector(`[data-element="guide"][data-index="${index}"]`);
    const horizontal = guide.direction === 'horizontal';
    const position = horizontal ? geometry.top + yToRatio(guide.value, graphic.scale) * geometry.height : geometry.left + xToRatio(guide.value, graphic.scale) * geometry.width;
    const extent = guideExtent(guide, graphic.scale);
    const start = horizontal
      ? geometry.left + xToRatio(extent.start, graphic.scale) * geometry.width
      : geometry.top + yToRatio(extent.start, graphic.scale) * geometry.height;
    const end = horizontal
      ? geometry.left + xToRatio(extent.end, graphic.scale) * geometry.width
      : geometry.top + yToRatio(extent.end, graphic.scale) * geometry.height;
    setAttrs(group?.querySelector('line'), horizontal ? { x1: start, y1: position, x2: end, y2: position } : { x1: position, y1: start, x2: position, y2: end });
    setAttrs(group?.querySelector('[data-role="guide"]'), { cx: horizontal ? (start + end) / 2 : position, cy: horizontal ? position : (start + end) / 2 });
    setAttrs(group?.querySelector('[data-role="guide-start"]'), { cx: horizontal ? start : position, cy: horizontal ? position : start });
    setAttrs(group?.querySelector('[data-role="guide-end"]'), { cx: horizontal ? end : position, cy: horizontal ? position : end });
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
  if (event.button !== 0 || state.spacePanActive) return;
  event.preventDefault();
  event.stopPropagation();
  state.selectedScenario = Number(handle.dataset.scenario);
  state.renderVersion += 1;
  state.dragging = { svg, role: handle.dataset.role, data: handle.dataset, changed: false };
  handle.setPointerCapture?.(event.pointerId);
}

function dragMove(event) {
  if (!state.dragging) return;
  const { svg, role, data } = state.dragging;
  const graphic = selectedGraphic();
  const card = svg.querySelector(`[data-scenario-card="${state.selectedScenario}"]`);
  if (!graphic || !card) return;
  const point = svgPoint(event, svg);
  const geometry = chartGeometry(card, graphic.scale, currentAxis());
  let changed = false;
  if (role === 'node') {
    const index = Number(data.point);
    const nextX = boundedPointX(ratioToX((point.x - geometry.left) / geometry.width, graphic.scale), graphic.curve.points, index, graphic.curve.type, graphic.scale);
    const nextY = ratioToY((point.y - geometry.top) / geometry.height, graphic.scale);
    changed = nextX !== graphic.curve.points[index].x || nextY !== graphic.curve.points[index].y;
    graphic.curve.points[index].x = nextX;
    graphic.curve.points[index].y = nextY;
  } else if (role === 'threshold') {
    const threshold = graphic.thresholds[Number(data.threshold)];
    const nextValue = ratioToX((point.x - geometry.left) / geometry.width, graphic.scale);
    changed = nextValue !== threshold.value;
    threshold.value = nextValue;
  } else if (role === 'guide') {
    const guide = graphic.guides[Number(data.guide)];
    const nextValue = guide.direction === 'horizontal'
      ? ratioToY((point.y - geometry.top) / geometry.height, graphic.scale)
      : ratioToX((point.x - geometry.left) / geometry.width, graphic.scale);
    changed = nextValue !== guide.value;
    guide.value = nextValue;
  } else if (role === 'guide-start' || role === 'guide-end') {
    const guide = graphic.guides[Number(data.guide)];
    const extent = guideExtent(guide, graphic.scale);
    const candidate = guide.direction === 'horizontal'
      ? ratioToX((point.x - geometry.left) / geometry.width, graphic.scale)
      : ratioToY((point.y - geometry.top) / geometry.height, graphic.scale);
    const [min, max] = guideExtentRange(graphic.scale, guide.direction);
    const next = role === 'guide-start'
      ? round(clamp(candidate, min, extent.end - 0.01))
      : round(clamp(candidate, extent.start + 0.01, max));
    const property = role === 'guide-start' ? 'start' : 'end';
    changed = guide[property] !== next;
    guide.start = extent.start;
    guide.end = extent.end;
    guide[property] = next;
  }
  if (!changed) return;
  state.dragging.changed = true;
  markDirty();
  queueLivePreview();
}

function endDrag() {
  if (!state.dragging) return;
  const changed = state.dragging.changed;
  state.dragging = null;
  if (!changed) return;
  flushLivePreview();
  updateUi();
  scheduleRender(0);
}

function beginCanvasPan(event) {
  const isMiddleButton = event.button === 1;
  const isSpaceDrag = event.button === 0 && state.spacePanActive;
  if (!isMiddleButton && !isSpaceDrag) return;
  if (!$('canvasStage') || state.dragging || state.panelResize) return;
  event.preventDefault();
  const viewport = $('canvasViewport');
  state.canvasPan = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    startLeft: viewport.scrollLeft,
    startTop: viewport.scrollTop,
    moved: false,
  };
  viewport.classList.add('is-panning');
  viewport.setPointerCapture?.(event.pointerId);
}

function moveCanvasPan(event) {
  const pan = state.canvasPan;
  if (!pan || event.pointerId !== pan.pointerId) return;
  const viewport = $('canvasViewport');
  const deltaX = event.clientX - pan.startX;
  const deltaY = event.clientY - pan.startY;
  if (Math.abs(deltaX) > 2 || Math.abs(deltaY) > 2) pan.moved = true;
  viewport.scrollLeft = Math.max(0, pan.startLeft - deltaX);
  viewport.scrollTop = Math.max(0, pan.startTop - deltaY);
}

function endCanvasPan(event) {
  const pan = state.canvasPan;
  if (!pan || (event?.pointerId !== undefined && event.pointerId !== pan.pointerId)) return;
  $('canvasViewport').classList.remove('is-panning');
  state.canvasPan = null;
  if (pan.moved) state.suppressCanvasClickUntil = performance.now() + 250;
}

function setSpacePan(active) {
  state.spacePanActive = active;
  $('canvasViewport').classList.toggle('pan-ready', active);
}

function isTextInput(target) {
  return target instanceof HTMLElement && (target.matches('input, textarea, select') || target.isContentEditable);
}

function updateGraphic(mutator, { wait = 180, structural = false } = {}) {
  const graphic = selectedGraphic();
  if (!graphic || !editable()) return { changed: false };
  const result = mutator(graphic) || { changed: true };
  if (!result.changed) {
    if (result.message) setNotice(null, result.message);
    return result;
  }
  markDirty();
  if (!structural) queueLivePreview();
  scheduleRender(structural ? 0 : wait);
  return result;
}

function setPoint(index, field, value) {
  updateGraphic((graphic) => {
    const points = graphic.curve.points;
    const next = field === 'x'
      ? boundedPointX(value, points, index, graphic.curve.type, graphic.scale)
      : round(clamp(value, graphic.scale.yMin, graphic.scale.yMax));
    if (points[index][field] === next) return { changed: false };
    points[index][field] = next;
    return { changed: true };
  });
}

function autoFitCurrentGraphic() {
  updateGraphic((graphic) => {
    const nextScale = autoFitScale(graphic, currentAxis());
    if (Object.keys(nextScale).every((key) => graphic.scale[key] === nextScale[key])) return { changed: false, message: '当前坐标范围已适合图形。' };
    graphic.scale = nextScale;
    return { changed: true };
  }, { structural: true });
  updateUi();
}

function panelWidth(side) {
  const variable = side === 'library' ? '--library-width' : '--inspector-width';
  return Number.parseFloat(getComputedStyle($('workbench')).getPropertyValue(variable)) || (side === 'library' ? 267 : 390);
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
  state.config = body.config;
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
  state.config = structuredClone(state.productDraft.config);
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
  state.config = body.config;
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
    state.config = body.config || state.config;
    state.editing = false;
    state.dirty = false;
    state.draftOrigin = 'stored';
    state.productDraft = { config: structuredClone(state.config), validation: body.validation };
    injectSvg(body.svg);
  }
  updateUi();
}

async function publishDraft() {
  const first = await request('/api/publish', { method: 'POST', body: { config: state.config } });
  if (!first.body.ok || !first.body.confirmationRequired) { state.validation = first.body.validation; updateUi(); return; }
  if (first.body.config) state.config = first.body.config;
  state.validation = first.body.review?.validation || state.validation;
  renderPublishReview(first.body.review);
  updateUi();
}

function reviewFormalLabel(formal) {
  if (formal?.kind === 'replace-external') return '将覆盖非原生SVG';
  if (formal?.kind === 'replace') return '将覆盖并归档旧SVG';
  return '将新建正式SVG';
}

function reviewScenarioLabel(item) {
  if (item.state === 'changed') return '已改';
  if (item.state === 'unchanged') return '不变';
  return '新增';
}

function renderPublishReview(review) {
  if (!review) return;
  const { summary = {}, scenarios = [], validation = {}, formal = {} } = review;
  const product = state.config?.library?.name || '当前产品';
  $('publishReviewCopy').textContent = `${product}共有${summary.scenarios || scenarios.length}个资料库情景。${reviewFormalLabel(formal)}。`;
  const reviewSummary = $('publishReviewSummary');
  const changeCount = (summary.changed || 0) + (summary.added || 0);
  const cells = [
    { value: `${summary.complete || 0}/${summary.scenarios || scenarios.length}`, label: '已绘制情景' },
    { value: String(summary.pending || 0), label: '待绘制情景' },
    { value: String(changeCount), label: '将写入改动' },
  ].map((item) => {
    const node = element('div');
    node.append(element('strong', item.value), element('span', item.label));
    return node;
  });
  reviewSummary.replaceChildren(...cells);
  const reviewList = $('publishReviewList');
  reviewList.replaceChildren(...scenarios.map((item) => {
    const row = element('div', '', `publish-review-row${item.curve === 'pending' ? ' pending' : ''}`);
    row.append(
      element('strong', String(item.index).padStart(2, '0')),
      element('span', item.title),
      element('small', `${item.curve === 'complete' ? `曲线${item.points}点` : '待绘制'}，关键价${item.thresholds}，辅助线${item.guides}，${reviewScenarioLabel(item)}`),
    );
    return row;
  }));
  const notes = [];
  if (summary.pending) notes.push(`有${summary.pending}个情景尚未绘制，仍会按当前规则发布为“待绘制”。`);
  if (validation.errors?.length) notes.push(`校验提示：${validation.errors[0].message}`);
  else if (validation.warnings?.length) notes.push(`校验提示：${validation.warnings[0].message}`);
  else notes.push('情景绑定和图形数值已通过校验。');
  $('publishReviewNote').textContent = notes.join(' ');
  const dialog = $('publishReviewDialog');
  if (!dialog.open) dialog.showModal();
}

function closePublishReview() {
  const dialog = $('publishReviewDialog');
  if (dialog.open) dialog.close();
}

async function confirmPublishReview() {
  const button = $('publishReviewConfirm');
  if (!state.config) return;
  button.disabled = true;
  try {
    const { body } = await request('/api/publish', { method: 'POST', body: { config: state.config, confirmed: true } });
    state.validation = body.validation;
    if (body.ok) {
      state.config = body.config || state.config;
      state.editing = false;
      state.dirty = false;
      state.draftOrigin = 'stored';
      state.productDraft = { config: structuredClone(state.config), validation: body.validation };
      injectSvg(body.svg);
      closePublishReview();
    }
    updateUi();
  } finally {
    button.disabled = false;
  }
}

function resetCurrentScenario() {
  const source = selectedSource();
  if (!editable() || !source) return;
  const label = `情景${state.selectedScenario + 1}：${source.title}`;
  if (!window.confirm(`确定重置“${label}”吗？该情景的曲线、坐标范围、关键价格线和辅助线将恢复为默认待绘制状态。`)) return;
  const blank = resetGraphic(source.id);
  if (JSON.stringify(state.config.graphics[state.selectedScenario]) === JSON.stringify(blank)) {
    setNotice(null, '当前情景已是默认待绘制状态。');
    return;
  }
  state.config.graphics[state.selectedScenario] = blank;
  markDirty();
  updateUi();
  scheduleRender(0);
}

function resetAllGraphics() {
  if (!editable()) return;
  const count = state.config.library.scenarios.length;
  const label = `${state.config.library.id} ${state.config.library.name}`;
  if (!window.confirm(`确定重置“${label}”整张图吗？全部${count}个情景的曲线、坐标范围、关键价格线和辅助线将恢复为默认待绘制状态。`)) return;
  const blankGraphics = createResetGraphics(state.config.library.scenarios.map((scenario) => scenario.id));
  if (JSON.stringify(state.config.graphics) === JSON.stringify(blankGraphics)) {
    setNotice(null, '整张图已是默认待绘制状态。');
    return;
  }
  state.config.graphics = blankGraphics;
  markDirty();
  updateUi();
  scheduleRender(0);
}

async function importExternalSvg(svg) {
  if (!confirmDiscardChanges('导入外部SVG')) return false;
  const { body } = await request('/api/external/check', { method: 'POST', body: { svg } });
  state.editing = false;
  if (body.editable) {
    state.config = body.config;
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
  $('publishReviewCancel').addEventListener('click', closePublishReview);
  $('publishReviewBack').addEventListener('click', closePublishReview);
  $('publishReviewConfirm').addEventListener('click', () => confirmPublishReview().catch((error) => setNotice(null, error.message)));
  $('recheckButton').addEventListener('click', async () => {
    await loadProducts();
    if (!state.config) return;
    const { body } = await request('/api/render', { method: 'POST', body: { config: state.config, editing: state.editing } });
    state.validation = body.validation || { errors: [{ message: body.issue?.message || body.message || '资料库检测失败。' }], warnings: [] };
    if (body.ok) {
      state.config = body.config || state.config;
      injectSvg(body.svg);
    }
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
    setZoom(state.zoom + (event.deltaY < 0 ? 0.01 : -0.01), event);
  }, { passive: false });
  $('canvasViewport').addEventListener('pointerdown', beginCanvasPan);
  $('curveType').addEventListener('change', () => updateGraphic((graphic) => {
    if (graphic.curve.type === $('curveType').value) return { changed: false };
    graphic.curve.type = $('curveType').value;
    return { changed: true };
  }, { structural: true }));
  bindNumericInput($('strokeWidth'), (value) => updateGraphic((graphic) => {
    const width = normaliseWidth(value);
    if (graphic.curve.strokeWidth === width) return { changed: false };
    graphic.curve.strokeWidth = width;
    return { changed: true };
  }));
  scaleInputs().forEach((input) => input.addEventListener('change', () => {
    const scale = readScaleInputs({ format: true });
    if (!scale) return;
    updateGraphic((graphic) => {
      const referencesInRange = graphic.curve.points.every((point) => valueInScale(point.x, scale.xMin, scale.xMax) && valueInScale(point.y, scale.yMin, scale.yMax))
        && graphic.thresholds.every((item) => valueInScale(item.value, scale.xMin, scale.xMax))
        && graphic.guides.every((item) => {
          const valueRange = item.direction === 'horizontal' ? [scale.yMin, scale.yMax] : [scale.xMin, scale.xMax];
          if (!valueInScale(item.value, valueRange[0], valueRange[1])) return false;
          if (!Object.prototype.hasOwnProperty.call(item, 'start')) return true;
          const extentRange = guideExtentRange(scale, item.direction);
          return valueInScale(item.start, extentRange[0], extentRange[1]) && valueInScale(item.end, extentRange[0], extentRange[1]);
        });
      if (!referencesInRange) return { changed: false, message: '新坐标范围必须覆盖已有曲线、关键价格线和辅助线。请先调整图形数值。' };
      if (Object.keys(scale).every((key) => graphic.scale[key] === scale[key])) return { changed: false };
      graphic.scale = scale;
      return { changed: true };
    }, { structural: true });
  }));
  $('autoScaleButton').addEventListener('click', autoFitCurrentGraphic);
  $('guideDirection').addEventListener('change', () => {
    $('guideValue').value = '';
    if (selectedGraphic()) applyGuideExtentDefaults(selectedGraphic().scale, $('guideDirection').value, { reset: true });
    updateUi();
  });
  $('guideValue').addEventListener('input', () => updateUi());
  $('guideStart').addEventListener('input', () => updateUi());
  $('guideEnd').addEventListener('input', () => updateUi());
  $('thresholdValue').addEventListener('input', () => updateUi());
  $('thresholdToken').addEventListener('change', () => updateUi());
  $('addCurveButton').addEventListener('click', () => {
    updateGraphic((graphic) => {
      if (graphic.curve.points.length) return { changed: false, message: '当前情景已有曲线节点。' };
      graphic.curve.points = blankCurvePoints(graphic.scale);
      return { changed: true };
    }, { structural: true });
    updateUi();
  });
  $('addNodeButton').addEventListener('click', () => {
    updateGraphic((graphic) => {
      const curve = graphic.curve;
      if (!curve.points.length) {
        curve.points = blankCurvePoints(graphic.scale);
        return { changed: true };
      }
      if (curve.points.length >= MAX_POINTS) return { changed: false, message: `每条曲线最多${MAX_POINTS}个节点。` };
      const gaps = curve.points.slice(1).map((point, index) => ({ index, width: point.x - curve.points[index].x }));
      const widest = gaps.reduce((current, candidate) => candidate.width > current.width ? candidate : current, gaps[0]);
      if (!widest || widest.width < 0.02) return { changed: false, message: '没有足够的价格区间添加节点；请先拉开相邻节点。' };
      const left = curve.points[widest.index];
      const right = curve.points[widest.index + 1];
      curve.points.splice(widest.index + 1, 0, { x: round((left.x + right.x) / 2), y: round((left.y + right.y) / 2) });
      return { changed: true };
    }, { structural: true });
    updateUi();
  });
  $('addGuideButton').addEventListener('click', () => {
    const graphic = selectedGraphic();
    const direction = $('guideDirection').value;
    const value = readControlValue($('guideValue'), graphic.scale, direction);
    const start = readGuideExtentValue($('guideStart'), graphic.scale, direction);
    const end = readGuideExtentValue($('guideEnd'), graphic.scale, direction);
    if (value === null || start === null || end === null) return;
    updateGraphic((target) => addGuide(target, { direction, value, start, end, style: 'dashed' }), { structural: true });
    updateUi();
  });
  $('addThresholdButton').addEventListener('click', () => {
    const graphic = selectedGraphic();
    const value = readControlValue($('thresholdValue'), graphic.scale, 'vertical');
    if (value === null) return;
    updateGraphic((target) => addThreshold(target, { token: $('thresholdToken').value, value }), { structural: true });
    updateUi();
  });
  $('resetScenarioButton').addEventListener('click', resetCurrentScenario);
  $('resetAllButton').addEventListener('click', resetAllGraphics);
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
  document.addEventListener('pointermove', moveCanvasPan);
  document.addEventListener('pointerup', endCanvasPan);
  document.addEventListener('pointercancel', endCanvasPan);
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
  document.addEventListener('keydown', (event) => {
    if (event.code !== 'Space' || isTextInput(event.target)) return;
    event.preventDefault();
    setSpacePan(true);
  });
  document.addEventListener('keyup', (event) => {
    if (event.code === 'Space') setSpacePan(false);
  });
  window.addEventListener('blur', () => {
    setSpacePan(false);
    endCanvasPan();
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
