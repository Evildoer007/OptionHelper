# DataFetcher模块指南

## 职责

历史行情由`fetch_data(DataRequest, CallerContext)`获取；中国交易日历由`fetch_calendar_data(CalendarRequest, CallerContext)`获取。两者分别形成独立`DataAssetRef`，Tool与页面不会直接访问Provider。

## 输入与输出

- 历史行情输入：资产标识、字段、频率、日期范围、复权口径、来源优先级和CallerContext。
- 交易日历输入：资产标识、开始日期和结束日期。交易所由标的后缀自动映射；误带`fields`或`adjustment`会被忽略，不传给iFind。
- 输出：Core定义的共享`DataAssetRef`、覆盖范围、字段口径、质量状态、来源与DataFetchRunRef；不输出本模块私有的兼容引用。
- 写入：安装目录之外的项目Store中的受控`LocalDataStore`、本机缓存索引和按租户分区的`$OPTIONHELPER_RESULT_ROOT/output_datafetch/tenants/`。

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

## 规则

缓存身份包含租户、资产、字段、频率、复权、Provider和显式本地CSV的内容身份；同机相同身份在查缓存至落盘期间串行。原始CSV可跨日历证据复用，但每个`DataAssetRef`再按创建主体和日历证据隔离，未验证请求不会复用带日历谱系的引用。没有显式交易所日历时只补已观测范围外的边缘区间，不以工作日推断中间缺口；有显式日历时仅补请求日期区间内缺失的session。

当前正式支持通过iFind API获取中国A股、ETF和指数日线，本地CSV只用于用户明确选择的离线任务。合同与回测结算数据必须能提供不复权`close`。历史行情的OHLC、复权、缺失值和HV输入质量校验保持不变。历史行情的结束日不得晚于Host确认的可观测行情日；未来日期只允许通过独立`trading-calendar`请求取得，日历资产不含价格，不执行OHLC字段审计，也不使用工作日推算。页面不得读取或暴露Provider明文凭据。

`fetch_calendar`按SSE或SZSE调用iFind`get_trade_dates`。多交易所标的只保留各交易所交易日交集。Provider不可用时只复用同租户、同标的、同交易所且完整覆盖请求区间的已验证缓存；覆盖不足时明确失败。

历史行情需要已验证交易日完整性时，Host将同一租户、同一主体可读的`trading-calendar` `DataAssetRef`注入`DataFetcherConfig.trading_calendar_ref`。DataFetcher通过DataStore核验其字节、哈希、交易所映射、覆盖范围和session交集，并将`calendar_ref`写入历史行情`coverage`及`lineage.trading_calendar_ref`。未注入时质量只能是`unverified`，不会伪装为完整。

缓存复用完全由DataFetcher内部完成。Agent和其他模块不得直接读取`datafetcher-cache`、`index.json`、`calendar-index.json`或缓存资产，也不得把历史行情日期、普通工作日或临时脚本当作未来交易日历。

## 交互边界

- Skill的统一就绪门禁已在任何工作流前确认`IFIND_REFRESH_TOKEN`；DataFetcher不重复索取、读取、显示或记录凭据内容。
- 若运行时凭据失效或数据配置被撤销，在Provider调用前说明：“当前数据服务不可用。请检查iFind Refresh Token配置后继续。”保留已确认的标的、日期和字段，不重复询问。
- 已配置但失败时，分别说明凭据无效或过期、网络不可用、Provider异常、数据权限不足，不把所有失败都归类为未配置。
- 默认使用iFind API实时获取或刷新中国市场数据；只有用户明确选择离线数据时才使用本地CSV，不把本地CSV作为无提示回退。
- iFind只要求Host保存Refresh Token，短期访问凭据由Provider自动获取和更新。不得要求用户同时维护两种Token。
- Host配置的`timeout_seconds`会同时传入iFind的Refresh Token交换、行情和交易日历HTTP请求。Wind默认禁用；即使Host显式启用，取数前仍必须通过SDK和会话探测，不把“已启用”当作“已可用”。
- 日期、字段和复权口径能按任务目标确定时直接使用；确有歧义时一次合并确认。不得要求普通用户提供Provider字段名、物理文件路径或Store位置。
- 单独调用只返回数据质量结论、预览和受控引用，不自动触发推荐、定价、回测或报告。

## Host登记与发行验收

Host登记`DataAssetRef`时必须执行`register_data_asset(ref, identity)`等价约束：`ref.tenant_id == identity.tenant_id`、`ref.created_by == identity.principal_id`、`access_scope`含`read`，并在读取时再次核验同一约束、`content_hash`和`storage_ref`。DataFetcher只返回Core字段，不接受页面传入的主体、租户或SecretRef覆盖。

正式Capability由官方构建流程从本模块源码生成，禁止手改冻结副本。发行验收必须对生成副本验证：`call_tool_from_app(request, caller_context, secret_ref, secret_port)`签名可由Host调用，`fetch_calendar`可生成`trading-calendar application/json`资产，且历史`market-history text/csv`资产可被Host只读DataStore、Pricer与Backtester按当前Core的`DataAssetRef`字段读取。该验收使用假SecretPort和假Provider，不读取真实Refresh Token，也不访问iFind。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；只有取数、校验明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。

组合分析或正式交付中由顶层工作流统一发进度，本模块不重复发送以下文案。

- 开始：我先核对标的、时间范围和数据口径，再获取并检查数据质量。
- 进度：数据已经取得，正在检查日期覆盖、缺失值和复权口径。
- 完成：数据检查完成。下面给出覆盖范围、质量结论和可用于后续分析的字段。
- 失败：本次数据暂时无法取得。我会说明失败原因、受影响的分析和需要补齐的条件，不使用未经确认的数据替代。
