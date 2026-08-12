# OptionHelper Skill安装与配置

README只说明安装、依赖、凭据、Store和Host能力。运行行为以`SKILL.md`为准，七个模块的输入、输出和用户沟通规则以对应`module-guide.md`为准。

## 1.安装

将ZIP解压到Agent Host的项目级Skill目录。解压后应直接看到`SKILL.md`、`README.md`、`scripts/`、`references/`和`assets/`，不要再多套一层目录。Claude Code可使用`.claude/skills/option-helper/`；其他Host使用自身的项目级Skill目录或工作区扩展目录。不要安装到全局用户目录。

从当前项目根目录设置Skill根目录：

```bash
export OPTIONHELPER_SKILL_ROOT="$PWD/.claude/skills/option-helper"
test -f "$OPTIONHELPER_SKILL_ROOT/SKILL.md"
```

其他Host只需把第一行改为实际项目级安装目录。

## 2.Python与依赖

运行Skill需要Python3.11或更高版本。若用户已经说明环境或解释器路径，或者Host已经设置`OPTIONHELPER_PYTHON`，直接复用，不重复询问。需要执行Python但两者都没有时，Agent必须在第一次执行前先只读枚举本机可用环境：优先读取Conda环境清单，同时检查当前`CONDA_PREFIX`、`VIRTUAL_ENV`以及PATH中的独立解释器；对绝对路径去重后，把环境名称、可安全取得的Python版本和解释器绝对路径展示给用户选择。发现环境不等于获得使用许可，Agent不得自行选择第一个、当前、base或任何看似可用的环境，也不得在用户选择前运行候选解释器、依赖检查、研究流程或模块。没有候选时，只请求用户提供解释器绝对路径。纯知识咨询不执行Python时不枚举也不询问环境。

用户选定后，该解释器就是本次任务统一的Python运行环境。后续依赖检查、数据获取、单模块计算、参数重算、推荐流程、报告整理、HTML或PDF渲染及验证命令全部显式使用`"$OPTIONHELPER_PYTHON"`，其Python子进程也沿用同一环境。Agent不得在中途改用裸`python`、`python3`、系统Python、base或其他Conda环境；安装依赖只能使用`"$OPTIONHELPER_PYTHON" -m pip`。只有当前环境确实无法继续并且用户明确同意更换时，才重新选择环境；更换后必须重新运行依赖检查。Shell、文件检查等非Python操作不受影响。

确定解释器后设置：

```bash
export OPTIONHELPER_PYTHON='/Python可执行文件的绝对路径'
"$OPTIONHELPER_PYTHON" --version
```

锁定依赖位于`scripts/requirements.lock`：

| 库 | 版本 | 用途 |
|---|---:|---|
| numpy | 2.4.6 | 数值计算 |
| pandas | 3.0.5 | 表格与时序数据 |
| scipy | 1.18.0 | 科学计算 |
| numba | 0.65.1 | 路径计算加速 |
| requests | 2.34.2 | iFind API请求 |
| jsonschema | 4.26.0 | JSON协议校验 |
| PyYAML | 6.0.3 | 配置解析 |
| reportlab | 5.0.0 | PDF输出 |
| Pillow | 12.3.0 | PDF图像与字体运行依赖 |

选定解释器后先运行依赖和Store检查。检查结果会把每个库标为`ok`、`missing`或`version_mismatch`。只要存在缺失库、版本不符或Python版本不支持，模块就不会启动。Agent必须一次列出锁定版本、当前版本和状态，让用户选择在已选环境安装锁定依赖，或者改选另一个已发现环境；不得自动安装、擅自换环境或回退到系统Python。

只有用户明确授权安装后，才在已选解释器中执行：

```bash
"$OPTIONHELPER_PYTHON" -m pip install -r "$OPTIONHELPER_SKILL_ROOT/scripts/requirements.lock"
```

