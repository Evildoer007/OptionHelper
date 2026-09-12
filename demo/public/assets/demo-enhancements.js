(() => {
  'use strict';

  const DATA = window.OptionHelperDemoData;
  const RESULTS = window.OptionHelperDemoResults ||= { pricing: {}, backtest: {} };
  if (!DATA) return;

  const resultLoads = new Map();
  function ensureHistoricalResult(module, id) {
    if(!DATA.result_index?.[module]?.[id] || RESULTS[module]?.[id])return;
    const key=module+':'+id;if(resultLoads.has(key))return;
    resultLoads.set(key,'loading');
    const script=document.createElement('script');script.src='data/'+module+'/'+id+'.js?v=20260907';
    const refresh=()=>{if(productId===id && ((module==='pricing'&&moduleName==='pricer')||(module==='backtest'&&moduleName==='backtester'))){captureInputs();renderModule();}};
    const timer=setTimeout(()=>{resultLoads.set(key,'unavailable');refresh();},10000);
    script.onload=()=>{clearTimeout(timer);resultLoads.set(key,RESULTS[module]?.[id]?'ready':'unavailable');refresh();};
    script.onerror=()=>{clearTimeout(timer);resultLoads.set(key,'unavailable');refresh();};document.head.appendChild(script);
  }
  function missingResultText(module) {
    const state=resultLoads.get(module+':'+productId);
    return state==='loading'?'正在读取历史快照。':state==='unavailable'?'历史文件暂时无法读取，请确认Demo文件已下载到本机后刷新。':'当前产品没有历史运行快照。';
  }

  const catalog = new Map(DATA.products.map(item => [item.product_id, item]));
  const groupLabels = {
    '1': '一、香草期权', '2': '二、价差结构', '3': '三、组合结构',
    '4': '四、障碍期权', '5': '五、二元与触碰', '6': '六、安全气囊',
    '7': '七、累购结构', '8': '八、雪球与票息', '9': '九、特色结构',
  };
  let activeDataSymbol = '000905.SH';
  let activeReportPath = DATA.reports.comparison;
  let reportDeliveryMode = 'comparison';
  let reportOutputType = 'report';
  let reportFormat = 'html';
  let payoffZoom = 1;
  let ledgerPage = 0;
  const ledgerPageSize = 30;
  const moduleProducts = {};
  const moduleInputs = new Map();
  let renderedModule = null;
  const inputKey = () => `${moduleName}:${productId}`;
  const fieldKey = field => field.id || `${field.closest('[data-term-key]')?.dataset.termKey || field.closest('label')?.querySelector('span')?.textContent || field.name}:${field.tagName}:${field.type}`;
  function captureInputs() {
    const values=new Map();
    document.querySelectorAll('.side.right input,.side.right select,.side.right textarea').forEach(field=>values.set(fieldKey(field),{value:field.value,checked:field.checked}));
    moduleInputs.set(inputKey(),values);
    if(moduleName==='payoffer'){const state=document.querySelector('#runState');if(state)state.textContent='条款已修改，图形仍为默认条款。';}
  }
  function restoreInputs() {
    const saved=moduleInputs.get(inputKey());
    if(!saved)return;
    document.querySelectorAll('.side.right input,.side.right select,.side.right textarea').forEach(field=>{const value=saved.get(fieldKey(field));if(value){field.value=value.value;if(field.type==='checkbox')field.checked=value.checked;}});
  }
  document.querySelector('#moduleHost').addEventListener('change',event=>{if(event.target.closest('.side.right'))captureInputs();},true);
  function snapshotNotice(module) {
    const compatibility=currentCatalog()?.snapshot_compatibility?.[module];
    return `<div class="status-banner">历史运行快照；修改右侧参数不会重新计算。${compatibility?.status==='outdated'?'该记录使用历史产品规则。':''}</div>`;
  }
  const finiteReturn = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  function settlementStats(backtest) {
    const returns=(backtest.trade_ledger||[]).map(row=>row.contract_settlement_return ?? row.gross_return).filter(finiteReturn).map(Number);
    const positive=returns.filter(value=>value>0).length,zero=returns.filter(value=>value===0).length,negative=returns.filter(value=>value<0).length;
    const count=returns.length;
    return {count,positive,zero,negative,rate:count?positive/count:null,average:count?returns.reduce((a,b)=>a+b,0)/count:null,minimum:count?Math.min(...returns):null};
  }

  const escapeText = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
  const numberText = (value, digits = 4) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
    ? Number(value).toLocaleString('zh-CN', { maximumFractionDigits: digits }) : '不适用';
  const percentText = (value, digits = 2) => finiteReturn(value)
    ? `${(Number(value) * 100).toLocaleString('zh-CN', { maximumFractionDigits: digits })}%` : '不适用';
  const shortHash = value => value ? `${String(value).slice(0, 10)}…${String(value).slice(-6)}` : '—';
  const currentCatalog = () => catalog.get(productId);
  const currentPricing = () => RESULTS.pricing[productId] || null;
  const currentBacktest = () => RESULTS.backtest[productId] || null;
  const priceConventionText = asset => {
    const convention = asset?.price_convention;
    if (!convention || typeof convention !== 'object') return convention || '价格口径已登记';
    const fields = convention.field_adjustment_by_asset?.[activeDataSymbol] || {};
    const close = fields.close || convention.asset_market_conventions?.[activeDataSymbol]?.close_convention;
    if (close === 'unadjusted' || close === 'unadjusted_index_close') return '未复权指数收盘价';
    if (close === 'forward_adjusted') return '前复权收盘价';
    if (close === 'backward_adjusted') return '后复权收盘价';
    return close ? String(close) : `${convention.frequency || '日线'}收盘价`;
  };

  function statusClass(item) {
    if (item.availability.complete_report) return 'complete';
    if (item.availability.pricing && item.availability.backtest) return 'partial';
    if (item.availability.pricing) return 'pricing';
    if (item.availability.backtest) return 'backtest';
    return '';
  }

  function productOptions() {
    const groups = new Map();
    DATA.products.forEach(item => {
      const group = item.product_id.split('.')[0];
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(item);
    });
    return '<option value="">请选择产品</option>' + [...groups.entries()].map(([group, items]) => (
      `<optgroup label="${groupLabels[group] || group}">${items.map(item => (
        `<option value="${item.product_id}" ${item.product_id === productId ? 'selected' : ''}>${item.product_id} ${escapeText(item.name)} · ${item.status_label}</option>`
      )).join('')}</optgroup>`
    )).join('');
  }

  actionbar = function(label,button,kind,disabled=false,extra='') {
    return `<div class="actionbar"><span class="state" id="runState">${kind==='payoffer'?'默认条款图':'离线演示'}</span><div class="actions">${extra}<button class="button primary" data-run="${kind}" ${disabled?'disabled':''}>${button}</button></div></div>`;
  };
  leftPanel = function enhancedLeftPanel(title) {
    const item = currentCatalog();
    return `<aside class="side left"><h2>${escapeText(title)}</h2><label>产品</label><select class="select product-select" aria-label="产品">${productOptions()}</select><div class="product-summary">${item ? `<strong>${item.product_id} ${escapeText(item.name)}</strong><span>${item.contract.paths.length}条产品路径</span>` : '<span>请选择产品</span>'}</div><div class="path-head">产品路径 <span>${item?.contract.paths.length ?? '—'}</span></div>${pathRows()}</aside>`;
  };

  function closeChoices(except = null) {
    document.querySelectorAll('.optionhelper-choice[data-open="true"]').forEach(choice => {
      if (choice !== except) {
        choice.dataset.open = 'false';
        choice.querySelector('.choice-trigger')?.setAttribute('aria-expanded', 'false');
        const menu = choice.querySelector('.choice-menu');
        if (menu) menu.hidden = true;
      }
    });
  }

  function enhanceSelect(select) {
    if (!select || select.closest('.optionhelper-choice')) return;
    const choice = document.createElement('div');
    choice.className = 'optionhelper-choice';
    choice.dataset.open = 'false';
    select.parentNode.insertBefore(choice, select);
    choice.appendChild(select);
    select.classList.add('choice-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'choice-trigger';
    trigger.innerHTML = '<span class="choice-trigger__value"></span><span class="choice-trigger__chevron" aria-hidden="true"></span>';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const label = select.getAttribute('aria-label') || [...(select.labels || [])].map(item => item.textContent.replace(select.textContent, '').trim()).filter(Boolean).join(' ');
    if (label) trigger.setAttribute('aria-label', label);
    const menu = document.createElement('div');
    menu.className = 'choice-menu';
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;
    choice.append(trigger, menu);

    const addOption = option => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'choice-option';
      button.setAttribute('role', 'option');
      button.dataset.value = option.value;
      button.textContent = option.textContent;
      button.disabled = option.disabled || option.parentElement?.disabled === true;
      button.tabIndex = -1;
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        if (select.disabled || button.disabled) return;
        const changed = select.value !== option.value;
        closeChoices();
        if (changed) {
          select.value = option.value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        if (trigger.isConnected && !select.disabled) trigger.focus({ preventScroll: true });
      });
      menu.appendChild(button);
    };
    [...select.children].forEach(child => {
      if (child.tagName === 'OPTGROUP') {
        const heading = document.createElement('div');
        heading.className = 'choice-group';
        heading.textContent = child.label;
        menu.appendChild(heading);
        [...child.children].forEach(addOption);
      } else if (child.tagName === 'OPTION') addOption(child);
    });

    const sync = () => {
      const option = select.selectedOptions[0];
      trigger.querySelector('.choice-trigger__value').textContent = option?.textContent || '请选择';
      trigger.disabled = select.disabled;
      trigger.setAttribute('aria-invalid', select.getAttribute('aria-invalid') || 'false');
      menu.querySelectorAll('.choice-option').forEach(button => button.setAttribute('aria-selected', String(button.dataset.value === select.value)));
    };
    const open = () => {
      if (trigger.disabled) return;
      closeChoices(choice);
      const rect = trigger.getBoundingClientRect();
      const menuHeight = Math.min(260, window.innerHeight * .42);
      choice.dataset.placement = window.innerHeight - rect.bottom < menuHeight && rect.top > menuHeight ? 'top' : 'bottom';
      choice.dataset.open = 'true';
      trigger.setAttribute('aria-expanded', 'true');
      menu.hidden = false;
      (menu.querySelector('[aria-selected="true"]:not(:disabled)') || menu.querySelector('.choice-option:not(:disabled)'))?.focus({ preventScroll: true });
    };
    trigger.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); choice.dataset.open === 'true' ? closeChoices() : open(); });
    trigger.addEventListener('keydown', event => {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        if (!event.repeat) open();
      }
    });
    menu.addEventListener('keydown', event => {
      const enabled = [...menu.querySelectorAll('.choice-option:not(:disabled)')];
      const index = enabled.indexOf(document.activeElement);
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopPropagation(); closeChoices(); trigger.focus({ preventScroll: true });
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        enabled[(index + step + enabled.length) % enabled.length]?.focus({ preventScroll: true });
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); enabled[event.key === 'Home' ? 0 : enabled.length - 1]?.focus({ preventScroll: true });
      } else if (event.key === 'Tab') {
        closeChoices();
        trigger.focus({ preventScroll: true });
      }
    });
    select.addEventListener('change', sync);
    sync();
  }

  function enhanceAllSelects(root = document) {
    root.querySelectorAll('select').forEach(enhanceSelect);
  }

  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('.optionhelper-choice')) closeChoices();
  });
  document.addEventListener('focusin', event => {
    closeChoices(event.target.closest('.optionhelper-choice'));
  });

  function dataPanelLeft() {
    return `<aside class="side left"><h2>数据资产</h2><label>已登记资产</label><select class="select" id="dataAssetSelect"><option value="000905.SH" ${activeDataSymbol === '000905.SH' ? 'selected' : ''}>000905.SH · 中证500</option><option value="000300.SH" ${activeDataSymbol === '000300.SH' ? 'selected' : ''}>000300.SH · 沪深300</option></select><p class="helper">已保存的离线行情，不发起网络请求。</p><div class="product-summary"><strong>${activeDataSymbol}</strong><span>${escapeText(DATA.market_assets[activeDataSymbol].row_count)}行 · ${escapeText(priceConventionText(DATA.market_assets[activeDataSymbol]))}</span><i class="availability-pill complete">真实数据</i></div><div class="path-head">字段与用途 <span>${(DATA.market_assets[activeDataSymbol].normalized_fields || []).length}</span></div>${(DATA.market_assets[activeDataSymbol].normalized_fields || []).map((field, index) => `<div class="path-row"><b>${String(index + 1).padStart(2, '0')}</b><div><strong>${escapeText(field)}</strong><small>标准化行情字段</small></div></div>`).join('')}</aside>`;
  }

  dataModule = function enhancedDataModule() {
    const asset = DATA.market_assets[activeDataSymbol];
    const coverage = asset.coverage?.by_asset?.[activeDataSymbol] || {};
    const fields = asset.normalized_fields || Object.keys(asset.preview?.[0] || {});
    const preview = (asset.preview || []).slice(-8).reverse();
    const center = `<section class="center"><div class="data-result"><div class="result-titlebar"><div><h1>${activeDataSymbol}行情数据</h1><p>行情快照 · 登记时间${escapeText(asset.registered_at)} · 离线只读快照</p></div><div class="result-actions"><span class="evidence-tag available">离线行情</span></div></div><div class="asset-switcher"><button data-data-symbol="000905.SH" aria-selected="${activeDataSymbol === '000905.SH'}">000905.SH</button><button data-data-symbol="000300.SH" aria-selected="${activeDataSymbol === '000300.SH'}">000300.SH</button></div><div class="asset-overview"><div><span>记录数</span><strong>${numberText(asset.row_count, 0)}</strong></div><div><span>实际区间</span><strong>${escapeText(coverage.start_date)}至${escapeText(coverage.end_date)}</strong></div><div><span>交易日历</span><strong>${escapeText(asset.coverage?.calendar_id || '未验证')}</strong></div><div><span>来源</span><strong>iFind</strong></div></div><section class="result-block"><header><div><h2>行情走势</h2><p>${escapeText(priceConventionText(asset))}；横轴为交易日期。</p></div></header><div id="marketChart" class="oh-chart"></div></section><section class="result-block"><header><div><h2>数据质量检查</h2><p>展示登记元数据和离线文件校验，不替代正式模块的完整质量报告。</p></div></header><div class="quality-grid"><div class="quality-item"><span>文件状态</span><strong>已核对来源</strong></div><div class="quality-item"><span>时间顺序</span><strong>按交易日升序</strong></div><div class="quality-item"><span>价格口径</span><strong>${escapeText(priceConventionText(asset))}</strong></div></div></section><section class="result-block"><header><div><h2>字段列表与数据预览</h2><p>${fields.map(escapeText).join('、')}</p></div></header><div class="table-wrap"><table class="audit-table"><thead><tr>${fields.map(field => `<th>${escapeText(field)}</th>`).join('')}</tr></thead><tbody>${preview.map(row => `<tr>${fields.map(field => `<td>${escapeText(row[field] ?? '—')}</td>`).join('')}</tr>`).join('')}</tbody></table></div></section><details class="audit-details"><summary>DataAssetRef与来源审计</summary><table class="audit-table"><tbody><tr><th>data_asset_id</th><td class="mono">${escapeText(asset.data_asset_id)}</td></tr><tr><th>content_hash</th><td class="mono">${escapeText(asset.content_hash)}</td></tr><tr><th>metadata_hash</th><td class="mono">${escapeText(asset.metadata_hash)}</td></tr><tr><th>离线文件</th><td>${escapeText(asset.storage_ref)}</td></tr><tr><th>来源</th><td>${escapeText(asset.lineage?.provider || asset.lineage?.source || 'iFind HTTP，来源字段见DataAssetRef')}</td></tr></tbody></table></details></div></section>`;
    const right = `<aside class="side right"><h2>数据请求</h2><div class="right-section"><h3>请求范围</h3><div class="control-stack">${demoCalendar('开始日期', asset.coverage?.requested_start_date || coverage.start_date)}${demoCalendar('结束日期', asset.coverage?.requested_end_date || coverage.end_date)}<label class="control-field"><span>资产标识，每行一个</span><textarea class="input" required rows="4">${activeDataSymbol}</textarea><small class="field-message">支持每行一个标的代码。</small></label></div></div><div class="right-section"><h3>数据口径</h3>${demoSelect('数据源', [['ifind', 'iFind', true]], '正式App统一使用已配置的iFind数据接口；Demo不发起网络请求。')}${demoSelect('频率', [['daily', '日线', true]])}<div class="read-only-row"><span>价格字段</span><strong>按模块用途自动选择</strong></div></div><div class="right-section"><h3>获取方式</h3><p class="helper">正式App先复用满足标的、字段、日期和交易日历要求的本地数据，覆盖不足时自动通过iFind补齐。</p></div><div class="right-section"><h3>数据保存</h3><label class="switchline"><input id="persistData" type="checkbox" ${dataPersistence ? 'checked' : ''}><span class="switch-ui"></span><span><strong>保存下载数据</strong><small>默认关闭。Demo只演示该开关，不写入正式数据目录。</small></span></label></div></aside>`;
    return `<div class="module-grid">${dataPanelLeft()}${center}${right}</div>${actionbar('', '读取登记数据', 'datafetcher', false)}`;
  };

  payoffModule = function enhancedPayoffModule() {
    const item = currentCatalog();
    if (!item) return `<div class="module-grid">${leftPanel('收益结构')}${emptyResult('选择产品', '选择后展示最新正式收益图、合同路径、条款与导出操作。')}${rightFields('payoff')}</div>${actionbar('', '导出SVG', 'payoffer', true)}`;
    const selectedPath = item.contract.paths[selectedPayoffPath] || item.contract.paths[0];
    const saved = Boolean(item.payoffer_run);
    const center = `<section class="center"><div class="multi-result"><div class="result-titlebar"><div><h1>${item.product_id} ${escapeText(item.name)}</h1><p>当前默认条款的合同结算收益率</p></div><div class="payoff-toolbar"><button class="button" data-payoff-zoom="out">－</button><span>${Math.round(payoffZoom * 100)}%</span><button class="button" data-payoff-zoom="in">＋</button><button class="button" data-payoff-zoom="reset">重置</button><a class="button" href="${item.payoff.svg}" download="${item.product_id}-${escapeText(item.name)}.svg">导出SVG</a></div></div><div class="payoff-frame" style="--payoff-zoom:${payoffZoom}"><img src="${item.payoff.svg}" alt="${escapeText(item.name)}正式收益图"></div><section class="result-block"><header><div><h2>选中路径详情</h2><p>路径${selectedPayoffPath + 1} · ${escapeText(selectedPath.condition)}</p></div></header><table class="audit-table"><thead><tr><th>情形</th><th>终值域</th><th>收益公式</th></tr></thead><tbody>${(selectedPath.cases || []).map((entry, index) => `<tr><td>情形${index + 1}</td><td>${escapeText(entry.domain)}</td><td class="mono">${escapeText(entry.pnl)}</td></tr>`).join('')}</tbody></table></section><details class="audit-details"><summary>图形与保存审计</summary><table class="audit-table"><tbody><tr><th>图形来源</th><td>modules/payoffer/figures/svg，当前工作区最新代码</td></tr><tr><th>SVG哈希</th><td class="mono">${escapeText(item.payoff.svg_sha256)}</td></tr><tr><th>JSON哈希</th><td class="mono">${escapeText(item.payoff.json_sha256)}</td></tr><tr><th>保存运行</th><td>${saved ? escapeText(item.payoffer_run.run_id) : '无当前结果库保存运行'}</td></tr><tr><th>合同指纹</th><td class="mono">${escapeText(item.payoffer_run?.contract_fingerprint || '代码内置图无运行合同指纹')}</td></tr></tbody></table></details></div></section>`;
    return `<div class="module-grid">${leftPanel('收益结构')}${center}${rightFields('payoff')}</div>${actionbar('', '导出SVG', 'payoffer', false, '<button class="button" data-payoff-default>恢复正式默认图</button>')}`;
  };

  function pricingResultMarkup(record) {
    const item = currentCatalog();
    const pricing = record.pricing || {};
    const market = pricing.market_snapshot || record.market_snapshot || {};
    const greeks = pricing.greeks || {};
    return `<div class="multi-result"><div class="result-titlebar"><div><h1>${item.product_id} ${escapeText(item.name)}估值结果</h1><p>真实运行${escapeText(record.run_id)} · ${escapeText(pricing.method || '未登记方法')} · ${escapeText(record.created_at || '')}</p></div><span class="evidence-tag available">历史定价快照</span></div><div class="run-overview"><div><span>现值，PV%</span><strong>${percentText(pricing.pv_percent, 4)}</strong></div><div><span>标准误，PV%</span><strong>${percentText(pricing.standard_error_percent, 4)}</strong></div><div><span>估值日</span><strong>${escapeText(market.valuation_date || pricing.input_snapshot?.pricing_config?.valuation_date || '—')}</strong></div><div><span>精度状态</span><strong>${escapeText(({quote_eligible:'具备报价资格',research_only:'研究估计',insufficient:'精度不足',not_applicable:'不适用'})[pricing.precision_status] || pricing.precision_status || '—')}</strong></div></div><div class="greek-switch">${Object.entries(greekMeta).map(([key, meta]) => `<button data-greek="${key}" aria-selected="${key === activeGreek}">${meta.label}</button>`).join('')}</div><div class="greek-cards">${Object.entries(greekMeta).map(([key, meta]) => `<div class="greek-card ${key === activeGreek ? 'active' : ''}"><span>${meta.label}</span><strong>${greeks[key]?.status === 'available' ? percentText(greeks[key].value, 6) : escapeText(greeks[key]?.status || '不可用')}</strong></div>`).join('')}</div><section class="result-block"><header><div><h2>风险敏感度曲线</h2><p id="curveAxisText">按真实风险曲线与当前市场点展示。</p></div><div id="curveFactorTabs" class="surface-switch"></div></header><div id="greekCurveChart" class="oh-chart"></div></section><section class="result-block"><header><div><h2>风险敏感度曲面</h2><p id="surfaceAxisText">支持3D曲面、热力图和数据表。</p></div><div class="surface-switch"><button data-surface-view="3d" aria-selected="${activeSurfaceView === '3d'}">3D曲面</button><button data-surface-view="heatmap" aria-selected="${activeSurfaceView === 'heatmap'}">热力图</button><button data-surface-view="table" aria-selected="${activeSurfaceView === 'table'}">数据表</button><button id="surfaceReset">重置视角</button></div></header><div id="greekSurfaceChart" class="oh-chart oh-surface"></div><div id="greekSurfaceTable" class="surface-table-wrap" hidden></div></section><details class="audit-details"><summary>估值输入、合同与运行审计</summary><table class="audit-table"><tbody><tr><th>run_id</th><td>${escapeText(record.run_id)}</td></tr><tr><th>contract_fingerprint</th><td class="mono">${escapeText(record.contract_fingerprint)}</td></tr><tr><th>execution_fingerprint</th><td class="mono">${escapeText(record.execution_fingerprint)}</td></tr><tr><th>数据引用</th><td class="mono">${escapeText((record.data_refs || []).map(ref => ref.data_asset_id || ref.content_hash).filter(Boolean).join('；') || '—')}</td></tr><tr><th>限制条件</th><td>${escapeText((pricing.limitations || record.limitations || []).map(value => typeof value === 'string' ? value : JSON.stringify(value)).join('；') || '无额外限制')}</td></tr></tbody></table></details></div>`;
  }

  pricerModule = function enhancedPricerModule() {
    const item = currentCatalog();
    const record = currentPricing();
    const result = item ? (record ? snapshotNotice('pricing') + (demoPricingObjective === 'fair_parameter' ? emptyResult('公平合同条款反解', '可配置当前支持的目标；求解请在正式App运行。') : pricingResultMarkup(record)) : emptyResult('暂无真实定价结果', missingResultText('pricing'))) : emptyResult('选择产品', '选择后读取当前结果库真实估值、Greeks Curve和Surface。');
    return `<div class="module-grid">${leftPanel('估值定价')}<section class="center">${result}</section>${rightFields('pricer')}</div>${actionbar('', '查看历史结果', 'pricer', !item)}`;
  };

  function surfaceGrid(surface) {
    const xs = surface?.x_axis?.values || [];
    const ys = surface?.y_axis?.values || [];
    const z = Array.from({ length: ys.length }, () => Array(xs.length).fill(null));
    (surface?.data || []).forEach(item => {
      const [x, y, value] = item.value || [];
      if (Number.isInteger(x) && Number.isInteger(y) && z[y]) z[y][x] = Number.isFinite(value) ? value : null;
    });
    return { xs, ys, z, values: z.flat().filter(Number.isFinite) };
  }

  function renderPricingCharts() {
    const record = currentPricing();
    if (!record || !document.querySelector('#greekCurveChart')) return;
    const pricing = record.pricing || {};
    const meta = greekMeta[activeGreek];
    const curves = (pricing.risk_curves || []).filter(curve => curve.key.startsWith(`${activeGreek}_`));
    if (!curves.some(curve => curve.key === activeCurveKey)) activeCurveKey = curves[0]?.key || '';
    const tabs = document.querySelector('#curveFactorTabs');
    if (tabs) tabs.innerHTML = curves.map(curve => `<button data-curve-key="${curve.key}" aria-selected="${curve.key === activeCurveKey}">${escapeText(curve.x_axis?.name || curve.name)}</button>`).join('');
    const curve = curves.find(item => item.key === activeCurveKey) || curves[0];
    if (curve) {
      const usable = (curve.points || []).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
      const marketAxis = (curve.annotations || []).find(item => item.kind === 'market' && Number.isFinite(item.axis_value))?.axis_value;
      const nearest = Number.isFinite(marketAxis) ? usable.reduce((best, item) => !best || Math.abs(item.x - marketAxis) < Math.abs(best.x - marketAxis) ? item : best, null) : null;
      const traces = [{ type: 'scatter', mode: 'lines', name: curve.name, x: usable.map(point => point.x), y: usable.map(point => point.y), line: { color: '#3f6078', width: 2.6, shape: 'spline', smoothing: .72, simplify: false }, hovertemplate: `<b>${curve.name}</b><br>${curve.x_axis.name}：%{x:.6g}<br>${curve.y_axis.name}：%{y:.6g}<extra></extra>` }];
      if (nearest) traces.push({ type: 'scatter', mode: 'markers', x: [nearest.x], y: [nearest.y], marker: { size: 8, color: '#ad2940', line: { width: 2, color: '#fff' } }, hovertemplate: '当前市场点<extra></extra>', showlegend: false });
      document.querySelector('#curveAxisText').textContent = `X：${curve.x_axis.name}；Y：${curve.y_axis.name}`;
      demoPlot(document.querySelector('#greekCurveChart'), traces, demoPlotLayout(curve.x_axis.name, curve.y_axis.name, { uirevision: `curve-${curve.key}` }));
    } else document.querySelector('#greekCurveChart').innerHTML = '<div class="chart-empty">当前结果未提供该Greek风险曲线</div>';
    renderPricingSurface();
    document.querySelectorAll('[data-curve-key]').forEach(button => button.onclick = () => { activeCurveKey = button.dataset.curveKey; renderPricingCharts(); });
  }

  function renderPricingSurface() {
    const record = currentPricing();
    const pricing = record?.pricing || {};
    const surface = (pricing.risk_surfaces || []).find(item => item.key === greekMeta[activeGreek].surface);
    const chart = document.querySelector('#greekSurfaceChart');
    const table = document.querySelector('#greekSurfaceTable');
    if (!chart || !table) return;
    if (!surface) {
      chart.hidden = false; table.hidden = true; chart.innerHTML = '<div class="chart-empty">当前结果未提供该Greek风险曲面</div>'; return;
    }
    if (activeSurfaceView === '3d' && !demoSupportsWebGL()) activeSurfaceView = 'heatmap';
    document.querySelectorAll('[data-surface-view]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.surfaceView === activeSurfaceView)));
    const grid = surfaceGrid(surface);
    const minimum = Math.min(...grid.values);
    const maximum = Math.max(...grid.values);
    const colorscale = demoColorScale(minimum, maximum);
    document.querySelector('#surfaceAxisText').textContent = `X：${surface.x_axis.name}；Y：${surface.y_axis.name}；Z：${surface.z_axis.name}`;
    chart.hidden = activeSurfaceView === 'table';
    table.hidden = activeSurfaceView !== 'table';
    if (activeSurfaceView === 'table') {
      try { if (chart._fullLayout) Plotly.purge(chart); } catch (_error) {}
      table.innerHTML = `<table class="surface-table"><thead><tr><th>${escapeText(surface.y_axis.name)} \\ ${escapeText(surface.x_axis.name)}</th>${grid.xs.map(value => `<th>${numberText(value, 4)}</th>`).join('')}</tr></thead><tbody>${grid.ys.map((y, row) => `<tr><th>${numberText(y, 4)}</th>${grid.z[row].map(value => `<td>${percentText(value, 6)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
      return;
    }
    table.innerHTML = '';
    if (activeSurfaceView === 'heatmap') {
      demoPlot(chart, [{ type: 'heatmap', x: grid.xs, y: grid.ys, z: grid.z, zmin: minimum, zmax: maximum, colorscale, colorbar: { thickness: 10, len: .78, tickfont: { size: 9 } }, hovertemplate: `${surface.x_axis.name}：%{x:.6g}<br>${surface.y_axis.name}：%{y:.6g}<br>${surface.z_axis.name}：%{z:.6g}<extra></extra>` }], demoPlotLayout(surface.x_axis.name, surface.y_axis.name, { margin: { l: 66, r: 44, t: 20, b: 54 }, uirevision: `heatmap-${surface.key}` }));
    } else {
      demoPlot(chart, [{ type: 'surface', x: grid.xs, y: grid.ys, z: grid.z, cmin: minimum, cmax: maximum, colorscale, colorbar: { thickness: 10, len: .68, tickfont: { size: 9 } }, hovertemplate: `${surface.x_axis.name}：%{x:.6g}<br>${surface.y_axis.name}：%{y:.6g}<br>${surface.z_axis.name}：%{z:.6g}<extra></extra>` }], { autosize: true, margin: { l: 0, r: 20, t: 12, b: 0 }, paper_bgcolor: 'rgba(0,0,0,0)', font: { family: getComputedStyle(document.body).fontFamily, size: 10, color: '#252525' }, scene: { camera: { projection: { type: 'orthographic' }, eye: { x: 1.45, y: -1.55, z: 1.08 }, center: { x: 0, y: 0, z: -.08 } }, aspectratio: { x: 1.5, y: 1, z: .68 }, dragmode: 'orbit', xaxis: { title: { text: surface.x_axis.name }, showbackground: false, gridcolor: '#d7dce0' }, yaxis: { title: { text: surface.y_axis.name }, showbackground: false, gridcolor: '#d7dce0' }, zaxis: { title: { text: surface.z_axis.name }, showbackground: false, gridcolor: '#d7dce0' } }, uirevision: `surface-${surface.key}` }).catch(() => { activeSurfaceView = 'heatmap'; renderPricingSurface(); });
    }
  }

  function backtestMarkup(record) {
    const item = currentCatalog();
    const backtest = record.backtest || {};
    const metrics = backtest.common_metrics || {};
    const stats = settlementStats(backtest);
    const trades = backtest.trade_ledger || [];
    const trade = trades[selectedTrade] || trades[0];
    const eventRows = Object.values(backtest.event_summary || {});
    ledgerPage=Math.max(0,Math.min(ledgerPage,Math.ceil(trades.length/ledgerPageSize)-1));
    return `<div class="multi-result"><div class="result-titlebar"><div><h1>${item.product_id} ${escapeText(item.name)}回测结果</h1><p>真实运行${escapeText(record.run_id)} · ${escapeText(backtest.metric_profile?.display_name || '合同回测')} · ${escapeText(record.created_at || '')}</p></div><span class="evidence-tag available">历史回测快照</span></div><div class="run-overview"><div><span>有效样本</span><strong>${numberText(metrics.valid_return_sample_count ?? backtest.sample_count, 0)}</strong></div><div><span>历史正收益样本占比</span><strong>${percentText(stats.rate)}</strong></div><div><span>平均合同结算收益率</span><strong>${percentText(metrics.average_contract_settlement_return ?? stats.average)}</strong></div><div><span>最低合同结算收益率</span><strong>${percentText(metrics.minimum_contract_settlement_return ?? stats.minimum)}</strong></div></div><section class="result-block"><header><div><h2>收益分布与结果分类</h2><p>正收益${stats.positive}个，持平${stats.zero}个，负收益${stats.negative}个，有效样本${stats.count}个。</p>${stats.count && stats.negative===0?'<p>本次历史样本未出现负收益，不代表未来获利概率。</p>':''}</div></header><div class="chart-grid"><div id="ohReturnChart" class="oh-chart"></div><div id="ohOutcomeChart" class="oh-chart"></div></div></section><section class="result-block"><header><div><h2>结构指标与事件统计</h2><p>${escapeText(backtest.metric_profile?.display_name || '按产品结构汇总')}</p></div></header><div class="chart-grid"><div id="ohEventChart" class="oh-chart"></div><div id="ohAnnualChart" class="oh-chart"></div></div></section><section class="result-block"><header><div><h2>分支覆盖与交易账本</h2><p>点击样本查看详情。</p><nav class="ledger-pages" aria-label="交易账本分页"><button class="button" data-ledger-page="prev" ${ledgerPage===0?'disabled':''}>上一页</button><span>${trades.length?ledgerPage*ledgerPageSize+1:0}–${Math.min((ledgerPage+1)*ledgerPageSize,trades.length)} / ${trades.length}</span><button class="button" data-ledger-page="next" ${(ledgerPage+1)*ledgerPageSize>=trades.length?'disabled':''}>下一页</button></nav></div></header><div class="ledger-area"><div class="table-wrap"><table class="audit-table"><thead><tr><th>入场日</th><th>出场日</th><th>路径</th><th>合同结算收益率</th></tr></thead><tbody>${trades.slice(ledgerPage * ledgerPageSize, (ledgerPage + 1) * ledgerPageSize).map((row, offset) => { const index=ledgerPage*ledgerPageSize+offset; return `<tr data-oh-trade="${index}" class="${index === selectedTrade ? 'selected' : ''}"><td>${escapeText(row.entry_date)}</td><td>${escapeText(row.exit_date)}</td><td>${escapeText(row.path_label || `路径${Number(row.path_id ?? 0) + 1}·情形${Number(row.case_id ?? 0) + 1}`)}</td><td>${percentText(row.contract_settlement_return ?? row.gross_return, 4)}</td></tr>`; }).join('') || '<tr><td colspan="4">当前结果未附逐笔账本</td></tr>'}</tbody></table></div><aside class="trade-inspector"><h2>样本详情</h2><div class="hint">${trade ? `入场日：${escapeText(trade.entry_date)}<br>出场日：${escapeText(trade.exit_date)}<br>实际天数：${numberText(trade.actual_calendar_days, 0)}<br>标的表现：${percentText(trade.terminal_performance, 4)}<br>合同结算收益率：${percentText(trade.contract_settlement_return ?? trade.gross_return, 4)}<br>事件：${escapeText(JSON.stringify(trade.events || {}))}` : '无逐笔样本'}</div></aside></div><div class="quality-grid" style="margin-top:14px"><div class="quality-item"><span>已观察分支</span><strong>${numberText(backtest.branch_coverage?.observed_pair_count, 0)}</strong></div><div class="quality-item"><span>声明分支</span><strong>${numberText(backtest.branch_coverage?.declared_pair_count, 0)}</strong></div><div class="quality-item"><span>跳过样本</span><strong>${numberText(backtest.skipped_count, 0)}</strong></div></div></section><details class="audit-details"><summary>合同、数据与运行审计</summary><table class="audit-table"><tbody><tr><th>run_id</th><td>${escapeText(record.run_id)}</td></tr><tr><th>contract_fingerprint</th><td class="mono">${escapeText(record.contract_fingerprint)}</td></tr><tr><th>execution_fingerprint</th><td class="mono">${escapeText(record.execution_fingerprint)}</td></tr><tr><th>数据区间</th><td>${escapeText(backtest.data_coverage?.date_start || backtest.data_coverage?.start_date || '—')}至${escapeText(backtest.data_coverage?.date_end || backtest.data_coverage?.end_date || '—')}</td></tr><tr><th>限制条件</th><td>${escapeText((backtest.limitations || record.limitations || []).map(value => typeof value === 'string' ? value : JSON.stringify(value)).join('；') || '无额外限制')}</td></tr></tbody></table></details></div>`;
  }

  backtesterModule = function enhancedBacktesterModule() {
    const item = currentCatalog();
    const record = currentBacktest();
    const result = item ? (record ? snapshotNotice('backtest') + backtestMarkup(record) : emptyResult('暂无真实回测结果', missingResultText('backtest'))) : emptyResult('选择产品', '选择后读取当前结果库真实摘要、结构指标、账本和审计信息。');
    return `<div class="module-grid">${leftPanel('历史回测')}<section class="center">${result}</section>${rightFields('backtester')}</div>${actionbar('', '查看历史结果', 'backtester', !item)}`;
  };

  function renderBacktestCharts() {
    const record = currentBacktest();
    const backtest = record?.backtest || {};
    if (!document.querySelector('#ohReturnChart')) return;
    const metrics = backtest.common_metrics || {};
    const distribution = metrics.return_distribution || {};
    const outcomes = backtest.outcome_summary || backtest.specialized_metrics?.selected_path_case_gross_return || [];
    const events = Object.values(backtest.event_summary || {});
    const annual = backtest.annual_summary || [];
    demoPlot(document.querySelector('#ohReturnChart'), [{ type: 'bar', x: Object.keys(distribution), y: Object.values(distribution), marker: { color: '#3f6078' }, hovertemplate: '%{x}<br>%{y}个样本<extra></extra>' }], demoPlotLayout('合同结算收益率区间', '样本数', { bargap: .32 }));
    demoPlot(document.querySelector('#ohOutcomeChart'), [{ type: 'bar', orientation: 'h', y: outcomes.map(item => item.label || item.code), x: outcomes.map(item => item.count), marker: { color: '#7892a5' }, hovertemplate: '%{y}<br>%{x}个样本<extra></extra>' }], demoPlotLayout('样本数', '', { margin: { l: 112, r: 24, t: 20, b: 44 } }));
    demoPlot(document.querySelector('#ohEventChart'), [{ type: 'bar', x: events.map(item => item.label || item.name), y: events.map(item => Number(item.trigger_rate || 0) * 100), marker: { color: events.map((_, index) => index % 2 ? '#3f6078' : '#ad2940') }, hovertemplate: '%{x}<br>%{y:.2f}%<extra></extra>' }], demoPlotLayout('事件', '触发率，%', { bargap: .42 }));
    demoPlot(document.querySelector('#ohAnnualChart'), [{ type: 'bar', name: '样本数', x: annual.map(item => String(item.year)), y: annual.map(item => item.sample_count), marker: { color: '#3f6078' } }, { type: 'scatter', mode: 'lines+markers', name: '平均收益率', x: annual.map(item => String(item.year)), y: annual.map(item => Number(item.average_gross_return ?? item.average_contract_settlement_return ?? 0) * 100), yaxis: 'y2', line: { color: '#ad2940', width: 2.2 } }], demoPlotLayout('入场年份', '样本数', { showlegend: true, yaxis2: { title: '平均收益率，%', overlaying: 'y', side: 'right', showgrid: false } }));
  }

  function selectedReportPath() {
    const selected = [...reportSelections].filter(id => DATA.reports.products[id]);
    if (selected.length === 1 && reportDeliveryMode === 'single') {
      return reportOutputType === 'card' ? DATA.reports.cards[selected[0]] : DATA.reports.products[selected[0]];
    }
    return reportOutputType === 'card' ? DATA.reports.comparison_card : DATA.reports.comparison;
  }

  reporterModule = function enhancedReporterModule() {
    const available = DATA.products.filter(item => DATA.reports.products[item.product_id]);
    const selected = [...reportSelections];
    const validSelection = selected.length > 0 && selected.every(id => DATA.reports.products[id]);
    const canGenerate = validSelection && (reportDeliveryMode !== 'single' || selected.length === 1);
    const activeName = activeReportPath.includes('comparison')
      ? (activeReportPath.includes('-card') ? '五结构研究简报' : '五结构完整研究报告')
      : `${activeReportPath.split('/').pop().replace('.html', '').replace('-card', '')}${activeReportPath.includes('-card') ? '研究简报' : '完整研究报告'}`;
    const formatRule = reportOutputType === 'card'
      ? '多结构研究简报横向展示关键条款、Greeks、回测摘要和风险，不包含损益图与图表。'
      : '完整报告固定为连续A4正文，包含七章目录、收益图、风险曲线、曲面热力图与回测明细。';
    return `<div class="report-layout"><section class="report-candidates"><div class="report-head"><div><h2>报告来源</h2><p class="report-help">历史报告，保留原计算结果。最新产品规则以当前目录为准。</p></div></div><label>研究与合同来源</label><select id="reportSource"><option>隔离真实运行 · 5个完整候选</option></select><div class="report-library"><button data-report-open="${DATA.reports.comparison}" class="${activeReportPath === DATA.reports.comparison ? 'active' : ''}"><strong>五结构完整研究报告</strong><span>横向对比 · HTML/PDF</span></button><button data-report-open="${DATA.reports.comparison_card}" class="${activeReportPath === DATA.reports.comparison_card ? 'active' : ''}"><strong>五结构研究简报</strong><span>对比简报 · HTML/PDF</span></button>${DATA.report_products.map(id => `<button data-report-open="${DATA.reports.products[id]}" class="${activeReportPath === DATA.reports.products[id] ? 'active' : ''}"><strong>${id} ${escapeText(catalog.get(id).name)}</strong><span>完整研究报告</span></button>`).join('')}</div></section><section class="report-selection"><div class="report-toolbar"><div><strong>选择分析结果</strong><span>${selected.length ? `已选择${selected.length}个候选` : '未选择候选'}</span></div><button class="button primary" data-run="reporter" ${canGenerate ? '' : 'disabled'}>打开报告</button></div><div class="candidate-scroll">${available.map(item => `<label class="candidate-row"><input type="checkbox" data-report-candidate="${item.product_id}" ${reportSelections.has(item.product_id) ? 'checked' : ''}><span><strong>${item.product_id} ${escapeText(item.name)}</strong><small><i class="evidence-tag available">Payoffer可用</i><i class="evidence-tag available">定价可用</i><i class="evidence-tag available">回测可用</i></small></span></label>`).join('')}</div><section class="report-preview"><div class="report-preview-head"><div><strong>${escapeText(activeName)}</strong><span>可编辑副本并导出。</span></div><button class="button primary" data-report-edit>编辑报告</button><a class="button" href="${activeReportPath}" download>下载HTML</a><a class="button" href="${activeReportPath.replace('.html', '.pdf')}" download>下载PDF</a></div><iframe title="已生成报告预览" src="${activeReportPath}"></iframe></section></section><aside class="report-settings"><h2>内容与格式</h2><div class="report-setting-group"><label>交付方式</label><select id="reportDeliveryMode"><option value="single" ${reportDeliveryMode === 'single' ? 'selected' : ''}>单结构</option><option value="batch" ${reportDeliveryMode === 'batch' ? 'selected' : ''}>批量独立报告</option><option value="combined" ${reportDeliveryMode === 'combined' ? 'selected' : ''}>组合索引与独立报告</option><option value="comparison" ${reportDeliveryMode === 'comparison' ? 'selected' : ''}>横向对比</option></select></div><fieldset class="report-setting-group"><legend>内容等级</legend><label class="report-choice"><input type="radio" name="reportOutputType" value="card" ${reportOutputType === 'card' ? 'checked' : ''}><span>研究简报</span></label><label class="report-choice disabled"><input type="radio" name="reportOutputType" value="quote" disabled><span>参考报价</span></label><label class="report-choice"><input type="radio" name="reportOutputType" value="report" ${reportOutputType === 'report' ? 'checked' : ''}><span>完整研究报告</span></label></fieldset><fieldset class="report-setting-group"><legend>输出格式</legend><label class="report-choice"><input type="radio" name="reportFormat" value="html" ${reportFormat === 'html' ? 'checked' : ''}><span>HTML</span></label><label class="report-choice"><input type="radio" name="reportFormat" value="pdf" ${reportFormat === 'pdf' ? 'checked' : ''}><span>PDF</span></label></fieldset><fieldset class="report-setting-group"><legend>纳入内容</legend>${['产品建议', '收益结构', '估值定价', '历史回测'].map(label => `<label class="report-choice"><input type="checkbox" checked><span>${label}</span></label>`).join('')}</fieldset><div class="report-setting-group"><label>受众</label><select><option>专业用户</option></select></div><div class="report-setting-group"><label>报告规则</label><p>${formatRule}</p><p class="quote-rule">参考报价未启用：5个候选不存在同一适用日期的正式报价组合。</p></div></aside></div>`;
  };

  function renderMarketChart() {
    const element = document.querySelector('#marketChart');
    if (!element || !window.echarts) return;
    const asset = DATA.market_assets[activeDataSymbol];
    const chart = echarts.init(element);
    chart.setOption({ animation: false, grid: { left: 54, right: 20, top: 25, bottom: 42 }, tooltip: { trigger: 'axis' }, dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 8 }], xAxis: { type: 'category', data: asset.series.map(row => row.date), boundaryGap: false, axisLabel: { fontSize: 9, color: '#6f7880' }, axisLine: { lineStyle: { color: '#cfd3d6' } } }, yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9, color: '#6f7880' }, splitLine: { lineStyle: { color: '#eceeef' } } }, series: [{ type: 'line', data: asset.series.map(row => row.close), symbol: 'none', lineStyle: { color: '#3f6078', width: 1.8 }, areaStyle: { color: 'rgba(63,96,120,.08)' } }] });
    charts.push({ resize: () => chart.resize() });
  }

  function installTenorControls() {
    document.querySelectorAll('[data-term-key="T"]').forEach(group=>{
      const input=group.querySelector('input'),select=group.querySelector('select'),message=group.querySelector('small');
      const scale={year:1,month:12,day:365};
      let previousUnit=select.value;
      const update=()=>{const years=Number(input.value)/scale[select.value];message.textContent=input.value && Number.isFinite(years) && years>0 ? 'ACT/365年数：'+Number(years.toFixed(8)) : '请输入大于0的期限';};
      select.addEventListener('change',()=>{const years=Number(input.value)/scale[previousUnit];if(input.value && Number.isFinite(years))input.value=String(Number((years*scale[select.value]).toFixed(8)));previousUnit=select.value;update();captureInputs();});
      input.addEventListener('input',update);update();
    });
  }

  function installEnhancedControls() {
    restoreInputs();
    installTenorControls();
    enhanceAllSelects(document.querySelector('#moduleHost'));
    document.querySelectorAll('.side.right input,.side.right select,.side.right textarea').forEach(field=>field.addEventListener('input',captureInputs));
    document.querySelectorAll('[data-ledger-page]').forEach(button=>button.onclick=()=>{captureInputs();ledgerPage+=button.dataset.ledgerPage==='next'?1:-1;selectedTrade=ledgerPage*ledgerPageSize;renderModule();});
    document.querySelector('[data-report-edit]')?.addEventListener('click',()=>window.OptionHelperDemoReportEditor?.open({path:activeReportPath}));
    document.querySelectorAll('.product-select').forEach(select => select.addEventListener('change', event => {
      productId = event.target.value;
      selectedPayoffPath = 0;
      selectedTrade = 0; ledgerPage=0; moduleInputs.delete(inputKey()); moduleProducts[moduleName]=productId;
      activeGreek = 'delta';
      activeCurveKey = '';
      activeSurfaceView = '3d';
      demoErrors[moduleName] = '';
      renderModule();
    }, { once: true }));
    document.querySelector('#dataAssetSelect')?.addEventListener('change', event => { activeDataSymbol = event.target.value; renderModule(); });
    document.querySelectorAll('[data-data-symbol]').forEach(button => button.onclick = () => { activeDataSymbol = button.dataset.dataSymbol; renderModule(); });
    document.querySelectorAll('[data-path-index]').forEach(row => {
      const select = () => { selectedPayoffPath = Number(row.dataset.pathIndex); renderModule(); };
      row.onclick = select;
      row.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(); } };
    });
    document.querySelectorAll('[data-greek]').forEach(button => button.onclick = () => { activeGreek = button.dataset.greek; activeCurveKey = ''; renderModule(); });
    document.querySelectorAll('[data-surface-view]').forEach(button => button.onclick = () => { activeSurfaceView = button.dataset.surfaceView; renderPricingSurface(); });
    document.querySelector('#surfaceReset')?.addEventListener('click', () => {
      const node = document.querySelector('#greekSurfaceChart');
      if (node?._fullLayout) Plotly.relayout(node, { 'scene.camera': { projection: { type: 'orthographic' }, eye: { x: 1.45, y: -1.55, z: 1.08 }, center: { x: 0, y: 0, z: -.08 } } });
    });
    document.querySelectorAll('[data-oh-trade]').forEach(row => row.onclick = () => { selectedTrade = Number(row.dataset.ohTrade); renderModule(); });
    document.querySelectorAll('[data-payoff-zoom]').forEach(button => button.onclick = () => {
      payoffZoom = button.dataset.payoffZoom === 'in' ? Math.min(1.6, payoffZoom + .1) : button.dataset.payoffZoom === 'out' ? Math.max(.6, payoffZoom - .1) : 1;
      renderModule();
    });
    document.querySelector('[data-payoff-default]')?.addEventListener('click', () => { payoffZoom = 1; moduleInputs.delete(inputKey()); toast('已恢复最新默认条款与图形'); renderModule(); });
    document.querySelectorAll('[data-report-candidate]').forEach(input => input.addEventListener('change', () => { input.checked ? reportSelections.add(input.dataset.reportCandidate) : reportSelections.delete(input.dataset.reportCandidate); renderModule(); }));
    document.querySelectorAll('[data-report-open]').forEach(button => button.onclick = () => { activeReportPath = button.dataset.reportOpen; renderModule(); });
    document.querySelector('#reportDeliveryMode')?.addEventListener('change', event => { reportDeliveryMode = event.target.value; renderModule(); });
    document.querySelectorAll('[name="reportOutputType"]').forEach(input => input.addEventListener('change', () => { reportOutputType = input.value; activeReportPath = selectedReportPath(); renderModule(); }));
    document.querySelectorAll('[name="reportFormat"]').forEach(input => input.addEventListener('change', () => { reportFormat = input.value; }));
    document.querySelector('#persistData')?.addEventListener('change', event => { dataPersistence = event.target.checked; });
    document.querySelectorAll('[data-run]').forEach(button => button.onclick = () => runDemo(button.dataset.run));
    document.querySelectorAll('[data-stop]').forEach(button => button.onclick = () => { demoRunState[button.dataset.stop] = 'idle'; renderModule(); });
  }

  renderModule = function enhancedRenderModule() {
    if(renderedModule !== moduleName){
      if(renderedModule)moduleProducts[renderedModule]=productId;
      productId=moduleProducts[moduleName]||productId;
      moduleInputs.delete(inputKey());ledgerPage=0;renderedModule=moduleName;
    }
    if(moduleName==='pricer')ensureHistoricalResult('pricing',productId);
    if(moduleName==='backtester')ensureHistoricalResult('backtest',productId);
    closeChoices();
    charts.forEach(chart => { try { chart.purge?.(); } catch (_error) {} });
    charts = [];
    if (!taskSelected) {
      document.querySelector('#moduleHost').innerHTML = '<div class="desk-empty"><div><h2>选择研究模块</h2><p>选择模块查看当前产品条款和历史结果。</p></div></div>';
      return;
    }
    const renderer = { datafetcher: dataModule, payoffer: payoffModule, pricer: pricerModule, backtester: backtesterModule, reporter: reporterModule }[moduleName];
    document.querySelector('#moduleHost').innerHTML = renderer ? renderer() : '';
    try { installDemoDynamicControls(); } catch (_error) {}
    try { installDemoPanelResizers(); } catch (_error) {}
    installEnhancedControls();
    if (moduleName === 'datafetcher') renderMarketChart();
    if (moduleName === 'pricer' && demoPricingObjective !== 'fair_parameter') renderPricingCharts();
    if (moduleName === 'backtester') renderBacktestCharts();
  };

  runDemo = function enhancedRun(kind) {
    if(kind === 'reporter') {
      if(!reportSelections.size)return;
      activeReportPath=selectedReportPath();renderModule();toast('已打开历史报告');return;
    }
    if(kind === 'payoffer') {
      const item=currentCatalog();if(!item)return;
      const link=document.createElement('a');link.href=item.payoff.svg;link.download=`${item.product_id}.svg`;link.click();
      toast('导出的是当前产品默认条款图');return;
    }
    captureInputs();
    const record=kind==='pricer'?currentPricing():kind==='backtester'?currentBacktest():DATA.market_assets[activeDataSymbol];
    toast(record?'已显示历史快照；参数计算请在正式App运行。':'当前产品没有可用历史快照。');
  };

  function installOfflineRuntimeStatus() {
    const runtime = document.querySelector('[data-settings-pane="runtime"]');
    if (runtime) runtime.innerHTML = '<div class="setting-section__head"><div><h2>运行状态</h2><p>本Demo完全离线运行，只读取包内静态资源。</p></div><span class="connection-status">离线演示</span></div><dl class="runtime-status-list"><div><dt>Demo资源</dt><dd><strong>本地可用</strong><span>界面、产品目录、行情快照、图表和报告均来自当前目录。</span></dd></div><div><dt>计算服务</dt><dd><strong>未连接</strong><span>不会启动OptionHelper后端，也不会提交正式计算。</span></dd></div><div><dt>模型服务</dt><dd><strong>未连接</strong><span>OptChat展示交互与能力范围，不调用外部模型。</span></dd></div><div><dt>iFind数据</dt><dd><strong>未连接</strong><span>DataFetcher只展示包内真实历史快照，不发起网络请求。</span></dd></div></dl>';
    const model = document.querySelector('[data-settings-pane="model"]');
    if (model) model.innerHTML = '<div class="setting-section__head"><div><h2>模型配置</h2><p>完全离线Demo不读取、保存或验证模型凭据。</p></div><span class="connection-status">未连接</span></div><div class="settings-empty"><strong>离线演示模式</strong><br>正式App中的Provider配置入口未在Demo中启用。</div>';
    const data = document.querySelector('[data-settings-pane="data"]');
    if (data) data.innerHTML = '<div class="setting-section__head"><div><h2>数据接口</h2><p>完全离线Demo不连接iFind或其他行情接口。</p></div><span class="connection-status">未连接</span></div><div class="settings-empty"><strong>使用本地行情快照</strong><br>包内数据只用于演示，不读取或保存任何真实凭据。</div>';
    const title = document.querySelector('#taskTitle');
    if (title && !document.querySelector('.offline-demo-badge')) title.insertAdjacentHTML('afterend','<span class="offline-demo-badge">完全离线Demo</span>');
  }

  installOfflineRuntimeStatus();
  document.querySelectorAll('select').forEach(enhanceSelect);
  if (document.querySelector('#appView.active') && mode === 'desk') renderModule();

  window.OptionHelperDemo = {
    data: DATA,
    results: RESULTS,
    openModule(name, id = productId) {
      taskSelected = true;
      moduleName = name;
      productId = id; moduleProducts[name]=id; renderedModule=name; moduleInputs.delete(inputKey());
      setMode('desk');
      document.querySelectorAll('#moduleTabs button').forEach(button => button.classList.toggle('active', button.dataset.module === name));
      renderModule();
    },
  };
})();
