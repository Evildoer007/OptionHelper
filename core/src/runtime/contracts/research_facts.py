"""Contract-backed research facts, independent of model-written commentary.

Only registered, verified economics are projected. Position scenarios reuse the
canonical settlement interpreter and require an explicit matching user holding.
They describe research exposure, not executable exchange contract quantities.
"""
from math import isfinite
from collections.abc import Mapping

from .contract_engine import (
    ResolvedContract, PricePath, evaluate_contract, contract_loss_bound,
    verify_current_product_rule, load_registry,
)

CONTEXT_FIELDS = frozenset({'reference_notional', 'holding_notional', 'underlying', 'market_view'})


def research_context(constraints):
    """Copy only explicit, typed user context; never parse model prose."""
    if not isinstance(constraints, Mapping):
        return {}
    result = {key: constraints[key] for key in ('underlying', 'market_view')
              if isinstance(constraints.get(key), str) and constraints[key].strip()}
    for key in ('reference_notional', 'holding_notional'):
        if key in constraints:
            number = constraints[key]
            if type(number) not in (int, float) or not isfinite(number) or number <= 0:
                raise ValueError(f'{key} must be a finite positive user amount')
            result[key] = float(number)
    if 'holding_notional' in result and result.get('reference_notional') != result['holding_notional']:
        raise ValueError('A holding must use its own amount as the reference notional')
    return result


def contract_research_facts(contract: ResolvedContract, context=None):
    """Prove facts against the current registry and frozen contract snapshot."""
    verify_current_product_rule(contract, load_registry())
    terms = contract.terms
    facts = {'product_id': contract.product_id, 'rule_revision': contract.identity['rule_revision'],
             'margin_call': terms.get('margin_call'), 'loss_bound': contract_loss_bound(contract),
             'context': research_context(context)}
    if terms.get('S0') != 100 or len(contract.underlyings) != 1:
        return facts
    product = contract.product_id
    if product in {'1.1', '1.2'}:
        quantity = terms['n_C' if product == '1.1' else 'n_P']
        facts['legs'] = [{'kind': 'call' if product == '1.1' else 'put',
                         'quantity': quantity, 'strike': terms['K']}]
        facts['initial_cost_ratio'] = quantity * terms['Pi_0'] / 100
    elif product == '2.1':
        facts['legs'] = [{'kind': 'call', 'quantity': 1, 'strike': terms['K1']},
                         {'kind': 'call', 'quantity': -1, 'strike': terms['K2']}]
        facts['initial_cost_ratio'] = terms['P_net'] / 100
        facts['maximum_net_return'] = (terms['K2'] - terms['K1'] - terms['P_net']) / 100
    else:
        return facts
    facts['long_only_legs'] = all(leg['quantity'] > 0 for leg in facts['legs'])
    ctx = facts['context']
    if ctx.get('underlying') != contract.underlyings[0] or contract.currency != 'CNY':
        return facts
    base = ctx.get('reference_notional')
    if base is not None:
        facts['initial_cost_amount'] = base * facts['initial_cost_ratio']
    holding = ctx.get('holding_notional')
    if product == '1.2' and holding is not None and quantity > 0:
        reference = contract.identity['reference_prices'][contract.underlyings[0]]
        scenarios = []
        for change in (-0.1, -0.2, -0.3, -1.0):
            path = PricePath.from_values([reference, reference*(1+change)],
                                         times=[0, terms['T']], asset_ids=contract.underlyings)
            option_return = evaluate_contract(contract, path).pnl / 100
            total_return = change + option_return
            scenarios.append({'underlying_return': change, 'option_net_return': option_return,
                              'position_net_return': total_return, 'position_change': total_return*holding})
        facts['protection'] = {'holding': holding, 'exposure_multiple': quantity,
                               'covered_exposure': quantity*holding,
                               'uncovered_exposure': max(0, 1-quantity)*holding,
                               'excess_exposure': max(0, quantity-1)*holding,
                               'activation_return': terms['K']/100-1, 'scenarios': scenarios}
    return facts


