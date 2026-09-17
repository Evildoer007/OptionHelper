# OH产品维护接入说明

源码核对日期：2026-09-16。以下路径均相对OH仓库根目录。不要硬编码维护者的本机路径；从当前仓库定位。此说明用于找入口，实际字段与行为以本次读取的源码为准。

## 1. 三库与默认图

| 源头 | 维护内容 | 注意事项 |
| --- | --- | --- |
| `references/optionlist.md` | 产品名称、ID、类别、顺序、状态 | 表格序号是展示顺序，不是产品ID；新增、删除后同步目录顺序 |
| `references/optionlib.md` | 条款、分段收益、默认例子、适用场景和风险 | 产品标题和子标题编号一起维护；材料不完整时不编造条款 |
| `references/optionreg.py` | `PRODUCTS`、`TERM_CATALOG`及`REGISTRY` | 改单个产品条目，不重写整个库；不要执行用户附件中的任意Python |
| `modules/payoffer/figures/json/产品中文名.json` | 默认条款、路径、绘图模板 | 改名需核对文件名引用；规则改变后重建相关资产 |
| `modules/payoffer/figures/svg/产品中文名.svg` | 默认收益图 | 使用现有Payoffer渲染器生成，不复制别的产品曲线 |

OptionReg身份含`product_id`、`name_zh`、`entry_status`、`rule_revision`。经济条款放在`terms`，观察规则参考`monitor`，派生量参考`derived_terms`，收益放在`paths`的`condition`、`cases`、`domain`、`pnl`。以对应产品当前实际层级为准，不凭字段名称自行增加一层。

在`TERM_CATALOG`复用相同含义的字段，核对存储键、公式符号、单位和取值范围。现有`Pi_0`和`P_net`在S0=100口径下的5表示5%，不能再当小数比例除一次100；`p`属于另一种费率表达。把所有百分比先写清楚再翻译字段。

`cash(0, ...)`是期初现金流，`cash(T, ...)`是到期现金流，正负号以持有方为准；`u`等换算变量由现有解释器处理。不要合并不同日期的付款。名义本金属于运行及金额展示参数，不是替换`S0`的理由。

已有默认图的受控维护入口是`modules/payoffer/maintenance/default_asset_manager.py`中的`plan_default_asset_refresh`和`publish_default_assets`。计划会扫描全部现有JSON，不是只处理所选产品：新增缺资产会触发数量检查；改名后旧文件未绑定当前名称会失败；其他产品的未经确认差异也会阻塞。先在候选副本整理目标产品的成对资产，再审阅完整计划，不能借本次授权顺手更新其他结构。发布记录只备份本次JSON/SVG，不备份三库、计算代码或安装包；捕获到替换异常时会尝试恢复资产，但不代表断电或进程被强制终止后必然自动恢复。

查看相近产品只是为了理解已支持的表达方式，不代表新结构可以直接复制。修改`rule_revision`后，还需查快照和资产引用如何生成，不能只改数字。

## 2. 编译与能力

共享入口是`core/src/runtime/contracts/contract_api.py`，导出`load_registry`、`validate_registry`等能力；具体校验与合同解释在`contract_engine.py`。先检查`validate_product_spec`和`validate_registry`，再按现有模块service的真实输入编译和运行。仅通过注册校验不等于金融结果正确。

| 能力 | 阅读入口 | 必须检查 |
| --- | --- | --- |
| 产品读取 | `core/src/runtime/knowledger/registry_loader.py` | `load_registry`，产品规则更新后重新启动宿主，保留现有完整性保护 |
| 收益图 | `modules/payoffer/src/asset_resolver.py`、`impl/engine.py`、`impl/svg_renderer.py` | `build_visual_template`、`build_payoff_input`、`render_paths`、`render_svg`；首次录入需创建两份资产，不只刷新旧图 |
| 定价路由 | `modules/pricer/src/model_router.py` | `PRODUCT_CAPABILITIES`、`capability_for`、`resolve_route`；MC可由启用产品的声明补充，解析方法还要有真实实现 |
| 定价适配 | `modules/pricer/src/product_pricing_adapter.py`及`engines/` | 结构语义、市场输入和引擎之间的映射 |
| Greeks与反解 | `modules/pricer/src/greeks.py`、`fair_parameter.py`、`fair_solver.py`、`valuation_solver.py` | 适用参数、边界、单位、收敛与最终估值；不把不支持项补零 |
| 回测 | `modules/backtester/src/entry_generator.py`、`path_replay.py`、`impl/engine.py` | 观察日、提前结束、真实路径复核、账本、HV口径及历史覆盖 |
| 推荐 | `modules/recommender/src/candidate_builder.py`、`service.py` | 候选产品读取与停用检查；新增结构是否可被检索并解释 |
| 报告 | `modules/reporter/src/service.py`、`products/app/backend/reporter_adapter.py` | 使用本次有效结果；新旧规则证据不得混用 |

