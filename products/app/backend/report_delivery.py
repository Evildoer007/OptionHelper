"""Host-owned report delivery: a document, not a model's claim to have made one."""
from __future__ import annotations

from datetime import date
import re
from typing import Mapping
from uuid import uuid5, NAMESPACE_URL

from modules.designer import render
from modules.designer.models import DesignerInput
from .errors import ValidationError

_TEMPLATE = re.compile(r'(?<![A-Za-z])(multireport|multicard|report|card|quote)(?![A-Za-z])', re.I)

def delivery_request(text):
    text = str(text or '').strip()
    if re.search(r'源码|源代码|代码示例|不要.{0,8}(报告|生成)|为什么|为何|怎么用|区别', text):
        return None
    template_match = _TEMPLATE.search(text)
    file_word = re.search(r'报告|简报|报价表|html|pdf|word|docx', text, re.I)
    action = re.search(r'生成|导出|下载|给我|给一|再给|改成|转成|整理成|输出|制作|做一', text)
    if (not file_word and not template_match) or (not action and text.lower() not in {'card','report','quote','multicard','multireport'}):
        return None
    template = template_match.group(1).lower() if template_match else ('quote' if '报价' in text else 'card' if '简报' in text else 'report')
    if re.search(r'多(产品|结构)', text) and template in {'card','report'}:
        template = 'multi' + template
    formats = list(re.finditer(r'html|pdf|word|docx', text, re.I))
    fmt = formats[-1].group().lower() if formats else 'html'
    return {'template': template, 'format': 'docx' if fmt == 'word' else fmt,
            'export_only': bool(re.search(r'再给|改成|转成|导出|下载', text)) and not bool(re.search(r'推荐|重新生成', text))}


def requested_analysis_modules(text):
    """Recognize explicitly requested calculations, leaving plain drafts optional."""
    clauses = re.split(r'[，。；;\n]', str(text or '').lower())
    requested = set()
    patterns = {'payoffer': r'payoffer|收益分析|收益图',
                'pricer': r'pricer|定价|估值', 'backtester': r'backtest(?:er)?|回测'}
    for clause in clauses:
        if re.search(r'(?:不|无需|不用|暂不)(?:需要|进行|执行|做|取行情[、和])?(?:计算|定价|估值|回测)', clause):
            continue
        if re.search(r'(?:各|所有|全部|三[个类])模块.{0,8}结果|完整量化报告|不要草稿', clause):
            requested.update(patterns)
        for module, pattern in patterns.items():
            if re.search(pattern, clause):
                requested.add(module)
    return tuple(module for module in patterns if module in requested)


