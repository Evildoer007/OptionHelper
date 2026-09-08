const OptionHelperDateInput = typeof module === 'object' && module.exports
  ? require('../../../core/src/runtime/browser/date_input_control.js')
  : globalThis.OptionHelperDateInput;
const DATE_RANGE_ERROR = '开始日期不得晚于结束日期。';

function parseAndFormatDate(value) {
  return OptionHelperDateInput.parseAndFormat(value);
}

function defaultDateRange(now = new Date()) {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  start.setFullYear(end.getFullYear() - 3);
  if (start.getMonth() !== end.getMonth()) start.setDate(0);
  const display = value => [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, '0'),
    String(value.getDate()).padStart(2, '0'),
  ].join('/');
  return {start: display(start), end: display(end)};
}

function splitList(value) {
  const source = String(value || '').trim();
  if (!source) return [];
  let values;
  try {
    const parsed = JSON.parse(source);
    values = Array.isArray(parsed) ? parsed : [parsed];
  } catch (_) {
    values = source.split(/[\n,，;；]/);
  }
  return values
    .map(item => String(item).trim().replace(/^(?:"([\s\S]*)"|'([\s\S]*)')$/, '$1$2').trim())
    .filter(Boolean);
}

const CACHE_REUSE_DECISIONS = new Set([
  'cache_hit', 'cache_revalidated', 'cache_rebound',
  'verified_cache_reused', 'verified_cache_fallback', 'latest_session_pending',
]);

function latestSessionPendingLabel(coverageEnd) {
  return coverageEnd
    ? `最后交易日日线待发布，当前数据截至${coverageEnd}`
    : '最后交易日日线待发布，当前数据截止日未提供';
}

function cacheDecisionLabel(value, coverageEnd = '') {
  if (value === 'latest_session_pending') return latestSessionPendingLabel(coverageEnd);
  return ({
    cache_hit: '使用已保存数据',
    cache_revalidated: '复核已保存数据',
    cache_rebound: '复用原始数据并重新登记',
    cache_miss_fetched: '实时获取',
    cache_extended: '补齐数据缺口',
    provider_fetched: '实时获取',
    verified_cache_reused: '复用已验证日历',
    verified_cache_fallback: '数据服务失败，复用完整日历',
  }[value] || '已完成');
}

function calendarCompletenessLabel(value, coverageEnd = '') {
  if (value === 'complete') return '已验证完整';
  if (value === 'incomplete') return '已验证但不完整';
  if (value === 'latest_session_pending') return latestSessionPendingLabel(coverageEnd);
  return '未验证';
}

function assetIndexViewModel(item) {
  const ref = item?.data_asset_ref || item || {};
  const coverage = ref.coverage || {};
  const lineage = ref.lineage || {};
  const schema = String(ref.schema_id || '');
  const cacheDecision = String(lineage.cache_decision || item?.cache_decision || '');
  const provider = String(lineage.provider || item?.provider || '').toLowerCase();
  const calendar = String(item?.quality_report?.calendar_completeness || '');
  const assetType = schema === 'market-history' ? '历史行情' : schema === 'trading-calendar' ? '交易日历' : '数据资产';
  const source = provider === 'local'
    ? '本地CSV'
    : provider === 'ifind_http'
      ? (CACHE_REUSE_DECISIONS.has(cacheDecision) ? 'iFind缓存' : 'iFind')
      : '来源未提供';
  let quality = '状态未提供';
  if (schema === 'trading-calendar') quality = '已验证';
  else if (cacheDecision === 'latest_session_pending' || calendar === 'latest_session_pending') quality = '末日待发布';
  else if (calendar === 'complete') quality = '日历已验证';
  else if (calendar === 'incomplete') quality = '日历不完整';
  else if (calendar === 'unverified' || coverage.calendar_revision === 'unverified') quality = '日历未验证';
  else if (coverage.calendar_revision) quality = '日历已验证';
  return {assetType, source, quality};
}

function dataSourceLabel(data) {
  const lineageProvider = data?.data_asset_ref?.lineage?.provider;
  const successfulCall = (Array.isArray(data?.provider_calls) ? data.provider_calls : [])
    .find(call => ['succeeded', 'success'].includes(String(call?.outcome || '').toLowerCase()));
  const provider = String(lineageProvider || successfulCall?.provider || '').toLowerCase();
  if (provider === 'local') return '本地CSV';
  if (['cache_hit', 'cache_revalidated', 'cache_rebound'].includes(data?.cache_decision)) return 'iFind远程缓存';
  if (provider) return 'iFind远程行情';
  return '数据来源未提供';
}

