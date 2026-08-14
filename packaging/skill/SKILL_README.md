# OptionHelper Skill安装与配置

这份README只解决四件事：把Skill装到项目里、选择Python、配置按需能力、确认运行边界。对话与工作流以`SKILL.md`为准，模块输入输出以`references/module-guides/`为准。

## 快速开始

首次使用按以下顺序完成一次即可：

1.把Skill解压到Agent Host的项目级Skill目录。
2.需要运行计算时，先只读枚举本机可用Python环境并让用户选择；纯知识咨询不执行Python时不枚举也不询问环境。
3.用选定环境完成依赖和Store检查。缺依赖时先征得用户同意，再安装锁定版本。
4.只有任务需要新行情时才检查iFind。当前对话Agent本身就是对话模型，不要再配置模型地址、模型名称或模型API Key。

配置完成后，用户可以直接提问、指定模块、调整参数或要求结构推荐。Agent负责调用现成模块；用户不需要手写JSON、Python或HTML。

## 1.安装

将ZIP解压到Agent Host的项目级Skill目录。解压后应直接看到`SKILL.md`、`README.md`、`scripts/`、`references/`和`assets/`，不要再多套一层目录。Claude Code可使用`.claude/skills/option-helper/`；其他Host使用自身的项目级Skill目录或工作区扩展目录。不要安装到全局用户目录。

从当前项目根目录设置Skill根目录：

```bash
export OPTIONHELPER_SKILL_ROOT="$PWD/.claude/skills/option-helper"
test -f "$OPTIONHELPER_SKILL_ROOT/SKILL.md"
```

其他Host只需把第一行改为实际项目级安装目录。

## 2.Python与依赖

运行Skill需要Python3.11或更高版本。用户已说明环境或解释器路径，或者Host已经设置`OPTIONHELPER_PYTHON`时，直接复用，不重复询问。

需要执行Python但尚未选定环境时，Agent必须在第一次执行前先只读枚举本机可用环境：优先读取Conda环境清单，同时检查当前`CONDA_PREFIX`、`VIRTUAL_ENV`和PATH中的独立解释器。对绝对路径去重后，向用户展示环境名称、可安全取得的Python版本和解释器绝对路径，再让用户选择。Agent不得自行选择第一个、当前、base或任何看似可用的环境；用户选择前不得运行候选解释器、依赖检查或模块。

用户选择后，本次任务的依赖检查、数据获取、参数重算、模块计算、报告整理、HTML或PDF渲染及Python子进程都使用同一个`OPTIONHELPER_PYTHON`：

```bash
export OPTIONHELPER_PYTHON='/Python可执行文件的绝对路径'
"$OPTIONHELPER_PYTHON" --version
```

不得在中途改用裸`python`、`python3`、系统Python、base或其他环境。安装依赖只能使用`"$OPTIONHELPER_PYTHON" -m pip`。只有当前环境无法继续且用户明确同意更换时，才重新选择环境；更换后重新运行依赖检查。

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
| Pillow | 12.3.0 | PDF图像与字体依赖 |

先检查依赖：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-dependencies
```

检查结果会列出锁定版本、当前版本和`ok`、`missing`或`version_mismatch`状态。只要有缺失、版本不符或Python版本不支持，立即停止模块执行，让用户选择在已选环境安装锁定依赖，或改选另一个环境。不得自动安装、擅自换环境或回退到系统Python。

只有用户明确授权安装后才执行：

```bash
"$OPTIONHELPER_PYTHON" -m pip install -r "$OPTIONHELPER_SKILL_ROOT/scripts/requirements.lock"
```

安装后重新检查，返回`"ok": true`才进入模块运行。安装失败时保留原始错误并停止，不循环尝试其他环境。Skill不会自动创建环境，也不会擅自安装或升级依赖。

## 3.首次配置：只检查需要的能力

### 3.1已经在对话的Agent

如果Claude、DeepSeek、Codex或其他Agent已经能回答用户请求，当前Agent本身就是对话模型，不要再配置模型地址、模型名称或模型API Key。

首次只确认：

1.Python与依赖检查通过；
2.项目级Store路径预检通过；
3.仅当任务需要获取新数据时，才配置iFind Refresh Token。

普通咨询、产品比较、条款解释、只做结构推荐和Payoffer不需要数据凭据。基于当前任务已有已验证结果生成交付物，也不重新检查数据凭据。

### 3.2iFind凭据

实时取数只配置长期有效的`IFIND_REFRESH_TOKEN`。Skill自动换取短期访问凭据，不要求用户维护Access Token。

优先由Host凭据库注入。临时验证可在同一终端安全输入：

```bash
read -rs "IFIND_REFRESH_TOKEN?粘贴iFind Refresh Token后按Enter: "
echo
export IFIND_REFRESH_TOKEN
```

不得把真实Token写入Skill目录、README、请求文件、日志或版本库。

### 3.3按任务预检，不固定盘问

| 请求 | 数据能力预检 |
|---|---|
| 咨询、比较、结构推荐、Payoffer | 不检查iFind |
| Pricer、Backtester或需要新行情的正式交付 | 先复用当前任务合格的DataAssetRef；无法复用时检查`IFIND_REFRESH_TOKEN` |
| 基于已有ModuleRunRef生成交付物 | 不重新取数或检查iFind |

需要新数据时，先运行不读取、不输出凭据值、不发起网络或数据请求的预检：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-data-api
```

未配置时，在取数和计算前一次提示：“当前数据服务尚未配置。请先配置iFind Refresh Token；配置完成后我会继续当前任务。”保留已确认条件，不得先运行、失败后再追问，也不得无提示改用本地CSV。

