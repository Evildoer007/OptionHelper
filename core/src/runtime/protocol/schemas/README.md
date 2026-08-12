# 机器Schema唯一目录

本目录是OptionHelper机器Schema的唯一开发源。模块不得保存第二份Schema。

## v1.2迁移

- 新`ModuleRunRef`必须同时携带`expected_semantic_result_hash`和`expected_artifact_manifest_hash`；后者锚定已提交`artifacts/artifact_manifest.json`的真实字节。
- 正式Host、App和Reporter不接受缺少清单锚的引用。既有五字段引用只能通过`LocalResultStore.attest_legacy_module_run_ref`显式再锚定，不得作为新运行写入。
- 升级会改变Capability协议内容；发行Capability及其Manifest必须重新构建和验证，不能沿用v1.1发布声明。

## v1.1迁移

- `ModuleRunRef`必须携带`module=payoffer|pricer|backtester`；解析不再跨模块猜测目录。
- `ResolvedContract`新增`registry_snapshot_hash`、`product_snapshot_hash`和`product_paths_hash`，正式合同须由Core `resolve_contract`生成并可经`verify_product_snapshot_binding`复核。
- 旧四字段合同仅保留`to_dict()`读取兼容，不是可持久化的正式v1.1合同。
