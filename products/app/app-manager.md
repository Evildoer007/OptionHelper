# OptionHelper App管理规范

## 1. 边界

App是同一Capability的身份、权限、模型、会话、任务、存储、审计和前端壳层，不复制Knowledger、合同解释器、金融计算或五个模块页面。Skill与App独立发行，但App只能内置已经验收的Capability。

## 2. 当前阶段

本目录提供可启动的本机HTTP平台：LocalAuthProvider签发明确的开发身份，后端强制销售用户与管理员权限，OptChat、OptDesk和设置中心壳层可用，任务、消息、非敏感设置和审计事件写入用户本机状态目录。macOS本机可通过钥匙串保存并调用DeepSeek兼容接口；iFind当前可安全保存凭据，但HTTP数据Provider仍待接入。远程登录、Wind和服务器能力未接入时必须返回结构化Unavailable，绝不生成模拟结果。

## 3. 设置与凭据

统一设置中心覆盖账号与权限、OpenAI兼容模型服务、数据接口、存储导出和界面偏好。macOS首次配置仅接收模型API Key与iFind Refresh Token；后端立即将它们写入受控凭据存储，设置文件只保存固定`SecretRef`。页面不回显、日志不记录、结果和报告不写入Secret正文。Windows与服务器环境分别接入Credential Manager和托管密钥服务。完整HTML报告固定为连续A4正文，不提供目录版。

## 4. 页面与权限

App前端只拥有登录、OptChat、OptDesk和设置中心壳层。DataFetcher、Payoffer、Pricer、Backtester、Reporter页面由内置Capability提供，并由PageRegistry逐文件校验Manifest哈希后直接挂载，不复制到`frontend/`。销售用户只允许OptChat；管理员允许OptChat和OptDesk。页面与Tool入口均由后端重新授权。

## 5. 平台与发布

Python后端及前端契约跨平台共用。macOS与Windows差异只能位于`desktop/`和`packaging/`，包括窗口、路径、凭据存储、签名和安装。当前不构建、签名或发布安装物。

## 6. Capability与回滚

App只接收已经验证的Skill候选目录，校验Capability Manifest后原样内置，不从模块源码另行拼装。App版本与Capability版本分离；回滚App或Capability时选择完整历史产物，不在安装目录覆盖局部文件。

## 7. 本机启动与验收

从仓库根目录使用通过依赖检查的Python解释器运行。系统默认Python不匹配时，可先设置`OPTIONHELPER_PYTHON`为其绝对路径：

```bash
"${OPTIONHELPER_PYTHON:-python3}" packaging/app/run_development_app.py --port 4181
```

启动器会在系统临时目录构建、验证并显式注入当次Capability；它不会读取或写入`products/app/capability/option-helper`。访问`http://127.0.0.1:4181/`。本机开发身份选择页会明确显示其不是生产登录。状态目录可通过`--data-dir`指定；目录只保存App状态，不能替代模块的数据或运行结果存储。

## 8. 验收入口

App测试覆盖角色权限、页面Manifest哈希、真实本机HTTP、任务与偏好持久化、钥匙串凭据写入边界和Unavailable状态。真实登录、iFind数据接口、服务器与Windows凭据适配分别完成后再追加集成验收，未接入能力保持明确不可用。
