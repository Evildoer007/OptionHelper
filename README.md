# OptionHelper开发仓库

OptionHelper是期权结构研究与交付系统。本仓库维护同一套权威源码，并由它生成可安装Skill、OptionHelper App和平台安装物。内部能力包括DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter和Designer。

Skill与App独立组装。Skill面向外部Agent Host，包含业务工作流、模块指南、环境检查和共享计算载荷，不包含五模块页面或App运行壳。App包含共享计算载荷、五模块页面、OptChat、OptDesk、Settings、Login、App后端、Agent Runtime和原生壳，不包含`SKILL.md`、Skill安装说明或模块指南。两者通过共享文件哈希、Catalog和协议标识确认同源，不互相整包复制。

Recommender的App入口与Skill入口共用同一状态机、严格角色Schema、运行凭证和确定性聚合器。结构推荐默认自动适配宿主能力：可证明独立子会话时执行多Agent，否则执行同一流程的单Agent路径。其他模块默认保持确定性执行，但顶层工作流架构不限制未来按正式策略接入多Agent。用户无需配置模型或选择Agent模式。

当前实现以[OptionHelper总设计蓝图](blueprint/OptionHelper总设计蓝图.html)为架构依据。金融口径、正式输入、结果引用和报告交付均由受控协议衔接，不允许页面、模型或临时脚本绕过模块自行计算或拼接报告。

## 文档分工

- 本文件：开发仓库的环境、目录、测试、构建和签发说明。
- [SKILL.md](SKILL.md)：安装后提供给当前对话Agent的行为与工作流约束。
- [packaging/skill/SKILL_README.md](packaging/skill/SKILL_README.md)：安装包内`README.md`的开发源，说明Skill安装、配置和使用方法。
- `modules/*/module-guide.md`：各模块的输入、输出、进度文案和失败边界。
- [tests/README.md](tests/README.md)：开发仓库级迁移基线和验收门禁。

修改开发说明时应先确认目标文件。根`README.md`不会进入Skill安装包；安装包内的`README.md`由`packaging/skill/SKILL_README.md`生成。

## 首次准备

### 1.选择Python解释器

开发构建首次运行时由开发者选择一个Python3.11及以上的解释器。设置后，macOS和Windows构建入口都会把绝对路径保存在当前仓库的本机忽略目录`.optionhelper/runtime/build-python-path`，后续从Finder双击或直接运行入口时继续使用同一解释器。也可以随时通过`OPTIONHELPER_PYTHON`明确更换：

```bash
export OPTIONHELPER_PYTHON=/absolute/path/to/python
"$OPTIONHELPER_PYTHON" --version
```

Windows（PowerShell）：

```powershell
$env:OPTIONHELPER_PYTHON = 'C:\Python311\python.exe'
& $env:OPTIONHELPER_PYTHON --version
```

如果不知道可用路径，可直接运行对应构建入口。入口只读取环境元数据并列出候选，不会擅自运行某个候选；选择后才用该解释器检查依赖并继续本次构建。选择会保存为本机设置。需要临时改用其他解释器时，再显式设置`OPTIONHELPER_PYTHON`。

```bash
./build-optionhelper-macos.command
```

Windows运行`build-optionhelper-windows.bat`时采用相同原则，并通过`packaging/list_python_environments.ps1`列出候选路径。

### 2.安装并核对依赖

运行依赖锁定在[core/requirements.lock](core/requirements.lock)，App封装依赖锁定在[packaging/build-requirements.lock](packaging/build-requirements.lock)。安装和检查命令如下：

```bash
"$OPTIONHELPER_PYTHON" -m pip install -r core/requirements.lock -r packaging/build-requirements.lock \
  --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
"$OPTIONHELPER_PYTHON" packaging/skill/environment_check.py --requirements core/requirements.lock --check-dependencies
"$OPTIONHELPER_PYTHON" packaging/skill/environment_check.py --requirements packaging/build-requirements.lock --check-dependencies
```

Windows（PowerShell）：

```powershell
& $env:OPTIONHELPER_PYTHON -m pip install -r core\requirements.lock -r packaging\build-requirements.lock `
  --index-url "https://pypi.tuna.tsinghua.edu.cn/simple"
& $env:OPTIONHELPER_PYTHON packaging\skill\environment_check.py --requirements core\requirements.lock --check-dependencies
& $env:OPTIONHELPER_PYTHON packaging\skill\environment_check.py --requirements packaging\build-requirements.lock --check-dependencies
```

依赖检查必须全部显示`ok`。缺依赖或版本不一致时先停止并显示诊断；只有用户明确确认后，才使用已选解释器执行安装，不会自动换环境或发布半成品。

## 目录与权威来源

- `references/`：Knowledger资料源，包括OptionList、OptionLib和唯一OptionReg。
- `core/`：共享合同、协议、运行端口、Store适配器、Tool入口和页面运行层。
- `modules/`：七个内部能力模块及各自测试。
- `products/app/`：App后端、前端和桌面壳层开发源，不复制金融内核。
- `packaging/`：从当前权威源码构建Skill、App和平台安装物。
- `assets/icons/`：正式公共图标。App壳层开发源位于`products/app/frontend/`，五个模块页面由Capability提供。
- `data/`、`result/`：开发仓库的本地数据和运行结果，不进入Skill发行包。已安装Skill使用宿主项目目录下的外部Store。
- `dist/`：可替换的当前候选交付目录。macOS候选构建成功后只保留`option-helper.zip`和DMG，可删除后重新构建，不承担版本追溯职责。
- `versions/`：不可覆盖的正式签发归档，是版本追溯的唯一权威来源。唯一正式目标为`versions/v1.0.0`。已签发的Catalog、Capability、Skill ZIP和每个平台安装物均不可覆盖；同一正式版本只允许补充尚未归档的平台安装物，且必须复用归档中的签发Skill。

不要手工修改`products/app/capability/`、`dist/`或`versions/`中的生成内容。修改权威开发源后应重新构建，并以Manifest、内容哈希和运行探针验证同一次构建。

## 开发测试

在仓库根目录运行。测试期间禁止向源码目录写入字节码缓存：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:core/src "$OPTIONHELPER_PYTHON" -m pytest -q tests core/tests modules products/app/tests packaging/skill/tests packaging/tests evals/tests
```

