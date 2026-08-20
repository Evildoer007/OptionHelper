# Payoffer模块指南

## 职责

读取`ResolvedContract`和同一ProductVersion的只读默认视觉JSON，调用共享现金流解释器与确定性SVG渲染器，生成本次参数化收益图。

## 输入与输出

```text
PayoffInput = {ResolvedContract}
```

正式运行只接受Core的`ResolvedContract.to_protocol_dict()`完整快照，并要求Host的`contract_ref.schema_id`严格等于`optionhelper.resolved-contract`。不接受其他Schema引用或页面裁剪投影。

输出路径面板、定义域、开闭端点、关键阈值和SVG，写入安装目录之外的项目Store中的`$OPTIONHELPER_RESULT_ROOT/output_payoff/{task_id}/{run_id}/`。不调用Pricer或Backtester。

输出还包含Schema为`optionhelper.reporter-payoff-facts`的`reporter_payoff_facts`。该对象只提供共享解释器已核对的分段收益范围、开闭端点、受控价格符号阈值和百分比单位，供Reporter在验证同一ModuleRun后消费；它不含HTML、名义本金、币种、内部N或内部100口径，也不替代Reporter的来源校验。

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

## 固定资产

默认JSON和SVG是资料库资产。运行时只可读取，并用ResolvedContract覆盖本次合法条款；不得修改默认图、默认JSON、OptionReg或OptionLib。资料库维护仅由管理员受控流程执行。

## 交互边界

- Payoffer只依赖已冻结合同，不调用行情数据或读取iFind凭据；单独运行只检查选定Python、依赖、Store与合同，不要求iFind。
- 未明确选择产品时不默认载入任何特定产品；先依据已确认候选或合同运行。
- 条款已由ResolvedContract冻结后直接生成收益结构，不重复询问标的、期限或交付格式。
- 单独调用时向用户说明收益机制、关键情景、边界和风险；原始JSON仅在用户明确要求或系统集成时展示。
- 用户明确变更期限、行权价、参与率或其他条款并要求重算时，Host将变更编译为新的`ResolvedContract`后直接运行；不改写此前收益图或此前结果。
- 不自动调用Pricer、Backtester或Reporter。报告中的收益图由Reporter和Designer采用报告版式重新表现；资料库默认示例图保持不变。
- 独立`payoffer.html`是开发预览页。其本地Store结果仅供本机核对，不能直接生成正式Card或Report；正式交付必须由App Host提交受控ModuleRun，再由Reporter显式选择并验证。
- 页面明确分为“默认示例”和“本次运行”。默认示例直接读取固定SVG并保持只读；切换到本次运行后，条款修改只生成当前ResolvedContract结果，不会写回默认资产。
- 默认资产更新只在Desk管理员显式启用的高级维护进程中出现。管理员先填写维护原因并检查OptionReg差异，再输入审批号和指定确认文字完成二次确认；发布过程归档旧JSON/SVG并支持失败回滚。页面临时参数不能直接成为资料库默认值。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；结构解析和图形生成明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。

组合分析或正式交付中由顶层工作流统一发进度，本模块不重复发送以下文案。

- 开始：我先核对已确认条款，再计算关键收益区间和边界情景。
- 进度：收益结构已经解析，正在核对阈值、端点和图形表达。
- 完成：收益结构完成。下面说明关键条款、主要情景、收益边界和核心风险。
- 失败：本次收益结构无法可靠生成。我会说明冲突或缺失条款，不用示例图或默认数值冒充本次结果。
