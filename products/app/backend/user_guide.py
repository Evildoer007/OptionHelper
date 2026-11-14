"""Role-specific, locally bundled user documentation for the App help panel.

Content is selected using the authenticated role, never a client role parameter.
Examples are instructions to try; no model calls or financial results are fabricated.
"""
from html import escape
from .authorization.roles import Role


def section(key, title, body):
    return {"id": key, "title": title, "body": body.strip()}


def workflow_diagram(key, title, nodes, paths, height=250):
    """Accessible offline SVG; labels also participate in help search."""
    boxes = []
    for index, (x, y, width, label) in enumerate(nodes):
        boxes.append(f'<rect x="{x}" y="{y}" width="{width}" height="48" rx="10"/>')
        boxes.append(f'<text x="{x + width / 2}" y="{y + 29}">{escape(label)}</text>')
    routes = ''.join(f'<path d="{route}" marker-end="url(#guide-arrow-{key})"/>' for route in paths)
    return (f'<figure class="user-guide__diagram" data-flow="{key}"><figcaption>{escape(title)}</figcaption>'
            f'<svg viewBox="0 0 640 {height}" role="img" aria-labelledby="guide-diagram-{key}">'
            f'<title id="guide-diagram-{key}">{escape(title)}：' + '，'.join(escape(n[3]) for n in nodes) + '</title>'
            f'<defs><marker id="guide-arrow-{key}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9"/></marker></defs>'
            f'<g class="user-guide__routes">{routes}</g><g class="user-guide__nodes">' + ''.join(boxes) + '</g></svg></figure>')


RESEARCH_DIAGRAMS = (
    workflow_diagram("single", "单智能体：按需调用，再核对结果", [
        (20,20,120,"研究需求"),(240,20,160,"主智能体"),(500,20,120,"结论与交付"),
        (210,132,220,"资料、行情与计算工具")],
        ["M140 44 H240","M400 44 H500","M280 68 V132","M360 132 V68"],200),
    workflow_diagram("loop", "产品交易循环：方案经过检验与修订", [
        (20,30,150,"设计方案"),(245,30,150,"交易与风险检验"),(470,30,150,"复核结论"),
        (245,142,150,"按反馈修订")],
        ["M170 54 H245","M395 54 H470","M320 78 V142","M245 166 H95 V78"],215),
    workflow_diagram("council", "独立评议：分支研究，汇总分歧", [
        (20,80,140,"明确条件"),(245,20,150,"产品匹配"),(245,140,150,"风险研究"),
        (480,80,140,"汇总与复核")],
        ["M160 104 H200 V44 H245","M200 104 V164 H245","M395 44 H440 V104 H480","M395 164 H440 V104"],215),
    workflow_diagram("ranking", "约束排序：有证据的指标才能进入排序", [
        (20,20,140,"明确筛选条件"),(250,20,140,"生成候选"),(480,20,140,"验证指标"),
        (480,140,140,"排序与审查"),(250,140,140,"缺项则补算或排除")],
        ["M160 44 H250","M390 44 H480","M550 68 V140","M480 164 H390","M320 140 V105 H550 V68"],215),
)

ENTRY_DIAGRAM = workflow_diagram("entry", "同一个任务，两种操作入口", [
    (20,78,140,"当前任务"),(235,18,170,"OptChat对话研究"),(235,138,170,"OptDesk模块操作"),
    (480,78,140,"运行记录与报告")],
    ["M160 102 H200 V42 H235","M200 102 V162 H235","M405 42 H445 V102 H480","M405 162 H445 V102"],215)


