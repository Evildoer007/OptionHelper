# HTML输出约定

## 1. 固定详细报告

Report为连续A4详细研究报告。HTML阅读页在宽屏显示左侧目录，未提供单次展示调整时目录按标准七章跳转；PDF隐藏目录并保留同一连续正文。正文采用结论先行、图表紧贴逻辑、指标和条款接续出现的投研材料构图。用户明确要求的单次排序、白名单别名或固定说明由`presentation_patch`应用，补充事实和多结构比较则必须先由Reporter冻结；不创建第二套版式，也不污染标准模板。

标准Card是同一冻结Report事实的简版研究简报：210mm宽度、高度随完整内容自然延展，无损益图和无交互图。它默认展示推荐结构与标的、推荐依据、估值定价、历史回测与最多2项风险提示；估值区保留最多四项核心指标和完整五个Greeks，回测区保留四项核心指标与四项产品专属指标。用户明确要求的补充内容必须先由Reporter冻结；`presentation_patch`只调整排序、白名单别名和固定说明。PDF导出使用A4自然分页。

横向对比不新增公开输出类型。`card+comparison`生成MultiCard：2个候选使用2列，3个以上每行最多3列并自然换行，不加载ECharts或收益图。`report+comparison`生成MultiReport：宽屏HTML保留窄幅目录，表格每批最多3个候选，单位和口径兼容的同类图每幅最多比较4个候选，更多候选拆成下一表或下一图。两者均只读取Reporter冻结的候选事实，不混合候选合同或计算结果。

完整Report的单结构默认顺序为：核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示。Payload本身不得改写该标准结构；只有受校验的`presentation_patch`可对已知章节排序或使用白名单别名。某一模块没有结果时保留对应章节，并显示真实状态。

Designer的唯一事实输入为`optionhelper.designer-payload`，且`schema`字段必须存在并逐字匹配。渲染器不迁移其他版本或其他展示协议；Report始终使用内部固定连续版，Card使用内部固定模板，公开输入不接受任何版式字段。

默认交付先生成HTML。仅在用户明确导出PDF时，Designer再根据同一冻结payload生成可打印静态投影。HTML与PDF共享章节顺序、数学文本、数字格式和事实边界；PDF不把HTML伪装成文件，也不依赖浏览器JavaScript。浏览器图表在PDF中保留可访问摘要和完整数据表，避免静默生成空图。

## 2. Quote参考报价

Quote是第三种正式交付，用于承载与推荐结构对应的参考报价。它与Card、Report复用同一份冻结payload，因此可在不重跑模型、收益结构、估值或回测的情况下单独重新生成。

Quote只接受显式`reference_quote`事实块。每个报价分组独立声明`title`、`columns`和`rows`，以适配香草、价差、雪球及其他结构各自不同的交易要素。列仅支持`text`、`number`和`percent`三种展示格式；每一行必须完整提供该分组的所有列。Designer不会用估值现值、Greek或回测结果推断报价，也不会把缺失字段替换为“未提供”。没有冻结报价事实时，Quote直接拒绝生成。

标准Quote为无图表的连续A4参考页，使用与Card、Report一致的光大金红研究纸风格和红色三线表。页末固定提示“以上为参考报价，实际以交易台正式报价为准。”；报价日期和有效期仅在上游明确提供时显示。用户明确要求的补充内容必须先由Reporter冻结，`presentation_patch`不写回报价事实或标准模板。

## 3. 文件与资源

```text
报告名称_YYYYMMDD.html
```

仅当最终版面实际包含图表时才加载ECharts。默认`shared`模式下，HTML相对引用Designer模块内的`assets/vendor/echarts.min.js`，并在产物清单登记资源路径和SHA-256；同一批报告不重复保留脚本副本。`portable`Tool输出将HTML引用固定为`assets/echarts.min.js`，并返回包含`path`、`media_type`、`sha256`和`content_base64`的完整资源清单。调用方按相对路径落盘并校验哈希后，即可脱离项目目录打开。Tool请求不得覆盖ECharts物理路径或传入外链。最终文件放入用户指定目录或项目`result`，Designer资源继续留在`modules/designer/assets`。视觉规则见`design-system.md`，运行时CSS变量由唯一Token源生成，`designer-theme.css`只保留结构规则。

不要将ECharts内联进每个HTML。离线HTML保留交互图表，重要数值不得只存在于tooltip中。

## 4. 交付校验

- Report：标准模板检查七个章节逐字一致、各出现一次且顺序正确；带`presentation_patch`的产物检查正文和HTML目录使用同一有效章节；PDF不输出目录；连续A4研究画布完整；图表和表格与正文边界对齐；窄屏自然转为单列。
- HTML Report只有内部固定连续A4正文；CLI和Tool均不提供版式参数或目录配置。
- Quote：标准模板检查每个报价分组的列名与取值完整，所有产品专属字段直接展开；不得以估值替代报价，不显示空字段占位或外链资源。若存在用户明确的单次Patch，另检查其内容不改写报价事实且使用固定视觉令牌。
