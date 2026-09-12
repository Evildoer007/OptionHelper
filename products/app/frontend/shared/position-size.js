import { request } from './app.js';

const formatAmount = value => {
  if (value === null || value === undefined) return '不适用';
  const number = Number(value);
  if (!Number.isFinite(number)) return '不适用';
  if (number !== 0 && Math.abs(number) < 0.000001) return number.toExponential(6);
  return number.toLocaleString('zh-CN', { maximumFractionDigits: 8 });
};


export function positionSizeInputError(position) {
  if (position === null) return '';
  const text = String(position.amount).trim();
  if (/[^\x00-\x7F]/.test(text) && /^\p{Decimal_Number}+(?:\.\p{Decimal_Number}{1,8})?$/u.test(text)) return '';
  if (text.startsWith('-') || /^0+(?:\.0+)?$/.test(text)) return '名义本金必须大于0。';
  if (text.length > 40 || !/^\d+(?:\.\d{1,8})?$/.test(text)) return '名义本金请填写普通数字，最多8位小数，不使用逗号或科学计数法。';
  const [integer, fraction = ''] = text.split('.');
  const normalized = integer.replace(/^0+/, '') || '0';
  if (normalized.length > 16 || (normalized.length === 16 && (normalized > '1000000000000000' || (normalized === '1000000000000000' && /[1-9]/.test(fraction))))) return '名义本金不能超过1000万亿元。';
  return '';
}

