# Recommender管理规范

Recommender拥有自然语言路由、推荐状态机、四个Agent步骤、候选证据门禁、冲突聚合、审批门禁和模块调度审计；不拥有产品事实、模型Provider、合同解析、市场数据或金融计算。

## 固定规则

- `RecommendationSet`唯一Schema为`optionhelper.recommendation-set`。
- 候选生成角色必须引用同一`catalog_version`下至少一条OptionLib章节证据，证据原文哈希必须匹配。
- OptionList、OptionLib、OptionReg证据状态不是`ready`时，候选不得运行计算模块。
- 复核角色必须逐一审阅已有候选，新增、漏审、重复审阅均失败。
- 默认请求3个候选，显式请求可在受控上限内扩展；排名从1连续递增，标的顺序不可改变。合格候选少于请求数量时保留现有候选并说明缺口，不得全部丢弃。
- 未批准候选不得进入面向客户的正式执行。Mode2至Mode4仅可在Host按模式授权后，对绑定当前CandidateVersion的候选运行研究性验证；验证结果不能冒充已确认合同的正式结果。客户批准后仍必须提供产品、标的顺序、约束指纹和合同指纹一致的`CandidateContract`。
- 单候选与多候选比较都按`candidate_id`选择；多候选必须逐一提供同一推荐结果中的CandidateContract，不能用排名替代身份。
- 用户调整合同条款时调用`create_term_variant`生成同产品的子CandidateVersion，不重新执行Research或Selector；Host随后解析新合同，并只调度受影响的正式模块。
- 金融评估角色只发送共享ToolSchema字段，同一候选可按约束重复调用获授权模块；所有Tool请求和真实返回均绑定CandidateVersion与FactRef。
- 自由流程按路由使用最小工具权限；相同模块与请求在单次运行内不得重复执行。
- 模型或端口失败不得生成候选、计算值或成功状态。模块不可用保留`unsupported/failed`及原因。
- Reporter是RecommendationSet的下游消费者；Recommender不导入Reporter，Designer不由Recommender直接调用。

## 回滚与升级

提示模板、路由和聚合策略可按版本回滚，但不得改写历史RecommendationSet。产品证据冲突交Knowledger，CandidateContract或正式Port缺口交共享Core与主任务处理，不得在本模块建立第二套产品库、合同解析器或模块适配实现。
