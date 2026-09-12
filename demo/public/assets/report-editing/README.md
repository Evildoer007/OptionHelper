# OH离线报告编辑器

入口文件位于Demo根目录下的`assets/report-editor.js`及`assets/report-editor.css`。本目录必须随Demo一起分发，不能只复制两个入口文件。

```html
<link rel="stylesheet" href="assets/report-editor.css">
<script src="assets/report-editor.js" defer></script>
```

报告工具栏调用：

```javascript
const opened = await window.OptionHelperDemoReportEditor.open({
  path: activeReportPath,
  title: activeName
});
```

`path`只接受当前Demo的`result/reports/*.html`，可传相对路径或同站绝对URL。`title`可省略。成功返回`true`，失败返回`false`并显示原因。重复加载入口不会重复注册；入口兼容VM执行时`document.currentScript=null`。`close()`触发编辑器原有的未保存确认。加载失败事件为`optionhelper:report-editor-error`，关闭事件为`optionhelper:report-editor-close`。

- HTTP/HTTPS：只读取静态HTML及同目录内资源，不存在后台编辑服务。
- file协议：按需载入`report-snapshots/`中现有12份报告的打包快照。原报告变更后须同步对应快照；HTTP模式始终读取当前报告文件。
- HTML副本：下载当前稿的独立HTML，图片内嵌，图表由当前ECharts SVG节点序列化保存，保留数据表和图表规格。不会改写原HTML。
- 打印/PDF：为当前稿建立独立打印frame，调用浏览器打印对话框，由用户打印或存为PDF。不会声称已创建PDF文件。
- DOCX：本机已有docx9.6.1浏览器库生成真实OOXML。正文、表格及支持的MathML公式转换为原生可编辑Word内容；图表、图片转PNG并内嵌，图表数据表保留为原生表格。并非逐像素HTML排版复制，也不包含可交互的Word图表。任一图片解码失败会停止导出并保留编辑稿，不跳过内容。

`report-editor-core.js`、`report-editor-core.css`、`chart-presentation.js`原样复制自OH最新`modules/reporter/page/editor`，来源与SHA256见`source-manifest.json`。`editor-markup.js`来自OH Reporter页面，只调整静态导出文案、ARIA和frame约束。离线读取、保存及打印由`offline-controller.js`覆盖，正式服务方法不会执行。`docx.LICENSE`保留原库许可。

修复记录：ECharts`getDataURL()`会将多系列图例的带引号font-family写成无效SVG。`export.js`改为XMLSerializer序列化已渲染SVG节点，既修复HTML图片，也修复DOCX转PNG。重新执行`export.js`会原位更新导出方法，因此可在保留当前稿时加载修复版本。

定向验收文件：`tests/report_editor.test.cjs`、`tests/report_editor_browser.test.cjs`、`tests/report_editor_comparison.test.cjs`。9项合同测试通过；独立Chromium确认8.2及comparison的当前稿HTML/DOCX下载、原生文本表格公式、图表图片、390px布局、file协议读取、零外网/API请求。打印测试实际加载当前稿打印frame，只拦截最终原生对话框调用，不能证明用户已保存PDF。comparison导出包含全部36张图片。`qa/`只保存验收夹具与输出，不参与运行时。

写入边界：仅编辑器入口、本目录、新增report测试。未修改Demo主页面、demo-enhancements.js、原报告、OH源码或用户状态。未创建子agent。