CHAT_SECTIONS = (
    section("start", "1. 开始使用", """
<p>先配置模型，再按需配置行情。已有可用配置时，可以直接新建任务。</p>
<h3>1.1 配置模型</h3>
<ol>
  <li>点击左下角用户名，进入<strong>设置中心</strong>的<strong>模型配置</strong>。</li>
  <li>用<strong>添加Provider</strong>选服务商，自建接入时选择<strong>添加自定义Provider</strong>。填写该服务商的<strong>API密钥</strong>。</li>
  <li>展开<strong>自定义设置</strong>，核对<strong>API地址</strong>。点击<strong>获取模型</strong>或<strong>添加模型</strong>，填写实际模型ID并至少启用一个模型。</li>
  <li>点击<strong>保存并测试</strong>。需要计算时，确认所选模型的工具调用能力也已通过测试。</li>
</ol>
<h3>1.2 配置行情</h3>
<p>在<strong>设置中心</strong>的<strong>数据接口</strong>配置实际使用的来源。选择iFinD时填写Refresh Token。选择Tushare时，先在<strong>接入服务</strong>选Tinyshare或Tushare官方，再填写对应Token。两种Token不能混用。</p>
<p>点击<strong>保存并测试</strong>。连接通过后，能否取得具体标的、字段和历史区间仍由数据账号权限决定。只做产品解释时可以先跳过行情配置。</p>
<h3>1.3 开始第一个任务</h3>
<p>返回OptChat，点击<strong>新建任务</strong>，在输入框下方选择已启用的模型，发送一条简单的研究需求。</p>
<blockquote data-example>请解释看涨期权和牛市看涨价差的区别，说明成本、收益上限和亏损情形。先不获取行情、不计算。</blockquote>
<h3>1.4 连接未通过</h3>
<p>根据测试提示核对API地址、凭据、模型ID或行情权限，再重新测试。密钥只填在设置中心。取数和报告问题可查第10章。</p>
"""),
    section("task", "2. 界面与任务管理", """
<p>任务保存对话、参数、运行和报告。同一研究继续原任务，不同客户或独立研究新建任务。</p>
<h3>2.1 新建与整理任务</h3><ol><li>点击<strong>新建任务</strong>先进入空白草稿；真正发送第一条消息后才创建任务，未发送就离开不会新增空任务。名称会根据首条输入生成。</li><li>通过任务管理菜单重命名，建议名称包含标的和研究主题，例如中证500三个月看涨结构研究。</li><li>常用任务可以置顶。切换历史任务后，先核对当前任务，再继续输入。</li></ol>
<p>删除任务会移除其对话入口；已经导出的文件需要在保存位置另行管理。不要通过删除任务尝试终止正在运行的计算。</p>
<h3>2.2 在原任务中修改条件</h3><p>说明修改对象、保留条件和重算范围。多个版本并存时，明确产品、期限和结果日期。</p>
<blockquote data-example>沿用当前看涨价差的标的和其他条款，把期限改为六个月，重新估值并比较变化。暂不重新回测。</blockquote>
<p>旧结果继续对应原输入。条款修改后，需要运行受影响的分析；已经生成的报告不会自动更新。</p>
"""),
    section("ask", "3. 单智能体与多智能体", """
<p>在OptChat输入框下方选择<strong>推荐预设</strong>和研究深度。已经确定产品的解释或单项计算，通常用单智能体即可。需要多角色设计、评议或排序时，再选多智能体预设。</p>
<h3>3.1 说明研究条件</h3>
<table><thead><tr><th>条件</th><th>需要说明的内容</th></tr></thead><tbody><tr><td>研究对象</td><td>标的代码或名称、产品名称；多标的逐一列出。</td></tr><tr><td>期限与日期</td><td>投资期限、合同起始日，以及估值日或回测入场区间。</td></tr><tr><td>观点与约束</td><td>市场判断、可承受损失、收益偏好、是否接受收益封顶。</td></tr><tr><td>所需结果</td><td>产品解释、结构推荐、收益分析、定价、反解、回测或报告。</td></tr></tbody></table>
<blockquote data-example>研究挂钩000905.SH、期限三个月的结构。我判断市场温和上涨，希望比较收益封顶和不封顶的方案。先解释收益与下跌风险，列出待确认条款，暂不计算。</blockquote>
<h3>3.2 明确要交付什么</h3><table><thead><tr><th>功能</th><th>回答的问题</th></tr></thead><tbody><tr><td>产品解释与收益结构</td><td>什么价格或观察路径会触发现金流，如何结算。</td></tr><tr><td>估值与Greeks</td><td>给定条款和市场假设下，合同价值及敏感度是多少。</td></tr><tr><td>公平参数反解</td><td>调整哪项可解条款，才能满足指定定价目标。</td></tr><tr><td>历史回测</td><td>同一组条款在历史入场样本中的结算表现如何。</td></tr><tr><td>研究报告</td><td>如何将选定资料和已完成结果整理成可阅读、可导出的文件。</td></tr></tbody></table>
<h3>3.3 选择推荐预设</h3><p>单智能体与各多智能体预设均可按需调用工具。各角色使用的模型可在<strong>设置中心</strong>的<strong>智能体预设</strong>查看和配置。</p>
<table><thead><tr><th>预设</th><th>适用情况与工作方式</th></tr></thead><tbody><tr><td>产品交易循环</td><td>需要打磨产品方案。先设计，再由交易角色检查估值与风险；必要时修改，最后复核。</td></tr><tr><td>独立评议</td><td>需要比较不同判断。两条研究分支分别分析，随后汇总分歧并补充验证。</td></tr><tr><td>约束排序</td><td>需要按明确规则比较候选。先检查所需指标，再执行排序。应说明排序指标、方向和缺失指标如何处理。</td></tr></tbody></table>
""" + "".join(RESEARCH_DIAGRAMS) + """
<h3>3.4 选择研究深度</h3><p>快速、标准、深度控制补充研究的预算上限。定价精度由定价参数决定。所选预设和深度用于后续请求。</p>
<h3>3.5 不满意时继续调整</h3><p>继续原任务，说明保留什么、修改什么。重新排序可复用仍适用的指标；新增胜率等要求可能需要补做回测。推荐次数不固定，已有推荐不妨碍继续筛选。</p>
<blockquote data-example>保留当前标的和三个月期限，候选仍不满意。先按Gamma从小到大排序，缺失指标的候选排除；再比较同一入场区间、每月入场的历史正收益样本占比。复用适用结果，补算缺项，不重新取已有行情。</blockquote>
<h3>3.6 核对合同条款</h3><p>选定候选后，核对行权价、障碍、票息、参与率、观察规则和合同日期。对缺失或冲突条件逐项补充；产品默认示例不能替代本次确认的条款。</p>
<blockquote data-example>采用刚才的牛市看涨价差。先列出本次完整条款和缺项，确认后再定价并检查Greeks。</blockquote>
"""),
    section("attachments", "4. 附件与条款确认", """
<h3>4.1 上传资料</h3><ol><li>点击输入框旁的加号添加附件，或将文件拖入附件区域。</li><li>核对文件名称，移除不属于当前任务的材料。</li><li>发送时说明用途，例如提取条款、核对差异或解释表格。</li></ol>
<p>支持PNG/JPEG/WebP/GIF/BMP及单页TIFF图片，PDF、DOCX、XLSX/XLS、PPTX、RTF、Markdown、TXT/LOG、CSV/TSV、HTML、JSON/JSONL、XML、YAML、ODT/ODS/ODP、EML邮件和常见代码文本。EML只提取邮件正文和基本邮件头，不递归读取内嵌附件；HTML不执行脚本或加载外链。</p>
<p>MOV、MP4、M4V、WebM在本机最多抽取4张画面，添加图片和抽帧说明，不转写音频，也不等于完整理解视频。具体编码由系统WebView决定；无法解码时可换兼容编码或自行提供截图。原视频不发送给模型；发送抽帧图片需要模型支持图像。</p>
<p>普通文件单个最多20MB，视频单个最多100MB；每条消息最多20个附件、总计200MB，抽帧生成的图片及说明也占附件名额。扫描PDF没有OCR；多页TIFF请转PDF，旧版DOC/PPT请转DOCX/PPTX。上传成功后仍需核对提取结果。</p>
<blockquote data-example>读取这份条款文件，整理标的、期限、观察日、敲入敲出和票息规则。把无法确认或前后冲突的地方一次列出，暂不计算。</blockquote>
<h3>4.2 核对提取结果</h3><p>逐项检查日期、数值、比较符号和单位。扫描件缺页、图片模糊或表格单位不清时，补充清晰材料或文字说明。附件被读取不等于条款已确认。</p>
<h3>4.3 回答补充问题</h3><p>按照问题填写选择或补充文字，区分年化票息、相对期初价格的障碍比例和实际日期。不确定的条件直接说明待确认；不要为了继续运行而填写没有依据的市场数据。</p>
"""),
    section("running", "5. 运行、停止与恢复", """
<h3>5.1 查看运行状态</h3><p>发送后查看当前任务的状态和运行记录。模型分析、行情获取、Monte Carlo与风险图计算耗时不同。页面已出现部分文字或某个数值时，仍需确认相应运行是否完整结束。</p>

<h3>5.2 停止当前运行</h3><ol><li>点击当前运行的停止按钮。</li><li>等待界面确认取消完成，避免立即重复启动。</li><li>调整条件后，在同一任务中重新发起需要的操作。</li></ol><p>已经完成的记录仍保留。停止不保证得到可用的部分估值。忙碌时继续输入，应以界面显示的排队或提交状态为准。</p>
<h3>5.3 读懂失败原因</h3><p>先看失败的业务步骤，再看原因和修复建议。收益结构、定价、回测或报告的某一步失败，不等于其他已完成结果失效。</p><table><thead><tr><th>提示</th><th>如何继续</th></tr></thead><tbody><tr><td>条款含未登记字段</td><td>核对所选产品及其条款，避免把普通看涨的执行价字段用于价差；让助手按该产品支持的字段重新提交。</td></tr><tr><td>行情不覆盖完整持有期</td><td>补充到期前所需行情，或明确修改入场范围，不能把缺失样本当成零收益。</td></tr><tr><td>报告回测与本轮要求不一致</td><td>核对入场日期、频率及完整合同要求，更新计算和报告来源后重新生成。</td></tr><tr><td>服务未返回具体原因</td><td>保留任务和发生时间后重试；不能仅凭通用提示判断是参数、网络还是模型问题。</td></tr></tbody></table>
<h3>5.4 恢复连接与重试</h3><p>出现连接恢复提示时，先查看任务与交付中的运行状态，后台可能仍在计算。确认失败后，按提示修复条款、行情覆盖或连接，再重试。更换模型只影响后续请求，不会重写旧回复或重算旧结果。</p>
"""),
    section("read-results", "6. 估值、风险与金额口径", """
<h3>6.1 先核对结果来源</h3><p>读取价格前，核对产品与条款、标的、合同起始日、请求估值日、实际估值交易日和行情截止日。非交易日或当日数据尚未可用时，实际采用日期可能不同。</p><p>新发与存续合同需要分别处理。存续的路径型产品还需要已经发生的观察状态，不能只填当前价格。</p>
<h3>6.2 价值与标准误</h3><p>估值是给定模型和市场假设下的合同价值。期初费用是否包含，应按本次现金流口径核对。Monte Carlo标准误描述估计的不确定性，不是未来涨跌区间或最大亏损。未提供、不适用和零是不同状态。</p>
<h3>6.3 Greeks</h3><table><thead><tr><th>指标</th><th>含义与单位</th></tr></thead><tbody><tr><td>Delta</td><td>价值对标的价格的一阶敏感度，同时核对标的坐标和价格单位。</td></tr><tr><td>Gamma</td><td>Delta对标的价格的敏感度。用它估计二阶损益时，还需乘以价格变动的平方及二分之一。</td></tr><tr><td>Vega</td><td>价值对波动率的敏感度，当前按波动率增加1个百分点披露。</td></tr><tr><td>Theta</td><td>按自然日披露的时间敏感度，结合结果符号阅读。</td></tr><tr><td>Rho</td><td>当前按利率增加1个百分点披露的敏感度。</td></tr><tr><td>Vanna</td><td>价格与波动率的交叉敏感度，保留两种风险单位。</td></tr><tr><td>Volga</td><td>波动率的二阶敏感度，按结果中的波动率百分点平方口径阅读。</td></tr></tbody></table>
<p>Greeks描述当前状态附近的局部变化。障碍附近、临近到期或市场大幅变化时，需要重新估值。多标的结果应区分逐资产变化和共同变化，不能直接当作单只资产的对冲数量。</p>
<h3>6.4 标准化结果与名义本金</h3><p>App内部使用100作为标准化基准。它不是金融基点；1个基点等于0.01个百分点。名义本金用于将适用的标准化结果显示为金额，不改变模型或计算精度。</p>
<ol><li>在对话中说明本任务采用的<strong>名义本金</strong>，默认100万元人民币。</li><li>查看回复，确认已保存的本金与币种，再要求展示适用结果的金额。</li><li>同一任务的定价和回测共用本金，其他任务独立。需要仅保留原口径时，可要求清空本任务的规模设置。</li></ol>
<blockquote data-example>本任务名义本金使用200万元人民币。请按当前已验证估值说明金额和Greeks单位。</blockquote>
<p>单位示例：普通合同价值为2.5%，名义本金100万元，对应价值2.5万元。金额Greeks按照各自的风险单位换算；Vega已经按波动率增加1个百分点披露时，不再额外除以100。</p>
<h3>6.5 特殊单位与报告金额</h3><p>9.4方差互换使用方差名义金额，表示每一个方差百分点平方对应的金额，与普通投资本金不同。币种是结算单位声明，App不会因此进行外汇转换。</p><p>改变本金不重跑已完成的百分比计算。新生成的正式报告会保存当时的本金和金额附表；旧报告保持原金额，不随当前参数变化。</p>
"""),
    section("backtest-reading", "7. 历史回测结果解读", """
<p>回测在多个历史入场日分别套用同一组条款，逐笔回放至到期或有效提前终止，再汇总合同结算结果。</p>
<h3>7.1 先核对样本</h3><p>先确认实际入场区间、入场规则、标的及条款。再看有效样本数、排除样本及原因。后续行情须覆盖持有期。有效样本为零时无法计算这组样本的收益统计。</p>
<h3>7.2 从汇总追到逐笔</h3><p>先看合同结算收益率分布和路径事件比例。遇到异常或关心的样本，再核对该笔入场日、出场日、路径分段、事件和结算收益率。按年汇总时，样本归入其入场年份。</p>
<h3>7.3 入场前年化波动率分组</h3><p>用截至入场日的前复权收盘价计算指定交易日窗口的年化波动率。多标的按最高年化波动率分组。行情不足的样本可能仍有结算结果，但会在分组统计中单独标明。</p>
<h3>7.4 比较不同回测</h3><p>对齐条款、标的、区间、入场规则和收益口径，再比较统计。同一段行情可能服务于多笔重叠的合约。历史正收益样本占比和路径事件比例只描述本次历史样本；结算收益率也不包含资金和交易等外部成本。</p>
"""),
    section("delivery", "8. 报告生成与导出", """
<h3>8.1 选择报告类型</h3><table><thead><tr><th>类型</th><th>用途</th></tr></thead><tbody><tr><td>简报</td><td>集中呈现条款、关键结果与风险。</td></tr><tr><td>研究报告</td><td>展开所选分析、计算口径及研究依据。</td></tr><tr><td>参考报价表</td><td>整理指定合同的报价信息和必要条款。</td></tr><tr><td>多产品简报或研究报告</td><td>在一份文件中比较选定的产品或合同版本。</td></tr></tbody></table>
<h3>8.2 从已有结果生成报告</h3><ol><li>确认本任务已有需要写入报告且条款一致的运行结果。说明采用的产品、版本、报告类型和文件格式。</li><li>生成后从<strong>任务与交付</strong>打开，核对合同、日期、样本、图表单位和风险。</li><li>导出并打开实际文件，确认内容和格式。</li></ol>
<blockquote data-example>基于本任务已完成且条款一致的收益分析、估值和回测，生成HTML完整研究报告。保留行情日期、样本范围、计算方法和风险说明。</blockquote>
<h3>8.3 合并多个产品</h3><p>明确列出要比较的产品及合同版本；同一产品存在多个期限或多次运行时，说明采用哪次结果。报告可以复用本任务已有的适用结果，不需要仅为合并或换格式而重新计算。</p><blockquote data-example>把本任务六个月看涨期权与六个月看涨价差的最新有效结果合成一份对比报告。分别保留条款和来源，不重新计算。</blockquote>
<h3>8.4 修改条件后更新报告</h3><p>如果条款、估值日期或回测入场规则变了，先重算受影响的项目，再以新运行生成报告。旧文件保留原合同、样本和名义本金。</p>
<h3>8.5 编辑草稿与转换格式</h3><p>草稿可用于整理尚待验证的分析。只改表述或转换格式时，沿用已核实的数据、结论和金额。</p>
<blockquote data-example>将刚才的研究报告导出PDF，沿用原有事实和金额，不重新取数或计算。</blockquote>
"""),
    section("examples", "9. 完整操作示例", """
<p>以下是操作示例，不包含真实行情、报价或产品推荐结论。</p>
<h3>9.1 只了解产品</h3><blockquote data-example>解释牛市看涨价差的收益机制，说明成本、收益上限和最大损失。再说明到期价格恰好等于两个执行价时如何结算。不要获取行情、定价、回测或生成报告。</blockquote><p>核对解释中的条款、价格区间和端点含义；有具体条款时，补充后再讨论，不把默认示例当作本次合同。</p>
<h3>9.2 从需求到研究报告</h3><p>按上图推进。计算前确认合同条款；估值后核对日期与单位；回测后检查有效样本和排除原因。报告应选择同一合同的有效结果，导出后再检查文件。</p>
<h3>9.3 修改已有研究</h3><blockquote data-example>沿用本任务的看涨价差，把期限从三个月改为六个月，其他条款不变。先列出需要变化的日期和参数，再重新估值。完成后比较新旧结果，暂不更新报告。</blockquote><p>核对新旧结果的合同与日期，确定采用新版本后，再要求更新报告。</p>
"""),
    section("troubleshooting", "10. 常见问题与处理", """
<table><thead><tr><th>问题</th><th>处理顺序</th></tr></thead><tbody><tr><td>不能发送或提示未配置模型</td><td>检查已保存的模型服务、启用模型及当前选择，再查看连接和能力检测。具体配置见第1章。</td></tr><tr><td>行情不足或无法获取</td><td>检查当前数据来源的连接，再核对标的、日期、字段、复权口径和权限。回测还需覆盖入场后的持有期。</td></tr><tr><td>没有价格或报告</td><td>确认已要求执行对应操作，再检查运行状态、缺失条件和失败原因。产品解释与候选推荐不等于定价完成。</td></tr><tr><td>改了参数仍显示旧结果</td><td>明确要求重新计算，完成后选择新运行；报告另行生成或更新。</td></tr><tr><td>同一产品的结果不同</td><td>逐项比较合同、市场日期、波动率、模型、路径数、随机条件和观察状态。</td></tr><tr><td>结果数值很小</td><td>先检查百分比和Greeks单位；需要金额时设置名义本金，不能仅根据数值大小判断计算异常。</td></tr><tr><td>多角色研究未完成</td><td>查看具体失败阶段、模型连接、计算输入和缺失证据。补全条件后再继续，不能把部分角色完成视作整个研究完成。</td></tr><tr><td>报告缺少图表或结果</td><td>核对所选来源是否具备对应计算，是否与合同匹配，再重新生成并检查导出文件。</td></tr><tr><td>某项功能不可用</td><td>阅读入口提示，确认账号权限、产品支持范围及所需数据。权限问题联系负责维护App的人处理。</td></tr></tbody></table>
<p>需要反馈问题时，保留操作步骤、产品与参数、发生时间和错误提示。截图前遮去API密钥及敏感业务资料。</p>
"""),
)

