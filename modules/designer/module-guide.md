# Designer模块指南

## 职责

Designer从唯一`design_tokens.py`构建设计系统，并把Reporter交接的冻结
`ReportUnit`或已整理的`designer_payload`表现为研究简报或完整研究报告。多Unit的
`ReportBundle`排序与拆分仍由Reporter负责；Designer只控制表达，不重新取数、
计算、排序或改变金融事实。

## 公共入口

```python
from modules.designer import build_design_system, call_tool, render
from modules.designer.models import DesignerInput

artifact = render(DesignerInput(
    payload=designer_payload,
    output_type="report",       # card或report
    format="html",              # html或pdf
    html_report_layout="continuous",  # Report HTML固定连续版
))
```

跨模块调用只使用公开Tool入口，不导入Designer内部实现：

```python
artifact_response = call_tool({"action": "render", "payload": designer_payload})
```

`load_designer_config()`是唯一运行时配置入口，负责校验当前Tool调用允许
的资源模式、PDF输出和设计系统版本声明。它不存放颜色、字体或组件规则，
这些仍只由`design_tokens.py`管理。Tool请求可显式传入`config`对象，未知
字段或不合法策略会以`invalid_configuration`拒绝。可用策略字段为
`default_asset_mode`、`allow_portable_assets`、`allow_pdf`和
`require_declared_design_system_version`。

`build_design_system()`返回`design_system_version`、令牌哈希、CSS变量、
组件规则、离线ECharts主题和Payoffer SVG主题。`render()`返回HTML及设计
系统信息；请求PDF时在安装ReportLab的运行环境生成PDF，缺少依赖会明确
返回错误，不会把HTML伪装成PDF。

返回清单包含`semantic_fact_hash`、`presentation_input_hash`和`artifact_hash`。
前者只标识输入事实，展示输入哈希标识版式与设计系统选择，产物哈希标识
最终HTML或PDF；切换研究简报和完整研究报告不会改写输入事实。完整研究报告固定为连续版。portable HTML
同时返回带相对路径、媒体类型、SHA-256和Base64内容的资源清单，调用方无需
读取Designer内部目录即可完成离线交付。无图报告不加载或打包ECharts；shared
报告仍在产物清单登记固定资源路径和SHA-256。Tool请求不得覆盖ECharts物理路径
或传入外链。含实际可见ECharts图表的PDF在静态图渲染器
接入前会明确拒绝，禁止静默输出缺图文件。

## 设计边界

统一DataFetcher、Payoffer、Pricer、Backtester、Reporter五个模块页面、
OptChat、OptDesk、研究简报、完整研究报告、ECharts和Payoffer SVG的字体、字号、光大
红金白颜色、间距、数学排版、状态与错误表达。正式产物必须离线可打开，
不依赖CDN或外链字体。

## 输入与输出

输入为冻结ReportUnit或Reporter整理后的Designer payload及设计系统版本；输出HTML/PDF、主题信息
和渲染清单。事实冲突、缺字段或状态不完整时退回Reporter。Designer不扫描
运行目录、不选择最新结果、不调用计算模块。

## 交互边界

- Designer不向用户追问研究条件、产品选择或计算参数；缺失事实统一退回Reporter一次汇总。
- Designer不检查模型配置或数据凭据，也不触发取数、估值或回测。它只接受Reporter已经冻结的事实、交付形式和覆盖状态。
- 研究简报不渲染损益图。完整研究报告按已有正式结果选择必要图表，图内不重复放标题，标题和图注由报告版式统一管理。
- 研究简报固定210mm宽度、高度随完整内容自然延展，不保留空白占位；不复用完整报告七段章节，无损益图或交互图。它只呈现推荐结构与标的、推荐依据、已冻结的估值定价、已冻结的历史回测和最多2项风险提示；PDF导出按A4自然分页，不因复杂结构或完整指标而拒绝交付。
- 完整研究报告只有连续A4正文一种版式。Designer固定且逐字输出以下七个章节，任何输入不得改名、删减、合并或重排：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。不提供目录版；选择理由、适用条件、不适用情形和主要权衡归入“结构推荐”，Designer不得自行推导或补写。
- `sections`与`meta.section_titles`仅作为历史输入兼容字段，不能控制公开报告结构。章节没有可用事实时仍保留，并显示真实空状态或模块状态。
- 模板不得自行增加内部说明、审计内容或重复章节。
- 产物正文不出现系统品牌、工具名、运行标识、文件路径、审计过程或内部机器字段。
- Designer不自行选择输出目录；产物只由Reporter写入安装目录之外的项目Store。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；渲染和产物校验明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。Designer通常承接Reporter已发出的报告进度；同一阶段已有提示时不重复发送。

组合交付中由Reporter统一发报告阶段进度，Designer只在独立渲染诊断时使用以下文案。

- 开始：报告事实已经确认，我现在按既定版式生成交付文件。
- 进度：主体版式已经完成，正在检查图表、公式和离线可用性。
- 完成：交付文件已经生成，版式和资源完整性检查通过。
- 失败：当前渲染能力不足以完成所需格式。我会说明受影响的交付形式，不输出缺图、缺公式或伪装格式的文件。