function buildFetchRequest(values) {
  const start = parseAndFormatDate(values.startDate);
  const end = parseAndFormatDate(values.endDate);
  if (!start || !end) throw new Error('日期必须为真实的yyyy/mm/dd或yyyy-mm-dd。');
  if (start.iso > end.iso) throw new Error(DATE_RANGE_ERROR);
  const assetIds = splitList(values.assetIds).map(item => item.toUpperCase());
  const requestedFields = splitList(values.fields).map(item => item.toLowerCase());
  const fields = [...new Set(['close', 'adj_close', ...requestedFields.flatMap(field => (
    ['open', 'high', 'low', 'close'].includes(field) ? [field, `adj_${field}`] : [field]
  ))])];
  if (!assetIds.length) throw new Error('请至少填写一个资产标识。');
  const invalidAsset = assetIds.find(item => !/^\d{6}\.(?:SH|SZ)$/.test(item));
  if (invalidAsset) throw new Error(`资产标识“${invalidAsset}”格式不正确，请使用000300.SH或399001.SZ。`);
  const request = {asset_ids: assetIds, start_date: start.iso, end_date: end.iso, fields, source_priority: ['local', 'ifind_http'], frequency: values.frequency || '1d', adjustment: values.adjustment, cache_policy: 'extend_only', offline: false, persistence_mode: values.saveDownloadedData ? 'library' : 'volatile'};
  return request;
}

function resultViewModel(data) {
  const ref = data.data_asset_ref || {};
  const coverage = ref.coverage || {};
  const quality = data.quality_report || {};
  const observed = quality.observed_rows || {};
  const calendar = quality.calendar_completeness || 'unverified';
  const rawByAsset = coverage.by_asset || quality.coverage_by_asset;
  const byAsset = rawByAsset && typeof rawByAsset === 'object' && !Array.isArray(rawByAsset) ? rawByAsset : {};
  const byAssetRows = Object.entries(byAsset).map(([assetId, value]) => {
    const detail = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const range = [detail.start_date, detail.end_date].filter(Boolean).join('至') || '日期未提供';
    const rows = detail.row_count == null ? '行数未提供' : `${detail.row_count}行`;
    return [`${assetId}覆盖`, `${range}，${rows}`];
  });
  return {
    overview: [
      ['数据来源', dataSourceLabel(data)],
      ['标的', (ref.asset_ids || []).join('、') || '未提供'],
      ['覆盖区间', [coverage.start_date, coverage.end_date].filter(Boolean).join('至') || '未提供'],
      ['记录数', ref.row_count ?? '未提供'],
      ['行情字段', Array.isArray(ref.normalized_fields) && ref.normalized_fields.length ? ref.normalized_fields.join('、') : '未提供'],
      ['保存状态', ref.lineage?.persistence_mode === 'volatile' ? '本次使用，未保存' : '已保存到数据资产'],
    ],
    coverage: [
      ['标的', (ref.asset_ids || []).join('、') || '未提供'],
      ['覆盖区间', [coverage.start_date, coverage.end_date].filter(Boolean).join('至') || '未提供'],
      ['交易日观测', coverage.sessions?.length ?? ref.row_count ?? '未提供'],
      ['逐标的覆盖', Object.keys(byAsset).length ? `${Object.keys(byAsset).length}个标的` : '未提供'],
      ...byAssetRows,
    ],
    fields: Array.isArray(ref.normalized_fields) ? ref.normalized_fields : [],
    quality: [
      ['观测行', observed.status === 'valid' ? `有效，${observed.row_count ?? ref.row_count ?? '—'}行` : (observed.status || '未提供')],
      ['交易日历完整性', calendarCompletenessLabel(calendar, coverage.end_date)],
      ['缺失值', quality.missing_values ?? '未提供'],
      ['重复行', quality.duplicate_rows ?? '未提供'],
      ['乱序行', quality.unordered_rows ?? '未提供'],
    ],
  };
}

