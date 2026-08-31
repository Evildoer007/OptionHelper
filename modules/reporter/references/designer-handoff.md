# Reporter与Designer交接

交接对象固定使用以下Schema：

```text
optionhelper.designer-payload
optionhelper.design-brief
```

Reporter先冻结ReportUnit，再构造`DesignBrief`和Designer payload。Reporter通过宿主注入的Core `ModulePort`调用Designer公开Tool：

```text
designer_port.call_tool({
  "action": "render",
  "payload": "冻结内容投影",
  "output_type": "card、quote或report",
  "format": "html或pdf",
  "asset_mode": "portable"
})
```

横向对比不新增`output_type`：`card+comparison`交给多结构研究简报模板，`report+comparison`交给多结构完整研究报告模板。参考报价使用`quote`及按标的分组的冻结报价事实。Reporter的能力目录声明3个基础输出类型和`single、combined、batch、comparison、quote`交付模式；Designer按实际渲染入口声明其能力。

Reporter不导入Designer内部渲染器、模型、CSS、字体、颜色或PDF实现。未注入Designer port时服务返回`designer_port_not_injected`；Designer返回`missing_dependency`时Reporter明确失败，绝不写出伪PDF或伪成功清单。

Designer只能影响呈现。禁止新增事实、改写数值、删去来源、限制或状态，或把未运行、失败、不支持和部分完成模块包装为结论。Reporter将冻结的`pricing.greeks`按`Delta、Gamma、Vega、Theta、Rho`五项完整投影，并传递风险曲线、四个Greek曲面、定价情景和假设；不重估、不补值。Backtest投影固定包含样本数、胜率、平均收益、最大亏损，并按`metric_profile`明确选取四项Card专属指标，同时交接公共统计、路径事件、监控、路径结果、年度、标的表现、产品专属统计与已提供图表。Card不接受图或收益图，Report才展示本次冻结结果中的图表和详情表。完整来源、哈希和限制始终保留在同一ReportUnit和`run_manifest.json`。

HTML一律请求`portable`。当报告包含ECharts图表时，Designer返回`portable_assets`；Reporter逐项校验受控相对路径、媒体类型、Base64内容、SHA-256，以及`artifact_manifest.assets`和`artifact_manifest.portable_assets`的集合一致性，随后原样落盘到ReportRun并写入`rendered.portable_assets`。Reporter不得解析、删除或改写Designer HTML，最终HTML哈希必须与Designer回执的`artifact_hash`相同。无图HTML的portable资源清单为空；含动态图表或本次收益图的Report PDF仅由Designer决定是否具备组件。组件不可用时Designer必须返回`missing_dependency`，Reporter中止且不写出成功交付物。

Designer可一次渲染单合同研究简报或完整研究报告、按标的分组的参考报价，以及冻结候选Bundle对应的多结构研究简报或多结构完整研究报告。`combined`和`batch`仍是组合索引加候选独立报告，不与正式横向对比模板混用。
