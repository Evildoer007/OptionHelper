# OptionHelper Demo

打开本目录的`index.html`，或打开`public/index.html`。静态托管目录为`public/`。

演示账号：`admin`，密码：`8888`。登录只在当前浏览器内生效，不连接正式App账号。

## 前端来源

登录、OptChat、OptDesk、设置中心以及数据获取、收益结构、估值定价、历史回测、研究报告，均从当前OptionHelper源码构建。页面结构、样式、图标、选择器和交互沿用App；仅适配静态地址、内嵌页面和离线数据接口。`public/source-manifest.json`记录每个前端资源的SHA-256。

离线版可管理本地演示任务、编辑条款、查看65种产品的默认收益图、切换主题和设置、编辑及导出已有HTML报告。行情下载、模型对话、Python定价和回测需要正式App服务；离线Demo不会把历史示例当作新计算结果。

## 更新两份Demo

项目内`OptionHelper/demo`作为构建源，桌面的`demo/OptionHelper demo`由同一次构建同步。`tools/refresh_demo.py`先重新构建，再备份旧桌面目录并同步，最后校验两份文件。源码默认位于桌面OptionHelper目录，也可通过`OPTIONHELPER_SOURCE_ROOT`指定。

构建使用现有Python环境和Node.js；构建依赖记录在`tools/package.json`与锁文件中。已安装的esbuild可通过`NODE_PATH`提供，也可在`tools/`安装锁定依赖。

## 验收

`tests/latest-source-parity.test.cjs`检查9个页面结构、63项资源、源码哈希、已安装macOS前端及两目录文件一致性。安装包中的Designer样式相对路径仅作路径归一化比较。

`tests/latest-ui-browser.test.cjs`检查登录、设置弹层、草稿保留、五模块、产品切换、主界面和模块的主题联动、窄窗口及外部请求，支持Chromium、WebKit及本地HTML入口。

`tests/installed-render-parity.test.cjs`在相同浏览器、窗口和离线数据下，对比安装版原始前端与Demo的实际截图，并逐项校验可见控件的文字、尺寸、位置、字体、颜色和边框。像素记录保留原始差异，只允许颜色通道不超过4/255的原生控件边缘抗锯齿波动，不遮挡界面元素。该检查不包含原生窗口标题栏和后端计算。

最新检查记录见`tests/latest-ui-review.md`。更新时，每个页面统一打包一个ES模块依赖图，避免主题、缩放和滚动模块被重复初始化。

`tests/frontend-current-20260908.test.cjs`是旧版文案测试，保留作历史记录。当前验收以最新源码和上述新版检查为准。
