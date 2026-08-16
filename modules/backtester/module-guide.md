# Backtester模块指南

## 1.职责

Backtester按每个入场日冻结`HistoricalResolvedContract`，调用共享合同解释器回放同一条款与现金流，先形成逐笔账本，再聚合公共指标和结构专属指标。不生成净值曲线，不调用Pricer或Payoffer。

正式职责仅分为四处：`entry_generator.py`生成入场样本，`path_replay.py`对齐价格并回放，`trade_ledger.py`冻结逐笔合同与账本，`common_metrics.py`从账本聚合通用指标；`metric_profiles.py`只保留产品独特指标。

## 2.正式输入与输出

`BacktestInput={ResolvedContract,BacktestConfig,HistoricalData}`。

`HistoricalData`保存交易日历史表及其`DataAssetRef`。合同观察、障碍判断与现金流结算只使用不复权`close`；本地CSV至少包含`date`、`asset_id`、`close`，需要入场HV特征或分组时另需前复权`adj_close`。`DataAssetRef`分别记录合同结算与HV字段及复权口径；`close=adj_close`仅在资产来源明确声明时允许。DataFetcher来源由外层同时注入ModulePort和共享`DataStorePort`读取已验证的`DataAssetRef`；模块不导入DataFetcher内部实现，也不猜测其存储路径。

输出`BacktestResult`包含：

- 公共统计、显式MetricProfile和结构专属指标
- 完整逐笔账本、现金流审计、事件、合同毛收益率百分比、数据标记与限制
- 合同、逐笔合同、数据和账本指纹
- 合同声明、样本已观察及未观察的path/case覆盖证据；单路径烟测不得表述为全分支覆盖

服务正式返回顶层`data_refs`和`module_run_ref`。`data_refs`必须通过Core`DataAssetRef`校验，`storage_ref`只能是受控opaque引用，禁止物理路径。成功结果由Core`LocalResultStore`原子提交，正式运行目录包含输入快照、合同、数据引用、限制、结果、JSON/CSV业务产物，以及Store生成的产物哈希清单和提交标记。合同、数据读取、配置校验或零有效样本在Host完成合同绑定之后失败时，同样提交`failed`ModuleRun，包含输入快照、数据引用、限制、`error.json`和提交标记，不伪造结果。

`data_request`仅通过构造参数注入的`historical_data_port`获取引用，并通过注入的`data_store.read_bytes`读取opaque资产。未注入端口时返回带`error_code`、`stage`、`retryable`和`missing_inputs`的结构化失败；App侧端口装配不属于Backtester职责。

## 3.口径与拒绝规则

现金流时间按实际自然日ACT/365计算；交易日窗口、观察计数和波动率年化不替代该口径。`complete_tenor=true`必须使用DataAssetRef原始声明的严格递增交易日sessions及日历覆盖终点，到期日在周末或节假日时只取该受控日历中到期日前最后一个session；缺少权威日历、覆盖不到期日或任一标的缺session均跳过或拒绝，绝不用固定自然日容差。多标的只按共同交易日对齐，缺标的不复用单标的价格。缺观察、零有效样本及未被共享合同接口表达的语义均明确拒绝。

公开逐笔和汇总结果统一使用合同毛收益率百分比。内部无量纲基准仅用于将共享解释器的合同现金流归一化，页面和正式配置不暴露名义本金、保证金或期权费分母。胜率为正合同毛收益样本数除以有效收益样本数，并同时返回分子和分母。原始现金流金额仅作非公开审计产物；期权费、资金成本、费用、税费、对冲和滑点未建模时，`client_net_return`与`client_net_pnl`均明确为`not_modelled`。

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

入场HV仅使用入场日及以前`adj_close`对数收益，禁止使用未来价格。回测输出保留合同结算字段、HV字段、HV窗口与每笔入场特征，供审计复算。

## 交互边界

- 运行前先复用当前任务覆盖本次标的、区间、字段、复权口径和交易日历的DataAssetRef。统一就绪门禁已确认iFind配置；无法复用且需要新数据时，按DataFetcher指南取得数据。运行时数据能力不可用则保留回测条件并停止，不猜测历史数据或无提示改用本地CSV。
- 历史数据由Host按当前任务的DataAssetRef注入；不得要求普通用户提供CSV路径、Store位置或运行目录。独立页面只可查看产品目录和输入，不执行回测；正式回测必须由OptionHelper App Host绑定ResolvedContract、DataAssetRef、DataStore和ResultStore。
- 回测区间、入场频率和观察口径能从已确认任务推导时直接使用；多个关键缺口一次列出，不逐项追问。
- 单独调用直接给出样本范围、收益与风险统计、结构专属指标和限制，不自动生成报告。原始JSON或逐笔账本仅在用户明确要求时展示。
- 用户明确变更期限、区间、入场频率或回测规则并要求重跑时，Host据此签发新的历史合同和回测运行；绝不修改既有账本或以旧结果冒充新口径。
- 运行数据、账本和结果只使用安装目录之外的项目Store；模块不接受或暴露物理路径。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；路径回放和统计明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。

组合分析或正式交付中由顶层工作流统一发进度，本模块不重复发送以下文案。

- 开始：我先核对回测区间、入场规则和数据口径，再按历史路径逐笔回放。
- 进度：历史路径已经回放，正在汇总收益、风险和结构专属指标。
- 完成：回测完成。下面给出样本范围、核心统计、主要风险和结果限制。
- 失败：本次回测无法形成可靠结果。我会说明数据、合同或样本条件缺口，不用单一路径代替完整回测。
