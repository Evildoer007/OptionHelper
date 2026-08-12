# OptionHelper开发评测

`evals/`是开发仓库专用的Skill回归评测，不是OptionHelper标准Skill或App Capability的一部分。评测只调用公开Tool边界、共享Knowledger只读入口和App授权策略，不连接真实模型、iFinD、Wind或任何付费数据源。

## 运行

```bash
"${OPTIONHELPER_PYTHON:-python3}" evals/runner.py
"${OPTIONHELPER_PYTHON:-python3}" evals/runner.py --json
"${OPTIONHELPER_PYTHON:-python3}" evals/runner.py --case compute.pricer.formal-run
```

运行器为每次执行创建临时DataStore和ResultStore，并只读使用`fixtures/market_history.csv`作为受控行情输入，结束后清理临时运行结果。案例使用固定任务ID、运行ID和随机种子；断言只比较稳定字段，不比较UUID、时间戳或临时路径。

## 案例格式

案例遵循`case.schema.json`：

- `id`：全局唯一、稳定的案例ID。
- `area`：能力域。
- `target`：公开Tool、受控协议探针或跨模块工作流。允许值由`case.schema.json`和Runner共同冻结。
- `input`：结构化输入。`$MARKET_HISTORY`、`$DATA_FIXTURE`和`$DESIGNER_PAYLOAD`由运行器解析为临时或受控夹具。
- `expect`：支持`equals`、`contains`、`not_contains`、`length`和`exists`断言，字段路径使用点号。

运行器把未捕获异常标准化到`exception.type`和`exception.message`，因此错误边界同样可以作为基准案例。

## 覆盖范围

- Knowledger产品定位、65产品登记和未知产品。
- DataFetcher离线状态、本地CSV、非法action、CallerContext权限，以及App入口的明文凭据、伪造上下文和Host SecretRef边界。
- Recommender无外部端口时的诚实Unavailable边界，同CatalogVersion的OptionList身份、OptionLib章节、OptionReg entry_status三库证据，以及三库缺失或冲突的拒绝路径。
- Payoffer目录、收益结构预览、错误边界，以及经Core、HostContext和正式ResultStore的公开运行协议。
- Pricer与Backtester受控本地行情运行、ModuleRunRef v1.2、外置artifact manifest哈希锚、缺锚与篡改锚拒绝、跨租户DataAssetRef拒绝、MC低精度、ObservedState、非触发路径、非交易日和多标的缺失边界。
- Reporter依赖注入、受控结果选择协议、未知或跨租户来源、结果哈希异常、Card/Report及HTML/PDF契约。
- Designer离线HTML、Card/Report和PDF不可用边界。
- Sales/Admin授权差异。
- App Agent的工具授权、敏感参数、重复调用、失败不虚构和金融数字事实引用边界。
- OptDesk ModuleHostContext的task_id传递和Bridge单层包装。
- ModuleHost v2完整签名范围、Caller与Host权限交集，以及正式App Host约束。
- Knowledger到Payoffer、三模块目录一致性和交付契约组合。

本评测不证明真实模型质量、实时行情正确性、付费Provider连通性、浏览器视觉效果或正式PDF引擎可用性。这些边界必须在相应环境中单独验收。

## 发行边界

`packaging/skill/package-source-map.json`是标准Skill白名单，禁止加入`evals/`。构建器显式拒绝该开发目录；架构测试同时检查Source Map、正式Skill目录和App Capability均不含`evals/`。评测结果也不得写入正式发行产物。