function chartViewModel(rawSeries) {
  const source = Array.isArray(rawSeries) ? rawSeries : [];
  const cleaned = source.map(series => {
    const points = (Array.isArray(series?.points) ? series.points : [])
      .map(point => ({date: String(point?.date || ''), rawValue: Number(point?.value)}))
      .filter(point => point.date && Number.isFinite(point.rawValue) && point.rawValue > 0 && Number.isFinite(Date.parse(point.date.includes('T') ? point.date : `${point.date}T00:00:00Z`)))
      .sort((left, right) => left.date.localeCompare(right.date));
    return {assetId: String(series?.asset_id || ''), field: String(series?.field || ''), points};
  }).filter(series => series.assetId && series.points.length);
  const normalized = cleaned.length > 1;
  const series = cleaned.map(item => {
    const baseline = item.points[0]?.rawValue;
    if (normalized && !Number.isFinite(baseline)) return null;
    return {
      ...item,
      points: item.points.map(point => ({
        ...point,
        timestamp: Date.parse(point.date.includes('T') ? point.date : `${point.date}T00:00:00Z`),
        value: normalized ? point.rawValue / baseline : point.rawValue,
      })),
    };
  }).filter(Boolean);
  const points = series.flatMap(item => item.points);
  if (!points.length) return {series: [], normalized, minDate: 0, maxDate: 0, minValue: 0, maxValue: 0};
  let minValue = Math.min(...points.map(point => point.value));
  let maxValue = Math.max(...points.map(point => point.value));
  if (minValue === maxValue) {
    const padding = Math.abs(minValue || 1) * 0.05;
    minValue -= padding;
    maxValue += padding;
  }
  return {
    series,
    normalized,
    minDate: Math.min(...points.map(point => point.timestamp)),
    maxDate: Math.max(...points.map(point => point.timestamp)),
    minValue,
    maxValue,
  };
}

function resultSummary(data) {
  if (data.ok === false) return [
    {label: '获取状态', value: '请求失败'},
    {label: '记录数', value: '暂无'},
    {label: '保存状态', value: '未保存'},
  ];
  const ref = data.data_asset_ref || {};
  return [{label: '获取方式', value: cacheDecisionLabel(data.cache_decision, ref.coverage?.end_date)}, {label: '记录数', value: ref.row_count ?? '暂无'}, {label: '保存状态', value: ref.lineage?.persistence_mode === 'volatile' ? '本次使用，未保存' : '已保存到数据资产'}];
}

function credentialStatusText(data) {
  const status = data?.credential_status;
  const configured = typeof data?.configured === 'boolean' ? data.configured : status?.configured;
  if (configured === false) return '未配置，请前往设置中心。';
  if (configured === true) {
    if (status?.remote_provider === 'available') return '已连接。';
    return '已配置，尚未验证。';
  }
  return '连接状态未知。';
}

function dataSettingsFailure(data) {
  const code = data?.failure_code || data?.error?.failure_code || data?.error?.code || '';
  const detail = `${data?.message || ''} ${data?.next_step || ''} ${data?.error?.message || ''} ${data?.error?.next_step || ''}`;
  return ['data_interface_configuration', 'data_interface_unverified', 'unauthorized', 'account_permission_denied'].includes(code)
    || /(?:(?:iFind|数据接口).*(?:凭据|认证|授权|验证|配置)|(?:凭据|认证|授权).*(?:iFind|数据接口))/i.test(detail);
}

function openDataSettings() {
  const bridgeNonce = new URLSearchParams(location.search).get('bridge_nonce');
  if (window.parent !== window && bridgeNonce) {
    window.parent.postMessage({type: 'optionhelper.open-settings', module: 'datafetcher', section: 'data', bridge_nonce: bridgeNonce}, location.origin);
    return;
  }
  location.assign('/settings#data');
}

async function parseServiceResponse(response) {
  const data = await response.json();
  if (!response.ok || !data.ok) {
    const reason = data.error?.message || data.message || '请求失败。';
    const nextStep = data.error?.next_step || data.next_step;
    const message = nextStep && !reason.includes(nextStep) ? `${reason.replace(/\s+$/u, '')}${nextStep}` : reason;
    const error = new Error(message);
    error.payload = data;
    throw error;
  }
  return data;
}

