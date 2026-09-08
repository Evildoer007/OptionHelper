(() => {
  const $ = id => document.getElementById(id);
  const state = { catalog: null, source: null, selected: new Set(), runRefs: {}, quoteItems: [], ready: false, loadingSources: false, quoteMode: false, previewReportRunId: null, pendingReport: null, sourceRequestRevision: 0, interactionRevision: 0, activeReportRequestId: null };
  let bootPromise = null;
  let initialized = false;
  const reportEditor = window.OptionHelperReportEditor?.createReportEditor({
    root: document,
    fetchImpl: (...args) => fetch(...args),
    notifySaved: data => notifyReportSaved(data),
    echarts: window.echarts,
  });
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const selectedValue = name => name === 'format' ? 'html' : $('reportTemplate').value.replace(/^multi/, '');
  const selectedDelivery = () => $('reportTemplate').value.startsWith('multi') ? 'comparison' : 'single';
  const hostedMode = new URLSearchParams(location.search).get('host') === 'optdesk';
  const selectedModules = () => [...document.querySelectorAll('[data-module]:checked')]
    .map(node => node.dataset.module).filter(module => !hostedMode || module !== 'recommender');
  if (hostedMode) {
    const recommendation = document.querySelector('input[data-module="recommender"]');
    if (recommendation) {
      recommendation.checked = false;
      recommendation.disabled = true;
      const option = recommendation.closest?.('label');
      if (option) option.hidden = true;
    }
  }
  const runRefFields = ['module','tenant_id','task_id','run_id','expected_result_file_hash','expected_artifact_manifest_hash'];
  const formalRunRef = value => Object.fromEntries(runRefFields.map(field => [field, value[field]]));
  const identifier = prefix => {
    const timestamp = new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 17);
    const random = globalThis.crypto?.randomUUID?.().replaceAll('-', '') || Math.random().toString(36).slice(2);
    return `${prefix}_${timestamp}_${random.slice(0, 16)}`;
  };
  const requestIdentifier = prefix => globalThis.crypto?.randomUUID?.() || `${identifier(prefix)}_${Math.random().toString(36).slice(2)}`;

  function attemptFor(selection) {
    const stable = {...selection}; delete stable.report_run_id;
    const fingerprint = JSON.stringify(stable);
    if (state.pendingReport?.fingerprint === fingerprint) {
      selection.report_run_id = state.pendingReport.reportRunId;
      return state.pendingReport;
    }
    const attempt = {fingerprint, requestId:requestIdentifier('report'), reportRunId:identifier(selection.output_type)};
    state.pendingReport = attempt; selection.report_run_id = attempt.reportRunId;
    return attempt;
  }
  function settleAttempt(attempt) {
    if (state.pendingReport?.requestId === attempt.requestId) state.pendingReport = null;
  }
  async function readDeliveryResponse(response, fallbackMessage) {
    const data = await response.json();
    if (response.ok && data.ok) {
      notifyReportSaved(data);
      return data;
    }
    const error = new Error(data.message || fallbackMessage);
    // A lost response may already have saved a report. Only a confirmed final
    // failure releases the claim; transport uncertainty retries the same one.
    error.operationFinished = ['failed', 'cancelled', 'interrupted'].includes(
      data.operation_state || data.error || data.failure_code,
    );
    throw error;
  }
  function notifyReportSaved(data) {
    const reference = data?.report_run_ref || data;
    if (data?.ok !== true || typeof reference?.task_id !== 'string' || !reference.task_id
        || typeof reference?.report_run_id !== 'string' || !reference.report_run_id) return;
    if (!window.parent || window.parent === window) return;
    const query = new URLSearchParams(location.search);
    const nonce = query.get('bridge_nonce');
    if (query.get('host') !== 'optdesk' || !nonce) return;
    window.parent.postMessage({type:'optionhelper.report-saved', module:'reporter', bridge_nonce:nonce,
      task_id:reference.task_id, report_run_id:reference.report_run_id}, location.origin);
  }
  function setPreviewEditorSource(data, fallbackReportRunId = '') {
    const reportRunId = data?.report_run_ref?.report_run_id || data?.report_run_id || fallbackReportRunId;
    state.previewReportRunId = typeof reportRunId === 'string' && reportRunId ? reportRunId : null;
    $('editReportButton').hidden = !state.previewReportRunId || !reportEditor;
  }
  function restoreSelectionFocus(target) {
    if (!target) return;
    const candidates = [...document.querySelectorAll('[data-select-candidate]')];
    const quoteControls = [...document.querySelectorAll('[data-quote-contract]')];
    const node = target.candidateId
      ? candidates.find(item => item.dataset.selectCandidate === target.candidateId)
      : quoteControls.find(item => item.dataset.quoteContract === target.quoteContract);
    node?.focus();
  }

  function notice(text, tone = '') { $('notice').textContent = text; $('notice').dataset.tone = tone; }
  function markReportInputChanged({quiet = false} = {}) {
    state.interactionRevision += 1;
    state.pendingReport = null;
    state.activeReportRequestId = null;
    if (!quiet && !$('previewPanel').hidden) notice('报告选项已修改；当前仍展示上一次完成报告，请重新生成。');
  }
  function isCurrentReportAttempt(attempt, revision) {
    return state.activeReportRequestId === attempt.requestId
      && state.pendingReport?.requestId === attempt.requestId
      && state.interactionRevision === revision;
  }
  function activeSource() { return state.catalog?.sources?.find(item => item.source_id === $('sourceSelect').value) || null; }
  function moduleStatus(value) {
    if (value?.status === 'succeeded' || value?.status === 'ready' || value?.expected_result_file_hash) return 'ready';
    return value?.status === 'failed' ? 'failed' : '缺失';
  }
  function candidateIdentity(candidate) {
    const underlyings = (candidate?.underlyings || []).join('、');
    return [underlyings, termSummary(candidate?.display_terms, hostedMode)].filter(Boolean).join('，');
  }
  function termSummary(terms, compact = false) {
    return (Array.isArray(terms) ? terms : []).filter(term => typeof term.value === 'number' && Number.isFinite(term.value))
      .map(term => {
        if (compact && term.label === '期限' && term.unit === '年' && term.value > 0) {
          // Decide exact units from the frozen number before formatting it.
          for (const [scale, unit] of [[1, '年'], [12, '个月'], [365, '自然日']]) {
            const count = term.value * scale;
            const integer = Math.round(count);
            if (integer > 0 && Math.abs(count - integer) < 1e-9) return `期限${integer}${unit}`;
          }
        }
        return `${term.label}${term.value.toLocaleString('zh-CN', {maximumSignificantDigits:8, useGrouping:false})}${term.unit || ''}`;
      }).join('，');
  }
  function runTime(value) {
    if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return '时间未记录';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间未记录';
    const parts = Object.fromEntries(new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}).formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
  }
  function moduleRunIdentity(run, candidate) {
    const display = candidate?.run_display?.[run?.run_id];
    return [runTime(display?.run_time), termSummary(display?.terms, hostedMode)].filter(Boolean).join('，');
  }
  const moduleNames = {payoff:'收益结构', pricing:'估值定价', backtest:'历史回测'};
  function sourceSummary(source) {
    const candidates = source.candidates || [];
    const unique = values => [...new Set(values.filter(Boolean))].join('、');
    const products = unique(candidates.map(candidate => candidate.product_name)) || '已保存分析';
    const underlyings = unique(candidates.flatMap(candidate => candidate.underlyings || []));
    const modules = unique(candidates.flatMap(candidate => Object.keys(candidate.module_run_options || {}).filter(module => candidate.module_run_options[module]?.length).map(module => moduleNames[module])));
    const terms = unique(candidates.map(candidate => termSummary(candidate.display_terms, hostedMode)));
    const times = [...new Set(candidates.flatMap(candidate => Object.values(candidate.module_run_options || {}).flat().map(run => runTime(candidate.run_display?.[run.run_id]?.run_time))))].sort();
    const known = times.filter(time => time !== '时间未记录');
    const timeSummary = known.length > 1 ? `${known[0]}至${known.at(-1)}` : known[0] || '时间未记录';
    return [products, underlyings, modules, terms, timeSummary, known.length && times.includes('时间未记录') ? '部分时间未记录' : ''].filter(Boolean).join('，');
  }
  function distinctLabels(labels) {
    return labels.map((label, index) => labels.filter(item => item === label).length > 1 ? `${label}，记录${index + 1}` : label);
  }
  function candidateAudit(candidate, source) {
    const audit = {source_id:source.source_id, task_id:source.task_id, candidate_id:candidate.candidate_id,
      product_id:candidate.product_id, rule_revision:candidate.rule_revision,
      module_run_refs:Object.fromEntries(Object.entries(candidate.module_run_options || {}).map(([module, runs]) => [module, runs.map(formalRunRef)])),
      run_display:candidate.run_display || {}, resolved_contract_snapshot:candidate.resolved_contract_snapshot};
    return `<details class="run-audit"><summary>来源与运行审计</summary><pre>${esc(JSON.stringify(audit, null, 2))}</pre></details>`;
  }
  function clearSourceState() {
    state.catalog = null; state.source = null; state.selected.clear(); state.runRefs = {}; state.quoteItems = []; renderSources();
    state.previewReportRunId = null; $('previewPanel').hidden = true; $('reportPreview').removeAttribute('src'); $('downloadLink').removeAttribute('href'); $('editReportButton').hidden = true; $('childReports').innerHTML = '';
  }
  function updateControls() {
    const outputType = selectedValue('outputType'); const format = selectedValue('format');
    const isQuote = outputType === 'quote';
    $('referenceQuotePanel').hidden = !isQuote;
    $('moduleGroup').hidden = isQuote;
    const count = isQuote ? state.quoteItems.length : state.selected.size; const moduleCount = selectedModules().length;
    const comparison = selectedDelivery() === 'comparison';
    $('selectionCount').textContent = isQuote ? (count ? `已加入${count}条报价行` : '未加入报价行') : (count ? `已选择${count}项分析来源` : '未选择分析来源');
    const unavailableReason = state.activeReportRequestId
      ? '报告正在生成，请等待当前请求完成。'
      : state.loadingSources
      ? '正在核对报告来源，请稍候。'
      : !state.ready
      ? '报告服务暂不可用，请稍后重试。'
      : !state.source
        ? '当前没有可用的分析结果，请先完成一项模块分析。'
        : !count
          ? (isQuote ? '请从已保存的参数运行中加入至少一条报价行。' : '请先选择至少一个分析结果。')
          : !isQuote && ((comparison && count < 2) || (!comparison && count > 1))
          ? (comparison ? '多产品模板需要至少两项分析来源。' : '已选多项来源，请选择多产品模板。')
          : !isQuote && !moduleCount
          ? '请至少选择一个报告模块。'
          : '';
    $('generateButton').disabled = Boolean(unavailableReason);
    $('generateButton').title = unavailableReason || '生成并保存报告';
  }
  function bindResizer(id, variable, direction, minimum, maximum) {
    const handle = $(id); const workbench = document.querySelector('.workbench');
    const current = () => parseFloat(getComputedStyle(workbench).getPropertyValue(variable)) || minimum;
    const apply = value => { const next = Math.max(minimum, Math.min(maximum, value)); workbench.style.setProperty(variable, `${next}px`); handle.setAttribute('aria-valuenow', String(next)); };
    handle.setAttribute('aria-valuemin', String(minimum)); handle.setAttribute('aria-valuemax', String(maximum)); handle.setAttribute('aria-valuenow', String(current()));
    handle.addEventListener('pointerdown', event => { const start = event.clientX; const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); handle.setPointerCapture(event.pointerId); const move = next => apply(initial + direction * (next.clientX - start)); handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', () => handle.removeEventListener('pointermove', move), {once:true}); });
    handle.addEventListener('keydown', event => { if (!['ArrowLeft','ArrowRight'].includes(event.key)) return; event.preventDefault(); const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); apply(initial + direction * (event.key === 'ArrowRight' ? 12 : -12)); });
  }
  function sourceRecency(source) {
    const timestamps = (source.candidates || []).flatMap(candidate =>
      Object.values(candidate.run_display || {}).map(display => {
        const value = display?.run_time;
        return typeof value === 'string' && /(Z|[+-]\d{2}:\d{2})$/.test(value) ? Date.parse(value) : NaN;
      })).filter(Number.isFinite);
    return timestamps.length ? Math.max(...timestamps) : -Infinity;
  }
  function renderSources() {
    const select = $('sourceSelect');
    const sources = [...(state.catalog?.sources || [])].sort((left, right) => {
      const a = sourceRecency(left), b = sourceRecency(right);
      return a === b ? 0 : a < b ? 1 : -1;
    });
    if (state.catalog) state.catalog = {...state.catalog, sources};
    const previous = state.source;
    const previousId = previous?.source_id || select.value;
    const previousRefs = Object.fromEntries((previous?.candidates || []).map(candidate => [candidate.candidate_id,
      Object.fromEntries(Object.entries(candidate.module_run_options || {}).map(([module, options]) => [module, state.runRefs[candidate.candidate_id]?.[module] || options[0]]))]));
    const labels = distinctLabels(sources.map(sourceSummary));
    const retained = sources.find(source => source.source_id === previousId);
    const missing = Boolean(previous && !retained);
    select.innerHTML = sources.length ? `${missing ? '<option value="">原来源不可用，请重新选择</option>' : ''}${sources.map((source, index) => `<option value="${esc(source.source_id)}">${esc(labels[index])}</option>`).join('')}` : '<option value="">暂无可用报告来源</option>';
    select.value = retained?.source_id || (missing ? '' : sources[0]?.source_id || '');
    select.disabled = !sources.length;
    state.source = activeSource(); state.runRefs = {};
    let lostSelection = missing;
    const candidates = state.source?.candidates || [];
    for (const id of [...state.selected]) {
      if (!candidates.some(candidate => candidate.candidate_id === id)) { state.selected.delete(id); lostSelection = true; }
    }
    if (retained && previous) for (const candidate of candidates) {
      for (const [module, oldRef] of Object.entries(previousRefs[candidate.candidate_id] || {})) {
        if (!oldRef) continue;
        const exact = (candidate.module_run_options?.[module] || []).find(ref => runRefFields.every(field => ref[field] === oldRef[field]));
        if (exact) (state.runRefs[candidate.candidate_id] ||= {})[module] = exact;
        else if (state.selected.has(candidate.candidate_id)) { state.selected.delete(candidate.candidate_id); lostSelection = true; }
      }
    }
    if (selectedValue('outputType') !== 'quote') state.quoteItems = [];
    else state.quoteItems = state.quoteItems.filter(item => {
      const candidate = sources.find(source => source.source_id === item.source_id)?.candidates?.find(candidate => candidate.candidate_id === item.candidate_id);
      const available = candidate?.module_run_options?.[item.module]?.some(ref => runRefFields.every(field => ref[field] === item.module_run_ref[field]));
      if (!available) lostSelection = true;
      return available;
    });
    if (lostSelection) markReportInputChanged({quiet:true});
    renderCandidates();
    return lostSelection;
  }
  function quoteContractKey(item) { return `${item.source_id}/${item.candidate_id}`; }
  function renderQuoteItems() {
    const panel = $('quoteItems'); const isQuote = selectedValue('outputType') === 'quote';
    panel.hidden = !isQuote;
    if (!isQuote) { panel.innerHTML = ''; return; }
    panel.innerHTML = state.quoteItems.length
      ? `<div class="quote-items__head"><span>已选报价结构</span><span>${state.quoteItems.length}条</span></div><div class="quote-items__list">${state.quoteItems.map((item, index) => `<span class="quote-item">${esc(item.product_name)}，${esc(item.display_summary || '已选估值记录')}<button type="button" data-remove-quote="${index}" data-quote-contract="${esc(quoteContractKey(item))}" aria-label="移除报价行">移除</button></span>`).join('')}</div>`
      : '<div class="quote-items__head"><span>已选报价结构</span><span>从下方加入已保存估值运行</span></div>';
    panel.querySelectorAll('[data-remove-quote]').forEach(button => button.addEventListener('click', () => {
      const item = state.quoteItems[Number(button.dataset.removeQuote)];
      state.quoteItems.splice(Number(button.dataset.removeQuote), 1);
      markReportInputChanged();
      renderCandidates(item ? {quoteContract:quoteContractKey(item)} : null);
    }));
  }
  function quoteCandidate(candidate, source) {
    const rows = ['pricing'].map(module => {
      const options = Array.isArray(candidate.module_run_options?.[module]) ? candidate.module_run_options[module] : [];
      const labels = distinctLabels(options.map(run => moduleRunIdentity(run, candidate)));
      return options.map((run, index) => {
        const item = {source_id:source.source_id, candidate_id:candidate.candidate_id, module, module_run_ref:run};
        const contractKey = quoteContractKey(item);
        const added = state.quoteItems.some(existing => quoteContractKey(existing) === contractKey);
        return `<div class="quote-run"><div><strong>估值定价</strong><span>${esc(labels[index])}</span></div><button class="quote-add" type="button" data-quote-source="${esc(source.source_id)}" data-quote-candidate="${esc(candidate.candidate_id)}" data-quote-module="${module}" data-quote-index="${index}" data-quote-contract="${esc(contractKey)}" ${added ? 'disabled' : ''}>${added ? '该产品已加入' : '加入报价表'}</button></div>`;
      }).join('');
    }).join('');
    return `<article class="candidate" data-candidate="${esc(candidate.candidate_id)}"><div class="candidate-head"><div class="candidate-title">${esc(candidate.product_name)}<div class="candidate-subtitle">${esc(candidateIdentity(candidate))}</div></div></div>${rows || '<p class="missing-note">暂无可用于报价的已保存运行结果。</p>'}${candidateAudit(candidate, source)}</article>`;
  }
  function renderCandidates(focusTarget = null) {
    state.source = activeSource(); const source = state.source; const list = $('candidateList'); const isQuote = selectedValue('outputType') === 'quote';
    if (!source) { list.innerHTML = ''; $('sourceDetail').textContent = '当前任务尚无可用于生成报告的分析结果。'; renderQuoteItems(); updateControls(); return; }
    $('sourceDetail').innerHTML = esc(sourceSummary(source));
    if (isQuote) {
      const sources = state.catalog?.sources || [];
      list.innerHTML = sources.map(item => `<section class="quote-source"><h3>${esc(sourceSummary(item))}</h3>${(item.candidates || []).map(candidate => quoteCandidate(candidate, item)).join('')}</section>`).join('');
      list.querySelectorAll('[data-quote-candidate]').forEach(button => button.addEventListener('click', () => {
        const itemSource = sources.find(item => item.source_id === button.dataset.quoteSource);
        const candidate = itemSource?.candidates?.find(item => item.candidate_id === button.dataset.quoteCandidate);
        const module = button.dataset.quoteModule; const index = Number(button.dataset.quoteIndex);
        const run = candidate?.module_run_options?.[module]?.[index];
        if (!candidate || !run) return;
        const contractKey = quoteContractKey({source_id:itemSource.source_id, candidate_id:candidate.candidate_id});
        state.quoteItems.push({source_id:itemSource.source_id, candidate_id:candidate.candidate_id, module, module_run_ref:run, product_name:candidate.product_name, display_summary:[candidateIdentity(candidate), moduleRunIdentity(run, candidate)].filter(Boolean).join('，')});
        markReportInputChanged();
        renderCandidates({quoteContract:contractKey});
      }));
      renderQuoteItems(); updateControls(); restoreSelectionFocus(focusTarget); return;
    }
    list.innerHTML = (source.candidates || []).map(candidate => {
      const checked = state.selected.has(candidate.candidate_id);
      const runs = ['payoff','pricing','backtest'].map(module => {
        const options = Array.isArray(candidate.module_run_options?.[module]) ? candidate.module_run_options[module] : [];
        const run = candidate.module_run_refs?.[module] || options[0];
        const status = moduleStatus(run);
        const selectedIndex = options.indexOf(state.runRefs[candidate.candidate_id]?.[module]);
        const selected = selectedIndex >= 0 ? selectedIndex : 0;
        const labels = distinctLabels(options.map(item => moduleRunIdentity(item, candidate)));
        const selector = options.length > 1
          ? `<select class="run-choice" aria-label="选择${esc(moduleNames[module])}运行" data-candidate="${esc(candidate.candidate_id)}" data-module="${esc(module)}">${options.map((item, index) => `<option value="${index}" ${index === selected ? 'selected' : ''}>${esc(labels[index])}</option>`).join('')}</select>`
          : options.length === 1 ? `<span>${esc(labels[0])}</span>` : `<span>${esc(status)}</span>`;
        return `<div class="run-row" data-status="${esc(status)}"><strong>${({payoff:'收益结构',pricing:'估值定价',backtest:'历史回测'})[module]}</strong>${selector}</div>`;
      }).join('');
      return `<article class="candidate" data-candidate="${esc(candidate.candidate_id)}" data-selected="${checked}"><div class="candidate-head"><input type="checkbox" data-select-candidate="${esc(candidate.candidate_id)}" ${checked?'checked':''} aria-label="选择${esc(candidate.product_name)}"><div class="candidate-title">${esc(candidate.product_name)}<div class="candidate-subtitle">${esc(candidateIdentity(candidate))}</div></div></div><div class="run-grid">${runs}</div>${candidateAudit(candidate, source)}</article>`;
    }).join('');
    list.querySelectorAll('.candidate').forEach(card => card.querySelector('input').addEventListener('change', event => { const id = card.dataset.candidate; event.target.checked ? state.selected.add(id) : state.selected.delete(id); markReportInputChanged(); renderCandidates({candidateId:id}); }));
    list.querySelectorAll('.run-choice').forEach(select => select.addEventListener('change', event => {
      const candidate = source.candidates.find(item => item.candidate_id === event.target.dataset.candidate);
      const options = candidate?.module_run_options?.[event.target.dataset.module];
      if (!Array.isArray(options)) return;
      state.runRefs[event.target.dataset.candidate] = { ...(state.runRefs[event.target.dataset.candidate] || {}), [event.target.dataset.module]: options[Number(event.target.value)] };
      markReportInputChanged();
      updateControls();
    }));
    renderQuoteItems(); updateControls(); restoreSelectionFocus(focusTarget);
  }
  async function loadSources() {
    const revision = ++state.sourceRequestRevision;
    state.loadingSources = true; updateControls();
    if (!state.activeReportRequestId && $('previewPanel').hidden) notice('正在读取当前任务的分析结果。');
    const task = $('taskFilter').value.trim(); const quote = selectedValue('outputType') === 'quote'; let response; let data;
    try { response = await fetch(`/api/report-sources${task && !quote ? `?task_id=${encodeURIComponent(task)}` : ''}`); data = await response.json(); }
    catch (error) { if (revision !== state.sourceRequestRevision) return; state.loadingSources = false; state.ready = false; updateControls(); throw error; }
    if (revision !== state.sourceRequestRevision) return;
    state.loadingSources = false;
    if (!response.ok) { state.ready = false; updateControls(); notice(data.message || '来源读取失败，已保留原选择和报告，请重试。', 'error'); return; }
    state.ready = true; state.catalog = data;
    const lostSelection = renderSources();
    const excludedRuns = Number(data.source_diagnostics?.excluded_run_count || 0);
    if (lostSelection) notice('原选中的来源或运行已不可用，请重新选择；已完成报告仍可查看。', 'error');
    else if (excludedRuns > 0) notice(`已排除${excludedRuns}条未通过完整性或合同绑定校验的运行，请检查对应计算结果。`, 'error');
    else if (!state.activeReportRequestId && $('previewPanel').hidden) notice((data.sources || []).length ? '选择来源及需要纳入报告的分析内容。' : '当前任务尚无可用的分析结果。');
  }
  function changeOutputType() {
    const quoteMode = selectedValue('outputType') === 'quote';
    const scopeChanged = quoteMode !== state.quoteMode;
    state.quoteMode = quoteMode;
    markReportInputChanged();
    if (scopeChanged) loadSources().catch(error => notice(error.message, 'error'));
    else renderCandidates();
  }
  async function generate() {
    if (state.activeReportRequestId) return;
    if (state.loadingSources) { notice('正在核对报告来源，请稍候。'); return; }
    const source = state.source;
    if (!state.ready) { notice('报告服务暂不可用，请稍后重试。', 'error'); return; }
    if (!source) { notice('当前没有可用的分析结果。请先完成收益结构、估值定价或历史回测。', 'error'); return; }
    const type = selectedValue('outputType'); const format = selectedValue('format');
    if (type === 'quote' && !state.quoteItems.length) { notice('请从已保存的参数运行中加入至少一条报价行。', 'error'); return; }
    if (type !== 'quote' && !state.selected.size) { notice('请先选择至少一个分析结果。', 'error'); return; }
    const modules = selectedModules();
    if (type === 'quote') {
      const selection = { source_id: source.source_id, quote_items: state.quoteItems.map(item => ({source_id:item.source_id, candidate_id:item.candidate_id, module:item.module, module_run_ref:formalRunRef(item.module_run_ref)})), output_type: type, format, audience: 'professional', metadata: { title: '参考报价表' } };
      const attempt = attemptFor(selection);
      const interactionRevision = state.interactionRevision;
      state.activeReportRequestId = attempt.requestId;
      $('generateButton').disabled = true; notice('正在按已选合同快照整理参考报价。');
      try { const response = await fetch('/api/run', {method:'POST',headers:{'Content-Type':'application/json','X-OptionHelper-Request-Id':attempt.requestId},body:JSON.stringify({selection})}); const data = await readDeliveryResponse(response, '报价表生成失败'); if (!isCurrentReportAttempt(attempt,interactionRevision)) return; settleAttempt(attempt); setPreviewEditorSource(data, selection.report_run_id); notice('草稿已生成'); await reportEditor?.open(state.previewReportRunId); } catch (error) { if (isCurrentReportAttempt(attempt,interactionRevision)) { notice(error.message || '报价表生成失败。', 'error'); if (error.operationFinished) settleAttempt(attempt); } } finally { if (state.activeReportRequestId === attempt.requestId) state.activeReportRequestId = null; updateControls(); }
      return;
    }
    const moduleRunRefs = {};
    [...state.selected].forEach(candidateId => {
      const candidate = source.candidates.find(item => item.candidate_id === candidateId);
      const refs = {};
      modules.forEach(module => {
        if (module === 'recommender') return;
        const options = candidate?.module_run_options?.[module];
        refs[module] = Array.isArray(options) && options.length
          ? formalRunRef(state.runRefs[candidateId]?.[module] || options[0])
          : null;
      });
      moduleRunRefs[candidateId] = refs;
    });
    const deliveryMode = selectedDelivery();
    const chosen = (source.candidates || []).filter(candidate => state.selected.has(candidate.candidate_id));
    const suffix = deliveryMode === 'comparison' ? (type === 'card' ? '多产品简报' : '多产品研究报告') : (type === 'card' ? '简报' : '研究报告');
    const subject = [...new Set(chosen.map(candidate => `${(candidate.underlyings || []).join('、')}${candidate.product_name || ''}`))].join('、');
    const deliveryTitle = subject.slice(0, 120 - suffix.length) + suffix;
    const selection = { source_id: source.source_id, candidate_ids: [...state.selected], selected_modules: modules, module_run_refs: moduleRunRefs, delivery_mode: deliveryMode, output_type: type, format, audience: 'professional', metadata: { title: deliveryTitle } };
    const attempt = attemptFor(selection);
    const interactionRevision = state.interactionRevision;
    state.activeReportRequestId = attempt.requestId;
    $('generateButton').disabled = true; notice('正在整理分析结果并生成报告。');
    try { const response = await fetch('/api/run', {method:'POST',headers:{'Content-Type':'application/json','X-OptionHelper-Request-Id':attempt.requestId},body:JSON.stringify({selection})}); const data = await readDeliveryResponse(response, '报告生成失败'); if (!isCurrentReportAttempt(attempt,interactionRevision)) return; settleAttempt(attempt); setPreviewEditorSource(data, selection.report_run_id); notice('草稿已生成'); await reportEditor?.open(state.previewReportRunId); } catch (error) { if (isCurrentReportAttempt(attempt,interactionRevision)) { notice(error.message || '报告生成失败。', 'error'); if (error.operationFinished) settleAttempt(attempt); } } finally { if (state.activeReportRequestId === attempt.requestId) state.activeReportRequestId = null; updateControls(); }
  }
  function boot() {
    if (initialized) return Promise.resolve();
    if (bootPromise) return bootPromise;
    bootPromise = (async () => {
      try {
        const response = await fetch('/api/status');
        const data = await response.json();
        const stateNode = $('serviceState');
        const usable = data.status === 'available';
        stateNode.textContent = usable ? '报告服务可用' : '报告服务暂不可用';
        stateNode.dataset.state = usable ? 'ready' : 'error';
        if (!usable) notice('报告服务尚未就绪，请检查当前任务和本机配置。', 'error');
      } catch {
        $('serviceState').textContent = '正在恢复报告服务'; $('serviceState').dataset.state = 'loading'; notice('正在恢复报告服务连接。');
        bootPromise = null;
        return;
      }
      try {
        await loadSources();
        initialized = true;
      } catch (error) {
        notice(error.message || '报告来源读取失败，请重试。', 'error');
      } finally {
        bootPromise = null;
      }
    })();
    return bootPromise;
  }
  window.addEventListener('optionhelper.module-host-context', () => { if (!initialized) void boot(); });
  $('refreshButton').addEventListener('click', () => { loadSources().catch(error => notice(error.message, 'error')); });
  $('taskFilter').addEventListener('change', () => { markReportInputChanged(); loadSources().catch(error => notice(error.message, 'error')); });
  $('sourceSelect').addEventListener('change', () => { markReportInputChanged(); state.selected.clear(); state.runRefs = {}; renderCandidates(); });
  $('generateButton').addEventListener('click', generate);
  $('importHtmlButton').addEventListener('click', () => $('importHtmlFile').click());
  $('importHtmlFile').addEventListener('change', async event => {
    const file = event.target.files?.[0];
    const taskId = new URLSearchParams(location.search).get('task_id') || $('taskFilter').value.trim() || state.source?.task_id;
    if (!file || !taskId) { notice('请先选择当前任务。', 'error'); return; }
    try {
      const response = await fetch('/api/report-documents', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_id:taskId, title:file.name.replace(/\.html?$/i, '').slice(0,120), html:await file.text(), chart_specs:{}})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || '导入失败');
      notifyReportSaved(data); await reportEditor.open(data.report_run_id); if (data.import_notice) reportEditor._setStatus(data.import_notice);
    } catch (error) { notice(error.message, 'error'); }
    event.target.value = '';
  });
  $('editReportButton').addEventListener('click', () => { if (state.previewReportRunId) void reportEditor?.open(state.previewReportRunId); });
  $('reportTemplate').addEventListener('change', changeOutputType);
  document.querySelectorAll('input[data-module]').forEach(node => node.addEventListener('change', () => { markReportInputChanged(); updateControls(); }));
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.data?.type !== 'optionhelper.report-editor-open') return;
    const query = new URLSearchParams(location.search);
    if (query.get('host') !== 'optdesk' || !query.get('bridge_nonce') || event.data?.bridge_nonce !== query.get('bridge_nonce')) return;
    const reportRunId = typeof event.data?.report_run_id === 'string' ? event.data.report_run_id.trim() : '';
    if (reportRunId) void reportEditor?.open(reportRunId);
  });
  bindResizer('sourceResizer', '--library-width', 1, 220, 420); bindResizer('settingsResizer', '--inspector-width', -1, 300, 520); void boot();
})();
