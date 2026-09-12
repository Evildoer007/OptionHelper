"""Build offline presentation directly from the current App source tree."""
from pathlib import Path
import re,json,subprocess,base64,mimetypes,sys,os,hashlib
ROOT=Path(os.environ.get('OPTIONHELPER_SOURCE_ROOT', Path.home()/'Desktop/OptionHelper')).resolve()
DEMO=Path(__file__).resolve().parents[1]
PUBLIC=DEMO/'public'
for folder in [PUBLIC/'data', PUBLIC/'assets', DEMO/'tests']:
    folder.mkdir(parents=True, exist_ok=True)
sys.path[:0]=[str(ROOT),str(ROOT/'core/src')]
import types
for name in ["pricer","payoffer","backtester","datafetcher","designer","reporter"]:
    package=types.ModuleType("modules."+name);package.__path__=[str(ROOT/"modules"/name/"src")];sys.modules["modules."+name]=package
from modules.pricer.service import PricerRuntime
from modules.payoffer.service import catalog_payload, default_example_payload
catalog={'pricing':PricerRuntime(result_store=object()).catalog(),'payoffer':catalog_payload()}
(PUBLIC/'data/current-ui-catalog.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
used={}
source_text={}
def read(p):
    p=p.resolve()
    text=p.read_text(encoding='utf-8')
    relative=str(p.relative_to(ROOT))
    if relative in source_text and source_text[relative]!=text:
        raise RuntimeError(f'构建期间源码发生变化，请重新构建：{relative}')
    source_text[relative]=text
    used[relative]=len(text.encode('utf-8'))
    return text

def resolve(url,base):
    url=url.split('?')[0]
    if url.startswith('/app/frontend/'):return ROOT/'products'/url.lstrip('/')
    if url=='/app/designer-token-vars.css':return ROOT/'modules/designer/assets/themes/designer-token-vars.css'
    if '/designer/' in url:return ROOT/'modules/designer/assets'/url.split('/designer/')[-1].removeprefix('assets/')
    if '/icons/' in url:return ROOT/'assets/icons'/url.split('/')[-1]
    p=(base.parent/url).resolve()
    if p.is_file():return p
    name=Path(url).name
    browser=ROOT/'core/src/runtime/browser'/name.replace('-','_')
    if browser.exists():return browser
    if name=='plotly-optionhelper.min.js':return ROOT/'core/src/runtime/browser/vendor'/name
    if name=='plotly-chart-system.js':return ROOT/'core/src/runtime/browser/plotly_chart_system.js'
    raise FileNotFoundError(f'{base}: {url}')

def uri(p):
    used[str(p.relative_to(ROOT))]=p.stat().st_size
    return 'data:'+(mimetypes.guess_type(p.name)[0] or 'application/octet-stream')+';base64,'+base64.b64encode(p.read_bytes()).decode()

def css(p):
    s=read(p)
    s=re.sub(r'@import\s+(?:url\()?\s*[\"\']([^\"\']+)[\"\']\s*\)?\s*;',lambda m:css(resolve(m[1],p)),s)
    def resource(m):
        u=m[1].strip('"\' ')
        if u.startswith(('data:','#')):return m[0]
        return 'url("'+uri(resolve(u,p))+'")'
    return re.sub(r'url\(([^)]+)\)',resource,s)

def adapt_transport(s):
    s=re.sub(r'\bwindow\.location\b','window.OHOffline.location',s)
    s=re.sub(r'(?<![\w.])location\b','window.OHOffline.location',s)
    s=s.replace('event.origin !== window.OHOffline.location.origin','event.origin !== window.OHOffline.receiveOrigin')
    return re.sub(r'},\s*window\.OHOffline\.location\.origin\s*\)','}, window.OHOffline.messageOrigin)',s)

def script(p,is_module=False,module=None):
    if is_module or p.name=='module_host_presentation.js':
        s=subprocess.check_output(['node',str(DEMO/'tools/bundle-current-ui.cjs')],input=json.dumps({'entry':str(p),'sourceRoot':str(ROOT)}).encode()).decode()
        read(p)
        for dep in json.loads((DEMO/'tools/current-bundle-inputs.json').read_text()):read(Path(dep))
    else:s=adapt_transport(read(p))
    if p.name=='module_host_bridge.js':
        s=s.replace('window.fetch = hostedFetch','void 0')
    # Asset URLs embedded in source templates are inlined for file:// use.
    s=re.sub(r'/(?:capability/assets|app/assets)/icons/([\w.-]+)',lambda m:uri(ROOT/'assets/icons'/m[1]),s)
    return s.replace('</script','<\\/script')

def html(p,module=None):
    s=read(p)
    # One module graph per document preserves native ESM singleton identity.
    # Separate bundles duplicated theme subscribers and broke iframe themes.
    module_tags=list(re.finditer(r'<script\b[^>]*type="module"[^>]*src="([^"]+)"[^>]*></script>',s))
    joint_bundle=None
    if module_tags:
        entries=[str(resolve(m[1],p)) for m in module_tags]
        joint_bundle=subprocess.check_output(['node',str(DEMO/'tools/bundle-current-ui.cjs')],input=json.dumps({'entries':entries,'sourceRoot':str(ROOT)}).encode()).decode()
        for dep in json.loads((DEMO/'tools/current-bundle-inputs.json').read_text()):read(Path(dep))
        joint_bundle=re.sub(r'/(?:capability/assets|app/assets)/icons/([\w.-]+)',lambda m:uri(ROOT/'assets/icons'/m[1]),joint_bundle).replace('</script','<\\/script')
        last_tag=module_tags[-1][0]
    def link(m):
        tag=m[0];href=re.search(r'href="([^"]+)"',tag)
        if not href:return tag
        if 'stylesheet' in tag:return '<style>'+css(resolve(href[1],p))+'</style>'
        if 'icon' in tag:return tag.replace(href[1],uri(resolve(href[1],p)))
        return '' if 'preload' in tag else tag
    s=re.sub(r'<link\b[^>]*>',link,s)
    def scripts(m):
        tag,body=m[1],m[2];src=re.search(r'src="([^"]+)"',tag)
        if src and 'type="module"' in tag and joint_bundle is not None:
            return '<script>'+joint_bundle+'</script>' if m[0]==last_tag else ''
        code=script(resolve(src[1],p),'type="module"' in tag,module) if src else adapt_transport(body)
        extra=''
        if module=='reporter' and src and src[1]=='./editor/report-editor.js':
            extra=''.join('<script src="'+u+'"></script>' for u in ['assets/report-editing/docx.iife.js','assets/report-editing/export.js','assets/current-report-export.js'])
        return '<script>'+code+'</script>'+extra
    s=re.sub(r'<script\b([^>]*)>(.*?)</script>',scripts,s,flags=re.S)
    s=re.sub(r'(src=")([^"<>]+)(")',lambda m:m[1]+uri(resolve(m[2],p))+m[3] if ('/icons/' in m[2]) else m[0],s)
    boot='<script>window.OH_MODULE='+json.dumps(module)+';</script><script src="assets/current-offline-runtime.js"></script>'
    return s.replace('<head>','<head>'+boot,1)

defaults={row['product_id']:default_example_payload(row['product_id']) for row in catalog['payoffer']['products']}
(PUBLIC/'assets/current-default-payoffs.js').write_text('window.OH_DEFAULT_PAYOFFS='+json.dumps(defaults,ensure_ascii=False)+';',encoding='utf-8')
reports=[]
for report in sorted((PUBLIC/'result/reports').glob('*.html')):
    title=re.search(r'<title>(.*?)</title>',report.read_text(),re.S)
    reports.append({'report_run_id':'historical-'+report.stem,'title':'历史示例：'+(title[1] if title else report.stem),'status':'succeeded','output_type':'report','artifacts':[{'name':report.name,'url':'result/reports/'+report.name}]})
(PUBLIC/'assets/current-report-library.js').write_text('window.OH_HISTORICAL_REPORTS='+json.dumps(reports,ensure_ascii=False)+';',encoding='utf-8')
pages={}
asset_map={f'/app/assets/icons/{p.name}':uri(p) for p in sorted((ROOT/'assets/icons').glob('*.svg'))}
(PUBLIC/'assets/current-icon-assets.js').write_text('window.OH_ICON_ASSETS='+json.dumps(asset_map)+';\n',encoding='utf-8')
for name in ['login','optdesk','optchat','settings']:
    pages[name]=html(ROOT/f'products/app/frontend/{name}/index.html')
for name in ['payoffer','pricer','backtester','datafetcher','reporter']:
    pages[name]=html(ROOT/f'modules/{name}/page/{name}.html',name)
(PUBLIC/'assets/current-app-pages.js').write_text('window.OH_CURRENT_PAGES='+json.dumps(pages,ensure_ascii=False)+';\n',encoding='utf-8')
(PUBLIC/'assets/current-catalog.js').write_text('window.OH_CURRENT_CATALOG='+json.dumps(catalog,ensure_ascii=False)+';\n',encoding='utf-8')
(PUBLIC/'index.html').write_text('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OptionHelper</title></head><body><script src="assets/current-app-pages.js"></script><script src="assets/current-catalog.js"></script><script src="assets/current-default-payoffs.js"></script><script src="assets/current-report-library.js"></script><script>const q=new URLSearchParams(location.search);const view=q.get('view');const page=view==='app'?(q.get('mode')==='chat'?'optchat':'optdesk'):view==='settings'?'settings':'login';document.open();document.write(window.OH_CURRENT_PAGES[page]);document.close();</script></body></html>''',encoding='utf-8')
entry=PUBLIC/'index.html'
entry.write_text(entry.read_text(encoding='utf-8').replace('<script src="assets/current-app-pages.js">','<script src="assets/current-icon-assets.js"></script><script src="assets/current-app-pages.js">'),encoding='utf-8')
(DEMO/'tests/current-source-inventory.json').write_text(json.dumps({'source_root':str(ROOT),'files':used,'pages':list(pages),'boundary':'Only routing, asset embedding and offline transport are adapted; original HTML and CSS drive the interface.'},ensure_ascii=False,indent=2),encoding='utf-8')
print('Built',len(pages),'current source pages;',len(used),'source resources')

(DEMO/'tests/current-source-text.json').write_text(json.dumps(source_text,ensure_ascii=False),encoding='utf-8')

hashes={relative:hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() for relative in sorted(used)}
(PUBLIC/'source-manifest.json').write_text(json.dumps({'pages':list(pages),'sha256':hashes},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

for relative, expected in source_text.items():
    if (ROOT/relative).read_text(encoding='utf-8')!=expected:
        raise RuntimeError(f'构建结束前源码发生变化：{relative}')
