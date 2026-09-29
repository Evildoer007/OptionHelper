/* Isolated responsive candidate. Uses the existing verified data and Plotly renderer. */
(() => {
  const api = window.OptionHelperDashboard;
  class AdaptiveDashboard extends api.Dashboard {
    constructor(container, request) {
      super(container, request);
      this.resize.disconnect();
      this.lastWidth = container.getBoundingClientRect().width;
      this.wide = this.lastWidth >= 940;
      this.resize = new ResizeObserver(entries => {
        const width = entries[0].contentRect.width;
        if (width <= 0 || Math.abs(width - this.lastWidth) < 1) return;
        this.lastWidth = width;
        cancelAnimationFrame(this.resizeFrame);
        this.resizeFrame = requestAnimationFrame(() => {
          const wide = this.lastWidth >= 940;
          if (wide !== this.wide) {
            this.wide = wide;
            if (this.reference) this.render();
          } else {
            this.charts.forEach(chart => chart.resize());
          }
        });
      });
      this.resize.observe(container);
    }
    async show(reference, selected) {
      const loading = super.show(reference, selected);
      const generation = this.generation;
      await loading;
      if (generation !== this.generation) return;
      const section = this.data?.asset_class === 'etf' ? 'etf' : 'valuation';
      if (this.data?.available_sections?.includes(section)) await this.load(section);
    }
    renderOverview(parent, price) {
      this.overviewChart = 'price';
      super.renderOverview(parent, price);
      parent.querySelector('.dashboard-chart-choices').remove();
      const rangeLabel = document.createElement('span');
      rangeLabel.className = 'adaptive-range-label';
      rangeLabel.textContent = '行情区间';
      parent.querySelector('.dashboard-chart-toolbar').prepend(rangeLabel);
      parent.dataset.density = this.wide ? 'expanded' : 'compact';
      // Research statistics are part of the overview, not hidden behind another click.
      const more = parent.querySelector('.dashboard-more');
      const statistics = document.createElement('section');
      statistics.className = 'adaptive-statistics';
      statistics.setAttribute('aria-label', '历史统计');
      const heading = document.createElement('h3');
      heading.textContent = '历史统计';
      statistics.append(heading);
      more.replaceWith(statistics);
      this.renderBoardStatistics(statistics, price);
      const facts = document.createElement('div');
      facts.className = 'adaptive-board-facts';
      const specialty = parent.querySelector('.dashboard-specialty');
      statistics.before(facts);
      if (specialty) facts.append(specialty);
      facts.append(statistics);
      const main = parent.querySelector('.dashboard-overview-main');
      const priceStack = document.createElement('div');
      priceStack.className = 'adaptive-price-stack';
      const priceChart = main.querySelector('.dashboard-main-chart');
      priceChart.before(priceStack);
      priceStack.append(priceChart);
      const volume = document.createElement('div');
      volume.className = 'adaptive-volume-chart';
      priceStack.append(volume);
      const companions = document.createElement('div');
      companions.className = 'adaptive-companion-charts';
      companions.setAttribute('aria-label', '关联走势');
      main.append(companions);
      const series = this.chartWindow ? price.series.slice(-this.chartWindow) : price.series;
      const candidates = [
        ['volatility', '历史波动率', [['hv20', '20日'], ['hv60', '60日']], true],
        ['drawdown', '历史回撤', [['drawdown', '回撤']], true],
      ];
      void this.chart(volume, '成交量', series, [['volume', '成交量（原始单位）']], false);
      for (const [, title, fields, percentage] of candidates) {
        void this.chart(companions, title, series, fields, percentage);
      }
    }
    plot(parent, title, option) {
      if (parent.matches('.adaptive-companion-charts, .adaptive-volume-chart')) {
        option.layout = {...option.layout,
          margin: {l: 48, r: 8, t: option.layout.showlegend ? 26 : 8, b: 26},
          xaxis: {...option.layout.xaxis, nticks: 4},
          yaxis: {...option.layout.yaxis, nticks: 3},
        };
      }
      return super.plot(parent, title, option);
    }
    renderSpecialty(parent) {
      super.renderSpecialty(parent);
      const button = parent.querySelector('.dashboard-specialty button');
      if (button && ['available', 'partial'].includes(this.panels[this.data?.asset_class === 'etf' ? 'etf' : 'valuation']?.status)) {
        button.textContent = '查阅数据';
        button.onclick = () => this.selectView('details');
      }
    }
    selectView(key) {
      this.tab = key;
      this.render();
      this.container.querySelector(`#dashboard-tab-${key}`)?.focus();
      const viewport = this.container.closest('.canvas-viewport');
      if (viewport) viewport.scrollTop = 0;
    }
    render() {
      this.tab = this.tab === 'overview' ? 'overview' : 'details';
      super.render();
      const nav = this.container.querySelector('.dashboard-tabs');
      nav.replaceChildren();
      nav.setAttribute('aria-label', '数据查看方式');
      const views = [['overview', '看板'], ['details', '数据明细']];
      for (const [key, label] of views) {
        const button = document.createElement('button');
        button.type = 'button'; button.textContent = label;
        button.id = `dashboard-tab-${key}`;
        button.setAttribute('role', 'tab');
        button.setAttribute('aria-controls', 'dashboard-panel');
        button.setAttribute('aria-selected', String(this.tab === key));
        button.tabIndex = this.tab === key ? 0 : -1;
        button.onclick = () => this.selectView(key);
        button.onkeydown = event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          this.selectView(event.key === 'Home' ? 'overview' : event.key === 'End' ? 'details' : key === 'overview' ? 'details' : 'overview');
        };
        nav.append(button);
      }
      if (this.tab === 'details') this.renderDetailSections();
    }
    renderDetailSections() {
      const content = this.container.querySelector('#dashboard-panel');
      const originalRows = [...content.childNodes];
      content.replaceChildren();
      const section = (title, className = '') => {
        const node = document.createElement('section'); node.className = 'adaptive-detail-section ' + className;
        const heading = document.createElement('h3'); heading.textContent = title;
        node.append(heading); content.append(node); return node;
      };
      const key = this.data?.asset_class === 'etf' ? 'etf' : 'valuation', specialty = this.panels[key];
      if (specialty?.indicators) {
        const target = section('估值数据');
        this.table(target, Object.entries(specialty.indicators).filter(([, value]) => Number.isFinite(value.value)).map(([field, value]) => ({
          label: specialty.labels?.[field] || field,
          value: ['dividend_yield', 'turnover_ratio'].includes(field) ? api.percent(value.value) : api.number(value.value),
          date: value.as_of, percentile: value.percentile?.value,
        })), [['label', '指标'], ['value', '数值'], ['date', '数据日期'], ['percentile', '历史分位', api.percent]]);
        const charts = document.createElement('div'); charts.className = 'dashboard-summary-grid'; target.append(charts);
        if (specialty.series?.length) {
          void this.chart(charts, 'PE历史', specialty.series, [['pe', 'PE']]);
          void this.chart(charts, 'PB历史', specialty.series, [['pb', 'PB']]);
        }
      } else if (key === 'etf' && specialty?.series?.length) {
        const target = section('ETF净值与份额');
        const charts = document.createElement('div'); charts.className = 'dashboard-summary-grid'; target.append(charts);
        void this.chart(charts, '价格与单位净值', specialty.series, [['close', '收盘价'], ['nav', '单位净值']]);
        void this.chart(charts, 'ETF折溢价', specialty.series, [['premium', '折溢价']], true);
        void this.chart(charts, '基金份额', specialty.series, [['shares', '份额']]);
        this.table(target, specialty.series.slice(-20).reverse(), [['date', '日期'], ['nav', '单位净值', v => api.number(v, 4)], ['shares', '份额', v => api.number(v, 0)], ['premium', '折溢价', api.percent]]);
      }
      if (this.panels.prices?.series?.length) super.renderAnalysis(section('历史统计与口径'), this.panels.prices);
      section('行情原始数据').append(...originalRows);
    }
    renderBoardStatistics(parent, price) {
      const research = price.research || {}, summary = price.summary || {};
      const values = document.createElement('dl');
      values.className = 'adaptive-board-values';
      const rows = [
        ['区间最大回撤', api.percent(research.max_drawdown)],
        ['近一年最大回撤', api.percent(summary.drawdown_1y)],
        ['成交量/20日均量', Number.isFinite(research.volume_ratio) ? api.number(research.volume_ratio) + '倍' : '—'],
        ['上涨日占比', api.percent(research.up_day_share)],
        ['日收益5%分位', api.percent(research.return_p05)],
        ['日收益95%分位', api.percent(research.return_p95)],
      ];
      for (const [label, value] of rows) {
        const row = document.createElement('div');
        const term = document.createElement('dt'), number = document.createElement('dd');
        term.textContent = label; number.textContent = value;
        row.append(term, number); values.append(row);
      }
      const note = document.createElement('p');
      note.className = 'dashboard-note';
      note.textContent = '统计基于完整历史样本；近一年回撤单独使用近一年窗口。';
      parent.append(values, note);
    }
    dispose() {
      cancelAnimationFrame(this.resizeFrame);
      super.dispose();
    }
  }
  api.Dashboard = AdaptiveDashboard;

})();
