# OptionHelper开发仓库

本仓库同时维护可安装的OptionHelper Skill源码、七个内部能力模块和OptionHelper App开发源。当前实施依据为[OptionHelper总设计蓝图](blueprint/OptionHelper总设计蓝图.html)第12.1节。

## 目录

- `SKILL.md`：唯一标准Skill入口。
- `references/`：Knowledger资料源，包括OptionList、OptionLib和唯一OptionReg。
- `core/`：共享合同、协议、端口、适配器、Tool入口和页面Host。
- `modules/`：DataFetcher、Recommender、Payoffer、Pricer、Backtester、Reporter、Designer七个内部能力模块。
- `products/app/`：App壳层开发源，不复制金融内核或模块页面。
- `packaging/`：从同一开发源构建Skill候选包及App内置Capability。
- `assets/icons/`：公共图标；`assets/web-design/`仅为冻结的旧前端参考。
- `data/`、`result/`：开发仓库本地数据与运行结果，不进入Skill发行包；已安装Skill运行时使用安装目录之外的宿主项目Store。
- `versions/`：不可覆盖的正式签发归档，是版本追溯的唯一权威来源。
- `dist/`：可替换的当前候选交付目录，不是版本权威；交付状态必须以Capability Manifest和`versions/`归档为准。

## Pricer数值内核

用户提供的既有定价代码已吸收到`modules/pricer/src/engines/pricing_core/`，作为Pricer唯一内部数值内核。后续允许在同一Pricer内重构接口、扩展结构和提升数值实现，但不得脱离既有数值证据从零重写，也不得形成两套正式定价引擎。

## 当前边界

12.1只建立清晰、可测试、可构建的开发仓库基础。尚未接入的服务器登录、Wind、真实模型Provider、App WebView安装物和自动更新必须明确返回不可用，不得伪造成功。

## 报告交付

完整研究报告固定为连续A4、不提供目录版，章节和顺序为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。研究简报Card规则不变。

测试和构建命令见`tests/README.md`。
