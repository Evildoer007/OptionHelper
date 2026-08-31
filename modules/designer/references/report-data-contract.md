# 报告输入约定

本约定只定义报告事实和运行状态。HTML的颜色、字体和版式规则见同目录的`design-system.md`，不得通过输入JSON覆盖其视觉令牌。

## 1. 顶层结构

```json
{
  "schema": "optionhelper.designer-payload",
  "meta": {
    "title": "场外衍生品投资策略",
    "as_of_date": "2026年8月3日",
    "report_id": "RPT-...",
    "generated_at": "2026年8月3日",
    "brand": "结构化产品研究"
  },
  "sections": ["conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"],
  "conclusion": {},
  "recommendation": {},
  "payoff": {},
  "pricing": {},
  "backtest": {},
  "parameters": {},
  "risk": {},
  "reference_quote": {}
}
```

Designer只消费`optionhelper.designer-payload`，且顶层`schema`字段必须存在并逐字匹配。其他schema或缺失schema均会被拒绝，不做版本转换、默认补齐或兼容读取。设计系统描述固定为`optionhelper.design-system`。内建`report-standard`默认七个章节依次为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。Card和Quote也各有自己的标准结构。默认结构不等于内容上限：补充说明必须先由Reporter冻结进Payload；Designer只按用户明确的`presentation_patch`选择当次展示结构。Report固定A4与Card的HTML壳和主题均由Designer内部固定，公开`DesignerInput`、Tool与CLI不接受`layout`或`html_report_layout`。payload的`meta`不参与版式选择。

`template_id`只选择Designer治理的`assets/templates/*.template.json`。模板定义可选择、命名和排序已有内容块，不能携带HTML、CSS、数值、公式或外部路径；缺少可展示事实的块直接省略。这样可以新增模板，而不要求模型修改Python或手写HTML。

正式对比输入在顶层提供`comparison.candidates`，每个候选包含公开标签、冻结排名、真实首选标记、产品名称、挂钩标的及完整单候选`facts`，候选不少于2个且不设固定上限。Designer按冻结排名展示，不把候选ID、Run ID、哈希或路径写入正文。多结构研究简报不消费候选图表；多结构完整研究报告仅合并横纵轴、单位与口径兼容的折线或柱状图，热力图按候选分别展示。两种多结构交付使用与单结构交付相同的`presentation_patch`和`supplemental_sections`规则。

`presentation_patch`是一次性交付展示指令，schema固定为`optionhelper.presentation-patch`。它支持章节排序、改名、隐藏、固定说明，以及加入Payload中已冻结的`supplemental_sections`。补充章节内容只支持文本、指标、表格、公式和图表；Card与Quote拒绝图表。原Payload、标准模板和Reporter事实始终不变，Patch本身不能携带或修改估值、回测、报价和合同数据。所有展示调整记录在内部回执中，HTML和PDF正文不展示该回执。Patch不接受HTML、脚本、CSS、颜色、字体、间距、圆角、阴影或外链字段。没有可展示事实的标准块继续省略，不渲染“未提供”。

`reference_quote`只在`output_type="quote"`时必填。它是Reporter已验证的冻结交易参考事实，不得从合同、估值、Greek、收益图、Payoff或回测结果推导。显式标记为demo、低精度、未验证或无效的事实会被拒绝。每个`groups`元素必须包含`title`、`columns`和`rows`；列由`key`、`label`和`format`组成，`format`只能为`text`、`number`或`percent`。不同结构应分组声明各自字段，行必须完整给出该组所有列值，不能以空值或“未提供”占位。

## 2. 核心结论与结构推荐

`conclusion`只接收结构化摘要：`structure_name`、`underlyings`、`reasons`、`valuation_summary`、`greeks_summary`、`backtest_summary`和`risk_summary`。缺少事实的分组不渲染，不补造套话，也不渲染历史`next_steps`。

`recommendation`只承载推荐结构、标的、推荐理由、市场观点匹配关系、适用条件、不适用情形与主要权衡，不重复合同参数。需要呈现研究图时，图必须写明标题、横轴、纵轴和来源口径：

```json
{
  "id": "market-path",
  "title": "图1：标的走势",
  "type": "line",
  "x": ["7/1", "7/2"],
  "series": [{"name": "标的", "data": [100, 101]}],
  "x_axis_name": "日期",
  "y_axis_name": "指数",
  "source_note": "数据来源：待填写。数据截至：待填写。"
}
```

`recommendation`可包含`headline`、`reason`、`structure_name`、`underlyings`、`suitable_for`、`not_suitable_for`和`alternatives`。`alternatives`用于备选结构或适配方案，每项使用`title`、`summary`和可选`tags`。备选结构不能替代主结构的正式条款、估值或回测结果。

## 3. Payoff、Pricing与Backtest

每个模块都有`status`。仅`ready`允许展示计算结果。状态值：

| 状态 | 含义 | 渲染规则 |
|---|---|---|
| `ready` | 已有本次运行的可复核结果 | 展示提供的结果、参数和图表 |
| `partial` | 仅部分输入或结果可复核 | 仅在上游提供公开说明时展示该说明，不冒充完整结果 |
| `pending` | 数据或运行仍待接入 | 不生成默认占位；有明确公开说明时才展示 |
| `not_run` | 本次任务未运行模块 | 不生成默认占位 |
| `unsupported` | 当前结构或引擎不支持 | 仅在上游提供公开说明时展示该说明 |
| `failed` | 已运行但失败 | 仅在上游提供公开失败摘要时展示，不显示伪结果 |

