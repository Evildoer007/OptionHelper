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
from .errors import UserActionError, ValidationError

_TEMPLATE = re.compile(r'(?<![A-Za-z])(multireport|multicard|report|card|quote)(?![A-Za-z])', re.I)

_NEGATED_DELIVERY = re.compile(
    r'(?:不需要|不必|不要|不用|无需|暂不|先不|不)(?:再|重新)?'
    r'(?:生成|导出|下载|输出|制作|提供|给我|报告|简报|报价表|html|pdf|word|docx)', re.I)

def delivery_request(text):
    text = str(text or '').strip()
    if re.search(r'源码|源代码|代码示例|为什么|为何|怎么用|区别', text):
        return None
    # Exclude negated delivery clauses before detecting actions and formats.
    # A separate affirmative clause still permits e.g. 不要PDF，生成HTML报告.
    text = '，'.join(clause for clause in re.split(r'[，,。；;！!\n]|但是|但|而是', text)
                    if not _NEGATED_DELIVERY.search(clause))
    templates = list(dict.fromkeys(match.group(1).lower() for match in _TEMPLATE.finditer(text)))
    template_match = _TEMPLATE.search(text)
    file_word = re.search(r'报告|简报|报价表|html|pdf|word|docx', text, re.I)
    action = re.search(r'生成|导出|下载|给我|给一|再给|改成|转成|整理成|输出|制作|做一', text)
    if (not file_word and not template_match) or (not action and text.lower() not in {'card','report','quote','multicard','multireport'}):
        return None
    template = template_match.group(1).lower() if template_match else ('quote' if '报价' in text else 'card' if '简报' in text else 'report')
    if len(templates) <= 1 and re.search(r'(?:多|两|双|[2-9])(?:个|款|种)?(?:产品|结构)|(?:产品|结构)(?:横向)?对比', text) and template in {'card','report'}:
        template = 'multi' + template
    formats = list(re.finditer(r'html|pdf|word|docx', text, re.I))
    fmt = formats[-1].group().lower() if formats else 'html'
    return {'template': template, 'templates': templates if len(templates) > 1 else [template],
            'multiple': len(templates) > 1, 'format': 'docx' if fmt == 'word' else fmt,
            'formats': list(dict.fromkeys('docx' if m.group().lower() == 'word' else m.group().lower() for m in formats)) or ['html'],
            'export_only': bool(re.search(r'再给|改成|转成|导出|下载', text)) and not bool(re.search(r'推荐|重新生成', text))}


def requested_analysis_modules(text):
    """Recognize calculations after removing explicitly negated business items.

    A negation applies to its named item/list, not the whole sentence: removing
    “无需回测” must leave “但需要定价” available for the positive check.
    """
    patterns = {'payoffer': r'payoffer|收益分析|收益图',
                'pricer': r'pricer|定价|估值', 'backtester': r'backtest(?:er)?|回测'}
    topic = (r'(?:backtester|backtest|payoffer|pricer|收益分析|收益图|'
             r'推荐|定价|估值|回测|行情|计算|金融判断)'
             r'(?:相关)?(?:结果|报告|模块|计算)?')
    modifier = r'(?:任何|一切|新的|实际|额外|相关|全部|各类|再|先|进行|执行|运行|调用|做|获取|取)*'
    negation = r'(?<!不是)(?<!不能)(?:不(?:涉及|包含|需要|要求|执行|进行|调用|做|用|要)?|无需|无须|不用|暂不)'
    # A comma can continue an enumeration, but a new request such as “需要定价”
    # starts a different item. Explicit postposed actions also end the list.
    item = modifier + topic + r'(?!继续|照常|需要|执行|进行|调用)'
    separator = r'\s*(?:、|，|,|以及|和|与|及|或)?\s*'
    negative_list = negation + r'\s*' + item + r'(?:' + separator + item + r')*'
    affirmative = re.sub(negative_list, '', str(text or '').lower())
    requested = set()
    for clause in re.split(r'[，,。；;\n]', affirmative):
        if re.search(r'(?:各|所有|全部|三[个类])模块.{0,8}结果|完整量化报告|不要草稿', clause):
            requested.update(patterns)
        for module, pattern in patterns.items():
            if re.search(pattern, clause):
                requested.add(module)
    return tuple(module for module in patterns if module in requested)



