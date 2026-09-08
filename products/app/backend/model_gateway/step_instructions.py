"""Shared provider-independent contract for fixed recommendation roles."""

def recommender_step_instruction() -> str:
    return (
        "你正在执行OptionHelper的recommender_fixed_step。只返回一个JSON对象，不要Markdown、解释或推理。"
        "固定格式为{\"action\":\"final\",\"result\":{...}}，result必须且只能满足输入required_output声明的结构。"
        "Interpreter和Framer只能提取用户已明确表达的事实，不能把已有confirmed_constraints列为缺失；"
        "Selector、Structurer、Matcher、Hedger和Generator只能从输入evidence生成可追溯候选；"
        "Trader和Evaluator只能通过已授权工具取得FactRef；Reviewer、Moderator和Ranker不得改写金融事实。"
        "不得输出reasoning、analysis、secret、token、文件路径或内部运行引用。"
    )

