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
  "output_type": "card或report",
  "format": "html或pdf",
  "asset_mode": "portable"
})
```

Reporter不导入Designer内部渲染器、模型、CSS、字体、颜色或PDF实现。未注入Designer port时服务返回`designer_port_not_injected`；Designer返回`missing_dependency`时Reporter明确失败，绝不写出伪PDF或伪成功清单。

Designer只能影响呈现。禁止新增事实、改写数值、删去来源、限制或状态，或把未运行、失败、不支持和部分完成模块包装为结论。Reporter将冻结的`pricing.greeks`按`Delta、Gamma、Vega、Theta、Rho`五项完整投影，并传递风险曲线、四个Greek曲面、定价情景和假设；不重估、不补值。Backtest投影固定包含样本数、胜率、平均收益、最大亏损，并按`metric_profile`明确选取四项Card专属指标，同时交接公共统计、路径事件、监控、路径结果、年度、标的表现、产品专属统计与已提供图表。Card不接受图或收益图，Report才展示本次冻结结果中的图表和详情表。完整来源、哈希和限制始终保留在同一ReportUnit和`run_manifest.json`。

HTML一律请求`portable`。当报告包含ECharts图表时，Designer返回`portable_assets`；Reporter逐项校验受控相对路径、媒体类型、Base64内容、SHA-256，以及`artifact_manifest.assets`和`artifact_manifest.portable_assets`的集合一致性，随后原样落盘到ReportRun并写入`rendered.portable_assets`。Reporter不得解析、删除或改写Designer HTML，最终HTML哈希必须与Designer回执的`artifact_hash`相同。无图HTML的portable资源清单为空；含动态图表或本次收益图的Report PDF仅由Designer决定是否具备组件。组件不可用时Designer必须返回`missing_dependency`，Reporter中止且不写出成功交付物。

当前Designer一次渲染一个完整合同payload。因此`combined`和`batch`的根页面明确标记为“组合索引+候选独立报告”：根页显示候选比较与逐候选摘要，每个候选的完整Payoff、估值和回测内容由独立Designer报告输出。它不声明为完整同页多合同渲染。
