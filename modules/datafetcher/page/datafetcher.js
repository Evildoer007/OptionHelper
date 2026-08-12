function parseAndFormatDate(value) {
  const match = /^(\d{4})([-/])(\d{2})\2(\d{2})$/.exec(String(value).trim());
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[3]), Number(match[4])];
  const date = new Date(Date.UTC(year, month - 1, day));
  if (year < 1 || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  const iso = `${match[1]}-${match[3]}-${match[4]}`;
  return {iso, display: iso.replaceAll('-', '/')};
}

function splitList(value) { return String(value || '').split(/[\n,]/).map(item => item.trim()).filter(Boolean); }

function buildFetchRequest(values) {
  const start = parseAndFormatDate(values.startDate);
  const end = parseAndFormatDate(values.endDate);
  if (!start || !end) throw new Error('日期必须为真实的yyyy/mm/dd或yyyy-mm-dd。');
  if (start.iso > end.iso) throw new Error('开始日期不得晚于结束日期。');
  const assetIds = splitList(values.assetIds);
  const fields = splitList(values.fields);
  const sourcePriority = splitList(values.providerPriority);
  if (!assetIds.length) throw new Error('请至少填写一个资产标识。');
  if (!fields.length) throw new Error('请至少填写一个字段。');
  if (!sourcePriority.length) throw new Error('请至少填写一个Provider。');
  const request = {asset_ids: assetIds, start_date: start.iso, end_date: end.iso, fields, frequency: values.frequency, adjustment: values.adjustment, source_priority: sourcePriority, cache_policy: values.cachePolicy, offline: Boolean(values.offline)};
  if (String(values.localCsv || '').trim()) request.local_csv = String(values.localCsv).trim();
  return request;
}