function initializePage() {
  const $ = id => document.getElementById(id);
  const state = (text, kind = '') => { $('canvas-state').textContent = text; $('state-dot').className = `state-dot ${kind}`; };
  const message = (text, kind = '') => { const node = $('notice'); node.textContent = text; node.className = `notice ${kind}`; node.hidden = !text; };
  let requestRevision = 0;
  let operationActive = false;
  let hasResult = false;
  const clearFieldError = id => {
    $(id).removeAttribute('aria-invalid');
    $(`${id}-error`).textContent = '';
  };
  const clearFieldErrors = () => {
    for (const id of ['asset-ids', 'start-date', 'end-date']) clearFieldError(id);
  };
  const showInputError = text => {
    const targets = /资产标识/.test(text) ? ['asset-ids'] : /开始日期不得晚于结束日期/.test(text) ? ['start-date', 'end-date'] : /开始日期/.test(text) ? ['start-date'] : /结束日期/.test(text) ? ['end-date'] : /日期/.test(text) ? ['start-date', 'end-date'] : [];
    targets.forEach(id => $(id).setAttribute('aria-invalid', 'true'));
    if (targets.includes('asset-ids')) $('asset-ids-error').textContent = text;
    if (targets.includes('start-date')) $('start-date-error').textContent = text;
    if (targets.includes('end-date')) $('end-date-error').textContent = text;
    $(targets[0])?.focus();
  };
  const revalidateDateErrors = event => {
    const changedId = event.target?.id;
    if (!['start-date', 'end-date'].includes(changedId)) return;
    const start = parseAndFormatDate($('start-date').value);
    const end = parseAndFormatDate($('end-date').value);
    const rangeErrorActive = [$('start-date-error'), $('end-date-error')]
      .some(node => node.textContent === DATE_RANGE_ERROR);
    if (rangeErrorActive) {
      if (start && end && start.iso <= end.iso) {
        clearFieldError('start-date');
        clearFieldError('end-date');
      } else if (!parseAndFormatDate($(changedId).value)) {
        clearFieldError(changedId);
      } else if (start && end) {
        for (const id of ['start-date', 'end-date']) {
          $(id).setAttribute('aria-invalid', 'true');
          $(`${id}-error`).textContent = DATE_RANGE_ERROR;
        }
      }
      return;
    }
    if (parseAndFormatDate($(changedId).value)) {
      clearFieldError(changedId);
    } else if ($(`${changedId}-error`).textContent) {
      $(changedId).setAttribute('aria-invalid', 'true');
    }
  };
  const revalidateAssetError = event => {
    if (event.target?.id !== 'asset-ids' || !$('asset-ids-error').textContent) return;
    const assetIds = splitList($('asset-ids').value).map(value => value.toUpperCase());
    if (assetIds.length && assetIds.every(value => /^\d{6}\.(?:SH|SZ)$/.test(value))) {
      clearFieldError('asset-ids');
    }
  };
  function setBusy(busy) {
    operationActive = busy;
    const controls = [...$('request-form').querySelectorAll('input,select,textarea,button')];
    controls.forEach(control => {
      if (busy) {
        if (!Object.hasOwn(control.dataset, 'operationDisabled')) control.dataset.operationDisabled = control.disabled ? 'true' : 'false';
        control.disabled = true;
      } else if (Object.hasOwn(control.dataset, 'operationDisabled')) {
        control.disabled = control.dataset.operationDisabled === 'true';
        delete control.dataset.operationDisabled;
      }
    });
    $('submit-request').textContent = busy ? '正在执行请求…' : '获取数据';
  }
  const requestJson = async (url, options) => parseServiceResponse(await fetch(url, options));
  const assetStore = new Map();
  let assetLoadError = false;
  function renderAssets() {
    const query = $('asset-search').value.trim().toLowerCase(); const entries = [...assetStore.values()].filter(item => JSON.stringify(item).toLowerCase().includes(query)); const list = $('asset-list'); list.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement('p'); empty.className = 'empty-list';
      empty.textContent = assetLoadError ? '资产索引加载失败，请刷新重试。' : query && assetStore.size ? '没有匹配的数据资产。' : '暂无已保存数据。';
      list.append(empty); return;
    }
    entries.forEach(item => {
      const ref = item.data_asset_ref || item; const id = ref.data_asset_id || ref.content_hash || item.id;
      if (!id) return;
      const entry = document.createElement('div'); entry.className = 'asset-entry';
      const select = document.createElement('button'); select.type = 'button'; select.className = 'asset-select'; select.innerHTML = '<strong></strong><span class="asset-range"></span><span class="asset-meta"></span>';
      const coverage = ref.coverage || {}; const range = [coverage.start_date, coverage.end_date].filter(Boolean).join('至');
      select.querySelector('strong').textContent = (ref.asset_ids || item.asset_ids || []).join('、') || '已保存数据'; select.querySelector('.asset-range').textContent = `${ref.row_count ?? '—'}行${range ? `，${range}` : ''}`;
      const indexView = assetIndexViewModel(item); const meta = select.querySelector('.asset-meta');
      [indexView.assetType, indexView.source, indexView.quality].forEach(value => { const label = document.createElement('span'); label.textContent = value; meta.append(label); });
      select.addEventListener('click', () => { if (operationActive) return; const ids = ref.asset_ids || item.asset_ids; if (Array.isArray(ids) && ids.length) { $('asset-ids').value = ids.join('\n'); syncSourceVisibility(); markRequestInputChanged(); } });
      const download = document.createElement('a'); download.className = 'asset-download'; download.href = `/api/assets/${encodeURIComponent(id)}/download`; download.textContent = ref.media_type === 'application/json' ? '下载JSON' : '下载CSV';
      entry.append(select, download); list.append(entry);
    });
  }
  function addAsset(item) { const ref = item?.data_asset_ref || item; if (ref?.lineage?.persistence_mode === 'volatile') return; const key = ref?.data_asset_id || ref?.content_hash; if (key) assetStore.set(key, item); renderAssets(); }
  function appendSummarySection(parent, title, rows, description = '') { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = '<h3></h3><p hidden></p><div class="summary-list"></div>'; section.querySelector('h3').textContent = title; if (description) { const copy = section.querySelector('p'); copy.textContent = description; copy.hidden = false; } rows.forEach(([label, value]) => { const item = document.createElement('div'); item.className = 'summary-item'; item.innerHTML = '<span></span><strong></strong>'; item.querySelector('span').textContent = label; item.querySelector('strong').textContent = String(value); section.querySelector('.summary-list').append(item); }); parent.append(section); }
  function appendAuditDetails(parent, value) { const details = document.createElement('details'); details.className = 'audit-details'; details.innerHTML = '<summary>技术详情</summary><pre class="json-block"></pre>'; details.querySelector('pre').textContent = JSON.stringify(value ?? null, null, 2); parent.append(details); }
  function appendMarketChart(parent, rawSeries) {
    const view = chartViewModel(rawSeries);
    const section = document.createElement('section');
    section.className = 'market-chart';
    section.innerHTML = '<div class="market-chart__heading"><div><h3>行情走势</h3><p></p></div><div class="market-chart__legend" aria-label="图例"></div></div><div class="market-chart__stage"></div><div class="market-chart__tooltip" hidden></div>';
    section.querySelector('.market-chart__heading p').textContent = view.normalized ? '多标的按首个有效观测归一，基准=1。' : '展示完整请求区间的收盘走势抽样。';
    const stage = section.querySelector('.market-chart__stage');
    if (!view.series.length) {
      stage.classList.add('market-chart__stage--empty');
      stage.textContent = '暂无可绘制的价格数据';
      parent.append(section);
      return;
    }
    const namespace = 'http://www.w3.org/2000/svg';
    const width = 760; const height = 270; const left = 58; const right = 18; const top = 18; const bottom = 34;
    const plotWidth = width - left - right; const plotHeight = height - top - bottom;
    const dateSpan = Math.max(1, view.maxDate - view.minDate); const valueSpan = Math.max(Number.EPSILON, view.maxValue - view.minValue);
    const xFor = timestamp => left + ((timestamp - view.minDate) / dateSpan) * plotWidth;
    const yFor = value => top + (1 - ((value - view.minValue) / valueSpan)) * plotHeight;
    const formatNumber = value => new Intl.NumberFormat('zh-CN', {maximumFractionDigits: view.normalized ? 4 : 4}).format(value);
    const svg = document.createElementNS(namespace, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', view.normalized ? '多标的归一化行情走势图，基准为1' : `${view.series[0].assetId}行情走势图`);
    const axes = document.createElementNS(namespace, 'g'); axes.setAttribute('class', 'market-chart__axes');
    const bottomAxis = document.createElementNS(namespace, 'line'); bottomAxis.setAttribute('class', 'market-chart__axis-line'); bottomAxis.setAttribute('x1', left); bottomAxis.setAttribute('x2', width - right); bottomAxis.setAttribute('y1', height - bottom); bottomAxis.setAttribute('y2', height - bottom); axes.append(bottomAxis);
    const leftAxis = document.createElementNS(namespace, 'line'); leftAxis.setAttribute('class', 'market-chart__axis-line'); leftAxis.setAttribute('x1', left); leftAxis.setAttribute('x2', left); leftAxis.setAttribute('y1', top); leftAxis.setAttribute('y2', height - bottom); axes.append(leftAxis);
    for (let index = 0; index <= 4; index += 1) {
      const value = view.maxValue - (valueSpan * index / 4); const y = top + (plotHeight * index / 4);
      const tick = document.createElementNS(namespace, 'line'); tick.setAttribute('class', 'market-chart__axis-tick'); tick.setAttribute('x1', left - 4); tick.setAttribute('x2', left); tick.setAttribute('y1', y); tick.setAttribute('y2', y); axes.append(tick);
      const label = document.createElementNS(namespace, 'text'); label.setAttribute('class', 'market-chart__axis-label'); label.setAttribute('x', left - 9); label.setAttribute('y', y + 4); label.setAttribute('text-anchor', 'end'); label.textContent = formatNumber(value); axes.append(label);
    }
    const dateLabel = timestamp => new Date(timestamp).toISOString().slice(0, 10);
    for (const [timestamp, anchor] of [[view.minDate, 'start'], [view.maxDate, 'end']]) { const x = anchor === 'start' ? left : width - right; const tick = document.createElementNS(namespace, 'line'); tick.setAttribute('class', 'market-chart__axis-tick'); tick.setAttribute('x1', x); tick.setAttribute('x2', x); tick.setAttribute('y1', height - bottom); tick.setAttribute('y2', height - bottom + 4); axes.append(tick); const label = document.createElementNS(namespace, 'text'); label.setAttribute('class', 'market-chart__axis-label market-chart__date'); label.setAttribute('x', x); label.setAttribute('y', height - 8); label.setAttribute('text-anchor', anchor); label.textContent = dateLabel(timestamp); axes.append(label); }
    svg.append(axes);
    const legend = section.querySelector('.market-chart__legend');
    const hoverPoints = [];
    view.series.forEach((series, index) => {
      const colorIndex = (index % 6) + 1;
      const item = document.createElement('span'); item.style.setProperty('--series-color', `var(--data-series-${colorIndex})`); item.innerHTML = '<i aria-hidden="true"></i><b></b>'; item.querySelector('b').textContent = series.assetId; legend.append(item);
      const path = document.createElementNS(namespace, 'path');
      path.setAttribute('class', 'market-chart__line'); path.style.setProperty('--series-color', `var(--data-series-${colorIndex})`);
      path.setAttribute('d', series.points.map((point, pointIndex) => `${pointIndex ? 'L' : 'M'}${xFor(point.timestamp).toFixed(2)},${yFor(point.value).toFixed(2)}`).join(' ')); svg.append(path);
      const marker = document.createElementNS(namespace, 'circle'); marker.setAttribute('class', 'market-chart__marker'); marker.setAttribute('r', '4'); marker.style.setProperty('--series-color', `var(--data-series-${colorIndex})`); marker.hidden = true; svg.append(marker); hoverPoints.push({series, marker});
    });
    const guide = document.createElementNS(namespace, 'line'); guide.setAttribute('class', 'market-chart__guide'); guide.setAttribute('y1', top); guide.setAttribute('y2', height - bottom); guide.hidden = true; svg.append(guide);
    const hitArea = document.createElementNS(namespace, 'rect'); hitArea.setAttribute('class', 'market-chart__hit'); hitArea.setAttribute('x', left); hitArea.setAttribute('y', top); hitArea.setAttribute('width', plotWidth); hitArea.setAttribute('height', plotHeight); svg.append(hitArea);
    const tooltip = section.querySelector('.market-chart__tooltip');
    const hideHover = () => { guide.hidden = true; tooltip.hidden = true; hoverPoints.forEach(item => { item.marker.hidden = true; }); };
    hitArea.addEventListener('pointermove', event => {
      const bounds = svg.getBoundingClientRect(); const pointerX = Math.max(left, Math.min(width - right, ((event.clientX - bounds.left) / bounds.width) * width)); const targetTime = view.minDate + ((pointerX - left) / plotWidth) * dateSpan;
      guide.setAttribute('x1', pointerX); guide.setAttribute('x2', pointerX); guide.hidden = false;
      const rows = hoverPoints.map(({series, marker}) => { const point = series.points.reduce((nearest, candidate) => Math.abs(candidate.timestamp - targetTime) < Math.abs(nearest.timestamp - targetTime) ? candidate : nearest); marker.setAttribute('cx', xFor(point.timestamp)); marker.setAttribute('cy', yFor(point.value)); marker.hidden = false; return `${series.assetId}  ${formatNumber(point.value)}`; });
      const nearestDate = hoverPoints[0].series.points.reduce((nearest, candidate) => Math.abs(candidate.timestamp - targetTime) < Math.abs(nearest.timestamp - targetTime) ? candidate : nearest).date;
      tooltip.textContent = `${nearestDate}　${rows.join('　')}`; tooltip.hidden = false; tooltip.style.left = `${Math.min(78, Math.max(12, (event.clientX - bounds.left) / bounds.width * 100))}%`;
    });
    hitArea.addEventListener('pointerleave', hideHover); hitArea.addEventListener('pointercancel', hideHover);
    stage.append(svg); parent.append(section);
  }
  function renderResult(data) {
    const content = $('result-content'); content.replaceChildren();
    if (data.ok === false) {
      const failure = document.createElement('section'); failure.className = 'result-section failure-card';
      failure.innerHTML = '<h3>请求未完成</h3><p class="failure-reason"></p><p class="failure-next" hidden></p><small class="failure-code"></small>';
      failure.querySelector('.failure-reason').textContent = data.error?.message || data.message || '数据服务未返回可用数据。';
      const nextStep = data.error?.next_step || data.next_step;
      if (nextStep) { const node = failure.querySelector('.failure-next'); node.textContent = nextStep; node.hidden = false; }
      failure.querySelector('.failure-code').textContent = data.error?.code ? `错误代码：${data.error.code}` : '';
      if (dataSettingsFailure(data)) { const action = document.createElement('button'); action.type = 'button'; action.className = 'button button-quiet'; action.textContent = '打开设置中心的数据接口'; action.addEventListener('click', openDataSettings); failure.append(action); }
      content.append(failure);
      if (Array.isArray(data.provider_calls) && data.provider_calls.length) appendAuditDetails(content, {provider_calls: data.provider_calls, quota_usage: data.quota_usage ?? null});
      $('empty-canvas').hidden = true; content.hidden = false; return;
    }
    const view = resultViewModel(data);
    appendMarketChart(content, data.chart_series);
    appendSummarySection(content, '数据概览', view.overview);
    appendSummarySection(content, '数据质量', view.quality);
    const schema = document.createElement('section'); schema.className = 'result-section'; schema.innerHTML = '<h3>行情字段</h3><div class="tag-list"></div>'; (view.fields.length ? view.fields : ['未提供']).forEach(value => { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = value; schema.querySelector('.tag-list').append(tag); }); content.append(schema);
    if (Array.isArray(data.data_preview) && data.data_preview.length) { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = '<h3>数据预览</h3><div class="preview-table-wrap"><table class="preview-table"><thead></thead><tbody></tbody></table></div>'; const columns = [...new Set(data.data_preview.flatMap(row => Object.keys(row)))]; const head = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('th'); cell.textContent = key; head.append(cell); }); section.querySelector('thead').append(head); data.data_preview.forEach(row => { const tr = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('td'); cell.textContent = row[key] ?? ''; tr.append(cell); }); section.querySelector('tbody').append(tr); }); content.append(section); }
    appendAuditDetails(content, {data_fetch_run_id: data.data_fetch_run_id ?? null, cache_decision: data.cache_decision ?? null, quality_report: data.quality_report ?? null, provider_calls: data.provider_calls ?? [], quota_usage: data.quota_usage ?? null, warnings: data.warnings ?? [], data_asset_ref: data.data_asset_ref ?? null});
    $('empty-canvas').hidden = true; content.hidden = false;
  }
  async function loadAssets() { assetLoadError = false; try { const data = await requestJson('/api/assets'); const items = Array.isArray(data.assets) ? data.assets : Array.isArray(data) ? data : []; assetStore.clear(); items.forEach(addAsset); renderAssets(); } catch (_) { assetLoadError = true; renderAssets(); } }
  async function loadStatus() { try { const data = await requestJson('/api/status'); $('service-status').textContent = data.status === 'available' ? '本机服务已就绪' : (data.status || '服务状态未知'); $('secret-status').textContent = credentialStatusText(data); } catch (_) { $('service-status').textContent = '本机服务未启动'; $('secret-status').textContent = '无法读取连接状态。'; } }
  let refreshInFlight = null;
  function refreshDataFetcherState() {
    if (refreshInFlight) return refreshInFlight;
    const control = $('refresh-assets');
    control.disabled = true;
    control.setAttribute('aria-busy', 'true');
    control.title = '正在刷新数据资产和连接状态';
    refreshInFlight = Promise.all([loadStatus(), loadAssets()]).finally(() => {
      refreshInFlight = null;
      control.disabled = false;
      control.removeAttribute('aria-busy');
      control.title = '刷新数据资产和连接状态';
    });
    return refreshInFlight;
  }
  const defaults = defaultDateRange();
  if (!$('start-date').value.trim()) $('start-date').value = defaults.start;
  if (!$('end-date').value.trim()) $('end-date').value = defaults.end;
  for (const id of ['start-date', 'end-date']) {
    const input = $(id);
    OptionHelperDateInput.bind(input, id === 'start-date' ? '开始日期' : '结束日期');
  }
  const fieldControls = [...document.querySelectorAll('.extra-field')];
  const formValues = () => ({assetIds: $('asset-ids').value, startDate: $('start-date').value, endDate: $('end-date').value, fields: fieldControls.filter(node => node.checked).map(node => node.value).join(','), frequency: $('frequency').value, adjustment: $('adjustment').value, saveDownloadedData: $('save-downloaded-data').checked});
  function syncSourceVisibility() { const selectedFields = fieldControls.filter(node => node.checked).length; $('field-summary').textContent = selectedFields === fieldControls.length ? '全部字段' : `${selectedFields}项字段`; }
  function markRequestInputChanged() {
    if (operationActive) return;
    requestRevision += 1;
    if (!hasResult) return;
    state('结果待更新', 'running');
    message('输入已修改；下方保留上一次完成结果，请重新获取数据。');
  }
  $('request-form').addEventListener('input', revalidateDateErrors);
  $('request-form').addEventListener('change', revalidateDateErrors);
  $('request-form').addEventListener('input', revalidateAssetError);
  $('request-form').addEventListener('change', revalidateAssetError);
  $('request-form').addEventListener('input', syncSourceVisibility);
  $('request-form').addEventListener('change', syncSourceVisibility);
  $('request-form').addEventListener('input', markRequestInputChanged);
  $('request-form').addEventListener('change', markRequestInputChanged);
  syncSourceVisibility();
  $('asset-search').addEventListener('input', renderAssets); $('refresh-assets').addEventListener('click', () => { void refreshDataFetcherState(); });
  document.addEventListener('optionhelper:modulevisibility', event => { if (event.detail?.active === true) void refreshDataFetcherState(); });
  $('request-form').addEventListener('submit', async event => { event.preventDefault(); if (operationActive) return; clearFieldErrors(); const hadResult = hasResult; let request; try { const values=formValues(); values.startDate=OptionHelperDateInput.read($('start-date'),'开始日期'); values.endDate=OptionHelperDateInput.read($('end-date'),'结束日期'); request = buildFetchRequest(values); $('asset-ids').value = request.asset_ids.join('\n'); OptionHelperDateInput.set($('start-date'),request.start_date,'开始日期'); OptionHelperDateInput.set($('end-date'),request.end_date,'结束日期'); syncSourceVisibility(); } catch (error) { state('请求配置错误', 'failed'); message(`${error.message}${hadResult ? ' 下方保留上一次完成结果。' : ''}`, 'failed'); showInputError(error.message); return; }
    const revision = ++requestRevision;
    state('正在检查数据与缓存', 'running'); message(hadResult ? '正在获取数据；下方暂时展示上一次完成结果。' : '正在获取数据并核验保存状态。'); setBusy(true); if (!hadResult) { $('result-content').hidden = true; $('empty-canvas').hidden = false; }
    try { const data = await requestJson('/api/fetch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request)}); if (revision !== requestRevision) return; renderResult(data); addAsset(data); hasResult = true; state('请求完成', 'complete'); message('数据获取完成。'); } catch (error) { if (revision !== requestRevision) return; if (error.payload && !hadResult) renderResult(error.payload); state('请求失败', 'failed'); message(`${error.message}${hadResult ? ' 下方保留上一次完成结果。' : ''}`, 'failed'); } finally { setBusy(false); }
  });
  function bindResizer(id, variable, minimum, maximum) { const resizer = $(id); const workbench = $('workbench'); const current = () => parseInt(getComputedStyle(workbench).getPropertyValue(variable), 10) || minimum; const set = value => { const next = Math.max(minimum, Math.min(maximum, value)); workbench.style.setProperty(variable, `${next}px`); resizer.setAttribute('aria-valuenow', String(next)); }; const direction = variable === '--library-width' ? 1 : -1; let startX = 0; let startValue = 0; const finish = () => { resizer.classList.remove('active'); document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', finish); }; const move = event => set(startValue + direction * (event.clientX - startX)); resizer.setAttribute('aria-valuemin', minimum); resizer.setAttribute('aria-valuemax', maximum); set(current()); resizer.addEventListener('pointerdown', event => { startX = event.clientX; startValue = current(); resizer.classList.add('active'); document.addEventListener('pointermove', move); document.addEventListener('pointerup', finish); }); resizer.addEventListener('keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); if (event.key === 'Home') return set(minimum); if (event.key === 'End') return set(maximum); set(current() + direction * (event.key === 'ArrowRight' ? 8 : -8)); }); }
  bindResizer('library-resizer', '--library-width', 220, 420); bindResizer('inspector-resizer', '--inspector-width', 300, 520); void refreshDataFetcherState();
}

if (typeof module === 'object' && module.exports) module.exports = {parseAndFormatDate, defaultDateRange, splitList, buildFetchRequest, resultSummary, resultViewModel, chartViewModel, credentialStatusText, dataSettingsFailure, assetIndexViewModel, parseServiceResponse};
if (typeof document !== 'undefined' && document.getElementById) initializePage();
