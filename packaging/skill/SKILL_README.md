# OptionHelper Skill安装与配置

这份README只解决安装、Capability Runtime配置、iFind和项目Store。`scripts/runtime`是七个期权业务模块共用的Capability Runtime，不是Agent Runtime，也不创建或管理Agent。对话与工作流以`SKILL.md`为准，模块输入输出以`references/module-guides/`为准。

## 快速开始

纯咨询、比较和条款解释只读取资料，不需要Python、iFind或Store。首次需要运行模块时，先完成第1至3步；只有需要实时数据时才继续第4步：

1.把Skill解压到当前项目的Skill目录。
2.只读枚举本机可用Python环境并让用户选择。
3.用选定环境检查锁定依赖；缺依赖时先征得用户同意，再安装锁定版本并复检。
4.首次需要实时数据前，安全配置iFind Refresh Token。
5.运行入口初始化并检查项目级Store。

配置完成后，用户可以直接提问、指定模块、调整参数或要求结构推荐。当前对话负责调用现成模块；用户不需要填写JSON，不需要手写Python或HTML，也不需要临时`.py`或手工HTML。

对话框架执行完整项目工作流时，使用Skill自带的`scripts/tool_entry.py --project-json`公开入口；不要导入内部Python函数，也不要在临时目录编写驱动脚本。Monte Carlo不设置最低路径数，Pricer结果会保留实际路径数、标准误和精度状态。

## 1.安装

将ZIP解压到当前项目的项目级Skill目录。解压后应直接看到`SKILL.md`、`README.md`、`scripts/`、`references/`和`assets/`，不要再多套一层目录。安装位置由宿主的项目级Skill机制决定；不要安装到全局用户目录。

从当前项目根目录设置Skill根目录：

macOS/Linux（Bash）：

```bash
export OPTIONHELPER_SKILL_ROOT="$PWD/.skills/option-helper"
test -f "$OPTIONHELPER_SKILL_ROOT/SKILL.md"
```

Windows（PowerShell）：

```powershell
$env:OPTIONHELPER_SKILL_ROOT = Join-Path (Get-Location) ".skills\option-helper"
if (-not (Test-Path -LiteralPath (Join-Path $env:OPTIONHELPER_SKILL_ROOT "SKILL.md") -PathType Leaf)) { throw "SKILL.md不存在" }
```

其他运行环境只需把路径改为实际项目级安装目录；不要安装到全局用户目录。

## 2.Python与依赖

运行Skill需要Python3.11或更高版本。首次需要运行Python时，用户已说明环境或解释器路径，或者运行环境已经设置`OPTIONHELPER_PYTHON`时，直接复用，不重复询问。

首次需要运行Python但尚未选定环境时，Agent必须先只读枚举本机可用环境：优先读取Conda环境清单，同时检查当前`CONDA_PREFIX`、`VIRTUAL_ENV`和PATH中的独立解释器。对绝对路径去重后，向用户展示环境名称、可安全取得的Python版本和解释器绝对路径，再让用户选择。Agent不得自行选择第一个、当前、base或任何看似可用的环境；用户选择前不得运行候选解释器、依赖检查或模块。没有Python3.11或更高版本候选时，先说明缺口并询问是否授权安装或恢复一个受支持环境；得到授权后才使用本机安装方式，完成后重新枚举和选择。

用户选择后，本次任务的依赖检查、数据获取、参数重算、模块计算、报告整理、HTML或PDF渲染及Python子进程都使用同一个`OPTIONHELPER_PYTHON`：

macOS/Linux（Bash）：

```bash
export OPTIONHELPER_PYTHON='/Python可执行文件的绝对路径'
"$OPTIONHELPER_PYTHON" --version
```

Windows（PowerShell）：

```powershell
$env:OPTIONHELPER_PYTHON = 'C:\Python311\python.exe'
& $env:OPTIONHELPER_PYTHON --version
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

macOS/Linux（Bash）：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-dependencies
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON (Join-Path $env:OPTIONHELPER_SKILL_ROOT "scripts\environment_check.py") --check-dependencies
```

