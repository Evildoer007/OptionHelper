# HTML输出约定

## 1. 固定详细报告

Report为连续A4详细研究报告。HTML阅读页在宽屏显示固定左侧目录，目录只跳转七个固定章节，不构成第二种版式；PDF隐藏目录并保留同一连续正文。正文采用结论先行、图表紧贴逻辑、指标和条款接续出现的投研材料构图。它不限制篇幅、模块数量或正文大小，承载完整报告的全部固定章节、事实、状态和风险提示。

Card是同一冻结Report事实的简版研究简报：210mm宽度、高度随完整内容自然延展，无损益图和无交互图。它固定展示推荐结构与标的、推荐依据、估值定价、历史回测与最多2项风险提示；估值区保留最多四项核心指标和完整五个Greeks，回测区保留四项核心指标与四项产品专属指标。PDF导出使用A4自然分页。

完整Report固定且逐字使用以下顺序：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。输入中的章节数组、标题映射或排列顺序均不得改变公开结果。某一模块没有结果时保留对应章节，并显示真实状态。

Designer的唯一事实输入为`optionhelper.designer-payload/v1.0.0`，且`schema`字段必须存在并逐字匹配。渲染器不迁移其他版本或其他展示协议；Report始终使用内部固定连续版，Card使用内部固定模板，公开输入不接受任何版式字段。

默认交付先生成HTML。仅在用户明确导出PDF时，Designer再根据同一冻结payload生成可打印静态投影。HTML与PDF共享章节顺序、数学文本、数字格式和事实边界；PDF不把HTML伪装成文件，也不依赖浏览器JavaScript。浏览器图表在PDF中保留可访问摘要和完整数据表，避免静默生成空图。

## 2. 文件与资源

```text
报告名称_YYYYMMDD.html
```

仅当最终版面实际包含图表时才加载ECharts。默认`shared`模式下，HTML相对引用Designer模块内的`assets/vendor/echarts.min.js`，并在产物清单登记资源路径和SHA-256；同一批报告不重复保留脚本副本。`portable`Tool输出将HTML引用固定为`assets/echarts.min.js`，并返回包含`path`、`media_type`、`sha256`和`content_base64`的完整资源清单。调用方按相对路径落盘并校验哈希后，即可脱离项目目录打开。Tool请求不得覆盖ECharts物理路径或传入外链。最终文件放入用户指定目录或项目`result`，Designer资源继续留在`modules/designer/assets`。视觉规则见`design-system.md`，运行时CSS变量由唯一Token源生成，`designer-theme.css`只保留结构规则。

不要将ECharts内联进每个HTML。离线HTML保留交互图表，重要数值不得只存在于tooltip中。

## 3. 交付校验

- Report：检查七个章节逐字一致、各出现一次且顺序固定；HTML目录只链接固定七章，PDF不输出目录；连续A4研究画布完整；图表和表格与正文边界对齐；窄屏自然转为单列。
- HTML Report只有内部固定连续A4正文；CLI和Tool均不提供版式参数或目录配置。
