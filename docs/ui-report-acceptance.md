# 界面与报告验收

验收日期：2026-09-10。以下区分生成链路、浏览器渲染、原生App和平台安装包证据；测试输出位于本地`result/ui-report-acceptance/`，不属于正式市场报告。

## 1. 本次修复

- HTML预览及下载在验证报告归属和资源哈希后，将报告自身的ECharts资源嵌入响应。原生预览的sandbox使外部相对脚本无法可靠加载，现保留隔离策略并消除这项外部依赖；冻结文件与证据不变，历史报告也适用，已有内联脚本不重复嵌入。
- 实时事件保留主Agent路由字段，思考与正文增量可在任务结束前显示。排队消息可以提升为下一条执行，复用原操作；必须等待当前操作取消确认。
- 模型菜单宽度跟随按钮，自定义模型按钮不再继承原生select预留的31px箭头空间，左右内边距均为11px；附件、预设及排队图标使用统一中性色。数据请求侧栏的“选择字段”采用主题正文色。运行详情增加底部留白，展开时滚动到可见范围。
- 已回答的澄清卡片收起旧选项，保留问题和对应回答。结构化运行结果不再显示为`[object Object]`。
- 计算工具公开必填参数并在执行前校验，避免无参数调用Payoffer和Backtester。Payoffer不再暴露网关拒绝的identity字段，并与真实网关合同一起检查。Pricer显式公开volatility_override，要求保留用户指定的波动率，避免模型无意采用历史默认值。校验通过不代表远端行情服务或每个产品计算必然成功。
- 直接计算按任务归集分析、按产品绑定候选；报告支持明确选择产品，多个不同候选对应同一产品时不会任意选择。正式结果哈希和合同一致性校验继续执行。
- 预设菜单区分单智能体与多智能体分组，浮层使用不透明背景；禁用附件仍保持与相邻图标相同线色。
- 报告库预览、下载、编辑及运行停止按钮改为中性小图标，保留悬浮提示、无障碍名称和真实操作；状态改用勾或警示图标。账号名称右移4px。macOS已有标题栏收起按钮，因此隐藏面板重复关闭按钮；浏览器和Windows保留网页关闭入口。
- 嵌入模块在窄窗口把来源区放在顶部，参数区保持可见；极窄窗口先显示参数，再显示结果。报告来源与运行选择器使用简短标签，完整时间和条款保留在提示及详情；不存在回测结果时关闭并禁用对应纳入选项。
- 聊天框使用局部柔和阴影，四边均为1px，无内阴影；布局外层保持透明。新版浮窗检查替代旧的无阴影要求，旧测试文件保留。
- 附件增加RTF、PPTX、XLS、TSV、HTML、JSON、XML、YAML和OpenDocument正文提取，扩展名与Windows通用MIME类型统一归类；解析器与版本同步加入macOS和Windows冻结运行时及许可证清单。RTF采用[striprtf](https://github.com/joshy/striprtf)。
- MultiCard条款保留说明列，期限单位与归一化口径不会丢失。

## 2. 五种HTML生成与离线检查

使用受控历史数据、显式20%波动率假设和真实解析定价引擎，生成看涨、看跌两个产品的冻结结果，经Reporter与Designer正式接口生成，再由隔离的App HTTP下载接口导出。该夹具不访问用户现有任务，也不代表实时行情。

|格式|生成及下载|可渲染图表数量|用途|
|---|---|---:|---|
|Card|通过|0|单产品条款与估值摘要|
|Report|通过|10|单产品分析及Greeks曲线|
|Quote|通过|0|报价条款表|
|MultiCard|通过|0|多产品摘要比较|
|MultiReport|通过|20|两项产品的详细分析|
|用户原报告修复副本|通过|10|保留原报告已有计算结果|

Card、Quote与MultiCard属于表格模板，不为凑数添加图表。原报告已有10张定价图；未成功运行的收益结构和回测不会补造结果。

Chromium直接在离线环境打开本地HTML；WebKit使用本地路由提供同一文件字节并拒绝所有额外请求。两引擎均检查1280px及860px宽度、全部图表挂载、目录锚点、横向溢出和JavaScript错误。均无外部资源请求、脚本错误或缺失图表。WebKit的路由检查不等于原生App或Windows实机验收。

本地独立验收文件：

- `products/app/tests/test_five_html_offline_acceptance.py`：2项通过。
- `products/app/tests/five_html_offline_render.test.cjs`：两浏览器通过，逐一检查六个文件。
- `products/app/tests/test_compute_tool_argument_contract.py`与`test_stream_scope_and_followup_priority.py`：16项通过；直接计算绑定与五种模板选择另有8项独立检查。
- `products/app/tests/chat_live_followup_regressions.test.cjs`与`ui_surface_floating_composer.test.cjs`以及模型按钮、预设分组与图标操作检查及模块布局检查加上独立浮窗检查：14项通过，包含实时增量、已有消息转引导、模型菜单、详情可见范围、澄清历史和浅深主题。

另用App报告路由的实际CSP检查Report、MultiReport及原报告修复副本，Chromium和WebKit均通过全部图表渲染，未放宽sandbox隔离策略。

## 3. 平台与安装包

macOS与Windows共用前端、计算模块、报告模板和下载实现。macOS使用WKWebView，Windows使用WebView2；系统字体、标题栏与文件选择器由平台负责，不承诺系统装饰像素完全相同。

- macOS输出：`dist/OptionHelper-v0.1.0.alpha-macOS-arm64.dmg`。
- Windows输出：`result/windows-candidate/OptionHelper-v0.1.0.alpha-Windows-x64.exe`，为安装程序，不是开发运行器。
- `packaging/tests/test_windows_current_alignment.py`与`modules/designer/tests/test_windows_text_output_contract.py`共15项通过，覆盖路径、深层报告资源、运行目录隔离及文本输出。
- 该Mac没有Windows运行环境，未执行Windows安装程序。Windows首次安装、WebView2启动、设置、对话、五种HTML生成下载和升级仍须实机验证。
- 原生App安装与交互证据单独记录在本地`result/ui-report-acceptance/native-acceptance.md`，只有实际完成才记录通过。

## 4. 回归范围与已知限制

本轮并未宣称全仓库测试全部通过。扩大运行的旧Designer/Reporter测试存在16项失败，涉及旧版多产品布局、标题和已变化的报告夹具字段；旧对话测试有2项失败，分别为旧提示词全文断言和无参数Pricer模拟调用。Windows旧夹具另有3项失败，使用过时安装包命名或非当前版本号。保留这些测试，没有通过修改旧断言来制造全绿结果。

这些旧测试仍需按当前合同逐项维护；本记录的通过结论仅覆盖上述独立生成、下载和交互验收，不能外推为所有产品、外部数据服务和Windows原生运行均已验收。

## 5. 最新安装与附件范围

macOS最新构建d3c12f42ba4dd962e6cd2b6704f580d9702196779aaea881b6f6cba5dd2ad1c7已完成DMG校验、安装及签名验证，App内已看到新的聊天框浮窗效果。此次安装后的前端8个关键文件与源码哈希一致。安装后的README与本文新增说明属于后续文档更新，不宣称已写入该构建快照。

附件格式与边界32项、前端选择器1项、HTTP上传与对话附件投影7项通过；Windows源码与输出合同15项通过。MD、RTF、PDF从原生文件选择器上传并由模型读取的完整复验在用户切换任务时中断，尚未记为通过。此前实际生成的五种HTML已通过两引擎离线检查；新版原生保存面板下载尚未完成复验。

RTF、PPTX、XLS、TSV、HTML、JSON、XML、YAML和OpenDocument均提取正文。扫描PDF没有OCR，旧版DOC/PPT需转换为DOCX/PPTX；不将这些情况标记成已读成功。
