# OH开发维护手册

有一个新产品，想让OH能够解释它、画收益图、定价和回测，应该从哪里开始？这份手册就按实际修改顺序来讲。

**录入新产品，直接看[第3章](#3-一步一步录入新产品)。**那里会用看涨期权作例子，逐个说明打开哪个文件、在哪里添加、字段怎么填，以及改完以后去App里看什么。

其他章节用于遇到具体问题时查阅：例如新增参数、改报告、更新安装版。文中的文件名可以点击打开。整理日期：2026-09-15；维护入口与产品增删发布要求核对：2026-09-16。

## 1. 先认识需要改的几个文件

OH没有一个能够完成全部新产品录入的后台表单。现在新增产品主要通过修改源码中的产品资料，再补上收益图和必要的计算支持。

最先要认识的是下面三个文件。它们描述同一个产品，但各有用途。

| 文件 | 可以把它理解成什么 | 新产品要加什么 |
| --- | --- | --- |
| [references/optionlist.md](../references/optionlist.md) | 产品目录 | 一行产品名称、编号和类别 |
| [references/optionlib.md](../references/optionlib.md) | 中文产品说明书 | 条款、收益规则、例子和适用场景 |
| [references/optionreg.py](../references/optionreg.py) | 给程序读的产品规则 | 默认参数、触发条件、分段收益和付款时间 |

比如，你只在目录里加了一个名字，程序还不知道这个产品怎么算。只在中文说明中写了收益公式，计算模块也不会自动把文字变成代码。这三处需要一起完成。

### 1.1 产品录入后，哪些地方会用到它

<div class="flow" role="group" aria-label="产品资料如何用于App">
<div><strong>产品目录和中文说明</strong><span>让维护者和Agent知道有哪些产品、各自是什么</span></div>
<div><strong>程序中的产品规则</strong><span>告诉计算模块使用哪些参数、如何判断事件、何时支付多少钱</span></div>
<div><strong>收益图、定价和回测</strong><span>根据这些规则画图、估值和回放历史行情</span></div>
<div><strong>推荐和报告</strong><span>解释产品，引用已经算出的结果，形成交付文件</span></div>
</div>

OH的七个模块分别是DataFetcher取数、Recommender推荐、Payoffer收益分析、Pricer定价、Backtester回测、Reporter组织报告、Designer排版。App中的OptDesk展示其中五个工作页面，推荐主要在OptChat里完成，排版由报告流程调用。

你修改产品条款时，通常从三个产品文件开始；只有现有程序无法表达新条款时，才需要往下修改计算模块。不要一开始就把所有模块都改一遍。

### 1.2 源码、安装App和Demo是什么关系

桌面上的源码文件夹是你修改程序的地方。安装好的App使用的是打包时放进去的一份程序，源码保存后，已安装App不会跟着自动更新。

Demo又是单独的一份静态演示。它可以展示页面和已有例子，但不会真的调用Python完成定价或回测。Skill提供计算工具和使用说明，当前包内不包含App的五个操作页面。

所以，**先把源码里的产品做好并运行检查，再更新App、Skill和Demo。**具体更新方法见第11章。

## 2. 开始修改前，怎样准备

先保留一份能正常工作的旧代码，再在另一份完整源码中修改。保留原来的文件夹结构，不要只把三个产品文件单独拿出来运行。这样改错了可以恢复，也不会影响你正在使用的App。

### 2.1 打开项目并选择Python

在编辑器里打开OptionHelper整个文件夹。当前机器继续使用Anaconda的MachineLearning环境，解释器是：

`/opt/anaconda3/envs/MachineLearning/bin/python`

不要为了改一个产品重新建环境。换机器时，先按照[README](../README.md)准备环境；当前运行检查要求Python至少3.12。运行依赖记录在[core/requirements.lock](../core/requirements.lock)，打包依赖在[packaging/build-requirements.lock](../packaging/build-requirements.lock)。

### 2.2 怎样打开正在修改的版本

开发时使用[packaging/app/run_development_app.py](../packaging/app/run_development_app.py)。它会准备运行需要的资源，然后启动本地App页面。可以在编辑器的运行配置中选择这个文件，工作目录设为OptionHelper根目录。

请给开发运行使用单独的测试数据目录，避免和日常任务混在一起。启动后打开程序输出的`OPTIONHELPER_URL`。修改产品规则后，关闭这次开发程序再重新运行，只刷新浏览器可能仍然使用旧规则。

<details>
<summary>运行配置怎么填</summary>

在编辑器的Python运行配置中填写：

| 配置项 | 填写内容 |
| --- | --- |
| Python解释器 | 上面列出的MachineLearning解释器 |
| 程序文件 | `packaging/app/run_development_app.py` |
| 工作目录 | 你正在修改的OptionHelper源码根目录 |
| 程序参数 | `--port 0 --data-dir /tmp/oh-maintenance-session` |

`--port 0`让程序选择空闲端口。`--data-dir`指定这次测试保存任务的地方，可以换成自己的独立测试目录。程序会生成临时运行资源，并在这个测试目录写入任务和结果；它不会替你更新安装好的App。

仓库里还有`core/start-pages.command`和`core/start-pages.bat`，它们用于独立模块页面，不是完整App的启动入口，也不是当前Skill包的安装入口。

</details>

### 2.3 模型和数据连接在哪里设置

模型服务、数据接口和导出目录优先在App设置页修改。只有增加新的配置选项或修复保存行为时，才需要改[settings_models.py](../products/app/backend/settings/settings_models.py)和[settings_service.py](../products/app/backend/settings/settings_service.py)。

测试录入产品时，使用自己的测试任务和脱敏数据。不要把模型密钥、数据账号或客户材料写进产品文件和示例。

## 3. 一步一步录入新产品

这一章以看涨期权为例，说明一个产品是怎样放进OH的。**看涨期权已经在库里，不要再添加一次。**你可以一边打开它的现有内容，一边按同样的方法填写真正的新产品。

### 3.0 让产品维护Skill协助录入

把条款文字、PDF、截图、表格或网页交给支持Skill的编程助手，同时提供本仓库的[update-option/SKILL.md](update-option/SKILL.md)。这个目录是维护Skill，不是OH面向业务用户的计算Skill；仅把它放在仓库里，不代表宿主已经安装或自动发现它。

可以直接说：“使用maintenance/update-option，按这份材料新增产品。先用人话说明你理解的收益和风险，等我确认后再修改。”修改已有结构时给出产品名称或编号；删除时说明是停止新业务使用，还是从当前目录彻底移除。

助手会先说明谁收付款、何时观察、怎样触发、何时结束，并给出几个手算情景。看不清或互相冲突的条款会先列出来，不猜填。你确认后，它才修改产品资料和必要的计算支持，并验证实际结果。

确认时也说明要在哪里生效：开发源码、安装App、业务Skill包或Demo。源码验证通过不等于安装版已更新。已有任务和历史结果应保留，不能因为改产品就被清空。

### 3.1 先判断是否真的需要新增

如果只是把现有雪球的票息从10%改为12%，或者把看涨期权的执行价从100改为105，先在App参数里修改即可，不需要建立一个新产品。

只有产品的业务结构不同，确实希望它作为独立结构出现在目录里，才做下面的录入。例如新增一种不同的敲出安排、票息结构或到期赔付方式。开始前也要搜一下目录，确认没有已经能够表达同样条款的产品。

把新产品先用几句话说明白：挂钩什么、什么时候观察、什么条件下付款、付多少、什么时候结束。尤其要写清楚“恰好等于障碍时算不算触发”，以及提前结束后是否还付票息。

以看涨期权为例，这段说明可以写成：持有者期初支付期权费；只看到期价格；到期价格高于执行价时获得价差，否则不再收款。暂用参考价100、执行价100、期权费5、1份期权作为默认例子。

### 3.2 第一步：在产品目录里加一行

打开[references/optionlist.md](../references/optionlist.md)，找到产品表格。现有看涨期权是这一行：

| 序号 | 中文唯一名称 | 标题编号 | 所属类别 | 入库情况 |
| --- | --- | --- | --- | --- |
| 1 | 看涨期权 | 1.1 | 方向性期权 | 已录入 |

真正新增时，在合适的位置插入一行，按下面的方法填写：

- **序号**是这张表的排列顺序，不是程序识别产品的编号。插入后把表格顺序整理好。
- **中文唯一名称**填写产品正式名称。之后中文说明、程序规则和收益图文件都使用这个名称。
- **标题编号**是产品ID。按所属类别选择尚未使用的编号，后面两个产品文件也要使用同一个编号。不要拿旧产品编号给新产品使用。
- **所属类别**填写现有分类中合适的一类。确实要增加分类时，再检查页面是否能显示它。
- **入库情况**沿用目录已有写法。不要把尚未完成的产品发布给正常使用者。

完成这一处，只是给产品起好名字、分好类。接着写它的具体规则。

### 3.3 第二步：写清楚中文条款

打开[references/optionlib.md](../references/optionlib.md)，搜索`### 1.1 看涨期权`。从这个标题开始，到下一个产品标题之前，是完整的一份产品说明。

新增时，可以复制一个相近产品的整个说明段落，放在对应类别下。先改产品编号和名称，再逐节替换内容。保留原来的标题层级，例如产品1.1下面依次是1.1.1到1.1.5；换了产品ID，这些小节编号也要一起改。

| 小节 | 应该怎么写 | 看涨期权的例子 |
| --- | --- | --- |
| 条款要素 | 写明标的、期限、数量、费用、观察、支付和提前终止安排 | 买入1份，期初付5，到期才行权，无提前终止 |
| 损益结构 | 把每一种结局分开写，说明判断条件和双方收付款 | 到期不高于100；到期高于100 |
| 默认示例 | 给具体参数和几个能手算的结果 | 到期100时净损益−5；到期110时净损益+5 |
| 适用场景 | 分别说明持有方和对手方为什么使用、如何盈亏 | 买方看涨，最大损失是期权费；卖方上涨风险不封顶 |
| 产品总结 | 用一段话说明产品主要特点和风险 | 付固定费用换上涨收益，涨幅不足以覆盖费用时仍亏损 |

这里的例子假定真实参考价格也是100，买入1份。其他参考价格下，价格点和真实现金金额之间还需要换算，不能直接照抄例子的金额。

如果产品有敲入、敲出或多次付息，中文说明要把先后顺序写出来。比如“先敲入后敲出”和“从未敲入直接敲出”是否支付同样金额；同一天敲入和敲出同时满足时，先执行哪条规则。程序不会替你决定这些业务条款。

### 3.4 第三步：把条款写进程序

打开[references/optionreg.py](../references/optionreg.py)，搜索`PRODUCTS =`。里面每一项对应一个产品。继续搜索`'1.1':`，就能找到看涨期权。

新产品要作为**PRODUCTS里的一个新条目**加入，不要覆盖整个PRODUCTS，也不要另建一份程序不读取的产品字典。文件最后还会把产品和参数说明组合成REGISTRY，沿用现有写法即可。

下面把看涨期权这一项整理成更容易阅读的格式。外层`"1.1"`是产品ID，里面分成身份、默认条款和收益规则三部分。

```python
{
    "1.1": {
        "identity": {
            "product_id": "1.1",
            "name_zh": "看涨期权",
            "entry_status": True,
            "rule_revision": 1,
        },
        "terms": {
            "S0": 100.0,
            "T": 1.0,
            "exercise_style": "European",
            "settlement": "cash",
            "observation_price": "close",
            "margin_call": False,
            "K": 100.0,
            "Pi_0": 5.0,
            "n_C": 1.0,
            "pricing_methods": ["analytical", "monte_carlo"],
        },
        "paths": [{
            "condition": "True",
            "cases": [
                {
                    "domain": "0 <= S_T <= K",
                    "pnl": "cash(0, -n_C * P)",
                },
                {
                    "domain": "S_T > K",
                    "pnl": "cash(0, -n_C * P) + cash(T, u * n_C * (S_T - K))",
                },
            ],
        }],
    },
}
```

**先改identity，说明这是哪个产品。**`product_id`要和目录、中文说明一致；`name_zh`也要完全一致。`entry_status=True`表示登记为启用。`rule_revision=1`表示第一版规则，以后改经济规则时递增这个数字，例如从1改成2。

**再改terms，填写产品的默认参数。**这里放的是产品条款，不放模型地址、模拟路径数、回测日期区间等运行设置。

| 字段 | 含义 | 怎么改 |
| --- | --- | --- |
| `S0` | 内部参考价格基准 | 通常保留100，不要用它填写名义本金 |
| `T` | 默认期限，以年表示 | 半年可写0.5，实际日期还按原有日历逻辑处理 |
| `K` | 执行价格水平 | 默认平值写100；改成105就表示参考水平的105% |
| `Pi_0` | 该例子的默认期权费 | 这里是5，损益表达式通过P使用它 |
| `n_C` | 看涨期权份数 | 这里是1，不要用它代替用户的名义本金 |
| `exercise_style` | 行权方式 | 此例European表示欧式，仅到期行权 |
| `settlement` | 结算方式 | 此例cash表示现金结算 |
| `observation_price` | 观察哪个价格 | 当前收益分析使用close，即收盘价；换成high不是只改一个单词就能支持 |
| `pricing_methods` | 准备支持的定价方法 | 现有1.1支持解析和MC；真正的新产品还要按第3.6节检查实现 |

字段看不懂时，先查看同文件的`TERM_CATALOG`。它相当于程序的参数词典。已经有相同含义的字段就复用，不要给同一个执行价另起一个名字。新增词典项也不会自动让公式会计算，新含义仍要检查计算代码是否支持。

**最后改paths，把各种结局写清楚。**上例没有敲入敲出，只看到期价格，所以只有一条路径，`condition="True"`表示所有情况都进入这一条，再由cases划分价格区间。

第一段的`domain`写`0 <= S_T <= K`，表示到期价不高于执行价；它只有期初支付期权费这一笔现金流。第二段写`S_T > K`，表示到期价高于执行价；除了期初付款，还在到期收到价差。

`cash(0, ...)`里的0表示期初，`cash(T, ...)`里的T表示到期。负数表示持有方付出，正数表示收到。公式里的u负责把标准化价格点换回实际金额，由现有程序处理。不要为了让公式看起来简短，把不同时间的付款合成一笔到期付款。

有障碍或多次观察的产品，还要参考相近产品的`monitor`来描述观察事件；需要先算中间量时，再看`derived_terms`。先把中文条款逐条对应到这些位置。遇到没有现成写法的新事件，进入第4章处理，不要把一段中文或任意Python函数塞进公式。

录入后还要试着改参数。例如牛市看涨价差的低执行价必须小于高执行价，不能默认95和105时能算，用户改成110和105后仍放行。需要自动计算的中间量应随基础参数重算，不应让用户直接填写；公式使用参数词典里的符号，例如K1对应K_1。先算哪个中间量、再使用哪个中间量，也要按依赖顺序写清楚。

### 3.5 第四步：补上默认收益图

只完成前三个文件，还不够。OH会按产品中文名读取默认收益图，因此还需要两份文件：

| 文件位置 | 内容 |
| --- | --- |
| `modules/payoffer/figures/json/产品中文名.json` | 画图要用的默认参数和分段信息 |
| `modules/payoffer/figures/svg/产品中文名.svg` | 打开默认示例时看到的收益图 |

看涨期权对应[看涨期权.json](../modules/payoffer/figures/json/看涨期权.json)和[看涨期权.svg](../modules/payoffer/figures/svg/看涨期权.svg)。新产品也要使用完全相同的中文名称，不能一个叫“新雪球”、另一个叫“新型雪球”。

JSON中的`name_zh`、`terms`、`paths`应与新产品登记相符。SVG用现有收益图程序生成，不需要自己手动画曲线。生成后打开图片，检查拐点、横轴、收益方向以及每种情况是否展示完整。

目前的“刷新默认图”代码只能更新已有文件，不能替你完成新产品的首次建图。下面提供具体做法；如果你请开发者或编程助手处理，可以直接让其按照这段生成两份新文件。

修改已有默认图时，先查看完整刷新计划：这个入口会扫描其他产品，不能把别的产品差异顺手一起发布。新增或改名先在候选目录准备名称一致的JSON和SVG，再处理已确认不再使用的旧文件。默认图维护记录只保留本次图形资产，三库和计算代码需要另外保留修改前副本；不要以为恢复两张图就恢复了整个产品。

<details>
<summary>新产品默认图怎么生成</summary>

先在第2章准备的源码副本中完成产品登记。在编辑器运行设置中，把这份源码根目录和其中的`core/src`加入Python导入路径，并把`OPTIONHELPER_PROJECT_ROOT`指向这份源码根目录。每次修改产品规则后重新启动Python。

把下面函数放进独立的临时Python文件中。它使用现有收益图程序；输出只写到你传入的文件夹，碰到已有同名文件会停止，避免覆盖。

```python
from dataclasses import asdict
from pathlib import Path
import json

from modules.payoffer.asset_resolver import build_visual_template
from modules.payoffer.impl.engine import build_payoff_input, render_paths
from modules.payoffer.impl.svg_renderer import render_svg


def prepare_candidate_assets(product, registry, candidate_figures: Path):
    name = product["identity"]["name_zh"]
    if not name or any(char in name for char in ("/", "\\", "\0")):
        raise ValueError("产品名称不能包含路径字符")
    template = build_visual_template(product["paths"])
    payload = {"name_zh": name, "terms": product["terms"],
               "paths": product["paths"], "visual_template": template}
    contract = build_payoff_input(name, maintenance_registry=registry)
    rendered = [asdict(path) for path in render_paths(contract, template)]
    svg = render_svg({"name_zh": name, "paths": rendered})
    json_path = candidate_figures / "json" / f"{name}.json"
    svg_path = candidate_figures / "svg" / f"{name}.svg"
    if json_path.exists() or svg_path.exists():
        raise ValueError("候选资产已存在，请先审阅差异")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    svg_path.write_text(svg, encoding="utf-8")
```

在这个函数下面，再按自己的源码副本位置和新产品ID填写调用部分：

```python
from runtime.contracts.contract_api import load_registry

# 改成你正在编辑的源码副本，不要指向已安装App。
project = Path("/path/to/OptionHelper-copy")
# 改成你在三个产品文件中填写的新ID。
product_id = "待填写的新产品ID"

registry = load_registry()
product = registry["products"][product_id]
prepare_candidate_assets(product, registry, project / "modules/payoffer/figures")
```

执行前确认编辑器加载的也是这份源码副本，输出路径正确并不代表导入的代码一定正确。完成后，进入json和svg文件夹，确认都出现了新文件，再打开SVG看图。

这段生成函数已经用现有看涨期权在临时文件夹运行过。新产品如果采用当前绘图程序无法表达的新横轴或新面板，需要再修改[asset_resolver.py](../modules/payoffer/src/asset_resolver.py)和[收益图引擎](../modules/payoffer/src/impl/engine.py)，不能直接借用旧产品图片。

</details>

### 3.6 第五步：确认定价和回测能不能用

**新增或删除不能只做三库和收益图。**还必须核对Pricer反解能力表、Backtester经济口径表与指标分类表。即使某项能力不支持，也要明确登记原因；表里少了新产品或多了已删除产品，都可能使目录或计算失败。

**容易漏掉的登记与发布依赖**

- [fair_parameter.py](../modules/pricer/src/fair_parameter.py)：反解能力表要求覆盖当前产品集合；改报价目标或现金流后需要重新审阅。

- [economic_conventions.py](../modules/backtester/src/economic_conventions.py)和[metric_profile_map.py](../modules/backtester/src/metric_profile_map.py)：分别维护费用口径与指标分类，不会仅凭新产品名称自动理解。

- [position_amounts.py](../products/app/backend/position_amounts.py)：特殊收益单位和方差名义本金需要单独核对金额换算。

- 默认JSON/SVG变化还影响发布读取的资产基线。保留旧基线作对照，确认差异后准备新的发行基线，不修改旧Golden来掩盖错误；公开仓库缺少基线时应说明构建依赖。

- 产品数量和启用状态检查分布在知识快照、Skill构建与校验、App计算包及macOS/Windows验收。当前仅改entry_status=False不能保证含停用产品的目录能发布。

详细入口见[产品维护接入说明](update-option/references/oh-integration.md)，实际完成标准见[验证与交接](update-option/references/verification.md)。清单必须随当前源码核对，不要求每次修改全部文件。

**录入产品规则后，不一定需要再改Pricer和Backtester。**如果新条款完全能用现有公式和观察规则表达，可以先运行现有计算。但要实际尝试，不能只在产品条款里写“支持定价”。

| 你希望新产品支持什么 | 先怎么做 | 不支持时去哪里改 |
| --- | --- | --- |
| 画收益图 | 打开默认图，再改一次参数重新画 | Payoffer的[asset_resolver.py](../modules/payoffer/src/asset_resolver.py)和[engine.py](../modules/payoffer/src/impl/engine.py) |
| MC定价 | 在产品方法里声明monte_carlo，重启后选MC运行 | [model_router.py](../modules/pricer/src/model_router.py)、[product_pricing_adapter.py](../modules/pricer/src/product_pricing_adapter.py)及具体引擎 |
| 解析定价 | 先确认程序已有这个产品的解析处理 | model_router中的PRODUCT_CAPABILITIES、ProductPricingAdapter和解析引擎；单加analytical字段不够 |
| Greeks和风险分析 | 定价成功后实际查看各项输出及单位 | [greeks.py](../modules/pricer/src/greeks.py)及产品使用的计算方法 |
| 反解票息或其他参数 | 选择目标参数和合理范围实际求解 | [fair_parameter.py](../modules/pricer/src/fair_parameter.py)、[fair_solver.py](../modules/pricer/src/fair_solver.py)、[valuation_solver.py](../modules/pricer/src/valuation_solver.py) |
| 历史回测 | 给完整行情和日历，先跑短区间 | [entry_generator.py](../modules/backtester/src/entry_generator.py)、[path_replay.py](../modules/backtester/src/path_replay.py) |
| 推荐与报告 | 让OptChat解释并使用它，再从真实结果生成报告 | [推荐模块](../modules/recommender/src/service.py)、[报告适配](../products/app/backend/reporter_adapter.py) |

当前MC会为启用且声明monte_carlo的新产品尝试通用规则计算。解析定价则还需要专门登记和适配，这就是二者维护方式不同的地方。不能因为旧产品1.1写了两种方法，就把新产品也直接标成全部支持。

### 3.7 第六步：实际看一遍结果

先用容易算清楚的例子。看涨期权取参考价格100、执行价100、期权费5、1份，分别看到期价格90、100、110、120，预期净损益是−5、−5、+5、+15。这里检查的是到期损益，不是今天的公平定价。

新结构也至少准备几种明确情况：正常到期、亏损、触发障碍、恰好等于障碍，以及提前结束。把每种情况应该收到或付出的金额先写出来，再与程序比较。

然后打开修改后的开发App，按这个顺序操作：

1. 找到新产品，确认名称、默认条款和收益图都正确。
2. 改一个参数，例如执行价或票息，再计算一次，确认结果确实随新参数变化。
3. 运行一次定价，查看价格、Greeks及错误提示是否合理。
4. 运行一次短区间回测，打开逐笔结果，检查日期、触发事件和收付款。
5. 生成报告，确认引用的是刚才这个产品、这组参数和这次结果。
6. 切回原来的产品再跑一次，确认旧产品没有被这次改动影响。

产品有多标的、存续状态或复杂观察日时，还要分别试这些情况。缺少必要行情时应明确提示缺什么，不能悄悄算出一个看似成功的结果。

### 3.8 最后：让其他人用到新产品

**新增或删除产品还有一道发布检查。**当前[knowledge_snapshot.py](../packaging/knowledge_snapshot.py)有多处按65个产品校验。产品数量变化时，需要一起调整目录顺序、产品源快照、默认图清单和发布验证，让各处与确认后的产品集合一致。不要只改一个数字，也不要关闭检查。旧发行包仍应按它自己保存的目录验证，不能拿新目录覆盖历史版本。

完成检查后，把改好的产品文件、默认收益图和确实需要修改的计算代码一起放回正式源码。重新启动开发程序确认能用，然后按第11章打包更新App和Skill，重建Demo。

**如果改完没有生效，先检查是不是还开着旧程序。**产品规则在程序启动后会保持固定；修改文件以后需要重启后端。安装版则还需要重新打包安装，浏览器刷新不能替代这一步。

出了问题，恢复修改前保存的代码和默认图，重新启动即可继续使用旧规则。不要为了回到旧版本，把新版本产生的任务和报告随手删掉。

### 3.9 只是修改已有产品，怎么处理

只改默认参数也要检查旧任务。当前合同校验会比较实际条款及其来源，不是只比较rule_revision；采用旧默认值的合同可能无法在新版直接重跑，显式覆盖参数的合同则要分别判断。改名要核对知识标题和默认图文件名；改观察规则要核对所需行情和日历。生效前确认构建使用的正是通过检查的那份源码，不能混用不同轮次的资料。

| 想做的事 | 具体改法 |
| --- | --- |
| 这一次换期限、执行价或票息 | 在App参数中修改；不改三个产品文件 |
| 以后默认就用新的参数 | 改optionreg.py中该产品的terms，同步中文默认示例和默认收益图，再检查新任务是否采用新默认值 |
| 修改敲入敲出、收益公式或支付时间 | 改optionlib.md对应条款、optionreg.py对应monitor/paths等规则，递增rule_revision，再重做相关默认图和检查 |
| 产品改名 | 三个产品文件中的名称一起改；JSON/SVG文件名和JSON里的name_zh也要改，编号通常保留 |
| 暂时不再提供产品 | 不要直接删除旧资料；检查entry_status、目录和推荐是否都阻止新使用，旧任务和报告另外保留 |

例如把“大于障碍才触发”改成“大于等于就触发”，真正需要改变的是恰好等于障碍的结果。测试时就重点检查相等、略低、略高三种情况，不要只看一条远离障碍的行情。

规则版本变了，旧任务仍然记录旧条款。不要把旧任务的rule_revision直接改成新值来消除错误。如果需要重现旧结果，用保存的旧版本程序和原数据运行。备份方法见第9章。

## 4. 遇到新规则，计算代码怎么改

三个产品文件解决的是“把已有程序能理解的规则写进去”。如果新产品引入以前没有的观察方式、状态变量或结算方法，就需要开发新的计算支持。

### 4.1 先找到程序不理解哪一条

把新条款逐句与相近产品比较。是多了一个已有含义的参数，还是出现了完全不同的事件？例如从月末观察改成当前不支持的某种观察安排，需要修改观察日期生成逻辑，而不只是给字段填一个新字符串。

共同的产品规则处理在[contract_engine.py](../core/src/runtime/contracts/contract_engine.py)，对外入口在[contract_api.py](../core/src/runtime/contracts/contract_api.py)。日期和数据准备还会经过[input_adapter.py](../core/src/runtime/contracts/input_adapter.py)及[observation_schedule.py](../modules/pricer/src/observation_schedule.py)。

优先在这套共用逻辑中增加支持，再让定价、回测和收益图使用它。不要在三个模块里分别写三套不同的同名规则。

### 4.2 修改Pricer时重点看什么

从[service.py](../modules/pricer/src/service.py)进入，再看model_router如何选择方法，ProductPricingAdapter如何把合同送入计算引擎。先确认产品是否支持该方法，以及数据是否齐全，再排查公式。

做速度优化时，固定同一组参数比较修改前后结果。路径数、随机数、观察日期、标准误、Greeks、风险和反解都要保留原含义。用户点停止和出错时的行为也属于功能，不能因为计算快了就忽略。

### 4.3 修改Backtester时重点看什么

回测先确定哪天入场，再取这一笔合同需要的历史行情，最后生成逐笔收付款和统计。对应入口是[entry_generator.py](../modules/backtester/src/entry_generator.py)、[path_replay.py](../modules/backtester/src/path_replay.py)、[trade_ledger.py](../modules/backtester/src/trade_ledger.py)和[common_metrics.py](../modules/backtester/src/common_metrics.py)。

如果产品提前结束，检查结束日期之后是否停止计息和付款。只看到一个可能的敲出日期还不够，原程序还有针对实际前段行情的复核，优化时不能省略。

HV是历史波动率，用于相应的筛选和分组。没有选择这种筛选时可以不启用HV，回测本身照常进行。修改相关逻辑时检查历史窗口不足、分组边界和多标的各自历史，不能把不同标的混成一条序列。

### 4.4 名义本金和内部100是什么关系

内部100是标准化参考价格，名义本金是用户这次业务希望对应的金额。它们不放在同一个参数里。当前App默认名义本金1000000，在定价和回测参数流程中使用。

金额显示主要看[position_amounts.py](../products/app/backend/position_amounts.py)。对于已经明确按每百元计量的金额字段，可以按下式换算：

<div class="formula" role="math" aria-label="金额换算">金额 = 每百元字段值 × 名义本金 ÷ 100</div>

概率、收益率、日期和不同单位的Greeks不能全部套这个公式。新增产品时，先确定输出单位，再确认金额附表是否支持。不要直接把内部标准化结果改大，来让Greeks看起来更明显。

## 5. 行情和日历有变化，怎么维护

行情下载和整理主要在[modules/datafetcher/src](../modules/datafetcher/src/service.py)。如果增加数据来源，先看[providers/base.py](../modules/datafetcher/src/providers/base.py)定义的接口，再参照现有Provider实现。

### 5.1 增加价格字段或数据来源

先拿一小段实际返回的数据，看清楚日期、标的代码、close和其他价格字段分别是什么。把来源特有的格式转换成OH统一格式，再接入使用它的模块。

请求检查在[request_validator.py](../modules/datafetcher/src/request_validator.py)，格式整理在[data_normalizer.py](../modules/datafetcher/src/data_normalizer.py)，数据完整性检查在[quality_validator.py](../modules/datafetcher/src/quality_validator.py)。根据问题发生的位置修改，不要直接在Pricer里临时补一段特定数据源代码。

合同使用的价格和计算HV的复权价格可能不同。字段同样是数字，不代表可以互换。拿缺失日期、重复日期、停牌、多标的和无效价格分别试一次。

### 5.2 交易日历不能用行情日期代替

[calendar_service.py](../modules/datafetcher/src/calendar_service.py)负责日历。某一天没有行情，可能是缺数据，不一定是休市。观察日和到期日应按正式交易日历确定。

遇到“数据覆盖不足”，先看缺的是合同期间、存续历史，还是HV所需的更早历史。补齐相同口径的数据后重新运行，不要补造价格或把日期直接删掉。

修改数据处理前保留原文件。新旧结果不同时，可以用同一份原始行情重跑，判断问题来自数据处理还是产品规则。不要修改桌面上独立的DataFetcher工具项目来替代OH内部维护。

## 6. Agent和工具参数怎么改

OptChat背后的对话逻辑主要看[conversation_agent.py](../products/app/backend/agent_runtime/conversation_agent.py)。推荐模式看[multi_agent.py](../products/app/backend/agent_runtime/multi_agent.py)，推荐业务流程看[RecommenderService](../modules/recommender/src/service.py)。

### 6.1 修改Agent说什么、什么时候调用工具

普通对话的提示词和分步说明可以从[step_instructions.py](../products/app/backend/model_gateway/step_instructions.py)查起。研究角色的职责在core/src/runtime/research_profiles下，按预设名、角色名找到AGENT.md，例如[产品交易循环的Structurer说明](../core/src/runtime/research_profiles/product-trader-loop/Structurer/AGENT.md)；App和Skill共用这套角色说明，不要只改App侧文字。修改前先写几个具体问题，例如“给某个产品估值”“缺少标的时怎么追问”“工具失败以后怎么解释”。修改后用这些问题重新尝试。

不仅要看回复是否顺畅，还要看它有没有实际调用正确工具、有没有把用户给的参数传进去、有没有把失败说成成功。解释产品可以读资料；输出价格和回测结果必须来自真实计算。

App有自己的Agent执行环境，Skill则使用外部宿主提供的模型和角色执行能力。App能运行的多角色流程，不代表任意Skill宿主都支持。

### 6.2 修改工具参数，按什么顺序

比如想给定价增加一个可选参数，先决定默认值是什么、缺省时是否保持原行为，以及它究竟属于产品条款、市场输入还是显示选项。

1. 在[tool-io.schema.json](../core/src/runtime/protocol/schemas/tool-io.schema.json)和[models.py](../core/src/runtime/protocol/models.py)查看输入结构，增加对应字段。
2. 查看[tool_gateway.py](../products/app/backend/tool_gateway.py)、[runtime_tool_bridge.py](../products/app/backend/agent_runtime/runtime_tool_bridge.py)和模块service，确认这个值能一直传到使用它的位置。
3. 如果它影响计算，把它保存进本次运行实际使用的参数中。以后打开历史结果时，要能看到当时用的值。
4. 在页面和工具说明中补上输入方式。只增加字段时通常不需要新工具；真的增加操作时，再改[tool_catalog.py](../core/src/runtime/protocol/tool_catalog.py)。
5. 分别试不填、填合法值、填非法值，检查提示和结果；然后通过OptChat和页面各运行一次。

### 6.3 修改推荐模式和模型配置

模型连接优先在设置页改。App公开Mode1产品交易循环、Mode2独立评议、Mode3约束排序，默认产品交易循环；Skill默认仍是兼容顺序研判。新增推荐模式时，再检查各角色职责、调用顺序、可用工具和中止方式。串行执行也可以包含多个角色，不要只按是否并发来判断模式。

预设、工具权限和快速、标准、深入三档预算在[presets.json](../core/src/runtime/research_profiles/presets.json)，由[research_policy.py](../core/src/runtime/research_policy.py)读取。默认标准研究；三档共享模块计算预算分别为6、12、24，产品循环最多1、2、4轮，评议复核和排序补充最多0、1、2轮。它们是研究上限，不是必须执行的次数，也不进入金融引擎参数。兼容顺序研判不受这些补充轮数控制。

[research_session.py](../modules/recommender/src/research_session.py)管理本次研究的候选访问、共享预算和计算证据。研究角色通过受控工具自主计算，独立评议先验证自己的候选，之后共享证据并定向复核。相同条件下的成功计算可以复用，读取已有结果不重新计算。App的工具接入看[runtime_tool_bridge.py](../products/app/backend/agent_runtime/runtime_tool_bridge.py)，Skill宿主接入看[ports.py](../modules/recommender/src/ports.py)；未绑定研究工具时不能声称完成了自主计算。

App使用说明的正文在[user_guide.py](../products/app/backend/user_guide.py)。[user-guide.js](../products/app/frontend/shared/user-guide.js)从/api/user-guide读取正文并负责搜索、目录和展示；更新工作流说明应修改正文源。Skill说明维护[skill-user-guide.md](../references/skill-user-guide.md)，再用现有生成器同步离线HTML。

保留几个原来答错或调用错误的例子。每次修改后重试，写清楚现在是否解决。这样比反复调整一句提示词、更换一个模型后凭感觉判断更有用。

## 7. 页面上的参数和交互怎么改

先分清是App外层页面，还是里面的模块页面。OptChat和OptDesk在[products/app/frontend](../products/app/frontend/optdesk/index.html)下，定价表单在[modules/pricer/page/pricer.html](../modules/pricer/page/pricer.html)。其他模块也有自己的page文件夹。

### 7.1 加一个参数，不能只加输入框

以增加默认本金输入为例，除了控件，还要确认默认值、保存草稿、切换产品、提交计算和重新打开任务时是否正确。输入框显示1000000，不代表后端收到的也是1000000。

修改后建一个测试任务，输入一个容易辨认的值，运行一次，再切页回来。查看结果和报告，确认使用的是这次填写的值。不要先在Demo里改控件，再以为正式App也有了。

### 7.2 切页、停止和重新运行

测试时实际走一遍：A产品运行中切到B，再回A；取消一次运行，再重新运行；改了参数之后查看旧结果。重点看是否串任务、串参数，或者把旧结果当成新结果。

页面和后端衔接在[module_host_bridge.js](../core/src/runtime/browser/module_host_bridge.js)。恢复历史结果失败和恢复正在运行的任务是两个问题，不应一个失败就让整个页面空白。

窗口大小、居中和启动画面属于原生壳。macOS主要看[OptionHelperApp.swift](../products/app/desktop/macos/OptionHelperApp.swift)，还要留意[Objective-C后备实现](../products/app/desktop/macos/OptionHelperApp.m)；Windows看[Program.cs](../products/app/desktop/windows/Program.cs)。改完要在对应系统打开App，浏览器缩窄不能代替Windows窗口检查。

## 8. 报告内容和样式怎么改

Reporter决定报告放哪些实际结果，Designer负责怎么排版。修改之前先判断：是缺了一项内容，还是内容正确但显示不好看。

### 8.1 找到正确的修改位置

| 想修改什么 | 先打开哪里 |
| --- | --- |
| 报告缺少某个计算指标 | [report_unit_builder.py](../modules/reporter/src/report_unit_builder.py)和[reporter_adapter.py](../products/app/backend/reporter_adapter.py) |
| 字体、颜色、表格、版式 | [design_tokens.py](../modules/designer/src/design_tokens.py)和[design_system_builder.py](../modules/designer/src/design_system_builder.py) |
| 报告编辑和保存 | [report_editor.py](../products/app/backend/report_editor.py) |
| 导出文件 | [report_delivery.py](../products/app/backend/report_delivery.py) |
| 名义本金对应的金额附表 | [position_amounts.py](../products/app/backend/position_amounts.py) |
| 图片、文档和其他附件 | [attachment_store.py](../products/app/backend/attachments/attachment_store.py)及同目录的document_store、image_store |

增加指标时，先确认计算结果里确实有这个字段，再把它传进报告。不要只在模板里写一个标题，让模型自行补数值。

### 8.2 修改报告后怎么检查

先用一份现成的测试结果生成旧报告，再用同一份结果生成新报告。只改样式时，金额、日期、价格和回测统计应该保持原样。

分别打开HTML、PDF和Word，检查中文、公式、图表、长表和分页。需要支持的单产品、多产品和报价报告都要试。HTML打开正常，不代表导出文件也正常。

再试编辑、保存、关闭、重新打开和再次导出。保留旧报告文件，出了问题恢复模板后可以直接对照，不需要删除整个报告库。

## 9. 任务和结果怎样备份

改代码之前保留旧代码；更新App之前还要保留用户任务和结果。这是两件事。代码恢复了，不代表误删的报告和附件也能恢复。

### 9.1 需要备份什么

macOS的常用数据根目录是`~/Library/Application Support/OptionHelper`，Windows是`~/AppData/Local/OptionHelper`。实际运行还可能指定单独的数据目录，先确认本次程序用的是哪一个。

备份时包括任务、结果、数据文件、报告和附件。如果设置里用了外部文件夹，那些文件也要保留。不要只复制任务索引，却漏掉索引指向的图片或行情。

对应代码在[session_store.py](../products/app/backend/stores/session_store.py)、[result_store.py](../products/app/backend/stores/result_store.py)和[data_store.py](../products/app/backend/stores/data_store.py)。凭据单独由[secret_provider.py](../products/app/backend/secrets/secret_provider.py)等处理，复制普通设置不保证在另一台电脑上仍能登录。

### 9.2 恢复时怎么做

先完成或停止正在运行的任务，再关闭程序并备份目录。恢复时先恢复到另一份测试目录，用对应版本的程序打开，检查任务、结果、附件和报告都能读取，再决定是否替换日常数据。

规则升级后，旧合同可能不能在新引擎里继续计算。需要重现时，使用修改前的程序、原参数和原行情。不要修改旧结果里的版本数字来绕过提示。

仓库中的[product_rule_revision.py](../products/app/backend/product_rule_revision.py)有清理旧规则结果的函数，但目前没有找到它在正式启动流程中的调用。它会删除相关结果，**不要把运行这个文件当成更新App或恢复数据的日常步骤。**

## 10. 改完以后，怎样知道改对了

检查围绕这次修改来做。改产品就重点检查收益规则，改页面就实际操作页面，改报告就打开生成的文件。不需要每次都填一大套表，但要留下能够重复使用的输入和预期结果。

### 10.1 产品和计算检查

先手算简单例子，再用程序比较。包含亏损、盈利、边界和提前终止；多标的产品增加不同标的先后触发的情况。除了总金额，还要看付款日期和事件判断。

保留旧产品的一组结果。新产品录入完成后，再跑一次旧产品，看看是否受到了意外影响。修复某个错误时，也把原来失败的输入保存下来，以后反复使用。

### 10.2 已有测试在哪里

当前维护者工作区有`tests/`和`packaging/tests/`，但它们并未随当前公开仓库提供。别人下载公开源码后不一定有同样的测试，不能直接把缺失当成测试通过。

常用本地检查包括`tests/knowledger/audit.py`检查三个产品文件，`tests/test_financial_baselines.py`检查原有金融结果。运行前确认文件和所需数据存在；没有时请原维护者提供匹配测试，或者另写针对本次产品的检查。

已有三个产品文件的检查能够发现一部分漏填和冲突，但不能替你判断合同本身是否写对。例如敲出应不应该包含当天，仍需要依据产品条款检查。

### 10.3 速度优化怎么比较

用相同机器、相同Python环境和相同输入，分别运行修改前后程序。各跑几次并交替顺序，记录整次运行用了多久，不只看某个小函数的时间。

同时比较价格、标准误、Greeks、现金流、回测账本和报错行为。默认要求结果一致；如果明确允许某个字段存在很小的浮点差异，要先说明是哪一项、差多少以及为什么。障碍触发或付款时间改变不是小数末位差异。

没有稳定加快，或者明显增加内存、让其他产品变慢，就先保留原实现。确认有效后再更新正式代码，并用同一组例子重跑。

## 11. 更新App、Skill和两份Demo

新产品在开发版本里能用以后，还需要把它交给实际使用者。当前版本分别是App v0.1.0和Skill v0.3.0；以后改版本时以[release_contract.py](../packaging/release_contract.py)中的APP_VERSION和SKILL_VERSION为准。

### 11.1 打包前先准备好

确认产品目录、中文条款、程序规则和默认图已经一起保存。需要改计算模块的，也要包含相应代码。先保留旧安装包和用户数据，方便出问题时回退。

新文件是否会被打进包，主要由[Skill文件映射](../packaging/skill/package-source-map.json)和[App文件映射](../packaging/app/capability-source-map.json)决定。如果文件放在原有产品和默认图目录下，仍要检查构建是否成功；如果加了全新资源目录，需要在打包配置中加入它。

### 11.2 更新安装版

1. 在macOS双击[build-optionhelper-macos.command](../build-optionhelper-macos.command)，Windows使用[build-optionhelper-windows.bat](../build-optionhelper-windows.bat)。复用已经选好的Python环境。
2. 等构建完成，查看输出说明。macOS当前产物在`dist/`，Windows候选产物在`result/windows-candidate/`。入口会准备App和Skill，不只是启动程序。
3. 退出旧App，保留旧安装物，再安装新生成的版本。不要把几个源码文件手工塞进旧App包里。
4. 打开新App，直接查找刚增加的产品，修改参数并运行定价和回测，再生成报告。真正跑一次，才能确认新产品已经进入安装版。
5. 如果有问题，退出新App，恢复旧安装物。需要恢复数据时按第9章操作，保留更新后产生的任务副本。

Skill要在实际使用它的宿主中更新包，再尝试解释新产品、调用计算和生成报告。只更新App不会更新另一处安装的Skill。

<details>
<summary>负责打包的开发者还需要看哪些文件</summary>

当前候选构建入口是[build_current.py](../packaging/build_current.py)，正式发行入口是[release.py](../packaging/release.py)。App与Skill版本独立，这两个入口的version默认含义也不同，不要直接复制旧文档中的v1.0.0示例。

Skill构建在[build_skill.py](../packaging/skill/build_skill.py)，App计算资源准备在[build_capability.py](../packaging/app/build_capability.py)，平台App构建在[build_app.py](../packaging/app/build_app.py)。原有检查会处理包内文件和版本一致性，不需要在日常录入说明中手工重复这些工作。

构建产生的`versions/`用于保留旧版本，不要为了让新包通过而覆盖或删除旧归档。macOS和Windows分别在对应系统检查，不能只在一个系统成功就说两边都好了。

</details>

### 11.3 更新两份Demo

两份位置分别是仓库里的`demo/`，以及桌面`demo/OptionHelper demo`。用现有[demo/tools/refresh_demo.py](../demo/tools/refresh_demo.py)更新：它先根据当前源码生成Demo，再备份旧桌面Demo并同步过去。可以在编辑器中选择这个文件，使用原来的Python运行。

运行结束后分别打开两份Demo，查找新产品、查看默认收益图、切换页面和主题，确认都更新了。构建所需Node依赖见[Demo说明](../demo/README.md)。

Demo里的历史报告不会因为重新生成页面就自动重新计算。如果需要展示新产品案例，要另行准备相应示例。静态Demo只能用于页面和资料演示，正式计算仍在App或Skill中完成。

## 12. 录入时常见问题

| 遇到的问题 | 通常先看哪里 | 怎么处理 |
| --- | --- | --- |
| 产品列表没有新名字 | 三个产品文件中的编号、名称、启用状态 | 确认都已保存，重启开发程序；安装版还需要重新打包 |
| 能选到产品，但没有默认图 | json和svg文件夹 | 检查两份文件是否都有，名称是否完全一致 |
| 规则编译报错 | optionreg.py的字段和表达式 | 对照同类产品与参数词典，确认没有错拼或未支持的新写法 |
| 写了analytical却不能解析定价 | model_router和ProductPricingAdapter | 需要真实解析支持，不能只改方法名称 |
| 修改后仍是旧条款 | 正在运行的程序 | 完整重启后端；确认打开的是开发版还是旧安装版 |
| 到期收益不对 | paths的分段、方向、付款时间 | 用一个能手算的价格逐项对照，先看现金流再看总额 |
| 敲入敲出边界不对 | monitor、condition和domain | 检查大于与大于等于、观察日和同日事件顺序 |
| 回测数据不足 | 合同区间、日历、HV历史窗口 | 补齐真正缺少的行情，不用删日期消除错误 |
| 报告显示旧参数 | 报告引用的运行结果 | 用新参数重新计算，再选择这次结果生成报告 |
| 金额看起来不对 | 本金、字段单位和金额附表 | 区分每百元、金额、收益率和概率，不能全部乘本金 |
| Windows窗口或启动画面异常 | Windows原生壳 | 在Windows实际打开检查，不只改网页CSS |
| 一份Demo更新另一份没更新 | Demo同步入口与目标目录 | 用同一次refresh_demo运行更新两份，再分别打开 |

修复时记下一个最小例子，例如具体产品、执行价、到期价和错误金额。这样的描述方便别人接手，也方便下一次确认这个问题有没有复发。

## 13. 日常维护只需留下这些说明

不用把每次录入变成一堆表格。保留一份简短记录，让下一位维护者知道这次改了什么、怎么验证、出问题去哪找旧版。

### 13.1 新产品交接怎么写

<details>
<summary>可以直接填写的交接模板</summary>

```text
产品名称：
产品编号：
主要条款：

修改的文件：
- 产品目录：
- 中文说明：
- 程序规则：
- 默认收益图：
- 额外修改的计算或页面代码：

已经实际运行：
- 收益图：
- 定价及Greeks：
- 回测：
- 推荐与报告：

暂时不支持的功能：
旧版本保留在哪里：
App、Skill和两份Demo是否已经更新：
```

不适用的项目写清楚原因，不要一律填“通过”。例如仅支持MC，就明确写不支持解析定价。

</details>

### 13.2 修改规则怎么说明

写清楚原条款、新条款和应该变化的例子。例如“原来价格等于障碍不触发，现在应触发；其他条件不变”。记录改了哪个产品、rule_revision从几改到几，以及对应中文说明和收益图是否一起更新。

### 13.3 修改工具参数怎么说明

记录参数名称、用户在哪里填写、默认值和后端在哪里使用。留一个不填和一个填写后的实际运行例子，方便别人检查是否传错或遗漏。

### 13.4 修改报告怎么说明

说明改的是内容还是样式，附一份修改前后的同输入报告。标注哪些格式已经打开检查，哪些还没有尝试。

### 13.5 更新安装版怎么说明

写明App和Skill版本、安装物的位置、哪台系统实际运行过，以及两份Demo是否更新。保留旧安装物的位置，出问题可以直接找回。

### 13.6 出错以后记什么

记录产品、输入、操作步骤、预期结果和实际结果。补上修复位置，再用原来的例子重跑。涉及客户信息先脱敏，不把账号和密码带进记录。

### 13.7 这份手册以后怎么更新

正文保留在[maintainer-guide.md](maintainer-guide.md)，离线阅读版保留在[maintainer-guide.html](maintainer-guide.html)。修改正文后同步更新HTML内容，沿用现有字体和样式；增删章节时一起调整左侧目录链接。当前不保留生成器。

保存后检查两份内容与链接一致，HTML的目录、折叠、公式及离线阅读仍然有效。同步检查[update-option](update-option/SKILL.md)中的维护路径。将maintenance目录纳入源码版本管理即可，不需要提交整个docs目录。手册更新不会自动更新安装App、业务Skill或Demo。
