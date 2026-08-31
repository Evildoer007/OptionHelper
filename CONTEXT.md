# OptionHelper上下文约定

## 1. 系统边界

OptionHelper由一个通用`option-helper`Skill和一个可独立交付的OptionHelper App组成。两者从同一权威源码独立组装，只共享受哈希绑定的计算载荷，不形成两套产品库、合同解释器或金融计算实现，也不复制彼此的完整目录树。

- 共享计算载荷：App与Skill共用的期权知识、合同、数据、计算、结果和交付实现。
- Skill：供外部Harness读取的业务工作流、规则、工具说明和共享计算载荷。Skill不拥有Agent生命周期，也不携带五模块页面、App前后端或原生壳。
- App：在共享计算载荷之外，单独携带五模块页面、Module Host浏览器资源、OptChat、OptDesk、Settings、Login、App后端、Agent Runtime和原生壳。App不携带`SKILL.md`、Skill安装说明、模块指南或Skill环境检查。
- External Harness：调用Skill的外部会话宿主，拥有模型、子Agent、并行、等待、取消、恢复和过程展示。
- App Harness：OptionHelper App内置的会话与Agent宿主，拥有Main Agent、Child Session、工具桥和过程展示。
- Recommender Mode：结构推荐内部的业务角色拓扑，不代表具体模型Provider、进程或会话实现。
- Shared Workflow Policy：咨询、指定模块、结构推荐和正式交付的共同业务路由规则。

七个内部能力模块为DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter、Designer。五个操作页面属于DataFetcher、Payoffer、Pricer、Backtester、Reporter；Recommender由对话或工作流调用，Designer提供设计系统和渲染能力。

App与Skill必须遵循同一Shared Workflow Policy和Capability事实边界。App行为由App Harness执行，Skill行为由External Harness执行；`SKILL.md`只对Skill运行具有行为权威，不是App Agent生命周期或前端过程展示的权威。本文只定义全局事实与领域边界；各`module-guide.md`只补充模块输入、输出和拒绝条件；`README.md`只负责安装与配置。低优先级文件不得改变Shared Workflow Policy。

## 2. 产品与合同事实

- OptionList：人工目录，保存产品编号、中文名称、分类和资料状态。
- OptionLib：人类和模型阅读的条款、唯一数学符号、路径、分段函数、默认示例与风险说明主源。
- OptionReg：机器产品源，只保存字段定义、默认条款、监测、路径和现金流表达。
- `ResolvedContract`：由OptionReg、ContractContext和合法条款覆盖解析出的不可变合同。

OptionReg顶层只允许`term_catalog`和`products`。每个产品只允许`identity`、`terms`和`paths`。`pnl`只用`cash(t,amount)`表达持有方现金流。

## 3. 输入与运行边界

```text
PayoffInput    = {ResolvedContract}
PricingInput   = {ResolvedContract, PricingConfig, market_data_refs}
BacktestInput  = {ResolvedContract, BacktestConfig, DataAssetRef}
```

`PricingConfig`仅保存市场、模型和数值控制。`BacktestConfig`仅保存历史样本、回放和统计控制。观察日程、障碍、票息、结算、同日顺序与现金流都属于合同，不得放入模块Config。

合同观察、障碍判断和历史结算使用不复权`close`。前复权`adjusted_close`只用于Pricer历史波动率和Backtester可选入场HV分组的对数收益率。年化票息与期权费按ACT/365，244仅用于交易日年化或模拟网格。

模块之间使用结构化JSON和不可变引用；普通用户界面默认给专业结论、依据和限制，不展示内部字段、Tool名、运行标识、文件路径或环境信息。用户明确要求JSON或进行系统集成时，才返回原始结构化结果。单模块运行不自动生成报告，Reporter只在用户已有明确交付需求时介入。

**控制平面**：App主进程内负责HTTP、Module Host上下文、权限、任务状态和唯一Store写入的边界。不得直接执行Pricer或Backtester金融计算。

**计算操作**：由TaskOperationService持久记录、可跨页面存活的一次Pricer或Backtester运行。页面切换和iframe隐藏不改变其生命周期。

**计算Worker**：只消费冻结执行快照并生成ModuleRunDraft的独立Backend进程。它没有凭据、用户目录、任务Store或结果Store写权限。

