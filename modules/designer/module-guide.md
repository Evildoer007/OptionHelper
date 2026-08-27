# Designer模块指南

## 职责

Designer从唯一`design_tokens.py`构建设计系统，并将Reporter通过公开Tool交接的
冻结报告IR表现为研究简报、参考报价或完整研究报告。多份报告的排序与拆分由Reporter负责；
Designer只控制表达，不重新取数、计算、排序或改变金融事实。

正式横向对比沿用相同公开类型：`card+comparison`自动生成MultiCard，
`report+comparison`自动生成MultiReport。MultiCard不含图表；MultiReport复用
标准Report的目录、离线图表、数据表和PDF静态投影。

## 公共入口

```python
from modules.designer import build_design_system, call_tool, render
from modules.designer.models import DesignerInput

artifact = render(DesignerInput(
    payload=payload,
    output_type="report",       # card、quote或report
    format="html",              # html或pdf
    template_id="report-standard",
    presentation_patch=None,      # 仅用户明确要求时传入一次性展示调整
))
```

跨模块调用只使用公开Tool入口，不导入Designer内部实现：

```python
artifact_response = call_tool({"action": "render", "payload": payload})
```

`load_designer_config()`是唯一运行时配置入口，负责校验当前Tool调用允许
的资源模式、PDF输出和设计系统版本声明。它不存放颜色、字体或组件规则，
这些仍只由`design_tokens.py`管理。Tool请求可显式传入`config`对象，未知
字段或不合法策略会以`invalid_configuration`拒绝。可用策略字段为
`default_asset_mode`、`allow_portable_assets`和`allow_pdf`。

`build_design_system()`返回`design_system_id`、令牌哈希、CSS变量、
组件规则、离线ECharts主题和Payoffer SVG主题。`render()`返回HTML及设计
系统信息；请求PDF时在安装ReportLab的运行环境生成PDF，缺少依赖会明确
返回错误，不会把HTML伪装成PDF。

返回清单包含`semantic_fact_hash`、`presentation_input_hash`和`artifact_hash`。
前者只标识输入事实，展示输入哈希标识版式与设计系统选择，产物哈希标识
最终HTML或PDF；切换研究简报和完整研究报告不会改写输入事实。完整研究报告固定为连续版。portable HTML
同时返回带相对路径、媒体类型、SHA-256和Base64内容的资源清单，调用方无需
读取Designer内部目录即可完成离线交付。无图报告不加载或打包ECharts；shared
报告仍在产物清单登记固定资源路径和SHA-256。Tool请求不得覆盖ECharts物理路径
或传入外链。含实际可见ECharts图表的PDF由Designer内部静态图渲染器投影；
无法完整投影时明确拒绝，禁止静默输出缺图文件。

## 设计边界

统一DataFetcher、Payoffer、Pricer、Backtester、Reporter五个模块页面、
OptChat、OptDesk、研究简报、完整研究报告、ECharts和Payoffer SVG的字体、字号、光大
红金白颜色、间距、数学排版、状态与错误表达。正式产物必须离线可打开，
不依赖CDN或外链字体。

## 输入与输出

输入为Reporter交接的冻结`optionhelper.designer-payload`；`schema`必须存在且逐字匹配，其他值及缺失值直接拒绝，不做迁移或降级。设计系统公开描述固定为`optionhelper.design-system`。输出HTML/PDF、主题信息和渲染清单。
事实冲突或字段结构不完整时退回Reporter。没有可读者展示事实的内容块直接省略，
不以“未提供”或默认结论填充。Designer不扫描
运行目录、不选择最新结果、不调用计算模块。

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

## 交互边界

- Designer不向用户追问研究条件、产品选择或计算参数；缺失事实统一退回Reporter一次汇总。
- Designer不重新打开统一就绪门禁，不检查或读取数据凭据，也不触发取数、估值或回测。它只接受Reporter已经冻结的事实、交付形式和覆盖状态。
- 标准Card无损益图，不渲染损益图。完整研究报告按已有正式结果选择必要图表，图内不重复放标题，标题和图注由报告版式统一管理。用户明确要求追加图表时，Reporter必须先把所需数据和图表定义冻结进本次Payload，Designer不能通过`presentation_patch`补造。
- 标准Quote只渲染Reporter冻结的`reference_quote`表，不把估值或回测结果解释为报价。用户明确要求的补充文本、指标、表格或公式必须先由Reporter冻结；Quote仍不展示图表。
- 标准Card为210mm宽度、高度随完整内容自然延展，不保留空白占位；默认展示结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险。PDF导出按A4自然分页，不因复杂结构或完整指标而拒绝交付。
- 内建完整研究报告为连续A4正文，宽屏HTML提供左侧目录，PDF不显示目录；单结构默认阅读顺序为核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。Card和Quote同样保留各自标准结构。标准结构是无额外要求时的默认值，不是不可突破的上限。
- `assets/templates/*.template.json`是实际运行时模板定义。模板只能选择、命名和排序已有内容块，不能嵌入HTML、CSS、公式或数值；通过`template_id`选择。一个内容块在同一模板中只能出现一次。内建单结构和Multi模板共用同一主题与组件；`assets/examples`中的MultiCard、MultiReport由正式渲染链生成，不维护第二套CSS或展示逻辑。
- 只有用户明确要求单次调整时才传入`presentation_patch`。它可排序、改名或隐藏当前章节，也可从Payload的`supplemental_sections`中加入一个已冻结补充章节；补充章节支持文本、指标、表格、公式，Report还支持图表。Patch只选择展示结构，不携带或改写金融事实，也不修改标准模板。颜色、字体、间距、表格规范和图表主题不可覆盖。
- 内容块没有可展示事实时直接省略；上游明确交接的部分完成或失败说明可以作为真实状态呈现。模板不得自行增加内部说明、审计内容、默认风险或重复章节。
- 产物正文不出现系统品牌、工具名、运行标识、文件路径、审计过程或内部机器字段。
- Designer不自行选择输出目录；产物只由Reporter写入安装目录之外的项目Store。

## 用户可见进度

以下文案直接面向用户，不包含Tool名、JSON字段、运行标识、环境名称或物理路径。开始提示一次；渲染和产物校验明显耗时时才发送进度，进度最多更新1至2次，不要求用户回复，完成后直接给结果。Designer通常承接Reporter已发出的报告进度；同一阶段已有提示时不重复发送。

组合交付中由Reporter统一发报告阶段进度，Designer只在独立渲染诊断时使用以下文案。

- 开始：报告事实已经确认，我现在按既定版式生成交付文件。
- 进度：主体版式已经完成，正在检查图表、公式和离线可用性。
- 完成：交付文件已经生成，版式和资源完整性检查通过。
- 失败：当前渲染能力不足以完成所需格式。我会说明受影响的交付形式，不输出缺图、缺公式或伪装格式的文件。
