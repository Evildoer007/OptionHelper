# Designer管理规范

维护`src/design_tokens.py`中的唯一Design Token、组件、
CSS、离线ECharts主题、SVG主题、模板、字体、打印和可访问性规则。新增风格
先改Token或通用组件，不直接在模块页面或单份报告中复制颜色、字号或状态
样式。`assets/themes/designer-theme.css`只保存结构规则，不能成为第二个令牌源。

验收覆盖DataFetcher、Payoffer、Pricer、Backtester、Reporter页面骨架、
OptChat/OptDesk壳层、研究简报Card、完整研究报告Report、参考报价Quote、
MultiCard、MultiReport及其HTML/PDF、
数学公式、长中文、状态、窄屏、打印、离线打开和视觉回归。视觉变化不得
改变金融事实哈希。

完整Report的标准七章由Designer单点治理，依次为核心结论、结构推荐、
合同参数、收益结构、估值定价、历史回测、风险提示。Card使用独立六块研究简报，
Quote使用按标的分组的合同条款表；MultiCard与MultiReport分别复用Card和Report
事实形成横向比较。标准模板不接受上游任意改写；只有用户明确提出单次展示调整时，
才使用受校验的Presentation Patch，且不得改变冻结金融事实。

## 修改与回滚

可修改Token、组件、模板、主题和渲染器，不得修改冻结事实、产品参数或排序结论。视觉版本独立登记；回滚恢复完整Token和模板集合，不能在单页打补丁形成分叉。

## 冲突升级

内容层级冲突交Reporter；模块交互冲突交General Manager；品牌与可访问性要求在Design System统一裁决。
