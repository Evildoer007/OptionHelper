(() => {
  'use strict';

  const MATH_NS = 'http://www.w3.org/1998/Math/MathML';
  const EDITOR_STYLE_ID = 'optionhelper-report-editor-style';
  const EDITOR_POLICY_ID = 'optionhelper-report-editor-policy';
  const clone = value => JSON.parse(JSON.stringify(value ?? null));
  const text = value => String(value ?? '');
  const finiteNumber = value => {
    if (value === null || value === undefined || (typeof value === 'string' && !value.trim())) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : value;
  };
  const reportDoctype = document => document.doctype ? '<!doctype html>' : '';

  function cleanIncomingHtml(html, ownerWindow) {
    const parser = new ownerWindow.DOMParser();
    const parsed = parser.parseFromString(text(html), 'text/html');
    parsed.querySelectorAll('script,iframe,object,embed,form,input,button,textarea,select,base,meta[http-equiv]').forEach(node => node.remove());
    parsed.querySelectorAll('*').forEach(node => {
      [...node.attributes].forEach(attribute => {
        const name = attribute.name.toLowerCase();
        const value = attribute.value.trim().toLowerCase();
        if (name.startsWith('on') || ((name === 'href' || name === 'src') && value.startsWith('javascript:'))) {
          node.removeAttribute(attribute.name);
        }
      });
    });
    // Permit trusted parent listeners while blocking executable report content.
    // A sandbox without allow-scripts suppresses those listeners in WebKit too.
    const policy = parsed.createElement('meta');
    policy.id = EDITOR_POLICY_ID;
    policy.httpEquiv = 'Content-Security-Policy';
    policy.content = "script-src 'none'; object-src 'none'; frame-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'";
    parsed.head.prepend(policy);
    return `${reportDoctype(parsed)}${parsed.documentElement.outerHTML}`;
  }

  function normalizeChartSpecs(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw).flatMap(([id, value]) => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
      const spec = clone(value);
      spec.id = text(spec.id || id);
      spec.type = ['line', 'bar', 'heatmap'].includes(text(spec.type).toLowerCase()) ? text(spec.type).toLowerCase() : 'line';
      spec.title = text(spec.title || '图表');
      spec.x = Array.isArray(spec.x) ? spec.x : [];
      if (spec.type === 'heatmap') {
        spec.y = Array.isArray(spec.y) ? spec.y : [];
        spec.data = Array.isArray(spec.data) ? spec.data.filter(item => Array.isArray(item) && item.length === 3) : [];
      } else {
        spec.series = Array.isArray(spec.series) ? spec.series.map((series, index) => ({
          name: text(series?.name || `系列${index + 1}`),
          data: Array.isArray(series?.data) ? series.data : [],
        })) : [];
      }
      return [[spec.id, spec]];
    }));
  }

  function responseError(response, data, fallback) {
    const error = new Error(data?.message || data?.detail || fallback);
    error.status = response?.status;
    error.body = data;
    return error;
  }

  class ReportEditorController {
    constructor({root, fetchImpl, notifySaved, frameDocumentFactory, echarts, chartPresentation, downloadHandler} = {}) {
      this.root = root || document;
      this.window = this.root.defaultView || window;
      this.fetchImpl = fetchImpl || this.window.fetch.bind(this.window);
      this.notifySaved = typeof notifySaved === 'function' ? notifySaved : () => {};
      this.frameDocumentFactory = frameDocumentFactory || null;
      this.echarts = echarts || this.window.echarts || null;
      this.chartPresentation = chartPresentation || this.window.OptionHelperChartPresentation || null;
      this.downloadHandler = downloadHandler || (url => this._download(url));
      this.nodes = Object.fromEntries([
        'reportEditor', 'editorTitle', 'editorStatus', 'editorExit', 'editorPaper', 'editorLoading', 'editorFrame',
        'editorOutline', 'editorOutlineCount', 'editorOutlinePanel', 'editorInspectorPanel', 'editorSelectionType',
        'editorInspectorEmpty', 'editorTextInspector', 'editorTableInspector', 'editorImageInspector',
        'editorFormulaInspector', 'editorChartInspector', 'editorBlockFormat', 'editorFontSize', 'editorTextColor',
        'editorAlignment', 'inspectorBlockFormat', 'inspectorFontSize', 'inspectorTextColor', 'inspectorAlignment',
        'editorFindToggle', 'editorFindBar', 'editorFindText', 'editorReplaceText', 'editorFindNext', 'editorReplaceOne',
        'editorReplaceAll', 'editorFindClose', 'editorInsertFormula', 'editorImageAlt', 'editorReplaceImage',
        'editorImageFile', 'editorFormulaKind', 'editorFormulaPrimary', 'editorFormulaSecondary',
        'editorFormulaPrimaryLabel', 'editorFormulaSecondaryLabel', 'editorApplyFormula', 'editorChartTitle',
        'editorChartType', 'editorChartXAxis', 'editorChartYAxis', 'editorChartZAxis', 'editorChartZAxisLabel',
        'editorChartData', 'editorChartSeriesActions', 'editorAddChartRow', 'editorRemoveChartRow',
        'editorAddChartSeries', 'editorRemoveChartSeries', 'editorSaveHtml', 'editorExportPdf', 'editorExportWord',
        'editorOutlineToggle', 'editorInspectorToggle', 'editorExitDialog', 'editorDiscard', 'editorSaveAndExit',
      ].map(id => [id, this.root.getElementById(id)]));
      this.document = null;
      this.sourceReportRunId = '';
      this.sourceArtifactName = '';
      this.taskId = '';
      this.chartSpecs = {};
      this.title = '';
      this.selectedElement = null;
      this.selectedCell = null;
      this.savedRange = null;
      this.chartInstances = new Map();
      this.loadRevision = 0;
      this.imageReaders = new WeakMap();
      this.saveRevision = 0;
      this.pendingSave = null;
      this.history = [];
      this.historyIndex = -1;
      this.historyTimer = 0;
      this.autosaveTimer = 0;
      this.baselineFingerprint = '';
      this.findMatches = [];
      this.findIndex = -1;
      this.saving = false;
      this.active = false;
      this._bind();
    }

    _bind() {
      const n = this.nodes;
      n.editorExit?.addEventListener('click', () => this.requestExit());
      n.editorTitle?.addEventListener('input', () => {
        this.title = n.editorTitle.value;
        const heading = this.document?.querySelector('h1');
        if (heading) heading.textContent = this.title;
        if (this.document) this.document.title = this.title;
        this.renderOutline();
        this._scheduleHistory();
        this._markDirty();
      });
      this.root.querySelectorAll('[data-editor-command]').forEach(button => button.addEventListener('click', () => {
        const command = button.dataset.editorCommand;
        if (command === 'undo') this.undo();
        else if (command === 'redo') this.redo();
        else this.applyTextCommand(command);
      }));
      n.editorBlockFormat?.addEventListener('change', event => this.formatBlock(event.target.value));
      n.inspectorBlockFormat?.addEventListener('change', event => this.formatBlock(event.target.value));
      n.editorFontSize?.addEventListener('change', event => this.applyTextStyle('fontSize', `${event.target.value}px`));
      n.inspectorFontSize?.addEventListener('change', event => this.applyTextStyle('fontSize', `${event.target.value}px`));
      n.editorTextColor?.addEventListener('change', event => this.applyTextStyle('color', event.target.value));
      n.inspectorTextColor?.addEventListener('change', event => this.applyTextStyle('color', event.target.value));
      n.editorAlignment?.addEventListener('change', event => this.applyTextStyle('textAlign', event.target.value));
      n.inspectorAlignment?.addEventListener('change', event => this.applyTextStyle('textAlign', event.target.value));
      n.editorFindToggle?.addEventListener('click', () => this.toggleFind());
      n.editorFindClose?.addEventListener('click', () => this.toggleFind(false));
      n.editorFindNext?.addEventListener('click', () => this.findNext());
      n.editorReplaceOne?.addEventListener('click', () => this.replaceCurrent());
      n.editorReplaceAll?.addEventListener('click', () => this.replaceAll());
      n.editorFindText?.addEventListener('keydown', event => {
        if (event.key === 'Enter') { event.preventDefault(); this.findNext(); }
      });
      n.editorInsertFormula?.addEventListener('click', () => this.insertFormula());
      this.root.querySelectorAll('[data-table-command]').forEach(button => button.addEventListener('click', () => this.tableCommand(button.dataset.tableCommand)));
      n.editorImageAlt?.addEventListener('change', event => this.updateSelectedImage({alt: event.target.value}));
      n.editorReplaceImage?.addEventListener('click', () => n.editorImageFile?.click());
      n.editorImageFile?.addEventListener('change', event => this.replaceSelectedImage(event.target.files?.[0]));
      n.editorFormulaKind?.addEventListener('change', () => this._syncFormulaLabels());
      n.editorApplyFormula?.addEventListener('click', () => this.updateFormula({
        kind: n.editorFormulaKind.value,
        primary: n.editorFormulaPrimary.value,
        secondary: n.editorFormulaSecondary.value,
      }));
      for (const [id, key] of [['editorChartTitle', 'title'], ['editorChartXAxis', 'x_axis_name'], ['editorChartYAxis', 'y_axis_name'], ['editorChartZAxis', 'z_axis_name']]) {
        n[id]?.addEventListener('change', event => this.updateSelectedChart({[key]: event.target.value}));
      }
      n.editorChartType?.addEventListener('change', event => this.updateSelectedChart({type: event.target.value}));
      n.editorChartData?.addEventListener('change', event => this._handleChartDataChange(event.target));
      n.editorAddChartRow?.addEventListener('click', () => this.chartStructureCommand('add-row'));
      n.editorRemoveChartRow?.addEventListener('click', () => this.chartStructureCommand('remove-row'));
      n.editorAddChartSeries?.addEventListener('click', () => this.chartStructureCommand('add-series'));
      n.editorRemoveChartSeries?.addEventListener('click', () => this.chartStructureCommand('remove-series'));
      this.root.getElementById('editorCombine')?.addEventListener('click', () => this.openCombine());
      this.root.getElementById('editorCombineReport')?.addEventListener('change', () => this.loadCombineSections());
      this.root.getElementById('editorCombineApply')?.addEventListener('click', () => this.applyCombine());
      this.root.getElementById('editorCombineCancel')?.addEventListener('click', () => { this.root.getElementById('editorCombinePanel').hidden = true; });
      this.root.getElementById('editorExportHtml')?.addEventListener('click', async () => { const saved = await this.submit('html'); if (saved) this.downloadHandler(saved.download_url, saved); });
      this.root.getElementById('editorSaveCopy')?.addEventListener('click', () => this.saveCopy());
      this.root.getElementById('editorAddSection')?.addEventListener('click', () => this.sectionCommand('add'));
      this.root.querySelectorAll('[data-section-command]').forEach(button => button.addEventListener('click', () => this.sectionCommand(button.dataset.sectionCommand)));
      this.root.getElementById('editorSourceToggle')?.addEventListener('click', () => {
        this.root.getElementById('editorSourceText').value = this.serializeHtml();
        this.root.getElementById('editorSourcePanel').hidden = false;
      });
      this.root.getElementById('editorSourceCancel')?.addEventListener('click', () => { this.root.getElementById('editorSourcePanel').hidden = true; });
      this.root.getElementById('editorSourceApply')?.addEventListener('click', async () => {
        this.flushPendingHistory();
        await this._loadDocument(cleanIncomingHtml(this.root.getElementById('editorSourceText').value, this.window), ++this.loadRevision);
        this.root.getElementById('editorSourcePanel').hidden = true;
        this._checkpoint(); this.renderOutline();
      });
      n.editorSaveHtml?.addEventListener('click', () => this.submit('html'));
      n.editorExportPdf?.addEventListener('click', () => this.submit('pdf'));
      n.editorExportWord?.addEventListener('click', () => this.submit('docx'));
      n.editorOutlineToggle?.addEventListener('click', () => this._toggleMobilePanel('outline'));
      n.editorInspectorToggle?.addEventListener('click', () => this._toggleMobilePanel('inspector'));
      n.editorDiscard?.addEventListener('click', event => { event.preventDefault(); this.nodes.editorExitDialog?.close?.(); this.close(); });
      n.editorSaveAndExit?.addEventListener('click', async event => {
        event.preventDefault();
        const saved = await this.submit('html');
        if (saved && !this.isDirty()) { this.nodes.editorExitDialog?.close?.(); this.close(); }
      });
      this.window.addEventListener('beforeunload', event => {
        if (!this.active || !this.isDirty()) return;
        event.preventDefault();
        event.returnValue = '';
      });
      this.window.addEventListener('resize', () => this.chartInstances.forEach(instance => instance.resize?.()), {passive: true});
    }

    async open(reportRunId) {
      const sourceId = text(reportRunId).trim();
      if (!sourceId) return false;
      const revision = ++this.loadRevision;
      this.combineSource = null;
      for (const id of ['editorCombinePanel','editorSourcePanel']) { const panel = this.root.getElementById(id); if (panel) panel.hidden = true; }
      this.saveRevision += 1;
      this.saving = false;
      this._setSubmitDisabled(true);
      this.active = true;
      this.nodes.reportEditor.hidden = false;
      this.root.documentElement.dataset.reportEditorOpen = 'true';
      this.nodes.editorPaper.dataset.ready = 'false';
      delete this.nodes.editorPaper.dataset.error;
      this.nodes.editorLoading.textContent = '正在读取报告正文';
      this._setStatus('正在读取报告');
      try {
        const response = await this.fetchImpl(`/api/reports/${encodeURIComponent(sourceId)}/editor`);
        const data = await response.json();
        if (revision !== this.loadRevision || !this.active) return false;
        if (!response.ok || data?.ok !== true) throw responseError(response, data, '报告读取失败');
        this.sourceReportRunId = text(data.source_report_run_id || sourceId);
        this.sourceArtifactName = text(data.source_artifact_name || 'report.html');
        this.taskId = text(data.task_id);
        this.chartSpecs = normalizeChartSpecs(data.chart_specs);
        this.title = text(data.title || '研究报告');
        this.nodes.editorTitle.value = this.title;
        await this._loadDocument(cleanIncomingHtml(data.html, this.window), revision);
        const heading = this.document?.querySelector('h1');
        if (heading) heading.textContent = this.title;
        if (this.document) this.document.title = this.title;
        this.renderOutline();
        if (revision !== this.loadRevision || !this.active) return false;
        const first = this._snapshot();
        this.history = [first];
        this.historyIndex = 0;
        this.baselineFingerprint = this._snapshotFingerprint(first);
        this.nodes.editorPaper.dataset.ready = 'true';
        this._setSubmitDisabled(false);
        this.nodes.editorPaper.setAttribute('aria-busy', 'false');
        this._setStatus('已载入，可直接编辑正文');
        this._syncHistoryButtons();
        return true;
      } catch (error) {
        if (revision !== this.loadRevision || !this.active) return false;
        this.nodes.editorPaper.dataset.error = 'true';
        this.nodes.editorPaper.setAttribute('aria-busy', 'false');
        this.nodes.editorLoading.textContent = error.message || '报告读取失败';
        this._setStatus(error.message || '报告读取失败，请重试', 'error');
        return false;
      }
    }

    async _loadDocument(html, revision) {
      if (this.frameDocumentFactory) {
        const next = this.frameDocumentFactory(html);
        if (revision === this.loadRevision) this._attachDocument(next);
        return;
      }
      const frame = this.nodes.editorFrame;
      await new Promise(resolve => {
        const loaded = () => {
          frame.removeEventListener('load', loaded);
          if (revision === this.loadRevision && frame.contentDocument) this._attachDocument(frame.contentDocument);
          resolve();
        };
        frame.addEventListener('load', loaded);
        frame.srcdoc = html;
      });
    }

    _attachDocument(nextDocument) {
      this._disposeCharts();
      this.document = nextDocument;
      this.selectedElement = null;
      this.selectedCell = null;
      this.savedRange = null;
      const editorStyle = nextDocument.createElement('style');
      editorStyle.id = EDITOR_STYLE_ID;
      editorStyle.textContent = `
        html{scroll-behavior:smooth}body[contenteditable="true"]{min-height:100vh;caret-color:#b21b36;outline:none}
        [data-report-editor-selected="true"]{outline:2px solid #b21b36!important;outline-offset:3px}
        .chart[data-report-editor-chart-host="true"],img,math{cursor:pointer}
      `;
      nextDocument.head.append(editorStyle);
      nextDocument.querySelectorAll('script,iframe,object,embed,form').forEach(node => node.remove());
      nextDocument.body.contentEditable = 'true';
      nextDocument.body.spellcheck = true;
      for (const id of Object.keys(this.chartSpecs)) {
        const host = nextDocument.getElementById(id);
        if (!host) continue;
        host.dataset.reportEditorChartHost = 'true';
        host.contentEditable = 'false';
      }
      nextDocument.querySelectorAll('.chart-data,img,math,svg').forEach(node => { node.contentEditable = 'false'; });
      nextDocument.body.addEventListener('input', event => {
        if (event.target.closest?.('[data-report-editor-chart-host="true"]')) return;
        const heading = this.document.querySelector('h1');
        if (heading?.textContent.trim()) { this.title = heading.textContent.trim().slice(0,120); this.nodes.editorTitle.value = this.title; this.document.title = this.title; }
        this._scheduleHistory();
        this._markDirty();
        // contenteditable input targets the editing host, not necessarily the heading.
        this.renderOutline();
      });
      nextDocument.body.addEventListener('click', event => this.selectElement(event.target));
      nextDocument.addEventListener('selectionchange', () => this._rememberSelection());
      nextDocument.addEventListener('keydown', event => {
        const modifier = event.metaKey || event.ctrlKey;
        if (modifier && event.key.toLowerCase() === 'z') {
          event.preventDefault();
          event.shiftKey ? this.redo() : this.undo();
        } else if (modifier && event.key.toLowerCase() === 'f') {
          event.preventDefault();
          this.toggleFind(true);
        }
      });
      this.renderAllCharts();
      this.renderOutline();
      this.selectElement(nextDocument.body);
    }

    close() {
      this.window.clearTimeout(this.autosaveTimer);
      this.loadRevision += 1;
      this.saveRevision += 1;
      this.window.clearTimeout(this.historyTimer);
      this.historyTimer = 0;
      this.saving = false;
      this._setSubmitDisabled(false);
      this.active = false;
      this._disposeCharts();
      this.nodes.reportEditor.hidden = true;
      delete this.root.documentElement.dataset.reportEditorOpen;
      delete this.nodes.reportEditor.dataset.mobilePanel;
      this.nodes.editorOutlineToggle?.setAttribute('aria-expanded', 'false');
      this.nodes.editorInspectorToggle?.setAttribute('aria-expanded', 'false');
    }

    requestExit() {
      this.flushPendingHistory();
      if (!this.isDirty()) { this.close(); return; }
      const dialog = this.nodes.editorExitDialog;
      if (dialog?.showModal) dialog.showModal();
      else if (this.window.confirm('当前修改尚未保存，确定放弃吗？')) this.close();
    }

    _setStatus(value, tone = '') {
      this.nodes.editorStatus.textContent = value;
      this.nodes.editorStatus.dataset.tone = tone;
    }

    statusText() { return this.nodes.editorStatus?.textContent || ''; }

    _snapshot() {
      return {title: this.nodes.editorTitle?.value || this.title, html: this.serializeHtml(), chartSpecs: clone(this.chartSpecs)};
    }

    _snapshotFingerprint(snapshot = this._snapshot()) {
      return JSON.stringify({title: snapshot.title, html: snapshot.html, chart_specs: snapshot.chartSpecs});
    }

    isDirty() {
      if (!this.document) return false;
      return this._snapshotFingerprint() !== this.baselineFingerprint;
    }

    _markDirty() {
      if (!this.isDirty()) return;
      this._setStatus('未保存修改');
      this.window.clearTimeout(this.autosaveTimer);
      this.autosaveTimer = this.window.setTimeout(() => {
        if (this.active && this.isDirty() && !this.saving) void this.submit('html');
      }, 1800);
    }

    _scheduleHistory() {
      this.window.clearTimeout(this.historyTimer);
      this.historyTimer = this.window.setTimeout(() => this.flushPendingHistory(), 320);
    }

    flushPendingHistory() {
      this.window.clearTimeout(this.historyTimer);
      this.historyTimer = 0;
      if (!this.document) return;
      const snapshot = this._snapshot();
      const current = this.history[this.historyIndex];
      if (current && this._snapshotFingerprint(current) === this._snapshotFingerprint(snapshot)) return;
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push(snapshot);
      if (this.history.length > 80) this.history.shift();
      this.historyIndex = this.history.length - 1;
      this._syncHistoryButtons();
    }

    _checkpoint() {
      this.flushPendingHistory();
      this._markDirty();
    }

    _syncHistoryButtons() {
      const undo = this.root.querySelector('[data-editor-command="undo"]');
      const redo = this.root.querySelector('[data-editor-command="redo"]');
      if (undo) undo.disabled = this.historyIndex <= 0;
      if (redo) redo.disabled = this.historyIndex < 0 || this.historyIndex >= this.history.length - 1;
    }

    undo() {
      this.flushPendingHistory();
      if (this.historyIndex <= 0) return false;
      this.historyIndex -= 1;
      this._restoreSnapshot(this.history[this.historyIndex]);
      return true;
    }

    redo() {
      if (this.historyIndex >= this.history.length - 1) return false;
      this.historyIndex += 1;
      this._restoreSnapshot(this.history[this.historyIndex]);
      return true;
    }

    _restoreSnapshot(snapshot) {
      this.title = snapshot.title;
      this.nodes.editorTitle.value = snapshot.title;
      this.chartSpecs = clone(snapshot.chartSpecs);
      const revision = ++this.loadRevision;
      if (this.frameDocumentFactory) this._attachDocument(this.frameDocumentFactory(snapshot.html));
      else void this._loadDocument(snapshot.html, revision);
      this._syncHistoryButtons();
      this._setStatus(this._snapshotFingerprint(snapshot) === this.baselineFingerprint ? '已回到最近保存内容' : '有未保存修改');
    }

    _rememberSelection() {
      const selection = this.document?.defaultView?.getSelection?.();
      if (!selection?.rangeCount) return;
      const range = selection.getRangeAt(0);
      if (this.document.body.contains(range.commonAncestorContainer)) this.savedRange = range.cloneRange();
    }

    _restoreSelection() {
      if (!this.savedRange || !this.document?.body.contains(this.savedRange.commonAncestorContainer)) return false;
      const selection = this.document.defaultView.getSelection();
      selection.removeAllRanges();
      selection.addRange(this.savedRange);
      return true;
    }

    selectElement(rawElement) {
      if (!this.document || !rawElement) return;
      const element = rawElement.nodeType === 1 ? rawElement : rawElement.parentElement;
      const chart = element.closest?.('.chart[id]');
      const formula = element.closest?.('math');
      const image = element.closest?.('img');
      const table = element.closest?.('table');
      const textBlock = element.closest?.('h1,h2,h3,h4,p,li,blockquote,td,th,figcaption');
      let selected = this.document.body;
      let kind = 'body';
      if (chart && this.chartSpecs[chart.id]) { selected = chart; kind = 'chart'; }
      else if (formula) { selected = formula; kind = 'formula'; }
      else if (image) { selected = image; kind = 'image'; }
      else if (table) { selected = table; kind = 'table'; this.selectedCell = element.closest?.('td,th') || table.querySelector('td,th'); }
      else if (textBlock) { selected = textBlock; kind = 'text'; }
      this.document.querySelectorAll('[data-report-editor-selected]').forEach(node => node.removeAttribute('data-report-editor-selected'));
      this.selectedElement = selected;
      if (selected !== this.document.body) selected.setAttribute('data-report-editor-selected', 'true');
      this._renderInspector(kind);
    }

    _renderInspector(kind) {
      const n = this.nodes;
      const sections = {
        text: n.editorTextInspector, table: n.editorTableInspector, image: n.editorImageInspector,
        formula: n.editorFormulaInspector, chart: n.editorChartInspector,
      };
      Object.values(sections).forEach(section => { if (section) section.hidden = true; });
      n.editorInspectorEmpty.hidden = Boolean(sections[kind]);
      if (sections[kind]) sections[kind].hidden = false;
      n.editorSelectionType.textContent = ({text: '文字', table: '表格', image: '图片', formula: '公式', chart: '图表'})[kind] || '正文';
      if (kind === 'text') this._renderTextInspector();
      if (kind === 'image') n.editorImageAlt.value = this.selectedElement.getAttribute('alt') || '';
      if (kind === 'formula') this._renderFormulaInspector();
      if (kind === 'chart') this._renderChartInspector();
    }

    _setSelectValue(select, value) {
      select.value = value;
      // Refresh hosted choice labels without invoking change handlers that edit
      // the document or create history entries merely from selecting content.
      select.dispatchEvent(new select.ownerDocument.defaultView.Event('input'));
    }

    _renderTextInspector() {
      const element = this.selectedElement;
      const tag = element.tagName.toLowerCase();
      const block = ['h1', 'h2', 'h3', 'blockquote'].includes(tag) ? tag : 'p';
      this._setSelectValue(this.nodes.inspectorBlockFormat, block);
      this._setSelectValue(this.nodes.editorBlockFormat, block);
      const computed = element.ownerDocument.defaultView.getComputedStyle(element);
      const size = Math.max(10, Math.min(72, Math.round(parseFloat(computed.fontSize) || 14)));
      this.nodes.inspectorFontSize.value = String(size);
      this.nodes.editorFontSize.value = String(size);
      const alignment = ['left', 'center', 'right', 'justify'].includes(computed.textAlign) ? computed.textAlign : 'left';
      this._setSelectValue(this.nodes.inspectorAlignment, alignment);
      this._setSelectValue(this.nodes.editorAlignment, alignment);
    }

    applyTextCommand(command) {
      if (!this.document || !['bold', 'italic'].includes(command)) return;
      this.flushPendingHistory();
      this._restoreSelection();
      if (typeof this.document.execCommand === 'function') this.document.execCommand(command, false, null);
      else {
        const element = this.selectedElement?.closest?.('h1,h2,h3,h4,p,li,blockquote,td,th') || this.selectedElement;
        if (element?.style) {
          if (command === 'bold') element.style.fontWeight = element.style.fontWeight === '700' ? '' : '700';
          if (command === 'italic') element.style.fontStyle = element.style.fontStyle === 'italic' ? '' : 'italic';
        }
      }
      this._checkpoint();
    }

    formatBlock(tagName) {
      if (!this.document || !['p', 'h1', 'h2', 'h3', 'blockquote'].includes(tagName)) return;
      const current = this.selectedElement?.closest?.('h1,h2,h3,h4,p,li,blockquote,td,th');
      if (!current || ['td', 'th'].includes(current.tagName.toLowerCase())) return;
      this.flushPendingHistory();
      const replacement = this.document.createElement(tagName);
      [...current.attributes].forEach(attribute => {
        if (!attribute.name.startsWith('data-report-editor')) replacement.setAttribute(attribute.name, attribute.value);
      });
      while (current.firstChild) replacement.append(current.firstChild);
      current.replaceWith(replacement);
      this.selectedElement = replacement;
      replacement.setAttribute('data-report-editor-selected', 'true');
      this.renderOutline();
      this._checkpoint();
      this._renderTextInspector();
    }

    applyTextStyle(property, value) {
      if (!this.document || !value) return;
      this.flushPendingHistory();
      this._restoreSelection();
      const selection = this.document.defaultView.getSelection();
      if (selection?.rangeCount && !selection.isCollapsed) {
        const range = selection.getRangeAt(0);
        const span = this.document.createElement('span');
        span.style[property] = value;
        try { range.surroundContents(span); }
        catch {
          const fragment = range.extractContents();
          span.append(fragment);
          range.insertNode(span);
        }
        this.selectedElement = span.closest('h1,h2,h3,h4,p,li,blockquote,td,th') || span;
      } else {
        const target = this.selectedElement?.closest?.('h1,h2,h3,h4,p,li,blockquote,td,th') || this.selectedElement;
        if (target?.style) target.style[property] = value;
      }
      this._checkpoint();
    }

    tableCommand(command) {
      const table = this.selectedElement?.closest?.('table');
      if (!table) return false;
      this.flushPendingHistory();
      const rows = [...table.rows];
      const activeRow = this.selectedCell?.parentElement || rows.at(-1);
      const column = Math.max(0, this.selectedCell?.cellIndex ?? 0);
      if (command === 'add-row') {
        const section = activeRow?.parentElement?.tagName === 'THEAD' ? table.tBodies[0] || table.createTBody() : activeRow?.parentElement || table.tBodies[0] || table.createTBody();
        const sectionRows = [...section.rows];
        const reference = section === activeRow?.parentElement ? activeRow : sectionRows.at(-1);
        const row = section.insertRow(reference ? reference.sectionRowIndex + 1 : -1);
        const count = Math.max(1, activeRow?.cells.length || rows[0]?.cells.length || 1);
        for (let index = 0; index < count; index += 1) row.insertCell().textContent = '';
        this.selectedCell = row.cells[Math.min(column, row.cells.length - 1)];
      } else if (command === 'delete-row' && activeRow && rows.length > 1) {
        const next = activeRow.previousElementSibling || activeRow.nextElementSibling;
        activeRow.remove();
        this.selectedCell = next?.cells[Math.min(column, next.cells.length - 1)] || table.querySelector('td,th');
      } else if (command === 'add-column') {
        rows.forEach(row => {
          const reference = row.cells[Math.min(column, Math.max(0, row.cells.length - 1))];
          const cell = this.document.createElement(reference?.tagName === 'TH' ? 'th' : 'td');
          if (cell.tagName === 'TH') cell.scope = 'col';
          cell.textContent = '';
          if (reference) reference.after(cell);
          else row.append(cell);
        });
        table.querySelector('colgroup')?.append(this.document.createElement('col'));
        this.selectedCell = activeRow?.cells[column + 1] || table.querySelector('td,th');
      } else if (command === 'delete-column' && (rows[0]?.cells.length || 0) > 1) {
        rows.forEach(row => row.cells[column]?.remove());
        table.querySelector('colgroup')?.children[column]?.remove();
        this.selectedCell = activeRow?.cells[Math.min(column, activeRow.cells.length - 1)] || table.querySelector('td,th');
      } else return false;
      this._syncTableLabels(table);
      this._checkpoint();
      return true;
    }

    _syncTableLabels(table) {
      const headingRows = [...(table.tHead?.rows || [])];
      const headings = [...(headingRows.at(-1)?.cells || [])].map(cell => cell.textContent.trim());
      [...table.tBodies].flatMap(body => [...body.rows]).forEach(row => [...row.cells].forEach((cell, index) => {
        if (headings[index]) cell.dataset.label = headings[index];
        else cell.removeAttribute('data-label');
      }));
    }

    updateSelectedImage({alt} = {}) {
      const image = this.selectedElement?.closest?.('img');
      if (!image) return false;
      this.flushPendingHistory();
      if (alt !== undefined) image.alt = text(alt);
      this._checkpoint();
      return true;
    }

    replaceSelectedImage(file) {
      const image = this.selectedElement?.closest?.('img');
      if (!image || !file || !text(file.type).startsWith('image/')) {
        this._setStatus('请选择本地图片文件', 'error');
        return false;
      }
      const reader = new this.window.FileReader();
      const revision = this.loadRevision;
      this.imageReaders.set(image, reader);
      const isCurrent = () => this.active && revision === this.loadRevision &&
        image.ownerDocument === this.document && this.document.body.contains(image) && this.imageReaders.get(image) === reader;
      reader.addEventListener('error', () => {
        if (isCurrent()) this._setStatus('图片读取失败，请重新选择', 'error');
      });
      reader.addEventListener('load', () => {
        if (!isCurrent() || typeof reader.result !== 'string' || !reader.result.startsWith('data:image/')) return;
        this.flushPendingHistory();
        image.src = reader.result;
        this._checkpoint();
        this._setStatus('图片已替换，保存后写入报告');
      });
      reader.readAsDataURL(file);
      return true;
    }

    insertFormula() {
      if (!this.document) return;
      this.flushPendingHistory();
      const math = this.document.createElementNS(MATH_NS, 'math');
      math.classList.add('math-inline');
      math.setAttribute('aria-label', '数学表达式');
      this._writeFormula(math, {kind: 'fraction', primary: '收益', secondary: '本金'});
      const selection = this.document.defaultView.getSelection();
      if (selection?.rangeCount && this.document.body.contains(selection.getRangeAt(0).commonAncestorContainer)) {
        const range = selection.getRangeAt(0);
        range.deleteContents();
        range.insertNode(math);
      } else {
        const paragraph = this.document.createElement('p');
        paragraph.append(math);
        this.document.body.append(paragraph);
      }
      math.contentEditable = 'false';
      this.selectElement(math);
      this._checkpoint();
    }

    _formulaRow(value) {
      const row = this.document.createElementNS(MATH_NS, 'mrow');
      const token = this.document.createElementNS(MATH_NS, /[\u3400-\u9fff\s]/.test(text(value)) ? 'mtext' : 'mi');
      token.textContent = text(value) || '□';
      row.append(token);
      return row;
    }

    _writeFormula(math, {kind, primary, secondary}) {
      math.replaceChildren();
      const first = this._formulaRow(primary);
      const second = this._formulaRow(secondary);
      if (kind === 'fraction') {
        const fraction = this.document.createElementNS(MATH_NS, 'mfrac');
        fraction.append(first, second); math.append(fraction);
      } else if (kind === 'superscript' || kind === 'subscript') {
        const node = this.document.createElementNS(MATH_NS, kind === 'superscript' ? 'msup' : 'msub');
        node.append(first, second); math.append(node);
      } else if (kind === 'sqrt') {
        const sqrt = this.document.createElementNS(MATH_NS, 'msqrt');
        sqrt.append(first); math.append(sqrt);
      } else math.append(first);
      math.setAttribute('aria-label', [primary, secondary].filter(Boolean).join('，') || '数学表达式');
    }

    updateFormula(values) {
      const math = this.selectedElement?.closest?.('math');
      if (!math) return false;
      this.flushPendingHistory();
      this._writeFormula(math, values || {});
      this._checkpoint();
      this._renderFormulaInspector();
      return true;
    }

    _renderFormulaInspector() {
      const math = this.selectedElement?.closest?.('math');
      if (!math) return;
      const kind = math.querySelector('mfrac') ? 'fraction' : math.querySelector('msup') ? 'superscript' : math.querySelector('msub') ? 'subscript' : math.querySelector('msqrt') ? 'sqrt' : 'text';
      const rows = [...math.querySelectorAll(':scope > mfrac > mrow,:scope > msup > mrow,:scope > msub > mrow,:scope > msqrt > mrow,:scope > mrow')];
      this._setSelectValue(this.nodes.editorFormulaKind, kind);
      this.nodes.editorFormulaPrimary.value = rows[0]?.textContent || math.textContent || '';
      this.nodes.editorFormulaSecondary.value = rows[1]?.textContent || '';
      this._syncFormulaLabels();
    }

    _syncFormulaLabels() {
      const kind = this.nodes.editorFormulaKind.value;
      const labels = {
        fraction: ['分子', '分母'], superscript: ['主体', '上标'], subscript: ['主体', '下标'], sqrt: ['根式内容', ''], text: ['表达式', ''],
      }[kind];
      this.nodes.editorFormulaPrimaryLabel.firstChild.textContent = labels[0];
      this.nodes.editorFormulaSecondaryLabel.firstChild.textContent = labels[1] || '第二项';
      this.nodes.editorFormulaSecondaryLabel.hidden = !labels[1];
    }

    renderOutline() {
      if (!this.document) return;
      const headings = [...this.document.body.querySelectorAll('h1,h2,h3,h4')].filter(node => node.textContent.trim());
      this.nodes.editorOutline.replaceChildren(...headings.map(heading => {
        const button = this.root.createElement('button');
        button.type = 'button';
        button.textContent = heading.textContent.trim();
        button.dataset.level = heading.tagName.slice(1);
        button.addEventListener('click', () => { this.selectElement(heading); heading.scrollIntoView({block: 'start', behavior: 'smooth'}); });
        return button;
      }));
      this.nodes.editorOutlineCount.textContent = `${headings.length}节`;
    }

    toggleFind(force) {
      const open = force === undefined ? this.nodes.editorFindBar.hidden : Boolean(force);
      this.nodes.editorFindBar.hidden = !open;
      this.nodes.editorFindToggle.setAttribute('aria-expanded', String(open));
      if (open) { this.nodes.editorFindText.focus(); this.nodes.editorFindText.select(); }
      else { this.findMatches = []; this.findIndex = -1; }
    }

    _collectFindMatches(term) {
      const matches = [];
      if (!term || !this.document) return matches;
      const walker = this.document.createTreeWalker(this.document.body, this.document.defaultView.NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (node.parentElement?.closest('style,script,[data-report-editor-chart-host="true"]')) continue;
        let offset = 0;
        while ((offset = node.nodeValue.indexOf(term, offset)) >= 0) {
          matches.push({node, start: offset, end: offset + term.length});
          offset += Math.max(1, term.length);
        }
      }
      return matches;
    }

    findNext() {
      const term = this.nodes.editorFindText.value;
      this.findMatches = this._collectFindMatches(term);
      if (!this.findMatches.length) { this._setStatus('未找到匹配内容', 'error'); return false; }
      this.findIndex = (this.findIndex + 1) % this.findMatches.length;
      const match = this.findMatches[this.findIndex];
      const range = this.document.createRange();
      range.setStart(match.node, match.start); range.setEnd(match.node, match.end);
      const selection = this.document.defaultView.getSelection();
      selection.removeAllRanges(); selection.addRange(range);
      match.node.parentElement?.scrollIntoView?.({block: 'center'});
      this._setStatus(`找到第${this.findIndex + 1}处，共${this.findMatches.length}处`);
      return true;
    }

    replaceCurrent() {
      if (this.findIndex < 0 || !this.findMatches[this.findIndex]) return this.findNext();
      const match = this.findMatches[this.findIndex];
      this.flushPendingHistory();
      match.node.nodeValue = `${match.node.nodeValue.slice(0, match.start)}${this.nodes.editorReplaceText.value}${match.node.nodeValue.slice(match.end)}`;
      this.findIndex = -1;
      this._checkpoint();
      this.findNext();
      return true;
    }

    replaceAll() {
      const term = this.nodes.editorFindText.value;
      const replacement = this.nodes.editorReplaceText.value;
      const matches = this._collectFindMatches(term);
      if (!matches.length) { this._setStatus('未找到匹配内容', 'error'); return 0; }
      this.flushPendingHistory();
      const nodes = [...new Set(matches.map(match => match.node))];
      nodes.forEach(node => { node.nodeValue = node.nodeValue.split(term).join(replacement); });
      this.findMatches = []; this.findIndex = -1;
      this.renderOutline();
      this._checkpoint();
      this._setStatus(`已替换${matches.length}处，保存后写入报告`);
      return matches.length;
    }

    _selectedChart() {
      const host = this.selectedElement?.closest?.('.chart[id]');
      return host ? this.chartSpecs[host.id] : null;
    }

    _convertChartType(spec, nextType) {
      const current = spec.type;
      if (current === nextType) return;
      if (nextType === 'heatmap') {
        const series = Array.isArray(spec.series) && spec.series.length ? spec.series : [{name: '系列1', data: spec.x.map(() => 0)}];
        spec.y = series.map(item => item.name);
        spec.data = series.flatMap((item, yIndex) => spec.x.map((_, xIndex) => [xIndex, yIndex, item.data[xIndex] ?? null]));
        delete spec.series;
        spec.z_axis_name ||= spec.y_axis_name || '数值';
      } else if (current === 'heatmap') {
        const cells = new Map((spec.data || []).map(item => [`${item[0]}:${item[1]}`, item[2]]));
        spec.series = (spec.y?.length ? spec.y : ['系列1']).map((name, yIndex) => ({
          name: text(name), data: spec.x.map((_, xIndex) => cells.get(`${xIndex}:${yIndex}`) ?? null),
        }));
        delete spec.y; delete spec.data;
      }
      spec.type = nextType;
    }

    updateSelectedChart(patch) {
      const spec = this._selectedChart();
      if (!spec) return false;
      this.flushPendingHistory();
      if (patch.type) this._convertChartType(spec, patch.type);
      for (const key of ['title', 'x_axis_name', 'y_axis_name', 'z_axis_name']) if (patch[key] !== undefined) spec[key] = text(patch[key]);
      this._syncChartFigure(spec);
      this._renderChart(spec);
      this._checkpoint();
      this._renderChartInspector();
      return true;
    }

    updateChartCell({row, column, value}) {
      const spec = this._selectedChart();
      if (!spec || !Number.isInteger(row) || !Number.isInteger(column)) return false;
      this.flushPendingHistory();
      if (spec.type === 'heatmap') {
        const cell = (spec.data || []).find(item => Number(item[0]) === column && Number(item[1]) === row);
        if (cell) cell[2] = finiteNumber(value);
        else spec.data.push([column, row, finiteNumber(value)]);
      } else if (column === 0) spec.x[row] = finiteNumber(value);
      else if (spec.series?.[column - 1]) spec.series[column - 1].data[row] = finiteNumber(value);
      this._syncChartFigure(spec);
      this._renderChart(spec);
      this._checkpoint();
      this._renderChartInspector();
      return true;
    }

    chartStructureCommand(command) {
      const spec = this._selectedChart();
      if (!spec) return false;
      this.flushPendingHistory();
      if (command === 'add-row') {
        spec.x.push(`数据${spec.x.length + 1}`);
        if (spec.type === 'heatmap') (spec.y || []).forEach((_, yIndex) => spec.data.push([spec.x.length - 1, yIndex, null]));
        else (spec.series || []).forEach(series => series.data.push(null));
      } else if (command === 'remove-row' && spec.x.length > 1) {
        const removed = spec.x.length - 1; spec.x.pop();
        if (spec.type === 'heatmap') spec.data = spec.data.filter(item => Number(item[0]) !== removed);
        else (spec.series || []).forEach(series => series.data.pop());
      } else if (command === 'add-series') {
        if (spec.type === 'heatmap') {
          spec.y.push(`系列${spec.y.length + 1}`);
          spec.x.forEach((_, xIndex) => spec.data.push([xIndex, spec.y.length - 1, null]));
        } else spec.series.push({name: `系列${spec.series.length + 1}`, data: spec.x.map(() => null)});
      } else if (command === 'remove-series') {
        if (spec.type === 'heatmap' && spec.y.length > 1) {
          const removed = spec.y.length - 1; spec.y.pop(); spec.data = spec.data.filter(item => Number(item[1]) !== removed);
        } else if (spec.type !== 'heatmap' && spec.series.length > 1) spec.series.pop();
        else return false;
      } else return false;
      this._syncChartFigure(spec); this._renderChart(spec); this._checkpoint(); this._renderChartInspector();
      return true;
    }

    _handleChartDataChange(input) {
      const spec = this._selectedChart();
      if (!spec || input.tagName !== 'INPUT') return;
      if (input.dataset.chartSeriesName !== undefined && spec.series?.[Number(input.dataset.chartSeriesName)]) {
        spec.series[Number(input.dataset.chartSeriesName)].name = input.value;
      } else if (input.dataset.chartX !== undefined) {
        spec.x[Number(input.dataset.chartX)] = finiteNumber(input.value);
      } else if (input.dataset.chartY !== undefined && spec.y?.[Number(input.dataset.chartY)] !== undefined) {
        spec.y[Number(input.dataset.chartY)] = input.value;
      } else if (input.dataset.chartCell) {
        const [row, column] = input.dataset.chartCell.split(':').map(Number);
        const value = column === 0 ? finiteNumber(input.value) : this._chartStoredValue(input.value, spec);
        this.updateChartCell({row, column, value}); return;
      } else if (input.dataset.chartHeatCell) {
        const [xIndex, yIndex] = input.dataset.chartHeatCell.split(':').map(Number);
        this.updateChartCell({row: yIndex, column: xIndex, value: this._chartStoredValue(input.value, spec)}); return;
      } else return;
      this._syncChartFigure(spec); this._renderChart(spec); this._checkpoint(); this._renderChartInspector();
    }

    _renderChartInspector() {
      const spec = this._selectedChart();
      if (!spec) return;
      const n = this.nodes;
      n.editorChartTitle.value = spec.title || '';
      this._setSelectValue(n.editorChartType, spec.type);
      n.editorChartXAxis.value = spec.x_axis_name || '';
      n.editorChartYAxis.value = spec.y_axis_name || '';
      n.editorChartZAxis.value = spec.z_axis_name || '';
      n.editorChartZAxisLabel.hidden = spec.type !== 'heatmap';
      n.editorChartSeriesActions.hidden = false;
      const table = this.root.createElement('table');
      const head = table.createTHead().insertRow();
      const body = table.createTBody();
      const makeInput = (value, dataset, label) => {
        const input = this.root.createElement('input');
        input.value = value ?? '';
        input.setAttribute('aria-label', label);
        Object.assign(input.dataset, dataset);
        return input;
      };
      const unit = this._chartUnit(spec);
      const axisHead = this.root.createElement('th');
      axisHead.textContent = `${spec.y_axis_name || spec.x_axis_name || '数据'}${unit ? `（数值单位：${unit}）` : ''}`;
      head.append(axisHead);
      if (spec.type === 'heatmap') {
        spec.x.forEach((value, xIndex) => {
          const cell = this.root.createElement('th');
          cell.append(makeInput(value, {chartX: String(xIndex)}, `横轴第${xIndex + 1}项`)); head.append(cell);
        });
        const cells = new Map((spec.data || []).map(item => [`${item[0]}:${item[1]}`, item[2]]));
        spec.y.forEach((value, yIndex) => {
          const row = body.insertRow();
          row.insertCell().append(makeInput(value, {chartY: String(yIndex)}, `纵轴第${yIndex + 1}项`));
          spec.x.forEach((_, xIndex) => row.insertCell().append(makeInput(this._chartInputValue(cells.get(`${xIndex}:${yIndex}`), spec), {chartHeatCell: `${xIndex}:${yIndex}`}, `第${yIndex + 1}行第${xIndex + 1}列数值`)));
        });
      } else {
        spec.series.forEach((series, seriesIndex) => {
          const cell = this.root.createElement('th');
          cell.append(makeInput(series.name, {chartSeriesName: String(seriesIndex)}, `系列${seriesIndex + 1}名称${unit ? `，数值单位${unit}` : ''}`)); head.append(cell);
        });
        spec.x.forEach((value, rowIndex) => {
          const row = body.insertRow();
          row.insertCell().append(makeInput(value, {chartCell: `${rowIndex}:0`}, `第${rowIndex + 1}行横轴值`));
          spec.series.forEach((series, seriesIndex) => row.insertCell().append(makeInput(this._chartInputValue(series.data[rowIndex], spec), {chartCell: `${rowIndex}:${seriesIndex + 1}`}, `第${rowIndex + 1}行${series.name}数值`)));
        });
      }
      n.editorChartData.replaceChildren(table);
    }

    _chartTable(spec) {
      const wrapper = this.document.createElement('div');
      wrapper.className = 'table-wrap';
      const table = this.document.createElement('table');
      table.className = 'chart-data-table table-density-normal';
      table.dataset.tableDensity = 'normal';
      const unit = this._chartUnit(spec);
      const caption = table.createCaption();
      caption.textContent = `${spec.title || '图表'}数据${unit ? `（数值单位：${unit}）` : ''}`;
      const head = table.createTHead().insertRow();
      const body = table.createTBody();
      const appendHeader = label => { const cell = this.document.createElement('th'); cell.scope = 'col'; cell.textContent = label; head.append(cell); };
      appendHeader(spec.type === 'heatmap' ? `${spec.y_axis_name || '纵轴'}/${spec.x_axis_name || '横轴'}` : spec.x_axis_name || '横轴');
      if (spec.type === 'heatmap') {
        spec.x.forEach(value => appendHeader(value));
        const cells = new Map((spec.data || []).map(item => [`${item[0]}:${item[1]}`, item[2]]));
        spec.y.forEach((value, yIndex) => {
          const row = body.insertRow(); row.insertCell().textContent = value;
          spec.x.forEach((_, xIndex) => { row.insertCell().textContent = this._chartDisplayValue(cells.get(`${xIndex}:${yIndex}`), spec); });
        });
      } else {
        spec.series.forEach(series => appendHeader(`${series.name}${unit ? `（${unit}）` : ''}`));
        spec.x.forEach((value, rowIndex) => {
          const row = body.insertRow(); row.insertCell().textContent = value;
          spec.series.forEach(series => { row.insertCell().textContent = this._chartDisplayValue(series.data[rowIndex], spec); });
        });
      }
      wrapper.append(table);
      return wrapper;
    }

    _chartDisplayValue(value, spec) {
      return this.chartPresentation?.formatValue ? this.chartPresentation.formatValue(value, spec) : text(value);
    }

    _chartInputValue(value, spec) {
      return this.chartPresentation?.inputValue ? this.chartPresentation.inputValue(value, spec) : text(value);
    }

    _chartStoredValue(value, spec) {
      return this.chartPresentation?.parseInputValue ? this.chartPresentation.parseInputValue(value, spec) : finiteNumber(value);
    }

    _chartUnit(spec) {
      return this.chartPresentation?.unitLabel ? this.chartPresentation.unitLabel(spec) : '';
    }

    _syncChartFigure(spec) {
      const host = this.document?.getElementById(spec.id);
      if (!host) return;
      host.setAttribute('aria-label', spec.title || '图表');
      const figure = host.closest('figure');
      if (!figure) return;
      const caption = figure.querySelector('figcaption');
      if (caption) caption.textContent = spec.title || '图表';
      const summary = figure.querySelector('.chart-summary');
      if (summary) summary.textContent = spec.accessibility_summary || `${spec.title || '图表'}，横轴为${spec.x_axis_name || '横轴'}，纵轴为${spec.y_axis_name || '纵轴'}。`;
      let data = figure.querySelector('.chart-data');
      if (!data) { data = this.document.createElement('div'); data.className = 'chart-data'; host.after(data); }
      data.contentEditable = 'false';
      data.replaceChildren(this._chartTable(spec));
      let note = figure.querySelector('.source-note');
      if (spec.source_note) {
        if (!note) { note = this.document.createElement('p'); note.className = 'source-note'; figure.append(note); }
        note.textContent = spec.source_note;
      }
    }

    renderAllCharts() {
      Object.values(this.chartSpecs).forEach(spec => {
        this._syncChartFigure(spec);
        this._renderChart(spec);
      });
    }

    _renderChart(spec) {
      const host = this.document?.getElementById(spec.id);
      if (!host) return;
      this.chartInstances.get(spec.id)?.dispose?.();
      this.chartInstances.delete(spec.id);
      host.replaceChildren();
      if (!this.echarts?.init || !this.chartPresentation?.renderChart) {
        host.textContent = '图表预览不可用，请使用下方数据表。';
        return;
      }
      try {
        const instance = this.chartPresentation.renderChart({echarts: this.echarts, host, spec});
        this.chartInstances.set(spec.id, instance);
      } catch (error) {
        host.textContent = '图表预览不可用，请使用下方数据表。';
      }
    }

    _disposeCharts() {
      this.chartInstances.forEach(instance => instance.dispose?.());
      this.chartInstances.clear();
    }

    serializeHtml() {
      if (!this.document?.documentElement) return '';
      const html = this.document.documentElement.cloneNode(true);
      html.querySelector(`#${EDITOR_STYLE_ID}`)?.remove();
      html.querySelector(`#${EDITOR_POLICY_ID}`)?.remove();
      html.querySelectorAll('[data-report-editor-chart-host]').forEach(host => {
        host.replaceChildren();
        host.classList.remove('chart--unavailable');
      });
      html.querySelectorAll('[data-report-editor-selected],[data-runtime-echarts]').forEach(node => {
        node.removeAttribute('data-report-editor-selected');
        node.removeAttribute('data-runtime-echarts');
      });
      html.querySelectorAll('[contenteditable],[spellcheck]').forEach(node => {
        node.removeAttribute('contenteditable');
        node.removeAttribute('spellcheck');
      });
      html.querySelectorAll('script,iframe,object,embed,form').forEach(node => node.remove());
      return `${reportDoctype(this.document)}${html.outerHTML}`;
    }

    buildPayload(format) {
      this.flushPendingHistory();
      return {
        html: this.serializeHtml(),
        title: (this.nodes.editorTitle.value || '').trim() || '未命名研究报告',
        chart_specs: clone(this.chartSpecs),
        format,
      };
    }

    async submit(format) {
      if (this.saving || !this.active || this.nodes.editorPaper.dataset.ready !== 'true' || !['html', 'pdf', 'docx'].includes(format) || !this.sourceReportRunId) return null;
      const payload = this.buildPayload(format);
      const contentFingerprint = JSON.stringify({title: payload.title, html: payload.html, chart_specs: payload.chart_specs});
      const requestKey = JSON.stringify([this.sourceReportRunId, payload]);
      if (this.pendingSave?.key !== requestKey) {
        const bytes = this.window.crypto.getRandomValues(new Uint8Array(16));
        this.pendingSave = {key: requestKey, id: Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')};
      }
      const saveAttempt = this.pendingSave;
      payload.edit_request_id = saveAttempt.id;
      const revision = ++this.saveRevision;
      this.saving = true;
      this._setSubmitDisabled(true);
      this._setStatus(format === 'html' ? '正在保存' : format === 'pdf' ? '正在导出PDF' : '正在导出Word');
      try {
        const response = await this.fetchImpl(`/api/reports/${encodeURIComponent(this.sourceReportRunId)}/edits`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || data?.ok !== true) throw responseError(response, data, '报告保存失败');
        if (this.pendingSave === saveAttempt) this.pendingSave = null;
        this.notifySaved(data);
        if (revision !== this.saveRevision || !this.active) return data;
        const stillCurrent = this._snapshotFingerprint() === contentFingerprint;
        if (stillCurrent) this.baselineFingerprint = contentFingerprint;
        const label = format === 'html' ? '已保存' : format === 'pdf' ? 'PDF已生成' : 'Word已生成';
        this._setStatus(stillCurrent ? label : `${label}，当前仍有新修改`, stillCurrent ? 'saved' : '');
        if (format !== 'html' && data.download_url) this.downloadHandler(data.download_url, data);
        return data;
      } catch (error) {
        if (revision === this.saveRevision) this._setStatus(`${error.message || '报告保存失败'}，当前修改仍保留`, 'error');
        return null;
      } finally {
        if (revision === this.saveRevision) {
          this.saving = false;
          this._setSubmitDisabled(false);
          if (this.isDirty() && this.nodes.editorStatus.dataset.tone !== 'error') this._markDirty();
        }
      }
    }

    async openCombine() {
      try {
        const response = await this.fetchImpl(`/api/tasks/${encodeURIComponent(this.taskId)}/reports`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || '无法读取报告');
        const reports = (data.reports || []).filter(item => item.report_run_id !== this.sourceReportRunId);
        if (!reports.length) { this._setStatus('当前任务没有其他报告'); return; }
        const select = this.root.getElementById('editorCombineReport');
        select.replaceChildren(...reports.map(item => { const option = this.root.createElement('option'); option.value = item.report_run_id; option.textContent = item.title || '研究报告'; return option; }));
        this.root.getElementById('editorCombinePanel').hidden = false;
        await this.loadCombineSections();
      } catch (error) { this._setStatus(error.message, 'error'); }
    }

    async loadCombineSections() {
      const reportId = this.root.getElementById('editorCombineReport').value;
      const revision = this.loadRevision;
      this.combineSource = null;
      this.root.getElementById('editorCombineSections').replaceChildren();
      try {
        const response = await this.fetchImpl(`/api/reports/${encodeURIComponent(reportId)}/editor`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || '报告读取失败');
        if (!this.active || revision !== this.loadRevision || reportId !== this.root.getElementById('editorCombineReport').value) return;
        const doc = new this.window.DOMParser().parseFromString(cleanIncomingHtml(data.html, this.window), 'text/html');
        const main = doc.querySelector('.report-document,main') || doc.body;
        let sections = [...main.children].filter(node => node.matches('section,article'));
        if (!sections.length) sections = [...main.children].filter(node => !node.matches('header,h1,script,style'));
        this.combineSource = {reportId, sections, charts:normalizeChartSpecs(data.chart_specs)};
        this.root.getElementById('editorCombineSections').replaceChildren(...sections.map((node,index) => {
          const label = this.root.createElement('label'); const checkbox = this.root.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = true; checkbox.value = index;
          label.append(checkbox, this.root.createTextNode(node.querySelector('h2,h3')?.textContent || node.textContent.trim().slice(0,40))); return label;
        }));
      } catch (error) { this._setStatus(error.message, 'error'); }
    }

    applyCombine() {
      if (!this.combineSource) return;
      this.flushPendingHistory();
      const {reportId,sections,charts} = this.combineSource;
      const target = this.document.querySelector('.report-document,main') || this.document.body;
      const prefix = `joined-${Date.now()}-`;
      this.root.querySelectorAll('#editorCombineSections input:checked').forEach(input => {
        const node = this.document.importNode(sections[Number(input.value)], true);
        node.dataset.reportSource = reportId;
        [node,...node.querySelectorAll('[id]')].forEach(element => {
          if (!element.id) return;
          const old = element.id; element.id = prefix + old;
          if (charts[old]) this.chartSpecs[element.id] = {...clone(charts[old]), id:element.id};
        });
        node.querySelectorAll('a[href^="#"]').forEach(link => link.setAttribute('href', '#' + prefix + link.getAttribute('href').slice(1)));
        target.append(node);
      });
      this.root.getElementById('editorCombinePanel').hidden = true;
      this.renderAllCharts(); this.renderOutline(); this._checkpoint();
    }

    async saveCopy() {
      if (!this.active || this.saving) return;
      try {
        const payload = this.buildPayload('html');
        delete payload.format;
        payload.title = `${payload.title.slice(0,117)}副本`;
        const response = await this.fetchImpl('/api/report-documents', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload, task_id:this.taskId})});
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || '另存失败');
        this.notifySaved(data); await this.open(data.report_run_id);
      } catch (error) { this._setStatus(error.message, 'error'); }
    }

    sectionCommand(command) {
      if (!this.document) return;
      this.flushPendingHistory();
      const section = this.selectedElement?.closest('section');
      if (command === 'add') {
        const next = this.document.createElement('section');
        next.innerHTML = '<h2>新章节</h2><p>在此输入正文。</p>';
        (this.document.querySelector('.report-document, .card-document, main') || this.document.body).append(next);
        this.selectElement(next.querySelector('h2')); next.scrollIntoView({block:'center'});
      } else if (!section) { this._setStatus('请先点击要调整的章节'); return; }
      else if (command === 'up' && section.previousElementSibling?.tagName === 'SECTION') section.previousElementSibling.before(section);
      else if (command === 'down' && section.nextElementSibling?.tagName === 'SECTION') section.nextElementSibling.after(section);
      else if (command === 'copy') {
        const copy = section.cloneNode(true);
        copy.querySelectorAll('[id]').forEach(node => { const oldId = node.id; node.id = `${oldId}-copy-${Date.now()}`; if (this.chartSpecs[oldId]) this.chartSpecs[node.id] = {...clone(this.chartSpecs[oldId]), id:node.id}; });
        copy.removeAttribute('id'); copy.querySelectorAll('[data-report-editor-selected]').forEach(node => node.removeAttribute('data-report-editor-selected')); section.after(copy); this.renderAllCharts();
      } else if (command === 'delete') section.remove();
      this.renderOutline(); this._checkpoint();
    }

    _setSubmitDisabled(disabled) {
      for (const node of [this.nodes.editorSaveHtml, this.nodes.editorExportPdf, this.nodes.editorExportWord]) if (node) node.disabled = disabled;
    }

    _download(url) {
      const anchor = this.root.createElement('a');
      anchor.href = url; anchor.download = ''; anchor.hidden = true;
      this.root.body.append(anchor); anchor.click(); anchor.remove();
    }

    _toggleMobilePanel(name) {
      const next = this.nodes.reportEditor.dataset.mobilePanel === name ? '' : name;
      if (next) this.nodes.reportEditor.dataset.mobilePanel = next;
      else delete this.nodes.reportEditor.dataset.mobilePanel;
      this.nodes.editorOutlineToggle.setAttribute('aria-expanded', String(next === 'outline'));
      this.nodes.editorInspectorToggle.setAttribute('aria-expanded', String(next === 'inspector'));
    }
  }

  function createReportEditor(options = {}) {
    return new ReportEditorController(options);
  }

  window.OptionHelperReportEditor = {createReportEditor, ReportEditorController, cleanIncomingHtml, normalizeChartSpecs};
})();
