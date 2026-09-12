/* Export transport only: keep the current App editor and all of its controls. */
(()=>{
 const original=window.OptionHelperReportEditor.createReportEditor;
 window.OptionHelperReportEditor.createReportEditor=options=>{
  const editor=original(options);
  editor.submit=async function(format){
   if(this.saving||!this.active||this.nodes.editorPaper.dataset.ready!=='true')return null;
   this.saving=true;this._setSubmitDisabled(true);
   try{
    const exporter=window.OptionHelperDemoReportExport,snapshot=exporter.capture(this);
    if(format==='pdf'){await exporter.print(snapshot,this.root);this._setStatus('已打开打印，请选择存储为PDF。');}
    else {const blob=format==='docx'?await exporter.toDocx(snapshot):await exporter.toHtml(snapshot);exporter.download(blob,exporter.filename(snapshot.title,format),this.root);this._setStatus(format==='docx'?'已发起Word下载':'已发起HTML下载');}
    return {format};
   }catch(error){this._setStatus(error.message,'error');return null;}
   finally{this.saving=false;this._setSubmitDisabled(false);}
  };
  return editor;
 };
})();