ADMIN_SECTIONS = (
    section("desk", "11. 工作台操作流程", """
<p>OptDesk供admin账号使用。左上角切换OptChat与OptDesk会沿用当前任务。在工作台内，点击右下角可打开该任务的对话。</p>
""" + ENTRY_DIAGRAM + """
<h3>11.1 推荐操作顺序</h3><ol><li>确认当前任务与研究对象，准备所需行情。</li><li>核对合同条款，运行收益结构分析。</li><li>按所需日期定价，或执行支持的公平参数反解。</li><li>按指定历史样本回测。</li><li>选择有效结果，生成并导出报告。</li></ol>
<h3>11.2 参数与结果</h3><p>各模块按需运行。修改条件或切换任务后，先核对当前参数，再更新需要的结果。报告使用生成时选定的运行版本。</p>
<p>定价和回测共用本任务的名义本金及币种。修改后移出输入框保存，点击<strong>金额设置与结果</strong>，按产品和运行时间核对金额。金额口径见第6章。</p>
"""),
    section("datafetcher", "12. 获取与检查行情", """
<h3>12.1 获取数据</h3><ol><li>进入<strong>数据获取</strong>，填写标的代码和起止日期，选择行情字段及复权口径。</li><li>核对<strong>数据来源</strong>和<strong>数据保留</strong>，点击<strong>获取数据</strong>。来源须已在设置中心配置。App优先复用符合请求的本地行情，再补取缺失区间。</li><li>在<strong>已获取数据</strong>核对标的、日期、字段与覆盖状态。</li></ol>
<h3>12.2 查看行情看板</h3><p>在已获取数据中选择记录，查看价格、成交量、历史波动率和回撤。宽窗口可并排阅读更多图表，窄窗口按可用空间排列。估值数据是否可取还取决于所选来源、资产及权限；已有历史行情不等于所有估值字段可用。</p><h3>12.3 选择价格口径</h3><p>合同观察通常需要不复权价格，历史波动率需要符合要求的复权历史价格，两者不能随意替换。交易日历需与观察规则一致，不能用普通工作日代替交易日，也不能用另一标的的历史填补缺失。</p>
<h3>12.4 保存与复用</h3><p><strong>仅本次使用</strong>是默认选项。需要重启后复用新取行情时，选择<strong>保存到本地</strong>。改回仅本次使用会保留以前已保存的数据。下载CSV是单独导出文件。</p>
"""),
    section("payoffer", "13. 分析收益结构", """
<h3>13.1 设置与运行</h3><ol><li>在<strong>收益结构</strong>选择产品。</li><li>核对标的、合同日期、行权价、票息、障碍、参与率和观察规则。</li><li>修改允许编辑的条款，处理冲突或缺项提示，再运行。</li><li>检查结果中的价格区间、开闭端点、触发条件、现金流和提前终止情景。</li></ol>
<h3>13.2 读取收益图</h3><p>产品默认示例用于理解机制，只有当前条款运行后的结果属于本次合同。路径型产品不能仅凭终值图判断结算；还要检查中途观察和触发事件。图中的采样最低或最高收益，不一定是合同在所有可能路径下的最大损失或收益。</p>
<h3>13.3 运行参数与默认资料</h3><p>本次研究的条款修改不应写回产品默认资料。确需维护默认资料时，使用专门入口，核对变更原因、差异和审批信息。</p>
"""),
    section("pricer", "14. 定价与公平参数反解", """
<h3>14.1 普通估值</h3><ol><li>进入<strong>估值定价</strong>，选择产品并核对本次条款。</li><li>填写标的、估值日和合同起始日，区分新发与存续合同；核对期初参考价，多标的按页面顺序逐一检查。</li><li>设置历史波动窗口、无风险利率和分红率，检查界面单位。历史波动窗口按交易日收益计算，当前按244个交易日年化。</li><li>选择产品支持的解析定价或Monte Carlo方法。Monte Carlo填写路径数和随机条件，风险网格决定风险图的情景采样。</li><li>在参数区设置需要的名义本金，运行并等待风险输出完整结束。</li><li>核对价格、标准误、全部Greeks、风险曲线与曲面，确认没有未完成项目。</li></ol>
<h3>14.2 风险曲线与曲面</h3><p>选择Greek查看对应风险分析。先辨认横轴是实际价格、相对期初比例、波动率还是剩余天数，再读取数值。图中节点是给定情景下的估值，不是未来预测；多标的共同变化也不等于单标的风险。</p>
<h3>14.3 公平参数反解</h3><ol><li>切换到反解模式，选择页面为当前产品提供的可用目标。</li><li>明确要求解的条款，核对定价目标和其他固定条件。</li><li>运行后检查解、基准参数、残差、不确定性和正式报价资格。</li></ol><p>未收敛或仅供研究的估计不能当作正式报价。产品不支持某个反解目标时，不能以修改其他条款替代。</p>
<h3>14.4 输入不完整时</h3><p>路径型合同需要真实交易日历及适用观察状态。遇到日历不足、历史观察缺失或方法不支持时，先修复输入；不要通过缩短期限、减少路径或更换模型改变原研究要求。</p>
"""),
    section("backtester", "15. 执行历史回测", """
<h3>15.1 确认合同和可用窗口</h3><p>进入<strong>历史回测</strong>，选择产品、核对条款并填写标的。行情和交易日历足够时，页面会填入自动计算的可用入场窗口。可以修改起始日或截止日。最后一个入场日之后，行情还需覆盖该笔合同的持有期。</p>
<h3>15.2 设置入场和统计规则</h3><ol><li>在<strong>入场规则</strong>选每日、每月或指定日期。选择指定日期后填写入场日，每行一个。</li><li>需要年度统计时勾选<strong>按入场年份汇总</strong>。展开<strong>高级分析</strong>可设置不完整样本处理及入场前年化波动率分组。分组时先选择窗口，再从小到大填写边界。</li><li>点击<strong>运行</strong>，检查有效样本数和排除原因。</li></ol>
<h3>15.3 复核逐笔结算</h3><p>在<strong>逐笔结算与审计</strong>选择一行，核对入场日、出场日、路径事件和合同结算收益率。展开<strong>审计记录</strong>查看合同、数据与运行口径。汇总和分组结果的读法见第7章。</p>
"""),
    section("reporter", "16. 工作台报告操作", """
<h3>16.1 选择来源并生成</h3><ol><li>进入<strong>研究报告</strong>，选择单产品简报、单产品研究报告或其他可用交付类型。</li><li>选择当前任务的合同及已完成运行，核对条款、日期和需要纳入的分析模块。</li><li>处理缺失或不匹配提示，再生成报告。</li><li>在报告库打开，检查条款、图表、单位和风险说明，按可用格式导出。</li></ol>
<h3>16.2 多产品报告</h3><p>选择多产品简报或多产品研究报告。在一个来源中勾选合同和所需运行，再切换来源选择其他合同；已选合同会保留，可在来源详情下移除。</p><p>每个合同分别确认采用的运行版本。跨来源选择时，每份合同至少需要一项已验证计算，不能纳入其他任务或已失效的结果。确认所选模块后生成，App复用已保存结果。</p>
<h3>16.3 修改与重新导出</h3><p>叙述修改与事实变化分开处理。只调整文字或格式时保留原事实；条款、行情或估值日期改变时，先重新运行相关模块，再选择新来源。报告类型与交付核对顺序见第8章。</p>
"""),
    section("settings", "17. 设置与日常维护", """
<h3>17.1 修改配置</h3><p>从账号菜单进入设置中心。新增模型或更新凭据后保存并测试；配置步骤见第1章。研究方式与深度的选择见第3章。</p><h3>17.2 维护记录</h3><p>迁移或清理前分别核对任务、行情和导出文件。切换账号需退出后重新登录；权限问题联系维护人员。</p>
"""),
)