function resultSummary(data) {
  const ref = data.data_asset_ref || {};
  return [{label: '缓存决策', value: data.cache_decision || '服务未返回'}, {label: '数据行数', value: ref.row_count ?? '服务未返回'}, {label: '资产引用', value: ref.data_asset_id || ref.content_hash || '服务未返回'}];
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
  const setBusy = busy => { $('submit-request').disabled = busy; $('submit-request').textContent = busy ? '正在执行请求…' : '实时获取数据'; };
  const requestJson = async (url, options) => parseServiceResponse(await fetch(url, options));
  const normalizeDate = input => { const parsed = parseAndFormatDate(input.value); input.setCustomValidity(parsed ? '' : '日期必须为真实的yyyy/mm/dd或yyyy-mm-dd'); if (parsed) input.value = parsed.display; return parsed; };
  const assetStore = new Map();
  function renderAssets() {
    const query = $('asset-search').value.trim().toLowerCase(); const entries = [...assetStore.values()].filter(item => JSON.stringify(item).toLowerCase().includes(query)); const list = $('asset-list'); list.replaceChildren();
    if (!entries.length) { list.innerHTML = '<p class="empty-list">尚无已加载数据对象。</p>'; return; }
    entries.forEach(item => {
      const ref = item.data_asset_ref || item; const id = ref.data_asset_id || ref.content_hash || item.id;
      if (!id) return;
      const entry = document.createElement('div'); entry.className = 'asset-entry';
      const select = document.createElement('button'); select.type = 'button'; select.className = 'asset-select'; select.innerHTML = '<strong></strong><span></span>';
      select.querySelector('strong').textContent = id; select.querySelector('span').textContent = `${ref.row_count ?? '—'}行 · ${ref.content_hash || '无Hash'}`;
      select.addEventListener('click', () => { const ids = ref.asset_ids || item.asset_ids; if (Array.isArray(ids) && ids.length) $('asset-ids').value = ids.join('\n'); });
      const download = document.createElement('a'); download.className = 'asset-download'; download.href = `/api/assets/${encodeURIComponent(id)}/download`; download.textContent = ref.media_type === 'application/json' ? '下载JSON' : '下载CSV';
      entry.append(select, download); list.append(entry);
    });
  }
  function addAsset(item) { const ref = item?.data_asset_ref || item; const key = ref?.data_asset_id || ref?.content_hash; if (key) assetStore.set(key, item); renderAssets(); }
  function appendJsonSection(parent, title, value, description) { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = `<h3>${title}</h3><p>${description}</p><pre class="json-block"></pre>`; section.querySelector('pre').textContent = JSON.stringify(value ?? null, null, 2); parent.append(section); }
  function renderResult(data) {
    const content = $('result-content'); content.replaceChildren(); const metrics = document.createElement('div'); metrics.className = 'result-grid'; resultSummary(data).forEach(metric => { const node = document.createElement('div'); node.className = 'result-metric'; node.innerHTML = '<span></span><strong></strong>'; node.querySelector('span').textContent = metric.label; node.querySelector('strong').textContent = String(metric.value); metrics.append(node); }); content.append(metrics);
    appendJsonSection(content, '数据资产引用与覆盖范围', {data_asset_ref: data.data_asset_ref ?? null, coverage: data.data_asset_ref?.coverage ?? null}, '服务返回的DataAssetRef与覆盖范围。');
    appendJsonSection(content, '质量报告', data.quality_report, '观测行有效性与日历完整性分开显示；未接入交易日历时完整性为unverified，不推断节假日缺口。');
    appendJsonSection(content, 'Provider调用与额度', {provider_calls: data.provider_calls ?? [], quota_usage: data.quota_usage ?? null, warnings: data.warnings ?? []}, '仅显示本次服务实际记录的Provider调用、额度与警告。');
    if (Array.isArray(data.data_preview) && data.data_preview.length) { const section = document.createElement('section'); section.className = 'result-section'; section.innerHTML = '<h3>数据预览</h3><p>仅展示服务返回的预览行。</p><div class="preview-table-wrap"><table class="preview-table"><thead></thead><tbody></tbody></table></div>'; const columns = [...new Set(data.data_preview.flatMap(row => Object.keys(row)))]; const head = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('th'); cell.textContent = key; head.append(cell); }); section.querySelector('thead').append(head); data.data_preview.forEach(row => { const tr = document.createElement('tr'); columns.forEach(key => { const cell = document.createElement('td'); cell.textContent = row[key] ?? ''; tr.append(cell); }); section.querySelector('tbody').append(tr); }); content.append(section); }
    appendJsonSection(content, '运行记录', data, '完整服务响应，便于追溯Provider调用和警告。'); $('empty-canvas').hidden = true; content.hidden = false;
  }
  async function loadAssets() { try { const data = await requestJson('/api/assets'); const items = Array.isArray(data.assets) ? data.assets : Array.isArray(data) ? data : []; items.forEach(addAsset); } catch (_) { renderAssets(); } }
  async function loadStatus() { try { const data = await requestJson('/api/status'); $('service-status').textContent = data.status === 'available' ? '本机服务已就绪' : (data.status || '服务状态未知'); const credentials = data.credential_status; $('secret-status').textContent = credentials && typeof credentials === 'object' ? Object.entries(credentials).map(([name, value]) => `${name}: ${value}`).join('；') : (credentials || '服务未返回凭据状态。'); } catch (_) { $('service-status').textContent = '本机服务未启动'; $('secret-status').textContent = '无法读取凭据状态。'; } }
  for (const id of ['start-date', 'end-date']) $(id).addEventListener('blur', () => { if ($(id).value.trim()) normalizeDate($(id)); });
  $('asset-search').addEventListener('input', renderAssets); $('refresh-assets').addEventListener('click', loadAssets);
  $('request-form').addEventListener('submit', async event => { event.preventDefault(); let request; try { request = buildFetchRequest({assetIds: $('asset-ids').value, startDate: $('start-date').value, endDate: $('end-date').value, fields: $('fields').value, frequency: $('frequency').value, adjustment: $('adjustment').value, providerPriority: $('provider-priority').value, cachePolicy: $('cache-policy').value, offline: $('offline').checked, localCsv: $('local-csv').value}); $('start-date').value = parseAndFormatDate($('start-date').value).display; $('end-date').value = parseAndFormatDate($('end-date').value).display; } catch (error) { state('请求配置错误', 'failed'); message(error.message, 'failed'); return; }
    state('正在执行缓存与Provider检查', 'running'); message('服务正在处理请求。'); setBusy(true); $('result-content').hidden = true; $('empty-canvas').hidden = false;
    try { const data = await requestJson('/api/fetch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request)}); renderResult(data); addAsset(data); $('run-id').textContent = data.data_fetch_run_id || ''; state('请求完成', 'complete'); message(`请求完成：${data.cache_decision || '服务未返回缓存决策'}。`); } catch (error) { if (error.payload) { renderResult(error.payload); $('run-id').textContent = error.payload.data_fetch_run_id || ''; } state('请求失败', 'failed'); message(error.message, 'failed'); } finally { setBusy(false); }
  });
  function bindResizer(id, variable, minimum, maximum) { const resizer = $(id); const workbench = $('workbench'); const current = () => parseInt(getComputedStyle(workbench).getPropertyValue(variable), 10) || minimum; const set = value => { const next = Math.max(minimum, Math.min(maximum, value)); workbench.style.setProperty(variable, `${next}px`); resizer.setAttribute('aria-valuenow', String(next)); }; const direction = variable === '--library-width' ? 1 : -1; let startX = 0; let startValue = 0; const finish = () => { resizer.classList.remove('active'); document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', finish); }; const move = event => set(startValue + direction * (event.clientX - startX)); resizer.setAttribute('aria-valuemin', minimum); resizer.setAttribute('aria-valuemax', maximum); resizer.addEventListener('pointerdown', event => { startX = event.clientX; startValue = current(); resizer.classList.add('active'); document.addEventListener('pointermove', move); document.addEventListener('pointerup', finish); }); resizer.addEventListener('keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); if (event.key === 'Home') return set(minimum); if (event.key === 'End') return set(maximum); set(current() + direction * (event.key === 'ArrowRight' ? 8 : -8)); }); }
  bindResizer('library-resizer', '--library-width', 220, 420); bindResizer('inspector-resizer', '--inspector-width', 300, 520); loadStatus(); loadAssets();
}

if (typeof module === 'object' && module.exports) module.exports = {parseAndFormatDate, splitList, buildFetchRequest, resultSummary, parseServiceResponse};
if (typeof document !== 'undefined' && document.getElementById) initializePage();
