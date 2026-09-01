(() => {
  const $ = id => document.getElementById(id);
  const state = { catalog: null, source: null, selected: new Set(), runRefs: {}, quoteItems: [], ready: false, delivery: null, pendingReport: null, pendingPdf: null };
  let bootPromise = null;
  let initialized = false;
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const selectedValue = name => document.querySelector(`input[name="${name}"]:checked`)?.value;
  const selectedModules = () => [...document.querySelectorAll('[data-module]:checked')].map(node => node.dataset.module);
  const runRefFields = ['module','tenant_id','task_id','run_id','expected_semantic_result_hash','expected_artifact_manifest_hash'];
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
  function pendingPdfAttempt(source, outputType) {
    const fingerprint = JSON.stringify({source, outputType, format:'pdf'});
    if (state.pendingPdf?.fingerprint === fingerprint) return state.pendingPdf;
    const attempt = {fingerprint, requestId:requestIdentifier('report-pdf'), reportRunId:identifier(`${outputType}-pdf`)};
    state.pendingPdf = attempt;
    return attempt;
  }
  function settlePdfAttempt(attempt) {
    if (state.pendingPdf?.requestId === attempt.requestId) state.pendingPdf = null;
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
  function activeSource() { return state.catalog?.sources?.find(item => item.source_id === $('sourceSelect').value) || null; }
  function moduleStatus(value) { return value?.status || (value?.expected_semantic_result_hash ? 'ready' : '缺失'); }
  function clearSourceState() {
    state.catalog = null; state.source = null; state.selected.clear(); state.runRefs = {}; state.quoteItems = []; renderSources();
    state.delivery = null; $('previewPanel').hidden = true; $('reportPreview').removeAttribute('src'); $('downloadLink').removeAttribute('href'); $('convertPdfButton').hidden = true; $('childReports').innerHTML = '';
  }
  function updateControls() {
    const outputType = selectedValue('outputType'); const format = selectedValue('format');
    const isQuote = outputType === 'quote';
    $('referenceQuotePanel').hidden = !isQuote;
    $('deliveryModeGroup').hidden = isQuote;
    $('moduleGroup').hidden = isQuote;
    const count = isQuote ? state.quoteItems.length : state.selected.size; const moduleCount = selectedModules().length;
    if (count <= 1 && $('deliveryMode').value !== 'single') $('deliveryMode').value = 'single';
    const comparison = $('deliveryMode').value === 'comparison';
    $('formatRule').textContent = format === 'pdf' ? 'PDF与HTML使用同一组冻结事实，不会重新运行计算模块。' : isQuote ? '参考报价仅使用当前选中的正式估值结果，不包含Greeks、回测或图表。' : comparison && outputType === 'card' ? '多结构研究简报横向展示候选的关键条款、五项Greeks、回测摘要和风险，不包含图表。' : comparison ? '多结构完整研究报告展示候选合同、收益、估值、回测和风险对比。' : outputType === 'card' ? '简报不使用章节目录或损益图。' : '完整报告固定为连续A4正文。';
    $('selectionCount').textContent = isQuote ? (count ? `已加入${count}条报价行` : '未加入报价行') : (count ? `已选择${count}个候选` : '未选择候选');
    const unavailableReason = !state.ready
      ? '报告服务暂不可用，请稍后重试。'
      : !state.source
        ? '当前没有可用的分析结果，请先完成一项模块分析。'
        : !count
          ? (isQuote ? '请从已保存的参数运行中加入至少一条报价行。' : '请先选择至少一个分析结果。')
          : !isQuote && !moduleCount
          ? '请至少选择一个报告模块。'
          : '';
    $('generateButton').disabled = Boolean(unavailableReason);
    $('generateButton').title = unavailableReason || '生成并保存报告';
    if (!isQuote && !moduleCount) $('formatRule').textContent = '请至少选择一个报告模块。';
    $('deliveryMode').querySelectorAll('option:not([value="single"])').forEach(option => option.disabled = count < 2);
  }
  function bindResizer(id, variable, direction, minimum, maximum) {
    const handle = $(id); const workbench = document.querySelector('.workbench');
    const current = () => parseFloat(getComputedStyle(workbench).getPropertyValue(variable)) || minimum;
    const apply = value => { const next = Math.max(minimum, Math.min(maximum, value)); workbench.style.setProperty(variable, `${next}px`); handle.setAttribute('aria-valuenow', String(next)); };
    handle.setAttribute('aria-valuemin', String(minimum)); handle.setAttribute('aria-valuemax', String(maximum)); handle.setAttribute('aria-valuenow', String(current()));
    handle.addEventListener('pointerdown', event => { const start = event.clientX; const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); handle.setPointerCapture(event.pointerId); const move = next => apply(initial + direction * (next.clientX - start)); handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', () => handle.removeEventListener('pointermove', move), {once:true}); });
    handle.addEventListener('keydown', event => { if (!['ArrowLeft','ArrowRight'].includes(event.key)) return; event.preventDefault(); const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); apply(initial + direction * (event.key === 'ArrowRight' ? 12 : -12)); });
  }
  function renderSources() {
    const select = $('sourceSelect'); const sources = state.catalog?.sources || [];
    select.innerHTML = sources.length ? sources.map(source => `<option value="${esc(source.source_id)}">${esc(source.label || source.source_id)} · ${esc(source.task_id)}</option>`).join('') : '<option>暂无可用报告来源</option>';
    select.disabled = !sources.length; state.source = activeSource(); state.selected.clear(); state.runRefs = {};
    if (selectedValue('outputType') !== 'quote') state.quoteItems = [];
    renderCandidates();
  }
  function quoteContractKey(item) { return `${item.source_id}/${item.candidate_id}`; }
  function renderQuoteItems() {
    const panel = $('quoteItems'); const isQuote = selectedValue('outputType') === 'quote';
    panel.hidden = !isQuote;
    if (!isQuote) { panel.innerHTML = ''; return; }
    panel.innerHTML = state.quoteItems.length
      ? `<div class="quote-items__head"><span>已选报价结构</span><span>${state.quoteItems.length}条</span></div><div class="quote-items__list">${state.quoteItems.map((item, index) => `<span class="quote-item">${esc(item.product_name)} · 合同版本${esc(item.candidate_version_id || item.product_version || '已冻结')} · 估值结果${item.version}<button type="button" data-remove-quote="${index}" data-quote-contract="${esc(quoteContractKey(item))}" aria-label="移除报价行">移除</button></span>`).join('')}</div>`
      : '<div class="quote-items__head"><span>已选报价结构</span><span>从下方加入已保存的参数版本</span></div>';
    panel.querySelectorAll('[data-remove-quote]').forEach(button => button.addEventListener('click', () => {
      const item = state.quoteItems[Number(button.dataset.removeQuote)];
      state.quoteItems.splice(Number(button.dataset.removeQuote), 1);
      renderCandidates(item ? {quoteContract:quoteContractKey(item)} : null);
    }));
  }
  function quoteCandidate(candidate, source) {
    const rows = ['pricing'].map(module => {
      const options = Array.isArray(candidate.module_run_options?.[module]) ? candidate.module_run_options[module] : [];
      return options.map((run, index) => {
        const item = {source_id:source.source_id, candidate_id:candidate.candidate_id, module, module_run_ref:run};
        const contractKey = quoteContractKey(item);
        const added = state.quoteItems.some(existing => quoteContractKey(existing) === contractKey);
        return `<div class="quote-run"><div><strong>估值定价</strong><span>合同版本${esc(candidate.candidate_version_id || candidate.product_version || '已冻结')} · 估值结果${index + 1}</span></div><button class="quote-add" type="button" data-quote-source="${esc(source.source_id)}" data-quote-candidate="${esc(candidate.candidate_id)}" data-quote-module="${module}" data-quote-index="${index}" data-quote-contract="${esc(contractKey)}" ${added ? 'disabled' : ''}>${added ? '该合同已加入' : '加入报价表'}</button></div>`;
      }).join('');
    }).join('');
    return `<article class="candidate" data-candidate="${esc(candidate.candidate_id)}"><div class="candidate-head"><div class="candidate-title">${esc(candidate.product_name)}<div class="candidate-subtitle">${esc(candidate.product_id)} · ${esc((candidate.underlyings || []).join('、'))} · 版本${esc(candidate.product_version)}</div></div></div>${rows || '<p class="missing-note">暂无可用于报价的已保存运行结果。</p>'}</article>`;
  }
  function renderCandidates(focusTarget = null) {
    state.source = activeSource(); const source = state.source; const list = $('candidateList'); const isQuote = selectedValue('outputType') === 'quote';
    if (!source) { list.innerHTML = ''; $('sourceDetail').textContent = '当前任务尚无可用于生成报告的分析结果。'; renderQuoteItems(); updateControls(); return; }
    $('sourceDetail').innerHTML = `任务：<b>${esc(source.label || source.task_id)}</b><br>候选数量：${esc((source.candidates || []).length)}<br>资料状态：已保存`;
    if (isQuote) {
      const sources = state.catalog?.sources || [];
      list.innerHTML = sources.map(item => `<section class="quote-source"><h3>${esc(item.label || '已保存任务')}</h3>${(item.candidates || []).map(candidate => quoteCandidate(candidate, item)).join('')}</section>`).join('');
      list.querySelectorAll('[data-quote-candidate]').forEach(button => button.addEventListener('click', () => {
        const itemSource = sources.find(item => item.source_id === button.dataset.quoteSource);
        const candidate = itemSource?.candidates?.find(item => item.candidate_id === button.dataset.quoteCandidate);
        const module = button.dataset.quoteModule; const index = Number(button.dataset.quoteIndex);
        const run = candidate?.module_run_options?.[module]?.[index];
        if (!candidate || !run) return;
        const contractKey = quoteContractKey({source_id:itemSource.source_id, candidate_id:candidate.candidate_id});
        state.quoteItems.push({source_id:itemSource.source_id, source_label:itemSource.label, candidate_id:candidate.candidate_id, module, module_run_ref:run, product_name:candidate.product_name, product_version:candidate.product_version, candidate_version_id:candidate.candidate_version_id, version:index + 1});
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
        const selector = options.length > 1
          ? `<select class="run-choice" data-candidate="${esc(candidate.candidate_id)}" data-module="${esc(module)}">${options.map((item, index) => `<option value="${index}" ${index === selected ? 'selected' : ''}>参数版本${index + 1}</option>`).join('')}</select>`
          : options.length === 1 ? `<span>参数版本1</span>` : `<span>${esc(status)}</span>`;
        return `<div class="run-row" data-status="${esc(status)}"><strong>${({payoff:'收益结构',pricing:'估值定价',backtest:'历史回测'})[module]}</strong>${selector}</div>`;
      }).join('');
      return `<article class="candidate" data-candidate="${esc(candidate.candidate_id)}" data-selected="${checked}"><div class="candidate-head"><input type="checkbox" data-select-candidate="${esc(candidate.candidate_id)}" ${checked?'checked':''} aria-label="选择${esc(candidate.product_name)}"><div class="candidate-title">${esc(candidate.product_name)}<div class="candidate-subtitle">${esc(candidate.product_id)} · ${esc((candidate.underlyings || []).join('、'))} · 版本${esc(candidate.candidate_version_id || candidate.product_version)}</div></div></div><div class="run-grid">${runs}</div></article>`;
    }).join('');
    list.querySelectorAll('.candidate').forEach(card => card.querySelector('input').addEventListener('change', event => { const id = card.dataset.candidate; event.target.checked ? state.selected.add(id) : state.selected.delete(id); renderCandidates({candidateId:id}); }));
    list.querySelectorAll('.run-choice').forEach(select => select.addEventListener('change', event => {
      const candidate = source.candidates.find(item => item.candidate_id === event.target.dataset.candidate);
      const options = candidate?.module_run_options?.[event.target.dataset.module];
      if (!Array.isArray(options)) return;
      state.runRefs[event.target.dataset.candidate] = { ...(state.runRefs[event.target.dataset.candidate] || {}), [event.target.dataset.module]: options[Number(event.target.value)] };
    }));
    renderQuoteItems(); updateControls(); restoreSelectionFocus(focusTarget);
  }
  async function loadSources() {
    notice('正在读取当前任务的分析结果。');
    const task = $('taskFilter').value.trim(); const quote = selectedValue('outputType') === 'quote'; const response = await fetch(`/api/report-sources${task && !quote ? `?task_id=${encodeURIComponent(task)}` : ''}`); const data = await response.json();
    if (!response.ok) { state.ready = false; clearSourceState(); notice(data.message || '当前任务没有可选择的结果。', 'error'); return; }
    state.ready = true; state.catalog = data; renderSources(); notice((data.sources || []).length ? '请选择候选及需要纳入报告的分析内容。' : '当前任务尚无可用的分析结果。');
  }
  async function generate() {
    const source = state.source;
    if (!state.ready) { notice('报告服务暂不可用，请稍后重试。', 'error'); return; }
    if (!source) { notice('当前没有可用的分析结果。请先完成收益结构、估值定价或历史回测。', 'error'); return; }
    const type = selectedValue('outputType'); const format = selectedValue('format');
    if (type === 'quote' && !state.quoteItems.length) { notice('请从已保存的参数运行中加入至少一条报价行。', 'error'); return; }
    if (type !== 'quote' && !state.selected.size) { notice('请先选择至少一个分析结果。', 'error'); return; }
    const modules = selectedModules();
    if (type === 'quote') {
      const selection = { source_id: source.source_id, quote_items: state.quoteItems.map(item => ({source_id:item.source_id, candidate_id:item.candidate_id, module:item.module, module_run_ref:formalRunRef(item.module_run_ref)})), output_type: type, format, audience: $('audience').value, metadata: { title: '推荐结构及参考报价' } };
      const attempt = attemptFor(selection);
      $('generateButton').disabled = true; notice('正在按已选合同快照整理参考报价。');
      try { const response = await fetch('/api/run', {method:'POST',headers:{'Content-Type':'application/json','X-OptionHelper-Request-Id':attempt.requestId},body:JSON.stringify({selection})}); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || '报价表生成失败'); settleAttempt(attempt); $('previewPanel').hidden = false; $('previewTitle').textContent = '参考报价预览'; $('reportPreview').src = data.preview_url; $('downloadLink').href = data.download_url; const canRerender = format === 'html' && data.output?.can_save_pdf === true; state.delivery = canRerender ? {source:{task_id:source.task_id, report_run_id:selection.report_run_id}, outputType:type} : null; $('convertPdfButton').hidden = !state.delivery; $('childReports').innerHTML = ''; notice(canRerender ? 'HTML报价表已保存，需要PDF时可直接另存，不会重新计算。' : '报价表已保存，可以预览或下载。'); } catch (error) { notice(error.message || '报价表生成失败。', 'error'); } finally { updateControls(); }
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
    const deliveryMode = $('deliveryMode').value;
    const deliveryTitle = deliveryMode === 'comparison'
      ? (type === 'card' ? '多结构研究简报' : '多结构完整研究报告')
      : (source.label || '场外衍生品研究报告');
    const selection = { source_id: source.source_id, candidate_ids: [...state.selected], selected_modules: modules, module_run_refs: moduleRunRefs, delivery_mode: deliveryMode, output_type: type, format, audience: $('audience').value, metadata: { title: deliveryTitle } };
    const attempt = attemptFor(selection);
    $('generateButton').disabled = true; notice('正在整理分析结果并生成报告。');
    try { const response = await fetch('/api/run', {method:'POST',headers:{'Content-Type':'application/json','X-OptionHelper-Request-Id':attempt.requestId},body:JSON.stringify({selection})}); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || '报告生成失败'); settleAttempt(attempt); $('previewPanel').hidden = false; $('previewTitle').textContent = deliveryMode === 'comparison' ? (type === 'card' ? '多结构研究简报预览' : '多结构完整研究报告预览') : type === 'card' ? '研究简报预览' : '完整研究报告预览'; $('reportPreview').src = data.preview_url; $('downloadLink').href = data.download_url; const canRerender = format === 'html' && data.output?.can_save_pdf === true; state.delivery = canRerender ? {source:{task_id:source.task_id, report_run_id:selection.report_run_id}, outputType:type} : null; $('convertPdfButton').hidden = !state.delivery; $('childReports').innerHTML = (data.output?.children || []).map(item => `<a href="${esc(item.preview_url)}" target="reportPreview">查看${esc(item.display_name || '独立报告')}</a><a href="${esc(item.download_url)}">下载</a>`).join(''); const savedNotice = canRerender ? 'HTML已保存。需要PDF时可直接另存，不会重新计算。' : 'HTML已保存。当前交付方式不支持在报告库中另存PDF。'; notice(format === 'html' ? savedNotice : '报告已保存，可以预览或下载。'); } catch (error) { notice(error.message || '报告生成失败。', 'error'); } finally { updateControls(); }
  }
  async function convertPdf() {
    if (!state.delivery) return;
    const source = state.delivery.source;
    const attempt = pendingPdfAttempt(source, state.delivery.outputType);
    $('convertPdfButton').disabled = true; notice('正在基于已保存的报告内容生成PDF，不会重新运行模型或计算模块。');
    try {
      const response = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json','X-OptionHelper-Request-Id':attempt.requestId}, body:JSON.stringify({derive:{source, report_run_id:attempt.reportRunId, format:'pdf'}})});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.message || 'PDF转换失败');
      settlePdfAttempt(attempt);
      $('previewTitle').textContent = 'PDF交付预览'; $('reportPreview').src = data.preview_url; $('downloadLink').href = data.download_url; $('convertPdfButton').hidden = true; state.delivery = null; notice('PDF已另存为新的交付，不影响原HTML。');
    } catch (error) { notice(error.message || 'PDF转换失败。', 'error'); $('convertPdfButton').disabled = false; }
  }
  function boot() {
    if (initialized) return Promise.resolve();
    if (bootPromise) return bootPromise;
    bootPromise = (async () => {
      try {
        const response = await fetch('/api/status'); const data = await response.json(); const stateNode = $('serviceState'); const usable = data.status === 'available'; stateNode.textContent = usable ? '报告服务可用' : '报告服务暂不可用'; stateNode.dataset.state = usable ? 'ready' : 'error'; if (!usable) notice('报告服务尚未就绪，请检查当前任务和本机配置。', 'error'); await loadSources(); initialized = true;
      } catch {
        $('serviceState').textContent = '正在恢复报告服务'; $('serviceState').dataset.state = 'loading'; notice('正在恢复报告服务连接。');
      } finally {
        bootPromise = null;
      }
    })();
    return bootPromise;
  }
  window.addEventListener('optionhelper.module-host-context', () => { if (!initialized) void boot(); });
  $('refreshButton').addEventListener('click', () => loadSources().catch(error => notice(error.message, 'error'))); $('taskFilter').addEventListener('change', () => loadSources().catch(error => notice(error.message, 'error'))); $('sourceSelect').addEventListener('change', renderCandidates); $('generateButton').addEventListener('click', generate); $('convertPdfButton').addEventListener('click', convertPdf); document.querySelectorAll('input[name="outputType"]').forEach(node => node.addEventListener('change', () => loadSources().catch(error => notice(error.message, 'error')))); document.querySelectorAll('input[name="format"],input[data-module],#deliveryMode').forEach(node => node.addEventListener('change', updateControls)); bindResizer('sourceResizer', '--library-width', 1, 220, 420); bindResizer('settingsResizer', '--inspector-width', -1, 300, 520); void boot();
})();
