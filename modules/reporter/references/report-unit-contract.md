# ReportUnit契约

`ContractReportUnit`是一份已解析合同的唯一冻结事实来源。它保存候选、完整`ResolvedContract.to_protocol_dict()`快照、模块状态、显式运行引用、来源摘要、限制、已验证产物引用和`semantic_fact_hash`。它不保存`result_dir`、临时路径或Designer样式。

一个合同只对应一个`ContractReportUnit`。组合、批量和横向比较使用`ReportBundle`保存多个独立单位，禁止把不同合同的Payoff、PV或回测统计混成一个模块结果。

```text
ReportRequest + ResultStorePort
  -> ContractReportUnit
  -> DesignerInput（冻结内容投影）
  -> Designer ModulePort
  -> HTML或PDF + run_manifest.json
```

`semantic_fact_hash`只覆盖冻结事实，不覆盖Card/Report、HTML/PDF、受众或报告运行编号。因此同一合同事实可以派生Card、Report、HTML和PDF而不改变事实哈希。

`run_manifest.json`记录请求哈希、ReportUnit文件和事实哈希、Designer输入与设计简报哈希、所有ModuleRunRef、产物路径/哈希、Designer设计系统版本、实际交付文件哈希和真实运行状态。未运行、失败、不支持和部分完成均保留其原因，不能用默认图、示例数值或空白结论替代；不满足正式协议的运行结果会被拒绝，不能作为部分可信事实保存。