`payoff`使用`report_svg_path`、`terms`、`formula`、`scenarios`和`note`。`report_svg_path`只接受Reporter从本次Payoffer运行结果派生的报告专用SVG；找不到有效派生图时不回退到默认样图，也不生成占位图。

`pricing`使用`method`、`valuation_date`、`metrics`、`greeks`、`assumptions`、`scenario_rows`和`charts`。已提供的`greeks`按`Delta`、`Gamma`、`Vega`、`Theta`、`Rho`排序；没有冻结值的Greek不补空行，事实明确为不适用时才显示“不适用”。`status=ready`时展示完整已验证事实；`status=partial`时显示部分完成状态，并仅展示已冻结且可验证的指标和Greeks，不补造其他项目。图表仍只在完整且口径有效时展示。

`charts`支持`line`、`bar`和`heatmap`。热力图用于二维Spot×剩余期限的Greek曲面，字段为`x`、`y`、`data`、`x_axis_name`、`y_axis_name`、`z_axis_name`和`source_note`，其中每项`data`为`[x索引,y索引,已冻结数值]`。所有图表都有读屏摘要与完整展开的数据表；Tooltip、坐标轴和数据表使用同一展示格式，不允许输入颜色覆盖Designer主题。

`backtest`使用`window`、`entry_rule`、`metrics`、`card_metrics`、`event_statistics`、`detail_tables`、`limitations`和`charts`。`metrics`可包含样本数、胜率、平均收益、最大亏损；`card_metrics`由Reporter按产品`metric_profile`显式投影产品专属统计。完整Report展示已交接的`event_statistics`；Card和多结构研究简报保持摘要定位，只提取四项核心指标和四项产品专属指标，不要求展示`event_statistics`。`detail_tables`可承载公共回测、路径事件、监控、路径结果、年度、标的表现及产品专属统计。标准图表顺序为收益率分布、路径结果分布、年度表现、事件触发率；没有冻结图表事实时直接省略该图，只有Reporter明确交接了公开原因时才展示该原因，不得伪造净值图或缺失说明。所有统计必须来自逐笔回测结果聚合，且收益率分母须在`parameters.backtest_input`中披露。

## 4. 参数与风险

`parameters`可包含`payoff_input`、`pricing_input`、`backtest_input`。每项都是参数行数组，行字段为`cn`、`en`、`symbol`、`value`、`source`。PayoffInput必须是PricingInput和BacktestInput的严格子集。渲染后的参数表将`source`机器键显示为中文来源标签，JSON仍保留机器键供校验。

`risk`使用`items`和`disclaimer`。没有冻结风险文本时不生成默认风险段落，Reporter负责在可交付性门禁中决定是否允许该事实集合外发。

## 5. 展示格式与Card边界

Designer只改变读者格式，不改写冻结事实。普通数值、指标、Greek、参数表、事件统计和图表数值最多保留两位小数，使用千分位并移除无意义尾零；百分比同样最多两位并保留`%`，例如`34.234%`显示为`34.23%`，`30.00%`显示为`30%`。日期、代码、公式、原始JSON和事实哈希不参与数值格式化。

标准Card为210mm宽度、高度随完整内容自然延展，无损益图、无交互图。其默认内容是同一Report事实的简版：推荐结构、挂钩标的、推荐依据、估值日与方法、最多四项核心估值指标、已提供的Greeks、四项核心回测指标、四项产品专属回测指标及最多2项风险提示。没有已冻结事实时不生成对应块，不填默认数字。用户明确要求的补充事实由Reporter先冻结；`presentation_patch`只负责排序、别名和固定说明。PDF保持相同固定宽度和自适应内容高度，不因复杂期权结构或完整指标而拒绝交付。

Report固定为连续A4正文。未提供`presentation_patch`时按标准七章展示；用户明确单次调整后，HTML目录和PDF正文按当前有效章节同步变化。在“估值定价”中展开估值方法、估值日、全部估值指标、五个Greeks、假设、风险曲线、Greek曲面与已提供定价情景。在“历史回测”中展开样本定义、核心统计、路径事件、产品专属统计、年度统计、标的表现与全部已提供图表。

## 6. 渲染校验

- `Payoff`、`Pricing`、`Backtest`的状态只能是`ready`、`partial`、`pending`、`not_run`、`unsupported`或`failed`。
- 图表序列长度必须与横轴一致。图表不提供缩放、滚动或折叠控件；全部冻结数据以完整表格随图表直接展示，确保HTML可直接转为PDF。
- 参数来源只能是`template_default`、`user_override`、`user_selection`、`market_fixing`或`schedule_derived`。
- 当PayoffInput、PricingInput和BacktestInput同时存在时，渲染器校验PayoffInput是后两者的子集。

## 7. 证据边界

- 从同一份冻结交接事实或同一任务运行快照取数，不得在报告渲染时重估、重算Greeks或重跑回测。
- 绝对价格条款、相对价格条款、日频近似、缺失数据处理和收益率分母必须按实际输入披露。
- 收益图、估值、回测三者分别引用本次任务的资产和结果，不用默认样图或案例数据替代。
