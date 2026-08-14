# OptionHelper App管理规范

## 1. 边界

App是同一Capability的身份、权限、模型、会话、任务、存储、审计和前端壳层，不复制Knowledger、合同解释器、金融计算或五个模块页面。Skill与App独立发行，但App只能内置已经验收的Capability。

## 2. 当前阶段

本目录提供可启动的本机HTTP平台。开发启动器使用明确的本机开发身份；桌面候选使用本机账号口令。后端统一执行角色权限，OptChat、OptDesk、设置中心、任务、消息、审计和本机状态持久化均已接入。模型服务采用OpenAI兼容接口；DataFetcher通过iFind Refresh Token获取行情和交易日历。远程身份、服务器托管密钥及未接入的数据源必须返回结构化Unavailable，绝不生成模拟结果。

## 3. 设置与凭据

统一设置中心覆盖账号与权限、OpenAI兼容模型服务、数据接口、存储导出和界面偏好。模型API Key与iFind Refresh Token写入受控凭据存储，设置文件只保存固定`SecretRef`。页面不回显、日志不记录、结果和报告不写入Secret正文。完整HTML报告使用连续A4正文并在宽屏提供左侧章节目录；PDF保留同一正文顺序但不显示导航。

## 4. 页面与权限

App前端只拥有登录、OptChat、OptDesk和设置中心壳层。DataFetcher、Payoffer、Pricer、Backtester、Reporter页面由内置Capability提供，并由PageRegistry逐文件校验Manifest哈希后直接挂载，不复制到`frontend/`。销售用户只允许OptChat；管理员允许OptChat和OptDesk。页面与Tool入口均由后端重新授权。

## 5. 平台与发布

Python后端及前端契约跨平台共用。macOS与Windows差异只能位于`desktop/`和`packaging/`，包括窗口、路径、凭据存储、签名和安装。仓库可生成本机候选安装物；Developer ID签名、公证及外部分发状态必须由发行清单如实声明，不能由开发构建结果推断。

## 6. Capability与回滚

App只接收已经验证的Skill候选目录，校验Capability Manifest后原样内置，不从模块源码另行拼装。App版本与Capability版本分离；回滚App或Capability时选择完整历史产物，不在安装目录覆盖局部文件。

## 7. 本机启动与验收

从仓库根目录使用通过依赖检查的Python解释器运行。系统默认Python不匹配时，可先设置`OPTIONHELPER_PYTHON`为其绝对路径：

```bash
"${OPTIONHELPER_PYTHON:-python3}" packaging/app/run_development_app.py --port 4181
```

启动器会在系统临时目录构建、验证并显式注入当次Capability；它不读取仓库内的旧Capability副本。访问`http://127.0.0.1:4181/`。开发启动与桌面候选的登录模式不同，不得混用。状态目录可通过`--data-dir`指定；目录只保存App状态，不能替代模块的数据或运行结果Store。

## 8. 验收入口

App测试覆盖登录、角色权限、页面Manifest哈希、本机HTTP、任务与偏好持久化、受控凭据、iFind行情与交易日历、计算模块及报告入口。平台签名、公证、Windows凭据和服务器托管能力分别验收；未接入能力保持明确不可用。