字段登记还要核对存储键与公式符号：例如K1对应K_1，公式不能直接照抄存储键。`validate_product_spec`和合同编译按登记顺序处理`derived_terms`；派生量只能引用已可用的变量，不能前向引用或循环依赖，`monitor`也要核对事件依赖顺序。派生量不能同时作为普通默认字段或允许用户直接覆盖。`constraints`既要约束默认值，也要在用户覆盖后继续成立；例如价差必须保持K_1 < K_2。核对页面可编辑性、编译约束和最终现金流三者一致，不能只试默认参数。

只有新结构超出现有表达能力时才扩展共享解释器或引擎。不能将未支持的支付函数偷偷近似为已有函数；先说明需要扩展什么并确认影响。

## 3. 当前新增与删除的发布阻碍

当前`packaging/knowledge_snapshot.py`存在多处固定65项的校验。重点查：

- `registry_product_revisions`：注册表身份与修订。
- `_optionlist_products`、`_published_optionlist_ids`：当前与已发行目录顺序。
- OptionLib片段数、冻结Payoff资产集合和`product_count`。
- 技术候选、产品源快照及知识源清单验证中的产品数量、顺序和引用集合。

新增第66个或删除一个产品时，三库改好仍可能无法构建。全仓搜索与产品数量有关的`65`、`range(1, 66)`、目录清单和旧产品名称/ID，区分真实数量限制与无关数值。不要全局替换数字。

当前源的产品集合由确认后的目录决定，三库和资产集合须完全一致，数量由这个集合得到。检查空目录、重复ID、缺片段、顺序不一致仍然会被拒绝。旧发行版本依据自己冻结的目录和清单验证，不能强迫旧版本满足新版本数量。产品ID不重新编号，表格序号可随删增重排。

现有合同编译入口拒绝`entry_status=False`，MC补充路由只登记启用产品；但静态解析路由表可能仍有旧项，推荐和页面也要分别检查。不能只看一个入口就宣称已完整停用。删除前搜索该产品的名称、ID、默认图和能力登记，检查页面、推荐、报告以及旧任务对当前规则的依赖。历史结果与旧版本归档不属于清理目标。

## 4. 运行与交付

当前维护者使用Anaconda的MachineLearning环境，不新建环境。换机器依据仓库README和锁定依赖定位解释器。先看仓库指令，验证代码放独立文件，不修改原测试和基线。公开仓库可能不包含本地tests；缺失时新建明确范围的验证，不能引用不存在的测试作为通过证据。

开发入口：`packaging/app/run_development_app.py`。在独立测试数据目录运行，观察实际启动路径和版本；不要读写用户日常任务来验证产品。测试金融调用需冻结输入、行情和随机条件，输出保留现金流、事件与单位，不只比较一个价格。

构建入口：`packaging/build_current.py`；正式发行：`packaging/release.py`。App计算资源：`packaging/app/build_capability.py`；平台构建：`packaging/app/build_app.py`；业务Skill：`packaging/skill/build_skill.py`。先读各入口当前参数和副作用，沿用App与Skill独立版本规则。不要给用户另造一套维护业务命令行工具。

Demo入口：`demo/tools/refresh_demo.py`，它可能同步仓库外的桌面Demo，因此只有目标范围已确认时才执行。Demo的实际计算能力不能替代App验收。

“生效”的验收例子：新产品能选择、能改参数、能生成收益图，适用的定价/回测可运行；新结果显示新规则，旧结果仍可查；停用或删除产品不能再发起新计算。安装版必须打开新安装物再验证，不能用开发页面代替。


## 5. 必须逐项处理的显式登记

以下是已核实的依赖，不代表以后没有新增依赖。每次操作仍需检索当前源码。表内路径均从仓库根目录起算。

