/* Static transport adapter for the unchanged OH ReportEditorController.
   Only same-site report assets are read. Export always uses the current in-memory draft. */
(() => {
  'use strict';
  const {ReportEditorController, cleanIncomingHtml, normalizeChartSpecs} = window.OptionHelperReportEditor;
  const Export = window.OptionHelperDemoReportExport;
  function extractChartSpecs(document) {
    const stored = document.querySelector('template[data-oh-chart-specs]');
    if (stored) return normalizeChartSpecs(JSON.parse(stored.content.textContent));
    for (const script of document.querySelectorAll('script:not([src])')) {
      const match = /\b(?:const|let|var)\s+chartSpecs\s*=\s*\[/.exec(script.textContent);
      if (!match) continue;
      const start = match.index + match[0].length - 1;
      let depth = 0, inString = false, escaped = false;
      for (let i = start; i < script.textContent.length; i++) {
        const char = script.textContent[i];
        if (inString) { if (escaped) escaped = false; else if (char === '\\') escaped = true; else if (char === '"') inString = false; }
        else if (char === '"') inString = true;
        else if (char === '[' || char === '{') depth++;
        else if (char === ']' || char === '}') {
          if (--depth === 0) {
            const specs = JSON.parse(script.textContent.slice(start, i + 1));
            return normalizeChartSpecs(Object.fromEntries(specs.map(spec => [spec.id, spec])));
          }
        }
      }
      throw new Error('报告图表数据不完整，无法安全打开编辑稿。');
    }
    return {};
  }
  class OfflineReportEditor extends ReportEditorController {
    constructor(options = {}) {
      super(options);
      this.reportRoot = new URL(options.reportRoot || 'result/reports/',this.root.baseURI);
      this.loadDocx = options.loadDocx || (async()=>{});
      this.loadFileSnapshot = options.loadFileSnapshot;
      this.onClose = options.onClose || (()=>{});
      this.downloadBlob = options.downloadBlob || ((blob,name)=>Export.download(blob,name,this.root));
      this.nodes.editorTitle.addEventListener('input',()=>{
        if (!this.document) return;
        this.syncTitle(this.nodes.editorTitle.value);
        this.renderOutline(); this._markDirty();
      });
      this.nodes.reportEditor.addEventListener('keydown',event=>{
        if (event.key === 'Escape') { event.preventDefault(); this.requestExit(); }
        if (event.key === 'Tab') {
          const focusable = [...this.nodes.reportEditor.querySelectorAll('button,input,select,summary,iframe')].filter(node=>!node.disabled && node.getClientRects().length);
          const first = focusable[0], last = focusable.at(-1);
          if (event.shiftKey && this.root.activeElement === first) {event.preventDefault();last?.focus();}
          if (!event.shiftKey && this.root.activeElement === last) {event.preventDefault();first?.focus();}
        }
      });
    }
    resolveReportPath(path) {
      if (typeof path !== 'string' || !path.trim()) throw new Error('请提供Demo内的HTML报告路径。');
      const url = new URL(path,new URL('../../',this.reportRoot));
      const suffix = decodeURIComponent(url.pathname).slice(decodeURIComponent(this.reportRoot.pathname).length);
      if (url.protocol !== this.reportRoot.protocol || url.origin !== this.reportRoot.origin ||
          !decodeURIComponent(url.pathname).startsWith(decodeURIComponent(this.reportRoot.pathname)) ||
          !/^[\w.-]+\.html$/i.test(suffix) || url.search || url.username || url.password) {
        throw new Error('只能编辑当前Demo的result/reports目录内HTML报告。');
      }
      url.hash = ''; return url;
    }
    async open({path,title} = {}) {
      let url;
      try { url = this.resolveReportPath(path); }
      catch (error) {this._setStatus(error.message,'error');return false;}
      if (this.active && this.document && this.isDirty()) {
        this._setStatus('请先保存HTML副本或退出当前编辑稿，再打开其他报告。','error'); return false;
      }
      const revision = ++this.loadRevision;
      this.saveRevision++; this.saving = false; this.active = true;
      this._disposeCharts(); this.document = null; this.chartSpecs = {}; this.history = []; this.historyIndex = -1;
      this.nodes.reportEditor.hidden = false; this.root.documentElement.dataset.reportEditorOpen = 'true';
      this.nodes.editorPaper.dataset.ready = 'false'; delete this.nodes.editorPaper.dataset.error;
      this.nodes.editorPaper.setAttribute('aria-busy','true'); this._setSubmitDisabled(true);
      this.nodes.editorLoading.textContent = '正在读取本地报告'; this._setStatus('正在读取本地报告');
      try {
        let html;
        if (url.protocol === 'file:') {
          if (!this.loadFileSnapshot) throw new Error('请通过静态站点打开Demo。');
          html = await this.loadFileSnapshot(decodeURIComponent(url.pathname.split('/').at(-1)));
        } else {
          const response = await this.fetchImpl(url.href,{credentials:'omit',redirect:'error'});
          if (!response.ok) throw new Error(`报告读取失败${response.status ? `：${response.status}` : ''}，请检查路径后重试。`);
          html = await response.text();
        }
        if (revision !== this.loadRevision || !this.active) return false;
        const parsed = new this.window.DOMParser().parseFromString(html,'text/html');
        if (!parsed.body.textContent.trim()) throw new Error('报告正文为空。');
        this.chartSpecs = extractChartSpecs(parsed);
        const prepared = await Export.prepareIncoming(parsed,url,this.reportRoot,this.fetchImpl);
        if (revision !== this.loadRevision || !this.active) return false;
        this.title = String(title || parsed.title || parsed.querySelector('h1')?.textContent || '研究报告').trim();
        this.nodes.editorTitle.value = this.title; this.sourceArtifactName = url.pathname.split('/').at(-1);
        this.sourceReportRunId = ''; this.sourceUrl = url;
        const clean = new this.window.DOMParser().parseFromString(cleanIncomingHtml(prepared,this.window),'text/html');
        const policy = clean.getElementById('optionhelper-report-editor-policy');
        policy.content = "default-src 'none'; script-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:; object-src 'none'; frame-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'";
        await this._loadDocument('<!doctype html>\n'+clean.documentElement.outerHTML,revision);
        if (revision !== this.loadRevision || !this.active) return false;
        this.syncTitle(this.title);
        const first = this._snapshot(); this.history = [first]; this.historyIndex = 0;
        this.baselineFingerprint = this._snapshotFingerprint(first);
        this.nodes.editorPaper.dataset.ready = 'true'; this.nodes.editorPaper.setAttribute('aria-busy','false');
        this._setSubmitDisabled(false);this._syncHistoryButtons();
        this._setStatus('已载入，可直接编辑。修改后请保存HTML副本。'); return true;
      } catch (error) {
        if (revision !== this.loadRevision || !this.active) return false;
        this.nodes.editorPaper.dataset.error = 'true';this.nodes.editorPaper.setAttribute('aria-busy','false');
        this.nodes.editorLoading.textContent = error.message;this._setStatus(error.message,'error');return false;
      }
    }
    _attachDocument(nextDocument) {
      super._attachDocument(nextDocument);
      const style = nextDocument.createElement('style');style.id='oh-demo-editor-canvas-style';
      style.textContent='body{margin:0!important}.report-toc,.skip-link{display:none!important}.report-shell{display:block!important;padding:0!important;max-width:none!important}.report-document{margin:0 auto!important;width:100%!important;box-shadow:none!important;min-width:0!important}';
      nextDocument.head.append(style);
      nextDocument.addEventListener('click',event=>{
        const link=event.target.closest?.('a');if(!link)return;
        event.preventDefault();if(link.hash)nextDocument.getElementById(link.hash.slice(1))?.scrollIntoView();
      });
      nextDocument.body.addEventListener('input',()=>{
        const heading=nextDocument.querySelector('h1');
        if(heading){this.title=heading.textContent;this.nodes.editorTitle.value=this.title;nextDocument.title=this.title;}
      });
      nextDocument.addEventListener('keydown',event=>{
        if(event.key==='Escape'){event.preventDefault();this.requestExit();}
        if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='s'){event.preventDefault();void this.submit('html');}
      });
      // Paste/drop never introduce external resources or executable HTML into the draft.
      nextDocument.addEventListener('paste',event=>{
        event.preventDefault();const value=event.clipboardData?.getData('text/plain')||'';
        nextDocument.execCommand?.('insertText',false,value);
      });
      nextDocument.addEventListener('drop',event=>event.preventDefault());
      nextDocument.addEventListener('dragover',event=>event.preventDefault());
    }
    syncTitle(title) {
      this.title = title || '未命名研究报告'; this.document.title = this.title;
      const heading = this.document.querySelector('h1'); if (heading) heading.textContent = this.title;
    }
    serializeHtml() {
      const html = super.serializeHtml();
      if (!html) return html;
      const parsed = new this.window.DOMParser().parseFromString(html,'text/html');
      parsed.getElementById('oh-demo-editor-canvas-style')?.remove();
      return '<!doctype html>\n'+parsed.documentElement.outerHTML;
    }
    async submit(format) {
      if (this.saving || !this.active || this.nodes.editorPaper.dataset.ready !== 'true' || !['html','pdf','docx'].includes(format)) return null;
      this.flushPendingHistory();
      const fingerprint=this._snapshotFingerprint(), revision=++this.saveRevision;
      this.saving=true;this._setSubmitDisabled(true);
      this._setStatus(format==='html'?'正在准备HTML副本':format==='pdf'?'正在准备打印内容':'正在生成可编辑DOCX');
      try {
        // Capture before awaiting: later edits cannot leak into a partially generated export.
        const snapshot=Export.capture(this);
        if(format==='pdf') await this.printDraft(snapshot);
        else {
          if(format==='docx') await this.loadDocx();
          const blob=format==='html'?await Export.toHtml(snapshot):await Export.toDocx(snapshot);
          this.downloadBlob(blob,Export.filename(snapshot.title,format));
        }
        if(revision!==this.saveRevision||!this.active)return {format};
        if(format==='html')this.baselineFingerprint=fingerprint;
        const label=format==='html'?'已发起HTML副本下载':format==='pdf'?'已打开打印对话框，请在浏览器中选择打印或存为PDF':'已发起DOCX下载。正文、表格、公式可编辑，图表为图片';
        this._setStatus(label+(format==='html'&&this.isDirty()?'，当前仍有新修改':''));
        return {format};
      } catch(error) {
        if(revision===this.saveRevision)this._setStatus(`${error.message || '导出失败'}，当前修改仍保留。`,'error');return null;
      } finally {
        if(revision===this.saveRevision){this.saving=false;this._setSubmitDisabled(false);}
      }
    }
    printDraft(snapshot) { return Export.print(snapshot,this.root); }
    close() { super.close();this.document=null;this.onClose(); }
  }
  window.OptionHelperDemoReportEditing={OfflineReportEditor,extractChartSpecs};
})();