安装后运行：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-dependencies
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-store
```

返回`"ok": true`才进入模块运行。安装失败时保留原始错误并停止，不循环尝试其他环境。Skill不会自动创建Python环境，也不会擅自安装或升级依赖。

## 3.首次配置：只检查需要的能力

### 3.1已经在对话的Agent

如果Claude、DeepSeek、Codex或其他Agent已经能接收并回答用户请求，**不要再配置模型地址、模型名称或模型API Key**。当前Agent本身就是对话模型；OptionHelper只提供资料、受控计算和报告模块。

首次只需要确认：

1. Python与依赖检查通过；
2. 项目级Store可读写；
3. 仅当任务需要获取新数据时，才配置iFind Refresh Token。

普通咨询、产品比较、条款解释、只做结构推荐和Payoffer收益分析不需要数据凭据。基于当前任务已有已验证结果生成交付物，也不重新检查数据凭据。不要因为安装Skill就向用户索取第二套模型配置。

### 3.2iFind凭据

实时取数只配置长期有效的`IFIND_REFRESH_TOKEN`。Skill在取数时自动换取短期访问凭据，不要求用户维护Access Token。

优先由Host加密凭据库在启动时注入`IFIND_REFRESH_TOKEN`。临时验证可在同一终端进程中安全输入：

```bash
read -rs "IFIND_REFRESH_TOKEN?粘贴iFind Refresh Token后按Enter: "
echo
export IFIND_REFRESH_TOKEN
```

不得把真实Token写入`.claude/`、README、请求文件或版本库。

### 3.3按任务预检，不固定盘问

Agent读取用户请求后先按`SKILL.md`判定路由，再检查本次会用到的能力：

| 请求 | 数据能力预检 |
|---|---|
| 咨询、比较、结构推荐、Payoffer | 不检查iFind |
| Pricer、Backtester或需要新行情的正式交付 | 先复用当前任务合格的DataAssetRef；无法复用时检查`IFIND_REFRESH_TOKEN` |
| 基于已有ModuleRunRef生成交付物 | 不重新取数或检查iFind |

缺少数据配置时，Agent必须在调用DataFetcher、Pricer或Backtester之前一次提示：“当前数据服务尚未配置。请先配置iFind Refresh Token；配置完成后我会继续当前任务。”任务条件继续保留。不得先运行、失败后再追问；不得要求Access Token；不得把本地CSV作为无提示回退。

凭据已经配置但调用失败时，Host应区分凭据无效或过期、网络不可用、Provider异常和数据权限不足。不要把所有失败都写成“未配置”。

需要新数据时，在已选解释器中运行以下预检。它只检查Host是否已配置凭据，不发起网络或数据请求，也不读取或输出凭据值：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-data-api
```

## 4.Store

Skill安装目录只读。项目级Skill默认把数据和结果放在安装目录之外的项目Store：当前项目的`./data`和`./result`，运行元数据放在`./.optionhelper/runtime`。正式报告默认保存到`$PWD/result`的受控子目录。Host首次运行时自动初始化以下目录并注入变量，不要求用户逐次确认Store位置：

```bash
mkdir -p "$PWD/.optionhelper/runtime" "$PWD/data" "$PWD/result"
export OPTIONHELPER_RUNTIME_ROOT="$PWD/.optionhelper/runtime"
export OPTIONHELPER_DATA_ROOT="$PWD/data"
export OPTIONHELPER_RESULT_ROOT="$PWD/result"
```

