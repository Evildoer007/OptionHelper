# OptionHelper上下文约定

## 1. 系统边界

OptionHelper由一个通用`option-helper`Skill和一个可独立交付的OptionHelper App组成。两者消费同一Capability，不形成两套产品库、合同解释器或金融计算实现。

七个内部能力模块为DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter、Designer。五个操作页面属于DataFetcher、Payoffer、Pricer、Backtester、Reporter；Recommender由对话或工作流调用，Designer提供设计系统和渲染能力。

用户意图路由、能力预检、追问、默认值和进度以`SKILL.md`为唯一行为主源。本文只定义全局事实与协议边界；各`module-guide.md`只补充本模块输入、输出和拒绝条件；`README.md`只负责安装与配置。低优先级文件不得另设对话流程或改变上层默认值。

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

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

Host必须复用对话中已确认的事实。多个关键缺口一次合并确认，默认格式和版式不单独追问；用户已经要求研究简报、完整研究报告或两份都要时，不得重复确认交付类型。

能力预检按本次请求最小化执行。咨询、比较、只做结构推荐及Payoffer不要求数据凭据；Pricer、Backtester或需要新行情的正式交付必须先复用合格DataAssetRef，无法复用时再确认iFind数据能力已配置。已有结果交付不重新取数。当前对话Agent模式下，当前对话模型就是模型能力，不得询问第二套模型配置；只有独立批处理入口检查兼容模型端点。

数据能力缺失时必须在取数或计算之前一次说明并保留当前任务条件。不得先运行后补问，不得要求Access Token，不得猜测行情或无提示回退到本地CSV。凭据无效、网络不可用、Provider异常和数据权限不足必须分别表达，不能全部归类为未配置。

交付形式与分析覆盖分开记录。研究简报或完整研究报告只描述版式和内容；`complete`、`partial`、`unavailable`描述本次用户要求的模块是否都有同一合同下的已验证结果。交付物存在不等于分析覆盖完整。完整研究报告固定为连续A4正文，HTML宽屏提供左侧章节目录，PDF保留同一正文顺序但不显示导航；章节和标题必须逐字、按序为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。研究简报固定为结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险；无损益图，HTML固定210mm宽度、高度随完整内容自然延展，不保留空白占位，PDF按A4分页。

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