| 入口 | 具体位置 | 什么变化需要处理 |
| --- | --- | --- |
| `modules/pricer/src/fair_parameter.py` | `_AUDIT_PRODUCTS`、`_EVIDENCE_IDS`、`_validate_audit_table`、`capability_matrix` | 新增/删除必须同步产品集合。即使不支持反解，也要明确登记真实状态和原因；修改报价目标、费用时点或约束后重新核对目标和逻辑证据，不能仅复制“supported” |
| `modules/backtester/src/economic_conventions.py` | `_ALL_PRODUCT_IDS`、`_PREMIUM_INCLUDED`、`_PREMIUM_FIXED_ZERO`、`economic_convention` | 新ID未登记会报错。修改费用规则时核对期权费是否已计入现金流；“没有单列期权费”不等于零期权费 |
| `modules/backtester/src/metric_profile_map.py` | `METRIC_PROFILE_MAP`、`REQUIRED_PROFILE_OUTPUT_KEYS`、`validate_metric_profile_coverage` | 产品集合需逐一覆盖且不能有多余映射。收益/事件类型变化时重新绑定指标类型，不能让新雪球沿用香草期权的统计字段 |
| `modules/backtester/src/metric_profiles.py` | `metric_profile_for`的调用及各类指标实现 | 新指标类型必须真正生成相应输出；只登记字段名无法生成事实 |
| `modules/backtester/src/trade_ledger.py` | 合同结算、归一化、方差产品分支 | 支付时间、费用、本金、份数和收益单位变化时核对逐笔账本与分母 |
| `modules/pricer/src/model_router.py` | `PRODUCT_CAPABILITIES`及MC动态补充 | 解析能力是显式登记。修改已有产品公式后，旧解析实现可能已不适用，不能沿用原来的支持声明；停用/删除核对残留路由 |
| `modules/pricer/src/product_pricing_adapter.py` | `ProductPricingAdapter`及按product_id/adapter分支 | 路由正确仍需参数翻译正确，核对旧解析公式是否忽略新增条款 |
| `modules/pricer/src/service.py` | `_recompile_fair_candidate` | 当前9.3的p反解存在专属重编译分支；修改该产品约束、费用或新增同类结构时，核对求解候选仍忠实于确认条款，不能只维护反解矩阵 |
| `modules/pricer/src/engines/pricing_core/optionhelper_core.py` | `price`及存续状态检查 | 当前1.1/1.2有专属欧式状态校验；新增相似产品或改变行权语义时，核对实际状态检查，不假设复制目录项就继承全部逻辑 |
| `modules/pricer/src/cashflow_lifecycle.py` | 期初费用、已实现与剩余价值处理 | 费用时点、存续状态或零时点现金流变化时，防止漏计或重复计入 |
| `modules/pricer/src/observed_state_rebuilder.py` | 存续状态、方差/区间计息历史输入 | 新发运行成功不等于存续可用，累计数量、已触发事件、历史观察单独验证 |
| `core/src/runtime/contracts/term_presentation.py` | `build_term_fields`、`_editability`、`term_value_encoding` | 增减参数、约束、百分比编码和可编辑范围时，检查页面字段是否确实可提交 |
| `core/src/runtime/contracts/input_adapter.py` | 输入适配和时间条件 | 新字段或日历语义变化不能只改词典，需检查进入合同的最终值 |
| `products/app/backend/position_amounts.py` | `project_backtest`、`project_payoff`及定价金额投影 | 当前存在按9.4识别方差名义本金的分支；新增类似单位的结构必须审阅，不把所有结构都按普通本金缩放 |
| `products/app/backend/product_rule_revision.py` | `reconcile_product_rule_revisions` | 会移除部分旧结果、候选、报告、生成消息并回收附件。核验时未找到正式启动调用，不要因此新增调用或直接运行它来更新产品 |
| `core/src/runtime/contracts/contract_engine.py` | `verify_current_product_rule` | 当前规则被删除、修订变化或条款不匹配可能拒绝旧合同重跑；旧结果保留不等于可按新版重算 |

新增产品时，反解矩阵与回测两张映射表属于必须作出明确处理的登记；不得等最终用户报错才补。删除时同步清理当前映射，保留已发行旧目录。既有产品改参数时这些表可能不用改，但须证明参数含义、现金流与统计分类没有变化。

## 6. 从页面、Agent到报告的检查链

产品目录与参数描述由`modules/payoffer/src/service.py`、`modules/pricer/src/service.py`、`modules/backtester/src/service.py`提供。先检查服务返回，再检查对应`modules/*/page/`页面；不要维护一份页面自造的产品列表。新增字段涉及共用展示时继续查`core/src/runtime/browser/`。

