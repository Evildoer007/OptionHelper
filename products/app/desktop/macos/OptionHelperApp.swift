import Cocoa
import WebKit

// Cocoa and WebKit resolve edit actions through the responder chain.
// Declare the shared Objective-C selector contract without assuming these
// methods are exposed as static members of NSResponder by the current SDK.
@objc private protocol OptionHelperEditingActions {
    func undo(_ sender: Any?)
    func redo(_ sender: Any?)
    func cut(_ sender: Any?)
    func copy(_ sender: Any?)
    func paste(_ sender: Any?)
    func selectAll(_ sender: Any?)
}

private final class OptionHelperWindow: NSWindow {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard modifiers == .command,
              let key = event.charactersIgnoringModifiers?.lowercased(),
              let action = switch key {
              case "x": #selector(OptionHelperEditingActions.cut(_:))
              case "c": #selector(OptionHelperEditingActions.copy(_:))
              case "v": #selector(OptionHelperEditingActions.paste(_:))
              case "a": #selector(OptionHelperEditingActions.selectAll(_:))
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

// A native sheet owns a WebKit reply until a choice or lifecycle cancellation.
private final class OptionHelperNativeDialog {
    weak var sourceView: WKWebView?
    let sheetWindow: NSWindow
    var completion: ((Bool) -> Void)?
    private var finished = false

    init(sourceView: WKWebView, sheetWindow: NSWindow) {
        self.sourceView = sourceView
        self.sheetWindow = sheetWindow
    }

    func finish(_ accepted: Bool) {
        guard !finished else { return }
        finished = true
        let callback = completion
        completion = nil
        callback?(accepted)
    }

    func cancel() {
        guard !finished else { return }
        finish(false)
        if let parent = sheetWindow.sheetParent {
            parent.endSheet(sheetWindow, returnCode: .cancel)
        }
    }
}

private enum OptionHelperNativeDownloadKind {
    case none, attachment, data, payoff, blob
}

// One download owns its picker reply and temporary file until it finishes.
private final class OptionHelperNativeDownload {
    weak var sourceView: WKWebView?
    let download: WKDownload
    let sourceURL: URL
    let requestURL: URL
    let kind: OptionHelperNativeDownloadKind
    var destinationDialog: OptionHelperNativeDialog?
    var destinationURL: URL?
    var stagingURL: URL?
    var finished = false

    init(sourceView: WKWebView, download: WKDownload, sourceURL: URL, requestURL: URL, kind: OptionHelperNativeDownloadKind) {
        self.sourceView = sourceView
        self.download = download
        self.sourceURL = sourceURL
        self.requestURL = requestURL
        self.kind = kind
    }
}

@main
final class OptionHelperApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate, NSMenuItemValidation {
    private enum PresentationDecision {
        case retry
        case ready
        case failed
    }

    private let presentationRetryInterval: TimeInterval = 0.15
    private let presentationMaxAttempts = 28
    private let presentationReadyTimeout: TimeInterval = 4.05
    private let uiScaleSteps: [CGFloat] = [0.8, 0.9, 1.0, 1.1, 1.25, 1.4]
    private var backend: Process?
    private var startupPipe: Pipe?
    private var window: NSWindow?
    private var nativeDialogs: [UUID: OptionHelperNativeDialog] = [:]
    private var nativeDownloads: [ObjectIdentifier: OptionHelperNativeDownload] = [:]
    private var webView: WKWebView?
    private var reportPreviewWindow: NSWindow?
    private var reportPreviewWebView: WKWebView?
    private var reportWindowIsEditor = false
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
    private var taskSearch: NSButton?
    private var reportToggle: NSButton?
    private var loadedURL = false
    private var themePreference = "light"
    private var sidebarMaterialView: NSVisualEffectView?
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
        cancelNativeDialogs()
        cancelNativeDownloads()
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
            showFailure("找不到内置应用服务")
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
                    self?.showFailure("应用服务未能启动")
                }
            }
        }
        do {
            try process.run()
            backend = process
            startupPipe = pipe
        } catch {
            showFailure("无法启动内置应用服务")
        }
    }

    private func consumeBackendOutput(_ output: String) {
        guard !loadedURL else { return }
        for line in output.split(whereSeparator: \.isNewline) where line.hasPrefix("OPTIONHELPER_URL=") {
            let address = String(line.dropFirst("OPTIONHELPER_URL=".count))
            guard let url = URL(string: address), url.host == "127.0.0.1" else {
                showFailure("应用服务返回了无效地址")
                return
            }
            loadedURL = true
            showWebView(url: url)
            return
        }
    }

    private func showWebView(url: URL) {
        guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            showFailure("应用服务返回了无效启动地址")
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
            showFailure("应用服务返回了无效启动地址")
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
            source: "document.documentElement.dataset.nativeShell='macos';document.documentElement.dataset.nativeMaterial='system';document.documentElement.dataset.uiScale='\(scaleValue)';document.documentElement.style.setProperty('--native-titlebar-height','\(titlebarHeight)px');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','\(collapsedSafeArea)px');if(location.pathname==='/'){document.documentElement.classList.add('login-boot');}",
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        let runtimeStatusScript = WKUserScript(
            source: "(()=>{const post=(state,count)=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'active_operations',state,...(Number.isFinite(count)?{count}: {})});let inFlight=false;const refresh=()=>{if(inFlight)return;inFlight=true;return fetch('/api/runtime/active-operations').then((response)=>{if(!response.ok)throw new Error('runtime_status_unavailable');return response.json();}).then((value)=>{const count=value?.active_count;if(!Number.isInteger(count)||count<0)throw new Error('runtime_status_invalid');post('ready',count);}).catch(()=>post('recovering')).finally(()=>{inFlight=false;});};addEventListener('pageshow',refresh);setInterval(refresh,1400);refresh();})();",
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
        // Clearing overscroll alone leaves WebKit's opaque page backing over
        // the native sidebar. Guard the macOS SPI for an opaque-safe fallback.
        if view.responds(to: NSSelectorFromString("_setDrawsBackground:")) {
            view.setValue(false, forKey: "drawsBackground")
        }
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor

        let materialView = NSVisualEffectView(frame: .zero)
        // Use one native material recipe with theme-specific appearance.
        sidebarMaterialView = materialView
        applySidebarMaterial(theme: "light")
        materialView.blendingMode = .behindWindow
        // Keep the frosted surface when focus moves to another window. AppKit
        // still honors the system Reduce Transparency setting.
        materialView.state = .active
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
        if taskSearch == nil || taskSearch?.superview !== titlebar {
            taskSearch?.removeFromSuperview()
            let button = NSButton(image: NSImage(systemSymbolName: "magnifyingglass", accessibilityDescription: "搜索任务")!, target: self, action: #selector(searchTasks(_:)))
            button.isBordered = false
            button.contentTintColor = .secondaryLabelColor
            button.toolTip = "搜索任务"
            button.setAccessibilityLabel("搜索任务")
            titlebar.addSubview(button)
            taskSearch = button
        }
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
            x: closeButton.frame.maxX + 60,
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
            x: closeButton.frame.maxX + 60,
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
        taskSearch?.frame = NSRect(x: (railToggle?.frame.maxX ?? closeButton.frame.maxX + 100) + 8, y: closeButton.frame.midY - 12, width: 26, height: 24)
        let dragLeading = (taskSearch?.frame.maxX ?? closeButton.frame.maxX + 134) + 8
        let dragTrailing = (reportToggle?.frame.minX ?? titlebar.bounds.maxX - 40) - 8
        titlebarDragView?.frame = NSRect(
            x: dragLeading,
            y: 0,
            width: max(0, dragTrailing - dragLeading),
            height: titlebar.bounds.height
        )
    }

    @objc private func toggleRail(_ sender: Any?) {
        webView?.evaluateJavaScript("window.dispatchEvent(new Event('optionhelper:toggle-task-rail'))")
    }

    @objc private func searchTasks(_ sender: Any?) {
        webView?.evaluateJavaScript("window.dispatchEvent(new Event('optionhelper:search-tasks'))")
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
        taskSearch?.isHidden = !showsWorkspaceControls
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
        "(()=>{const path=location.pathname;const selector=path==='/settings'?'.setup-card':(['/optchat','/optdesk'].includes(path)?'[data-workspace-shell]':(path==='/'?'body.login-page':'main'));const root=document.querySelector(selector);if(!root)return {ready:false,selector,reason:'missing'};const style=getComputedStyle(root);const rect=root.getBoundingClientRect();const geometry=root.getClientRects().length>0&&rect.width>80&&rect.height>80&&rect.bottom>0&&rect.right>0&&rect.top<innerHeight&&rect.left<innerWidth;const painted=style.display!=='none'&&style.visibility!=='hidden'&&style.visibility!=='collapse'&&Number(style.opacity)>0.01;const staged=root.dataset.initializing==='true'&&geometry;const visible=geometry&&(painted||staged);return {ready:['interactive','complete'].includes(document.readyState),textLength:((root.innerText||root.textContent)||'').trim().length,width:rect.width,height:rect.height,visible,staged,selector};})()"
    }

    private func presentationDecision(
        contentHealthy: Bool,
        snapshotReady: Bool,
        attempt: Int,
        withinDeadline: Bool
    ) -> PresentationDecision {
        if contentHealthy && snapshotReady { return .ready }
        if withinDeadline && attempt + 1 < presentationMaxAttempts { return .retry }
        // WKWebView may reject snapshots for a healthy page when the signed
        // App runs from a read-only DMG. The DOM gate already requires a
        // visible, non-empty workspace with real geometry, so do not leave a
        // usable Desk covered by the recovery overlay at the terminal retry.
        return contentHealthy ? .ready : .failed
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
            // The native recovery overlay may have been shown by the loading
            // watchdog.  It sits above WKWebView, so keeping it visible while
            // requesting a snapshot can turn a healthy page into a permanent
            // false failure.  Reveal the verified DOM before the snapshot;
            // the terminal failure path restores the overlay if needed.
            self.navigationRecoveryView?.isHidden = true
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
        guard let data = try? JSONSerialization.data(withJSONObject: [url.fragment ?? ""]),
              let arguments = String(data: data, encoding: .utf8) else { return }
        let script = "void import('/app/frontend/shared/app.js').then(module => module.openWorkspaceSettings(...\(arguments)));"
        webView.evaluateJavaScript(script, completionHandler: nil)
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
        cancelNativeDialogs(for: webView)
        cancelNativeDownloads(for: webView)
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
            showSettingsFailure("设置页面连接未能建立。请确认应用服务仍在运行后重试。")
            return
        }
        guard webView === self.webView, !isCancelledNavigationError(error) else { return }
        cancelNavigationWatchdog()
        showNavigationFailure("页面连接未能建立。请确认应用服务仍在运行后重试。")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        cancelNativeDialogs(for: webView)
        cancelNativeDownloads(for: webView)
        if webView === settingsWebView {
            cancelSettingsNavigationWatchdog()
            showSettingsFailure("设置页面进程已结束。工作台和后台任务仍保持原状。")
            return
        }
        guard webView === self.webView else { return }
        cancelNavigationWatchdog()
        showNavigationFailure("页面进程已结束。任务和后台计算仍由应用服务保存。")
    }

    private func isPayoffImageDownloadURL(_ url: URL?) -> Bool {
        guard let url, let appOrigin,
              url.scheme == appOrigin.scheme, url.host == appOrigin.host,
              url.port == appOrigin.port, isReportDownloadURL(url) else { return false }
        return url.path.range(of: #"^/api/tasks/[A-Za-z0-9._:-]{1,160}/payoff-images/[A-Za-z0-9._:-]{1,160}\.svg$"#, options: .regularExpression) != nil
    }

    private func isAllowedReportArtifactURL(_ url: URL?) -> Bool {
        guard let url, let appOrigin,
              url.scheme == appOrigin.scheme,
              url.host == appOrigin.host,
              url.port == appOrigin.port,
              url.path.hasPrefix("/api/reports/"),
              (url.path.contains("/artifacts/") || url.path.contains("/document-artifacts/")) else { return false }
        guard let queryItems = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return true }
        return queryItems.allSatisfy { $0.name == "download" && $0.value == "1" }
    }

    private func isAllowedReportEditorURL(_ url: URL?) -> Bool {
        guard let url, let appOrigin,
              url.scheme == appOrigin.scheme, url.host == appOrigin.host,
              url.port == appOrigin.port, url.query == nil || url.query == "" else { return false }
        return url.path.range(of: "^/reports/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/edit$", options: .regularExpression) != nil
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

    private func nativeDownloadKind(for url: URL?) -> OptionHelperNativeDownloadKind {
        guard let url else { return .none }
        if url.scheme?.lowercased() == "blob" {
            return nativeDialogURLIsSameOrigin(URL(string: String(url.absoluteString.dropFirst(5)))) ? .blob : .none
        }
        guard nativeDialogURLIsSameOrigin(url),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return .none }
        let encoded = components.percentEncodedPath.lowercased()
        guard !["%2e", "%2f", "%5c", "%25"].contains(where: encoded.contains),
              !url.pathComponents.contains(where: { $0 == "." || $0 == ".." }),
              url.fragment == nil || url.fragment == "" else { return .none }
        let identifier = "[A-Za-z0-9][A-Za-z0-9._:-]{0,159}"
        if url.query == nil || url.query == "" {
            if url.path.range(of: "^/api/tasks/\(identifier)/attachments/\(identifier)$", options: .regularExpression) != nil { return .attachment }
            if url.path.range(of: "^/api/assets/\(identifier)/download$", options: .regularExpression) != nil { return .data }
        }
        if isPayoffImageDownloadURL(url), components.queryItems?.count == 1 { return .payoff }
        return .none
    }

    private func nativeDownloadFilename(_ suggestedFilename: String, kind: OptionHelperNativeDownloadKind) -> String? {
        let candidate = suggestedFilename.trimmingCharacters(in: .whitespacesAndNewlines)
        let forbidden = CharacterSet.controlCharacters.union(CharacterSet(charactersIn: "/\\:%\u{202a}\u{202b}\u{202c}\u{202d}\u{202e}\u{2066}\u{2067}\u{2068}\u{2069}"))
        guard !candidate.isEmpty, candidate.count <= 160, !candidate.hasPrefix("."),
              candidate.rangeOfCharacter(from: forbidden) == nil else { return nil }
        let fileExtension = (candidate as NSString).pathExtension.lowercased()
        let allowed: Set<String>
        switch kind {
        case .attachment:
            allowed = ["pdf", "docx", "xlsx", "xls", "pptx", "rtf", "odt", "ods", "odp",
                "txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "xml", "yaml", "yml", "log", "eml",
                "html", "htm", "png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff",
                "mov", "mp4", "m4v", "webm", "py", "js", "ts", "jsx", "tsx", "sql", "r", "css", "toml", "ini", "srt", "vtt"]
        case .data: allowed = ["csv", "json"]
        case .payoff: allowed = ["svg"]
        case .blob: allowed = ["csv", "json", "svg"]
        case .none: return nil
        }
        return allowed.contains(fileExtension) ? candidate : nil
    }

    private func isTrustedNativeDownloadSource(_ source: WKWebView?) -> Bool {
        guard let source, source === webView else { return false }
        return isSafeAppURL(source.url) && nativeDialogURLIsSameOrigin(source.url) && nativeDialogWindow(for: source) != nil
    }

    private func nativeDownload(_ item: OptionHelperNativeDownload?, matches url: URL?) -> Bool {
        guard let item, !item.finished, isTrustedNativeDownloadSource(item.sourceView) else { return false }
        return item.sourceView?.url == item.sourceURL && url == item.requestURL && nativeDownloadKind(for: url) == item.kind
    }

    private func finishNativeDownload(_ item: OptionHelperNativeDownload?, cancel: Bool) {
        guard let item, !item.finished else { return }
        item.finished = true
        nativeDownloads.removeValue(forKey: ObjectIdentifier(item.download))
        item.destinationDialog?.cancel()
        item.destinationDialog = nil
        if cancel { item.download.cancel(nil) }
        if let staging = item.stagingURL { try? FileManager.default.removeItem(at: staging) }
    }

    private func cancelNativeDownloads(for source: WKWebView? = nil) {
        for item in Array(nativeDownloads.values) where source == nil || item.sourceView === source {
            finishNativeDownload(item, cancel: true)
        }
    }

    private func registerNativeDownload(_ download: WKDownload, webView source: WKWebView, requestURL url: URL?) {
        let kind = nativeDownloadKind(for: url)
        if nativeDownloads[ObjectIdentifier(download)] != nil { return }
        guard isTrustedNativeDownloadSource(source), kind != .none, let sourceURL = source.url, let url else { download.cancel(nil); return }
        let item = OptionHelperNativeDownload(sourceView: source, download: download, sourceURL: sourceURL, requestURL: url, kind: kind)
        nativeDownloads[ObjectIdentifier(download)] = item
        download.delegate = self
    }

    private func showDataAssetDownloadFailure(_ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "下载未完成"
        alert.informativeText = detail
        if let window { alert.beginSheetModal(for: window) }
        else { alert.runModal() }
    }

    private func reportDownloadFilename(_ url: URL) -> String {
        let raw = url.lastPathComponent.removingPercentEncoding ?? url.lastPathComponent
        let lower = raw.lowercased()
        let suffix = [".pdf", ".html", ".docx"].first(where: lower.hasSuffix) ?? ""
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
                    let allowedMIMETypes = Set(["text/html", "application/xhtml+xml", "application/pdf",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"])
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
                    var saveError = validationError
                    if saveError == nil, let temporaryURL {
                        do {
                            // URLSession removes this file when its completion handler returns.
                            let contents = try Data(contentsOf: temporaryURL, options: .mappedIfSafe)
                            try contents.write(to: destination, options: .atomic)
                        } catch {
                            saveError = error.localizedDescription
                        }
                    }
                    if let saveError {
                        DispatchQueue.main.async {
                            self.showReportDownloadFailure(saveError)
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
        reportWindowIsEditor = isAllowedReportEditorURL(request.url)
        previewWindow.title = reportWindowIsEditor ? "OptionHelper报告编辑" : "OptionHelper报告预览"
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
        if webView === self.webView, navigationAction.sourceFrame.isMainFrame,
           isAllowedReportEditorURL(navigationAction.request.url) {
            showReportPreview(navigationAction.request, configuration: configuration)
            return nil
        }
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

    private func nativeDialogURLIsSameOrigin(_ url: URL?) -> Bool {
        guard let url, let appOrigin else { return false }
        return url.user == nil && url.password == nil && url.scheme == appOrigin.scheme
            && url.host == appOrigin.host && url.port == appOrigin.port
    }

    private func isTrustedNativeDialogSource(_ source: WKWebView, frame: WKFrameInfo) -> Bool {
        let ownedMain = source === webView && isSafeAppURL(source.url)
        let ownedEditor = source === reportPreviewWebView && reportWindowIsEditor
            && isAllowedReportEditorURL(source.url)
        guard ownedMain || ownedEditor, let appOrigin,
              nativeDialogURLIsSameOrigin(source.url), nativeDialogURLIsSameOrigin(frame.request.url) else { return false }
        let origin = frame.securityOrigin
        let expectedPort = appOrigin.port ?? (appOrigin.scheme == "https" ? 443 : 80)
        guard origin.protocol == appOrigin.scheme, origin.host == appOrigin.host,
              origin.port == expectedPort, let url = frame.request.url else { return false }
        if isSafeAppURL(url) || isAllowedReportEditorURL(url) { return true }
        return ["datafetcher", "payoffer", "pricer", "backtester", "reporter"].contains { module in
            url.path == "/capability/assets/pages/\(module)/\(module).html"
        }
    }

    private func nativeDialogWindow(for source: WKWebView) -> NSWindow? {
        guard let host = source.window, host.isVisible else { return nil }
        let ownedMain = source === webView && host === window
        let ownedEditor = source === reportPreviewWebView && host === reportPreviewWindow && reportWindowIsEditor
        return ownedMain || ownedEditor ? host : nil
    }

    private func cancelNativeDialogs(for source: WKWebView? = nil) {
        for dialog in Array(nativeDialogs.values) where source == nil || dialog.sourceView === source {
            dialog.cancel()
        }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        guard isTrustedNativeDialogSource(webView, frame: frame), let host = nativeDialogWindow(for: webView) else {
            completionHandler(false)
            return
        }
        cancelNativeDialogs(for: webView)
        let alert = NSAlert()
        alert.messageText = String(message.prefix(4000))
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消").keyEquivalent = "\u{1b}"
        let identity = UUID()
        let dialog = OptionHelperNativeDialog(sourceView: webView, sheetWindow: alert.window)
        let sourceURL = webView.url
        dialog.completion = { [weak self, weak webView] accepted in
            self?.nativeDialogs.removeValue(forKey: identity)
            guard let self, let webView else { completionHandler(false); return }
            completionHandler(accepted && webView.url == sourceURL && self.isTrustedNativeDialogSource(webView, frame: frame))
        }
        nativeDialogs[identity] = dialog
        alert.beginSheetModal(for: host) { response in
            dialog.finish(response == .alertFirstButtonReturn)
        }
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        guard isTrustedNativeDialogSource(webView, frame: frame), let host = nativeDialogWindow(for: webView) else {
            completionHandler(nil)
            return
        }
        cancelNativeDialogs(for: webView)
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        let identity = UUID()
        let dialog = OptionHelperNativeDialog(sourceView: webView, sheetWindow: panel)
        let sourceURL = webView.url
        dialog.completion = { [weak self, weak webView] accepted in
            self?.nativeDialogs.removeValue(forKey: identity)
            guard let self, let webView else { completionHandler(nil); return }
            let valid = accepted && webView.url == sourceURL && self.isTrustedNativeDialogSource(webView, frame: frame)
            completionHandler(valid ? panel.urls : nil)
        }
        nativeDialogs[identity] = dialog
        panel.beginSheetModal(for: host) { response in
            dialog.finish(response == .OK)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        let url = navigationAction.request.url
        let kind = nativeDownloadKind(for: url)
        if kind != .none, kind != .blob || navigationAction.shouldPerformDownload {
            let trusted = isTrustedNativeDownloadSource(webView) && isTrustedNativeDialogSource(webView, frame: navigationAction.sourceFrame)
            decisionHandler(trusted ? .download : .cancel)
            return
        }
        if navigationAction.shouldPerformDownload, !isAllowedReportArtifactURL(url) {
            decisionHandler(.cancel)
            return
        }
        if webView === self.webView, navigationAction.targetFrame?.isMainFrame == true, isSafeAppURL(url), url?.path == "/settings" {
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
        if isAllowedReportArtifactURL(url), isReportDownloadURL(url) {
            downloadReportArtifact(navigationAction.request)
            decisionHandler(.cancel)
            return
        }
        guard webView === reportPreviewWebView else {
            decisionHandler(.allow)
            return
        }
        if reportWindowIsEditor, isSafeAppURL(url), url?.path == "/optchat" {
            reportPreviewWindow?.orderOut(nil)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(url?.scheme == "about" || isAllowedReportArtifactURL(url)
            || (reportWindowIsEditor && isAllowedReportEditorURL(url)) ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let response = navigationResponse.response
        let kind = nativeDownloadKind(for: response.url)
        if kind != .none {
            let http = response as? HTTPURLResponse
            let validStatus = kind == .blob || http.map { (200..<300).contains($0.statusCode) } == true
            decisionHandler(isTrustedNativeDownloadSource(webView) && validStatus ? .download : .cancel)
            return
        }
        let disposition = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased() ?? ""
        decisionHandler(!navigationResponse.canShowMIMEType || disposition.hasPrefix("attachment") ? .cancel : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        registerNativeDownload(download, webView: webView, requestURL: navigationAction.request.url)
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        registerNativeDownload(download, webView: webView, requestURL: download.originalRequest.url)
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let item = nativeDownloads[ObjectIdentifier(download)]
        let http = response as? HTTPURLResponse
        let validStatus = item?.kind == .blob || http.map { (200..<300).contains($0.statusCode) } == true
        guard let item, nativeDownload(item, matches: response.url), validStatus,
              let filename = nativeDownloadFilename(suggestedFilename, kind: item.kind), item.destinationDialog == nil,
              let source = item.sourceView, let hostWindow = nativeDialogWindow(for: source) else {
            completionHandler(nil)
            finishNativeDownload(item, cancel: true)
            if let item, nativeDownloadFilename(suggestedFilename, kind: item.kind) == nil {
                showDataAssetDownloadFailure("下载文件名或类型不受支持。")
            }
            return
        }
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = filename
        panel.allowedFileTypes = [(filename as NSString).pathExtension]
        panel.allowsOtherFileTypes = false
        let dialog = OptionHelperNativeDialog(sourceView: source, sheetWindow: panel)
        item.destinationDialog = dialog
        dialog.completion = { [weak self] accepted in
            item.destinationDialog = nil
            guard let self, accepted, self.nativeDownload(item, matches: response.url),
                  let destination = panel.url, destination.isFileURL,
                  destination.pathExtension.lowercased() == (filename as NSString).pathExtension.lowercased() else {
                completionHandler(nil)
                self?.finishNativeDownload(item, cancel: true)
                return
            }
            item.destinationURL = destination
            let temporaryName = ".optionhelper-download-\(UUID().uuidString).\((filename as NSString).pathExtension)"
            let staging = destination.deletingLastPathComponent().appendingPathComponent(temporaryName)
            item.stagingURL = staging
            completionHandler(staging)
        }
        panel.beginSheetModal(for: hostWindow) { response in dialog.finish(response == .OK) }
    }

    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                  decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        let item = nativeDownloads[ObjectIdentifier(download)]
        let allowed = nativeDownload(item, matches: request.url)
        decisionHandler(allowed ? .allow : .cancel)
        if !allowed { finishNativeDownload(item, cancel: true) }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let item = nativeDownloads[ObjectIdentifier(download)], !item.finished else { return }
        guard nativeDownload(item, matches: item.requestURL), let staging = item.stagingURL, let destination = item.destinationURL else {
            finishNativeDownload(item, cancel: true)
            return
        }
        var saveError: Error?
        do {
            if FileManager.default.fileExists(atPath: destination.path) {
                _ = try FileManager.default.replaceItemAt(destination, withItemAt: staging)
            } else {
                try FileManager.default.moveItem(at: staging, to: destination)
            }
        } catch { saveError = error }
        finishNativeDownload(item, cancel: false)
        if let saveError { showDataAssetDownloadFailure(saveError.localizedDescription) }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        guard let item = nativeDownloads[ObjectIdentifier(download)], !item.finished else { return }
        finishNativeDownload(item, cancel: false)
        if !isCancelledNavigationError(error) { showDataAssetDownloadFailure(error.localizedDescription) }
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
            cancelNativeDialogs(for: reportPreviewWebView)
            cancelNativeDownloads(for: reportPreviewWebView)
            sender.orderOut(nil)
            return false
        }
        guard sender === window else { return true }
        if closeAfterInterrupt || (activeOperationState == "ready" && activeOperationCount == 0) {
            cancelNativeDialogs()
            cancelNativeDownloads()
            return true
        }
        let alert = NSAlert()
        alert.messageText = activeOperationState == "ready" ? "仍有任务正在运行" : "正在确认后台任务状态"
        alert.informativeText = activeOperationState == "ready"
            ? "退出会停止这些运行，并将它们标记为中断。"
            : "当前无法确认活动Operation数量。退出仍会请求停止后台运行。"
        alert.addButton(withTitle: "退出并停止")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return false }
        closeAfterInterrupt = true
        cancelNativeDialogs()
        cancelNativeDownloads()
        webView?.evaluateJavaScript("fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{}).finally(()=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'shutdown_complete'}));")
        return false
    }

    func windowWillClose(_ notification: Notification) {
        if notification.object as? NSWindow === window {
            cancelNativeDialogs()
            cancelNativeDownloads()
        }
        guard notification.object as? NSWindow === window else { return }
        NSApp.terminate(nil)
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "optionhelperSession", message.webView === webView,
           message.frameInfo.isMainFrame, isSafeAppURL(message.frameInfo.request.url),
           let value = message.body as? [String: Any], value["type"] as? String == "workspace_rail_state",
           let collapsed = value["collapsed"] as? NSNumber,
           CFGetTypeID(collapsed) == CFBooleanGetTypeID() {
            let label = collapsed.boolValue ? "展开任务栏" : "收起任务栏"
            railToggle?.toolTip = label
            railToggle?.setAccessibilityLabel(label)
            railToggle?.setAccessibilityValue(collapsed.boolValue ? "已收起" : "已展开")
            return
        }
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
        let syncThemeScript = "window.OptionHelperTheme?.setThemePreference?.('\(preference)', {persist:false,notifyNative:false});"
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(syncThemeScript, completionHandler: nil)
            self?.settingsWebView?.evaluateJavaScript(syncThemeScript, completionHandler: nil)
        }
        if preference == "auto" {
            window?.appearance = nil
            settingsWindow?.appearance = nil
            let appearance = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
            applySidebarMaterial(theme: appearance == .darkAqua ? "dark" : "light")
            applyDockIcon(theme: appearance == .darkAqua ? "dark" : "light")
        } else {
            window?.appearance = NSAppearance(named: theme == "dark" ? .darkAqua : .aqua)
            settingsWindow?.appearance = window?.appearance
            applySidebarMaterial(theme: theme)
            applyDockIcon(theme: theme)
        }
    }

    private func clearAuxiliaryWindowsAfterSignOut() {
        cancelNativeDialogs()
        cancelNativeDownloads()
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
            applySidebarMaterial(theme: appearance == .darkAqua ? "dark" : "light")
            applyDockIcon(theme: appearance == .darkAqua ? "dark" : "light")
            return
        }
        super.observeValue(forKeyPath: keyPath, of: object, change: change, context: context)
    }

    private func applySidebarMaterial(theme: String) {
        let dark = theme == "dark"
        sidebarMaterialView?.material = .sidebar
        sidebarMaterialView?.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    }

    private func applyDockIcon(theme: String) {
        guard theme == "dark", let resources = Bundle.main.resourceURL else {
            NSApp.applicationIconImage = nil
            return
        }
        let icon = resources.appendingPathComponent("assets/icons/optionhelper-app-icon-tile-dark.icns")
        NSApp.applicationIconImage = NSImage(contentsOf: icon)
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(toggleRail(_:)) {
            return ["/optchat", "/optdesk"].contains(webView?.url?.path ?? "")
        }
        return true
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
        addEditItem("撤销", action: #selector(OptionHelperEditingActions.undo(_:)), key: "z", to: editMenu)
        addEditItem("重做", action: #selector(OptionHelperEditingActions.redo(_:)), key: "z", modifiers: [.command, .shift], to: editMenu)
        editMenu.addItem(.separator())
        addEditItem("剪切", action: #selector(OptionHelperEditingActions.cut(_:)), key: "x", to: editMenu)
        addEditItem("复制", action: #selector(OptionHelperEditingActions.copy(_:)), key: "c", to: editMenu)
        addEditItem("粘贴", action: #selector(OptionHelperEditingActions.paste(_:)), key: "v", to: editMenu)
        addEditItem("全选", action: #selector(OptionHelperEditingActions.selectAll(_:)), key: "a", to: editMenu)
        mainMenu.addItem(editMenuItem)

        let viewMenu = NSMenu(title: "显示")
        let viewMenuItem = NSMenuItem()
        viewMenuItem.submenu = viewMenu
        let sidebar = NSMenuItem(title: "收起或展开任务栏", action: #selector(toggleRail(_:)), keyEquivalent: "s")
        sidebar.keyEquivalentModifierMask = [.control, .command]
        sidebar.target = self
        viewMenu.addItem(sidebar)
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
