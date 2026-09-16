"""Task-scoped image delivery for completed Payoffer runs, without Reporter."""
from collections.abc import Mapping
import re
from urllib.parse import quote

_ID = re.compile(r'[A-Za-z0-9._:-]{1,160}\Z')

def payoff_image_block(task_id, run_id):
    if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (task_id, run_id)):
        return None
    path = f'/api/tasks/{quote(task_id, safe="")}/payoff-images/{quote(run_id, safe="")}.svg'
    return {'type': 'payoff-image', 'task_id': task_id, 'run_id': run_id,
            'title': '损益图', 'preview_url': path, 'download_url': path+'?download=1'}

def image_from_result(task_id, raw):
    if not isinstance(raw, Mapping) or raw.get('status') != 'succeeded' or raw.get('ok') is False:
        return None
    ref = raw.get('module_run_ref')
    if not isinstance(ref, Mapping) or ref.get('module') != 'payoffer' or ref.get('task_id') != task_id:
        return None
    return payoff_image_block(task_id, ref.get('run_id'))


def requests_payoff_image_only(message):
    """Recognise a scoped image request, preserving affirmative report requests."""
    text = str(message or '')
    if not re.search(r'损益图|收益图|payoff\s*(?:图|chart|diagram)', text, re.I):
        return False
    if not re.search(r'只|单独|单单|仅|only|just', text, re.I):
        return False
    clauses = re.split(r'[，,。；;\n]|但是|但|而是', text)
    for clause in clauses:
        if re.search(r'报告|简报|\b(?:html|pdf|report|card)\b', clause, re.I):
            if not re.search(r'不要|不用|无需|不需要|不生成|no\s|without', clause, re.I):
                return False
    return True