检查结果会列出锁定版本、当前版本和`ok`、`missing`或`version_mismatch`状态。只要有缺失、版本不符或Python版本不支持，立即停止模块执行，让用户选择在已选环境安装锁定依赖，或改选另一个环境。不得自动安装、擅自换环境或回退到系统Python。

只有用户明确授权安装后才执行。默认优先使用清华PyPI镜像；镜像不可用时保留原始错误，不自动切换其他软件源：

macOS/Linux（Bash）：

```bash
"$OPTIONHELPER_PYTHON" -m pip install -r "$OPTIONHELPER_SKILL_ROOT/scripts/requirements.lock" \
  --progress-bar on \
  --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON -m pip install -r (Join-Path $env:OPTIONHELPER_SKILL_ROOT "scripts\requirements.lock") `
  --progress-bar on `
  --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
```

安装后必须重新检查依赖；只有依赖就绪的模块才可运行。需要数据或正式交付时，再执行对应的完整就绪检查。安装失败时保留原始错误并停止，不循环尝试其他环境。Skill不会自动创建环境，也不会擅自安装或升级依赖。

依赖检查成功后会在项目级`.optionhelper/runtime/dependency-readiness.json`保存不含凭据的就绪指纹。解释器、Python版本、平台、锁定文件或环境包目录发生变化时自动完整复检；缓存缺失、损坏或无法确认状态时按首次检查处理。iFind配置和Store边界每次仍检查。正式发布始终忽略缓存并完整检查全部锁定依赖。

### 用户可见阶段进度

Agent执行环境检查、经确认后的依赖安装与复检时，首次或环境变化显示`[进行中] 正在检查运行环境和锁定依赖`；命中有效检查记录时显示`[完成] 运行条件已就绪`。缺依赖时先列出锁定版本、当前版本和状态，用户确认后才安装，安装完成后完整复检。安装阶段内部保留pip逐项下载、安装输出，并启用`--progress-bar on`；不得用静默参数隐藏过程。完成后明确显示“已完成”；失败时保留当前阶段、原始失败原因和下一步。依赖下载没有可靠总量时不伪造百分比或剩余时间，也不因同一阶段反复发送消息。

## 3.首次配置：运行时与数据就绪

当前对话负责理解请求，Skill负责调用受控模块。实时数据使用长期有效的`IFIND_REFRESH_TOKEN`，只从项目级`.optionhelper/memory.md`读取。首次需要iFind数据时，在项目目录执行：

macOS/Linux（Bash）：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" \
  --save-ifind-refresh-token --project-root "$PWD"
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON (Join-Path $env:OPTIONHELPER_SKILL_ROOT "scripts\environment_check.py") `
  --save-ifind-refresh-token --project-root (Get-Location)
```

不得把真实Token写入Skill目录、README、请求文件、日志或版本库。Windows不要求在README中粘贴凭据；宿主环境或系统凭据管理器中的凭据也不回显。项目级`.optionhelper/memory.md`仅在用户明确确认保存后写入，属于明文本地配置，不是加密保险箱。iFind只使用Refresh Token，不要求用户维护Access Token。预检不输出凭据值，也不发起网络或数据请求：

macOS/Linux（Bash）：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" \
  --check-readiness --skill-root "$OPTIONHELPER_SKILL_ROOT" --project-root "$PWD"
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON (Join-Path $env:OPTIONHELPER_SKILL_ROOT "scripts\environment_check.py") `
  --check-readiness --skill-root $env:OPTIONHELPER_SKILL_ROOT --project-root (Get-Location)
```

完整检查会返回依赖、数据API、Store和下一步操作。缺依赖时一次列出锁定版本和当前状态，等用户确认后才安装；缺iFind或Store时只报告相应缺口并保留原任务。它用于数据获取、依赖新市场数据的计算、首次正式交付和项目级运行，不阻断纯咨询、基于已有合同的Payoffer或已验证事实的重新渲染。实际数据调用失败时，分别说明凭据无效或过期、网络不可用、Provider异常和数据权限不足，不要统称为“未配置”。

## 4.Store

