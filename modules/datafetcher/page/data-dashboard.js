/* Desk research view. Every response belongs to one asset selection generation. */
(function (root) {
  'use strict';
  const tabs = [['overview', '总览'], ['prices', '走势与波动'], ['valuation', '估值'], ['etf', '净值与份额'], ['futures', '期货升贴水'], ['details', '原始数据']];
  const typeNames = {etf:'ETF', index:'指数', stock:'个股'};
  function hasSectionData(key, section) {
    if (!section || !['available','partial'].includes(section.status)) return false;
    const fields = {prices:['close','return_price'], valuation:['pe','pb','ps','dividend_yield','total_shares','market_value','turnover_ratio'], etf:['nav','shares'], futures:['basis','annualized_basis']}[key];
    if (!fields) return false;
    const records = key === 'futures' ? section.contracts : section.series;
    return Array.isArray(records) && records.some(row => fields.some(field => typeof row[field] === 'number' && Number.isFinite(row[field])));
  }
  function visibleTabs(data, panels = data?.sections || {}) {
    const sections = data?.available_sections || ['prices', 'details'];
    return tabs.filter(([key]) => key === 'overview' || (sections.includes(key) &&
      (key === 'prices' || key === 'details' || !(key in panels) || hasSectionData(key, panels[key]))));
  }
  const statusNames = {available: '已获取', partial: '部分可用', unavailable: '暂不可用', not_applicable: '不适用', cancelled: '已停止'};
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const number = (value, digits = 2) => finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '—';
  const percent = value => finite(value) ? `${number(value * 100)}%` : '—';
  const chartOption = (records, fields, percentage = false) => ({
    data: fields.map(([field, name], index) => ({
      name, type: field === 'volume' ? 'bar' : 'scatter', mode: 'lines', connectgaps: false,
      x: records.map(row => row.date), y: records.map(row => finite(row[field]) ? row[field] : null),
      line: {width: 1.8, color: ['#b52b40','#477fae','#bf7e88','#809caf'][index % 4]},
      marker: {color: '#477fae'},
      hovertemplate: `%{x}<br>${name}：%{y:${percentage ? '.2%' : ',.2f'}}<extra></extra>`,
    })),
    layout: {showlegend: fields.length > 1, hovermode: 'x unified',
      xaxis: {showgrid: false, tickformat: '%Y/%m'}, yaxis: {tickformat: percentage ? '.1%' : '~s', showgrid: false},
      margin: {l: 56, r: 12, t: fields.length > 1 ? 38 : 12, b: 36}},
  });
  function distributionOption(records) {
    return {
      data: [{name:'观察日数',type:'bar',x:records.map(row=>row.label),y:records.map(row=>row.count),
        marker:{color:records.map((row,index)=>index<5?'#477fae':'#b52b40')},
        hovertemplate:'%{x}<br>%{y}个交易日<extra></extra>'}],
      layout:{showlegend:false,margin:{l:44,r:12,t:16,b:68},
        xaxis:{type:'category',tickangle:-35},yaxis:{rangemode:'tozero',tickformat:'d',showgrid:false}},
    };
  }
  function overviewSummary(price, kind) {
    const research=price.research || {}, summary=price.summary || {};
    const row=(label,value,format=percent,colored=false)=>({label,value:format(value),tone:colored&&finite(value)?value>0?'dashboard-positive':value<0?'dashboard-negative':'':''});
    if(kind==='volatility') {
      const windows=research.volatility_windows || [];
      return [{title:'历史波动率',note:'最新观察值，均为年化值',rows:[20,60,122,244].map(window=>row(`${window}日`,windows.find(item=>item.window===window)?.value))},
        {title:'历史位置',note:'20日波动率在完整历史中的分位，不是隐含波动率',rows:[row('历史分位',price.hv_percentile?.value)]}];
    }
    if(kind==='drawdown') return [
      {title:'回撤幅度',note:'相对完整历史样本高点',rows:[row('当前回撤',research.current_drawdown),row('最大回撤',research.max_drawdown)]},
      {title:'近一年',note:price.year_coverage_complete?'最近一年价格路径':'样本不足一年，不补估算值',rows:[row('最大回撤',summary.drawdown_1y)]},
    ];
    if(kind==='volume') {
      const latest=price.series?.[price.series.length-1];
      return [{title:'成交量',note:'成交量沿用数据源原始单位',rows:[row('最新成交量',latest?.volume,value=>number(value,0)),row('相对20日均量',research.volume_ratio,value=>finite(value)?`${number(value)}倍`:'—')]},
        {title:'成交额',note:'沿用数据源原始单位；20日均量及均额均含当日',rows:[row('20日平均成交额',summary.amount_20d,value=>number(value,0))]}];
    }
    return [{title:'区间涨跌',note:'按交易日计算',rows:[1,5,20,60,122,244].map(window=>row(`${window}日`,price.returns?.[window],percent,true))},
      {title:'与均线的距离',note:'与价格曲线采用相同复权口径',rows:[row('距20日均线',research.ma20_distance),row('距60日均线',research.ma60_distance)]}];
  }
  function element(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; }
  const scriptElement = typeof document !== 'undefined' ? document.currentScript : null;
  const sourceScript = scriptElement?.src;
  const plotlySource = scriptElement?.dataset.plotlySrc;
  const chartSystemSource = scriptElement?.dataset.chartSystemSrc;
  let chartRuntime;
  function loadScript(src) {
    return new Promise((resolve,reject) => {
      const script=document.createElement('script'); script.src=src;
      script.onload=resolve;
      script.onerror=()=>{script.remove();reject(new Error('图表未能加载，可先查看数据明细。'));};
      document.head.append(script);
    });
  }
  function loadCharts() {
    if (root.Plotly && root.OptionHelperPlotlyCharts) return Promise.resolve(root.OptionHelperPlotlyCharts);
    if (!chartRuntime) chartRuntime = (async () => {
      if (!root.Plotly) await loadScript(plotlySource || new URL('../vendor/plotly-optionhelper.min.js',sourceScript).href);
      if (!root.OptionHelperPlotlyCharts) await loadScript(chartSystemSource || new URL('../plotly-chart-system.js',sourceScript).href);
      if (!root.Plotly || !root.OptionHelperPlotlyCharts) throw new Error('图表未能加载，可先查看数据明细。');
      return root.OptionHelperPlotlyCharts;
    })().catch(error=>{chartRuntime=null;throw error;});
    return chartRuntime;
  }
  class Dashboard {
    constructor(container, request) {
      this.container = container; this.request = request; this.generation = 0; this.charts = []; this.pending = new Map(); this.tab = 'overview'; this.chartWindow = 244; this.analysisOpen = false; this.overviewChart = 'price';
      this.queue = []; this.activeRequest = null; this.cache = new Map();
      this.resize = new ResizeObserver(() => this.charts.forEach(chart => chart.resize())); this.resize.observe(container);
      if (typeof MutationObserver !== 'undefined') {
        this.themeObserver = new MutationObserver(() => { if(this.reference) this.render(); });
        this.themeObserver.observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
      }
    }
    cancel() { this.generation++; for (const entry of this.pending.values()) entry.controller.abort(); this.pending.clear(); }
    dispose() { this.cancel(); this.cache.clear(); this.clearCharts(); this.resize.disconnect(); this.themeObserver?.disconnect(); }
    clearCharts() { this.charts.forEach(chart => chart.dispose()); this.charts = []; }
    cacheKey(reference, asset) { return JSON.stringify([reference, asset]); }
    async show(reference, selected) {
      this.cancel(); this.clearCharts(); this.reference = reference; this.asset = selected || reference.asset_ids[0];
      this.data = null; this.panels = {}; this.tab = 'overview'; this.chartWindow = 244; this.analysisOpen = false; this.overviewChart = 'price';
      const key = this.cacheKey(reference, this.asset), saved = this.cache.get(key);
      if (saved && Date.now() < saved.expires) {
        this.data = saved.data; this.panels = {...saved.panels};
        this.cache.delete(key); this.cache.set(key, saved);
      } else this.cache.delete(key);
      this.render(); await this.load('prices');
    }
    stop(section) {
      const entry = this.pending.get(section);
      if (!entry) return;
      entry.controller.abort(); this.render();
    }
    pendingLabel(section) {
      const entry = this.pending.get(section);
      return entry?.controller.signal.aborted ? '正在停止…' : entry?.phase === 'queued' ? '等待前一请求结束…' : '正在获取本板块数据…';
    }
    load(section) {
      if (this.pending.has(section)) return this.pending.get(section).promise;
      if (this.panels[section]) return Promise.resolve();
      const entry = {section, generation: this.generation, controller: new AbortController(), phase: 'queued',
        reference: this.reference, asset: this.asset};
      entry.promise = new Promise(resolve => { entry.resolve = resolve; });
      this.pending.set(section, entry); this.queue.push(entry); this.render(); void this.drain();
      return entry.promise;
    }
    async drain() {
      if (this.activeRequest) return;
      const entry = this.queue.shift();
      if (!entry) return;
      this.activeRequest = entry;
      const {section, generation, controller, reference, asset} = entry;
      try {
        if (generation !== this.generation || controller.signal.aborted) return;
        entry.phase = 'running'; this.render();
        const result = await this.request('/api/dashboard', {method: 'POST', headers: {'Content-Type': 'application/json'}, signal: controller.signal,
          body: JSON.stringify({data_asset_id: reference.data_asset_id, asset_id: asset, sections: [section]})});
        if (generation !== this.generation || controller.signal.aborted) return;
        this.data = result.dashboard; Object.assign(this.panels, result.dashboard.sections);
        const panels = Object.fromEntries(Object.entries(this.panels).filter(([, panel]) => ['available', 'partial'].includes(panel.status)));
        const key = this.cacheKey(reference, asset), previous = this.cache.get(key);
        this.cache.delete(key);
        this.cache.set(key, {data: this.data, panels, expires: previous?.expires || Date.now() + 120000});
        while (this.cache.size > 4) this.cache.delete(this.cache.keys().next().value);
      } catch (error) {
        if (generation === this.generation && !controller.signal.aborted) {
          this.panels[section] = {status: 'unavailable', message: error.message || '本板块加载失败，可重试。'};
        }
      } finally {
        if (generation === this.generation && this.pending.get(section) === entry) {
          if (controller.signal.aborted) this.panels[section] = {status: 'cancelled', message: '可重新获取。'};
          this.pending.delete(section); this.render();
        }
        this.activeRequest = null; entry.resolve(); void this.drain();
      }
    }
    chart(parent, title, records, fields, percentage = false) {
      const populated = fields.filter(([field]) => records.some(row => finite(row[field])));
      if (!populated.length) return;
      return this.plot(parent, title, chartOption(records, populated, percentage));
    }
    async plot(parent, title, option) {
      const wrap=element('section','dashboard-chart-section'), heading=element('h3','',title), canvas=element('div','dashboard-chart');
      const reset=element('button','dashboard-chart-reset','重置缩放'); reset.type='button';
      const header=element('div','dashboard-plot-heading'); header.append(heading,reset);
      canvas.setAttribute('role','img'); canvas.setAttribute('aria-label',title); wrap.append(header,canvas); parent.append(wrap);
      try {
        const runtime=await loadCharts(); if(!canvas.isConnected) return;
        const layout=runtime.cartesianLayout(option.layout);
        const palette=runtime.colors(), colors=[palette.risk,palette.primary,palette.riskMid,palette.primaryMid];
        const traces=option.data.map((trace,index)=>({...trace,
          ...(trace.line?{line:{...trace.line,color:colors[index % colors.length]}}:{}),
          ...(trace.marker?{marker:{...trace.marker,color:Array.isArray(trace.marker.color)?trace.marker.color.map(color=>color==='#477fae'?palette.primary:palette.risk):palette.primary}}:{}),
        }));
        layout.yaxis.gridcolor=runtime.colors().rule;
        const chart={dispose:()=>runtime.purge(canvas),resize:()=>runtime.resize(canvas)}; this.charts.push(chart);
        await runtime.render(canvas,traces,layout,{scrollZoom:false});
        if(!canvas.isConnected){runtime.purge(canvas);return;}
        reset.onclick=()=>root.Plotly.relayout(canvas,{'xaxis.autorange':true,'yaxis.autorange':true});
      } catch(error) { if(canvas.isConnected) canvas.replaceWith(element('p','dashboard-note',error.message)); }
    }
    table(parent, records, columns) {
      const wrap = element('div', 'dashboard-table-wrap'); wrap.tabIndex = 0; wrap.setAttribute('aria-label', '数据表，可横向滚动');
      const table = element('table', 'dashboard-table'), head = element('thead'), tr = element('tr'), body = element('tbody');
      for (const [, label] of columns) { const th = element('th', '', label); th.scope = 'col'; tr.append(th); } head.append(tr);
      for (const row of records) { const line = element('tr'); for (const [key, , format] of columns) { const cell=element('td','',format?format(row[key]):row[key]??'—'); if(key==='value'&&row.exactValue)cell.title='原始数值：'+row.exactValue; line.append(cell); } body.append(line); }
      table.append(head, body); wrap.append(table); parent.append(wrap);
    }
    metricRows(parent, title, records) {
      const section = element('section', 'dashboard-summary-section');
      section.append(element('h3', '', title));
      this.table(section, records, [['label','指标'],['value','数值'],['note','口径']]);
      parent.append(section);
    }
    renderOverview(parent, price) {
      const layout=element('div','dashboard-overview-layout');
      const main=element('div','dashboard-overview-main'), aside=element('aside','dashboard-overview-aside');
      aside.setAttribute('aria-label','当前图表摘要');
      layout.append(main,aside);parent.append(layout);
      this.renderOverviewSummary(aside,price);
      const toolbar = element('div','dashboard-chart-toolbar');
      const choices=element('div','dashboard-chart-choices'); choices.setAttribute('role','group'); choices.setAttribute('aria-label','选择主图');
      for(const [key,label] of [['price','价格走势'],['volatility','波动率'],['drawdown','回撤'],['volume','成交量']]) {
        const button=element('button','',label);button.type='button';button.dataset.chart=key;
        button.setAttribute('aria-pressed',String(this.overviewChart===key));
        button.onclick=()=>{this.overviewChart=key;this.render();this.container.querySelector(`[data-chart="${key}"]`)?.focus();}; choices.append(button);
      }
      toolbar.append(choices);
      const ranges = element('div','dashboard-range'); ranges.setAttribute('role','group'); ranges.setAttribute('aria-label','图表区间');
      for (const [count,label] of [[60,'近60日'],[244,'近244日'],[0,'全部']]) {
        const button=element('button','',label); button.type='button'; button.setAttribute('aria-pressed',String(this.chartWindow===count));
        button.onclick=()=>{this.chartWindow=count;this.render();}; ranges.append(button);
      }
      toolbar.append(ranges); main.append(toolbar);
      const series = this.chartWindow ? price.series.slice(-this.chartWindow) : price.series;
      const charts=element('div','dashboard-main-chart'); main.append(charts);
      const selected={
        price:[price.price_field==='close'?'价格与均线':'复权价格与均线',[['return_price','价格'],['ma20','20日均线'],['ma60','60日均线']],false],
        volatility:['历史波动率',[['hv20','20日'],['hv60','60日'],['hv244','244日']],true],
        drawdown:['从历史高点回落的幅度',[['drawdown','回撤']],true],
        volume:['成交量',[['volume','成交量（原始单位）']],false],
      }[this.overviewChart];
      void this.chart(charts,selected[0],series,selected[1],selected[2]);
      const specialtyKey=this.data?.asset_class==='etf'?'etf':'valuation';
      if(this.panels[specialtyKey]) this.renderSpecialty(parent);
      const more=element('details','dashboard-more'); more.open=this.analysisOpen;
      more.append(element('summary','','更多统计：回撤、涨跌分布与波动率比较'));
      const body=element('div'); more.append(body); parent.append(more);
      const fill=()=>{if(!body.childElementCount) this.renderAnalysis(body,price);};
      more.addEventListener('toggle',()=>{this.analysisOpen=more.open;if(more.open){fill();this.charts.forEach(chart=>chart.resize());}});
      if(more.open) fill();
      parent.append(element('p','dashboard-note',`历史样本：${price.start_date || '—'}至${price.as_of || '—'}。拖动图表可放大，双击或点击“重置缩放”恢复。`));
    }
    renderOverviewSummary(parent,price) {
      for(const item of overviewSummary(price,this.overviewChart)) {
        const section=element('section','dashboard-side-section'),values=element('dl','dashboard-side-values');
        section.append(element('h3','',item.title));
        for(const value of item.rows) {
          const row=element('div');row.append(element('dt','',value.label),element('dd',value.tone,value.value));values.append(row);
        }
        section.append(values,element('p','dashboard-note',item.note));parent.append(section);
      }
    }
    renderAnalysis(parent,price) {
      const research=price.research || {},summary=price.summary || {};
      parent.append(element('p','dashboard-note',`共${research.observations ?? '—'}个有效日收益。以下统计使用完整历史，不随图表显示范围变化，也不代表未来收益。`));
      const analysis=element('div','dashboard-summary-grid'); parent.append(analysis);
      this.metricRows(analysis,'趋势与风险',[
        {label:'偏离20日均线',value:percent(research.ma20_distance),note:'同一复权口径'},
        {label:'偏离60日均线',value:percent(research.ma60_distance),note:'同一复权口径'},
        {label:'当前回撤',value:percent(research.current_drawdown),note:'距样本区间高点'},
        {label:'区间最大回撤',value:percent(research.max_drawdown),note:'完整历史'},
        {label:'近一年最大回撤',value:percent(summary.drawdown_1y),note:price.year_coverage_complete?'最近一年':'样本不足一年'},
        {label:'成交量相对20日均量',value:finite(research.volume_ratio)?`${number(research.volume_ratio)}倍`:'—',note:'含当日'},
      ]);
      this.metricRows(analysis,'历史日收益',[
        {label:'上涨日占比',value:percent(research.up_day_share),note:'零收益不计上涨'},
        {label:'5%分位',value:percent(research.return_p05),note:'历史左尾'},
        {label:'中位数',value:percent(research.return_median),note:'历史样本'},
        {label:'95%分位',value:percent(research.return_p95),note:'历史右尾'},
        {label:'最差单日',value:percent(research.worst_day),note:'简单收益率'},
        {label:'最好单日',value:percent(research.best_day),note:'简单收益率'},
      ]);
      if (research.distribution?.length) void this.plot(analysis,'日收益分布',distributionOption(research.distribution));
      if (research.volatility_windows?.length) {
        const points=research.volatility_windows.map(row=>({date:`${row.window}日`,value:row.value}));
        const option=chartOption(points,[['value','历史波动率']],true); option.layout.xaxis={type:'category'};
        void this.plot(analysis,'不同观察窗口的历史波动率',option);
      }
    }
    renderSpecialty(parent) {
      const key=this.data?.asset_class==='etf'?'etf':'valuation';
      if (!this.data?.available_sections?.includes(key)) return;
      const panel=this.panels[key], section=element('section','dashboard-specialty');
      const heading=element('div','dashboard-chart-toolbar');
      heading.append(element('h3','',key==='etf'?'ETF净值与份额概况':'估值概况'));
      const button=element('button','button button-quiet',this.pending.has(key)?'停止获取':panel?.status==='available'||panel?.status==='partial'?'查看详情':key==='etf'?'获取净值与份额':'获取估值'); button.type='button';
      button.onclick=()=>{
        if(this.pending.has(key)){this.stop(key);return;}
        if(panel?.status==='available'||panel?.status==='partial'){this.tab=key;this.render();return;}
        delete this.panels[key]; void this.load(key);
      }; heading.append(button); section.append(heading);
      if(this.pending.has(key)) section.append(element('p','dashboard-note',this.pendingLabel(key)));
      else if(!panel) section.append(element('p','dashboard-note',key==='etf'?'查看ETF折溢价及份额变化。':'查看市盈率、市净率及历史分位。'));
      else if((key==='etf' && panel.summary) || panel.indicators) {
        const metrics=element('dl','dashboard-specialty-values');
        const labels=panel.labels || {pe:'PE',pb:'PB',dividend_yield:'股息率'};
        const entries=key==='etf'?[
          ['单位净值',number(panel.summary.nav,4),panel.as_of],
          ['折溢价',percent(panel.summary.premium),`区间分位${percent(panel.premium_percentile?.value)}`],
          ['基金份额',number(panel.summary.shares,0),panel.as_of],
          ['近20期份额变化',percent(panel.summary.shares_change_20d),'份额变化不等于净流入'],
        ]:Object.entries(panel.indicators).map(([field,value])=>[
          labels[field],field==='dividend_yield'?percent(value.value):number(value.value),
          `${value.as_of || '—'}，分位${percent(value.percentile?.value)}`,
        ]);
        for(const [label,value,note] of entries){
          const item=element('div');item.append(element('dt','',label),element('dd','',value),element('small','',note));metrics.append(item);
        }
        section.append(metrics);
      } else section.append(element('p','dashboard-note',panel.message || '本板块暂不可用。'));
      parent.append(section);
    }
    render() {
      this.clearCharts(); const rootNode = element('div', 'data-dashboard');
      const header = element('div', 'dashboard-heading'), name = element('div'), select = element('select', 'control');
      select.setAttribute('aria-label', '切换标的');
      for (const asset of this.reference.asset_ids) { const option = element('option', '', asset); option.value = asset; select.append(option); }
      select.value = this.asset; select.addEventListener('change', () => void this.show(this.reference, select.value));
      name.append(element('h2', '', `${this.asset} ${typeNames[this.data?.asset_class] || '标的'}`), element('p', 'dashboard-note', this.data ? `截至${this.data.as_of || '—'}，日频` : '正在读取已获取的行情'));
      header.append(name);if(this.reference.asset_ids.length>1){const picker=element('label','dashboard-asset-picker');select.setAttribute('aria-label','查看本批标的');picker.append(element('span','','查看本批标的'),select);header.append(picker);} rootNode.append(header);
      const price = this.panels.prices, summary = price?.summary || {}, overview = element('div', 'dashboard-overview');
      for (const [label, value] of [['收盘价', number(summary.close,this.data?.asset_class==='etf'?3:2)], ['20日涨跌', percent(summary.change_20d)], ['20日历史波动率', percent(summary.hv20)], ['波动率历史分位', percent(price?.hv_percentile?.value)]]) {
        const item=element('div');item.append(element('span','',label),element('strong','',value));
        const hints={'20日涨跌':'最近20个交易日的价格变化','20日历史波动率':'按最近20个交易日计算，已年化','波动率历史分位':'越低，表示相对自身历史波动越小'};if(hints[label]) item.title=hints[label];overview.append(item);
      }
      rootNode.append(overview);
      const definitions = element('details','dashboard-definitions');
      definitions.append(element('summary','','数据来源与计算说明'), element('p','dashboard-note',`行情来源：${price?.source || '当前任务已获取的数据'}。价格口径：${price?.price_field === 'close' ? '未复权收盘价' : price?.price_field ? '复权价格' : '待获取'}。`), element('p','dashboard-note','本页展示历史研究数据，不会写入定价或回测参数。历史波动率不是隐含波动率；历史股息率不等同于定价使用的未来分红率。定价口径在相应模块确认。'));

      const shownTabs = visibleTabs(this.data, this.panels);
      if (!shownTabs.some(([key]) => key === this.tab)) this.tab = 'overview';
      const nav = element('div', 'dashboard-tabs'); nav.setAttribute('role', 'tablist'); nav.setAttribute('aria-label', '研究板块');
      for (const [key, label] of shownTabs) {
        const button = element('button', '', label); button.type = 'button'; button.setAttribute('role', 'tab'); button.setAttribute('aria-selected', String(this.tab === key)); button.tabIndex = this.tab === key ? 0 : -1;
        button.id = `dashboard-tab-${key}`; button.setAttribute('aria-controls', 'dashboard-panel');
        button.addEventListener('click', () => { this.tab = key; this.render(); if (key !== 'details') void this.load(key === 'overview' ? 'prices' : key); });
        button.addEventListener('keydown', event => { if (!['ArrowRight','ArrowLeft','Home','End'].includes(event.key)) return; event.preventDefault(); const index = shownTabs.findIndex(t => t[0] === key), next = event.key === 'Home' ? 0 : event.key === 'End' ? shownTabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + shownTabs.length) % shownTabs.length; this.tab = shownTabs[next][0]; this.render(); document.getElementById(`dashboard-tab-${this.tab}`)?.focus(); if (this.tab !== 'details') void this.load(this.tab === 'overview' ? 'prices' : this.tab); });
        nav.append(button);
      }
      rootNode.append(nav); const content = element('section', 'dashboard-panel'); content.id = 'dashboard-panel'; content.setAttribute('role', 'tabpanel'); content.setAttribute('aria-labelledby', `dashboard-tab-${this.tab}`); rootNode.append(content);
      const sourceTab = this.tab === 'overview' ? 'prices' : this.tab;
      const current = this.panels[sourceTab];
      if (this.pending.has(sourceTab)) {
        const loading = element('div', 'dashboard-panel-state'); loading.setAttribute('role', 'status'); loading.append(element('p', '', this.pendingLabel(sourceTab)));
        const stop = element('button', 'button button-quiet', '停止'); stop.type = 'button'; stop.disabled = this.pending.get(sourceTab).controller.signal.aborted; stop.onclick = () => this.stop(sourceTab); loading.append(stop); content.append(loading);
      } else if (this.tab === 'details') {
        content.append(element('p', 'dashboard-note', `价格口径：${price?.price_field || '—'}；波动率年化交易日：${price?.annualization || '—'}。缺失值保留为空，不补零。`));
        const records = price?.records || []; this.table(content, records.slice(-100).reverse(), [['date','日期'],['close','收盘',number],['adj_close','复权收盘',number],['volume','成交量',v=>number(v,0)]]);
        content.append(element('p', 'dashboard-note', `表格展示最近${Math.min(records.length,100)}条，共${records.length}条。图表使用全部观察值。`));
      } else if (current) {
        const noteText=[current.status==='available'?'':statusNames[current.status],current.source,current.message].filter(Boolean).join('，');
        if(noteText)content.append(element('p','dashboard-note',noteText));
        if (['unavailable','cancelled'].includes(current.status)) { const retry = element('button','button button-quiet','重新获取'); retry.type='button'; retry.onclick=()=>{delete this.panels[sourceTab]; void this.load(sourceTab);}; content.append(retry); }
        if (this.tab === 'overview' && current.series) {
          this.renderOverview(content, current);
        } else if (this.tab === 'prices' && current.series) {
          void this.chart(content, '价格走势', current.series, current.price_field === 'close' ? [['close','收盘价']] : [['close','收盘价'],['return_price','复权价格']]);
          void this.chart(content, '历史波动率', current.series, [["hv20","20日"],["hv60","60日"],["hv122","122日"],["hv244","244日"]], true);
          void this.chart(content, '区间回撤', current.series, [['drawdown','距区间高点']], true);
          this.table(content, [
            {label:'近一年最大回撤', value:percent(current.summary?.drawdown_1y), note:current.year_coverage_complete?'近一年价格路径':'需要完整一年行情'},
            {label:'近20日平均成交额', value:number(current.summary?.amount_20d), note:'沿用原始数据单位；缺失不补值'},
          ], [['label','风险与流动性'],['value','数值'],['note','口径']]);
          this.table(content, Object.entries(current.returns).map(([window,value])=>({window:`${window}个交易日`,value})), [['window','观察窗口'],['value','区间涨跌',percent]]);
        } else if (this.tab === 'valuation' && current.indicators) {
          const labels = current.labels || {pe:'PE',pb:'PB',dividend_yield:'股息率'};
          this.table(content, Object.entries(current.indicators).filter(([,v])=>finite(v.value)).map(([field,v])=>({label:labels[field],value:['dividend_yield','turnover_ratio'].includes(field)?percent(v.value):(['total_shares','market_value'].includes(field)?number(v.value/1e8)+'亿':number(v.value)),exactValue:number(v.value,['total_shares','market_value'].includes(field)?0:6),date:v.as_of,percentile:v.percentile?.value})), [['label','指标'],['value','数值'],['date','数据日期'],['percentile','区间分位',percent]]);
          if (current.series?.length) { void this.chart(content,'PE历史',current.series,[['pe',labels.pe]]); void this.chart(content,'PB历史',current.series,[['pb',labels.pb]]); }
        } else if (this.tab === 'etf' && current.series) {
          void this.chart(content,'价格与单位净值',current.series,[['close','收盘价'],['nav','单位净值']]);
          void this.chart(content,'ETF折溢价',current.series,[['premium','折溢价']],true);
          void this.chart(content,'基金份额',current.series,[['shares','份额']]);
          this.table(content,current.series.slice(-20).reverse(),[['date','日期'],['nav','单位净值',v=>number(v,4)],['shares','份额',v=>number(v,0)],['shares_change','份额增减',v=>number(v,0)],['premium','折溢价',percent]]);
        } else if (this.tab === 'futures' && current.contracts) {
          this.table(content,current.contracts,[['contract_id','合约'],['expiry','到期日'],['basis','升贴水',number],['annualized_basis','年化升贴水',percent]]);
        } else if (this.tab === 'options' && current.expiries) {
          this.table(content,current.expiries,[['expiry','到期日'],['iv_method','IV口径'],['call_iv','平值认购IV',percent],['put_iv','平值认沽IV',percent],['volume_pcr','成交量PCR',number]]);
        }
      }
      rootNode.append(definitions);
      this.container.replaceChildren(rootNode);
    }
  }
  function installDeskLayout(workbench, toolbar) {
    if (!workbench || !toolbar || workbench.dataset.dashboardLayout) return;
    workbench.dataset.dashboardLayout = 'query';
    const form=workbench.querySelector('#request-form'), canvas=workbench.querySelector('.canvas-panel');
    const library=workbench.querySelector('.library-panel'), inspector=workbench.querySelector('.inspector-panel');
    const query=element('section','dashboard-query'); query.setAttribute('aria-label','查询行情');
    const groups=Array.from(form.querySelectorAll(':scope > .inspector-group'));
    form.querySelector(':scope > h2')?.remove();
    const scope=groups.shift(); scope.classList.add('dashboard-query-fields'); scope.querySelector('h3')?.remove();
    scope.querySelector('label[for="asset-ids"] > span').textContent='标的代码';
    const input=form.querySelector('#asset-ids'); input.rows=1; input.placeholder='例如000300.SH、510300.SH';
    const submit=toolbar.querySelector('#submit-request'); scope.append(submit);
    const advanced=element('details','dashboard-query-details'); advanced.id='dashboard-query-options';
    advanced.append(element('summary','','更多设置'),...groups);
    const recent=element('details','dashboard-query-details dashboard-recent'); recent.id='dashboard-recent';
    recent.append(element('summary','','最近查看'),library);
    library.querySelector('.library-heading')?.remove();
    library.querySelector('#asset-search')?.addEventListener('keydown',event=>{if(event.key==='Enter') event.preventDefault();});
    library.querySelector('#asset-list')?.addEventListener('click',event=>{
      if(event.target.closest('.asset-select,.asset-example')) recent.open=false;
    });
    const extras=element('div','dashboard-query-extras'); extras.append(recent,advanced);
    const state=workbench.querySelector('.canvas-state'); if(state) extras.append(state);
    form.replaceChildren(scope,extras); query.append(form);
    canvas.prepend(query); toolbar.closest('.canvas-toolbar').remove(); inspector.remove();
    workbench.querySelectorAll('.panel-resizer').forEach(node=>node.remove());
    const empty=workbench.querySelector('#empty-canvas');
    if(empty){empty.querySelector('strong').textContent='先选择要研究的标的';empty.querySelector('span').textContent='在上方填写代码和日期，点击获取数据后查看分析。';}
    return {toggle(key){const target=key==='library'?recent:advanced;target.open=!target.open;}};
  }
  const api = {Dashboard, number, percent, chartOption, visibleTabs, distributionOption, overviewSummary, installDeskLayout, hasSectionData};
  if (typeof module === 'object' && module.exports) module.exports = api; else root.OptionHelperDashboard = api;
})(globalThis);
