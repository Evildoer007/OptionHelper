/* Browser-only export of the current OH editor draft. No service, upload or model call.
   DOCX uses native Word paragraphs/tables/OMML and ECharts-rendered figure images. */
(() => {
  'use strict';
  const BODY_FONT = {ascii:'Arial',hAnsi:'Arial',eastAsia:'Songti SC',cs:'Arial'};
  const BLOCKS = new Set('address article aside blockquote dd div dl dt figcaption figure footer h1 h2 h3 h4 h5 h6 header li main ol p pre section table ul'.split(' '));
  const SKIP = 'script,style,noscript,template,nav,.report-toc,.skip-link,.chart-summary,[hidden],[aria-hidden="true"]';
  const safeText = value => String(value || '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g,'');
  const normalText = value => safeText(value).replace(/\s+/g,' ');
  function localAsset(value,base,root) {
    const url=new URL(value,base);
    if(url.protocol!==root.protocol||url.origin!==root.origin||!decodeURIComponent(url.pathname).startsWith(decodeURIComponent(root.pathname))||url.username||url.password) {
      throw new Error('报告包含站外资源，离线编辑器不会读取；请改为内嵌图片或本地资源。');
    }
    return url;
  }
  function cleanCss(value) {
    return value.replace(/@import\s+[^;]+;/gi,'').replace(/url\(\s*(['"]?)(?!data:)[^)]*\)/gi,'none');
  }
  async function prepareIncoming(parsed,url,root,fetchImpl) {
    parsed.querySelectorAll('base,meta[http-equiv],iframe,object,embed,form,video,audio,source').forEach(node=>node.remove());
    for(const link of [...parsed.querySelectorAll('link')]) {
      if(link.rel!=='stylesheet'){link.remove();continue;}
      const response=await fetchImpl(localAsset(link.getAttribute('href'),url,root).href,{credentials:'omit',redirect:'error'});
      if(!response.ok)throw new Error('报告样式读取失败，请检查Demo资源。');
      const style=parsed.createElement('style');style.textContent=cleanCss(await response.text());link.replaceWith(style);
    }
    for(const image of parsed.querySelectorAll('img')) {
      const src=image.getAttribute('src')||'';
      if(!src.startsWith('data:image/')) {
        const response=await fetchImpl(localAsset(src,url,root).href,{credentials:'omit',redirect:'error'});
        if(!response.ok)throw new Error('报告图片读取失败，请检查Demo资源。');
        image.src=await blobData(await response.blob());
      }
      image.removeAttribute('srcset');image.removeAttribute('loading');
    }
    for(const style of parsed.querySelectorAll('style'))style.textContent=cleanCss(style.textContent);
    for(const node of parsed.querySelectorAll('*')) {
      for(const attr of [...node.attributes]) {
        if(attr.name.startsWith('on')||['srcset','ping','srcdoc','action','formaction'].includes(attr.name))node.removeAttribute(attr.name);
      }
      if(node.hasAttribute('style'))node.setAttribute('style',cleanCss(node.getAttribute('style')));
    }
    // The inner editing frame runs no scripts and can load no network resources.
    const csp=parsed.createElement('meta');csp.httpEquiv='Content-Security-Policy';
    csp.content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:; form-action 'none'; base-uri 'none'";
    parsed.head.prepend(csp);
    return '<!doctype html>\n'+parsed.documentElement.outerHTML;
  }
  function blobData(blob) {
    return new Promise((resolve,reject)=>{
      const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(blob);
    });
  }
  function capture(editor) {
    const source=editor.document, copy=source.cloneNode(true), styles=new WeakMap();
    const live=[...source.querySelectorAll('*')], cloned=[...copy.querySelectorAll('*')];
    const computedWindow=source.defaultView;
    live.forEach((node,index)=>{
      if(!node.matches('h1,h2,h3,h4,h5,h6,p,li,td,th,span,strong,b,i,em,a,font,div,dt,dd,figcaption'))return;
      const computed=computedWindow?.getComputedStyle(node);
      if(computed)styles.set(cloned[index],{
        bold:parseInt(computed.fontWeight,10)>=600||computed.fontWeight==='bold',
        italics:computed.fontStyle==='italic',color:colorHex(computed.color),
        size:Math.round((parseFloat(computed.fontSize)||14)*1.5),
        alignment:computed.textAlign,underline:computed.textDecorationLine?.includes('underline'),
      });
    });
    const title=editor.nodes.editorTitle.value.trim()||'未命名研究报告';copy.title=title;
    if(copy.querySelector('h1'))copy.querySelector('h1').textContent=title;
    for(const [id,spec] of Object.entries(editor.chartSpecs)) {
      const host=copy.getElementById(id);if(!host)continue;
      const instance=editor.chartInstances.get(id);
      if(!instance)throw new Error(`图表${spec.title||id}尚未渲染，无法导出完整报告`);
      // Serialize the rendered SVG DOM: ECharts getDataURL can emit unescaped font-family quotes in legends.
      const svg=source.getElementById(id)?.querySelector('svg');
      if(!svg)throw new Error(`图表${spec.title||id}缺少完整SVG，无法导出`);
      const svgText=new editor.window.XMLSerializer().serializeToString(svg);
      const image=copy.createElement('img');image.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svgText);
      image.alt=spec.title||id;image.style.cssText='display:block;width:100%;height:auto;max-width:100%';
      host.replaceChildren(image);host.style.height='auto';
    }
    copy.querySelectorAll('#optionhelper-report-editor-style,#oh-demo-editor-canvas-style,script,base,iframe,object,embed,form,input,button,textarea,select').forEach(node=>node.remove());
    for(const node of copy.querySelectorAll('*')) {
      for(const attr of [...node.attributes]) {
        if(attr.name.startsWith('data-report-editor')||attr.name.startsWith('on')||['contenteditable','spellcheck','_echarts_instance_','data-runtime-echarts'].includes(attr.name))node.removeAttribute(attr.name);
      }
    }
    const specs=copy.createElement('template');specs.setAttribute('data-oh-chart-specs','');
    specs.content.append(copy.createTextNode(JSON.stringify(editor.chartSpecs)));
    copy.body.append(specs);
    const style=copy.createElement('style');style.textContent='@media print{details>*{display:block!important}.chart img{max-width:100%;height:auto}table{max-width:100%}}';copy.head.append(style);
    return {document:copy,title,styles,window:editor.window};
  }
  function colorHex(value) {
    if(/^#[0-9a-f]{6}$/i.test(value))return value.slice(1).toUpperCase();
    const rgb=/rgba?\(\s*(\d+)[, ]+\s*(\d+)[, ]+\s*(\d+)/.exec(value||'');
    return rgb?rgb.slice(1).map(v=>Math.min(255,+v).toString(16).padStart(2,'0')).join('').toUpperCase():undefined;
  }
  function filename(title,format) {
    return `${title.replace(/[\\/:*?"<>|\u0000-\u001f]/g,'_').slice(0,100)||'研究报告'}-编辑副本.${format}`;
  }
  function download(blob,name,document) {
    const url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download=name;link.hidden=true;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  async function toHtml(snapshot) {
    return new Blob(['<!doctype html>\n'+snapshot.document.documentElement.outerHTML],{type:'text/html;charset=utf-8'});
  }
  async function rasterImage(src,ownerWindow) {
    if(!/^data:image\//i.test(src))throw new Error('导出图片必须内嵌在当前报告中。');
    const image=new ownerWindow.Image();
    await new Promise((resolve,reject)=>{
      const timer=ownerWindow.setTimeout(()=>reject(new Error('图片解码超时，当前报告未导出。')),15000);
      image.onload=()=>{ownerWindow.clearTimeout(timer);resolve();};
      image.onerror=()=>{ownerWindow.clearTimeout(timer);reject(new Error('图片解码失败，当前报告未导出。'));};
      image.src=src;
    });
    const width=image.naturalWidth||800,height=image.naturalHeight||450;
    if(width*height>36000000)throw new Error('图片过大，请先缩小后再导出。');
    const scale=Math.min(2,2200/width),canvas=ownerWindow.document.createElement('canvas');
    canvas.width=Math.max(1,Math.round(width*scale));canvas.height=Math.max(1,Math.round(height*scale));
    const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
    const data=canvas.toDataURL('image/png');return {data,width,height};
  }
  class WordBuilder {
    constructor(snapshot,docx) {this.snapshot=snapshot;this.d=docx;this.images=new Map();this.nextList=0;this.numbering=[];}
    style(node,inherited={}) {
      const computed=this.snapshot.styles.get(node)||{};
      const inline=node.style||{};
      return {...inherited,...computed,
        ...(inline.color?{color:colorHex(inline.color)}:{}),
        ...(inline.fontSize?{size:Math.round(parseFloat(inline.fontSize)*(inline.fontSize.endsWith('pt')?2:1.5))}:{}),
        ...(inline.fontWeight?{bold:parseInt(inline.fontWeight,10)>=600||inline.fontWeight==='bold'}:{}),
        ...(inline.fontStyle?{italics:inline.fontStyle==='italic'}:{}),
        ...(inline.textAlign?{alignment:inline.textAlign}:{}),
      };
    }
    textRun(text,style={}) {
      const {alignment,underline,...format}=style;
      return new this.d.TextRun({text:safeText(text),font:BODY_FONT,size:21,...format,...(underline?{underline:{}}:{})});
    }
    math(node) {
      const d=this.d,kids=[...node.children],convert=n=>this.math(n),tag=node.localName;
      if(['mi','mo','mn','mtext','ms'].includes(tag))return [new d.MathRun(safeText(node.textContent))];
      if(['math','mrow','semantics','mstyle','mpadded'].includes(tag))return kids.filter(n=>!['annotation','annotation-xml'].includes(n.localName)).flatMap(convert);
      if(tag==='mfrac')return [new d.MathFraction({numerator:convert(kids[0]),denominator:convert(kids[1])})];
      if(tag==='msup')return [new d.MathSuperScript({children:convert(kids[0]),superScript:convert(kids[1])})];
      if(tag==='msub')return [new d.MathSubScript({children:convert(kids[0]),subScript:convert(kids[1])})];
      if(tag==='msubsup')return [new d.MathSubSuperScript({children:convert(kids[0]),subScript:convert(kids[1]),superScript:convert(kids[2])})];
      if(tag==='msqrt')return [new d.MathRadical({children:kids.flatMap(convert)})];
      if(tag==='mroot')return [new d.MathRadical({children:convert(kids[0]),degree:convert(kids[1])})];
      if(tag==='mfenced')return [new d.MathRun(node.getAttribute('open')||'('),...kids.flatMap(convert),new d.MathRun(node.getAttribute('close')||')')];
      if(tag==='mspace')return [new d.MathRun(' ')];
      throw new Error(`公式结构${tag}暂不支持可编辑DOCX，请改用HTML副本或打印。`);
    }
    async inline(nodes,style={},maxWidth=640) {
      const result=[];
      for(const node of nodes) {
        if(node.nodeType===3){if(node.nodeValue)result.push(this.textRun(normalText(node.nodeValue),style));continue;}
        if(node.nodeType!==1||node.matches(SKIP))continue;
        const tag=node.localName,next=this.style(node,style);
        if(tag==='br'){result.push(new this.d.TextRun({break:1}));continue;}
        if(tag==='math'){result.push(new this.d.Math({children:this.math(node)}));continue;}
        if(tag==='img'||tag==='svg') {
          const src=tag==='img'?node.getAttribute('src'):'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(new XMLSerializer().serializeToString(node));
          if(!this.images.has(src))this.images.set(src,rasterImage(src,this.snapshot.window));
          const image=await this.images.get(src);
          const width=Math.min(image.width,maxWidth),height=Math.min(850,image.height*width/image.width);
          const adjustedWidth=height*image.width/image.height;
          result.push(new this.d.ImageRun({type:'png',data:image.data,transformation:{width:adjustedWidth,height},altText:{title:node.getAttribute('alt')||'报告图表',description:node.getAttribute('alt')||'报告图表',name:'报告图表'}}));
          continue;
        }
        if(['b','strong','th'].includes(tag))next.bold=true;
        if(['i','em'].includes(tag))next.italics=true;
        if(tag==='sub')next.subScript=true;if(tag==='sup')next.superScript=true;
        result.push(...await this.inline(node.childNodes,next,maxWidth));
      }
      return result;
    }
    paragraph(children,options={}) {
      const {alignment,...rest}=options;
      return new this.d.Paragraph({children,spacing:{after:100,line:280},...(alignment&&['left','right','center','justify'].includes(alignment)?{alignment:alignment==='justify'?'both':alignment}:{}),...rest});
    }
    async table(element,maxWidth=640,rowsOverride=null) {
      const d=this.d,rows=rowsOverride||[...element.rows].map(row=>[...row.cells]);
      const columns=Math.max(1,...rows.map(row=>row.reduce((sum,cell)=>sum+(Number(cell.getAttribute('colspan'))||1),0)));
      const rowsOut=[];
      for(let index=0;index<rows.length;index++) {
        const cells=[];
        for(const cell of rows[index]) {
          const span=Math.max(1,Number(cell.getAttribute('colspan'))||1);
          const children=await this.blocks(cell.childNodes,Math.max(50,maxWidth*span/columns-14),{size:columns>6?16:19,bold:cell.tagName==='TH'});
          if(!children.length||!(children.at(-1) instanceof d.Paragraph))children.push(this.paragraph([]));
          cells.push(new d.TableCell({children,columnSpan:span,rowSpan:Math.max(1,Number(cell.getAttribute('rowspan'))||1),
            width:{size:Math.round(9600*span/columns),type:d.WidthType.DXA},margins:{top:65,bottom:65,left:80,right:80},
            ...(index===0&&rows[index].some(n=>n.tagName==='TH')?{shading:{fill:'F1F2F3'}}:{}),
          }));
        }
        rowsOut.push(new d.TableRow({children:cells,tableHeader:index===0&&rows[index].some(n=>n.tagName==='TH')}));
      }
      if(!rowsOut.length)return [];
      return [new d.Table({rows:rowsOut,width:{size:100,type:d.WidthType.PERCENTAGE},layout:d.TableLayoutType.FIXED,
        columnWidths:Array(columns).fill(Math.round(9600/columns)),
        borders:Object.fromEntries(['top','bottom','left','right','insideHorizontal','insideVertical'].map(side=>[side,{style:d.BorderStyle.SINGLE,size:4,color:'D8DADD'}])),
      }),this.paragraph([],{spacing:{after:60}})];
    }
    async blocks(nodes,maxWidth=640,inherited={}) {
      const output=[];let pending=[];
      const flush=async()=>{if(pending.length){const inline=await this.inline(pending,inherited,maxWidth);if(inline.length)output.push(this.paragraph(inline,inherited));pending=[];}};
      for(const node of nodes) {
        if(node.nodeType===3){if(node.nodeValue.trim())pending.push(node);continue;}
        if(node.nodeType!==1||node.matches(SKIP))continue;
        const tag=node.localName;
        if(!BLOCKS.has(tag)&&!node.classList.contains('comparison-matrix')){pending.push(node);continue;}
        await flush();
        const style=this.style(node,inherited);
        if(/^h[1-6]$/.test(tag)) {
          const level=+tag.slice(1),heading=level===1?this.d.HeadingLevel.TITLE:this.d.HeadingLevel[`HEADING_${Math.min(level-1,6)}`];
          output.push(this.paragraph(await this.inline(node.childNodes,{...style,bold:true,size:[0,38,28,24,22,21,21][level]},maxWidth),{
            heading,keepNext:true,spacing:{before:level===1?100:240,after:120},alignment:style.alignment,
          }));
        } else if(tag==='table') {
          if(node.caption)output.push(this.paragraph(await this.inline(node.caption.childNodes,{size:18,bold:true},maxWidth),{keepNext:true}));
          output.push(...await this.table(node,maxWidth));
        } else if(node.classList.contains('comparison-matrix')) {
          const rows=[...node.children].filter(n=>n.children.length).map(row=>[...row.children]);
          output.push(...await this.table(node,maxWidth,rows));
        } else if(tag==='ul'||tag==='ol') {
          const reference=`report-list-${++this.nextList}`;
          this.numbering.push({reference,levels:[{level:0,format:tag==='ol'?this.d.LevelFormat.DECIMAL:this.d.LevelFormat.BULLET,text:tag==='ol'?'%1.':'•',alignment:this.d.AlignmentType.LEFT,style:{paragraph:{indent:{left:360,hanging:180}}}}]});
          for(const item of node.children) {
            const content=[...item.childNodes].filter(n=>!['ul','ol'].includes(n.localName));
            output.push(this.paragraph(await this.inline(content,style,maxWidth-24),{numbering:{reference,level:0}}));
            for(const nested of item.children)if(['ul','ol'].includes(nested.localName))output.push(...await this.blocks([nested],maxWidth-24,style));
          }
        } else if(node.classList.contains('metric')) {
          const label=node.querySelector('.metric__label'),value=node.querySelector('.metric__value'),note=node.querySelector('.metric__note');
          if(label&&value)output.push(this.paragraph([this.textRun(label.textContent+'：',{bold:true}),...await this.inline(value.childNodes,this.style(value,{bold:true}),maxWidth),...(note?[this.textRun('  '+note.textContent,{size:18})]:[])]));
          else output.push(...await this.blocks(node.childNodes,maxWidth,style));
        } else if(['p','dt','dd','figcaption','blockquote','pre'].includes(tag)||!node.children.length) {
          const inline=await this.inline(node.childNodes,style,maxWidth);
          if(inline.length)output.push(this.paragraph(inline,{alignment:style.alignment,...(tag==='figcaption'?{keepNext:true}:{})}));
        } else output.push(...await this.blocks(node.childNodes,maxWidth,style));
      }
      await flush();return output;
    }
    async build() {
      const d=this.d,children=await this.blocks(this.snapshot.document.body.childNodes);
      if(!children.length)throw new Error('当前报告没有可导出的正文。');
      return new d.Document({creator:'OptionHelper Demo',title:this.snapshot.title,description:'编辑副本',
        styles:{default:{document:{run:{font:BODY_FONT,size:21,color:'252525'},paragraph:{spacing:{after:100,line:280}}}}},
        numbering:{config:this.numbering},
        sections:[{properties:{page:{size:{width:11906,height:16838},margin:{top:1020,bottom:1020,left:1134,right:1134}}},children}],
      });
    }
  }
  async function toDocx(snapshot) {
    if(!window.docx?.Packer)throw new Error('DOCX库未能加载，请检查Demo资源。');
    const builder=new WordBuilder(snapshot,window.docx),document=await builder.build();
    return window.docx.Packer.toBlob(document);
  }
  async function print(snapshot,document) {
    const frame=document.createElement('iframe');frame.title='报告打印副本';frame.setAttribute('aria-hidden','true');
    frame.setAttribute('sandbox','allow-same-origin allow-modals');
    frame.style.cssText='position:fixed;width:1px;height:1px;left:-10000px;top:0;border:0';
    const html='<!doctype html>\n'+snapshot.document.documentElement.outerHTML;
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{frame.remove();reject(new Error('打印页面加载超时'));},15000);
      frame.onload=()=>{clearTimeout(timer);resolve();};frame.srcdoc=html;document.body.append(frame);
    });
    try {
      await Promise.all([...frame.contentDocument.images].map(image=>image.decode()));
      await frame.contentDocument.fonts?.ready;
      frame.contentWindow.addEventListener('afterprint',()=>frame.remove(),{once:true});
      frame.contentWindow.focus();frame.contentWindow.print();
      setTimeout(()=>frame.remove(),60000);
    } catch(error) {frame.remove();throw error;}
    return true;
  }
  window.OptionHelperDemoReportExport=Object.assign(window.OptionHelperDemoReportExport||{},{prepareIncoming,capture,toHtml,toDocx,print,filename,download,WordBuilder});
})();
