"""Shared provider-independent contract for fixed recommendation roles."""

BACKTEST_WINDOW_RULE = '回测入场区间已有工具默认规则：用户未指定时省略backtest_config.start_date和end_date，先交Backtester按当前合同、实际行情和交易日历规划最近约三年的可完整回放入场窗口，不为日期留空而追问，也不把今天、行情截止日或产品到期日填成入场截止日。各预设均遵守这一规则；换产品或期限后重新规划自动窗口。用户明确指定的区间和入场日须保留；若无法执行，说明具体原因和工具验证过的建议，再询问是否调整。数据或日历不足先尝试补齐，不把数据错误说成用户必须改日期。'

def recommender_step_instruction() -> str:
    return BACKTEST_WINDOW_RULE + (
        "你正在执行OptionHelper的recommender_fixed_step。只返回一个JSON对象，不要Markdown、解释或推理。"
        "固定格式为{\"action\":\"final\",\"result\":{...}}，result必须且只能满足输入required_output声明的结构。"
        "Interpreter和Framer只能提取用户已明确表达的事实，不能把已有confirmed_constraints列为缺失；"
        "Selector、Structurer、Matcher、Hedger和Generator只能从输入evidence生成可追溯候选；"
        "Trader和Evaluator只能通过已授权工具取得FactRef；Reviewer、Moderator和Ranker不得改写金融事实。"
        "不得输出reasoning、analysis、secret、token、文件路径或内部运行引用。"
    )