export function initializePositionSize(shell) {
  if (document.querySelector('.position-size')) return;
  let task = null;
  let revision = 0;
  const parameterGroups = new Map();
  let sizeWriteRevision = 0;
  let formEditRevision = 0;
  let positionSettledAt = 0;
  let pendingSizeWrites = 0;
  let latestWriteSettledAt = 0;
  let writeQueue = Promise.resolve();
  let returnFocus = null;
  const panel = document.createElement('dialog');
  panel.className = 'position-size';
  panel.setAttribute('aria-labelledby', 'position-size-title');
  panel.innerHTML = `<header><h2 id="position-size-title">名义本金与金额</h2><button type="button" data-size-close aria-label="关闭金额面板">×</button></header><div class="position-size__body"><p>填写当前任务的名义规模后，显示对应金额。原百分比、合同条款与计算保持不变。</p><form data-size-form><label>规模口径<select name="basis"><option value="notional">名义本金</option><option value="variance_notional">方差名义金额，每方差百分点平方</option></select></label><div class="position-size__fields"><label>金额<input name="amount" type="text" inputmode="decimal" placeholder="留空保留百分比" autocomplete="off" maxlength="40"></label><label>币种<select name="currency">${['CNY','USD','HKD','EUR','JPY','GBP','CHF','AUD','CAD','SGD'].map(value => `<option>${value}</option>`).join('')}</select></label></div><small>名义本金是金额换算基准，不是保证金，也不代表保本；币种不触发汇率换算。方差互换单独选择方差名义金额。</small><div class="position-size__actions"><button type="submit">保存规模</button><button type="button" data-size-clear>清空规模</button><button type="button" data-size-refresh>刷新金额结果</button></div></form><p role="status" data-size-status></p><section data-size-results aria-label="当前任务金额结果"></section></div>`;
  document.body.append(panel);
  const form = panel.querySelector('form');
  const status = panel.querySelector('[data-size-status]');
  const results = panel.querySelector('[data-size-results]');
  const field = name => form.elements.namedItem(name);
  const populate = () => {
    field('amount').value = task?.position_size?.amount || '';
    field('currency').value = task?.position_size?.currency || 'CNY';
    field('basis').value = task?.position_size?.basis || 'notional';
  };
  const cell = (row, tag, text) => { const element = document.createElement(tag); element.textContent = text ?? '不适用'; row.append(element); return element; };
  const table = (headings, rows) => {
    const element = document.createElement('table');
    const head = element.createTHead().insertRow();
    headings.forEach(text => cell(head, 'th', text));
    const body = element.createTBody();
    rows.forEach(values => { const row = body.insertRow(); values.forEach(text => cell(row, 'td', text)); });
    const scroll = document.createElement('div');
    scroll.className = 'position-size__table-scroll';
    scroll.tabIndex = 0;
    scroll.setAttribute('role', 'region');
    scroll.setAttribute('aria-label', headings.join('、'));
    scroll.append(element);
    return scroll;
  };
  const render = view => {
    results.replaceChildren();
    if (!view.position_size) { status.textContent = '尚未设置名义规模，当前只显示原百分比结果。'; return; }
    status.textContent = view.runs.length ? '以下为当前已保存计算的金额换算，请按产品和来源日期核对。' : '规模已保存。完成定价或回测后，可在这里查看金额结果。';
    for (const run of [...view.runs].reverse()) {
      const section = document.createElement('section');
      section.className = 'position-size__run';
      const heading = document.createElement('h3');
      heading.textContent = `${run.product_id || ''} ${({pricer:'估值定价',backtester:'历史回测',payoffer:'收益结构'})[run.module]}`;
      section.append(heading);
      const date = document.createElement('p');
      date.className = 'position-size__source';
      date.textContent = run.created_at ? `来源时间：${run.created_at}` : `来源：${run.run_id}`;
      section.append(date);
      const projection = run.projection;
      if (projection.status === 'available') {
        section.append(table(['指标', '金额', '单位'], projection.rows.map(row => [row.label, formatAmount(row.amount), row.unit])));
        const note = document.createElement('p'); note.textContent = projection.note || ''; section.append(note);
        if (projection.trade_ledger?.length) {
          const details = document.createElement('details');
          const summary = document.createElement('summary'); summary.textContent = `逐笔结算，共${projection.trade_ledger.length}笔`; details.append(summary);
          // Populate on first expansion; no ledger rows or values are discarded.
          details.addEventListener('toggle', () => {
            if (!details.open || details.dataset.rendered) return;
            details.dataset.rendered = 'true';
            details.append(table(['入场日', '结算日', '结算金额'], projection.trade_ledger.map(row => [row.entry_date, row.exit_date, formatAmount(row.amount)])));
          });
          section.append(details);
        }
        if (projection.risk_curves?.length) {
          const details = document.createElement('details');
          const summary = document.createElement('summary'); summary.textContent = '风险曲线金额明细'; details.append(summary);
          details.addEventListener('toggle', () => {
            if (!details.open || details.dataset.rendered) return;
            details.dataset.rendered = 'true';
            for (const curve of projection.risk_curves) {
              const title = document.createElement('h4'); title.textContent = curve.name || curve.title || curve.curve_id || '风险曲线'; details.append(title);
              details.append(table([curve.x_axis?.name || '情景', curve.y_axis?.unit || '金额'], curve.points.map(point => [String(point.x), formatAmount(point.y)])));
            }
          });
          section.append(details);
        }
        if (projection.risk_surfaces?.length) {
          const details = document.createElement('details');
          const summary = document.createElement('summary'); summary.textContent = '风险曲面金额明细'; details.append(summary);
          details.addEventListener('toggle', () => {
            if (!details.open || details.dataset.rendered) return;
            details.dataset.rendered = 'true';
            for (const surface of projection.risk_surfaces) {
              const title = document.createElement('h4'); title.textContent = surface.name || surface.title || '风险曲面'; details.append(title);
              details.append(table([surface.x_axis?.name || '横轴情景', surface.y_axis?.name || '纵轴情景', surface.z_axis?.unit || '金额'], (surface.data || []).filter(point => Array.isArray(point.value) && point.value.length >= 3).map(point => [String(point.value[0]), String(point.value[1]), formatAmount(point.value[2])])));
            }
          });
          section.append(details);
        }
        if (projection.risk_scenarios?.length) {
          const details = document.createElement('details');
          const summary = document.createElement('summary'); summary.textContent = '风险情景金额明细'; details.append(summary);
          details.append(table(['情景', '金额', '单位'], projection.risk_scenarios.map((row, index) => [row.name || `情景${index + 1}`, formatAmount(row.amount), row.unit])));
          section.append(details);
        }
      } else { const note = document.createElement('p'); note.textContent = projection.reason; section.append(note); }
      results.append(section);
    }
  };
  const refresh = async () => {
    if (!task) return;
    const selected = task.task_id;
    const attempt = ++revision;
    const formEdit = formEditRevision;
    status.textContent = '正在读取当前任务金额结果…';
    try {
      await writeQueue.catch(() => {});
      if (attempt !== revision || selected !== task?.task_id) return;
      const readStartedAt = performance.now();
      const view = await request(`/api/tasks/${encodeURIComponent(selected)}/position-size`);
      if (attempt !== revision || selected !== task?.task_id) return;
      task = { ...task, position_size: view.position_size };
      positionSettledAt = readStartedAt;
      syncParameterGroups();
      if (panel.open && formEdit === formEditRevision) populate();
      render(view);
    } catch (error) { if (attempt === revision) { results.replaceChildren(); status.textContent = `金额结果未读取：${error.message}。可点击刷新重试。`; } }
  };
  const persistSize = async position_size => {
    const inputError = positionSizeInputError(position_size);
    if (inputError) throw new Error(inputError);
    const selected = task?.task_id;
    if (!selected) return;
    const attempt = ++sizeWriteRevision;
    ++revision;
    ++pendingSizeWrites;
    const writing = writeQueue.catch(() => {}).then(() => request(`/api/tasks/${encodeURIComponent(selected)}/position-size`, {method: 'POST', body: JSON.stringify({position_size})})).then(result => {
      latestWriteSettledAt = performance.now();
      return result;
    }).finally(() => { --pendingSizeWrites; });
    writeQueue = writing;
    const result = await writing;
    if (selected !== task?.task_id || attempt !== sizeWriteRevision) return;
    task = result.task;
    positionSettledAt = performance.now();
    populate(); syncParameterGroups();
    if (panel.open) await refresh();
  };
  const save = async clear => {
    if (!task) return;
    const selected = task.task_id;
    const position = clear || !field('amount').value.trim() ? null : {amount: field('amount').value.trim(), currency: field('currency').value, basis: field('basis').value};
    const controls = [...form.querySelectorAll('button, input, select')];
    controls.forEach(control => control.disabled = true);
    status.textContent = '正在保存…';
    try { await persistSize(position); }
    catch (error) { if (selected === task?.task_id) status.textContent = error.message; }
    finally { controls.forEach(control => control.disabled = false); }
  };
  const openPanel = trigger => {
    if (!task || panel.open) return;
    returnFocus = trigger;
    populate(); panel.showModal(); field('amount').focus(); void refresh();
  };
  function syncParameterGroups() {
    for (const [frame, group] of parameterGroups) {
      if (!frame.isConnected) { parameterGroups.delete(frame); continue; }
      if (group.dataset.editing === 'true') continue;
      const size = task?.position_size;
      group.querySelector('[data-notional-amount]').value = size?.amount || '';
      group.querySelector('[data-notional-currency]').value = size?.currency || 'CNY';
      group.querySelector('[data-notional-basis]').value = size?.basis || 'notional';
    }
  }
  window.addEventListener('optionhelper.module-parameters-ready', event => {
    const {frame, moduleName, taskId} = event.detail;
    if (!['pricer', 'backtester'].includes(moduleName) || taskId !== task?.task_id || !frame.isConnected) return;
    const doc = frame.contentDocument;
    const parameterSection = doc?.querySelector(moduleName === 'pricer' ? '#dividend' : '#endDate')?.closest('.module-parameter-section');
    if (!parameterSection || doc.querySelector('[data-task-notional]')) return;
    const style = doc.createElement('link');
    style.rel = 'stylesheet'; style.href = '/app/frontend/shared/position-size.css'; doc.head.append(style);
    const group = doc.createElement('div');
    group.className = 'task-notional';
    group.dataset.taskNotional = '';
    group.setAttribute('aria-label', '名义本金参数');
    group.innerHTML = `<div class="task-notional__fields"><label class="control-field"><span>名义本金</span><input data-notional-amount type="text" inputmode="decimal" autocomplete="off" maxlength="40" placeholder="默认1,000,000"></label><label class="control-field"><span>币种</span><select data-notional-currency>${['CNY','USD','HKD','EUR','JPY','GBP','CHF','AUD','CAD','SGD'].map(value => `<option>${value}</option>`).join('')}</select></label></div><input data-notional-basis type="hidden" value="notional"><button type="button" class="task-notional__results" data-notional-results>金额设置与结果</button><p role="status" class="task-notional__status"></p>`;
    parameterSection.append(group);
    parameterGroups.set(frame, group); syncParameterGroups();
    const feedback = group.querySelector('[role="status"]');
    // These are App display parameters. Do not trigger module input checks or
    // contract reconstruction through the enclosing calculation form.
    let saveTimer;
    let editRevision = 0;
    const scheduleSave = immediate => {
      window.clearTimeout(saveTimer);
      const edit = ++editRevision;
      group.dataset.editing = 'true';
      const amount = group.querySelector('[data-notional-amount]').value.trim();
      const position = amount ? {amount, currency: group.querySelector('[data-notional-currency]').value, basis: group.querySelector('[data-notional-basis]').value} : null;
      const submit = async () => {
        if (taskId !== task?.task_id || !frame.isConnected) return;
        feedback.textContent = '正在保存…';
        try {
          await persistSize(position);
          if (taskId === task?.task_id && edit === editRevision) {
            delete group.dataset.editing; syncParameterGroups(); feedback.textContent = '已同步当前任务。';
          }
        } catch (error) { if (taskId === task?.task_id && edit === editRevision) feedback.textContent = `未保存：${error.message}`; }
      };
      if (immediate) void submit();
      else saveTimer = window.setTimeout(submit, 300);
    };
    group.addEventListener('input', event => { event.stopPropagation(); feedback.textContent = '正在等待输入完成…'; scheduleSave(false); });
    group.addEventListener('change', event => { event.stopPropagation(); scheduleSave(true); });
    group.addEventListener('keydown', event => {
      if (event.key === 'Enter' && event.target.matches('input')) { event.preventDefault(); event.stopPropagation(); event.target.blur(); }
    });
    group.querySelector('[data-notional-results]').addEventListener('click', event => openPanel(event.currentTarget));
  });
  panel.querySelector('[data-size-close]').addEventListener('click', () => panel.close());
  panel.addEventListener('close', () => { if (returnFocus?.isConnected) returnFocus.focus(); });
  form.addEventListener('input', () => { ++formEditRevision; });
  form.addEventListener('change', () => { ++formEditRevision; });
  form.addEventListener('submit', event => { event.preventDefault(); void save(false); });
  panel.querySelector('[data-size-clear]').addEventListener('click', () => void save(true));
  panel.querySelector('[data-size-refresh]').addEventListener('click', () => void refresh());
  window.addEventListener('optionhelper.task-selected', event => {
    ++revision;
    const incoming = event.detail.task;
    const sameTask = task?.task_id === incoming?.task_id;
    const olderPositionRead = sameTask && Number.isFinite(event.detail.readStartedAt)
      && event.detail.readStartedAt < positionSettledAt;
    if (!sameTask) {
      positionSettledAt = 0;
      ++sizeWriteRevision;
      parameterGroups.clear();
    }
    if (panel.open) panel.close();
    task = olderPositionRead ? { ...incoming, position_size: task.position_size } : incoming;
    results.replaceChildren(); status.textContent = ''; syncParameterGroups();
    // A task may be revisited before its prior write response has settled.
    // Read after the existing write queue, without reusing another task's value.
    if (pendingSizeWrites || (Number.isFinite(event.detail.readStartedAt)
      && event.detail.readStartedAt < latestWriteSettledAt)) void refresh();
  });
}
