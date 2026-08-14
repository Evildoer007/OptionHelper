# Reporter管理规范

维护ReportRequest、ModuleRunRef解析、证据一致性、ReportUnit、ReportBundle、状态展示、导出和审计。页面选择由宿主`ResultSelectionPort`提供当前租户的受控证据投影，服务端重建请求；不得扫描目录或信任浏览器提交的source_refs。`config.py`只拥有Reporter输出与验证默认值；`artifact_validator.py`唯一负责交付物格式、哈希和PDF头校验。报告只接受显式引用，必须校验合同指纹、产品版本、标的顺序、数据口径和事实哈希。

验收覆盖兼容与不兼容引用、缺失或失败模块、Card/Report内容裁剪、HTML/PDF同源、Report HTML连续正文与左侧章节目录、批量交付及产物回溯。Designer只能接收冻结事实。

## 修改与回滚

可修改取证、内容组织、状态表达和导出编排，但不得重新计算或扫描“最新结果”。内容结构与模板版本分离；回滚时恢复ReportUnit构建规则和Designer版本，不覆盖既有ReportRun。

## 冲突升级

金融事实冲突退回来源模块；视觉冲突交Designer；权限、保存位置和服务器发布交App。
