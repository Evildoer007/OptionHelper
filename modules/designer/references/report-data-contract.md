# 报告输入约定

本约定只定义报告事实和运行状态。HTML的颜色、字体和版式规则见同目录的`design-system.md`，不得通过输入JSON覆盖其视觉令牌。

## 1. 顶层结构

```json
{
  "meta": {
    "title": "场外衍生品投资策略",
    "as_of_date": "2026年8月3日",
    "report_id": "RPT-...",
    "generated_at": "2026年8月3日",
    "brand": "结构化产品研究",
    "layout": "brief"
  },
  "sections": ["conclusion", "recommendation", "parameters", "payoff", "pricing", "backtest", "risk"],
  "conclusion": {},
  "recommendation": {},
  "payoff": {},
  "pricing": {},
  "backtest": {},
  "parameters": {},
  "risk": {}
}
```

Designer固定输出七段完整报告，标题逐字为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。`sections`和`meta.section_titles`只用于接收历史输入，渲染器不使用它们改名、删减、合并或重排；历史`research`和`next_steps`字段只兼容读取，不控制公开结构；`risk`始终位于报告末尾。`recommendation`表现Reporter冻结的选择理由、适用条件、不适用情形和主要权衡。公开`DesignerInput.html_report_layout`只支持`continuous`且只适用于HTML Report。`meta.layout`是Designer内部连续版的`brief`表现值，不是公开输入字段。

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
| `partial` | 仅部分输入或结果可复核 | 只显示部分接入状态与已确认边界，不冒充完整结果 |
| `pending` | 数据或运行仍待接入 | 只显示待接入说明 |
| `not_run` | 本次任务未运行模块 | 只显示未运行说明 |
| `unsupported` | 当前结构或引擎不支持 | 只显示不支持原因 |
| `failed` | 已运行但失败 | 只显示失败摘要，不显示伪结果 |

`payoff`使用`report_svg_path`、`terms`、`formula`、`scenarios`和`note`。`report_svg_path`只接受Reporter从本次Payoffer运行结果派生的报告专用SVG；找不到有效派生图时，模板只显示“待补齐收益图”，绝不回退到默认样图。

`pricing`使用`method`、`valuation_date`、`metrics`、`greeks`、`assumptions`、`scenario_rows`和`charts`。`greeks`固定按`Delta`、`Gamma`、`Vega`、`Theta`、`Rho`输出；五项均展示，未提供或不适用时写出真实公开状态。`metrics`、`greeks`、情景表和图表只有在`status=ready`时才会渲染。

`charts`支持`line`、`bar`和`heatmap`。热力图用于二维Spot×剩余期限的Greek曲面，字段为`x`、`y`、`data`、`x_axis_name`、`y_axis_name`、`z_axis_name`和`source_note`，其中每项`data`为`[x索引,y索引,已冻结数值]`。所有图表都有读屏摘要与完整展开的数据表；Tooltip、坐标轴和数据表使用同一展示格式，不允许输入颜色覆盖Designer主题。

`backtest`使用`window`、`entry_rule`、`metrics`、`card_metrics`、`detail_tables`、`limitations`和`charts`。`metrics`固定保留样本数、胜率、平均收益、最大亏损；`card_metrics`由Reporter按产品`metric_profile`显式投影四项产品专属统计，Card与Report均展示，确保Card是同一事实的简版。`detail_tables`可承载公共回测、路径事件、监控、路径结果、年度、标的表现及产品专属统计。标准图表顺序为收益率分布、路径结果分布、年度表现、事件触发率；未提供净值曲线时必须说明未生成原因，不得伪造净值图。所有统计必须来自逐笔回测结果聚合，且收益率分母须在`parameters.backtest_input`中披露。

## 4. 参数与风险

`parameters`可包含`payoff_input`、`pricing_input`、`backtest_input`。每项都是参数行数组，行字段为`cn`、`en`、`symbol`、`value`、`source`。PayoffInput必须是PricingInput和BacktestInput的严格子集。渲染后的参数表将`source`机器键显示为中文来源标签，JSON仍保留机器键供校验。

`risk`使用`items`和`disclaimer`。没有风险文本时，输出会明确标记为内部草稿，禁止外发。

## 5. 展示格式与Card边界

Designer只改变读者格式，不改写冻结事实。普通数值、指标、Greek、参数表、事件统计和图表数值最多保留两位小数，使用千分位并移除无意义尾零；百分比同样最多两位并保留`%`，例如`34.234%`显示为`34.23%`，`30.00%`显示为`30%`。日期、代码、公式、原始JSON和事实哈希不参与数值格式化。

Card固定为210mm宽度、高度随完整内容自然延展，无损益图、无交互图。其内容是同一Report事实的简版：推荐结构、挂钩标的、推荐依据、估值日与方法、最多四项核心估值指标、完整五个Greeks、四项核心回测指标、四项产品专属回测指标及最多2项风险提示。没有已冻结事实时显示“未提供”，不填默认数字。PDF导出使用A4自然分页，不因复杂期权结构或完整指标而拒绝交付。

Report固定为连续A4正文并完整展示七个章节，不提供目录版。在“估值定价”中展开估值方法、估值日、全部估值指标、五个Greeks、假设、风险曲线、Greek曲面与已提供定价情景。在“历史回测”中展开样本定义、核心统计、路径事件、产品专属统计、年度统计、标的表现与全部已提供图表。

## 6. 渲染校验

- `Payoff`、`Pricing`、`Backtest`的状态只能是`ready`、`partial`、`pending`、`not_run`、`unsupported`或`failed`。
- 图表序列长度必须与横轴一致。图表不提供缩放、滚动或折叠控件；全部冻结数据以完整表格随图表直接展示，确保HTML可直接转为PDF。
- 参数来源只能是`template_default`、`user_override`、`user_selection`、`market_fixing`或`schedule_derived`。
- 当PayoffInput、PricingInput和BacktestInput同时存在时，渲染器校验PayoffInput是后两者的子集。

## 7. 证据边界

- 从同一`ReportUnit`或同一任务运行快照取数，不得在报告渲染时重估、重算Greeks或重跑回测。
- 绝对价格条款、相对价格条款、日频近似、缺失数据处理和收益率分母必须按实际输入披露。
- 收益图、估值、回测三者分别引用本次任务的资产和结果，不用默认样图或案例数据替代。