这条命令覆盖开发仓库的单元测试、协议测试和静态构建门禁。真实iFind、浏览器交互、回环HTTP、挂载DMG和平台签名属于独立验收，不能用单元测试通过替代。

Pricer数值变化必须先建立Golden基线。无法解释的PV、Greeks、风险曲线、路径哈希或精度变化应立即阻断，不得直接更新基线掩盖差异。

## 构建当前候选

当前候选用于本机开发验收，不创建或读取正式`versions/v1.0.0`归档。

### macOS

```bash
export OPTIONHELPER_PYTHON=/absolute/path/to/python
./build-optionhelper-macos.command
```

构建链依次检查运行依赖和PyInstaller，再执行七个阶段：临时Skill、Skill与App契约验证、Skill ZIP、App与DMG、内容绑定、目录检查和原子发布。成功后输出：

```text
dist/option-helper.zip
dist/OptionHelper-v1.0.0-macOS-arm64.dmg
```

任何阶段失败时，脚本返回非零状态，并保留上一次完整`dist`，不会发布本次半成品。DMG为本地候选；未完成Developer ID签名和Apple公证前，不得称为正式外部分发包。

### Windows

在Windows实机中运行：

```bat
set OPTIONHELPER_PYTHON=C:\absolute\path\to\python.exe
build-optionhelper-windows.bat
```

Windows候选写入`result/windows-candidate/`，不会替换macOS的`dist/`或正式版本归档。

## 正式签发v1.0.0

正式签发会先在临时事务树中完成Knowledger快照、Skill、Capability、平台App、安装物以及ZIP、Manifest、签名、挂载和运行验收；全部通过后才提交唯一`versions/v1.0.0`和`dist/`。只有准备建立不可覆盖版本时才运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:core/src "$OPTIONHELPER_PYTHON" packaging/release.py --version v1.0.0 --platform macos
```

首次签发前`versions/`不得含旧版归档，且`versions/v1.0.0`必须不存在。首次签发后，可以在另一平台用同一命令补充尚未归档的平台安装物；补充流程只从归档的签发Skill构建，不读取可变开发源码，也不会改写已有文件。重复签发已归档的平台仍会拒绝。签发成功后：

- `versions/v1.0.0/`保存不可覆盖的正式追溯材料、Manifest、哈希、Skill ZIP和安装物。
- `dist/`保存最近一次签发动作对应的当前交付副本。
- 后续普通开发构建只更新`dist/`，不修改`versions/v1.0.0/`。

若存在旧版归档，先在获得明确授权后原样迁移至项目外可恢复目录；签发器不会删除、覆盖或把旧归档解释为当前版本历史。

## 模块链路

1. DataFetcher获取并冻结受控数据引用。
2. Recommender基于用户观点、期限和约束选择结构，并展示合同条款供用户确认或调整。
3. Payoffer生成收益结构，Pricer完成估值与风险计算，Backtester完成历史表现分析。
4. Reporter只整理已验证的模块结果，不重新计算。
5. Designer只负责固定模板的HTML或PDF渲染，不改写事实。

单独调用模块时返回结构化结果；需要报告时，必须由Reporter接收正式结果，再由Designer生成，不允许模型自行编写临时Python或HTML替代。

## 公共投影口径

面向用户的估值、回测和报告只展示百分比。`S0Raw`仅用于真实价格与标准化合同换算，不作为面向用户字段；内部现金流、点数、金额和名义本金不得投影到页面、正式Tool、CSV或报告。

## Pricer数值内核

用户提供的既有定价代码已吸收到`modules/pricer/src/engines/pricing_core/`，作为Pricer唯一内部数值内核。后续允许在同一Pricer内重构接口、扩展结构和提升数值实现，但不得脱离既有数值证据从零重写，也不得形成两套正式定价引擎。

## 报告交付

正式交付包括研究简报、完整研究报告和参考报价。横向对比沿用相同公开类型，分别生成多结构研究简报或多结构完整研究报告，不另造输出类型。所有用户交付均由Reporter冻结事实，再由Designer生成HTML或PDF。

完整研究报告默认采用连续A4正文；宽屏HTML提供左侧目录，PDF不显示目录。单结构报告默认依次呈现核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。研究简报默认呈现结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险，不含损益图。多结构研究简报保持无图，多结构完整研究报告展示可比的收益、估值和回测事实。参考报价按标的整理多个已验证合同版本的条款，不展示Greeks、估值、回测或图表。用户明确要求补充说明或多结构比较时可以调整展示结构，但金融数值、合同条款、指标、表格、公式和图表必须来自Reporter冻结事实，不能由Designer或Presentation Patch改写。

报告中的数字、单位、公式、图表和文字必须来自已验证的模块结果。Reporter与Designer不可补造缺失数据，也不可把内部JSON、字段名、运行引用或文件路径直接展示给用户。

## 当前边界

未接入或未验证的服务器登录、外部数据源、平台签名、公证和自动更新必须明确返回不可用，不得伪造成功。Secret只能由受控运行环境保存和注入，不能写入源码、设置文件、日志、结果、报告或构建产物。
