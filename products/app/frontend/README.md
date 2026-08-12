# App前端壳层

本目录只维护App自身的登录、OptChat、OptDesk和统一设置中心。每个页面均为可由AppServer同源静态托管的独立HTML、CSS与ES Module资源。

静态路由约定：

- `/app/frontend/login/index.html`
- `/app/frontend/optchat/index.html`
- `/app/frontend/optdesk/index.html`
- `/app/frontend/settings/index.html`
- `/app/frontend/shared/styles.css`与`/app/frontend/shared/app.js`

AppServer的`/`、`/optchat`、`/optdesk`和`/settings`正式路由应分别返回这些页面；前端只调用同源`/api/*`接口。五个模块页面由内置Capability以`/capability/assets/pages/{module}/{module}.html`挂载，不能复制到本目录。
