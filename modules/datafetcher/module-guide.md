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

当前正式支持通过iFind API获取中国A股、ETF和指数日线；LocalProvider只读取Host受控DataStore内的CSV，不代表远程缓存。远程缓存复用仍保留`ifind_http`来源，并由`cache_policy`决定实时获取、补齐或仅复用。合同与回测结算数据必须能提供不复权`close`。历史行情检查缺失值、重复记录、正值约束以及原始和复权OHLC关系。历史行情的结束日不得晚于Host确认的可观测行情日；未来日期只允许通过独立`trading-calendar`请求取得，日历资产不含价格，不执行OHLC字段审计，也不使用工作日推算。页面不得读取或暴露Provider明文凭据。

`fetch_calendar`按SSE或SZSE调用iFind`get_trade_dates`。多交易所标的只保留各交易所交易日交集。Provider不可用时只复用同租户、同标的、同交易所且完整覆盖请求区间的已验证缓存；覆盖不足时明确失败。

历史行情需要已验证交易日完整性时，Host将同一租户、同一主体可读的`trading-calendar` `DataAssetRef`注入`DataFetcherConfig.trading_calendar_ref`。DataFetcher通过DataStore核验其字节、哈希、交易所映射、覆盖范围和session交集，并将`calendar_ref`写入历史行情`coverage`及`lineage.trading_calendar_ref`。未注入时质量只能是`unverified`，不会伪装为完整。

单日行情请求若Provider明确返回空数据，只能沿上述已验证日历回退到所有请求标的共同的最近交易日；没有验证日历、没有共同session或多日区间整体为空时明确失败，不用weekday推断。

缓存复用完全由DataFetcher内部完成。Agent和其他模块不得直接读取`datafetcher-cache`、`index.json`、`calendar-index.json`或缓存资产，也不得把历史行情日期、普通工作日或临时脚本当作未来交易日历。

## 交互边界

- 只有当前动作实际取数时才确认宿主注入或用户明确保存的`IFIND_REFRESH_TOKEN`；DataFetcher不向用户重复索取，不显示或记录凭据内容。
- 若运行时凭据失效或数据配置被撤销，在Provider调用前说明：“当前数据服务不可用。请检查iFind Refresh Token配置后继续。”保留已确认的标的、日期和字段，不重复询问。
- 已配置但失败时，分别说明凭据无效或过期、网络不可用、Provider异常、数据权限不足，不把所有失败都归类为未配置。
- 默认使用iFind API实时获取或刷新中国市场数据；只有用户明确选择离线数据时才使用本地CSV，不把本地CSV作为无提示回退。
- iFind只要求Host保存Refresh Token，短期访问凭据由Provider自动获取和更新。不得要求用户同时维护两种Token。
- 连接测试成功后，DataFetcher按租户、主体和SecretRef修订号记录当前进程内的已验证状态；Token失败或连接失败会清除该状态。状态只含摘要，不含Secret值。
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

### 可选Tushare数据连接

App设置默认仍为iFinD。选择“Tushare”后，需要明确接入服务为Tinyshare或Tushare官方，并保存对应Token；二者不是同一凭据或同一数据来源。各服务的凭据分别保留，可以切回已保存来源。sales只保存本人的连接，不能覆盖管理员连接。连接测试实际读取一段交易日历，只证明该接口可用，不代表已获得所有行情及复权权限。

Chat、Desk和研究自动补数采用当前所选服务。自动查找已有行情和日历时也检查实际来源；明确选定的历史数据引用仍保持原来源，不重写历史记录。行情及日历的`lineage.provider`分别记录`ifind_http`、`tinyshare`或`tushare`，缓存按实际服务及用户隔离。鉴权失败立即终止，不自动转向其他服务。

|资产|行情接口|复权因子|价格单位|
|---|---|---|---|
|A股|daily|adj_factor|元|
|ETF|fund_daily|fund_adj|元|
|指数|index_daily|不适用|指数点|

接口日期为YYYYMMDD，统一输出YYYY-MM-DD。成交量`vol`按接口的手数乘100输出为股或基金份额；本模块暂不提供成交额字段。指数保留原值，证券前复权按“原始价格×当日因子÷请求窗口最后一个已观察交易日因子”计算，四个原始OHLC始终保留。因子缺失、重复、非正或未覆盖行情日期时失败，不用原始价替代。前复权缓存包含完整请求窗口，不跨不同基准拼接。交易日历使用`trade_cal`，分别查询SSE/SZSE，并要求开市与休市日期完整覆盖请求。

Tinyshare使用可审计的HTTP适配层，不导入或打包Tinyshare字节码SDK。当前协议依据本机0.1030.0发布包静态核查；其默认服务是HTTP，不支持同端口TLS。请求只发往固定服务，禁止重定向；遇到外部CDN返回明确报不支持，不追踪任意链接。已有Tinyshare设备标识优先复用；Windows没有既有设备标识时明确提示配置，本轮未声明Windows原生验收。

共享模块默认与Skill原有iFinD流程保持不变。程序调用可显式指定`provider="tinyshare"`或`provider="tushare"`，并通过对应的`TINYSHARE_TOKEN`、`TUSHARE_TOKEN`环境变量或受控`DataFetcherConfig`凭据端口注入Token。不得在请求、示例、日志或版本库保存真实Token。

接口口径参考：[股票日线](https://tushare.pro/document/2?doc_id=27)、[ETF日线](https://tushare.pro/document/2?doc_id=127)、[指数日线](https://tushare.pro/document/2?doc_id=95)、[交易日历](https://tushare.pro/document/2?doc_id=26)。不支持的市场、代码类别、频率或字段直接报错，不替换为其他资产或填零。
