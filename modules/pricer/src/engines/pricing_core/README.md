# 统一衍生品定价框架

本目录是Pricer唯一的内部数值引擎，按“产品族+具体结构+定价方法”统一期权定价。原六族九结构的Golden与固定随机源作为迁移基线保留在测试与审计资料中。

## 一、当前状态

- 原6个产品族、9个结构保持Golden回归；新增声明式离散路径MC结构，构成7个产品族、10个结构。
- 65个`entry_status=True`的OptionReg产品均由同一离散路径、观察日、事件与现金流解释器执行；简单结构仍优先使用已验证闭式解。
- 支持PV、Delta、Gamma、Theta、Vega、Rho、Volga和Vanna。
- 支持Autocall公平coupon反解和Path Accumulator公平strike反解。
- 支持iFinD HTTP收盘行情和离线市场快照。
- 只使用CPU，不依赖CUDA。
- macOS已运行验证；Windows只完成代码级兼容设计，尚未实机验证。
- 快速回归固定`paths=10、seed=20240101、threads=1`，不代表正式报价精度。

## 二、目录

```text
pricing_core/
├── main.py                 唯一公共入口
├── engine/                 STANDARD定价、Greek、反解和市场数据
├── data/
│   ├── rand_normal.npy     固定随机矩阵
│   └── market_snapshots/   脱敏收盘市场快照
├── examples/quick_start.py 最小运行示例
├── tests/                  Golden、数值与独立运行测试
├── docs/adr/               架构决策
└── baseline_manifest.json  固定随机源与Golden迁移基线
```

## 三、产品目录

|产品族|结构|方法|
|---|---|---|
|VANILLA|EUROPEAN_VANILLA|BLACK_SCHOLES|
|DIGITAL|BINARY|BINARY_ANALYTIC|
|BARRIER|BARRIER|REINER_RUBINSTEIN|
|AIRBAG|AIRBAG|STATIC_REPLICATION|
|ACCUMULATOR|STATIC_ACCUMULATOR|STATIC_REPLICATION|
|ACCUMULATOR|PATH_ACCUMULATOR|MONTE_CARLO_CPU|
|AUTOCALL|SNOWBALL|MONTE_CARLO_CPU|
|AUTOCALL|PHOENIX|MONTE_CARLO_CPU|
|AUTOCALL|TRIGGER|MONTE_CARLO_CPU|
|OPTIONREG|声明式离散路径MC|MONTE_CARLO_CPU|

可直接查询目录：

```python
from main import list_families, list_structures, describe_structure

print(list_families())
print(list_structures("AUTOCALL"))
print(describe_structure("AUTOCALL", "SNOWBALL"))
```

## 四、安装与测试

使用已通过项目依赖检查的Python解释器。若系统默认解释器不匹配，可设置`OPTIONHELPER_PYTHON`为绝对路径：

```bash
cd /Users/haoranxu/Desktop/OptionHelper/modules/pricer/src/modules/pricer/engines/pricing_core
"${OPTIONHELPER_PYTHON:-python3}" run_tests.py
```

也可直接运行：

```bash
NUMBA_NUM_THREADS=4 "${OPTIONHELPER_PYTHON:-python3}" -m unittest discover -s tests -p 'test_*.py' -v
```

依赖版本记录在`requirements.txt`。Windows应使用相同Python和依赖版本，再执行：

```text
python run_tests.py
```

## 五、最小定价示例

```python
from main import price_option

run = price_option(
    "VANILLA",
    "EUROPEAN_VANILLA",
    {
        "contract": {
            "strike": 100.0,
            "maturity_years": 0.25,
            "call_put": "CALL",
            "basis": {
                "notional": 1_000_000.0,
                "currency": "CNY",
            },
        },
        "market": {
            "as_of": "2026-08-06",
            "spot": 100.0,
            "volatility": 0.20,
            "risk_free_rate": 0.02,
            "dividend_yield": 0.0,
            "source": "offline_example",
        },
    },
    "BLACK_SCHOLES",
    output="TERMINAL_AND_JSON",
)

print(run.result.pv_points_100)
print(run.result.greeks["Delta"])
```

默认完整打印请求参数、有效参数、市场、存续状态、模拟配置、PV、Greek、警告、诊断和引擎版本，同时写入：

```text
result/pricing/{run_id}/pricing_run.json
```

## 六、PV口径

- `pv_points_100`：每100名义本金的价值点数。
- `pv_percent`：占名义本金的比例，等于`pv_points_100/100`。
- `pv_amount`：货币金额，等于`pv_percent×notional`。

例如`pv_points_100=5`、名义本金100万元，则`pv_percent=5%`，`pv_amount=5万元`。

## 七、Greek口径

核心Greek固定为：

- Delta：每1单位现价变化的PV变化。
- Gamma：每1单位现价平方变化的二阶PV变化。
- Theta：估值日向前推进后的每自然日PV变化。
- Vega：波动率绝对变化1个百分点的PV变化。
- Rho：无风险利率绝对变化1个百分点的PV变化。

