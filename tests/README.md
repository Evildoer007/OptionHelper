# OptionHelper 12.1迁移验收

本目录只保存开发仓库级验收，不替代各模块自身的单元测试。测试分成两类：

1. `test_financial_baselines.py`保护迁移前已经存在的金融证据。当前目录尚未迁移时也必须通过。
2. `test_architecture_12_1.py`检查蓝图12.1目标结构。目标尚未完成时必须明确失败，不允许跳过、`xfail`或用空文件伪造通过。

## 执行环境

在项目根目录使用已验证的Python解释器。若系统默认Python不符合依赖锁定要求，先设置`OPTIONHELPER_PYTHON`为其绝对路径：

```bash
"${OPTIONHELPER_PYTHON:-python3}" -m unittest tests.test_financial_baselines -v
"${OPTIONHELPER_PYTHON:-python3}" -m unittest tests.test_architecture_12_1 -v
"${OPTIONHELPER_PYTHON:-python3}" -m unittest discover -s tests -p 'test_*.py' -v
```

测试运行时应设置`PYTHONDONTWRITEBYTECODE=1`。涉及Numba时将缓存定向到系统临时目录，不能在源码目录生成新的缓存文件。

## 迁移门禁

### 门禁A：冻结现有金融证据

- 65个结构必须分别存在一份默认JSON和一份默认SVG。
- 每个JSON/SVG资产对的内容哈希必须与`baselines/payoffer_default_assets.sha256`一致。
- Pricer的9个现有Golden结构必须与`baselines/pricer_option_pricing_golden.json`逐数字一致。
- `rand_normal.npy`的SHA-256固定为`5762ac55cb922bd9dd2cb9ebf7f97857153519bf0405470299b8c4061910b8ed`。
- 迁移前运行`assets/pricer/option_pricing/tests/test_golden_results.py`；旧基座退役后必须运行`modules/pricer/tests/test_option_pricing_golden.py`，后者必须通过目标`price(PricingInput)`入口验证同一组PV、Greeks、扩展风险和路径哈希。
- 无法解释的变化必须阻断迁移。不能直接更新Golden或哈希掩盖变化。

### 门禁B：共享核心与导入

- OptionReg开发源只能是`references/optionreg.py`。
- `core/src/runtime/knowledger/registry_loader.py`是唯一加载入口。
- 模块源码不得直接导入`references.optionreg`、读取`optionreg.py`或导入旧`assets.*`业务包。
- 共享核心不得反向导入七个模块。
- 内部绝对导入只使用`runtime.*`和`modules.*`。

### 门禁C：七模块与五页面

- 七个模块均有指南、Manager、`service.py`、`config.py`、`models.py`和测试目录。
- 仅DataFetcher、Payoffer、Pricer、Backtester、Reporter拥有页面。
- 每个操作页面至少具有同名HTML、CSS和JavaScript。
- App前端不得保存五个模块页面副本。

### 门禁D：Skill、App、构建和Secret

- 根目录、`core/`、`products/app/`和`packaging/`满足12.1文件清单。
- 模块目录不得再有`SKILL.md`或模型厂商专用`agents/`。
- 源码、页面、配置和文档中不得出现高置信明文Token、密码、私钥或API Key。
- Secret只能以引用名进入配置；真实值不能写入仓库。

### 门禁E：旧assets退役

旧路径只能在以下条件全部满足后退役：

1. 目标模块测试和金融基线测试通过。
2. 所有活动Import已经切换到`runtime.*`或`modules.*`。
3. 五个页面的独立启动和OptDesk承载使用同一字节内容。
4. 现有结果路径和输入快照完成兼容验证。
5. Pricer目标Golden通过且旧基座已有只读历史快照。
6. 全项目活动源码不再引用旧路径。

最终12.1验收要求`assets/`不再保存活动Python业务代码。`assets/web-design/`只能作为冻结参考，不能被正式入口加载；旧Payoffer、Pricer、Backtester、Reporter、Designer、DataFetcher和共享合同实现必须移出活动`assets/`。

## 基线更新规则

基线文件不是自动生成的“最新结果”。只有产品库正式版本或已解释的Pricer数值变更完成独立审核后，才允许在同一变更中更新基线，并记录旧值、新值、原因、数值影响和审核人。目录搬迁本身不构成更新基线的理由。
