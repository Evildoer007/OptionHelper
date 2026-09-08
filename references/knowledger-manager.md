# Knowledger管理规范

## 1. 管理范围

Knowledger包含OptionList、OptionLib、OptionReg、默认Payoffer JSON与SVG及其发布来源清单。产品身份使用`product_id`和正整数`rule_revision`，发布归档通过KnowledgeSourceManifest与ProductSourceManifest记录来源。三份资料库必须映射一致，不得由普通运行、页面、模型或计算模块写回。

## 2. 修改产品的顺序

1. 在OptionList确认产品编号、中文名称、分类和资料状态。
2. 在OptionLib核对条款、唯一符号、路径、分段函数、默认示例与风险。
3. 将已确认规则写入OptionReg的`identity`、`terms`、`paths`。
4. 校验默认JSON只保存对应的视觉与路径展示规格，默认SVG是其确定性渲染产物。
5. 逐路径回归现金流、边界、端点、默认示例与图形；产品规则变更须维护对应`rule_revision`，通过后由统一发布流程签发来源清单。

## 3. OptionReg规则

顶层只允许`term_catalog`和`products`。每个产品只允许`identity`、`terms`、`paths`。`terms`保存默认合同条款、`monitor`、`pricing_methods`和必要`constraints`；不得保存市场数据、模块Config、页面状态、运行结果或默认SVG。

观察日程只使用交易日`daily`、`monthly_1st`至`monthly_31st`及`monthly_last`。未指定时默认交易日收盘价，月度默认`monthly_last`。所有产品当前为现金结算，路径现金流仅用`cash(t,amount)`表示。

## 4. 默认资产权限

默认JSON和SVG只在确认资料库结构错误时更新。先修产品资料和机器路径，再重建、回归并经人工确认发布。正常Payoffer、Pricer、Backtester、Reporter、Desk和大模型只能读取默认资产，本次运行只写`result/output_*`。

## 5. 验收

产品发布前必须核对：编号与名称、符号、字段、默认条款、监测、路径互斥完整性、分段边界、现金流、默认JSON、默认SVG、运行状态及引用关系。无法确认的条款不得由代码猜测。

## 6. 只读机器审计

正式入口为`tests.knowledger.audit`。普通审计仅在高置信结构错误时返回非零；严格发布门禁还会阻断蓝图已登记的迁移缺口与跨模块接口冲突。人工复核项不单独改变退出码。

```bash
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" -m tests.knowledger.audit audit --root . --format markdown
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" -m tests.knowledger.audit audit --root . --format markdown --strict-release --output /tmp/knowl-release-audit.md
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" -m unittest discover -s tests/knowledger -p 'test_*.py' -v
```

审计自动验证三库身份、名称、类别、状态、顺序、OptionLib五小节与标准表、OptionReg固定外形与重复字面量键、路径数量和可可靠解析的默认数值。`monitor`、`pricing_methods`、`constraints`及`condition/domain/pnl`的跨源经济等价性不由工具猜测，报告必须列为人工复核或未完全证明。

## 7. 候选产品流程

新增产品先在临时目录同时准备OptionList目录行、OptionLib完整正文和OptionReg运行条目，再执行只读候选门禁。新增或删除必须三源原子一致；既有产品的最小校正按三库职责归属修改，不要求机械改动无关资料，但报告必须列出实际影响源。

```bash
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" -m tests.knowledger.audit candidate --baseline-root . --candidate-root /absolute/path/to/candidate --format markdown --strict-release --output /tmp/knowl-candidate-audit.md
```

候选流程只计算差异与文件哈希，不自动写回三份资料库。weekly日程已迁移为正式支持的月度交易日日程。`derived_terms`是`terms`内唯一允许的派生声明：只能引用基础条款和已在前序声明的派生条款，解析期计算一次、不可由用户覆盖、不得与直接默认条款重名；它不形成第二份默认值或产品规则来源。

## 8. 开发候选与正式发布

以下命令仅用于完整开发仓库，不能在安装后的Skill目录运行。开发构建先冻结当前工作树，再生成Skill、App及安装物并完成验证；成功后替换`dist/`中的当前候选，不读取或创建正式`versions/v1.0.0`归档。开发候选使用`development`身份，不得当作正式签发版本。

```bash
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" packaging/build_current.py --version v1.0.0 --platform macos
```

正式发布由统一事务生成65个产品的三源片段、默认条款及冻结Payoff JSON与SVG。中间知识源技术快照只供完整性校验，不是独立运行入口。准备建立不可覆盖的正式归档时执行：

```bash
PYTHONPATH=core/src "${OPTIONHELPER_PYTHON:-python3}" packaging/release.py --version v1.0.0 --platform macos
```

Windows宿主使用同一入口并将`--platform`设为`windows`。发布流程全部验证通过后，才提交`versions/v1.0.0`与`dist/`。正式来源清单记录产品编号、规则修订号、六件套哈希、签发人和签发时间；哈希用于构建完整性校验，不作为产品身份或运行选择条件。已归档内容不可覆盖，另一平台补充发行须复用归档中的签发Skill。普通运行不建立`current`，也不写回产品资料库。
