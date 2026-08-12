# 三模块UI暂存区

此目录只用于统一调整Payoffer、Pricer、Backtester的人机界面，不是模块运行入口。

```text
module-ui/
├── payoffer/                 当前Payoffer页面与UI样式副本
├── pricer/                   当前Pricer页面与UI样式副本
├── backtester/               当前Backtester页面与UI样式副本
└── vendor/echarts.min.js     Pricer、Backtester页面的离线ECharts依赖
```

每个子目录包含页面HTML和`ui/style.css`、`ui/controls.css`。Pricer与Backtester的
`vendor`为指向本目录离线ECharts的链接，因此暂存页面可独立预览。

当前运行入口仍在：

```text
assets/pricer/pricer.html
assets/backtester/backtester.html
blueprint/payoffer.html
```

在本暂存区完成统一UI修改并确认后，再由人工决定回填到三个运行页面；运行内核、
OptionReg、默认SVG和`result/`均不在本次UI暂存范围内。
