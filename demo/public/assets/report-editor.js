/* Offline entry point. Integration: link report-editor.css, load this script, call open({path,title}). */
(() => {
  'use strict';
  if (window.OptionHelperDemoReportEditor) return;
  const entryScript = document.currentScript?.src ? document.currentScript :
    [...document.querySelectorAll('script[src]')].find(script => /(?:^|\/)report-editor\.js(?:[?#]|$)/.test(script.getAttribute('src') || ''));
  const entryUrl = new URL(entryScript?.getAttribute('src') || 'assets/report-editor.js', document.baseURI);
  const assetRoot = new URL('./', entryUrl);
  const reportRoot = new URL('../result/reports/', assetRoot);
  let controller, ready, host, previousFocus;
  const inertElements = new Map();
  const scripts = new Map();
  function loadScript(relative) {
    const url = new URL(relative, assetRoot).href;
    if (!scripts.has(url)) scripts.set(url, new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url;
      script.onload = resolve;
      script.onerror = () => { scripts.delete(url); script.remove(); reject(new Error('编辑器文件未能加载，请检查Demo文件是否完整。')); };
      document.head.append(script);
    }));
    return scripts.get(url);
  }
  function hide() {
    for (const [element, wasInert] of inertElements) element.inert = wasInert;
    inertElements.clear();
    if (previousFocus?.isConnected) previousFocus.focus();
    window.dispatchEvent(new CustomEvent('optionhelper:report-editor-close'));
  }
  async function initialize() {
    if (!document.querySelector('link[data-oh-report-editor],link[href$="report-editor.css"]')) {
      const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = new URL('report-editor.css',assetRoot).href;
      style.dataset.ohReportEditor = ''; document.head.append(style);
    }
    await loadScript('report-editing/editor-markup.js');
    await loadScript('report-editing/report-editor-core.js');
    await loadScript('report-editing/chart-presentation.js');
    if (!window.echarts) await loadScript('echarts.min.js');
    await loadScript('report-editing/export.js');
    await loadScript('report-editing/offline-controller.js');
    host = document.createElement('div'); host.id = 'oh-demo-report-editor';
    host.innerHTML = window.OptionHelperDemoReportMarkup; document.body.append(host);
    controller = new window.OptionHelperDemoReportEditing.OfflineReportEditor({
      root: document, reportRoot, onClose: hide,
      loadDocx: () => loadScript('report-editing/docx.iife.js'),
      loadFileSnapshot: async name => {
        await loadScript(`report-editing/report-snapshots/${name.replace(/\.html$/i,'.js')}`);
        const html = window.OptionHelperDemoReportSnapshots?.[name];
        if (!html) throw new Error('此报告未打包为本地文件快照，请通过静态站点打开Demo。');
        return html;
      },
    });
  }
  window.OptionHelperDemoReportEditor = Object.freeze({
    async open({path,title} = {}) {
      try {
        if (!ready) ready = initialize().catch(error => { ready = null; throw error; });
        await ready;
        if (!controller.active) {
          previousFocus = document.activeElement;
          for (const element of document.body.children) {
            if (element === host || ['SCRIPT','STYLE','LINK'].includes(element.tagName)) continue;
            inertElements.set(element, element.inert); element.inert = true;
          }
        }
        const opened = await controller.open({path,title});
        if (controller.active) controller.nodes.editorExit.focus();
        else hide();
        return opened;
      } catch (error) {
        hide();
        window.dispatchEvent(new CustomEvent('optionhelper:report-editor-error',{detail:{message:error.message}}));
        window.alert(error.message);
        return false;
      }
    },
    close() { controller?.requestExit(); },
    get isOpen() { return Boolean(controller?.active); },
  });
})();