Skill安装目录只读。运行入口首次执行时自动初始化安装目录之外的项目Store：项目目录下的`./data`、`./result`和`./.optionhelper/runtime`，不要求用户逐次确认Store位置。macOS/Linux使用目录句柄完成防替换校验；Windows使用等价的根目录重复校验与非符号链接检查，不依赖POSIX专有的`dir_fd`或`O_DIRECTORY`：

macOS/Linux（Bash）：

```bash
mkdir -p "$PWD/.optionhelper/runtime" "$PWD/data" "$PWD/result"
export OPTIONHELPER_RUNTIME_ROOT="$PWD/.optionhelper/runtime"
export OPTIONHELPER_DATA_ROOT="$PWD/data"
export OPTIONHELPER_RESULT_ROOT="$PWD/result"
```

Windows（PowerShell）：

```powershell
$projectRoot = (Get-Location).Path
New-Item -ItemType Directory -Force (Join-Path $projectRoot ".optionhelper\runtime"), (Join-Path $projectRoot "data"), (Join-Path $projectRoot "result") | Out-Null
$env:OPTIONHELPER_RUNTIME_ROOT = Join-Path $projectRoot ".optionhelper\runtime"
$env:OPTIONHELPER_DATA_ROOT = Join-Path $projectRoot "data"
$env:OPTIONHELPER_RESULT_ROOT = Join-Path $projectRoot "result"
```

只有用户明确指定其他受控Store，或默认目录不可写时，才使用环境变量覆盖。三个目录必须位于Skill安装目录之外且互不复用。预检验证路径边界，不以此宣称已完成真实数据请求或写入测试。

macOS/Linux（Bash）：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" \
  --check-readiness --skill-root "$OPTIONHELPER_SKILL_ROOT" --project-root "$PWD"
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON (Join-Path $env:OPTIONHELPER_SKILL_ROOT "scripts\environment_check.py") `
  --check-readiness --skill-root $env:OPTIONHELPER_SKILL_ROOT --project-root (Get-Location)
```

## 5.项目级入口与能力范围

README只说明安装、依赖、iFind、Store和运行能力。运行行为以`SKILL.md`为准；在需要模块时检查对应条件。三种工作流如下：

| 场景 | 正常行为 | iFind要求 |
|---|---|---|
| 咨询、比较、结构解释 | 当前Agent读取资料并回答；需要收益机制时调用Payoffer | 不需要 |
| 指定模块或参数重算 | 用户可单独调用任一模块；参数变化产生新的合同快照；已有快照可直接生成交付，已有参考报价可组合多个已保存结果 | 只有需要新市场数据时 |
| 结构推荐 | Recommender筛选结构并按需计算；只有明确交付时Reporter→Designer | 推荐本身不需要；首次取数或计算时需要 |

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

### 5.1当前对话使用方式

当前对话只需用自然语言提出需求，不填写JSON、不输入命令参数，也不手写Python或HTML。推荐与正式交付相互独立：系统仅在需要从市场观点或约束中选择结构时运行Recommender；用户已指定结构、期限、条款或已保存结果时，即使要求研究简报、完整研究报告、参考报价或多结构对比，也不进入Recommender，而是按交付需要完成必要计算后交付。用户明确要求交付但未指定格式时默认HTML。研究简报默认呈现结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险，无损益图；单结构完整研究报告默认依次呈现核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示，采用连续A4正文，宽屏HTML提供左侧目录，PDF不显示目录。用户明确要求补充说明或多结构比较时可以调整展示结构，但金融事实必须先由Reporter冻结，Designer和Presentation Patch均不得新增、修改或隐藏金融数值、合同条款、指标、表格、公式或图表。用户只要要求参考报价，当前对话默认决定应纳入的多个结构、标的、期限和参数版本；每一行完成必要定价后汇入同一份报价表，不要求用户逐行选择候选、期限、旧结果或拼接方式。后续可复用用户明确选择的已保存运行结果。用户可随时修改条款后只运行一个模块，形成新的合同快照；也可从旧版或新版快照直接生成任一交付。已有单合同事实集可在研究简报与完整研究报告之间直接转换，已有多结构事实集也可在两种报告详略之间直接转换；参考报价由本次有序选择的合同快照组合生成。上述重新渲染不重跑分析。

