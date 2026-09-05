# 机器Schema唯一目录

本目录是OptionHelper机器Schema的唯一开发源。模块不得保存第二份Schema。

## 当前内部协议

- `ModuleRunRef`携带`module`、运行范围和两个文件完整性校验值，用于读取已提交的结果文件与`artifacts/artifact_manifest.json`，不承担产品或合同身份。
- `ResolvedContract`记录本次运行实际使用的`product_id`、正整数`rule_revision`、身份、条款、观察日程和路径。页面每次运行都从当前OptionReg重新编译，不读取历史产品定义或任务合同。
- Host按本次输入绑定`product_id`与`rule_revision`；App、Reporter和Capability只从明确的ModuleRun读取执行快照，不存在任务级活动合同、合同版本或业务语义Hash协议。
