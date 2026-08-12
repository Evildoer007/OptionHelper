# OptionHelper本机前端

本目录实现分发给终端用户的本机版本。它同时服务于两种启动方式：

| 方式 | 适用场景 | 功能边界 |
| --- | --- | --- |
| App | macOS用户直接打开应用 | 内置WebView、本机服务、许可证、模型连接和Report库 |
| 脚本加HTML | 需要浏览器运行或二次集成 | 同一套本机服务、网页和数据，不依赖外部账号系统 |

两种方式的许可证、模式、模型连接、本机任务和Report规则一致。唯一差别是App由WebView承载，脚本版由默认浏览器承载。

## 用户版本

| 许可证层级 | 页面可见模式 | 能力 |
| --- | --- | --- |
| `optchat` | OptChat | 模型对话、自动简版Report、本机Report库 |
| `optdesk` | OptChat、OptDesk | OptChat能力加Payoff、Pricing、Backtest、完整版Report原型 |
| `admin` | OptChat、OptDesk | 与OptDesk相同的使用能力，另供发行方签发和维护授权 |

普通用户界面只显示OptChat和OptDesk，不显示内部权限名称。系统不再使用本地账号、密码、销售或研究团队身份。

## 发行方操作

以下命令只应在发行方安全的macOS主机执行一次：

```bash
node assets/web-design/optionhelper-license.mjs init
```

它会把Ed25519私钥保存到当前macOS钥匙串，并生成`assets/web-design/distribution/license-public.pem`。私钥不能进入安装包、代码仓库、聊天记录或用户设备。

签发许可证：

```bash
node assets/web-design/optionhelper-license.mjs issue optchat "客户名称" --out /绝对路径/OptionHelper.license
node assets/web-design/optionhelper-license.mjs issue optdesk "客户名称" --out /绝对路径/OptionHelper.license
```

许可证默认永久有效，不含模型Key、聊天内容和Report。更换产品层级或发布不兼容版本时重新签发即可。第一版不进行在线校验、远程撤销或硬件绑定。

## 用户首次使用

1. 打开App，或运行脚本版。
2. 导入发行方提供的`OptionHelper.license`。
3. 选择DeepSeek、OpenAI、Anthropic、Google Gemini或OpenAI兼容接口。
4. 填写模型名称、接口地址和自己的API Key，保存并测试连接。需要时可继续新增其他模型连接。
5. 测试通过后进入OptChat或OptDesk，并在对话输入框中选择当前使用的模型。

每个模型服务的API Key只保存于当前macOS钥匙串。授权状态、模式偏好、模型连接元信息、任务、完整对话、模块参数和Report数据库保存于`~/Library/Application Support/OptionHelper`，其权限仅限当前系统用户。用户换电脑后重新导入许可证并配置自己的Key；Report可通过HTML导出手动迁移。

## 启动脚本加HTML版本

```bash
zsh assets/web-design/optionhelper-web.command
```

脚本只监听`127.0.0.1:4180`。不要使用`file:///`直接打开HTML，因为许可证校验、钥匙串访问、模型请求和Report库都需要本机服务。

## 构建macOS App

```bash
zsh apps/macos/OptionHelperApp/build-app.command
```

生成物位于`apps/macos/OptionHelperApp/build/OptionHelper.app`。构建脚本把当前Node.js运行时、网页资源和Logo复制到App中；正式分发前仍需要完成签名、公证和发布版Node运行时审计。

## Report与模块边界

OptChat只保存模型输出的有效简版Report。OptDesk可保存完整版Report，包含任务背景、模块来源、参数、估值假设、回测状态、运行记录和待确认事项。所有自动Report固定标注“AI生成，未经人工审核”。

Report可导出HTML；在Report页面点击“打印为PDF”即可使用macOS浏览器或系统打印面板保存PDF。导出内容不包含API Key。

OptChat与OptDesk共享同一任务ID。切换模式后会保留完整对话和任务上下文。长对话会在本机保存较早内容摘要，并将摘要与最近消息发送给用户当前选择的模型。删除任务只删除该任务对话和模块草稿，已生成Report仍保留。

OptDesk首版每个任务只支持一只标的。多标的结构不会拆成多个单标的任务，也不会出现在产品选择列表中。Payoff、Pricing和Backtest当前可保存参数，但不会调用真实引擎，也不会展示伪造的价格、收益、Greeks或回测数据。收益结构工作台直接位于Desk内，只读展示`assets/payoffer/figures/svg/`中已经发布的正式收益图；Desk不会读取或改写`assets/payoffer/figures/json/`中的固定默认JSON。

## 本机接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/access/profile` | 返回激活状态、模式和连接状态 |
| `POST /api/activation/import` | 导入并校验签名许可证 |
| `GET/POST/DELETE /api/model-connection` | 读取、保存或移除本机模型连接 |
| `POST /api/model-connection/active` | 切换当前对话默认模型 |
| `POST /api/ai/chat` | 发送OptChat或OptDesk对话，返回可选Report |
| `GET/POST /api/tasks` | 读取任务历史或新建本机任务 |
| `GET/PATCH/DELETE /api/tasks/:id` | 读取、保存模块状态、重命名或删除任务 |
| `GET /api/tasks/:id/messages` | 读取指定任务完整对话 |
| `GET /api/reports` | 读取当前设备的Report列表 |
| `GET /api/reports/:id` | 读取指定Report |
| `POST /api/reports/:id/export` | 导出HTML |
| `DELETE /api/reports/:id` | 主动清除本机Report |

服务端在每个Desk和完整版Report入口校验许可证。前端隐藏不是权限边界。

## 验证

```bash
node --test assets/web-design/tests/local-core.test.mjs
```

测试覆盖许可证签名、许可证层级、Report权限和OptChat对Desk入口的服务端拦截。测试不读取真实Key，也不调用外部模型。
