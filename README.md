# OptionHelper开发仓库

OptionHelper是期权结构研究与交付系统。本仓库维护同一套权威源码，并由它生成可安装Skill、OptionHelper App和平台安装物。内部能力包括DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter和Designer。

当前实现以[OptionHelper总设计蓝图](blueprint/OptionHelper总设计蓝图.html)为架构依据。金融口径、正式输入、结果引用和报告交付均由受控协议衔接，不允许页面、模型或临时脚本绕过模块自行计算或拼接报告。

## 文档分工

- 本文件：开发仓库的环境、目录、测试、构建和签发说明。
- [SKILL.md](SKILL.md)：安装后提供给Agent Host的行为与工作流约束。
- [packaging/skill/SKILL_README.md](packaging/skill/SKILL_README.md)：安装包内`README.md`的开发源，说明Skill安装、配置和使用方法。
- `modules/*/module-guide.md`：各模块的输入、输出、进度文案和失败边界。
- [tests/README.md](tests/README.md)：开发仓库级迁移基线和验收门禁。

修改开发说明时应先确认目标文件。根`README.md`不会进入Skill安装包；安装包内的`README.md`由`packaging/skill/SKILL_README.md`生成。

## 首次准备

### 1.选择Python解释器

开发构建首次运行时由开发者选择一个Python3.11及以上的解释器。设置后，macOS构建入口会把绝对路径保存在当前仓库的本机忽略目录`.optionhelper/runtime/build-python-path`，后续从Finder双击时继续使用同一解释器。也可以随时通过`OPTIONHELPER_PYTHON`明确更换：

```bash
export OPTIONHELPER_PYTHON=/absolute/path/to/python
"$OPTIONHELPER_PYTHON" --version
```

如果不知道可用路径，可直接运行一次macOS构建入口。首次运行只枚举候选解释器并退出，不会擅自运行某个候选。选定后在终端执行一次`export OPTIONHELPER_PYTHON=...`并重新运行，构建入口会保存这次本机选择：

```bash
./build-optionhelper-macos.command
```

Windows运行`build-optionhelper-windows.bat`时采用相同原则，并通过`packaging/list_python_environments.ps1`列出候选路径。

### 2.安装并核对依赖

运行依赖锁定在[core/requirements.lock](core/requirements.lock)，App封装依赖锁定在[packaging/build-requirements.lock](packaging/build-requirements.lock)。安装和检查命令如下：

```bash
"$OPTIONHELPER_PYTHON" -m pip install -r core/requirements.lock -r packaging/build-requirements.lock
"$OPTIONHELPER_PYTHON" packaging/skill/environment_check.py --requirements core/requirements.lock --check-dependencies
"$OPTIONHELPER_PYTHON" packaging/skill/environment_check.py --requirements packaging/build-requirements.lock --check-dependencies
```

依赖检查必须全部显示`ok`。版本不一致时构建会停止，不会发布半成品。

## 目录与权威来源

- `references/`：Knowledger资料源，包括OptionList、OptionLib和唯一OptionReg。
- `core/`：共享合同、协议、Host端口、Store适配器、Tool入口和页面Host。
- `modules/`：七个内部能力模块及各自测试。
- `products/app/`：App后端、前端和桌面壳层开发源，不复制金融内核。
- `packaging/`：从当前权威源码构建Skill、App和平台安装物。
- `assets/icons/`：正式公共图标。App壳层开发源位于`products/app/frontend/`，五个模块页面由Capability提供。
- `data/`、`result/`：开发仓库的本地数据和运行结果，不进入Skill发行包。已安装Skill使用宿主项目目录下的外部Store。
- `dist/`：可替换的当前候选交付目录。macOS候选构建成功后只保留`option-helper.zip`和DMG，可删除后重新构建，不承担版本追溯职责。
- `versions/`：不可覆盖的正式签发归档，是版本追溯的唯一权威来源。唯一正式目标为`versions/v1.0.0`；已存在时签发器会拒绝覆盖。

不要手工修改`products/app/capability/`、`dist/`或`versions/`中的生成内容。修改权威开发源后应重新构建，并以Manifest、内容哈希和运行探针验证同一次构建。

## 开发测试

在仓库根目录运行。测试期间禁止向源码目录写入字节码缓存：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:core/src "$OPTIONHELPER_PYTHON" -m pytest -q tests core/tests modules products/app/tests packaging/skill/tests packaging/tests evals/tests
```

这条命令覆盖开发仓库的单元测试、协议测试和静态构建门禁。真实模型、真实iFind、浏览器交互、回环HTTP、挂载DMG和平台签名属于独立验收，不能用单元测试通过替代。

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

签发前`versions/`不得含旧版归档，且`versions/v1.0.0`必须不存在。签发成功后：

- `versions/v1.0.0/`保存不可覆盖的正式追溯材料、Manifest、哈希、Skill ZIP和安装物。
- `dist/`保存与本次签发对应的当前交付副本。
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

完整研究报告固定为连续A4正文，HTML宽屏提供左侧章节目录，PDF按同一正文顺序分页；章节和顺序为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。研究简报Card固定呈现结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险，不含损益图。

报告中的数字、单位、公式、图表和文字必须来自已验证的模块结果。Reporter与Designer不可补造缺失数据，也不可把内部JSON、字段名、运行引用或文件路径直接展示给用户。

## 当前边界

未接入或未验证的服务器登录、外部数据源、模型Provider、平台签名、公证和自动更新必须明确返回不可用，不得伪造成功。Secret只能由Host受控保存和注入，不能写入源码、设置文件、日志、结果、报告或构建产物。
