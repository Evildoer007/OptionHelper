"""Host-neutral research presets; no model, compute engine or lifecycle ownership."""
from copy import deepcopy
import json
from pathlib import Path

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