def report_research_projection(facts):
    """Reader text from proven facts. Selection rank remains upstream-owned."""
    def number(value):
        return f'{value:,.2f}'.rstrip('0').rstrip('.').replace('-', '−')
    def percent(value):
        return number(100*value)+'%'
    reason, risks, suitable, unsuitable = [], [], [], []
    legs = facts.get('legs', [])
    if legs:
        descriptions = []
        for leg in legs:
            side = '买入' if leg['quantity'] > 0 else '卖出'
            kind = '看涨期权' if leg['kind'] == 'call' else '看跌期权'
            descriptions.append(f"{side}{number(abs(leg['quantity']))}倍{kind}，行权价为期初价格的{percent(leg['strike']/100)}")
        reason.append('；'.join(descriptions)+'。')
        risks.append('上述倍数表示研究合同的相对敞口，不能直接当作市场合约张数；实际交易还需核对合约乘数、期限及流动性。')
    if facts.get('product_id') == '2.1' and legs:
        suitable.append('适用于接受收益封顶的看涨判断。')
        unsuitable.append('不适用于要求仅含买入腿，或希望完整参与大幅上涨的需求。')
        risks.append(f"净收益上限为{percent(facts['maximum_net_return'])}；高行权价是合同封顶边界，不能据此认定市场压力位。")
    elif facts.get('product_id') == '1.1' and legs:
        suitable.append('适用于接受期权费损失、希望参与上涨的需求。')
        risks.append('标的上涨不等于合同盈利；到期收益仍需覆盖已支付的期权费。')
    elif facts.get('product_id') == '1.2' and legs:
        suitable.append('适用于承担保护成本、对标的下跌进行对冲的需求。')
        risks.append('期权费损失上限仅适用于期权合同，不是持仓加期权组合的亏损上限。')
        if not facts.get('protection'):
            risks.append('缺少与标的对应的明确持仓金额，未计算实际持仓的覆盖金额与组合损益。')
    has_short_leg = any(leg['quantity'] < 0 for leg in legs)
    if has_short_leg:
        # The frozen contract proves expiry economics, not broker margin or
        # executable paired fills. Never infer those from margin_call=False.
        if facts.get('margin_call') is False:
            risks.append('合同约定不追加保证金，不代表场内卖出腿免收保证金或不会追保；实际保证金、组合抵扣和资金占用须核实交易场所及经纪商规则，未核实前不能认定满足不追加保证金的实盘要求。')
        else:
            risks.append('本结构包含卖出腿；实际保证金、组合抵扣、资金占用和追保安排须核实交易场所及经纪商规则。')
        risks.append('组合到期损益边界依赖各腿按约定共同持有。拆腿、单腿提前平仓或不同步成交会改变敞口，须重新评估最大亏损与保证金；成交价差、流动性不足和提前退出成本未纳入测算。')
    if facts.get('margin_call') is False:
        reason.append('合同条款不要求追加保证金；该设置与是否包含卖出腿分别判断。')
    bound = facts.get('loss_bound')
    if bound is not None:
        risks.append(f"合同结算亏损上限为参考规模的{percent(bound['value'])}，不包含合同外费用、信用或流动性损失。")
    if 'initial_cost_amount' in facts:
        reason.append(f"按用户确认的参考规模折算，期初净支出为{number(facts['initial_cost_amount']/10000)}万元。")
    protection = facts.get('protection')
    if protection:
        reason.append(f"持仓{number(protection['holding']/10000)}万元，保护敞口{number(protection['covered_exposure']/10000)}万元，未覆盖敞口{number(protection['uncovered_exposure']/10000)}万元。")
        if protection['excess_exposure']:
            risks.append(f"保护敞口超过持仓{number(protection['excess_exposure']/10000)}万元，额外部分构成方向性期权敞口。")
        for row in protection['scenarios']:
            risks.append(f"标的到期下跌{percent(-row['underlying_return'])}时，持仓与期权合计净损益为{number(row['position_change']/10000)}万元，净收益率为{percent(row['position_net_return'])}。")
        risks.append('以上情景按同一标的持仓、到期现金结算和静态持有计算，未计融资、交易费用及持仓分红。')
    if not reason:
        reason.append('本方案的收益与风险以已确认合同条款及下列计算结果为依据。')
    return {'reason': ''.join(reason), 'suitable_for': suitable, 'not_suitable_for': unsuitable,
            'main_risks': risks}