已经配置但调用失败时，分别说明凭据无效或过期、网络不可用、Provider异常和数据权限不足，不要统称为“未配置”。

## 4.Store

Skill安装目录只读。Host首次运行时自动初始化安装目录之外的项目Store：项目目录下的`./data`、`./result`和`./.optionhelper/runtime`，不要求用户逐次确认Store位置：

```bash
mkdir -p "$PWD/.optionhelper/runtime" "$PWD/data" "$PWD/result"
export OPTIONHELPER_RUNTIME_ROOT="$PWD/.optionhelper/runtime"
export OPTIONHELPER_DATA_ROOT="$PWD/data"
export OPTIONHELPER_RESULT_ROOT="$PWD/result"
```

只有用户明确指定其他受控Store，或默认目录不可写时，才使用环境变量覆盖。三个目录必须位于Skill安装目录之外且互不复用。预检验证路径边界，不以此宣称已完成真实数据请求或写入测试。

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-store
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" --list
```

## 5.项目级入口与能力范围

README只说明安装、依赖、凭据、Store和Host能力。运行行为以`SKILL.md`为准。三种工作流如下：

| 场景 | 正常行为 | iFind要求 |
|---|---|---|
| 咨询、比较、结构解释 | 当前Agent读取资料并回答；需要收益机制时调用Payoffer | 否 |
| 指定模块或参数重算 | 调用用户指定或受参数变化影响的模块 | Payoffer否；Pricer和Backtester按需 |
| 结构推荐与正式交付 | Recommender筛选结构；按需计算；Reporter→Designer交付 | 需要新数据时才检查 |

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

### 5.1当前对话Agent模式

当前对话Agent通过受控结构化入口运行，不需要用户填写JSON。研究简报固定为结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险，无损益图；完整研究报告采用连续A4正文，固定为核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。完整HTML在宽屏提供左侧章节目录，PDF保留同一正文顺序但不显示导航。两类交付默认HTML；任何报告都必须由Reporter整理已验证结果，再由Designer生成。不得让模型自行拼接模块JSON，不需要临时`.py`或手工HTML。

项目级独立CLI当前只支持单份HTML。PDF、两份同时交付和Host内已有结果选择需要正式Reporter和Designer端口；能力不足时明确返回不可用，不静默降级。

### 5.2无对话Agent的批处理模式

只有在没有现成对话模型、确实需要终端独立理解自然语言时，才配置兼容模型端点：

```bash
export OPTIONHELPER_MODEL_BASE_URL='https://模型服务地址/compatible-api'
export OPTIONHELPER_MODEL='已验证的模型名称'
read -rs 'OPTIONHELPER_MODEL_API_KEY?粘贴模型API Key后按Enter: '
echo
export OPTIONHELPER_MODEL_API_KEY
```

从研究项目根目录执行：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" --project-request '我认为000300.SH未来会上涨，波动变大，给我推荐一个期权结构，并且生成完整研究报告'
```

该入口执行Recommender→DataFetcher→Payoffer→Pricer→Backtester→Reporter→Designer，不需要临时`.py`或手工HTML。缺少模型配置、iFind Refresh Token、网络、数据权限或依赖时，一次返回明确缺口。

## 6.Host能力矩阵

| 能力层级 | Host必须提供 | 可用能力 | 缺失时的处理 |
|---|---|---|---|
| 知识咨询 | 读取`SKILL.md`及资料 | 产品解释、比较、风险分析 | 说明资料缺口，不编造 |
| 单模块运行 | Python、依赖、外部Store、模块调用能力 | 收益结构、估值或回测 | 返回明确不可用及缺失能力 |
| 实时数据 | `IFIND_REFRESH_TOKEN`和网络 | iFind数据获取与刷新 | 不把本地文件作为无提示回退 |
| 受控计算 | CallerContext、ModuleHostContext、合同和Store | 正式Payoffer、Pricer、Backtester | 不自行拼造身份、合同或结果 |
| 结构推荐 | 当前对话Agent或等价受控适配 | 固定推荐流程与候选审阅 | 不生成伪候选 |
| 正式报告 | 已验证结果、Reporter和Designer端口 | HTML或PDF | 不自行写脚本或HTML替代 |

Host只要具备对应能力即可接入，不绑定某一模型厂商。普通研究对话默认先给专业结论；原始JSON只在用户明确要求或系统集成时展示。

## 7.常见问题

**为什么Agent不应再次询问模型服务？**
当前对话模型已经承担理解与沟通。只有终端独立批处理才需要额外模型端点。

**为什么没有马上检查iFind？**
咨询、比较、结构推荐和Payoffer不需要行情。Pricer、Backtester或需要新数据的正式交付才检查。

**缺少Python库会怎样？**
模块不会启动。Agent一次列出缺失项和版本差异，等待用户授权安装或改选环境。

**报告由谁生成？**
Reporter整理已验证事实，Designer按固定模板渲染。模型不得自己写HTML、PDF、SVG或临时程序。

**结果保存在哪里？**
数据写入项目`data/`，结果写入项目`result/`，运行元数据写入项目`.optionhelper/runtime/`；不会写到Skill安装目录。

## 8.安全边界

- 请求不得包含Token、密码、API Key或物理路径。
- 安装目录不得写入数据、结果、会话或凭据。
- iFind只使用Refresh Token换取短期访问凭据。
- 估值、Greeks、回测和报告必须来自正式模块链路，不得由模型补写。
- 正式交付必须经过Reporter→Designer；任何端口缺失都返回真实不可用状态。
