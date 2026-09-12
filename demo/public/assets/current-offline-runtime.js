/* Offline transport for the unmodified current App presentation. */
(()=>{
 const topWindow=window.parent!==window?window.parent:window;
 const pages=topWindow.OH_CURRENT_PAGES, catalog=topWindow.OH_CURRENT_CATALOG;
 const module=window.OH_MODULE;
 const origin=topWindow.location.protocol==='file:'?'http://offline.local':topWindow.location.origin;
 const taskKey='oh.offline.current.tasks';
 const readTasks=()=>{try{return JSON.parse(localStorage.getItem(taskKey))||[]}catch{return []}};
 const saveTasks=t=>localStorage.setItem(taskKey,JSON.stringify(t));
 const route=path=>{const u=new URL(path,origin);const q=new URLSearchParams(u.search);if(u.pathname.includes('settings'))q.set('view','settings');else if(u.pathname.includes('optchat')||u.pathname.includes('optdesk')){q.set('view','app');q.set('mode',u.pathname.includes('optdesk')?'desk':'chat')}else if(u.pathname==='/')q.set('view','login');return 'index.html?'+q+u.hash};
 const originalPush=history.pushState.bind(history),originalReplace=history.replaceState.bind(history);
 // A virtual App address keeps source routing intact on static hosting and file://.
 // Browser origin remains real for iframe message validation; file URLs use a
 // virtual URL only for URL construction and a null target origin for receiving.
 const virtualURL=()=>{
  if(window.OH_PAGE_URL)return new URL(window.OH_PAGE_URL,origin);
  if(module)return new URL('/capability/assets/pages/'+module+'/'+module+'.html?host=optdesk&task_id='+encodeURIComponent(new URLSearchParams(topWindow.location.search).get('task')||'')+'&bridge_nonce='+encodeURIComponent(window.OH_BRIDGE_NONCE||''),origin);
  const q=new URLSearchParams(location.search),view=q.get('view'),mode=q.get('mode');
  q.delete('view');q.delete('mode');
  return new URL((view==='app'?(mode==='desk'?'/optdesk':'/optchat'):view==='settings'?'/settings':'/')+'?'+q+location.hash,origin);
 };
 const virtualLocation={
  get href(){return virtualURL().href}, get pathname(){return virtualURL().pathname},
  get search(){return virtualURL().search},get hash(){return virtualURL().hash},
  get protocol(){return virtualURL().protocol},get origin(){return topWindow.location.origin},
  assign(path){location.assign(route(path))},replace(path){location.replace(route(path))},
 };
 // new URL('/path', 'null') is invalid: file mode uses a construction origin.
 if(topWindow.location.protocol==='file:')Object.defineProperty(virtualLocation,'origin',{get:()=>origin});
 function embeddedPage(name,head){return pages[name].replace('<head>','<head><base href="'+new URL('.',topWindow.location.href).href+'">'+head)}
 window.OHOffline={origin:topWindow.location.origin,baseOrigin:origin,location:virtualLocation,receiveOrigin:topWindow.location.protocol==='file:'?'null':topWindow.location.origin,messageOrigin:topWindow.location.protocol==='file:'?'*':origin,
  navigate(path){virtualLocation.assign(path)},pushState(a,b,url){originalPush(a,b,route(url))},replaceState(a,b,url){originalReplace(a,b,route(url))},
  modulePage(name,nonce){return embeddedPage(name,'<script>window.OH_BRIDGE_NONCE='+JSON.stringify(nonce)+';<\/script>')},
  settingsPage(url){return embeddedPage('settings','<script>window.OH_PAGE_URL='+JSON.stringify(url.href)+';<\/script>')},
  assetURL(url){return topWindow.OH_ICON_ASSETS?.[url]||url},
 };
 const json=(x,status=200)=>new Response(JSON.stringify(x),{status,headers:{'content-type':'application/json'}});
 const fail=message=>json({ok:false,message,status:'unavailable',stage:'offline_demo'},422);
 window.fetch=async(input,options={})=>{
  const url=new URL(typeof input==='string'?input:input.url,origin),p=url.pathname;
  let body={};try{body=JSON.parse(options.body||'{}')}catch{}
  if(p==='/api/auth/login'){if(body.account==='admin'&&body.password==='8888'){sessionStorage.setItem('oh.offline.admin','true');return json({ok:true})}return json({message:'账号或密码不正确。'},401)}
  if(p==='/api/auth/logout'){sessionStorage.removeItem('oh.offline.admin');return json({ok:true})}
  if(p==='/api/me')return sessionStorage.getItem('oh.offline.admin')?json({identity:{principal_id:'admin',role:'admin',tenant_id:'offline'},capabilities:['optdesk','optchat','report.card.request','report.full.request','report.quote.request','settings.read','settings.model.write','settings.data.write']}):json({message:'请登录'},401);
  if(p==='/api/settings')return json({settings:{preferences:{theme:localStorage.getItem('oh-theme')||'light'},storage_export:{},model:{providers:[],default_provider_id:null},data_interface:{},multi_agent:{enabled:false}}});
  if(p==='/api/settings/preferences'){if(['light','dark','auto'].includes(body.theme))localStorage.setItem('oh-theme',body.theme);return json({ok:true})}
  if(p==='/api/settings/storage')return json({ok:true});
  if(p==='/api/settings/model-providers')return json({providers:[],models:[],configured:false});
  if(p.includes('multi-agent'))return json({presets:[],models:[],enabled:false});
  if(p.includes('runtime')||p==='/api/status')return json({ok:true,status:module==='reporter'?'available':'offline',mode:'offline',configured:false,connected:false,assets:[],message:'完全离线Demo，未连接计算、行情或模型服务。'});
  if(p==='/api/tasks'){
   const tasks=readTasks();if(options.method==='POST'){const task={task_id:crypto.randomUUID(),subject:body.subject||'新建研究任务',messages:[],events:[],reports:[],active_operations:[],state:'idle',created_at:new Date().toISOString(),updated_at:new Date().toISOString()};tasks.unshift(task);saveTasks(tasks);return json({task})}return json({tasks});
  }
  if(p.startsWith('/api/module-host/'))return json({context:{task_id:new URLSearchParams(url.search).get('task_id'),module:p.split('/').pop(),role:'admin',permissions:['read','run'],result_refs:[]}});
  if(p.startsWith('/api/tasks/')){
   const id=p.split('/')[3],tasks=readTasks(),task=tasks.find(t=>t.task_id===id);
   if(p.endsWith('/rename')){if(task)task.subject=body.subject||body.title;saveTasks(tasks);return json({task})}
   if(p.endsWith('/delete')){saveTasks(tasks.filter(t=>t.task_id!==id));return json({ok:true})}
   if(p.endsWith('/operations'))return options.method==='POST'?fail('离线Demo不连接计算服务。'):json({operations:[]});
   if(p.endsWith('/reports'))return json({reports:topWindow.OH_HISTORICAL_REPORTS||[]});
   if(p.includes('attachment'))return json({attachments:[]});
   return task?json({task}):json({message:'任务不存在'},404);
  }
  if(p==='/api/report-documents'){const id=crypto.randomUUID();localStorage.setItem('oh.offline.report.'+id,JSON.stringify({...body,source_report_run_id:id,ok:true}));return json({ok:true,report_run_id:id,task_id:body.task_id})}
  if(p.startsWith('/api/reports/')&&p.endsWith('/editor')){const draft=localStorage.getItem('oh.offline.report.'+p.split('/')[3]);return draft?json(JSON.parse(draft)):json({message:'报告未找到'},404)}
  if(p==='/api/catalog')return json(module==='payoffer'?catalog.payoffer:catalog.pricing);
  if(p==='/api/assets')return json({ok:true,assets:[],data_assets:[]});
  if(p==='/api/report-sources')return json({ok:true,tasks:readTasks(),sources:[],runs:[],report_sources:[],available_modules:[],reports:[]});
  if(p==='/api/default'&&module==='payoffer')return json(window.OH_DEFAULT_PAYOFFS?.[body.product_id]||topWindow.OH_DEFAULT_PAYOFFS?.[body.product_id]||{ok:false,message:'默认收益图尚未载入。'});
  if(p==='/api/preview'&&module==='backtester')return fail('完全离线Demo没有连接历史数据服务。可以编辑日期和条款；正式回测请在App中运行。');
  if(p==='/api/run'||p==='/api/preview'||p==='/api/fetch')return fail('完全离线Demo没有连接计算或行情服务。当前输入已保留，请在App中计算。');
  return fail('此功能需要App本地服务，完全离线Demo未连接该服务。');
 };
 window.addEventListener('click',event=>{if(event.defaultPrevented)return;const a=event.target.closest('a[href]');if(a&&/^\/(optchat|optdesk|settings|$)/.test(a.getAttribute('href'))){event.preventDefault();window.OHOffline.navigate(a.getAttribute('href'))}});
 if(module){
  window.addEventListener('message',event=>{if(event.source!==window.parent)return;const d=event.data||{};if(d.type==='optionhelper.module-host-context'){window.dispatchEvent(new CustomEvent('optionhelper.module-host-context',{detail:{context:d.context}}));window.parent.postMessage({type:'optionhelper.module-host-context-ack',module,bridge_nonce:window.OH_BRIDGE_NONCE},'*')}if(d.type==='optionhelper.module-theme')document.documentElement.dataset.theme=d.theme;});
  window.addEventListener('load',()=>{window.parent.postMessage({type:'optionhelper.module-host-ready',module,bridge_nonce:window.OH_BRIDGE_NONCE},'*');window.dispatchEvent(new CustomEvent('optionhelper.module-host-ready',{detail:{module}}))});
 }
})();
