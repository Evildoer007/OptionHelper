# OptionHelper Skill安装与配置

这份README只解决首次安装和统一就绪配置。对话与工作流以`SKILL.md`为准，模块输入输出以`references/module-guides/`为准。

## 快速开始

首次使用必须按以下顺序完成；Python、依赖、iFind和Store全部就绪前，不进入咨询、推荐、计算或交付：

1.把Skill解压到当前项目的Skill目录。
2.只读枚举本机可用Python环境并让用户选择。
3.用选定环境检查锁定依赖；缺依赖时先征得用户同意，再安装锁定版本并复检。
4.安全配置iFind Refresh Token。
5.初始化并检查项目级Store。

配置完成后，用户可以直接提问、指定模块、调整参数或要求结构推荐。Agent负责调用现成模块；用户不需要填写JSON，不需要手写Python或HTML。

## 1.安装

将ZIP解压到当前项目的Skill目录。解压后应直接看到`SKILL.md`、`README.md`、`scripts/`、`references/`和`assets/`，不要再多套一层目录。Claude Code可使用`.claude/skills/option-helper/`；其他运行环境使用自身的项目级Skill目录或工作区扩展目录。不要安装到全局用户目录。

从当前项目根目录设置Skill根目录：

```bash
export OPTIONHELPER_SKILL_ROOT="$PWD/.claude/skills/option-helper"
test -f "$OPTIONHELPER_SKILL_ROOT/SKILL.md"
```

其他运行环境只需把第一行改为实际项目级安装目录。

## 2.Python与依赖

运行Skill需要Python3.11或更高版本。首次配置时，用户已说明环境或解释器路径，或者运行环境已经设置`OPTIONHELPER_PYTHON`时，直接复用，不重复询问。

首次配置但尚未选定环境时，Agent必须先只读枚举本机可用环境：优先读取Conda环境清单，同时检查当前`CONDA_PREFIX`、`VIRTUAL_ENV`和PATH中的独立解释器。对绝对路径去重后，向用户展示环境名称、可安全取得的Python版本和解释器绝对路径，再让用户选择。Agent不得自行选择第一个、当前、base或任何看似可用的环境；用户选择前不得运行候选解释器、依赖检查或模块。没有Python3.11或更高版本候选时，先说明缺口并询问是否授权安装或恢复一个受支持环境；得到授权后才使用本机安装方式，完成后重新枚举和选择。

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

安装后必须运行统一就绪检查，只有返回`"ok": true`才可进入任何工作流。安装失败时保留原始错误并停止，不循环尝试其他环境。Skill不会自动创建环境，也不会擅自安装或升级依赖。

## 3.首次配置：统一就绪门禁

当前对话负责理解请求，Skill负责调用受控模块。实时数据使用长期有效的`IFIND_REFRESH_TOKEN`。优先由本机安全凭据配置注入；临时验证可在同一终端安全输入：

```bash
read -rs "IFIND_REFRESH_TOKEN?粘贴iFind Refresh Token后按Enter: "
echo
export IFIND_REFRESH_TOKEN
```

不得把真实Token写入Skill目录、README、请求文件、日志或版本库。iFind只使用Refresh Token，不要求用户维护Access Token。预检不读取、不输出凭据值，也不发起网络或数据请求：

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" \
  --check-readiness --skill-root "$OPTIONHELPER_SKILL_ROOT" --project-root "$PWD"
```

检查会统一返回依赖、数据API、Store和下一步操作。缺依赖时一次列出锁定版本和当前状态，等用户确认后才安装；缺iFind或Store时只报告相应缺口并保留原任务。通过前不得进入任何工作流。实际数据调用失败时，分别说明凭据无效或过期、网络不可用、Provider异常和数据权限不足，不要统称为“未配置”。

## 4.Store

Skill安装目录只读。运行入口首次执行时自动初始化安装目录之外的项目Store：项目目录下的`./data`、`./result`和`./.optionhelper/runtime`，不要求用户逐次确认Store位置。macOS/Linux使用目录句柄完成防替换校验；Windows使用等价的根目录重复校验与非符号链接检查，不依赖POSIX专有的`dir_fd`或`O_DIRECTORY`：

```bash
mkdir -p "$PWD/.optionhelper/runtime" "$PWD/data" "$PWD/result"
export OPTIONHELPER_RUNTIME_ROOT="$PWD/.optionhelper/runtime"
export OPTIONHELPER_DATA_ROOT="$PWD/data"
export OPTIONHELPER_RESULT_ROOT="$PWD/result"
```

只有用户明确指定其他受控Store，或默认目录不可写时，才使用环境变量覆盖。三个目录必须位于Skill安装目录之外且互不复用。预检验证路径边界，不以此宣称已完成真实数据请求或写入测试。

```bash
"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" \
  --check-readiness --skill-root "$OPTIONHELPER_SKILL_ROOT" --project-root "$PWD"
