# Recommender管理规范

Recommender拥有自然语言路由、推荐状态机、四个Agent步骤、候选证据门禁、冲突聚合、审批门禁和模块调度审计；不拥有产品事实、模型Provider、合同解析、市场数据或金融计算。

## 固定规则

- `RecommendationSet`唯一Schema为`optionhelper.recommendation-set`。
- Research候选必须引用同一`catalog_version`下至少一条OptionLib章节证据，证据原文哈希必须匹配。
- OptionList、OptionLib、OptionReg证据状态不是`ready`时，候选不得运行计算模块。
- Critic必须逐一审阅Research产品，新增、漏审、重复审阅均失败。
- 主候选与备选最多3个，排名从1连续递增，标的顺序不可改变。
- 未批准候选不得运行模块；已批准候选还必须提供产品和标的顺序一致的`CandidateContract`。
- Executor只发送共享ToolSchema字段，每个候选每个模块最多调用一次；所有Tool请求和真实返回均写入审计哈希。
- 自由流程按路由使用最小工具权限；相同模块与请求在单次运行内不得重复执行。
- 模型或端口失败不得生成候选、计算值或成功状态。模块不可用保留`unsupported/failed`及原因。
- Reporter是RecommendationSet的下游消费者，不在固定推荐流程的Executor内调用；Recommender不导入Reporter，Designer不由Recommender直接调用。

## 回滚与升级

提示模板、路由和聚合策略可按版本回滚，但不得改写历史RecommendationSet。产品证据冲突交Knowledger，CandidateContract或正式Port缺口交共享Core与主任务处理，不得在本模块建立第二套产品库、合同解析器或模块适配实现。
