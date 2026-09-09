# OptionHelper开发仓库

OptionHelper用于期权结构推荐、收益分析、估值、历史回测和研究报告交付。本仓库维护App与Skill的共享源码，并提供macOS和Windows构建入口。

从源码生成安装包，按第二节构建；已有安装包，直接看第四节安装与配置。

## 一、项目结构

App通过OptChat处理研究任务，通过OptDesk操作数据获取、收益结构、定价、回测和研究报告五个模块。Skill供Agent宿主安装使用，与App共享业务源码。

| 目录 | 职责 |
| --- | --- |
| `references/` | 产品定义、合同规则和研究资料 |
| `core/` | 共享合同、运行协议和模块调用 |
| `modules/` | DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter、Designer |
| `products/app/` | App前端、后端和平台原生壳 |
| `packaging/` | 构建入口、依赖锁、资源清单和构建验证器 |
| `assets/` | 图标等公共资源 |

公开仓库不发布本地测试、离线审计产物、下载数据和`node_modules`。构建时按锁文件恢复依赖；运行审计模块和构建验证器保留在源码中。

## 二、环境准备与构建

克隆仓库或下载完整ZIP后，在仓库根目录执行构建命令。两端安装物分别在对应系统上生成。以下环境要求针对开发者，成品App使用者无需安装Python或Node。

| 依赖 | 要求 |
| --- | --- |
| Python | 64位，建议3.12或3.13；当前锁定依赖最低要求3.12 |
| Node.js | 24.18.0，附带npm |
| macOS工具 | Apple Silicon、Xcode Command Line Tools |
| Windows工具 | Windows x64、.NET8 SDK、Windows PowerShell |
| 网络 | 可访问Python包源、npm和NuGet |

构建需要本机已安装的Node.js和npm，但不会把开发者的安装路径写死到产物中。Python运行依赖见[core/requirements.lock](core/requirements.lock)，打包依赖见[packaging/build-requirements.lock](packaging/build-requirements.lock)。不要自行替换锁定版本。

<details>
<summary>Python依赖清单与用途</summary>

以下为当前锁定版本，按后文命令一次安装，无需逐个安装。版本更新以锁文件为准。

| 运行依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| numpy | 2.4.6 | 数值与数组计算 |
| pandas | 3.0.5 | 表格和时间序列处理 |
| scipy | 1.18.0 | 科学计算与统计 |
| numba | 0.65.1 | 数值计算加速 |
| llvmlite | 0.47.0 | Numba编译支持 |
| requests | 2.34.2 | 数据API请求 |
| certifi | 2026.7.22 | HTTPS证书校验 |
| jsonschema | 4.26.0 | 输入与协议校验 |
| PyYAML | 6.0.3 | YAML配置读取 |
| reportlab | 5.0.0 | PDF生成 |
| Pillow | 12.3.0 | 图像处理 |
| pypdf | 6.16.0 | PDF读取与处理 |
| python-docx | 1.2.0 | Word文档生成与编辑 |
| beautifulsoup4 | 4.15.0 | HTML解析 |
| soupsieve | 2.9.1 | HTML元素选择 |
| lxml | 6.1.1 | XML与HTML处理 |
| openpyxl | 3.1.5 | Excel读写 |

| 打包依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| PyInstaller | 6.21.0 | 封装App后端与Python运行时 |
| ds-store | 1.3.3 | macOS安装镜像布局 |
| mac-alias | 2.2.3 | macOS文件别名元数据 |

打包依赖仅用于源码构建；Skill使用者只安装Skill内的运行依赖，成品App使用者无需手动安装这些库。

</details>

### 2.1 首次安装环境

