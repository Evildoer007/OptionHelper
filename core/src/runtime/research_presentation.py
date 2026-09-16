"""Explain existing financial encodings at the model boundary, without conversion.

The registry and the calculator remain authoritative. This projection adds
reading metadata to a copy; it never prepares or edits calculation inputs.
"""
from copy import deepcopy
from collections.abc import Mapping

from runtime.contracts.term_presentation import build_term_fields


MODEL_FINANCIAL_READING_RULE = (
    'financial_value_contract是宿主提供的只读单位说明，不是输出字段或新增金融结果。'
    '不得将其回写到角色result、term_overrides或工具参数。'
    '比较数值前读取其value_encoding和price_convention。normalized_100价格条款'
    '与原始reference_prices处于不同坐标，不能直接比较两者大小来判定输入错误。'
    'absolute_market按其明确的市场价格坐标解释。工具的decimal_ratio是比例小数，'
    '例如0.04表示4%，不是0.04%；已有比例结果无须另取原始指数点位才能解释。'
    '仍须核对证据适用的候选、日期及条款；未知单位应标为未知，不能猜测。'
)


def financial_model_view(value, term_catalog):
    """Return a JSON-compatible copy with catalog-backed quantity definitions."""
    def conventions(node, inherited):
        identity = node.get('identity')
        if isinstance(identity, Mapping):
            return {str(identity.get('price_convention') or 'normalized_100')}
        modules = node.get('module_inputs')
        if isinstance(modules, Mapping):
            found = set()
            for module, inputs in modules.items():
                if module in {'pricer', 'payoffer', 'backtester'} and isinstance(inputs, Mapping):
                    identity = inputs.get('identity', {})
                    if isinstance(identity, Mapping):
                        found.add(str(identity.get('price_convention') or 'normalized_100'))
            if found:
                return found
        if isinstance(node.get('current_inputs'), Mapping):
            return conventions(node['current_inputs'], inherited)
        return inherited

    def visit(node, inherited=frozenset()):
        if isinstance(node, (list, tuple)):
            return [visit(item, inherited) for item in node]
        if not isinstance(node, Mapping):
            return deepcopy(node)
        basis = conventions(node, inherited)
        result = {key: deepcopy(item) if key in {'required_output', 'output_schema'} else visit(item, basis)
                  for key, item in node.items()
                  if key != 'financial_value_contract'}
        reading = {}
        terms = node.get('term_overrides')
        if isinstance(terms, Mapping) and terms:
            fields = build_term_fields(terms, term_catalog)['contract_fields']
            quantities = {}
            for field in fields:
                key = field['key']
                # Output schemas and undeclared symbols are not financial inputs.
                if key not in term_catalog:
                    quantities[key] = {'value': deepcopy(terms[key]), 'value_encoding': 'unknown'}
                    continue
                encoding = field['value_encoding']
                if field['unit'] == 'price':
                    encoding = ('absolute_market_price' if basis == {'absolute_market'} else
                                'normalized_price_s0_100' if basis == {'normalized_100'} else
                                'unresolved_module_price_convention')
                quantities[key] = {
                    'value': deepcopy(terms[key]), 'label': field['label'],
                    'catalog_unit': field['unit'], 'value_encoding': encoding,
                }
                if encoding == 'normalized_price_s0_100':
                    quantities[key]['reference_coordinate'] = 100
                elif encoding == 'percentage_points_internal':
                    quantities[key]['reading'] = '百分数数值；例如13表示13%，与市场volatility_override=0.13编码不同。'
            reading['term_overrides'] = quantities
            reading['price_conventions'] = sorted(basis) or ['unresolved']
            reading['source'] = 'verified_capability.term_catalog + contract identity/default'
            reading['scope'] = '输入单位说明；未声称合同已编译或完成计算'
        references = node.get('reference_prices')
        if isinstance(references, Mapping):
            reading['reference_prices'] = {
                'values': deepcopy(references), 'value_encoding': 'original_reference_price',
                'reading': '合同冻结的逐标的参考坐标；normalized_100条款以参考价格对应100。不得将原始点位与归一化执行价直接比较。',
            }
        facts = node.get('verified_metrics')
        if isinstance(facts, Mapping):
            reading['verified_metrics'] = {
                key: {'unit': fact.get('unit'), 'value_encoding': fact.get('value_encoding', 'unknown'),
                      'reading': '比例小数；百分比显示为value×100%；这已经是比例，无需再除以原始标的点位。'}
                for key, fact in facts.items()
                if isinstance(fact, Mapping) and fact.get('value_encoding') == 'decimal_ratio'
            }
        if reading:
            result['financial_value_contract'] = reading
        return result

    return visit(value)
