# 机器Schema唯一目录

本目录是OptionHelper机器Schema的唯一开发源。模块不得保存第二份Schema。

## 当前内部协议

- `ModuleRunRef`必须同时携带`module`、`expected_semantic_result_hash`和`expected_artifact_manifest_hash`；后者锚定已提交`artifacts/artifact_manifest.json`的真实字节。
- `ResolvedContract`必须含`registry_snapshot_hash`、`product_snapshot_hash`和`product_paths_hash`，且由Core `resolve_contract`生成并经`verify_product_snapshot_binding`复核。
- Host、App、Reporter和Capability仅接受此完整形状；缺字段的旧对象不得作为新运行或重新签发引用。