def guide_figure(title, body):
    """Static interface illustrations contain no credentials or customer data."""
    return f'<figure class="guide-visual"><figcaption>{escape(title)}<span>操作示意</span></figcaption>{body}</figure>'


def guide_steps(*steps):
    return '<ol class="guide-steps">' + ''.join(f'<li><strong>{escape(title)}</strong><span>{escape(body)}</span></li>' for title, body in steps) + '</ol>'


def guide_screen(sidebar, title, body):
    return ('<div class="guide-screen"><header class="guide-screen__chrome"><span class="guide-window-dots" aria-hidden="true">● ● ●</span><span>OptionHelper</span></header><aside>' + ''.join(f'<span>{escape(item)}</span>' for item in sidebar)
            + f'</aside><div class="guide-screen__main"><strong>{escape(title)}</strong>{body}</div></div>')


CHAPTER_VISUALS = {
    "start": guide_figure("先连模型，再按需连接行情", guide_screen(
        ["设置中心", "模型配置", "数据接口"], "模型配置",
        '<dl class="guide-fields"><div><dt>API地址</dt><dd>服务商API地址</dd></div><div><dt>API密钥</dt><dd>••••••••</dd></div><div><dt>模型ID</dt><dd>服务商提供的模型标识</dd></div></dl><span class="guide-action">保存并测试</span>')),
    "task": guide_figure("找到任务、输入和交付入口", '<div class="guide-workspace"><aside><b>任务列表</b><span>新建任务</span><span>当前研究</span><span>历史研究</span><small>账号与设置</small></aside><div><header>当前任务<strong>任务与交付</strong></header><p>对话、研究进度与结果</p><footer><b>输入你的问题…</b><span>＋附件　推荐预设　标准　模型</span></footer></div></div>'),
    "attachments": guide_figure("文件进入任务前，先明确用途", guide_steps(
        ("添加资料", "点击加号或拖入文件"), ("说明用途", "提取条款、对比或解释"), ("核对提取", "检查日期、数值和单位"))
        + '<div class="guide-document"><strong>条款提取</strong><dl class="guide-fields"><div><dt>已识别</dt><dd>标的、期限、行权价</dd></div><div><dt>待确认</dt><dd>观察规则、模糊单位</dd></div></dl></div>'),
    "running": guide_figure("看业务状态，决定下一步", '<div class="guide-states"><div><i>进行中</i><strong>等待当前步骤结束</strong><span>需要调整时停止或追加说明</span></div><div><i>待补充</i><strong>补齐问题中的条件</strong><span>继续原任务，保留已有结果</span></div><div><i>部分完成</i><strong>修复具体失败项</strong><span>不把部分完成当成全部交付</span></div><div><i>已完成</i><strong>核对结果与文件</strong><span>检查合同、日期和样本范围</span></div></div>'),
    "read-results": guide_figure("先读口径，再读数值", '<div class="guide-result"><header><strong>估值结果</strong><span>当前合同与估值日期</span></header><div class="guide-result__columns"><div><b>价值</b><span>百分比或金额</span></div><div><b>Greeks</b><span>各自风险单位</span></div><div><b>标准误</b><span>估计不确定性</span></div></div><footer>名义本金影响金额显示，不改变定价精度</footer></div>'),
    "backtest-reading": guide_figure("入场区间与行情覆盖不是同一段时间", '<svg class="guide-timechart" viewBox="0 0 640 220" role="img" aria-label="入场区间之后，行情仍须覆盖最后一笔合同的持有期"><path class="guide-time-axis" d="M138 36H616M138 96H616M138 156H616"/><text x="12" y="42">入场窗口</text><text x="12" y="102">最后一笔合同</text><text x="12" y="162">所需行情</text><rect class="guide-time-entry" x="140" y="22" width="254" height="28" rx="5"/><rect class="guide-time-hold" x="394" y="82" width="206" height="28" rx="5"/><rect class="guide-time-data" x="140" y="142" width="460" height="28" rx="5"/><path class="guide-time-guide" d="M394 16V178M600 76V178"/><text x="140" y="204">开始入场</text><text x="394" y="204" text-anchor="middle">最后入场</text><text x="600" y="204" text-anchor="end">合同终止</text></svg><p>历史正收益样本占比描述这批样本，不代表未来胜率。</p>'),
    "delivery": guide_figure("报告保存的是选定版本", guide_steps(
        ("选来源", "合同及已完成运行"), ("生成新报告", "保存本次事实与金额"), ("打开实际文件", "核对后再导出"))
        + '<div class="guide-version"><span>旧合同／旧运行 → 旧报告保留</span><strong>修改条款 → 重算受影响部分 → 新报告</strong></div>'),
    "examples": guide_figure("按这条路径完成一次研究", guide_steps(
        ("提出需求", "对象、期限、观点与约束"), ("比较候选", "选结构，确认条款"), ("执行研究", "按需分析、定价和回测"), ("检查交付", "核对结果，生成报告"))),
    "troubleshooting": guide_figure("反馈问题时保留这四项", '<dl class="guide-fields guide-diagnostic"><div><dt>在哪一步</dt><dd>数据、收益、定价、回测或报告</dd></div><div><dt>具体原因</dt><dd>复制错误说明，不只写执行失败</dd></div><div><dt>本次输入</dt><dd>产品、日期、参数与数据来源</dd></div><div><dt>发生时间</dt><dd>保留原任务，便于定位运行记录</dd></div></dl>'),
    "datafetcher": guide_figure("参数、数据记录和看板", guide_screen(
        ["标的与日期", "复权口径", "行情字段", "数据保留"], "已获取数据",
        '<div class="guide-board"><span>价格走势</span><span>成交量</span><span>历史波动率</span><span>回撤</span></div>')
        + '<div class="guide-version"><span>仅本次使用</span><span>保存到本地</span><strong>下载CSV另存为文件</strong></div>'),
    "payoffer": guide_figure("收益图与合同规则一起阅读", guide_steps(
        ("选择产品", "只填写该产品支持的条款"), ("生成收益结构", "检查价格区间与端点"), ("检查路径事件", "观察、敲入敲出、提前终止"))
        + '<div class="guide-version"><strong>到期价格相同，经过的路径不同，结算也可能不同。</strong></div>'),
    "pricer": guide_figure("估值与反解是两种研究方向", '<div class="guide-compare"><div><strong>普通估值</strong><span>已知条款与市场假设</span><b>↓</b><span>价值、Greeks与风险图</span></div><div><strong>公平参数反解</strong><span>指定定价目标与可解条款</span><b>↓</b><span>参数解、残差与收敛状态</span></div></div>'),
    "backtester": guide_figure("从样本规则到逐笔结算", guide_steps(
        ("设入场规则", "起止日、频率、完整合同"), ("准备行情", "覆盖所有标的与持有期"), ("检查样本", "有效、排除及原因"), ("阅读结算", "逐笔事件与汇总统计"))),
    "reporter": guide_figure("逐个确认合同与运行版本", guide_screen(
        ["选择来源", "合同与版本", "分析模块"], "研究报告",
        '<dl class="guide-fields"><div><dt>收益结构</dt><dd>选定合同的有效运行</dd></div><div><dt>估值定价</dt><dd>核对估值日与模型</dd></div><div><dt>历史回测</dt><dd>核对入场频率与样本</dd></div></dl><span class="guide-action">生成报告</span>')),
    "settings": guide_figure("设置变更影响什么", '<dl class="guide-fields"><div><dt>模型与角色配置</dt><dd>影响后续请求，不改写旧回复</dd></div><div><dt>行情来源</dt><dd>Chat与Desk共用所选来源</dd></div><div><dt>主题与显示</dt><dd>调整界面，不改变计算结果</dd></div><div><dt>任务与导出文件</dt><dd>分别管理，更新App不等于清理研究记录</dd></div></dl>'),
}


def guide_for_role(role: Role) -> dict:
    admin = role == Role.ADMIN
    return {
        "title": "使用说明", "edition": "OptChat与OptDesk使用说明" if admin else "OptChat使用说明",
        "revision": "2026-11-15", "sections": [{**item, "body": CHAPTER_VISUALS.get(item["id"], "") + item["body"]}
                     for item in CHAT_SECTIONS + (ADMIN_SECTIONS if admin else ())],
    }
