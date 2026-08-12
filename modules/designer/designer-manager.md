# Designer管理规范

维护`src/design_tokens.py`中的唯一Design Token、组件、
CSS、离线ECharts主题、SVG主题、模板、字体、打印和可访问性规则。新增风格
先改Token或通用组件，不直接在模块页面或单份报告中复制颜色、字号或状态
样式。`assets/themes/designer-theme.css`只保存结构规则，不能成为第二个令牌源。

验收覆盖DataFetcher、Payoffer、Pricer、Backtester、Reporter页面骨架、
OptChat/OptDesk壳层、Card、连续A4 HTML Report、PDF、
数学公式、长中文、状态、窄屏、打印、离线打开和视觉回归。视觉变化不得
改变金融事实哈希。

完整Report的七个公开章节由Designer单点治理，固定为核心结论、结构推荐、
合同参数、收益结构、估值定价、历史回测、风险提示。Card是独立
单页研究简报，不套用Report章节结构。任何输入字段都不得改变Report章节名称、
数量或顺序。

## 修改与回滚

可修改Token、组件、模板、主题和渲染器，不得修改冻结事实、产品参数或排序结论。视觉版本独立登记；回滚恢复完整Token和模板集合，不能在单页打补丁形成分叉。

## 冲突升级

内容层级冲突交Reporter；模块交互冲突交General Manager；品牌与可访问性要求在Design System统一裁决。