1. **Python**：从[Python官网](https://www.python.org/downloads/)选择3.12或3.13的64位版本安装；已有兼容的Anaconda、Miniconda或虚拟环境可直接复用，无需另外安装。macOS使用原生arm64解释器，Windows使用x64解释器。不要使用系统自带的旧Python。
2. **Node.js**：从[Node.js官方版本目录](https://nodejs.org/download/release/)选择24.18.0。macOS选择对应安装包，Windows选择x64 MSI；保留npm和PATH选项，安装后重新打开终端。不要直接用其他版本代替，仓库校验对应版本的许可证。
3. **macOS构建工具**：在终端执行`xcode-select --install`，按提示安装[Xcode Command Line Tools](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/)。已安装完整Xcode并配置好命令行工具时可复用。
4. **Windows构建工具**：安装[.NET8 SDK的Windows x64版本](https://dotnet.microsoft.com/en-us/download/dotnet/8.0)，不是仅安装.NET Runtime。保留系统自带的Windows PowerShell。运行App还需安装[Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)，选择Evergreen Runtime；已安装则无需重复安装。
5. **源码目录**：完整解压仓库ZIP后再运行脚本，不要在压缩包内双击。目录须可写，建议放在本地磁盘，避免云盘未下载文件或受保护的系统目录。Git仅在克隆和后续拉取更新时需要，下载ZIP不要求安装Git。

两端均可用`node --version`和`npm --version`检查Node与npm。macOS用`xcrun --find swiftc`检查编译器；Windows用`dotnet --list-sdks`确认存在8.x SDK。以下依赖安装和构建命令都在**仓库根目录**执行。

### 2.2 macOS

先在终端进入解压后的仓库目录，将解释器路径替换为本机实际路径。若使用Conda环境，可激活目标环境后运行`python -c 'import sys; print(sys.executable)'`查询：

```bash
export OPTIONHELPER_PYTHON="/absolute/path/to/python"
"$OPTIONHELPER_PYTHON" --version
"$OPTIONHELPER_PYTHON" -m pip --version
"$OPTIONHELPER_PYTHON" -m pip install -r core/requirements.lock -r packaging/build-requirements.lock
./build-optionhelper-macos.command
```

也可直接双击`build-optionhelper-macos.command`选择已安装依赖的解释器。若下载的脚本没有执行权限，可在仓库目录运行`zsh build-optionhelper-macos.command`。

### 2.3 Windows

在仓库目录打开PowerShell，将路径替换为实际使用的Python解释器。普通Python安装可用`py -0p`列出路径；Conda环境可激活后用`python -c "import sys; print(sys.executable)"`查询：

```powershell
$env:OPTIONHELPER_PYTHON = 'C:\Python312\python.exe'
& $env:OPTIONHELPER_PYTHON --version
& $env:OPTIONHELPER_PYTHON -m pip --version
& $env:OPTIONHELPER_PYTHON -m pip install -r core\requirements.lock -r packaging\build-requirements.lock
.\build-optionhelper-windows.bat
```

也可双击`build-optionhelper-windows.bat`选择已安装依赖的解释器。运行生成的App需要Microsoft Edge WebView2 Runtime。

构建入口会记住本机选择，保存在被Git忽略的`.optionhelper/runtime/build-python-path`。后续如需更换解释器，重新设置`OPTIONHELPER_PYTHON`即可。依赖不匹配时会停止并显示安装命令，不会自动切换Python环境。

首次构建需要联网下载依赖。Python包由上述pip命令安装，Agent运行时的npm依赖由构建流程按锁文件恢复，Windows的NuGet依赖由.NET构建恢复，不需要在各子目录手动执行`npm install`。普通升级可复用原环境；锁文件变化时，先重新执行对应平台的pip安装命令，再构建。


## 三、构建产物

运行一次对应平台的构建入口，会同时生成Skill ZIP和本平台App安装物，无需分别打包。

| 平台 | 输出目录 | 目标文件 |
| --- | --- | --- |
| macOS | `dist/` | `option-helper.zip`、`OptionHelper-macOS-arm64.dmg` |
| Windows | `result/windows-candidate/` | `option-helper.zip`、`OptionHelper-windows-x86_64.zip` |

当前版本：Skill为`v0.2.0`，App为`v0.1.0.alpha`。安装包和ZIP带版本号，解压后的目录与应用名称保持不变。

上述入口生成本地候选。正式对外发行还需完成相应平台的签名或公证。构建失败时，按终端显示的失败阶段处理后重新运行。

## 四、安装与首次配置

### 4.1 安装App并登录

- macOS：打开DMG，将`OptionHelper.app`复制到应用程序目录后启动。
- Windows：完整解压ZIP，运行其中的`OptionHelper.exe`，不要单独移动EXE或删除旁边的资源目录。首次运行前确认已安装WebView2 Runtime。

成品App已封装Python和Agent运行时，报告图表也使用内置运行时完成语法校验，无需安装Python、Node.js或.NET SDK。

新数据目录首次启动时提供两个账号：

| 账号 | 密码 | 权限 |
| --- | --- | --- |
| `admin` | `66666666` | 可使用OptChat和OptDesk |
| `sales` | `88888888` | 可使用OptChat并配置自己的模型与数据API，不能使用OptDesk |

默认账号定义位于`products/app/backend/identity/password_store.py`。已有本机账号不会因重新构建而重置。

### 4.2 配置App

1. 打开账号菜单中的“设置中心”，在“模型配置”选择服务商或添加自定义Provider。
2. 填写自己的API地址、API密钥和模型名称，启用模型，点击“保存并测试”。
3. 需要市场数据时，在“数据接口”填写自己的iFind Refresh Token，点击“保存并测试”。

保存后自动测试连接，并区分对话连接状态与工具能力。接口须符合App支持的协议；测试失败会保留配置并显示原因，修正后再次保存即可。

凭据保存在当前设备，不随源码和安装包分发。其他使用者需要填写自己的API，不会继承构建者的密钥。

“智能体预设”默认使用单Agent，需要分工时可选择多Agent并配置角色模型。修改对后续推荐生效。

### 4.3 安装与配置Skill

Skill安装在支持项目级Skill的Agent宿主中，不是独立App，也不使用App的登录账号或模型设置。

1. 将Skill ZIP解压到宿主识别的**项目级Skill目录**，文件夹名保留`option-helper`，不带版本号。目录内应直接包含`SKILL.md`、`README.md`、`scripts/`、`references/`和`assets/`，不要多套一层。具体安装路径按宿主要求选择。
2. 在宿主中配置自己的模型服务、API密钥和模型名称，并确认宿主能够加载该Skill及调用本地工具。
3. 让宿主加载OptionHelper并检查运行环境。运行计算模块需要Python3.12及以上；选择要使用的解释器后，按提示安装Skill包内的`scripts/requirements.lock`，不需要安装App打包依赖。仅阅读资料不需要Python。
4. 需要行情时，按Skill引导配置自己的iFind Refresh Token。凭据保存在项目级`.optionhelper/memory.md`，属于本地明文配置，不要提交Git或写入Skill安装目录。不要把Token作为普通聊天内容发送。

可以先对宿主说：

> 使用OptionHelper，先检查运行环境和数据配置。缺少Python环境时让我选择，缺少依赖时先说明再安装。

模块运行时会在项目目录初始化`data/`、`result/`和`.optionhelper/runtime/`，结果不写回Skill安装目录。手动配置命令见[Skill安装说明](packaging/skill/SKILL_README.md)；安装包内也附有同一份README。

### 4.4 开始研究与生成报告

在App的OptChat或已加载Skill的宿主中，用自然语言说明标的、期限、市场观点、亏损上限和交付要求。例如：

> 为510300.SH推荐一个期权结构，期限3个月，温和看涨，最大亏损20%，接受本金波动。生成HTML格式card；需要定价或回测的缺失参数请先问我。

报告模板与导出格式分别选择：

| 模板 | 用途 |
| --- | --- |
| `card` | 单结构研究简报 |
| `report` | 单结构完整研究报告 |
| `quote` | 参考报价 |
| `multicard` | 多结构研究简报 |
| `multireport` | 多结构完整研究报告 |

支持HTML、PDF和Word；App提供报告预览、编辑和下载入口，Skill由宿主交付生成的文件。没有计算结果的内容属于研究草稿；定价、回测和报价结论必须有对应数据来源。

推荐型研究简报和研究报告默认按推荐、合同确认、收益分析、定价、历史回测、报告交付继续执行；参考报价至少完成必要定价。单Agent与多Agent共用该流程，明确要求研究草稿时保留未计算状态。

需要补充条件时，主聊天框显示问题、选项和自定义回答，仍通过同一个发送按钮提交。运行详情折叠后保留动态状态，用量分别显示Token输入、输出和总计。

生成的HTML均可通过文件卡上的编辑图标打开，修改正文、标题和版式后保存或导出。编辑报告不会重算上游结果；修改合同条款后需要重新运行相关计算。

## 五、版本迭代与发布

Skill与App的版本统一在[packaging/release_contract.py](packaging/release_contract.py)中维护，分别修改`SKILL_VERSION`和`APP_VERSION`。`RELEASE_VERSION`跟随Skill，用于知识库与Capability签发；App预发布标识会转换为各平台接受的内部版本格式。

发布新版本时：

1. 修改对应源码，并更新相应产品的版本配置。
2. 分别构建macOS和Windows候选。
3. 验证首次配置、重复运行、产品与参数切换、报告编辑及PDF/Word导出。
4. 完成平台签名和发行验收后归档。

安装包和Skill ZIP名称带版本号，安装后的App名及Skill目录名不带版本号。日常源码修改后也需要重新构建，已有安装物不会自动更新。

常规构建自动将新旧交付物保存在`versions/skill/<版本>/<内容哈希>/`和`versions/app/<版本>/<内容哈希>/`，同版本重新构建也不会覆盖历史文件。正式归档由`packaging/release.py`负责，保存到`versions/<版本号>/`；平台签名需另行配置，同版本已归档的平台产物不可覆盖。其他版本的历史归档无需删除或移动。

请勿直接修改`dist/`、`versions/`或`products/app/capability/`中的生成内容。金融规则修改应从`references/`及对应模块入手，页面修改应在App或模块页面源码中完成。

## 六、常见问题

| 问题 | 检查方法 |
| --- | --- |
| 提示Python依赖缺失 | 用构建时选择的同一个解释器安装两份锁定依赖，避免安装到另一个环境 |
| pip提示找不到匹配版本 | 先核对Python版本和64位架构，再检查包源是否同步了锁定版本；不要通过修改锁文件绕过 |
| 安装后仍提示找不到node或dotnet | 关闭并重新打开终端，检查PATH；Windows双击构建也会继承当前系统环境 |
| Node许可证不匹配 | 检查`node --version`是否为24.18.0；升级Node需同步更新对应许可证 |
| Windows缺少SDK | 检查`dotnet --list-sdks`是否包含8.x |
| Windows启动提示WebView2不可用 | 安装Microsoft Edge WebView2 Runtime后重试 |
| API连接失败 | 检查地址、密钥、模型名称及网络；再次保存后会自动测试 |
| 修改源码后App没有变化 | 重新构建并打开新产物，确认没有继续运行旧App |

维护者应分别记录源码、浏览器、原生App和安装包验收结果，不能互相替代。公开仓库不附带本地测试集；完整测试仅在保留测试文件的维护者工作区执行。
