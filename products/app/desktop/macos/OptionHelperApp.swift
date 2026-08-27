import Cocoa
import WebKit

private final class OptionHelperWindow: NSWindow {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard modifiers == .command,
              let key = event.charactersIgnoringModifiers?.lowercased(),
              let action = switch key {
              case "x": #selector(NSResponder.cut(_:))
              case "c": #selector(NSResponder.copy(_:))
              case "v": #selector(NSResponder.paste(_:))
              case "a": #selector(NSResponder.selectAll(_:))
              default: nil
              }
        else {
            return super.performKeyEquivalent(with: event)
        }

        // WKWebView keeps the focused HTML control in its responder chain.  If
        // macOS cannot resolve the menu shortcut, route it through that chain.
        return NSApp.sendAction(action, to: nil, from: self) || super.performKeyEquivalent(with: event)
    }
}

private final class OptionHelperTitlebarDragView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.performDrag(with: event)
    }
}

@main
final class OptionHelperApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    private let uiScaleSteps: [CGFloat] = [0.8, 0.9, 1.0, 1.1, 1.25, 1.4]
    private var backend: Process?
    private var startupPipe: Pipe?
    private var window: NSWindow?
    private var webView: WKWebView?
    private var titlebarDragView: OptionHelperTitlebarDragView?
    private var railToggle: NSButton?
    private var reportToggle: NSButton?
    private var loadedURL = false
    private var themePreference = "light"
    private var activeOperationCount = 0
    private var closeAfterInterrupt = false
    private var uiScale: CGFloat = 1.0
    private var uiPreferencesURL: URL?
    private var zoomInMenuItem: NSMenuItem?
    private var zoomOutMenuItem: NSMenuItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installMainMenu()
        NSApp.addObserver(
            self,
            forKeyPath: #keyPath(NSApplication.effectiveAppearance),
            options: [.new],
            context: nil
        )
        startBackend()
    }

    func applicationWillTerminate(_ notification: Notification) {
        startupPipe?.fileHandleForReading.readabilityHandler = nil
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperTheme")
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperRuntime")
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperUIScale")
        NSApp.removeObserver(self, forKeyPath: #keyPath(NSApplication.effectiveAppearance))
        if let backend, backend.isRunning {
            backend.terminate()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard !flag, let window else { return true }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }

    private func startBackend() {
        guard let resources = Bundle.main.resourceURL else {
            showFailure("找不到内置资源目录")
            return
        }
        let executable = resources
            .appendingPathComponent("backend", isDirectory: true)
            .appendingPathComponent("OptionHelperBackend", isDirectory: true)
            .appendingPathComponent("OptionHelperBackend", isDirectory: false)
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            showFailure("找不到内置App Host")
            return
        }
        let environment = ProcessInfo.processInfo.environment
        let requestedDataDirectory = environment["OPTIONHELPER_APP_DATA_DIR"]?.trimmingCharacters(in: .whitespacesAndNewlines)
        let support: URL
        if let requestedDataDirectory, !requestedDataDirectory.isEmpty {
            support = URL(fileURLWithPath: requestedDataDirectory, isDirectory: true)
        } else {
            support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("OptionHelper", isDirectory: true)
                .appendingPathComponent("local-state", isDirectory: true)
        }
        do {
            try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
        } catch {
            showFailure("无法创建本地数据目录")
            return
        }
        uiPreferencesURL = support.appendingPathComponent("ui-preferences.json", isDirectory: false)
        uiScale = loadUIScale()
        updateScaleMenuAvailability()

        let process = Process()
        process.executableURL = executable
        process.currentDirectoryURL = executable.deletingLastPathComponent()
        process.arguments = [
            "--host", "127.0.0.1", "--port", "0", "--data-dir", support.path,
            "--resource-dir", resources.path,
        ]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let output = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.consumeBackendOutput(output) }
        }
        process.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                if !(self?.loadedURL ?? false) && terminated.terminationStatus != 0 {
                    self?.showFailure("App Host未能启动")
                }
            }
        }
        do {
            try process.run()
            backend = process
            startupPipe = pipe
        } catch {
            showFailure("无法启动内置App Host")
        }
    }

    private func consumeBackendOutput(_ output: String) {
        guard !loadedURL else { return }
        for line in output.split(whereSeparator: \.isNewline) where line.hasPrefix("OPTIONHELPER_URL=") {
            let address = String(line.dropFirst("OPTIONHELPER_URL=".count))
            guard let url = URL(string: address), url.host == "127.0.0.1" else {
                showFailure("App Host返回了无效地址")
                return
            }
            loadedURL = true
            showWebView(url: url)
            return
        }
    }

    private func showWebView(url: URL) {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            showFailure("App Host返回了无效启动地址")
            return
        }
        var queryItems = components.queryItems ?? []
        queryItems.removeAll { $0.name == "app_startup" }
        // A fresh native launch must play once even if WebKit restores an old
        // sessionStorage page.  The login page removes this token from the URL
        // after consuming it, so route changes never replay the animation.
        queryItems.append(URLQueryItem(name: "app_startup", value: UUID().uuidString))
        components.queryItems = queryItems
        guard let startupURL = components.url else {
            showFailure("App Host返回了无效启动地址")
            return
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        // Show the splash before the login page's module graph is evaluated.
        // The login module owns the timer and always removes this class again.
        let scaleValue = javascriptNumber(uiScale)
        let titlebarHeight = javascriptNumber(36 / uiScale)
        let collapsedSafeArea = javascriptNumber(166 / uiScale)
        let startupScript = WKUserScript(
            source: "document.documentElement.dataset.nativeShell='macos';document.documentElement.dataset.nativeMaterial='system';document.documentElement.dataset.uiScale='\(scaleValue)';document.documentElement.style.setProperty('--native-titlebar-height','\(titlebarHeight)px');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','\(collapsedSafeArea)px');if (location.pathname === '/') document.documentElement.classList.add('login-boot');",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        let runtimeStatusScript = WKUserScript(
            source: "(()=>{const post=(count)=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'active_operations',count});const refresh=()=>fetch('/api/runtime/active-operations').then((response)=>response.ok?response.json():{active_count:0}).then((value)=>post(Number(value.active_count)||0)).catch(()=>post(0));addEventListener('pageshow',refresh);setInterval(refresh,1400);refresh();})();",
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        )
        configuration.userContentController.addUserScript(startupScript)
        configuration.userContentController.addUserScript(runtimeStatusScript)
        configuration.userContentController.add(self, name: "optionhelperTheme")
        configuration.userContentController.add(self, name: "optionhelperRuntime")
        configuration.userContentController.add(self, name: "optionhelperUIScale")
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsMagnification = false
        view.pageZoom = uiScale
        view.navigationDelegate = self
        view.underPageBackgroundColor = .clear
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor

        let materialView = NSVisualEffectView(frame: .zero)
        materialView.material = .sidebar
        materialView.blendingMode = .behindWindow
        materialView.state = .followsWindowActiveState
        materialView.isEmphasized = false

        let contentView = NSView(frame: .zero)
        contentView.wantsLayer = true
        contentView.layer?.backgroundColor = NSColor.clear.cgColor
        materialView.translatesAutoresizingMaskIntoConstraints = false
        view.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(materialView)
        contentView.addSubview(view)
        NSLayoutConstraint.activate([
            materialView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            materialView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            materialView.topAnchor.constraint(equalTo: contentView.topAnchor),
            materialView.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            view.topAnchor.constraint(equalTo: contentView.topAnchor),
            view.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
        ])
        let window = OptionHelperWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "OptionHelper"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.titlebarSeparatorStyle = .none
        window.isMovableByWindowBackground = true
        window.backgroundColor = .clear
        window.isOpaque = false
        window.center()
        window.contentView = contentView
        window.delegate = self
        window.makeFirstResponder(view)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window
        self.webView = view
        ensureTitlebarControls(in: window)
        DispatchQueue.main.async { [weak self, weak window] in
            guard let self, let window else { return }
            self.ensureTitlebarControls(in: window)
            self.syncWorkspaceTitlebarControls(for: self.webView?.url)
        }
        view.load(URLRequest(url: startupURL))
    }

    private func ensureTitlebarControls(in window: NSWindow) {
        guard let closeButton = window.standardWindowButton(.closeButton),
              let titlebar = closeButton.superview else { return }

        if let titlebarDragView, titlebarDragView.superview !== titlebar {
            titlebarDragView.removeFromSuperview()
            self.titlebarDragView = nil
        }
        if let railToggle, railToggle.superview !== titlebar {
            railToggle.removeFromSuperview()
            self.railToggle = nil
        }
        if let reportToggle, reportToggle.superview !== titlebar {
            reportToggle.removeFromSuperview()
            self.reportToggle = nil
        }

        if titlebarDragView == nil { installTitlebarDragView(in: window) }
        if railToggle == nil { installRailToggle(in: window) }
        if reportToggle == nil { installReportToggle(in: window) }
        layoutTitlebarControls()
    }

    private func installTitlebarDragView(in window: NSWindow) {
        guard let closeButton = window.standardWindowButton(.closeButton), let titlebar = closeButton.superview else { return }
        let dragView = OptionHelperTitlebarDragView(frame: .zero)
        titlebar.addSubview(dragView)
        titlebarDragView = dragView
        layoutTitlebarControls()
    }

    private func installRailToggle(in window: NSWindow) {
        guard let closeButton = window.standardWindowButton(.closeButton), let titlebar = closeButton.superview else { return }
        let size = NSSize(width: 26, height: 24)
        let button = NSButton(frame: NSRect(
            x: closeButton.frame.maxX + 94,
            y: closeButton.frame.midY - size.height / 2,
            width: size.width,
            height: size.height
        ))
        button.image = NSImage(systemSymbolName: "sidebar.left", accessibilityDescription: "收起或展开任务栏")
        button.contentTintColor = .secondaryLabelColor
        button.bezelStyle = .inline
        button.isBordered = false
        button.target = self
        button.action = #selector(toggleRail(_:))
        button.toolTip = "收起或展开任务栏"
        button.isHidden = true
        titlebar.addSubview(button)
        railToggle = button
        layoutTitlebarControls()
    }

    private func installReportToggle(in window: NSWindow) {
        guard let closeButton = window.standardWindowButton(.closeButton), let titlebar = closeButton.superview else { return }
        let button = NSButton(frame: .zero)
        button.image = NSImage(systemSymbolName: "sidebar.right", accessibilityDescription: "打开或收起报告库")
        button.contentTintColor = .secondaryLabelColor
        button.bezelStyle = .inline
        button.isBordered = false
        button.target = self
        button.action = #selector(toggleReport(_:))
        button.toolTip = "打开或收起报告库"
        button.isHidden = true
        titlebar.addSubview(button)
        reportToggle = button
        layoutTitlebarControls()
    }

    private func layoutTitlebarControls() {
        guard let window,
              let closeButton = window.standardWindowButton(.closeButton),
              let titlebar = closeButton.superview else { return }
        let size = NSSize(width: 26, height: 24)
        railToggle?.frame = NSRect(
            x: closeButton.frame.maxX + 94,
            y: closeButton.frame.midY - size.height / 2,
            width: size.width,
            height: size.height
        )
        reportToggle?.frame = NSRect(
            x: titlebar.bounds.maxX - size.width - 14,
            y: closeButton.frame.midY - size.height / 2,
            width: size.width,
            height: size.height
        )
        let dragLeading = (railToggle?.frame.maxX ?? closeButton.frame.maxX + 120) + 8
        let dragTrailing = (reportToggle?.frame.minX ?? titlebar.bounds.maxX - 40) - 8
        titlebarDragView?.frame = NSRect(
            x: dragLeading,
            y: 0,
            width: max(0, dragTrailing - dragLeading),
            height: titlebar.bounds.height
        )
    }

    @objc private func toggleRail(_ sender: Any?) {
        webView?.evaluateJavaScript("document.querySelector('[data-rail-collapse-toggle]')?.click()")
    }

    @objc private func toggleReport(_ sender: Any?) {
        webView?.evaluateJavaScript("document.querySelector('[data-report-toggle]')?.click()")
    }

    private func syncWorkspaceTitlebarControls(for url: URL?) {
        let route = url?.path
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .lowercased()
        let showsWorkspaceControls = route == "optchat" || route == "optdesk"
        railToggle?.isHidden = !showsWorkspaceControls
        reportToggle?.isHidden = !showsWorkspaceControls
        layoutTitlebarControls()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        syncUIScaleToPage()
        if let window { ensureTitlebarControls(in: window) }
        syncWorkspaceTitlebarControls(for: webView.url)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        syncUIScaleToPage()
    }

    func windowDidResize(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        if let window { ensureTitlebarControls(in: window) }
        syncWorkspaceTitlebarControls(for: webView?.url)
    }

    func windowDidBecomeKey(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        if let window { ensureTitlebarControls(in: window) }
        syncWorkspaceTitlebarControls(for: webView?.url)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard sender === window else { return true }
        if closeAfterInterrupt || activeOperationCount == 0 { return true }
        let alert = NSAlert()
        alert.messageText = "仍有任务正在运行"
        alert.informativeText = "退出会停止这些运行，并将它们标记为中断。"
        alert.addButton(withTitle: "退出并停止")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return false }
        closeAfterInterrupt = true
        webView?.evaluateJavaScript("fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{}).finally(()=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'shutdown_complete'}));")
        return false
    }

    func windowWillClose(_ notification: Notification) {
        guard notification.object as? NSWindow === window else { return }
        NSApp.terminate(nil)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "optionhelperUIScale", let value = message.body as? [String: Any],
           value["type"] as? String == "set_ui_scale", let scale = value["scale"] as? NSNumber,
           let accepted = validatedUIScale(scale.doubleValue) {
            applyUIScale(accepted, persist: true, notifyPage: true)
            return
        }
        if message.name == "optionhelperRuntime", let value = message.body as? [String: Any] {
            if value["type"] as? String == "active_operations", let count = value["count"] as? Int {
                activeOperationCount = max(0, count)
            } else if value["type"] as? String == "shutdown_complete" {
                activeOperationCount = 0
                window?.performClose(nil)
            }
            return
        }
        guard message.name == "optionhelperTheme", let value = message.body as? [String: Any],
              let theme = value["theme"] as? String, let preference = value["preference"] as? String,
              ["light", "dark", "auto"].contains(preference), ["light", "dark"].contains(theme) else { return }
        themePreference = preference
        if preference == "auto" {
            window?.appearance = nil
            let appearance = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
            applyDockIcon(theme: appearance == .darkAqua ? "dark" : "light")
            DispatchQueue.main.async { [weak self] in
                self?.webView?.evaluateJavaScript("window.OptionHelperTheme?.refreshSystemTheme?.();", completionHandler: nil)
            }
        } else {
            window?.appearance = NSAppearance(named: theme == "dark" ? .darkAqua : .aqua)
            applyDockIcon(theme: theme)
        }
    }

    override func observeValue(
        forKeyPath keyPath: String?,
        of object: Any?,
        change: [NSKeyValueChangeKey: Any]?,
        context: UnsafeMutableRawPointer?
    ) {
        if keyPath == #keyPath(NSApplication.effectiveAppearance), object as AnyObject? === NSApp {
            guard themePreference == "auto" else { return }
            let appearance = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
            applyDockIcon(theme: appearance == .darkAqua ? "dark" : "light")
            return
        }
        super.observeValue(forKeyPath: keyPath, of: object, change: change, context: context)
    }

    private func applyDockIcon(theme: String) {
        guard theme == "dark", let resources = Bundle.main.resourceURL else {
            NSApp.applicationIconImage = nil
            return
        }
        let icon = resources.appendingPathComponent("assets/icons/optionhelper-app-icon-tile-dark.icns")
        NSApp.applicationIconImage = NSImage(contentsOf: icon)
    }

    private func installMainMenu() {
        let mainMenu = NSMenu()

        let appMenu = NSMenu(title: "OptionHelper")
        let appMenuItem = NSMenuItem()
        appMenuItem.submenu = appMenu
        appMenu.addItem(withTitle: "退出OptionHelper", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        mainMenu.addItem(appMenuItem)

        let editMenu = NSMenu(title: "编辑")
        let editMenuItem = NSMenuItem()
        editMenuItem.submenu = editMenu
        addEditItem("撤销", action: #selector(NSResponder.undo(_:)), key: "z", to: editMenu)
        addEditItem("重做", action: #selector(NSResponder.redo(_:)), key: "z", modifiers: [.command, .shift], to: editMenu)
        editMenu.addItem(.separator())
        addEditItem("剪切", action: #selector(NSResponder.cut(_:)), key: "x", to: editMenu)
        addEditItem("复制", action: #selector(NSResponder.copy(_:)), key: "c", to: editMenu)
        addEditItem("粘贴", action: #selector(NSResponder.paste(_:)), key: "v", to: editMenu)
        addEditItem("全选", action: #selector(NSResponder.selectAll(_:)), key: "a", to: editMenu)
        mainMenu.addItem(editMenuItem)

        let viewMenu = NSMenu(title: "显示")
        let viewMenuItem = NSMenuItem()
        viewMenuItem.submenu = viewMenu
        let zoomIn = NSMenuItem(title: "放大", action: #selector(increaseUIScale(_:)), keyEquivalent: "+")
        zoomIn.keyEquivalentModifierMask = .command
        zoomIn.target = self
        viewMenu.addItem(zoomIn)
        let zoomOut = NSMenuItem(title: "缩小", action: #selector(decreaseUIScale(_:)), keyEquivalent: "-")
        zoomOut.keyEquivalentModifierMask = .command
        zoomOut.target = self
        viewMenu.addItem(zoomOut)
        viewMenu.addItem(.separator())
        let actualSize = NSMenuItem(title: "实际大小", action: #selector(resetUIScale(_:)), keyEquivalent: "0")
        actualSize.keyEquivalentModifierMask = .command
        actualSize.target = self
        viewMenu.addItem(actualSize)
        zoomInMenuItem = zoomIn
        zoomOutMenuItem = zoomOut
        mainMenu.addItem(viewMenuItem)

        NSApp.mainMenu = mainMenu
        updateScaleMenuAvailability()
    }

    private func validatedUIScale(_ value: Double) -> CGFloat? {
        uiScaleSteps.first { abs(Double($0) - value) < 0.0001 }
    }

    private func javascriptNumber(_ value: CGFloat) -> String {
        String(format: "%.4f", locale: Locale(identifier: "en_US_POSIX"), Double(value))
    }

    private func loadUIScale() -> CGFloat {
        guard let uiPreferencesURL,
              let data = try? Data(contentsOf: uiPreferencesURL),
              let decoded = try? JSONSerialization.jsonObject(with: data),
              let object = decoded as? [String: Any],
              (object["schema_version"] as? NSNumber)?.intValue == 1,
              let value = object["ui_scale"] as? NSNumber,
              let accepted = validatedUIScale(value.doubleValue) else { return 1.0 }
        return accepted
    }

    private func persistUIScale() {
        guard let uiPreferencesURL,
              let data = try? JSONSerialization.data(
                withJSONObject: ["schema_version": 1, "ui_scale": Double(uiScale)],
                options: [.prettyPrinted, .sortedKeys]
              ) else { return }
        try? data.write(to: uiPreferencesURL, options: .atomic)
    }

    private func applyUIScale(_ scale: CGFloat, persist: Bool, notifyPage: Bool) {
        guard uiScaleSteps.contains(scale) else { return }
        uiScale = scale
        webView?.pageZoom = scale
        if persist { persistUIScale() }
        updateScaleMenuAvailability()
        if notifyPage { syncUIScaleToPage() }
    }

    private func syncUIScaleToPage() {
        guard let webView else { return }
        let value = javascriptNumber(uiScale)
        let titlebarHeight = javascriptNumber(36 / uiScale)
        let collapsedSafeArea = javascriptNumber(166 / uiScale)
        let script = "document.documentElement.dataset.uiScale='\(value)';document.documentElement.style.setProperty('--native-titlebar-height','\(titlebarHeight)px');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','\(collapsedSafeArea)px');window.OptionHelperUIScale?.syncFromNative?.(\(value));"
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    private func changeUIScale(by offset: Int) {
        let currentIndex = uiScaleSteps.firstIndex(of: uiScale) ?? uiScaleSteps.firstIndex(of: 1.0)!
        let nextIndex = max(0, min(uiScaleSteps.count - 1, currentIndex + offset))
        applyUIScale(uiScaleSteps[nextIndex], persist: true, notifyPage: true)
    }

    private func updateScaleMenuAvailability() {
        zoomOutMenuItem?.isEnabled = uiScale > uiScaleSteps.first!
        zoomInMenuItem?.isEnabled = uiScale < uiScaleSteps.last!
    }

    @objc private func increaseUIScale(_ sender: Any?) { changeUIScale(by: 1) }
    @objc private func decreaseUIScale(_ sender: Any?) { changeUIScale(by: -1) }
    @objc private func resetUIScale(_ sender: Any?) { applyUIScale(1.0, persist: true, notifyPage: true) }

    private func addEditItem(
        _ title: String,
        action: Selector,
        key: String,
        modifiers: NSEvent.ModifierFlags = .command,
        to menu: NSMenu
    ) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.keyEquivalentModifierMask = modifiers
        item.target = nil
        menu.addItem(item)
    }

    private func showFailure(_ text: String) {
        let alert = NSAlert()
        alert.messageText = "OptionHelper无法启动"
        alert.informativeText = text
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }
}
