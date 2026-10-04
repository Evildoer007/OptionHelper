"""Host-neutral research presets; no model, compute engine or lifecycle ownership."""
from copy import deepcopy
import json
from pathlib import Path
import re

_ROOT=Path(__file__).with_name('research_profiles')
_DEFINITION=json.loads((_ROOT/'presets.json').read_text(encoding='utf-8'))

def preset_definitions():
    return deepcopy(_DEFINITION['presets'])

def depth_policy(depth='standard'):
    if not isinstance(depth, str) or depth not in _DEFINITION['depths']:
        raise ValueError('研究深度必须为quick、standard或deep')
    return deepcopy(_DEFINITION['depths'][depth])

def role_tools(preset):
    return {key:tuple(value) for key,value in preset_definitions()[preset]['tools'].items()}

def load_role_instructions(preset):
    roles=preset_definitions()[preset]['roles']
    return {role:(_ROOT/preset/role/'AGENT.md').read_text(encoding='utf-8').strip() for role in roles}

def research_depth_from_text(text, default='standard'):
    # Only explicit research-depth phrases select a tier; '深度报告' does not.
    selected=default
    matches=[]
    for marker,depth in [('快速研究','quick'),('标准研究','standard'),('深入研究','deep')]:
        position=str(text).rfind(marker)
        if position>=0:matches.append((position,depth))
    if matches:selected=max(matches)[1]
    depth_policy(selected)
    return selected

# Host-only execution metadata. It never becomes an engine parameter.
from contextlib import contextmanager
from contextvars import ContextVar
_RESEARCH_EXECUTION=ContextVar('optionhelper_research_execution',default=None)

def current_research_execution():
    return deepcopy(_RESEARCH_EXECUTION.get())

@contextmanager
def research_execution(metadata):
    token=_RESEARCH_EXECUTION.set(deepcopy(metadata))
    try:yield
    finally:_RESEARCH_EXECUTION.reset(token)


def workflow_limits(preset, depth='standard'):
    """Translate the selected preset's research depth into scheduler bounds."""
    definition=preset_definitions()[preset]
    control=definition['depth_control']
    if control is None:
        return {'rounds':1,'reworks':0,'role_turns':definition['default_role_budget']}
    policy=depth_policy(depth)
    rounds=policy[control] if control=='product_iterations' else 1+policy[control]
    return {'rounds':rounds,'reworks':rounds-1,'role_turns':policy['role_turns']}


def blocked_research_modules(text):
    """Recognize explicit execution restrictions before any role can start tools."""
    value = str(text).lower()
    all_modules = {'payoffer', 'pricer', 'backtester'}
    negative = r'(?:不需要|不要|不得|禁止|无需|不用|先别|别|不)(?:再|先|实际)?'
    chinese_action = r'(?:做|进行|执行|启动|生成)?(?:任何|实际|金融|数值)?'
    english_negative = r"(?:do not|don't|no|without)\s+(?:(?:perform|run|start)\s+)?(?:any\s+)?"
    topics = {
        'pricer': r'(?:定价|估值|pricing)',
        'backtester': r'(?:回测|历史回放|backtest(?:ing)?)',
        'payoffer': r'(?:收益分析|收益计算|payoff)',
        'report': r'(?:报告|reports?)',
        'calculation': r'(?:计算|calculations?|computations?|computing)',
    }
    topic = '(?:' + '|'.join(topics.values()) + ')'
    action = rf'(?:{chinese_action}|(?:perform|run|start|generate)\s+)?{topic}'
    connector = r'\s*(?:和|或|与|、|及|/|and|or)\s*'
    restriction = re.compile(rf'(?:{negative}|{english_negative})\s*{action}(?:{connector}{action})*')
    declined_text = ' '.join(match.group() for match in restriction.finditer(value))
    affirmative_text = restriction.sub('', value)
    named_topics = '|'.join(rf'(?P<{name}>{pattern})' for name, pattern in topics.items())
    declined = {match.lastgroup for match in re.finditer(named_topics, declined_text)}
    if 'calculation' in declined:
        return all_modules
    if re.search(r'(?:只|仅)(?:做|要|需)?(?:结构(?:比较|对比|筛选|推荐)|(?:比较|对比|筛选|推荐)结构|(?:比较|对比)(?=$|[，。；;!?！？\s]|[一二三四五六七八九十两\d]))', value):
        return all_modules
    # A selection mandate declining pricing and delivery requests catalog
    # comparison only, unless it expressly asks for another financial calculation.
    from .workflow_policy import requests_structure_selection
    financial_action = '|'.join(topics[name] for name in all_modules)
    selection_requested = requests_structure_selection(affirmative_text) or bool(
        re.search(r'(?:筛选|挑选|选出)\s*[一二三四五六七八九十两\d]+\s*(?:种|个)\s*$', affirmative_text)
    )
    if {'pricer', 'report'} <= declined and selection_requested and not re.search(financial_action, affirmative_text):
        return all_modules
    return declined.intersection(all_modules)

_RESEARCH_CALCULATION_START=ContextVar('optionhelper_research_calculation_start', default=None)

@contextmanager
def research_calculation_scope(on_start):
    token=_RESEARCH_CALCULATION_START.set(on_start)
    try:
        yield
    finally:
        _RESEARCH_CALCULATION_START.reset(token)

def notify_research_calculation_started(module):
    callback=_RESEARCH_CALCULATION_START.get()
    if callback is not None:
        callback(module)
