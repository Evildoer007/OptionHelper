(() => {
  const $ = id => document.getElementById(id);
  const state = { catalog: null, source: null, selected: new Set(), runRefs: {}, ready: false };
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const selectedValue = name => document.querySelector(`input[name="${name}"]:checked`)?.value;
  const selectedModules = () => [...document.querySelectorAll('[data-module]:checked')].map(node => node.dataset.module);
  const identifier = prefix => `${prefix}_${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}`;

  function notice(text, tone = '') { $('notice').textContent = text; $('notice').dataset.tone = tone; }
  function activeSource() { return state.catalog?.sources?.find(item => item.source_id === $('sourceSelect').value) || null; }
  function moduleStatus(value) { return value?.status || (value?.expected_semantic_result_hash ? 'ready' : '缺失'); }
  function clearSourceState() {
    state.catalog = null; state.source = null; state.selected.clear(); state.runRefs = {}; renderSources();
    $('previewPanel').hidden = true; $('reportPreview').removeAttribute('src'); $('downloadLink').removeAttribute('href'); $('childReports').innerHTML = '';
  }
  function updateControls() {
    const outputType = selectedValue('outputType'); const format = selectedValue('format');
    $('formatRule').textContent = format === 'pdf' ? 'PDF采用固定页式；若Designer缺少PDF依赖，将真实显示不可用。' : outputType === 'card' ? '简报不使用目录或损益图。' : '完整报告固定为连续A4正文，不提供目录版。';
    const count = state.selected.size; const moduleCount = selectedModules().length;
    $('selectionCount').textContent = count ? `已选择${count}个候选` : '未选择候选';
    const unavailableReason = !state.ready
      ? '报告服务暂不可用，请稍后重试。'
      : !state.source
        ? '当前没有可用的分析结果，请先完成一项模块分析。'
        : !count
          ? '请先选择至少一个分析结果。'
          : !moduleCount
            ? '请至少选择一个报告模块。'
            : '';
    $('generateButton').disabled = Boolean(unavailableReason);
    $('generateButton').title = unavailableReason || '生成并保存报告';
    if (!moduleCount) $('formatRule').textContent = '请至少选择一个报告模块。';
    if (count <= 1 && $('deliveryMode').value !== 'single') $('deliveryMode').value = 'single';
    $('deliveryMode').querySelectorAll('option:not([value="single"])').forEach(option => option.disabled = count < 2);
  }
  function bindResizer(id, variable, direction) {
    const handle = $(id); const workbench = document.querySelector('.workbench');
    const apply = value => workbench.style.setProperty(variable, `${Math.max(190, Math.min(380, value))}px`);
    handle.addEventListener('pointerdown', event => { const start = event.clientX; const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); handle.setPointerCapture(event.pointerId); const move = next => apply(initial + direction * (next.clientX - start)); handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', () => handle.removeEventListener('pointermove', move), {once:true}); });
    handle.addEventListener('keydown', event => { if (!['ArrowLeft','ArrowRight'].includes(event.key)) return; event.preventDefault(); const initial = parseFloat(getComputedStyle(workbench).getPropertyValue(variable)); apply(initial + direction * (event.key === 'ArrowRight' ? 12 : -12)); });
  }
  function renderSources() {
    const select = $('sourceSelect'); const sources = state.catalog?.sources || [];
    select.innerHTML = sources.length ? sources.map(source => `<option value="${esc(source.source_id)}">${esc(source.label || source.source_id)} · ${esc(source.task_id)}</option>`).join('') : '<option>暂无可用报告来源</option>';
    select.disabled = !sources.length; state.source = activeSource(); state.selected.clear(); state.runRefs = {}; renderCandidates();
  }
  function renderCandidates() {
    state.source = activeSource(); const source = state.source; const list = $('candidateList');
    if (!source) { list.innerHTML = ''; $('sourceDetail').textContent = '当前租户没有可用于Reporter的已保存研究与合同来源。'; updateControls(); return; }
    $('sourceDetail').innerHTML = `任务：<b>${esc(source.task_id)}</b><br>分析案例：${esc(source.analysis_case_id)}<br>资料版本：${esc(source.catalog_version || '未记录')}`;
    list.innerHTML = (source.candidates || []).map(candidate => {
      const checked = state.selected.has(candidate.candidate_id);
      const runs = ['payoff','pricing','backtest'].map(module => {
        const options = Array.isArray(candidate.module_run_options?.[module]) ? candidate.module_run_options[module] : [];
        const run = candidate.module_run_refs?.[module] || options[0];
        const status = moduleStatus(run);
        const selectedIndex = options.indexOf(state.runRefs[candidate.candidate_id]?.[module]);
        const selected = selectedIndex >= 0 ? selectedIndex : 0;
        const selector = options.length > 1
          ? `<select class="run-choice" data-candidate="${esc(candidate.candidate_id)}" data-module="${esc(module)}">${options.map((item, index) => `<option value="${index}" ${index === selected ? 'selected' : ''}>${esc(item.run_id)}</option>`).join('')}</select>`
          : options.length === 1 ? `<span>${esc(options[0].run_id)}</span>` : `<span>${esc(status)}</span>`;
        return `<div class="run-row" data-status="${esc(status)}"><strong>${({payoff:'Payoffer',pricing:'Pricer',backtest:'Backtester'})[module]}</strong>${selector}</div>`;
      }).join('');
      return `<article class="candidate" data-candidate="${esc(candidate.candidate_id)}" data-selected="${checked}"><div class="candidate-head"><input type="checkbox" ${checked?'checked':''} aria-label="选择${esc(candidate.product_name)}"><div class="candidate-title">${esc(candidate.product_name)}<div class="candidate-subtitle">${esc(candidate.product_id)} · ${esc((candidate.underlyings || []).join('、'))} · 版本${esc(candidate.product_version)}</div></div></div><div class="fingerprint">合同指纹：${esc(candidate.contract_fingerprint || '未记录')}</div><div class="run-grid">${runs}</div></article>`;
    }).join('');
    list.querySelectorAll('.candidate').forEach(card => card.querySelector('input').addEventListener('change', event => { const id = card.dataset.candidate; event.target.checked ? state.selected.add(id) : state.selected.delete(id); renderCandidates(); }));
    list.querySelectorAll('.run-choice').forEach(select => select.addEventListener('change', event => {
      const candidate = source.candidates.find(item => item.candidate_id === event.target.dataset.candidate);
      const options = candidate?.module_run_options?.[event.target.dataset.module];
      if (!Array.isArray(options)) return;
      state.runRefs[event.target.dataset.candidate] = { ...(state.runRefs[event.target.dataset.candidate] || {}), [event.target.dataset.module]: options[Number(event.target.value)] };
    }));
    updateControls();
  }
  async function loadSources() {
    notice('正在读取当前租户受控的报告来源。');
    const task = $('taskFilter').value.trim(); const response = await fetch(`/api/report-sources${task ? `?task_id=${encodeURIComponent(task)}` : ''}`); const data = await response.json();
    if (!response.ok) { state.ready = false; clearSourceState(); notice(data.message || '当前Host未提供可选择的结果目录。', 'error'); return; }
    state.ready = true; state.catalog = data; renderSources(); notice((data.sources || []).length ? '请选择同一研究来源中的候选与模块结果。服务端会再次校验合同、版本与哈希。' : '当前筛选没有可用来源。');
  }
  async function generate() {
    const source = state.source;
    if (!state.ready) { notice('报告服务暂不可用，请稍后重试。', 'error'); return; }
    if (!source) { notice('当前没有可用的分析结果。请先完成收益结构、估值定价或历史回测。', 'error'); return; }
    if (!state.selected.size) { notice('请先选择至少一个分析结果。', 'error'); return; }
    const type = selectedValue('outputType'); const format = selectedValue('format');
    const modules = selectedModules();
    const moduleRunRefs = {};
    [...state.selected].forEach(candidateId => {
      const candidate = source.candidates.find(item => item.candidate_id === candidateId);
      const refs = {};
      modules.forEach(module => {
        if (module === 'recommender') return;
        const options = candidate?.module_run_options?.[module];
        refs[module] = Array.isArray(options) && options.length
          ? state.runRefs[candidateId]?.[module] || options[0]
          : null;
      });
      moduleRunRefs[candidateId] = refs;
    });
    const selection = { source_id: source.source_id, candidate_ids: [...state.selected], selected_modules: modules, module_run_refs: moduleRunRefs, delivery_mode: $('deliveryMode').value, output_type: type, format, audience: $('audience').value, report_run_id: identifier('report'), metadata: { title: source.label || '场外衍生品研究报告' } };
    $('generateButton').disabled = true; notice('正在通过Reporter验证证据、冻结ReportUnit并调用Designer生成交付物。');
    try { const response = await fetch('/api/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection})}); const data = await response.json(); if (!response.ok || !data.ok) throw new Error(data.message || '报告生成失败'); $('previewPanel').hidden = false; $('previewTitle').textContent = `${type === 'card' ? 'Card' : 'Report'}预览`; $('reportPreview').src = data.preview_url; $('downloadLink').href = data.download_url; $('childReports').innerHTML = (data.output?.children || []).map(item => `<a href="${esc(item.preview_url)}" target="reportPreview">查看${esc(item.candidate_id)}独立报告</a><a href="${esc(item.download_url)}">下载</a>`).join(''); notice('报告已保存。预览和下载均只读取本次ReportRun清单声明的产物。'); } catch (error) { notice(error.message || '报告生成失败。', 'error'); } finally { updateControls(); }
  }
  async function boot() {
    try { const response = await fetch('/api/status'); const data = await response.json(); const stateNode = $('serviceState'); const usable = data.status === 'available'; stateNode.textContent = usable ? '受控报告服务可用' : '等待受控依赖'; stateNode.dataset.state = usable ? 'ready' : 'error'; if (!usable) notice('需要ResultStore、Designer和结果选择端口。页面不会扫描服务器目录。', 'error'); await loadSources(); } catch { $('serviceState').textContent = '本机服务未启动'; $('serviceState').dataset.state = 'error'; notice('无法连接Reporter服务。', 'error'); }
  }
  $('refreshButton').addEventListener('click', () => loadSources().catch(error => notice(error.message, 'error'))); $('taskFilter').addEventListener('change', () => loadSources().catch(error => notice(error.message, 'error'))); $('sourceSelect').addEventListener('change', renderCandidates); $('generateButton').addEventListener('click', generate); document.querySelectorAll('input[name="outputType"],input[name="format"],input[data-module],#deliveryMode').forEach(node => node.addEventListener('change', updateControls)); bindResizer('sourceResizer', '--source-width', 1); bindResizer('settingsResizer', '--settings-width', -1); boot();
})();
