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

private final class OptionHelperReportDownloadDelegate: NSObject, URLSessionTaskDelegate {
    private let allowsURL: (URL?) -> Bool

    init(allowsURL: @escaping (URL?) -> Bool) {
        self.allowsURL = allowsURL
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(allowsURL(request.url) ? request : nil)
    }
}

@main
final class OptionHelperApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private enum PresentationDecision {
        case retry
        case ready
        case failed
    }

    private let presentationRetryInterval: TimeInterval = 0.15
    private let presentationMaxAttempts = 28
    private let presentationReadyTimeout: TimeInterval = 4.05
    private let uiScaleSteps: [CGFloat] = [0.8, 0.9, 1.0, 1.1, 1.25, 1.4]
    private let initializationToken = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    private var backend: Process?
    private var startupPipe: Pipe?
    private var window: NSWindow?
    private var webView: WKWebView?
    private var reportPreviewWindow: NSWindow?
    private var reportPreviewWebView: WKWebView?
    private var settingsWindow: NSWindow?
    private var settingsWebView: WKWebView?
    private var settingsRecoveryView: NSView?
    private var settingsRecoveryDetail: NSTextField?
    private var settingsNavigationWatchdog: DispatchWorkItem?
    private var settingsNavigationGeneration = 0
    private var appOrigin: URL?
    private var lastSafeURL: URL?
    private var navigationFailureVisible = false
    private var navigationRecoveryView: NSView?
    private var navigationRecoveryDetail: NSTextField?
    private var navigationWatchdog: DispatchWorkItem?
    private var navigationGeneration = 0
    private var titlebarDragView: OptionHelperTitlebarDragView?
    private var railToggle: NSButton?
    private var reportToggle: NSButton?
    private var loadedURL = false
    private var themePreference = "light"
    private var activeOperationCount = 0
    private var activeOperationState = "recovering"
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
        cancelNavigationWatchdog()
        cancelSettingsNavigationWatchdog()
        startupPipe?.fileHandleForReading.readabilityHandler = nil
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperTheme")
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperRuntime")
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperSession")
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperUIScale")
        settingsWebView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperTheme")
        settingsWebView?.configuration.userContentController.removeScriptMessageHandler(forName: "optionhelperUIScale")
        NSApp.removeObserver(self, forKeyPath: #keyPath(NSApplication.effectiveAppearance))
        if let backend, backend.isRunning {
            backend.terminate()
        }
        reportPreviewWindow?.orderOut(nil)
        settingsWindow?.orderOut(nil)
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
        var arguments = [
            "--host", "127.0.0.1", "--port", "0", "--data-dir", support.path,
            "--resource-dir", resources.path,
            "--initialization-token", initializationToken,
        ]
        if environment["OPTIONHELPER_VERIFICATION_FIXTURE"] == "1" {
            arguments.append("--verification-fixture")
        }
        process.arguments = arguments
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
        var originComponents = URLComponents()
        originComponents.scheme = url.scheme
        originComponents.host = url.host
        originComponents.port = url.port
        appOrigin = originComponents.url
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        // Show the splash before the login page's module graph is evaluated.
        // The login module owns the timer and always removes this class again.
        let scaleValue = javascriptNumber(uiScale)
        let titlebarHeight = javascriptNumber(36 / uiScale)
        let collapsedSafeArea = javascriptNumber(166 / uiScale)
        let startupScript = WKUserScript(
            source: "document.documentElement.dataset.nativeShell='macos';document.documentElement.dataset.nativeMaterial='system';document.documentElement.dataset.uiScale='\(scaleValue)';document.documentElement.style.setProperty('--native-titlebar-height','\(titlebarHeight)px');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','\(collapsedSafeArea)px');if(location.pathname==='/'){window.__optionhelperInitializationToken='\(initializationToken)';document.documentElement.classList.add('login-boot');}",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        let runtimeStatusScript = WKUserScript(
            source: "(()=>{const post=(state,count)=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'active_operations',state,...(Number.isFinite(count)?{count}: {})});const refresh=()=>fetch('/api/runtime/active-operations').then((response)=>{if(!response.ok)throw new Error('runtime_status_unavailable');return response.json();}).then((value)=>{const count=value?.active_count;if(!Number.isInteger(count)||count<0)throw new Error('runtime_status_invalid');post('ready',count);}).catch(()=>post('recovering'));addEventListener('pageshow',refresh);setInterval(refresh,1400);refresh();})();",
            injectionTime: .atDocumentEnd,
            forMainFrameOnly: true
        )
        configuration.userContentController.addUserScript(startupScript)
        configuration.userContentController.addUserScript(runtimeStatusScript)
        configuration.userContentController.add(self, name: "optionhelperTheme")
        configuration.userContentController.add(self, name: "optionhelperRuntime")
        configuration.userContentController.add(self, name: "optionhelperSession")
        configuration.userContentController.add(self, name: "optionhelperUIScale")
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsMagnification = false
        view.pageZoom = uiScale
        view.navigationDelegate = self
        view.uiDelegate = self
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
        lastSafeURL = startupURL
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
        let reportLabel = route == "optchat" ? "打开或收起任务与交付" : "打开或收起报告库"
        railToggle?.isHidden = !showsWorkspaceControls
        reportToggle?.isHidden = !showsWorkspaceControls
        reportToggle?.toolTip = reportLabel
        reportToggle?.setAccessibilityLabel(reportLabel)
        reportToggle?.image = NSImage(systemSymbolName: "sidebar.right", accessibilityDescription: reportLabel)
        layoutTitlebarControls()
    }

    private func isSafeAppURL(_ url: URL?) -> Bool {
        guard let url, let appOrigin,
              url.scheme == appOrigin.scheme,
              url.host == appOrigin.host,
              url.port == appOrigin.port else { return false }
        return ["/", "/optchat", "/optdesk", "/settings"].contains(url.path)
    }

    private func ensureNavigationRecoveryView() -> NSView? {
        if let navigationRecoveryView { return navigationRecoveryView }
        guard let contentView = window?.contentView else { return nil }

        let overlay = NSView(frame: .zero)
        overlay.translatesAutoresizingMaskIntoConstraints = false
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        overlay.setAccessibilityLabel("OptionHelper页面恢复")

        let title = NSTextField(labelWithString: "页面暂时无法显示")
        title.font = .systemFont(ofSize: 24, weight: .semibold)
        title.alignment = .left

        let detail = NSTextField(wrappingLabelWithString: "")
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        detail.maximumNumberOfLines = 0
        navigationRecoveryDetail = detail

        let retry = NSButton(title: "重新打开上一个页面", target: self, action: #selector(recoverLastSafePage(_:)))
        retry.bezelStyle = .rounded
        retry.keyEquivalent = "\r"
        let workspace = NSButton(title: "返回工作台", target: self, action: #selector(recoverWorkspace(_:)))
        workspace.bezelStyle = .rounded
        let actions = NSStackView(views: [retry, workspace])
        actions.orientation = .horizontal
        actions.alignment = .centerY
        actions.spacing = 10

        let panel = NSStackView(views: [title, detail, actions])
        panel.translatesAutoresizingMaskIntoConstraints = false
        panel.orientation = .vertical
        panel.alignment = .leading
        panel.spacing = 14
        overlay.addSubview(panel)
        contentView.addSubview(overlay, positioned: .above, relativeTo: webView)
        NSLayoutConstraint.activate([
            overlay.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            overlay.topAnchor.constraint(equalTo: contentView.topAnchor),
            overlay.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            panel.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            panel.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
            panel.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            panel.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor, constant: 24),
            panel.trailingAnchor.constraint(lessThanOrEqualTo: overlay.trailingAnchor, constant: -24),
        ])
        overlay.isHidden = true
        navigationRecoveryView = overlay
        return overlay
    }

    private func beginNavigationWatchdog() {
        navigationWatchdog?.cancel()
        navigationGeneration += 1
        let watchdog = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.navigationWatchdog = nil
            self.showNavigationFailure("页面载入没有完成。后台任务不会因此取消，可重新打开上一个页面。")
        }
        navigationWatchdog = watchdog
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0, execute: watchdog)
    }

    private func cancelNavigationWatchdog() {
        navigationWatchdog?.cancel()
        navigationWatchdog = nil
    }

    private func hideNavigationFailure() {
        navigationRecoveryView?.isHidden = true
        navigationFailureVisible = false
    }

    private func showNavigationFailure(_ detail: String) {
        guard webView != nil, appOrigin != nil, let recoveryView = ensureNavigationRecoveryView() else { return }
        navigationFailureVisible = true
        navigationRecoveryDetail?.stringValue = detail
        recoveryView.isHidden = false
        syncWorkspaceTitlebarControls(for: nil)
        window?.makeFirstResponder(recoveryView)
    }

    private func loadRecoveryURL(_ url: URL?) {
        guard let url, isSafeAppURL(url), let webView else { return }
        navigationRecoveryDetail?.stringValue = "正在重新打开页面…"
        beginNavigationWatchdog()
        webView.load(URLRequest(url: url))
    }

    @objc private func recoverLastSafePage(_ sender: Any?) {
        loadRecoveryURL(lastSafeURL ?? appOrigin)
    }

    @objc private func recoverWorkspace(_ sender: Any?) {
        loadRecoveryURL(appOrigin?.appendingPathComponent("optchat"))
    }

    private var visibleRootHealthScript: String {
        "(()=>{const path=location.pathname;const selector=path==='/settings'?'.setup-card':(['/optchat','/optdesk'].includes(path)?'[data-workspace-shell]':(path==='/'?'body.login-page':'main'));const root=document.querySelector(selector);if(!root)return {ready:false,selector,reason:'missing'};const style=getComputedStyle(root);const rect=root.getBoundingClientRect();const visible=style.display!=='none'&&style.visibility!=='hidden'&&style.visibility!=='collapse'&&Number(style.opacity)>0.01&&root.getClientRects().length>0&&rect.width>80&&rect.height>80&&rect.bottom>0&&rect.right>0&&rect.top<innerHeight&&rect.left<innerWidth;return {ready:['interactive','complete'].includes(document.readyState),textLength:((root.innerText||root.textContent)||'').trim().length,width:rect.width,height:rect.height,visible,selector};})()"
    }

    private func presentationDecision(
        contentHealthy: Bool,
        snapshotReady: Bool,
        attempt: Int,
        withinDeadline: Bool
    ) -> PresentationDecision {
        if contentHealthy && snapshotReady { return .ready }
        return withinDeadline && attempt + 1 < presentationMaxAttempts ? .retry : .failed
    }

    private func scheduleMainPresentationRetry(generation: Int, attempt: Int, deadline: TimeInterval) {
        DispatchQueue.main.asyncAfter(deadline: .now() + presentationRetryInterval) { [weak self] in
            guard let self, generation == self.navigationGeneration else { return }
            self.verifyMainPresentation(generation: generation, attempt: attempt, deadline: deadline)
        }
    }

    private func failMainPresentation(generation: Int, message: String) {
        guard generation == navigationGeneration else { return }
        cancelNavigationWatchdog()
        showNavigationFailure(message)
    }

    private func completeMainPresentation(webView: WKWebView, generation: Int) {
        guard generation == navigationGeneration, webView === self.webView else { return }
        cancelNavigationWatchdog()
        if isSafeAppURL(webView.url) { lastSafeURL = webView.url }
        hideNavigationFailure()
        window?.makeFirstResponder(webView)
    }

    private func verifyMainPresentation(generation: Int, attempt: Int, deadline: TimeInterval) {
        guard let webView, generation == navigationGeneration else { return }
        webView.evaluateJavaScript(visibleRootHealthScript) { [weak self, weak webView] result, error in
            guard let self, let webView, webView === self.webView,
                  generation == self.navigationGeneration else { return }
            let health = result as? [String: Any]
            let healthy = error == nil
                && health?["ready"] as? Bool == true
                && health?["visible"] as? Bool == true
                && ((health?["textLength"] as? NSNumber)?.intValue ?? 0) > 0
                && ((health?["width"] as? NSNumber)?.doubleValue ?? 0) > 80
                && ((health?["height"] as? NSNumber)?.doubleValue ?? 0) > 80
            guard healthy else {
                let withinDeadline = ProcessInfo.processInfo.systemUptime < deadline
                switch self.presentationDecision(
                    contentHealthy: false,
                    snapshotReady: false,
                    attempt: attempt,
                    withinDeadline: withinDeadline
                ) {
                case .retry:
                    self.scheduleMainPresentationRetry(generation: generation, attempt: attempt + 1, deadline: deadline)
                case .failed:
                    self.failMainPresentation(
                        generation: generation,
                        message: "页面内容没有正确呈现。后台任务不会因此取消，可重新打开上一个页面。"
                    )
                case .ready:
                    break
                }
                return
            }
            let configuration = WKSnapshotConfiguration()
            configuration.rect = webView.bounds
            configuration.snapshotWidth = 480
            webView.takeSnapshot(with: configuration) { [weak self, weak webView] snapshot, snapshotError in
                guard let self, let webView, webView === self.webView,
                      generation == self.navigationGeneration else { return }
                let snapshotReady = snapshotError == nil
                    && (snapshot?.size.width ?? 0) > 1
                    && (snapshot?.size.height ?? 0) > 1
                let withinDeadline = ProcessInfo.processInfo.systemUptime < deadline
                switch self.presentationDecision(
                    contentHealthy: true,
                    snapshotReady: snapshotReady,
                    attempt: attempt,
                    withinDeadline: withinDeadline
                ) {
                case .ready:
                    self.completeMainPresentation(webView: webView, generation: generation)
                case .retry:
                    self.scheduleMainPresentationRetry(generation: generation, attempt: attempt + 1, deadline: deadline)
                case .failed:
                    self.failMainPresentation(
                        generation: generation,
                        message: "页面没有生成可见画面。后台任务不会因此取消，可重新打开上一个页面。"
                    )
                }
            }
        }
    }

    private func ensureSettingsRecoveryView(contentView: NSView, relativeTo webView: WKWebView) -> NSView {
        if let settingsRecoveryView { return settingsRecoveryView }
        let overlay = NSView(frame: .zero)
        overlay.translatesAutoresizingMaskIntoConstraints = false
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        overlay.setAccessibilityLabel("OptionHelper设置中心状态")
        let title = NSTextField(labelWithString: "正在打开设置中心")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        let detail = NSTextField(wrappingLabelWithString: "正在确认设置页面已完整呈现。")
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        detail.maximumNumberOfLines = 0
        settingsRecoveryDetail = detail
        let retry = NSButton(title: "重新打开设置中心", target: self, action: #selector(retrySettingsCenter(_:)))
        retry.bezelStyle = .rounded
        let close = NSButton(title: "返回工作台", target: self, action: #selector(closeSettingsCenter(_:)))
        close.bezelStyle = .rounded
        let actions = NSStackView(views: [retry, close])
        actions.orientation = .horizontal
        actions.alignment = .centerY
        actions.spacing = 10
        let panel = NSStackView(views: [title, detail, actions])
        panel.translatesAutoresizingMaskIntoConstraints = false
        panel.orientation = .vertical
        panel.alignment = .leading
        panel.spacing = 14
        overlay.addSubview(panel)
        contentView.addSubview(overlay, positioned: .above, relativeTo: webView)
        NSLayoutConstraint.activate([
            overlay.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            overlay.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            overlay.topAnchor.constraint(equalTo: contentView.topAnchor),
            overlay.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            panel.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            panel.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
            panel.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            panel.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor, constant: 24),
            panel.trailingAnchor.constraint(lessThanOrEqualTo: overlay.trailingAnchor, constant: -24),
        ])
        settingsRecoveryView = overlay
        return overlay
    }

    private func beginSettingsNavigationWatchdog() {
        settingsNavigationWatchdog?.cancel()
        settingsNavigationGeneration += 1
        let generation = settingsNavigationGeneration
        let watchdog = DispatchWorkItem { [weak self] in
            guard let self, generation == self.settingsNavigationGeneration else { return }
            self.showSettingsFailure("设置页面载入没有完成。工作台和后台任务仍保持原状。")
        }
        settingsNavigationWatchdog = watchdog
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: watchdog)
    }

    private func cancelSettingsNavigationWatchdog() {
        settingsNavigationWatchdog?.cancel()
        settingsNavigationWatchdog = nil
        settingsNavigationGeneration += 1
    }

    private func showSettingsFailure(_ detail: String) {
        settingsRecoveryDetail?.stringValue = detail
        settingsRecoveryView?.isHidden = false
        if let settingsRecoveryView { settingsWindow?.makeFirstResponder(settingsRecoveryView) }
    }

    private func hideSettingsFailure() {
        settingsRecoveryView?.isHidden = true
        if let settingsWebView { settingsWindow?.makeFirstResponder(settingsWebView) }
    }

    private func showSettingsCenter(_ request: URLRequest) {
        guard let url = request.url, isSafeAppURL(url), url.path == "/settings", let webView else { return }
        if settingsWindow == nil || settingsWebView == nil {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = webView.configuration.websiteDataStore
            configuration.userContentController.add(self, name: "optionhelperTheme")
            configuration.userContentController.add(self, name: "optionhelperUIScale")
            let settingsView = WKWebView(frame: .zero, configuration: configuration)
            settingsView.translatesAutoresizingMaskIntoConstraints = false
            settingsView.navigationDelegate = self
            settingsView.uiDelegate = self
            settingsView.pageZoom = uiScale
            settingsView.underPageBackgroundColor = .windowBackgroundColor
            settingsView.wantsLayer = true
            settingsView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            let contentView = NSView(frame: .zero)
            contentView.wantsLayer = true
            contentView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
            contentView.addSubview(settingsView)
            NSLayoutConstraint.activate([
                settingsView.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
                settingsView.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
                settingsView.topAnchor.constraint(equalTo: contentView.topAnchor),
                settingsView.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            ])
            let settingsWindow = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 960, height: 760),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            settingsWindow.title = "OptionHelper设置中心"
            settingsWindow.backgroundColor = .windowBackgroundColor
            settingsWindow.isOpaque = true
            settingsWindow.delegate = self
            settingsWindow.contentView = contentView
            settingsWindow.center()
            self.settingsWindow = settingsWindow
            settingsWebView = settingsView
            _ = ensureSettingsRecoveryView(contentView: contentView, relativeTo: settingsView)
        }
        settingsRecoveryDetail?.stringValue = "正在确认设置页面已完整呈现。"
        settingsRecoveryView?.isHidden = false
        settingsWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        beginSettingsNavigationWatchdog()
        settingsWebView?.load(request)
    }

    @objc private func retrySettingsCenter(_ sender: Any?) {
        let fallback = appOrigin?.appendingPathComponent("settings")
        let current = settingsWebView?.url.flatMap { isSafeAppURL($0) && $0.path == "/settings" ? $0 : nil }
        guard let url = current ?? fallback else { return }
        showSettingsCenter(URLRequest(url: url))
    }

    @objc private func closeSettingsCenter(_ sender: Any?) {
        cancelSettingsNavigationWatchdog()
        settingsWindow?.orderOut(nil)
        window?.makeKeyAndOrderFront(nil)
        if let webView { window?.makeFirstResponder(webView) }
        NSApp.activate(ignoringOtherApps: true)
    }

    private func verifySettingsPresentation(generation: Int) {
        guard let settingsWebView else { return }
        settingsWebView.evaluateJavaScript(visibleRootHealthScript) { [weak self, weak settingsWebView] result, error in
            guard let self, let settingsWebView, settingsWebView === self.settingsWebView,
                  generation == self.settingsNavigationGeneration else { return }
            let health = result as? [String: Any]
            let healthy = error == nil
                && health?["ready"] as? Bool == true
                && health?["visible"] as? Bool == true
                && ((health?["textLength"] as? NSNumber)?.intValue ?? 0) > 0
                && ((health?["width"] as? NSNumber)?.doubleValue ?? 0) > 80
                && ((health?["height"] as? NSNumber)?.doubleValue ?? 0) > 80
            guard healthy else {
                self.cancelSettingsNavigationWatchdog()
                self.showSettingsFailure("设置页面结构已载入，但可见内容没有正确呈现。")
                return
            }
            let configuration = WKSnapshotConfiguration()
            configuration.rect = settingsWebView.bounds
            configuration.snapshotWidth = 480
            settingsWebView.takeSnapshot(with: configuration) { [weak self, weak settingsWebView] snapshot, snapshotError in
                guard let self, let settingsWebView, settingsWebView === self.settingsWebView,
                      generation == self.settingsNavigationGeneration else { return }
                self.cancelSettingsNavigationWatchdog()
                guard snapshotError == nil, let snapshot, snapshot.size.width > 1, snapshot.size.height > 1 else {
                    self.showSettingsFailure("设置页面没有生成可见画面。工作台仍保持原状，可重试或返回。")
                    return
                }
                self.hideSettingsFailure()
            }
        }
    }

    private func isCancelledNavigationError(_ error: Error) -> Bool {
        let value = error as NSError
        return value.domain == NSURLErrorDomain && value.code == NSURLErrorCancelled
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        if webView === settingsWebView {
            beginSettingsNavigationWatchdog()
            return
        }
        guard webView === self.webView else { return }
        beginNavigationWatchdog()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView === settingsWebView {
            verifySettingsPresentation(generation: settingsNavigationGeneration)
            return
        }
        guard webView === self.webView else { return }
        syncUIScaleToPage()
        if let window { ensureTitlebarControls(in: window) }
        syncWorkspaceTitlebarControls(for: webView.url)
        cancelNavigationWatchdog()
        let navigationToken = navigationGeneration
        let deadline = ProcessInfo.processInfo.systemUptime + presentationReadyTimeout
        verifyMainPresentation(generation: navigationToken, attempt: 0, deadline: deadline)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        guard webView === self.webView else { return }
        syncUIScaleToPage()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if webView === settingsWebView, !isCancelledNavigationError(error) {
            cancelSettingsNavigationWatchdog()
            showSettingsFailure("设置页面载入失败。工作台和后台任务仍保持原状。")
            return
        }
        guard webView === self.webView, !isCancelledNavigationError(error) else { return }
        cancelNavigationWatchdog()
        showNavigationFailure("页面载入失败。后台任务不会因此取消，可重新打开上一个页面。")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if webView === settingsWebView, !isCancelledNavigationError(error) {
            cancelSettingsNavigationWatchdog()
            showSettingsFailure("设置页面连接未能建立。请确认App Host仍在运行后重试。")
            return
        }
        guard webView === self.webView, !isCancelledNavigationError(error) else { return }
        cancelNavigationWatchdog()
        showNavigationFailure("页面连接未能建立。请确认App Host仍在运行后重试。")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if webView === settingsWebView {
            cancelSettingsNavigationWatchdog()
            showSettingsFailure("设置页面进程已结束。工作台和后台任务仍保持原状。")
            return
        }
        guard webView === self.webView else { return }
        cancelNavigationWatchdog()
        showNavigationFailure("页面进程已结束。任务和后台计算仍保存在App Host中。")
    }

    private func isAllowedReportArtifactURL(_ url: URL?) -> Bool {
        guard let url, let appOrigin,
              url.scheme == appOrigin.scheme,
              url.host == appOrigin.host,
              url.port == appOrigin.port,
              url.path.hasPrefix("/api/reports/"),
              url.path.contains("/artifacts/") else { return false }
        guard let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return true }
        return queryItems.allSatisfy { $0.name == "download" && $0.value == "1" }
    }

    private func isReportDownloadURL(_ url: URL?) -> Bool {
        guard let url else { return false }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains {
            $0.name == "download" && $0.value == "1"
        } == true
    }

    private func showReportDownloadFailure(_ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "报告下载未完成"
        alert.informativeText = detail
        if let window { alert.beginSheetModal(for: window) }
        else { alert.runModal() }
    }

    private func reportDownloadFilename(_ url: URL) -> String {
        let raw = url.lastPathComponent.removingPercentEncoding ?? url.lastPathComponent
        let lower = raw.lowercased()
        let suffix = lower.hasSuffix(".pdf") ? ".pdf" : lower.hasSuffix(".html") ? ".html" : ""
        if let fragment = url.fragment,
           let components = URLComponents(string: "https://optionhelper.invalid/?\(fragment)"),
           let publicName = components.queryItems?.first(where: { $0.name == "display_name" })?.value {
            let cleaned = publicName
                .components(separatedBy: .newlines).joined(separator: " ")
                .replacingOccurrences(of: "/", with: "-")
                .replacingOccurrences(of: "\\", with: "-")
                .replacingOccurrences(of: ":", with: "-")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !cleaned.isEmpty, cleaned.count <= 160 {
                let publicLower = cleaned.lowercased()
                return suffix.isEmpty || publicLower.hasSuffix(suffix) ? cleaned : "\(cleaned)\(suffix)"
            }
        }
        if lower.contains("multicard") { return "多结构研究简报\(suffix)" }
        if lower.contains("multireport") { return "多结构完整研究报告\(suffix)" }
        return raw
    }

    private func cookie(_ cookie: HTTPCookie, appliesTo url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        let domain = cookie.domain.lowercased()
        let domainMatches: Bool
        if domain.hasPrefix(".") {
            let suffix = String(domain.dropFirst())
            domainMatches = host == suffix || host.hasSuffix(".\(suffix)")
        } else {
            domainMatches = host == domain
        }
        let path = cookie.path.isEmpty ? "/" : cookie.path
        let pathMatches = url.path == path
            || (url.path.hasPrefix(path) && (path.hasSuffix("/") || url.path.dropFirst(path.count).first == "/"))
        let schemeAllowsCookie = !cookie.isSecure || url.scheme?.lowercased() == "https"
        return domainMatches && pathMatches && schemeAllowsCookie
    }

    private func downloadReportArtifact(_ request: URLRequest) {
        guard let url = request.url, let webView, let window else { return }
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = reportDownloadFilename(url)
        panel.beginSheetModal(for: window) { [weak self, weak webView] response in
            guard response == .OK, let destination = panel.url, let self, let webView else { return }
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                var authorized = request
                if var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
                    components.fragment = nil
                    authorized.url = components.url
                }
                let applicableCookies = cookies.filter { cookie in
                    authorized.url.map { self.cookie(cookie, appliesTo: $0) } ?? false
                }
                for (name, value) in HTTPCookie.requestHeaderFields(with: applicableCookies) {
                    authorized.setValue(value, forHTTPHeaderField: name)
                }
                let sessionConfiguration = URLSessionConfiguration.ephemeral
                sessionConfiguration.httpShouldSetCookies = false
                let sessionDelegate = OptionHelperReportDownloadDelegate { [weak self] redirectedURL in
                    self?.isAllowedReportArtifactURL(redirectedURL) == true
                }
                let session = URLSession(configuration: sessionConfiguration, delegate: sessionDelegate, delegateQueue: nil)
                session.downloadTask(with: authorized) { temporaryURL, response, error in
                    defer { session.finishTasksAndInvalidate() }
                    let httpResponse = response as? HTTPURLResponse
                    let mimeType = response?.mimeType?.lowercased() ?? ""
                    let allowedMIMETypes = Set(["text/html", "application/xhtml+xml", "application/pdf"])
                    let fileSize = temporaryURL.flatMap {
                        try? $0.resourceValues(forKeys: [.fileSizeKey]).fileSize
                    } ?? 0
                    let validationError: String? = {
                        if let error { return error.localizedDescription }
                        guard let httpResponse else { return "报告服务未返回HTTP响应。" }
                        guard self.isAllowedReportArtifactURL(httpResponse.url) else {
                            return "报告服务跳转到了不受信任的位置。"
                        }
                        guard (200..<300).contains(httpResponse.statusCode) else {
                            return "报告服务返回HTTP \(httpResponse.statusCode)。"
                        }
                        guard allowedMIMETypes.contains(mimeType) else {
                            return "报告服务返回了不受支持的文件类型。"
                        }
                        guard temporaryURL != nil, fileSize > 0 else { return "报告文件为空。" }
                        return nil
                    }()
                    DispatchQueue.main.async {
                        guard validationError == nil, let temporaryURL else {
                            self.showReportDownloadFailure(validationError ?? "未收到报告文件。")
                            return
                        }
                        do {
                            if FileManager.default.fileExists(atPath: destination.path) {
                                try FileManager.default.removeItem(at: destination)
                            }
                            try FileManager.default.copyItem(at: temporaryURL, to: destination)
                        } catch {
                            self.showReportDownloadFailure(error.localizedDescription)
                        }
                    }
                }.resume()
            }
        }
    }

    private func showReportPreview(_ request: URLRequest, configuration: WKWebViewConfiguration) {
        let preview: WKWebView
        let previewWindow: NSWindow
        if let existingView = reportPreviewWebView, let existingWindow = reportPreviewWindow {
            preview = existingView
            previewWindow = existingWindow
        } else {
            let isolatedConfiguration = WKWebViewConfiguration()
            isolatedConfiguration.websiteDataStore = configuration.websiteDataStore
            isolatedConfiguration.preferences.javaScriptCanOpenWindowsAutomatically = false
            preview = WKWebView(frame: .zero, configuration: isolatedConfiguration)
            preview.navigationDelegate = self
            preview.uiDelegate = self
            preview.allowsMagnification = true
            previewWindow = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 980, height: 760),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            previewWindow.title = "OptionHelper报告预览"
            previewWindow.delegate = self
            previewWindow.contentView = preview
            previewWindow.center()
            reportPreviewWebView = preview
            reportPreviewWindow = previewWindow
        }
        preview.load(request)
        previewWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard navigationAction.targetFrame == nil else { return nil }
        guard isAllowedReportArtifactURL(navigationAction.request.url) else {
            NSSound.beep()
            return nil
        }
        if isReportDownloadURL(navigationAction.request.url) {
            downloadReportArtifact(navigationAction.request)
            return nil
        }
        showReportPreview(navigationAction.request, configuration: configuration)
        return nil
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        guard webView === self.webView else {
            completionHandler(nil)
            return
        }
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        let finish: (NSApplication.ModalResponse) -> Void = { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
        if let hostWindow = webView.window ?? window {
            panel.beginSheetModal(for: hostWindow, completionHandler: finish)
        } else {
            panel.begin(completionHandler: finish)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        let url = navigationAction.request.url
        if webView === self.webView, isSafeAppURL(url), url?.path == "/settings" {
            showSettingsCenter(navigationAction.request)
            decisionHandler(.cancel)
            return
        }
        if webView === settingsWebView {
            if isSafeAppURL(url), url?.path == "/settings" {
                decisionHandler(.allow)
            } else if isSafeAppURL(url) {
                closeSettingsCenter(nil)
                decisionHandler(.cancel)
            } else {
                decisionHandler(.cancel)
            }
            return
        }
        guard webView === reportPreviewWebView else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(url?.scheme == "about" || isAllowedReportArtifactURL(url) ? .allow : .cancel)
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
        if sender === settingsWindow {
            closeSettingsCenter(nil)
            return false
        }
        if sender === reportPreviewWindow {
            sender.orderOut(nil)
            return false
        }
        guard sender === window else { return true }
        if closeAfterInterrupt || (activeOperationState == "ready" && activeOperationCount == 0) { return true }
        let alert = NSAlert()
        alert.messageText = activeOperationState == "ready" ? "仍有任务正在运行" : "正在确认后台任务状态"
        alert.informativeText = activeOperationState == "ready"
            ? "退出会停止这些运行，并将它们标记为中断。"
            : "当前无法确认活动Operation数量。退出仍会请求停止后台运行。"
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
        if message.name == "optionhelperSession", let value = message.body as? [String: Any],
           value["type"] as? String == "signed_out" {
            clearAuxiliaryWindowsAfterSignOut()
            return
        }
        if message.name == "optionhelperUIScale", let value = message.body as? [String: Any],
           value["type"] as? String == "set_ui_scale", let scale = value["scale"] as? NSNumber,
           let accepted = validatedUIScale(scale.doubleValue) {
            applyUIScale(accepted, persist: true, notifyPage: true)
            return
        }
        if message.name == "optionhelperRuntime", let value = message.body as? [String: Any] {
            if value["type"] as? String == "active_operations" {
                let state = value["state"] as? String
                if let count = value["count"] as? Int, count >= 0, state == "ready" {
                    activeOperationCount = max(0, count)
                    activeOperationState = "ready"
                } else {
                    activeOperationState = "recovering"
                }
            } else if value["type"] as? String == "shutdown_complete" {
                activeOperationCount = 0
                activeOperationState = "ready"
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
            settingsWindow?.appearance = nil
            let appearance = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
            applyDockIcon(theme: appearance == .darkAqua ? "dark" : "light")
            DispatchQueue.main.async { [weak self] in
                self?.webView?.evaluateJavaScript("window.OptionHelperTheme?.refreshSystemTheme?.();", completionHandler: nil)
            }
        } else {
            window?.appearance = NSAppearance(named: theme == "dark" ? .darkAqua : .aqua)
            settingsWindow?.appearance = window?.appearance
            applyDockIcon(theme: theme)
        }
    }

    private func clearAuxiliaryWindowsAfterSignOut() {
        cancelSettingsNavigationWatchdog()
        settingsWebView?.stopLoading()
        settingsWebView?.loadHTMLString("", baseURL: nil)
        settingsWindow?.orderOut(nil)
        reportPreviewWebView?.stopLoading()
        reportPreviewWebView?.loadHTMLString("", baseURL: nil)
        reportPreviewWindow?.orderOut(nil)
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
        settingsWebView?.pageZoom = scale
        if persist { persistUIScale() }
        updateScaleMenuAvailability()
        if notifyPage { syncUIScaleToPage() }
    }

    private func syncUIScaleToPage() {
        guard webView != nil || settingsWebView != nil else { return }
        let value = javascriptNumber(uiScale)
        let titlebarHeight = javascriptNumber(36 / uiScale)
        let collapsedSafeArea = javascriptNumber(166 / uiScale)
        let script = "document.documentElement.dataset.uiScale='\(value)';document.documentElement.style.setProperty('--native-titlebar-height','\(titlebarHeight)px');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','\(collapsedSafeArea)px');window.OptionHelperUIScale?.syncFromNative?.(\(value));"
        webView?.evaluateJavaScript(script, completionHandler: nil)
        settingsWebView?.evaluateJavaScript(script, completionHandler: nil)
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