涉及工具输入时检查`core/src/runtime/protocol/schemas/tool-io.schema.json`、`core/src/runtime/protocol/models.py`、`core/src/runtime/protocol/tool_catalog.py`、`products/app/backend/tool_gateway.py`、`products/app/backend/agent_runtime/runtime_tool_bridge.py`。确认参数经校验后确实进入冻结合同，不是前端显示成功但计算仍使用旧默认。

资料可被Agent检索的链路查`core/src/runtime/knowledger/`、`core/tool_entry.py`和`products/app/backend/agent_runtime/recommender_adapter.py`。产品改名时，普通解释、推荐候选和计算目录分别核对。解释知识中出现名称不证明该产品可以计算。

报告链路查`modules/reporter/src/evidence_resolver.py`、`report_unit_builder.py`及`modules/designer/src/`实际模板。新输出字段必须来自本次计算的有效证据。新费用口径需检查金额附表、账本、Greeks单位和报告文字是否一致，不用旧报告事实填充新结构。

## 7. 发布源头、生成物与已知阻塞

| 文件或位置 | 必查内容 |
| --- | --- |
| `packaging/knowledge_snapshot.py` | 三库集合与顺序、固定65项、启用状态、产品快照及默认资产基线 |
| `packaging/skill/build_skill.py` | working-tree目录、产品修订映射、`product_count`，不能只改知识快照 |
| `packaging/skill/verify_skill.py` | 产品数、资产对数、目录顺序、解析能力数；当前有65产品和34个解析产品断言 |
| `packaging/app/verify_capability.py` | 默认图对数、产品ID数与唯一性 |
| `packaging/app/macos/verify_macos.py` | 平台验收中的产品目录数量 |
| `packaging/app/windows/verify_windows.py` | 平台验收中的产品目录数量 |
| `packaging/app/capability-source-map.json` | 扩展核心或新增文件时是否进入App计算包 |
| `packaging/skill/package-source-map.json` | 同样的实现是否进入业务Skill包，不能只在源码可用 |
| `tests/baselines/payoffer_default_assets.sha256` | 现有发布读取的默认资产对基线；可能未包含在公开仓库 |
| `demo/tools/build_current_ui.py`、`demo/tools/refresh_demo.py` | 页面目录、默认参数和收益图由现有入口重新生成，再同步已授权位置 |

停止启用产品还有独立限制：`_write_candidate_tree`目前要求OptionList状态为“已录入”且OptionReg的`entry_status`为True。产品仍留在集合中但改成False，发布可能失败。要发布含停用条目的目录，需要显式设计停用产品是否随包携带、如何展示、怎样拒绝执行及旧版本验证方式；先确认方案，不能简单改回True通过构建。

`_baseline_pairs`读取上面的基线文件，`_asset_sources`还校验JSON与SVG的组合内容。新产品无基线、改名未迁移条目、默认图变化但基线未对应，都会阻塞。不要复制旧产品摘要或修改旧Golden。可以为确认后的新发行准备独立的候选基线及相应消费入口，保留原基线供差异核对；确需替换当前发行基线时说明原因、证据和回退，按用户授权处理。公开仓库缺少基线时，准确报告这个构建依赖，不造一个“通过”记录。

源文件由人或Skill修改；知识源快照、Catalog、打包清单、安装资源与Demo由现有入口生成和验证。不得手工修改包内快照、删完整性字段或只修改产物。历史归档不跟随当前目录批量重写。


## 8. 默认值、改名和资料解析的隐藏影响

`core/src/runtime/contracts/contract_engine.py`中的`verify_current_product_rule`不仅比较修订号，还依据当前默认值及旧合同中明确标记为override的参数重建合同，比较terms、term_sources、paths等内容。因此，改默认期权费但不递增修订号，也可能使采用旧默认值的合同被拒绝重跑；显式覆盖该参数的合同可能不受同一种变化影响。分别验证新任务、旧默认任务、旧覆盖任务，不直接替换旧任务参数。是否递增版本按确认后的经济变化与版本策略处理，不把不递增版本当作兼容方案。

改名时检查三库名称、OptionLib产品标题与子标题、默认JSON内name_zh、JSON/SVG文件名、目录显示和检索输入。`core/src/runtime/knowledger/search.py`的`product_section`依赖`### 产品ID 名称`的标题形式，`product_relevance`使用产品ID和当前名称；没有证据证明旧名称会自动成为别名。若用户要求旧称继续可检索，需验证并按范围补充支持，不能为了保留旧称再建重复产品。