用户要求打开交付物时，当前宿主在Reporter→Designer成功后预览该受控HTML或PDF；不重新计算、不改写HTML，也不另建临时页面。当前宿主不能打开时，直接说明原因并返回交付路径。

项目级Skill入口按当前对话的自然语言自动完成推荐、必要计算和交付。用户进度统一显示“正在分析需求并整理候选结构”“正在完成必要计算”“正在生成多结构研究简报”，完成时显示“HTML交付已生成，正在打开”；不显示命令参数、JSON字段、内部角色名或英文交付类型。横向比较生成一份多结构研究简报或多结构完整研究报告，不拆成多份单结构文件。研究简报、完整研究报告和组合参考报价共用同一交付服务：首次参考报价按当前对话确定的组合完成必要定价，已有事实集可直接重渲染。PDF和应用内报价使用同一正式Reporter和Designer端口；能力不足时仅说明缺少的能力，不静默降级。

## 6.运行能力矩阵

| 能力层级 | 所需条件 | 可用能力 | 缺失时的处理 |
|---|---|---|---|
| 知识咨询 | 读取`SKILL.md`及资料 | 产品解释、比较、风险分析 | 说明资料缺口，不编造 |
| 单模块运行 | Python、依赖、外部Store、模块调用能力 | 收益结构、估值或回测 | 返回明确不可用及缺失能力 |
| 实时数据 | 项目级`memory.md`中的`IFIND_REFRESH_TOKEN`和网络 | iFind数据获取与刷新 | 用户确认保存后自动复用，不读取环境变量 |
| 受控计算 | CallerContext、ModuleHostContext、合同和Store | 正式Payoffer、Pricer、Backtester | 不自行拼造身份、合同或结果 |
| 结构推荐 | 当前对话与Recommender | 自动适配宿主能力，用户无需选择模式；无法证明独立子任务时按同一流程完成 | 不生成伪候选或伪称多Agent |
| 正式交付 | 已验证结果、Reporter和Designer端口 | 研究简报、完整研究报告或参考报价的HTML或PDF | 不自行写脚本或HTML替代 |

普通研究对话默认先给专业结论；原始JSON只在用户明确要求或系统集成时展示。

## 7.常见问题

**什么时候检查iFind？**
在首次取数、依赖新市场数据的估值或回测、或首次需要取数的正式交付前检查。纯咨询、收益机制解释以及从已验证事实重渲染交付不要求iFind；预检不显示或记录凭据、不下载行情，也不发送研究内容。

**缺少Python库会怎样？**
模块不会启动。Agent一次列出缺失项和版本差异，等待用户授权安装或改选环境。

**报告和参考报价由谁生成？**
Reporter负责整理或复用已验证合同快照，Designer按固定模板渲染。首次参考报价由当前对话确定组合后完成必要定价，再由Reporter汇总；后续可直接组合多个已保存运行结果。用户可随时改条款并单独运行任一模块；已保存事实集可以直接补生成研究简报、完整研究报告或另一种格式。模型不会把未经验证的聊天文本当作正式交付事实。

结构推荐会自动判断宿主是否支持独立子任务。支持时内部执行多Agent，不支持时执行同一推荐流程的单Agent路径；用户无需配置模型或选择Agent模式。普通进度只显示需求分析、候选整理、必要计算和交付生成，不显示内部角色、会话、运行标识或端口字段。咨询、指定产品、参数重算和单模块请求默认不创建子Agent。

**结果保存在哪里？**
数据写入项目`data/`，结果写入项目`result/`，运行元数据写入项目`.optionhelper/runtime/`；不会写到Skill安装目录。

## 8.安全边界

- 请求不得包含Token、密码、API Key或物理路径。
- 安装目录不得写入数据、结果、会话或凭据。
- iFind只使用Refresh Token换取短期访问凭据。
- 估值、Greeks、回测和报告必须来自正式模块链路，不得由模型补写。
- 新的正式交付事实组合必须经过Reporter→Designer；已保存的冻结Designer输入可直接重渲染为另一种交付形式。任何端口缺失都返回真实不可用状态。