def existing_results_only(text):
    """Explicitly reuse completed runs without authorizing another calculation."""
    return bool(re.search(
        r'(?:不|不要|无需|不用)(?:再|重新|额外)(?:进行|执行|运行)?计算'
        r'|(?:仅|只)(?:需|要)?(?:使用|复用|采用)已有(?:的)?(?:计算)?结果',
        str(text or '')))


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


def _document_markdown(content):
    """Keep headings and tables from an HTML draft; never execute its markup."""
    if not re.search(r'<(?:html|body|section|h[1-6]|p|table)\b', content, re.I):
        return content
    from bs4 import BeautifulSoup, NavigableString
    soup = BeautifulSoup(content, 'html.parser')
    for node in soup(['script', 'style', 'iframe', 'object', 'embed']):
        node.decompose()
    lines = []
    def visit(node):
        if isinstance(node, NavigableString):
            if str(node).strip(): lines.append(str(node).strip())
            return
        name = node.name or ''
        if name == 'table':
            rows = [[cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'], recursive=False)]
                    for row in node.find_all('tr')]
            rows = [row for row in rows if row]
            if rows:
                lines.append('| ' + ' | '.join(rows[0]) + ' |')
                lines.append('| ' + ' | '.join('---' for _ in rows[0]) + ' |')
                lines.extend('| ' + ' | '.join(row) + ' |' for row in rows[1:])
                lines.append('')
        elif re.fullmatch(r'h[1-6]', name):
            lines.extend(['#' * int(name[1]) + ' ' + node.get_text(' ', strip=True), ''])
        elif name in {'p', 'li', 'blockquote'}:
            lines.extend([('- ' if name == 'li' else '') + node.get_text(' ', strip=True), ''])
        else:
            for child in node.children: visit(child)
    visit(soup.body or soup)
    return '\n'.join(lines)


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
            level = len(heading.group(1))
            heading_text = plain(heading.group(2))
            if level >= 3:
                flush()
                current["content"].append({"type": "heading", "level": level, "text": heading_text})
            else:
                finish()
                # The document shell owns h1; h2 opens a section and keeps all
                # subordinate headings with their body in that same section.
                current = {"title": "研究摘要" if level == 1 or heading_text == title else heading_text, "content": []}
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

    def export_selection(self, identity, task_id, message):
        """Resolve existing files from this task without asking the model.

        A report belongs to its first delivery batch. Later format conversions
        repeat its identity and therefore cannot replace that original batch.
        ``None`` means this request still needs the normal research workflow.
        """
        request = delivery_request(message)
        if not request or not request['export_only'] or requested_analysis_modules(message):
            return None
        text = str(message or '')
        existing_reference = bool(re.search(r'刚才|之前|已有|原有|刚生成|刚.*报告|这[份些两三]|那[份些两三]|上[一次轮份]|保留.*(?:正文|标题|模板)', text))
        if re.search(r'(?:重新|新建|另行)生成|生成.{0,24}(?:并|再|然后|同时)导出', text) and not existing_reference:
            return None
        documents = {}
        batches = []
        last_ids = []
        for row in self.tasks.get(identity, task_id).get('messages', []):
            if row.get('role') not in {None, 'assistant'}:
                continue
            blocks = [block for block in row.get('content_blocks', [])
                      if block.get('type') == 'document' and block.get('report_run_id')]
            ids = list(dict.fromkeys(str(block['report_run_id']) for block in blocks))
            new_ids = [report_id for report_id in ids if report_id not in documents]
            if new_ids:
                batches.append(new_ids)
            for block in blocks:
                documents[str(block['report_run_id'])] = dict(block)
            if ids:
                last_ids = ids
        # Desk deliveries belong to this task even if no Chat card was posted.
        # Keep conversation batches intact; the library supplies named choices.
        title_index = {}
        for report_id, block in documents.items():
            title = str(block.get('title') or '').strip()
            if title:
                title_index.setdefault(title, set()).add(report_id)
        for report in self.results.list_report_runs(identity, task_id):
            report_id = str(report.get('report_run_id') or '').strip()
            title = str(report.get('title') or '').strip()
            if not report_id or not title:
                continue
            documents.setdefault(report_id, dict(report))
            title_index.setdefault(title, set()).add(report_id)
        if not documents:
            if existing_reference:
                raise ValidationError('当前任务没有可转换的已交付报告，请先选择或生成报告。')
            return None

        known_titles = set(title_index)
        # Book-title brackets explicitly name a report. Other quote marks are
        # also used for formats such as “PDF”, so accept only known titles.
        explicit_titles = [title.strip() for title in re.findall(r'《([^》]+)》', text)]
        quoted_titles = [title.strip() for match in re.findall(r'“([^”]+)”|"([^\"]+)"', text)
                         for title in match if title.strip() in known_titles]
        mentioned_titles = sorted((title for title in known_titles if len(title) >= 4 and title in text),
                                  key=text.find)
        named_titles = explicit_titles or quoted_titles or mentioned_titles
        if named_titles:
            selected = []
            for title in dict.fromkeys(named_titles):
                matches = sorted(title_index.get(title, set()))
                if not matches:
                    raise ValidationError(f'当前任务未找到标题为“{title}”的报告，请使用报告卡片上的完整标题。')
                if len(matches) > 1:
                    raise ValidationError(f'当前任务有多份同名报告“{title}”，请从对应报告卡片打开编辑页导出。')
                selected.extend(matches)
        else:
            count_match = re.search(r'([一二两三四五六七八九十]|[1-9][0-9]*)份', text)
            counts = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
            expected_count = (counts.get(count_match.group(1)) or int(count_match.group(1))) if count_match else None
            plural = expected_count not in {None, 1} or bool(re.search(r'两[个种]|全部|所有|各份|这些|都导出|均导出', text)) or request['multiple']
            if not batches:
                if len(documents) != 1 or plural:
                    raise ValidationError('当前任务报告库中有已保存报告，请说明要导出的完整标题；未将独立报告合并为同一批交付。')
                selected = list(documents)
            else:
                selected = list(batches[-1]) if plural else [last_ids[-1]]
            if expected_count is not None and expected_count != len(selected):
                raise ValidationError(f'当前选择有{len(selected)}份报告，与要求的{expected_count}份不一致，请说明完整标题。')
        selected = list(dict.fromkeys(selected))
        # Validate the entire selection before writing even the first export.
        for report_id in selected:
            if self.editor.editor_payload(identity, report_id)['task_id'] != task_id:
                raise ValidationError('报告不属于当前对话')
        return selected

    def export_documents(self, identity, task_id, report_ids, fmt, *, cancelled=None):
        """Convert selected frozen documents, retaining each successful delivery."""
        blocks = []
        documents = []
        notices = []
        incomplete = False
        stopped = False
        for report_id in report_ids:
            if cancelled and cancelled():
                stopped = True
                break
            title = self.editor.editor_payload(identity, report_id)['title']
            try:
                exported = self.export(identity, task_id, report_id, fmt)
            except ValidationError as error:
                incomplete = True
                detail = error.message if isinstance(error, UserActionError) else '导出未完成，请在报告编辑页重试。'
                notices.append(f'{title}：{detail}')
                continue
            incomplete = incomplete or exported.get('status') != 'completed'
            blocks.extend(block for block in exported.get('_assistant_blocks', []) if block.get('type') == 'document')
            if exported.get('document'):
                documents.append(exported['document'])
            label = 'Word' if fmt == 'docx' else fmt.upper()
            notices.append(f'{title}：{label}已导出。' if exported.get('status') == 'completed'
                           else f'{title}：{exported.get("text") or "所选格式暂未导出。"}')
        if stopped:
            notices.append('已停止转换；已完成的文件仍可下载，其余报告未转换。')
        status = 'cancelled' if stopped else 'partial' if incomplete and documents else 'needs_input' if incomplete else 'completed'
        text = '\n'.join(notices)
        response = {'status': status, 'text': text, '_assistant_blocks': [{'type': 'text', 'text': text}, *blocks],
                    'documents': documents}
        if documents:
            response['document'] = documents[-1]
        return response

    def export(self, identity, task_id, report_id, fmt, *, reuse_completed=False):
        document = self.editor.editor_payload(identity, report_id)
        if document['task_id'] != task_id:
            raise ValidationError('报告不属于当前对话')
        request = {'title': document['title'], 'html': document['html'],
                   'chart_specs': document['chart_specs'], 'format': fmt}
        failed_format = None
        saved = None
        if reuse_completed:
            # Only a report created in this conversation turn may use this path.
            # Store writes invalidate prior exports whenever editable content changes.
            current = self.results.get_report_document(identity, report_id)
            if current and all(current.get(key) == document.get(key) for key in ('title', 'html', 'chart_specs')):
                artifact = next((item for item in current.get('artifacts', [])
                                 if item.get('name') == f'report.{fmt}' and item.get('content')), None)
                if artifact:
                    from urllib.parse import quote
                    saved = {'download_url': f'/api/reports/{report_id}/document-artifacts/report.{fmt}?download=1#display_name='
                             + quote(f'{document["title"]}.{fmt}', safe='')}
        try:
            if saved is None:
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

    def _with_export(self, identity, task_id, report_id, fmt, response, *, reuse_completed=False):
        exported = self.export(identity, task_id, report_id, fmt, reuse_completed=reuse_completed)
        blocks = list(response.get('_assistant_blocks') or [])
        if not blocks and str(response.get('text') or '').strip():
            blocks.append({'type': 'text', 'text': response['text']})
        blocks = [block for block in blocks if not (block.get('type') == 'document' and block.get('report_run_id') == report_id)]
        blocks.extend(exported.get('_assistant_blocks') or [])
        status = response.get('status') or 'completed'
        if status == 'completed' and exported.get('status') != 'completed':
            status = exported['status']
        return {**response, 'status': status, '_assistant_blocks': blocks,
                'text': response.get('text') or exported.get('text') or '报告已生成，可预览、编辑或下载。',
                'document': exported['document']}

    def attach_documents(self, identity, task_id, response, report_ids):
        """Expose every report created this turn, including short follow-up requests.

        Resolve ownership and source HTML through the editor, never model URLs.
        Attaching a card neither rewrites the report nor changes analysis status.
        """
        blocks = list(response.get('_assistant_blocks') or [])
        if not blocks and str(response.get('text') or '').strip():
            blocks.append({'type': 'text', 'text': response['text']})
        ordered_ids = list(dict.fromkeys(report_ids))
        existing = {block.get('report_run_id'): block for block in blocks if block.get('type') == 'document'}
        blocks = [block for block in blocks if not (block.get('type') == 'document' and block.get('report_run_id') in ordered_ids)]
        for report_id in ordered_ids:
            if report_id in existing:
                blocks.append(existing[report_id])
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
                           'preview_url': preview, 'download_url': preview + '?download=1#display_name=' + quote(document['title'] + '.html', safe='')})
        return {**response, '_assistant_blocks': blocks}

    def create_document(self, identity, task_id, arguments, *, request_id=None):
        task = self.tasks.get(identity, task_id)
        messages = task.get('messages', [])
        expected = delivery_request(messages[-1].get('content')) if messages and messages[-1].get('role') == 'user' else None
        latest = next((str(row.get('content', '')) for row in reversed(messages) if row.get('role') == 'user'), '')
        if not expected and _NEGATED_DELIVERY.search(latest):
            raise ValidationError('本轮明确不生成报告，请直接回答用户问题。')
        if requested_analysis_modules(latest):
            raise ValidationError('本次要求实际计算结果，请先完成所需分析并从分析结果生成报告，不能改交草稿。')
        allowed = {'title','content','template','format','product_ids'}
        if set(arguments)-allowed:
            raise ValidationError('报告草稿只接受标题、正文、模板、格式和product_ids产品编号列表')
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
        content = _document_markdown(content)
        content = re.sub(r'^```[^\n]*\n|\n```\s*$', '', content).strip()
        default_title = {'card':'结构研究简报','report':'结构研究报告','multicard':'多结构研究简报','multireport':'多结构研究报告'}[template]
        title = str(arguments.get('title') or default_title)[:120]
        candidates = sorted((pending or {}).get('candidates') or [], key=lambda item: (not bool(item.get('is_primary')), item.get('rank') if isinstance(item.get('rank'), (int,float)) else 1000000))
        # Explicit draft identities are catalog references, not recommendations or
        # contracts. Never infer them from generated prose or create pending state.
        explicit_products = 'product_ids' in arguments
        if explicit_products:
            product_ids = arguments['product_ids']
            if (not isinstance(product_ids, list) or not product_ids
                or any(not isinstance(item, str) or not item.strip() for item in product_ids)
                or len(product_ids) != len(set(product_ids))):
                raise UserActionError('draft_product_selection_invalid', 'product_ids必须是非空、不重复的产品编号列表。',
                                      next_step='查询产品目录后填写真实编号；修改产品选择后再试，不要重复原请求或启动计算。')
            resolver = getattr(self.tools, 'draft_product_identities', None)
            if not callable(resolver):
                raise UserActionError('draft_catalog_unavailable', '当前报告草稿无法读取产品目录。',
                                      next_step='保留草稿正文并说明目录不可用，不要通过计算或推荐绕过。')
            candidates = resolver(identity, task_id, product_ids)
        if template.startswith('multi') and len(candidates) < 2:
            raise UserActionError('draft_product_selection_required', '多产品草稿需要至少两个明确的产品编号。',
                                  next_step='用knowledger_search核对用户指定产品，将至少两个编号放入product_ids后重试。已有明确编号时不要重复询问；不需要推荐登记或计算。')
        kind = template.removeprefix('multi')
        def facts(candidate):
            label = str(candidate.get('product_name') or candidate.get('title') or candidate.get('product_id') or title)
            has_recommendation = bool(candidate) and not explicit_products
            return {'schema':'optionhelper.designer-payload','meta':{'title':title,'as_of_date':date.today().isoformat()},
                    'sections':['conclusion','recommendation','parameters','payoff','pricing','backtest','risk'],
                    'conclusion':{'structure_name':label,'has_recommendation':has_recommendation,'reasons':[]},
                    'recommendation':{'structure_name':label,'reason':'','reason_points':[],'has_recommendation':has_recommendation},
                    'parameters':{},'payoff':{'status':'not_run'},'pricing':{'status':'not_run'},'backtest':{'status':'not_run'},
                    'risk':{'items':['本草稿根据当前对话整理，未完成的定价和回测不构成计算结论。']}}
        payload = facts(candidates[0] if candidates else {})
        if template.startswith('multi'):
            payload['comparison']={'delivery_mode':'comparison','candidates':[
                {'label':f'方案{index+1}','rank':index+1,'is_primary':index==0 and not explicit_products,
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
        created_this_turn = bool(report_id)
        report_id = report_id or (self.current_document(identity,task_id) if request['export_only'] else None)
        if report_id:
            return self._with_export(identity, task_id, report_id, request['format'], response, reuse_completed=created_this_turn)
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