class ReportDelivery:
    def __init__(self, editor, results, tasks, tools=None):
        self.editor, self.results, self.tasks, self.tools = editor, results, tasks, tools

    def current_document(self, identity, task_id):
        task = self.tasks.get(identity, task_id)
        for message in reversed(task.get('messages', [])):
            for block in reversed(message.get('content_blocks', [])):
                if block.get('type') == 'document':
                    return str(block['report_run_id'])
        return None

    def export(self, identity, task_id, report_id, fmt):
        document = self.editor.editor_payload(identity, report_id)
        if document['task_id'] != task_id:
            raise ValidationError('报告不属于当前对话')
        request = {'title': document['title'], 'html': document['html'],
                   'chart_specs': document['chart_specs'], 'format': fmt}
        failed_format = None
        try:
            saved = self.editor.save_edit(identity, report_id, request)
        except ValidationError:
            if fmt == 'html':
                raise
            failed_format = fmt
            fmt = 'html'
            saved = self.editor.save_edit(identity, report_id, {**request, 'format':'html'})
        ref = {'type':'document', 'report_run_id':report_id, 'title':document['title'],
               'format':fmt, 'preview_url':f'/api/reports/{report_id}/document-artifacts/report.html',
               'download_url':saved['download_url']}
        if failed_format:
            label = 'Word' if failed_format == 'docx' else 'PDF'
            notice = f'HTML报告已保存，{label}暂时无法导出。可在编辑页重试，或回复“再给{label}”。'
            return {'status':'partial','text':notice,'_assistant_blocks':[{'type':'text','text':notice},ref],'document':ref}
        return {'status':'completed','text':f'{document["title"]}已生成，可预览、编辑或下载。',
                '_assistant_blocks':[ref], 'document':ref}

    def create_document(self, identity, task_id, arguments, *, request_id=None):
        task = self.tasks.get(identity, task_id)
        messages = task.get('messages', [])
        expected = delivery_request(messages[-1].get('content')) if messages and messages[-1].get('role') == 'user' else None
        latest = next((str(row.get('content', '')) for row in reversed(messages) if row.get('role') == 'user'), '')
        if requested_analysis_modules(latest):
            raise ValidationError('本次要求实际计算结果，请先完成所需分析并从分析结果生成报告，不能改交草稿。')
        allowed = {'title','content','template','format'}
        if set(arguments)-allowed:
            raise ValidationError('报告草稿只接受标题、正文、模板和格式')
        template = expected['template'] if expected else str(arguments.get('template') or 'report')
        fmt = expected['format'] if expected else str(arguments.get('format') or 'html').lower()
        if template not in {'card','report','multicard','multireport'} or fmt not in {'html','pdf','docx'}:
            raise ValidationError('请选择研究简报或报告，以及HTML、PDF或Word格式；报价需使用已验证结果')
        content = str(arguments.get('content') or '').strip()
        if not content or len(content)>64000:
            raise ValidationError('报告正文必须非空且不超过64000字符')
        # Model-generated HTML is content, never an executable program.
        if re.search(r'<(?:html|body|section|h1|p)\b', content, re.I):
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content, 'html.parser')
            for node in soup(['script','style']): node.decompose()
            content = soup.get_text('\n', strip=True)
        content = re.sub(r'^```[^\n]*\n|\n```\s*$', '', content).strip()
        default_title = {'card':'结构研究简报','report':'结构研究报告','multicard':'多结构研究简报','multireport':'多结构研究报告'}[template]
        title = str(arguments.get('title') or default_title)[:120]
        pending = self.tasks.pending_recommendation(identity, task_id)
        candidates = sorted((pending or {}).get('candidates') or [], key=lambda item: (not bool(item.get('is_primary')), item.get('rank') if isinstance(item.get('rank'), (int,float)) else 1000000))
        kind = template.removeprefix('multi')
        def facts(candidate):
            label = str(candidate.get('product_name') or candidate.get('title') or candidate.get('product_id') or title)
            return {'schema':'optionhelper.designer-payload','meta':{'title':title,'as_of_date':date.today().isoformat()},
                    'sections':['conclusion','recommendation','parameters','payoff','pricing','backtest','risk'],
                    'conclusion':{'structure_name':label,'has_recommendation':bool(candidate),'reasons':[]},
                    'recommendation':{'structure_name':label,'reason':content,'reason_points':content.split('\n\n'),'has_recommendation':bool(candidate)},
                    'parameters':{},'payoff':{'status':'not_run'},'pricing':{'status':'not_run'},'backtest':{'status':'not_run'},
                    'risk':{'items':['本草稿根据当前对话整理，未完成的定价和回测不构成计算结论。']}}
        payload = facts(candidates[0] if candidates else {})
        if template.startswith('multi'):
            if len(candidates)<2:
                raise ValidationError('多结构报告需要至少两个明确的研究结构，请指定要比较的结构')
            payload['comparison']={'delivery_mode':'comparison','candidates':[
                {'label':f'方案{index+1}','rank':index+1,'is_primary':index==0,
                 'title':str(item.get('product_name') or item.get('product_id') or f'方案{index+1}'),
                 'facts':facts(item)} for index,item in enumerate(candidates)]}
        html = render(DesignerInput(payload=payload,output_type=kind,asset_mode='portable'))['html']
        report_id = 'chat-document-' + uuid5(NAMESPACE_URL, f'{identity.tenant_id}:{identity.principal_id}:{task_id}:{request_id}').hex if request_id else None
        saved = self.editor.import_document(identity,{'task_id':task_id,'title':title,'html':html,'chart_specs':{}},
                                            report_id=report_id, output_type=kind, comparison=template.startswith('multi'))
        return self.export(identity, task_id, saved['report_run_id'], fmt)

    def complete(self, identity, task_id, message, response, *, request_id=None, report_id=None):
        request = delivery_request(message)
        if not request or response.get('status') in {'cancelled','timed_out','unavailable','blocked'} or self.tasks.is_cancelled(identity,task_id):
            return response
        if response.get('document'):
            return response
        # Questions and failed/partial analyses are not report content. An existing
        # verified document can still be exported without rerunning its analysis.
        if not report_id and response.get('status', 'completed') != 'completed':
            return response
        report_id = report_id or (self.current_document(identity,task_id) if request['export_only'] else None)
        if report_id:
            return self.export(identity,task_id,report_id,request['format'])
        if requested_analysis_modules(message) and not self.tools:
            return {'status': 'needs_input', 'text': '本次要求实际计算结果，当前尚不能执行所需分析，未生成草稿。'}
        if self.tools and requested_analysis_modules(message):
            result = self.tools.call(identity, task_id, 'recommendation_delivery.run',
                                     {'kind': request['template'], 'format': request['format']})
            return {**result, 'text': result.get('text') or result.get('next_step') or result.get('message') or '所需计算尚未完成。'}
        pending = self.tasks.pending_recommendation(identity,task_id)
        candidates = sorted((pending or {}).get('candidates') or [], key=lambda item: (not bool(item.get('is_primary')), item.get('rank') if isinstance(item.get('rank'), (int,float)) else 1000000))
        if self.tools:
            args={'kind':request['template'].removeprefix('multi'),'format':'html',
                  'delivery_mode':'comparison' if request['template'].startswith('multi') else 'single'}
            if candidates:
                selected=candidates if request['template'].startswith('multi') else candidates[:1]
                ids=[item.get('candidate_id') for item in selected]
                if all(ids): args['candidate_ids']=ids
            # Never let a current recommendation fall back to unrelated historical runs.
            if not candidates or 'candidate_ids' in args:
                result=self.tools.call(identity,task_id,'reporter.run',args)
                ref=result.get('report_run_ref') or result.get('document') or {}
                report_id=ref.get('report_run_id') or result.get('report_run_id')
                if report_id:
                    return self.export(identity,task_id,report_id,request['format'])
        if request['template']=='quote':
            raise ValidationError('尚无可验证的报价结果，不能生成报价表。请提供报价来源或先完成估值。')
        text=str(response.get('text') or '').strip()
        if not text:
            raise ValidationError('报告内容尚未生成，请重试本次报告请求')
        return self.create_document(identity,task_id,{'content':text,'template':request['template'],'format':request['format']},request_id=request_id)
