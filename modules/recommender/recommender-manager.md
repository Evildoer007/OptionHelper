# Recommender管理规范

Recommender拥有自然语言路由、推荐状态机、四个Agent步骤、候选证据门禁、冲突聚合、审批门禁和模块调度审计；不拥有产品事实、模型Provider、合同解析、市场数据或金融计算。

## 固定规则

- `RecommendationSet`唯一Schema为`optionhelper.recommendation-set`。
- 候选生成角色必须引用同一`catalog_version`下至少一条OptionLib章节证据，证据原文哈希必须匹配。
- OptionList、OptionLib、OptionReg证据状态不是`ready`时，候选不得运行计算模块。
- 复核角色必须逐一审阅已有候选，新增、漏审、重复审阅均失败。
- 默认请求3个候选，显式请求可在受控上限内扩展；排名从1连续递增，标的顺序不可改变。合格候选少于请求数量时保留现有候选并说明缺口，不得全部丢弃。
- 候选只保存`candidate_id`、`product_id`、正整数`rule_revision`及当前输入。任务不绑定活动产品、合同或数据，同一任务可以研究不同产品。
- 确认和计算相互独立。客户确认后提交选定候选的当前输入，Host按最新OptionReg编译，并仅执行明确请求的模块。研究性评估由Host按模式授权，不代表客户已确认正式执行。
- 单候选与多候选比较都按`candidate_id`选择，不能用排名替代身份。用户调整条款时，`create_term_variant`更新同一候选的当前输入并清除旧运行证据；Host按模块输入依赖执行后续计算。
- 金融评估角色只发送共享ToolSchema字段。当前评估必须携带`product_id`、`rule_revision`、明确的Core ModuleRunRef和对应模块状态；有评估记录时还须校验候选、轮次、完成状态及引用一致性。Host负责从Store核验金融事实，领域层不得通过任务状态查找替代结果。
- 排序只消费有对应已完成Run的指标，保留相应运行引用；模块失败、引用跨任务或缺失时不得用残留数值参与排序。缺少必要输入应返回待补充问题，不能伪装为完成或不可恢复故障。
- 执行前完整核验计划，禁止执行前半段后才发现重复、遗漏或越权请求。局部重试保留其他模块的有效Run，并替换重试模块的当前状态和证据。
- 自由流程按路由使用最小工具权限；相同模块与请求在单次运行内不得重复执行。
- 模型或端口失败不得生成候选、计算值或成功状态。模块不可用保留`unsupported/failed`及原因。
- Reporter是RecommendationSet的下游消费者；Recommender不导入Reporter，Designer不由Recommender直接调用。

## 回滚与升级

提示模板、路由和聚合策略可以回滚，但不得改写已保存的运行事实。产品证据冲突交Knowledger，Host编译或正式Port缺口交共享Core与主任务处理，不得在本模块建立第二套产品库、合同解析器或金融计算实现。
