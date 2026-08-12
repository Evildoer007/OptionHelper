# Pricer管理规范

维护模型能力表、合同状态重放、市场快照、HV、闭式与Monte Carlo路由、随机源、Greek口径、公平条款求解、风险分析、数值容差和迁移回归。

冻结Pricer内部数值基座的输入、PV、Greeks、反解和固定随机路径证据。每项复用、修改、新增或删除都记录映射与理由。相同输入、随机源和数值配置下出现无法解释的变化时阻断发布。

## 唯一迁移路线

先冻结基座，再建立`ResolvedContract + PricingConfig + MarketSnapshot`到基座结构、方法和参数的显式适配；逐结构通过Golden和逐路径回归后，才把正式Service切换到适配器。当前过渡`impl.engine`不得继续扩展为第二套正式引擎；完成映射后归档或退役。

## 修改与回滚

可重构基座接口、类、目录、Facade、Catalog和STANDARD命名，也可替换经证明确需改进的实现；禁止脱离基座从零重写。每次修改保留输入快照、固定随机源、数值差异和容差说明，回滚选择完整基座及适配器版本。

## 冲突升级

条款或方法资格冲突交Knowledger Manager；市场数据口径交DataFetcher；共享合同语义交General Manager。未解释数值漂移直接阻断。