**基础设施恢复**：计算Worker启动、通信或存活状态异常后，对同一计算操作和冻结执行快照进行的自动续跑。基础设施恢复不创建新的计算操作，也不改变计算输入。

**Worker尝试**：计算操作在一个具体计算Worker上的单次执行。一次计算操作可以经历多次Worker尝试，但最多形成一个正式ModuleRun。

**业务拒绝**：合同、行情、交易日历、权限或参数不满足运行条件时，在结果提交前形成的明确终态。业务拒绝必须说明可操作原因，不属于Worker故障。

**冻结执行快照**：主进程完成合同、数据、日历、Capability版本和权限校验后形成的不可变计算输入。运行期间任务切换合同不会改变该快照。

**ModuleRunDraft**：计算Worker输出的带文件哈希清单的暂存结果。只有主进程复核执行范围、Capability哈希、合同指纹和文件哈希后才能提交为ModuleRun。

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

Agent必须复用对话中已确认的事实。多个关键缺口一次合并确认，默认格式和版式不单独追问；用户已经要求单结构或多结构的研究简报、完整研究报告、参考报价或多种交付组合时，不得重复确认交付类型。

纯咨询、比较和条款解释不启动运行时，不要求选择Python、配置iFind或初始化Store。需要运行模块时，先选择一次Python并检查依赖和外部Store；只有DataFetcher、依赖新市场数据的Pricer或Backtester，以及首次需要取数的推荐或正式交付，才检查iFind。当前对话负责理解用户意图。运行后按任务复用已确认事实和合格DataAssetRef，不重复索取数据凭据；已有结果交付不重新取数。

数据能力缺失时必须在取数或计算之前一次说明并保留当前任务条件。不得先运行后补问，不得要求Access Token，不得猜测行情或无提示回退到本地CSV。凭据无效、网络不可用、Provider异常和数据权限不足必须分别表达，不能全部归类为未配置。

交付形式与分析覆盖分开记录。单结构或多结构的研究简报、完整研究报告和参考报价只描述版式与内容；`complete`、`partial`、`unavailable`描述本次用户要求的模块是否具备对应合同下的已验证结果。交付物存在不等于分析覆盖完整。完整研究报告默认采用连续A4正文；宽屏HTML提供左侧目录，PDF不显示目录。单结构报告默认依次呈现核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。研究简报默认呈现结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险；无损益图，HTML固定210mm宽度、高度随完整内容自然延展，不保留空白占位，PDF按A4分页。多结构交付沿用相同的简报或完整报告规则，并按已确认顺序呈现各合同；参考报价是按标的分组的合同条款集合。用户明确要求补充说明或多结构比较时可以调整展示结构，但任何金融事实必须来自Reporter冻结输入，Designer和Presentation Patch均不得新增、修改或隐藏金融数值、合同条款、指标、表格、公式或图表。

组合分析和正式交付只有顶层工作流向用户发进度；子模块保留内部进度，不逐项重复展示。单模块独立调用时才使用本模块指南的用户可见文案。

## 4. 命名规则

- 模块、产品与领域对象：正式英文PascalCase，如Payoffer、PricingConfig、ResolvedContract。
- Python包、目录与文件：`lowercase_snake_case`或既定`lowercase-kebab-case`；不在内部路径重复`optionhelper`。
- 机器键可用稳定英文键；页面、公式和报告必须显示中文名称加OptionLib唯一数学符号。
- 不使用Po、P、BT、Payoff Editor、OptionPackage、Knowledge等别名替代正式名称。

## 5. 结果与版本

DataAsset、DataFetchRun、ModuleRun和ReportRun为不可变事实。用户指定目录只保存导出副本，不取代canonical结果。固定默认Payoffer资产与本次运行产物严格分离。

产品版本使用`product_version`与`catalog_version`；能力包使用`capability_version`；App使用`app_version`。不使用额外release ID。

## 6. 开发约束

共享运行时只能位于`core/`，模块实现只能位于各自`modules/{module}/`。所有模块通过Registry Loader读取OptionReg，不得硬编码Registry路径。历史目录仅供追溯，不能作为新实现的权威来源。