```

## 5.项目级入口与能力范围

README只说明安装、依赖、iFind、Store和运行能力。统一就绪通过后，运行行为以`SKILL.md`为准。三种工作流如下：

| 场景 | 正常行为 | iFind要求 |
|---|---|---|
| 咨询、比较、结构解释 | 当前Agent读取资料并回答；需要收益机制时调用Payoffer | 已在统一门禁配置，不重复询问 |
| 指定模块或参数重算 | 调用用户指定或受参数变化影响的模块；已有结果交付走Reporter→Designer；参考报价可组合多个明确选择的已保存结果 | 已在统一门禁配置，不重复询问 |
| 结构推荐与正式交付 | Recommender筛选结构；按需计算；Reporter→Designer交付 | 已在统一门禁配置，不重复询问 |

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

### 5.1当前对话使用方式

当前对话通过受控结构化入口运行，不需要用户填写JSON，用户也不需要手写Python或HTML。研究简报固定为结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险，无损益图；完整研究报告采用连续A4正文，固定为核心结论、研究逻辑、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示，无章节目录。参考报价按本次明确选择的已保存结构与参数运行结果整理期限、执行价、期权费及必要条款，可组合多个结构，不把估值或回测结果当作报价。HTML和PDF均按固定顺序连续呈现。用户先生成报告、后续再需要简报或另一种格式时，直接复用已保存的冻结Designer输入重新渲染，不重跑分析；新的结构组合或参考报价则由Reporter冻结本次明确选择的事实。不得让模型自行拼接模块JSON，不需要临时`.py`或手工HTML。

项目级独立CLI当前只支持单份HTML。PDF、两份同时交付和运行环境内已有结果选择需要正式Reporter和Designer端口；能力不足时明确返回不可用，不静默降级。

## 6.运行能力矩阵

| 能力层级 | 所需条件 | 可用能力 | 缺失时的处理 |
|---|---|---|---|
| 知识咨询 | 读取`SKILL.md`及资料 | 产品解释、比较、风险分析 | 说明资料缺口，不编造 |
| 单模块运行 | Python、依赖、外部Store、模块调用能力 | 收益结构、估值或回测 | 返回明确不可用及缺失能力 |
| 实时数据 | `IFIND_REFRESH_TOKEN`和网络 | iFind数据获取与刷新 | 不把本地文件作为无提示回退 |
| 受控计算 | CallerContext、ModuleHostContext、合同和Store | 正式Payoffer、Pricer、Backtester | 不自行拼造身份、合同或结果 |
| 结构推荐 | 当前对话与受控适配 | 固定推荐流程与候选审阅 | 不生成伪候选 |
| 正式交付 | 已验证结果、Reporter和Designer端口 | 研究简报、完整研究报告或参考报价的HTML或PDF | 不自行写脚本或HTML替代 |

普通研究对话默认先给专业结论；原始JSON只在用户明确要求或系统集成时展示。

## 7.常见问题

**为什么首次使用就检查iFind？**
任何工作流都从可复现的项目运行环境开始。预检只确认iFind已安全配置，不读取凭据、不下载行情，也不发送研究内容。

**缺少Python库会怎样？**
模块不会启动。Agent一次列出缺失项和版本差异，等待用户授权安装或改选环境。

**报告和参考报价由谁生成？**
每一份新的事实组合由Reporter整理并冻结，Designer按固定模板渲染。已保存的冻结输入可以直接补生成研究简报、完整研究报告或另一种格式；参考报价可组合多个明确选择的已保存运行结果，并按当前结构和参数整理合同条款。模型不得自己写HTML、PDF、SVG或临时程序。

**结果保存在哪里？**
数据写入项目`data/`，结果写入项目`result/`，运行元数据写入项目`.optionhelper/runtime/`；不会写到Skill安装目录。

## 8.安全边界

- 请求不得包含Token、密码、API Key或物理路径。
- 安装目录不得写入数据、结果、会话或凭据。
- iFind只使用Refresh Token换取短期访问凭据。
- 估值、Greeks、回测和报告必须来自正式模块链路，不得由模型补写。
- 新的正式交付事实组合必须经过Reporter→Designer；已保存的冻结Designer输入可直接重渲染为另一种交付形式。任何端口缺失都返回真实不可用状态。
