# Reporter请求契约

Reporter只接收显式引用，不接收物理结果目录，也不会搜索最近一次运行。计算模块使用Core的`ModuleRunRef`与宿主注入的`ResultStorePort`解析；Designer由宿主注入公开`ModulePort`。

```json
{
  "schema": "optionhelper.report-request",
  "tenant_id": "tenant_001",
  "task_id": "task_20260804",
  "report_run_id": "report_001",
  "analysis_case_id": "case_001",
  "subject_type": "contract",
  "subject_ref": {
    "delivery_mode": "single",
    "candidate_ids": ["candidate_01"],
    "selected_modules": ["recommender", "payoff", "pricing", "backtest"]
  },
  "source_refs": {
    "product_version_refs": {
      "candidate_01": {"product_id": "7.1", "product_version": "<product-release>", "content_hash": "<sha256>"}
    },
    "catalog_version_ref": {"catalog_version": "<catalog-release>", "content_hash": "<sha256>"},
    "evidence_refs": {
      "recommendation_set": {
        "source_id": "recommender/run-001",
        "run_id": "run-001",
        "expected_semantic_result_hash": "<sha256>",
        "payload": {"schema": "optionhelper.recommendation-set"}
      }
    },
    "module_run_refs": {
      "candidate_01": {
        "payoff": {"module": "payoffer", "tenant_id": "tenant_001", "task_id": "task_20260804", "run_id": "payoff-001", "expected_semantic_result_hash": "<sha256>", "expected_artifact_manifest_hash": "<sha256>"},
        "pricing": {"module": "pricer", "tenant_id": "tenant_001", "task_id": "task_20260804", "run_id": "pricing-001", "expected_semantic_result_hash": "<sha256>", "expected_artifact_manifest_hash": "<sha256>"},
        "backtest": {"module": "backtester", "tenant_id": "tenant_001", "task_id": "task_20260804", "run_id": "backtest-001", "expected_semantic_result_hash": "<sha256>", "expected_artifact_manifest_hash": "<sha256>"}
      }
    }
  },
  "output_type": "report",
  "format": "html",
  "audience": "professional",
  "metadata": {"title": "结构化产品研究报告", "as_of_date": "2026-08-04"}
}
```

`selected_modules`必须显式给出任意非空子集。未选择的模块在冻结单位中记为`not_requested`，已选择但没有运行引用的模块记为`not_run`。`single`对应一个候选；`combined`、`batch`和`comparison`至少对应两个候选。所有交付均以正式推荐候选、合同快照和受控运行引用为基础。

## 验证

- 每个计算运行必须显式携带Core正式模块名，并匹配`tenant_id`、`task_id`、`analysis_case_id`、`candidate_id`、模块名和`ModuleRunRef`语义哈希。
- Reporter读取`ResolvedContract.to_protocol_dict()`完整快照，逐项比对产品编号、中文名称、产品版本、合同指纹、分析基础、标的顺序、币种和价格口径。
- Core LocalResultStore的`artifacts/artifact_manifest.json`及`commit_marker.json`必须通过校验；清单列出的每个文件都校验存在性、非符号链接与SHA-256。
- 未满足当前正式协议字段的结果、合同冲突、哈希冲突或清单冲突均会拒绝生成；不会把不完整记录作为部分可信事实展示。

`output_type`仅决定Card或Report的内容密度，`format`仅决定HTML或PDF。Report固定为连续正文，HTML宽屏提供左侧章节目录，PDF不显示导航；Card固定为无图简报。调用方不传递版式参数。

Card是简洁交付，固定呈现结构推荐、推荐理由、关键合同条款、估值摘要、回测摘要、主要风险；不展示收益结构章节、损益图或其他图表。

Report的公开章节是不可覆盖的交付契约，必须按以下顺序、逐字输出：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。请求中的`metadata`、上游模块结果或调用方不得新增、替换、删除或重排这些章节；未运行模块只能在自己的固定章节说明覆盖缺口。