`modules/payoffer/src/asset_resolver.py`的`_asset_name`拒绝空名称、斜杠、反斜杠和空字符。跨平台还要审阅Windows文件名限制和大小写冲突；不能默默改动用户确认的正式名称。确需内部文件名映射时属于额外实现，说明影响后处理。先验证新资源，再清理本次确认不再使用的旧资源。

三库结构也有解析要求：Markdown表格中的竖线和换行可能破坏目录解析；重复字典键在Python中可能静默覆盖。新增资料后检查真实解析出的产品集合、顺序和完整说明，不只看文件打开后的外观。`packaging/knowledge_snapshot.py`的`_assert_no_duplicate_registry_keys`是现有重复键检查入口，不能省略。

## 9. 数据、日历与实际发布来源

`core/src/runtime/contracts/input_adapter.py`的`compile_compute_data_requirements`从条款推导行情字段、期限、观察次数与日历要求。改变observation_price会增加相应行情字段需求；新增观察规则可能要求未来交易日历。继续核对`modules/pricer/src/calendar_policy.py`和App取数适配，以及Backtester历史输入/回放。所需字段被列出只证明提出了需求，不证明Provider和各引擎已经支持。

对平均价、最高/最低价、多标的篮子、连续障碍、交易时段或新的计息方式，先写出资料要求，再对照现有表达能力、数据粒度和采样逻辑。不以模型能够给出一个数作为语义正确的证据。语义不等价时阻塞并说明差别，不能将离散观察冒称连续观察。

`packaging/source_snapshot.py`的`default_snapshot_selectors`由App/Skill源映射及平台输入组装构建源，`frozen_source_snapshot`负责冻结。新增实现文件还需核对实际快照是否包含它，防止开发态可导入但发行包缺文件。生成完成后核对产品ID、规则修订、默认条款和资产与这份候选一致；App、业务Skill和Demo不能分别使用不同时间的工作区冒称同一次更新。

发布中断时先保存当前状态和失败阶段，恢复到一套完整的旧交付物，不混搭新规则与旧默认图。运行中的研究、未保存的编辑及旧结果不要由本Skill清理。等待用户已授权的运行结束或按原停止机制处理，再重启相关宿主；重启后实际创建新任务验证，不能用旧页面残留结果证明生效。

## 10. 先选构建模式，再检查对应依赖

| 目标 | 当前入口与来源 | 必须核对 |
| --- | --- | --- |
| 技术候选 | `packaging/build_current.py`，业务Skill构建的working-tree Catalog | 当前三库、资产和源映射；产物标记technical_candidate；目标平台校验 |
| 正式发行 | `packaging/release.py`及所调用的发行快照路径 | 冻结目录、默认资产基线、发行清单和旧版本兼容 |

前文提及的`_baseline_pairs`及旧发行基线限制适用于实际调用它们的路径；不能一概断言技术候选也必须读取旧Golden。先沿当前入口确认调用链。候选构建成功不等于正式发行已通过；只修候选所需数量校验时，明确正式发行和其他平台仍未验收。不要为每个新产品再散落一个固定总数，应由已确认产品集合校验三库、资产与登记表的完整一致性。

## 11. 用户要求独立App试用时

记录候选源码目录、安装位置、应用身份与数据目录。使用现有可配置入口，只有当前入口无法隔离时才在候选源码做最小调整；不把某个测试目录永久写进正式产品。

macOS构建的`compile_shell`可能从Swift回退到Objective-C。核对实际`app-manifest.json`中的shell_language/build_tool，再检查真正生效的壳源码；若更改通用启动行为，应核对两个实现。环境变量可能覆盖默认数据路径，不能只看代码默认值。

首次启动后、登录和业务操作前，核对后端进程实际`--data-dir`及资源路径。若误指向日常数据，立即退出并记录实际情况；不能因为未登录就保证后端没有写入。不同App名字或bundle ID不能单独证明数据隔离。

新数据目录通常没有模型和行情配置。先检查依赖状态，再验收对应能力；不默认复制日常凭据。连接测试成功只证明该测试请求成功，真实行情请求仍需检查标的、日期、字段和接口响应。签名失败时保留错误并定位资源变化或复制元数据问题，不关闭签名验证来完成安装。