扩展风险：

- Volga：波动率变化1个百分点平方对应的二阶PV变化。
- Vanna：现价变化1单位与波动率变化1个百分点的交叉敏感度。

每个Greek均附带：

- `value`和明确单位。
- 扰动幅度和差分方式。
- `pv_points_100、pv_percent、pv_amount`三种结果口径。
- Theta的自然日和交易日前移信息。

Vanilla核心Greek使用解析公式。其他结构使用统一中央差分、共同随机数和固定扰动。Monte Carlo路径较少时，Greek只用于回归，不应用于正式风险报价。

显式`carry`优先于`risk_free_rate-dividend_yield`。Rho在显式carry或显式远期曲线存在时保持该曲线不变，只扰动贴现利率；否则保持股息率不变并重建carry。

## 八、统一反解

```python
from main import solve_option

solution = solve_option(
    "AUTOCALL",
    "SNOWBALL",
    parameters,
    "MONTE_CARLO_CPU",
    {
        "variable": "coupon",
        "target_pv_points_100": 0.0,
        "lower_bound": -100.0,
        "upper_bound": 100.0,
        "solver_absolute_tolerance": 1e-8,
        "target_pv_absolute_tolerance": 1e-3,
        "maximum_iterations": 100,
    },
    output="TERMINAL_AND_JSON",
)

print(solution.result.value)
print(solution.result.contract_patch)
```

当前支持：

|结构|反解变量|
|---|---|
|Snowball|coupon|
|Phoenix|coupon|
|Trigger|coupon|
|Path Accumulator|strike|

`contract_patch`可直接更新原合约后重新定价。反解结果必须同时满足求根收敛和目标PV残差容差。

## 九、iFinD收盘市场快照

HTTP接口只从临时环境变量读取鉴权信息，项目文件不保存账号、密码或Token：

```bash
export IFIND_REFRESH_TOKEN='本机Token'
```

```python
from main import fetch_ifind_market_snapshot

snapshot = fetch_ifind_market_snapshot(
    "510300.SH",
    "2026-08-06",
    asset_type="ETF",
    volatility_window=20,
    risk_free_rate=0.02,
    dividend_yield=0.0,
    output_path="data/market_snapshots/510300.SH_2026-08-06_HV20.json",
)

market = snapshot.to_market_parameters()
```

数据口径：

- 现价使用估值日或估值日前最近交易日的不复权收盘价。
- 股票和ETF的历史波动率使用前复权收盘价。
- 指数不做复权。
- HV10、HV20、HV60、HV122使用对数收益率样本标准差并按244个交易日年化。
- 快照保存字段、口径、实际交易日、观察数量和数据哈希，不保存完整历史序列。
- 当前利率、股息率和carry由调用者明确提供，尚未从iFinD自动拉取。

项目内包含两份已实盘验证的脱敏样例：

- `data/market_snapshots/000300.SH_2026-08-06_HV20.json`
- `data/market_snapshots/510300.SH_2026-08-06_HV20.json`

定价和测试优先读取离线快照，避免网络状态改变回归结果。

## 十、显式日程与参数规则

- Autocall和Path Accumulator必须提供严格递增的显式观察日。
- 显式日程是事实源，不再同时接收不生效的月份和频率字段。
- `forward_curve_weight`表示现价与显式远期曲线偏离的使用权重，范围为0到1。
- Static和Path Accumulator当前只支持`WHOLE_CONTRACT`数量口径，其他口径直接拒绝。
- 已敲出的Autocall需要明确待结算现金流，不能按存续合约重新模拟。
- 不认识的字段、错误产品族、错误方法、空日程、乱序日程和越界路径均直接报错。

## 十一、STANDARD与history

生产代码中不存在Legacy Adapter、Legacy模式、`compare_option()`、`legacy_unit`或`legacy_raw`。

新旧数值对照只在独立审计进程中运行：

```bash
"${OPTIONHELPER_PYTHON:-python3}" \
  history/audit/compare_standard_snapshot.py
```

当前9个代表结构与冻结0022 STANDARD快照的PV绝对差均为0；Snowball、Phoenix、Trigger和Path Accumulator的逐路径PV哈希也完全一致。该结论只覆盖固定输入和固定随机矩阵，不等于所有参数空间的数学证明。

## 十二、已知边界

- 10条路径仅用于快速回归。
- 当前固定随机矩阵最多支持2000条路径和800步。
- Forward Delta尚未实现；Duration对当前期权结构标记为不适用。
- Windows尚未实机验收，不能声明正式通过。
- iFinD实盘已在macOS验证指数和ETF收盘快照；Token过期、账户权限和网络状态仍由运行环境决定。
- Legacy只保留在history中用于审计，不应被OptionHelper正式运行时导入。
