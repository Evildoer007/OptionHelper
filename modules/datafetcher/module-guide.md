# DataFetcher模块指南

## 职责

历史行情由`fetch_data(DataRequest, CallerContext)`获取；中国交易日历由`fetch_calendar_data(CalendarRequest, CallerContext)`获取。两者分别形成独立`DataAssetRef`，Tool与页面不会直接访问Provider。

## 输入与输出

- 历史行情输入：资产标识、字段、频率、日期范围、复权口径、来源优先级和CallerContext。
- 交易日历输入：资产标识、开始日期和结束日期。交易所由标的后缀自动映射；误带`fields`或`adjustment`会被忽略，不传给iFind。
- 输出：共享`DataAssetRef`、覆盖范围、字段口径、质量状态、来源与DataFetchRunRef。
- 写入：安装目录之外的项目Store中的受控`LocalDataStore`、本机缓存索引和按租户分区的`$OPTIONHELPER_RESULT_ROOT/output_datafetch/tenants/`。

## 规则

缓存身份包含租户、资产、字段、频率、复权、Provider和显式本地CSV的内容身份；同机相同身份在查缓存至落盘期间串行。没有显式交易所日历时只补已观测范围外的边缘区间，不以工作日推断中间缺口；有显式日历时仅补请求日期区间内缺失的session。

当前正式支持通过iFind API获取中国A股、ETF和指数日线，本地CSV只用于用户明确选择的离线任务。合同与回测结算数据必须能提供不复权`close`。历史行情的OHLC、复权、缺失值和HV输入质量校验保持不变。独立`trading-calendar`资产只保存交易所交易日期及其交集，不含价格，不执行OHLC字段审计，也不使用工作日推算。页面不得读取或暴露Provider明文凭据。

`fetch_calendar`按SSE或SZSE调用iFind`get_trade_dates`。多交易所标的只保留各交易所交易日交集。Provider不可用时只复用同租户、同标的、同交易所且完整覆盖请求区间的已验证缓存；覆盖不足时明确失败。

缓存复用完全由DataFetcher内部完成。Agent和其他模块不得直接读取`datafetcher-cache`、`index.json`、`calendar-index.json`或缓存资产，也不得把历史行情日期、普通工作日或临时脚本当作未来交易日历。

## 交互边界

- 只有本次任务确实需要新数据时才进入DataFetcher。进入前先检查App公开的数据服务配置状态，或确认Skill Host已注入`IFIND_REFRESH_TOKEN`；只检查是否配置，不读取、显示或记录凭据内容。
- 未配置时在Provider调用前一次说明：“当前数据服务尚未配置。请先配置iFind Refresh Token；配置完成后我会继续当前任务。”保留已确认的标的、日期和字段，不重复询问。
- 已配置但失败时，分别说明凭据无效或过期、网络不可用、Provider异常、数据权限不足，不把所有失败都归类为未配置。
- 默认使用iFind API实时获取或刷新中国市场数据；只有用户明确选择离线数据时才使用本地CSV，不把本地CSV作为无提示回退。
- iFind只要求Host保存Refresh Token，短期访问凭据由Provider自动获取和更新。不得要求用户同时维护两种Token。
- 日期、字段和复权口径能按任务目标确定时直接使用；确有歧义时一次合并确认。不得要求普通用户提供Provider字段名、物理文件路径或Store位置。
- 单独调用只返回数据质量结论、预览和受控引用，不自动触发推荐、定价、回测或报告。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；只有取数、校验明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。

组合分析或正式交付中由顶层工作流统一发进度，本模块不重复发送以下文案。

- 开始：我先核对标的、时间范围和数据口径，再获取并检查数据质量。
- 进度：数据已经取得，正在检查日期覆盖、缺失值和复权口径。
- 完成：数据检查完成。下面给出覆盖范围、质量结论和可用于后续分析的字段。
- 失败：本次数据暂时无法取得。我会说明失败原因、受影响的分析和需要补齐的条件，不使用未经确认的数据替代。
