"""Host-owned report delivery: a document, not a model's claim to have made one."""
from __future__ import annotations

from datetime import date
import re
from typing import Mapping
from uuid import uuid5, NAMESPACE_URL

from modules.designer import render
from modules.designer.models import DesignerInput
from modules.designer.config import DesignerConfig
from modules.designer.template_definition import load_template_definition
from .errors import ValidationError

_TEMPLATE = re.compile(r'(?<![A-Za-z])(multireport|multicard|report|card|quote)(?![A-Za-z])', re.I)

def delivery_request(text):
    text = str(text or '').strip()
    if re.search(r'源码|源代码|代码示例|不要.{0,8}(报告|生成)|为什么|为何|怎么用|区别', text):
        return None
    templates = list(dict.fromkeys(match.group(1).lower() for match in _TEMPLATE.finditer(text)))
    template_match = _TEMPLATE.search(text)
    file_word = re.search(r'报告|简报|报价表|html|pdf|word|docx', text, re.I)
    action = re.search(r'生成|导出|下载|给我|给一|再给|改成|转成|整理成|输出|制作|做一', text)
    if (not file_word and not template_match) or (not action and text.lower() not in {'card','report','quote','multicard','multireport'}):
        return None
    template = template_match.group(1).lower() if template_match else ('quote' if '报价' in text else 'card' if '简报' in text else 'report')
    if len(templates) <= 1 and re.search(r'多(产品|结构)', text) and template in {'card','report'}:
        template = 'multi' + template
    formats = list(re.finditer(r'html|pdf|word|docx', text, re.I))
    fmt = formats[-1].group().lower() if formats else 'html'
    return {'template': template, 'templates': templates if len(templates) > 1 else [template],
            'multiple': len(templates) > 1, 'format': 'docx' if fmt == 'word' else fmt,
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



def required_delivery_modules(text, template, *, has_recommendation=False):
    """One coverage policy for tool calls, model fallback and draft admission."""
    explicit = requested_analysis_modules(text)
    if template == 'quote':
        return tuple(module for module in ('payoffer', 'pricer', 'backtester')
                     if module in explicit or module == 'pricer')
    draft = bool(re.search(r'(?<!不要)(?<!不生成)草稿|只(?:需|要).{0,8}(?:整理|文字)|(?:不|无需|不用|暂不)(?:需要|进行|执行|做)?计算', str(text)))
    if has_recommendation and template in {'card', 'report', 'multicard', 'multireport'} and not draft:
        return ('payoffer', 'pricer', 'backtester')
    return explicit


def draft_sections(content, title):
    """Project document blocks into Designer nodes without flattening Markdown.

    Titles, paragraphs and pipe tables remain separate. Raw markup is never
    rendered; Designer owns escaping, table layout and mathematical notation.
    """
    def plain(value):
        value = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), value)
        value = re.sub(r"`([^`]+)`", r"\1", value)
        return value.strip()

    sections = []
    current = {"title": "研究摘要", "content": []}
    paragraph = []

    def flush():
        if paragraph:
            current["content"].append({"type": "paragraph", "text": plain(" ".join(paragraph))})
            paragraph.clear()

    def finish():
        flush()
        if current["content"]:
            sections.append({"id": f"research-{len(sections)+1}", **current})

    lines = content.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            finish()
            heading_text = plain(heading.group(2))
            # The document shell already owns the single top-level title.
            current = {"title": "研究摘要" if len(heading.group(1)) == 1 or heading_text == title else heading_text, "content": []}
        elif index+1 < len(lines) and "|" in line and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index+1]):
            flush()
            cells = lambda row: [plain(cell) for cell in row.strip().strip("|").split("|")]
            headers = cells(line)
            columns = [{"key": f"column-{n}", "label": label} for n, label in enumerate(headers)]
            rows = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                values = cells(lines[index])
                rows.append({column["key"]: values[n] if n < len(values) else "" for n, column in enumerate(columns)})
                index += 1
            current["content"].append({"type": "table", "columns": columns, "rows": rows})
            continue
        elif not line or re.fullmatch(r"[-*_]{3,}", line):
            flush()
        elif re.match(r"^(?:[-*+] |\d+[.)] )", line):
            flush()
            current["content"].append({"type": "paragraph", "text": plain(re.sub(r"^[-*+]\s+", "• ", line))})
        else:
            paragraph.append(re.sub(r"^>\s?", "", line))
        index += 1
    finish()
    return sections


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

    def _with_export(self, identity, task_id, report_id, fmt, response):
        exported = self.export(identity, task_id, report_id, fmt)
        blocks = list(response.get('_assistant_blocks') or [])
        if not blocks and str(response.get('text') or '').strip():
            blocks.append({'type': 'text', 'text': response['text']})
        blocks = [block for block in blocks if not (block.get('type') == 'document' and block.get('report_run_id') == report_id)]
        blocks.extend(exported.get('_assistant_blocks') or [])
        status = response.get('status') or 'completed'
        if status == 'completed' and exported.get('status') != 'completed':
            status = exported['status']
        return {**response, 'status': status, '_assistant_blocks': blocks,
                'document': exported['document']}

    def attach_documents(self, identity, task_id, response, report_ids):
        """Expose every report created this turn, including short follow-up requests.

        Resolve ownership and source HTML through the editor, never model URLs.
        Attaching a card neither rewrites the report nor changes analysis status.
        """
        blocks = list(response.get('_assistant_blocks') or [])
        if not blocks and str(response.get('text') or '').strip():
            blocks.append({'type': 'text', 'text': response['text']})
        existing = {block.get('report_run_id') for block in blocks if block.get('type') == 'document'}
        for report_id in dict.fromkeys(report_ids):
            if report_id in existing:
                continue
            document = self.editor.editor_payload(identity, report_id)
            if document['task_id'] != task_id:
                raise ValidationError('报告不属于当前对话')
            from urllib.parse import quote
            edited = self.results.get_report_document(identity, report_id)
            source = 'document-artifacts/report.html' if edited else 'artifacts/' + quote(document['source_artifact_name'], safe='/')
            preview = f'/api/reports/{quote(report_id, safe="")}/{source}'
            blocks.append({'type': 'document', 'report_run_id': report_id,
                           'title': document['title'], 'format': 'html',
                           'preview_url': preview, 'download_url': preview + '?download=1'})
        return {**response, '_assistant_blocks': blocks}

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
        template = expected['template'] if expected and not expected['multiple'] else str(arguments.get('template') or 'report')
        fmt = expected['format'] if expected else str(arguments.get('format') or 'html').lower()
        if template not in {'card','report','multicard','multireport'} or fmt not in {'html','pdf','docx'}:
            raise ValidationError('请选择研究简报或报告，以及HTML、PDF或Word格式；报价需使用已验证结果')
        pending = self.tasks.pending_recommendation(identity, task_id)
        if latest and required_delivery_modules(latest, template, has_recommendation=bool((pending or {}).get('candidates'))):
            raise ValidationError('推荐型简报和报告需先完成收益分析、定价与回测，请继续推荐交付流程；只有明确要求草稿时才使用对话草稿入口。')
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
                    'recommendation':{'structure_name':label,'reason':'','reason_points':[],'has_recommendation':bool(candidate)},
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
        sections = draft_sections(content, title)
        payload['supplemental_sections'] = sections
        # Draft prose is a document, not a single recommendation-rationale field.
        template_id = template + '-standard'
        definition = load_template_definition(DesignerConfig(), template_id, kind)
        operations = [{'op':'hide_section','section':item.id} for item in definition.sections]
        operations.extend({'op':'add_section','section':section['id']} for section in sections)
        html = render(DesignerInput(payload=payload,output_type=kind,asset_mode='portable',
                      presentation_patch={'schema':'optionhelper.presentation-patch','operations':operations}))['html']
        report_id = 'chat-document-' + uuid5(NAMESPACE_URL, f'{identity.tenant_id}:{identity.principal_id}:{task_id}:{request_id}').hex if request_id else None
        saved = self.editor.import_document(identity,{'task_id':task_id,'title':title,'html':html,'chart_specs':{}},
                                            report_id=report_id, output_type=kind, comparison=template.startswith('multi'))
        return self.export(identity, task_id, saved['report_run_id'], fmt)

    def complete(self, identity, task_id, message, response, *, request_id=None, report_id=None):
        request = delivery_request(message)
        if not request or response.get('status') in {'cancelled','timed_out','unavailable','blocked'} or self.tasks.is_cancelled(identity,task_id):
            return response
        if response.get('document') or request['multiple']:
            # Each requested template is produced by its own tool call. The
            # conversation service attaches all resulting documents together.
            return response
        # Questions and failed/partial analyses are not report content. An existing
        # verified document can still be exported without rerunning its analysis.
        if not report_id and response.get('status', 'completed') != 'completed':
            return response
        report_id = report_id or (self.current_document(identity,task_id) if request['export_only'] else None)
        if report_id:
            return self._with_export(identity, task_id, report_id, request['format'], response)
        pending = self.tasks.pending_recommendation(identity,task_id)
        modules = required_delivery_modules(message, request['template'], has_recommendation=bool((pending or {}).get('candidates')))
        if modules and not self.tools:
            return {'status': 'needs_input', 'text': '本次要求实际计算结果，当前尚不能执行所需分析，未生成草稿。'}
        if self.tools and modules:
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
                    return self._with_export(identity, task_id, report_id, request['format'], response)
        if request['template']=='quote':
            raise ValidationError('尚无可验证的报价结果，不能生成报价表。请提供报价来源或先完成估值。')
        text=str(response.get('text') or '').strip()
        if not text:
            raise ValidationError('报告内容尚未生成，请重试本次报告请求')
        return self.create_document(identity,task_id,{'content':text,'template':request['template'],'format':request['format']},request_id=request_id)