只有用户明确指定其他受控Store，或默认目录不可写时，才通过环境变量覆盖这些默认位置。三者必须都在Skill安装目录之外且互不复用。随后运行：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-store
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" --list
```

## 5.项目级入口与能力范围

用户意图的三种工作流只在`SKILL.md`定义。README只说明各类入口需要的能力，不能改变路由、追问或默认值。当前Agent不能自行写Python、HTML、SVG或PDF替代模块输出。

| 场景 | 入口与结果 | iFind要求 |
|---|---|---|
| 咨询、比较、结构解释 | 当前Agent读取资料并回答；需要收益机制时调用Payoffer | 否 |
| 指定模块或参数重算 | 当前Agent调用用户指定的一个或多个模块 | Payoffer否；Pricer/Backtester按需 |
| 结构推荐与正式交付 | Recommender形成主候选；已有结果直接进入Reporter→Designer，否则运行本次交付所需模块后再交付 | 需要新数据时才检查 |

在当前对话Agent模式中，用户不需要手动运行命令或填写模型配置。Agent将研究结论转换为受控请求，正式交付的请求形态如下，仅供Host实现和调试参考：

```json
{
  "prompt": "沪深300未来上涨，波动率可能上升，生成完整研究报告",
  "constraints": {"horizon": "3个月"},
  "selection": {
    "product_id": "2.1",
    "underlyings": ["000300.SH"],
    "reason": "与上涨观点及期限匹配",
    "main_risks": ["到期未上涨可能损失期权费"]
  },
  "term_overrides": {"T": 0.25}
}
```

Skill会验证产品是否属于已发布目录，并自行创建合同、数据引用、计算运行和报告目录。Agent不能传入路径、哈希、运行标识、模块结果或内部候选标识。研究简报与完整研究报告默认HTML；完整研究报告默认连续A4正文，不提供目录版，章节固定为核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。

当前对话Agent模式通过结构化入口运行已完成的选择与参数。项目级独立CLI当前只支持单份HTML；PDF、两份同时交付以及Host内已有结果选择，需要提供正式Reporter和Designer端口的App或Host。能力不足时必须明确返回不可用，不能静默降级或手工补写文件。

### 5.1无对话Agent的批处理模式

只有在没有现成对话模型、确实需要在终端由`--project-request`理解自然语言时，才配置一个兼容模型端点：

```bash
export OPTIONHELPER_MODEL_BASE_URL='https://模型服务地址/v1'
export OPTIONHELPER_MODEL='已验证的模型名称'
read -rs 'OPTIONHELPER_MODEL_API_KEY?粘贴模型API Key后按Enter: '
echo
export OPTIONHELPER_MODEL_API_KEY
```

然后从研究项目根目录执行：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" --project-request '我认为000300.SH未来会上涨，波动变大，给我推荐一个期权结构，并且生成完整研究报告'
```

该批处理入口执行Recommender→DataFetcher→Payoffer→Pricer→Backtester→Reporter→Designer，不需要临时`.py`或手工HTML。缺少模型配置、iFind Refresh Token、网络、数据权限或运行依赖时，入口只返回一次明确配置缺口。

批处理入口在执行前完成模型与数据预检。它不能把当前对话Agent的身份误判为还需要第二个模型；只有真正从终端独立理解自然语言时才使用本节配置。

## 6.Host能力矩阵

| 能力层级 | Host必须提供 | 可用能力 | 缺失时的处理 |
|---|---|---|---|
| 知识咨询 | 读取`SKILL.md`及引用资料 | 产品解释、比较、风险分析 | 说明资料缺口，不编造 |
| 单模块运行 | Python、依赖、外部Store、模块调用能力 | 支持的数据、收益结构、估值或回测 | 返回明确不可用及缺失能力 |
| 实时数据 | 单模块能力、`IFIND_REFRESH_TOKEN`、网络访问 | iFind数据获取与刷新 | 不把本地文件作为无提示回退 |
| 受控计算 | CallerContext、ModuleHostContext、合同和受控Store | 正式Payoffer、Pricer、Backtester运行 | 不自行拼造身份、合同或结果 |
| 结构推荐 | 当前对话Agent，或AgentPort、KnowledgePort、ToolPort等价受控适配 | 固定推荐流程与候选审阅 | 返回明确不可用，不生成伪候选 |
| 正式报告 | 已验证模块结果、Reporter端口、Designer端口 | Reporter整理事实后由Designer生成HTML或PDF | 任一端口缺失都明确报告能力不可用，不自行写脚本或HTML替代 |

Host只要具备对应能力即可接入，不要求某一厂商的专属命令。普通研究对话默认先给专业结论；原始JSON只用于用户明确要求或系统集成。具体路由、默认报告格式、进度文案和失败行为全部由`SKILL.md`及模块指南规定。

## 7.安全检查

- 请求不得包含Token、密码、API Key或物理路径。
- 安装目录不得写入数据、结果、会话或凭据。
- iFind只以Refresh Token换取短期访问凭据。
- 估值、Greeks、回测和报告必须来自正式模块链路，不得由模型补写。
- 正式交付必须由Reporter→Designer生成；当前Agent不得创建替代HTML、PDF、SVG或临时程序。
