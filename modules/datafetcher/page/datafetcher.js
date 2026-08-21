function parseAndFormatDate(value) {
  const match = /^(\d{4})([-/])(\d{2})\2(\d{2})$/.exec(String(value).trim());
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[3]), Number(match[4])];
  const date = new Date(Date.UTC(year, month - 1, day));
  if (year < 1 || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  const iso = `${match[1]}-${match[3]}-${match[4]}`;
  return {iso, display: iso.replaceAll('-', '/')};
}

function formatDateTyping(value) {
  const digits = String(value || '').replace(/\D/g, '').slice(0, 8);
  if (digits.length <= 4) return digits;
  if (digits.length <= 6) return `${digits.slice(0, 4)}/${digits.slice(4)}`;
  return `${digits.slice(0, 4)}/${digits.slice(4, 6)}/${digits.slice(6)}`;
}

function defaultDateRange(now = new Date()) {
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  // 日频行情在周末不能形成新的观测。默认取最近工作日，避免首次打开
  // 数据页就在服务端被拒绝为“未来日期”。交易所节假日仍由服务端日历校验。
  while (end.getDay() === 0 || end.getDay() === 6) end.setDate(end.getDate() - 1);
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

function nearestWeekday(value) {
  const date = new Date(`${value.iso}T00:00:00Z`);
  while (date.getUTCDay() === 0 || date.getUTCDay() === 6) date.setUTCDate(date.getUTCDate() - 1);
  const iso = date.toISOString().slice(0, 10);
  return {iso, display: iso.replaceAll('-', '/')};
}

function normalizeCurrentWeekendEndDate(value) {
  // A manually typed Saturday or Sunday is no more observable than today's
  // weekend default.  Normalise every daily end date before it reaches the
  // Host so a valid-looking calendar entry cannot become a rejected request.
  return nearestWeekday(value);
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

function cacheDecisionLabel(value) {
  return ({cache_hit: '使用已保存数据', cache_revalidated: '已校验保存数据', cache_rebound: '已更新保存数据', provider_fetch: '实时获取', force_refresh: '实时获取'}[value] || '已完成');
}

function buildFetchRequest(values) {
  const start = parseAndFormatDate(values.startDate);
  const endInput = parseAndFormatDate(values.endDate);
  const end = endInput && normalizeCurrentWeekendEndDate(endInput, values.now);
  if (!start || !end) throw new Error('日期必须为真实的yyyy/mm/dd或yyyy-mm-dd。');
  if (start.iso > end.iso) throw new Error('开始日期不得晚于结束日期。');
  const assetIds = splitList(values.assetIds).map(item => item.toUpperCase());
  const fields = splitList(values.fields).map(item => item.toLowerCase());
  const providers = splitList(values.providerPriority).map(item => item.toLowerCase());
  if (!assetIds.length) throw new Error('请至少填写一个资产标识。');
  const invalidAsset = assetIds.find(item => !/^\d{6}\.(?:SH|SZ)$/.test(item));
  if (invalidAsset) throw new Error(`资产标识“${invalidAsset}”格式不正确，请使用000300.SH或399001.SZ。`);
  if (!fields.length) throw new Error('请至少填写一个字段。');
  if (providers.length !== 1) throw new Error('请选择一个数据服务。');
  const request = {asset_ids: assetIds, start_date: start.iso, end_date: end.iso, fields, provider: providers[0], frequency: values.frequency, adjustment: values.adjustment, cache_policy: values.cachePolicy, offline: Boolean(values.offline)};
  if (String(values.localCsv || '').trim()) request.local_csv = String(values.localCsv).trim();
  return request;
}

function resultSummary(data) {
  const ref = data.data_asset_ref || {};
  return [{label: '数据来源', value: cacheDecisionLabel(data.cache_decision)}, {label: '记录数', value: ref.row_count ?? '暂无'}, {label: '保存状态', value: ref.data_asset_id || ref.content_hash ? '已保存到当前任务' : '待保存'}];
}

async function parseServiceResponse(response) {
  const data = await response.json();
  if (!response.ok || !data.ok) {
    const error = new Error(data.error?.message || data.message || '请求失败');
    error.payload = data;
    throw error;
  }
  return data;
}

function initializePage() {
  const $ = id => document.getElementById(id);
  const state = (text, kind = '') => { $('canvas-state').textContent = text; $('state-dot').className = `state-dot ${kind}`; };
  const message = (text, kind = '') => { const node = $('notice'); node.textContent = text; node.className = `notice ${kind}`; node.hidden = !text; $('form-message').textContent = text; $('form-message').className = `form-message ${kind === 'failed' ? 'error' : ''}`; };
  const clearFieldErrors = () => {
    for (const id of ['asset-ids', 'start-date', 'end-date', 'fields']) $(id).removeAttribute('aria-invalid');
    $('asset-ids-error').textContent = '';
  };
  const showInputError = text => {
    const targets = /资产标识/.test(text) ? ['asset-ids'] : /开始日期/.test(text) ? ['start-date'] : /结束日期|日期/.test(text) ? ['start-date', 'end-date'] : /字段/.test(text) ? ['fields'] : [];
    targets.forEach(id => $(id).setAttribute('aria-invalid', 'true'));
    if (targets.includes('asset-ids')) $('asset-ids-error').textContent = text;
    $(targets[0])?.focus();
  };
  const setBusy = busy => { $('submit-request').disabled = busy; $('submit-request').textContent = busy ? '正在执行请求…' : '获取数据'; };
  const requestJson = async (url, options) => parseServiceResponse(await fetch(url, options));
  const normalizeDate = input => { const parsed = parseAndFormatDate(input.value); input.setCustomValidity(parsed ? '' : '日期必须为真实的yyyy/mm/dd或yyyy-mm-dd'); if (parsed) input.value = parsed.display; return parsed; };
  const assetStore = new Map();
  function renderAssets() {
    const query = $('asset-search').value.trim().toLowerCase(); const entries = [...assetStore.values()].filter(item => JSON.stringify(item).toLowerCase().includes(query)); const list = $('asset-list'); list.replaceChildren();
    if (!entries.length) { list.innerHTML = '<p class="empty-list">当前任务尚无数据资产。</p>'; return; }
    entries.forEach(item => {
      const ref = item.data_asset_ref || item; const id = ref.data_asset_id || ref.content_hash || item.id;
      if (!id) return;
      const entry = document.createElement('div'); entry.className = 'asset-entry';
      const select = document.createElement('button'); select.type = 'button'; select.className = 'asset-select'; select.innerHTML = '<strong></strong><span></span>';
      const coverage = ref.coverage || {}; const range = [coverage.start_date, coverage.end_date].filter(Boolean).join('至');
      select.querySelector('strong').textContent = (ref.asset_ids || item.asset_ids || []).join('、') || '已保存数据'; select.querySelector('span').textContent = `${ref.row_count ?? '—'}行${range ? `，${range}` : ''}`;
      select.addEventListener('click', () => { const ids = ref.asset_ids || item.asset_ids; if (Array.isArray(ids) && ids.length) $('asset-ids').value = ids.join('\n'); });
      const download = document.createElement('a'); download.className = 'asset-download'; download.href = `/api/assets/${encodeURIComponent(id)}/download`; download.textContent = ref.media_type === 'application/json' ? '下载JSON' : '下载CSV';
      entry.append(select, download); list.append(entry);
    });
  }
  function addAsset(item) { const ref = item?.data_asset_ref || item; const key = ref?.data_asset_id || ref?.content_hash; if (key) assetStore.set(key, item); renderAssets(); }
  function appendJsonSection(parent, title, value, description) { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = `<h3>${title}</h3><p>${description}</p><pre class="json-block"></pre>`; section.querySelector('pre').textContent = JSON.stringify(value ?? null, null, 2); parent.append(section); }
  function renderResult(data) {
    const content = $('result-content'); content.replaceChildren(); const metrics = document.createElement('div'); metrics.className = 'result-grid'; resultSummary(data).forEach(metric => { const node = document.createElement('div'); node.className = 'result-metric'; node.innerHTML = '<span></span><strong></strong>'; node.querySelector('span').textContent = metric.label; node.querySelector('strong').textContent = String(metric.value); metrics.append(node); }); content.append(metrics);
    const asset = data.data_asset_ref || {};
    appendJsonSection(content, '数据范围', {标的: asset.asset_ids ?? [], 起止日期: asset.coverage ?? null, 记录数: asset.row_count ?? null}, '展示本次已保存数据的标的、日期范围与记录数。');
    appendJsonSection(content, '质量报告', data.quality_report, '观测行有效性与日历完整性分开显示；未接入交易日历时完整性为unverified，不推断节假日缺口。');
    appendJsonSection(content, '服务记录', {数据服务: data.provider_calls ?? [], 用量: data.quota_usage ?? null, 提示: data.warnings ?? []}, '展示本次数据服务调用、用量与提示。');
    if (Array.isArray(data.data_preview) && data.data_preview.length) { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = '<h3>数据预览</h3><p>仅展示服务返回的预览行。</p><div class="preview-table-wrap"><table class="preview-table"><thead></thead><tbody></tbody></table></div>'; const columns = [...new Set(data.data_preview.flatMap(row => Object.keys(row)))]; const head = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('th'); cell.textContent = key; head.append(cell); }); section.querySelector('thead').append(head); data.data_preview.forEach(row => { const tr = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('td'); cell.textContent = row[key] ?? ''; tr.append(cell); }); section.querySelector('tbody').append(tr); }); content.append(section); }
    $('empty-canvas').hidden = true; content.hidden = false;
  }
  async function loadAssets() { try { const data = await requestJson('/api/assets'); const items = Array.isArray(data.assets) ? data.assets : Array.isArray(data) ? data : []; items.forEach(addAsset); } catch (_) { renderAssets(); } }
  async function loadStatus() { try { const data = await requestJson('/api/status'); $('service-status').textContent = data.status === 'available' ? '本机服务已就绪' : (data.status || '服务状态未知'); const credentials = data.credential_status; $('secret-status').textContent = credentials && typeof credentials === 'object' ? Object.entries(credentials).map(([name, value]) => `${name}: ${value}`).join('；') : (credentials || '服务未返回凭据状态。'); } catch (_) { $('service-status').textContent = '本机服务未启动'; $('secret-status').textContent = '无法读取凭据状态。'; } }
  const defaults = defaultDateRange();
  if (!$('start-date').value.trim()) $('start-date').value = defaults.start;
  if (!$('end-date').value.trim()) $('end-date').value = defaults.end;
  for (const id of ['start-date', 'end-date']) {
    const input = $(id);
    input.addEventListener('input', () => {
      input.setCustomValidity('');
      input.value = formatDateTyping(input.value);
    });
    input.addEventListener('paste', event => {
      const text = event.clipboardData?.getData('text');
      const parsed = parseAndFormatDate(text);
      if (!parsed) return;
      event.preventDefault();
      input.value = parsed.display;
      input.setCustomValidity('');
    });
    input.addEventListener('blur', () => { if (input.value.trim()) normalizeDate(input); });
  }
  $('asset-search').addEventListener('input', renderAssets); $('refresh-assets').addEventListener('click', loadAssets);
  $('request-form').addEventListener('submit', async event => { event.preventDefault(); clearFieldErrors(); let request; try { request = buildFetchRequest({assetIds: $('asset-ids').value, startDate: $('start-date').value, endDate: $('end-date').value, fields: $('fields').value, frequency: $('frequency').value, adjustment: $('adjustment').value, providerPriority: $('provider-priority').value, cachePolicy: $('cache-policy').value, offline: $('offline').checked, localCsv: $('local-csv').value}); $('asset-ids').value = request.asset_ids.join('\n'); $('start-date').value = request.start_date.replaceAll('-', '/'); $('end-date').value = request.end_date.replaceAll('-', '/'); } catch (error) { state('请求配置错误', 'failed'); message(error.message, 'failed'); showInputError(error.message); return; }
    state('正在检查数据与缓存', 'running'); message('正在获取数据。'); setBusy(true); $('result-content').hidden = true; $('empty-canvas').hidden = false;
    try { const data = await requestJson('/api/fetch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request)}); renderResult(data); addAsset(data); $('run-id').textContent = data.data_fetch_run_id || ''; state('请求完成', 'complete'); message(`请求完成：${data.cache_decision || '服务未返回缓存决策'}。`); } catch (error) { if (error.payload) { renderResult(error.payload); $('run-id').textContent = error.payload.data_fetch_run_id || ''; } state('请求失败', 'failed'); message(error.message, 'failed'); } finally { setBusy(false); }
  });
  function bindResizer(id, variable, minimum, maximum) { const resizer = $(id); const workbench = $('workbench'); const current = () => parseInt(getComputedStyle(workbench).getPropertyValue(variable), 10) || minimum; const set = value => { const next = Math.max(minimum, Math.min(maximum, value)); workbench.style.setProperty(variable, `${next}px`); resizer.setAttribute('aria-valuenow', String(next)); }; const direction = variable === '--library-width' ? 1 : -1; let startX = 0; let startValue = 0; const finish = () => { resizer.classList.remove('active'); document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', finish); }; const move = event => set(startValue + direction * (event.clientX - startX)); resizer.setAttribute('aria-valuemin', minimum); resizer.setAttribute('aria-valuemax', maximum); resizer.addEventListener('pointerdown', event => { startX = event.clientX; startValue = current(); resizer.classList.add('active'); document.addEventListener('pointermove', move); document.addEventListener('pointerup', finish); }); resizer.addEventListener('keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); if (event.key === 'Home') return set(minimum); if (event.key === 'End') return set(maximum); set(current() + direction * (event.key === 'ArrowRight' ? 8 : -8)); }); }
  bindResizer('library-resizer', '--library-width', 220, 420); bindResizer('inspector-resizer', '--inspector-width', 300, 520); loadStatus(); loadAssets();
}

if (typeof module === 'object' && module.exports) module.exports = {parseAndFormatDate, formatDateTyping, defaultDateRange, nearestWeekday, normalizeCurrentWeekendEndDate, splitList, buildFetchRequest, resultSummary, parseServiceResponse};
if (typeof document !== 'undefined' && document.getElementById) initializePage();
