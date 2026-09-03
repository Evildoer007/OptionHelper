# Backtester管理规范

维护入场生成、历史对齐、无前视回放、完整期限、逐笔账本、公共统计、MetricProfile、365/244口径及缺失数据处理。每笔`S0Raw`代理固定使用入场日未复权`close`；合同观察、障碍判断和现金流结算按`ResolvedContract.observation_price`使用相应未复权OHLC字段。可选入场HV只使用入场日及以前前复权`adj_close`对数收益。新增专属指标必须从逐笔账本可复算，MetricProfile只能由稳定product_id显式绑定，不能按中文名称或字段猜测。

验收覆盖交易日观察、ACT/365现金流、244年化边界、零样本、缺失值、多标的对齐、完整期限、路径事件、合同结算收益率百分比、正零负样本分解和逐笔人工复算。不得新增名义本金、保证金或期权费收益率选择器。

## 修改与回滚

可修改入场、回放、账本和指标实现，但不得把合同观察规则移入BacktestConfig。新增MetricProfile先定义适用条件和账本字段；回滚保留历史运行结果，只切换实现版本。

## 冲突升级

合同日程与现金流冲突交共享合同核心；历史字段与复权口径交DataFetcher公开ModulePort和共享DataStorePort；指标展示冲突交Reporter与Designer。Backtester不得导入DataFetcher内部实现或推断其存储路径。
