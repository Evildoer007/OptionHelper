#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <math.h>

static const NSTimeInterval presentationRetryInterval = 0.15;
static const NSUInteger presentationMaxAttempts = 28;
static const NSTimeInterval presentationReadyTimeout = 4.05;

typedef NS_ENUM(NSInteger, OptionHelperPresentationDecision) {
    OptionHelperPresentationDecisionRetry,
    OptionHelperPresentationDecisionReady,
    OptionHelperPresentationDecisionFailed,
};

static OptionHelperPresentationDecision OptionHelperPresentationDecisionForAttempt(
    BOOL contentHealthy,
    BOOL snapshotReady,
    NSUInteger attempt,
    BOOL withinDeadline
) {
    if (contentHealthy && snapshotReady) return OptionHelperPresentationDecisionReady;
    return withinDeadline && attempt + 1 < presentationMaxAttempts
        ? OptionHelperPresentationDecisionRetry
        : OptionHelperPresentationDecisionFailed;
}

@interface OptionHelperWindow : NSWindow
@end

@implementation OptionHelperWindow

- (BOOL)performKeyEquivalent:(NSEvent *)event {
    NSEventModifierFlags modifiers = event.modifierFlags & NSEventModifierFlagDeviceIndependentFlagsMask;
    if (modifiers == NSEventModifierFlagCommand) {
        NSString *key = event.charactersIgnoringModifiers.lowercaseString;
        SEL action = nil;
        if ([key isEqualToString:@"x"]) action = @selector(cut:);
        else if ([key isEqualToString:@"c"]) action = @selector(copy:);
        else if ([key isEqualToString:@"v"]) action = @selector(paste:);
        else if ([key isEqualToString:@"a"]) action = @selector(selectAll:);
        if (action != nil && [NSApp sendAction:action to:nil from:self]) return YES;
    }
    return [super performKeyEquivalent:event];
}

@end

@interface OptionHelperReportDownloadDelegate : NSObject <NSURLSessionTaskDelegate>
@property(nonatomic, copy) BOOL (^allowsURL)(NSURL *url);
@end

@implementation OptionHelperReportDownloadDelegate

- (void)URLSession:(NSURLSession *)session
              task:(NSURLSessionTask *)task
willPerformHTTPRedirection:(NSHTTPURLResponse *)response
        newRequest:(NSURLRequest *)request
 completionHandler:(void (^)(NSURLRequest * _Nullable))completionHandler {
    completionHandler(self.allowsURL != nil && self.allowsURL(request.URL) ? request : nil);
}

@end

@interface OptionHelperTitlebarDragView : NSView
@end

@implementation OptionHelperTitlebarDragView

- (BOOL)mouseDownCanMoveWindow {
    return YES;
}

- (BOOL)acceptsFirstMouse:(NSEvent *)event {
    return YES;
}

- (void)mouseDown:(NSEvent *)event {
    [self.window performWindowDragWithEvent:event];
}

@end

@interface OptionHelperAppDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate>
@property(nonatomic, strong) NSTask *backend;
@property(nonatomic, strong) NSPipe *startupPipe;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSWindow *reportPreviewWindow;
@property(nonatomic, strong) WKWebView *reportPreviewWebView;
@property(nonatomic, strong) NSWindow *settingsWindow;
@property(nonatomic, strong) WKWebView *settingsWebView;
@property(nonatomic, strong) NSView *settingsRecoveryView;
@property(nonatomic, strong) NSTextField *settingsRecoveryDetail;
@property(nonatomic) NSUInteger settingsNavigationGeneration;
@property(nonatomic, strong) NSURL *appOrigin;
@property(nonatomic, strong) NSURL *lastSafeURL;
@property(nonatomic) BOOL navigationFailureVisible;
@property(nonatomic, strong) NSView *navigationRecoveryView;
@property(nonatomic, strong) NSTextField *navigationRecoveryDetail;
@property(nonatomic) NSUInteger navigationGeneration;
@property(nonatomic, strong) OptionHelperTitlebarDragView *titlebarDragView;
@property(nonatomic, strong) NSButton *railToggle;
@property(nonatomic, strong) NSButton *reportToggle;
@property(nonatomic) BOOL loadedURL;
@property(nonatomic, strong) NSMutableString *startupOutput;
@property(nonatomic, copy) NSString *themePreference;
@property(nonatomic) NSInteger activeOperationCount;
@property(nonatomic, copy) NSString *activeOperationState;
@property(nonatomic) BOOL closeAfterInterrupt;
@property(nonatomic) CGFloat uiScale;
@property(nonatomic, strong) NSURL *uiPreferencesURL;
@property(nonatomic, strong) NSMenuItem *zoomInMenuItem;
@property(nonatomic, strong) NSMenuItem *zoomOutMenuItem;
@property(nonatomic, copy) NSString *initializationToken;
@end

@implementation OptionHelperAppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [self installMainMenu];
    self.startupOutput = [NSMutableString string];
    self.themePreference = @"light";
    self.uiScale = 1.0;
    self.initializationToken = [[[NSUUID UUID] UUIDString] stringByReplacingOccurrencesOfString:@"-" withString:@""];
    [NSApp addObserver:self
            forKeyPath:@"effectiveAppearance"
               options:NSKeyValueObservingOptionNew
               context:NULL];
    [self startBackend];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    [self cancelNavigationWatchdog];
    [self cancelSettingsNavigationWatchdog];
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperTheme"];
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperRuntime"];
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperSession"];
    [self.webView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperUIScale"];
    [self.settingsWebView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperTheme"];
    [self.settingsWebView.configuration.userContentController removeScriptMessageHandlerForName:@"optionhelperUIScale"];
    [NSApp removeObserver:self forKeyPath:@"effectiveAppearance"];
    if (self.backend.running) {
        [self.backend terminate];
    }
    [self.reportPreviewWindow orderOut:nil];
    [self.settingsWindow orderOut:nil];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return YES;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    if (!flag && self.window != nil) {
        [self.window makeKeyAndOrderFront:nil];
        [NSApp activateIgnoringOtherApps:YES];
    }
    return YES;
}

- (void)startBackend {
    NSURL *resources = [[NSBundle mainBundle] resourceURL];
    if (resources == nil) {
        [self showFailure:@"找不到内置资源目录"];
        return;
    }
    NSString *backendPath = [[[[resources URLByAppendingPathComponent:@"backend" isDirectory:YES]
                                      URLByAppendingPathComponent:@"OptionHelperBackend" isDirectory:YES]
                                      URLByAppendingPathComponent:@"OptionHelperBackend" isDirectory:NO] path];
    if (![[NSFileManager defaultManager] isExecutableFileAtPath:backendPath]) {
        [self showFailure:@"找不到内置App Host"];
        return;
    }
    NSString *requestedDataDirectory = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPTIONHELPER_APP_DATA_DIR"];
    NSURL *support = nil;
    if (requestedDataDirectory.length > 0) {
        support = [NSURL fileURLWithPath:requestedDataDirectory isDirectory:YES];
    } else {
        NSArray<NSURL *> *supportDirectories = [[NSFileManager defaultManager]
            URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask];
        NSURL *applicationSupport = supportDirectories.firstObject;
        support = [[applicationSupport URLByAppendingPathComponent:@"OptionHelper" isDirectory:YES]
            URLByAppendingPathComponent:@"local-state" isDirectory:YES];
    }
    NSError *directoryError = nil;
    if (![[NSFileManager defaultManager] createDirectoryAtURL:support
                                   withIntermediateDirectories:YES
                                                    attributes:nil
                                                         error:&directoryError]) {
        [self showFailure:@"无法创建本地数据目录"];
        return;
    }
    self.uiPreferencesURL = [support URLByAppendingPathComponent:@"ui-preferences.json" isDirectory:NO];
    self.uiScale = [self loadUIScale];
    [self updateScaleMenuAvailability];

    self.backend = [[NSTask alloc] init];
    self.backend.executableURL = [NSURL fileURLWithPath:backendPath];
    self.backend.currentDirectoryURL = [NSURL fileURLWithPath:[backendPath stringByDeletingLastPathComponent]];
    NSMutableArray<NSString *> *arguments = [@[@"--host", @"127.0.0.1", @"--port", @"0", @"--data-dir", support.path,
                                               @"--resource-dir", resources.path,
                                               @"--initialization-token", self.initializationToken] mutableCopy];
    NSString *verificationFixture = [[[NSProcessInfo processInfo] environment] objectForKey:@"OPTIONHELPER_VERIFICATION_FIXTURE"];
    if ([verificationFixture isEqualToString:@"1"]) {
        [arguments addObject:@"--verification-fixture"];
    }
    self.backend.arguments = arguments.copy;
    self.startupPipe = [[NSPipe alloc] init];
    self.backend.standardOutput = self.startupPipe;
    self.backend.standardError = self.startupPipe;
    [[NSNotificationCenter defaultCenter] addObserver:self
                                             selector:@selector(readBackendOutput:)
                                                 name:NSFileHandleReadCompletionNotification
                                               object:self.startupPipe.fileHandleForReading];
    [self.startupPipe.fileHandleForReading readInBackgroundAndNotify];
    __weak typeof(self) weakSelf = self;
    self.backend.terminationHandler = ^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!weakSelf.loadedURL && task.terminationStatus != 0) {
                [weakSelf showFailure:@"App Host未能启动"];
            }
        });
    };
    NSError *launchError = nil;
    if (![self.backend launchAndReturnError:&launchError]) {
        [self showFailure:@"无法启动内置App Host"];
    }
}

- (void)readBackendOutput:(NSNotification *)note {
    NSData *data = note.userInfo[NSFileHandleNotificationDataItem];
    if (data.length == 0) {
        return;
    }
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (text != nil) {
        [self.startupOutput appendString:text];
        [self consumeBackendOutput];
    }
    if (!self.loadedURL) {
        [self.startupPipe.fileHandleForReading readInBackgroundAndNotify];
    }
}

- (void)consumeBackendOutput {
    NSRange marker = [self.startupOutput rangeOfString:@"OPTIONHELPER_URL="];
    if (marker.location == NSNotFound) {
        return;
    }
    NSUInteger start = marker.location + marker.length;
    NSRange newline = [self.startupOutput rangeOfCharacterFromSet:[NSCharacterSet newlineCharacterSet]
                                                          options:0
                                                            range:NSMakeRange(start, self.startupOutput.length - start)];
    if (newline.location == NSNotFound) {
        return;
    }
    NSString *address = [self.startupOutput substringWithRange:NSMakeRange(start, newline.location - start)];
    NSURL *url = [NSURL URLWithString:address];
    if (url == nil || ![url.host isEqualToString:@"127.0.0.1"]) {
        [self showFailure:@"App Host返回了无效地址"];
        return;
    }
    self.loadedURL = YES;
    [self showWebView:url];
}

- (void)showWebView:(NSURL *)url {
    NSURLComponents *components = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
    if (components == nil) {
        [self showFailure:@"App Host返回了无效启动地址"];
        return;
    }
    NSMutableArray<NSURLQueryItem *> *queryItems = [NSMutableArray array];
    for (NSURLQueryItem *item in components.queryItems ?: @[]) {
        if (![item.name isEqualToString:@"app_startup"]) {
            [queryItems addObject:item];
        }
    }
    // A fresh native launch must play once even if WebKit restores an old
    // sessionStorage page.  The login page removes this token after use.
    [queryItems addObject:[NSURLQueryItem queryItemWithName:@"app_startup" value:[NSUUID UUID].UUIDString]];
    components.queryItems = queryItems;
    NSURL *startupURL = components.URL;
    if (startupURL == nil) {
        [self showFailure:@"App Host返回了无效启动地址"];
        return;
    }
    NSURLComponents *originComponents = [[NSURLComponents alloc] init];
    originComponents.scheme = url.scheme;
    originComponents.host = url.host;
    originComponents.port = url.port;
    self.appOrigin = originComponents.URL;
    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    // Show the splash before the login page's module graph is evaluated.  The
    // login module owns the timer and always removes this class again.
    NSString *startupSource = [NSString stringWithFormat:@"document.documentElement.dataset.nativeShell='macos';document.documentElement.dataset.nativeMaterial='system';document.documentElement.dataset.uiScale='%.4f';document.documentElement.style.setProperty('--native-titlebar-height','%.4fpx');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','%.4fpx');if(location.pathname==='/'){window.__optionhelperInitializationToken='%@';document.documentElement.classList.add('login-boot');}", self.uiScale, 36.0 / self.uiScale, 166.0 / self.uiScale, self.initializationToken];
    WKUserScript *startupScript = [[WKUserScript alloc] initWithSource:startupSource
                                                         injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                      forMainFrameOnly:YES];
    WKUserScript *runtimeStatusScript = [[WKUserScript alloc] initWithSource:@"(()=>{const post=(state,count)=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'active_operations',state,...(Number.isFinite(count)?{count}: {})});const refresh=()=>fetch('/api/runtime/active-operations').then((response)=>{if(!response.ok)throw new Error('runtime_status_unavailable');return response.json();}).then((value)=>{const count=value?.active_count;if(!Number.isInteger(count)||count<0)throw new Error('runtime_status_invalid');post('ready',count);}).catch(()=>post('recovering'));addEventListener('pageshow',refresh);setInterval(refresh,1400);refresh();})();"
                                                              injectionTime:WKUserScriptInjectionTimeAtDocumentEnd
                                                           forMainFrameOnly:YES];
    [configuration.userContentController addUserScript:startupScript];
    [configuration.userContentController addUserScript:runtimeStatusScript];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperTheme"];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperRuntime"];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperSession"];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperUIScale"];
    WKWebView *webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    webView.allowsMagnification = NO;
    webView.pageZoom = self.uiScale;
    webView.navigationDelegate = self;
    webView.UIDelegate = self;
    webView.underPageBackgroundColor = NSColor.clearColor;
    webView.wantsLayer = YES;
    webView.layer.backgroundColor = NSColor.clearColor.CGColor;

    NSVisualEffectView *materialView = [[NSVisualEffectView alloc] initWithFrame:NSZeroRect];
    materialView.material = NSVisualEffectMaterialSidebar;
    materialView.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    materialView.state = NSVisualEffectStateFollowsWindowActiveState;
    materialView.emphasized = NO;

    NSView *contentView = [[NSView alloc] initWithFrame:NSZeroRect];
    contentView.wantsLayer = YES;
    contentView.layer.backgroundColor = NSColor.clearColor.CGColor;
    materialView.translatesAutoresizingMaskIntoConstraints = NO;
    webView.translatesAutoresizingMaskIntoConstraints = NO;
    [contentView addSubview:materialView];
    [contentView addSubview:webView];
    [NSLayoutConstraint activateConstraints:@[
        [materialView.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
        [materialView.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
        [materialView.topAnchor constraintEqualToAnchor:contentView.topAnchor],
        [materialView.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
        [webView.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
        [webView.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
        [webView.topAnchor constraintEqualToAnchor:contentView.topAnchor],
        [webView.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
    ]];
    self.window = [[OptionHelperWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1320, 860)
                                               styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable | NSWindowStyleMaskFullSizeContentView)
                                                 backing:NSBackingStoreBuffered
                                                   defer:NO];
    self.window.title = @"OptionHelper";
    self.window.titleVisibility = NSWindowTitleHidden;
    self.window.titlebarAppearsTransparent = YES;
    self.window.titlebarSeparatorStyle = NSTitlebarSeparatorStyleNone;
    self.window.movableByWindowBackground = YES;
    self.window.backgroundColor = NSColor.clearColor;
    self.window.opaque = NO;
    self.window.delegate = self;
    self.window.contentView = contentView;
    [self.window makeFirstResponder:webView];
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    self.webView = webView;
    self.lastSafeURL = startupURL;
    self.activeOperationState = @"recovering";
    [self ensureTitlebarControlsForWindow:self.window];
    dispatch_async(dispatch_get_main_queue(), ^{
        [self ensureTitlebarControlsForWindow:self.window];
        [self syncWorkspaceTitlebarControlsForURL:self.webView.URL];
    });
    [webView loadRequest:[NSURLRequest requestWithURL:startupURL]];
}

- (void)ensureTitlebarControlsForWindow:(NSWindow *)window {
    NSButton *closeButton = [window standardWindowButton:NSWindowCloseButton];
    NSView *titlebar = closeButton.superview;
    if (closeButton == nil || titlebar == nil) return;

    if (self.titlebarDragView != nil && self.titlebarDragView.superview != titlebar) {
        [self.titlebarDragView removeFromSuperview];
        self.titlebarDragView = nil;
    }
    if (self.railToggle != nil && self.railToggle.superview != titlebar) {
        [self.railToggle removeFromSuperview];
        self.railToggle = nil;
    }
    if (self.reportToggle != nil && self.reportToggle.superview != titlebar) {
        [self.reportToggle removeFromSuperview];
        self.reportToggle = nil;
    }

    if (self.titlebarDragView == nil) [self installTitlebarDragViewForWindow:window];
    if (self.railToggle == nil) [self installRailToggleForWindow:window];
    if (self.reportToggle == nil) [self installReportToggleForWindow:window];
    [self layoutTitlebarControls];
}

- (void)installTitlebarDragViewForWindow:(NSWindow *)window {
    NSButton *closeButton = [window standardWindowButton:NSWindowCloseButton];
    NSView *titlebar = closeButton.superview;
    if (closeButton == nil || titlebar == nil) return;
    OptionHelperTitlebarDragView *dragView = [[OptionHelperTitlebarDragView alloc] initWithFrame:NSZeroRect];
    [titlebar addSubview:dragView];
    self.titlebarDragView = dragView;
    [self layoutTitlebarControls];
}

- (void)installRailToggleForWindow:(NSWindow *)window {
    NSButton *closeButton = [window standardWindowButton:NSWindowCloseButton];
    NSView *titlebar = closeButton.superview;
    if (closeButton == nil || titlebar == nil) return;
    NSSize size = NSMakeSize(26, 24);
    NSRect frame = NSMakeRect(NSMaxX(closeButton.frame) + 94, NSMidY(closeButton.frame) - size.height / 2, size.width, size.height);
    NSButton *button = [[NSButton alloc] initWithFrame:frame];
    button.image = [NSImage imageWithSystemSymbolName:@"sidebar.left" accessibilityDescription:@"收起或展开任务栏"];
    button.contentTintColor = NSColor.secondaryLabelColor;
    button.bezelStyle = NSBezelStyleInline;
    button.bordered = NO;
    button.target = self;
    button.action = @selector(toggleRail:);
    button.toolTip = @"收起或展开任务栏";
    button.hidden = YES;
    [titlebar addSubview:button];
    self.railToggle = button;
    [self layoutTitlebarControls];
}

- (void)installReportToggleForWindow:(NSWindow *)window {
    NSButton *closeButton = [window standardWindowButton:NSWindowCloseButton];
    NSView *titlebar = closeButton.superview;
    if (closeButton == nil || titlebar == nil) return;
    NSButton *button = [[NSButton alloc] initWithFrame:NSZeroRect];
    button.image = [NSImage imageWithSystemSymbolName:@"sidebar.right" accessibilityDescription:@"打开或收起报告库"];
    button.contentTintColor = NSColor.secondaryLabelColor;
    button.bezelStyle = NSBezelStyleInline;
    button.bordered = NO;
    button.target = self;
    button.action = @selector(toggleReport:);
    button.toolTip = @"打开或收起报告库";
    button.hidden = YES;
    [titlebar addSubview:button];
    self.reportToggle = button;
    [self layoutTitlebarControls];
}

- (void)layoutTitlebarControls {
    NSButton *closeButton = [self.window standardWindowButton:NSWindowCloseButton];
    NSView *titlebar = closeButton.superview;
    if (closeButton == nil || titlebar == nil) return;
    NSSize size = NSMakeSize(26, 24);
    self.railToggle.frame = NSMakeRect(NSMaxX(closeButton.frame) + 94, NSMidY(closeButton.frame) - size.height / 2, size.width, size.height);
    self.reportToggle.frame = NSMakeRect(NSMaxX(titlebar.bounds) - size.width - 14, NSMidY(closeButton.frame) - size.height / 2, size.width, size.height);
    CGFloat dragLeading = (self.railToggle != nil ? NSMaxX(self.railToggle.frame) : NSMaxX(closeButton.frame) + 120) + 8;
    CGFloat dragTrailing = (self.reportToggle != nil ? NSMinX(self.reportToggle.frame) : NSMaxX(titlebar.bounds) - 40) - 8;
    self.titlebarDragView.frame = NSMakeRect(dragLeading, 0, MAX(0, dragTrailing - dragLeading), NSHeight(titlebar.bounds));
}

- (void)toggleRail:(id)sender {
    [self.webView evaluateJavaScript:@"document.querySelector('[data-rail-collapse-toggle]')?.click()" completionHandler:nil];
}

- (void)toggleReport:(id)sender {
    [self.webView evaluateJavaScript:@"document.querySelector('[data-report-toggle]')?.click()" completionHandler:nil];
}

- (void)syncWorkspaceTitlebarControlsForURL:(NSURL *)url {
    NSString *route = [[url.path stringByTrimmingCharactersInSet:[NSCharacterSet characterSetWithCharactersInString:@"/"]] lowercaseString];
    BOOL showsWorkspaceControls = [route isEqualToString:@"optchat"] || [route isEqualToString:@"optdesk"];
    NSString *reportLabel = [route isEqualToString:@"optchat"] ? @"打开或收起任务与交付" : @"打开或收起报告库";
    self.railToggle.hidden = !showsWorkspaceControls;
    self.reportToggle.hidden = !showsWorkspaceControls;
    self.reportToggle.toolTip = reportLabel;
    [self.reportToggle setAccessibilityLabel:reportLabel];
    self.reportToggle.image = [NSImage imageWithSystemSymbolName:@"sidebar.right" accessibilityDescription:reportLabel];
    [self layoutTitlebarControls];
}

- (BOOL)isSafeAppURL:(NSURL *)url {
    if (url == nil || self.appOrigin == nil) return NO;
    BOOL samePort = (url.port == nil && self.appOrigin.port == nil) || [url.port isEqualToNumber:self.appOrigin.port];
    BOOL sameOrigin = [url.scheme isEqualToString:self.appOrigin.scheme]
        && [url.host isEqualToString:self.appOrigin.host]
        && samePort;
    return sameOrigin && [@[@"/", @"/optchat", @"/optdesk", @"/settings"] containsObject:url.path];
}

- (NSView *)ensureNavigationRecoveryView {
    if (self.navigationRecoveryView != nil) return self.navigationRecoveryView;
    NSView *contentView = self.window.contentView;
    if (contentView == nil) return nil;

    NSView *overlay = [[NSView alloc] initWithFrame:NSZeroRect];
    overlay.translatesAutoresizingMaskIntoConstraints = NO;
    overlay.wantsLayer = YES;
    overlay.layer.backgroundColor = NSColor.windowBackgroundColor.CGColor;
    overlay.accessibilityLabel = @"OptionHelper页面恢复";

    NSTextField *title = [NSTextField labelWithString:@"页面暂时无法显示"];
    title.font = [NSFont systemFontOfSize:24 weight:NSFontWeightSemibold];
    NSTextField *detail = [NSTextField wrappingLabelWithString:@""];
    detail.font = [NSFont systemFontOfSize:14];
    detail.textColor = NSColor.secondaryLabelColor;
    detail.maximumNumberOfLines = 0;
    self.navigationRecoveryDetail = detail;

    NSButton *retry = [NSButton buttonWithTitle:@"重新打开上一个页面" target:self action:@selector(recoverLastSafePage:)];
    retry.bezelStyle = NSBezelStyleRounded;
    retry.keyEquivalent = @"\r";
    NSButton *workspace = [NSButton buttonWithTitle:@"返回工作台" target:self action:@selector(recoverWorkspace:)];
    workspace.bezelStyle = NSBezelStyleRounded;

    NSStackView *actions = [NSStackView stackViewWithViews:@[retry, workspace]];
    actions.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    actions.alignment = NSLayoutAttributeCenterY;
    actions.spacing = 10;
    NSStackView *panel = [NSStackView stackViewWithViews:@[title, detail, actions]];
    panel.translatesAutoresizingMaskIntoConstraints = NO;
    panel.orientation = NSUserInterfaceLayoutOrientationVertical;
    panel.alignment = NSLayoutAttributeLeading;
    panel.spacing = 14;
    [overlay addSubview:panel];
    [contentView addSubview:overlay positioned:NSWindowAbove relativeTo:self.webView];
    [NSLayoutConstraint activateConstraints:@[
        [overlay.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
        [overlay.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
        [overlay.topAnchor constraintEqualToAnchor:contentView.topAnchor],
        [overlay.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
        [panel.centerXAnchor constraintEqualToAnchor:overlay.centerXAnchor],
        [panel.centerYAnchor constraintEqualToAnchor:overlay.centerYAnchor],
        [panel.widthAnchor constraintLessThanOrEqualToConstant:520],
        [panel.leadingAnchor constraintGreaterThanOrEqualToAnchor:overlay.leadingAnchor constant:24],
        [panel.trailingAnchor constraintLessThanOrEqualToAnchor:overlay.trailingAnchor constant:-24],
    ]];
    overlay.hidden = YES;
    self.navigationRecoveryView = overlay;
    return overlay;
}

- (void)beginNavigationWatchdog {
    self.navigationGeneration += 1;
    NSUInteger generation = self.navigationGeneration;
    __weak typeof(self) weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil || generation != self.navigationGeneration) return;
        [self showNavigationFailure:@"页面载入没有完成。后台任务不会因此取消，可重新打开上一个页面。"];
    });
}

- (void)cancelNavigationWatchdog {
    self.navigationGeneration += 1;
}

- (void)hideNavigationFailure {
    self.navigationRecoveryView.hidden = YES;
    self.navigationFailureVisible = NO;
}

- (void)showNavigationFailure:(NSString *)detail {
    if (self.webView == nil || self.appOrigin == nil) return;
    NSView *recoveryView = [self ensureNavigationRecoveryView];
    if (recoveryView == nil) return;
    self.navigationFailureVisible = YES;
    self.navigationRecoveryDetail.stringValue = detail ?: @"页面暂时无法显示。";
    recoveryView.hidden = NO;
    [self syncWorkspaceTitlebarControlsForURL:nil];
    [self.window makeFirstResponder:recoveryView];
}

- (void)loadRecoveryURL:(NSURL *)url {
    if (![self isSafeAppURL:url]) return;
    self.navigationRecoveryDetail.stringValue = @"正在重新打开页面…";
    [self beginNavigationWatchdog];
    [self.webView loadRequest:[NSURLRequest requestWithURL:url]];
}

- (void)recoverLastSafePage:(id)sender {
    [self loadRecoveryURL:self.lastSafeURL ?: self.appOrigin];
}

- (void)recoverWorkspace:(id)sender {
    [self loadRecoveryURL:[self.appOrigin URLByAppendingPathComponent:@"optchat"]];
}

- (NSString *)visibleRootHealthScript {
    return @"(()=>{const path=location.pathname;const selector=path==='/settings'?'.setup-card':(['/optchat','/optdesk'].includes(path)?'[data-workspace-shell]':(path==='/'?'body.login-page':'main'));const root=document.querySelector(selector);if(!root)return {ready:false,selector,reason:'missing'};const style=getComputedStyle(root);const rect=root.getBoundingClientRect();const visible=style.display!=='none'&&style.visibility!=='hidden'&&style.visibility!=='collapse'&&Number(style.opacity)>0.01&&root.getClientRects().length>0&&rect.width>80&&rect.height>80&&rect.bottom>0&&rect.right>0&&rect.top<innerHeight&&rect.left<innerWidth;return {ready:['interactive','complete'].includes(document.readyState),textLength:((root.innerText||root.textContent)||'').trim().length,width:rect.width,height:rect.height,visible,selector};})()";
}

- (void)scheduleMainPresentationRetryForGeneration:(NSUInteger)generation
                                           attempt:(NSUInteger)attempt
                                          deadline:(NSTimeInterval)deadline {
    __weak typeof(self) weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(presentationRetryInterval * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil || generation != self.navigationGeneration) return;
        [self verifyMainPresentationForGeneration:generation attempt:attempt deadline:deadline];
    });
}

- (void)failMainPresentationForGeneration:(NSUInteger)generation message:(NSString *)message {
    if (generation != self.navigationGeneration) return;
    [self cancelNavigationWatchdog];
    [self showNavigationFailure:message];
}

- (void)completeMainPresentationForWebView:(WKWebView *)webView generation:(NSUInteger)generation {
    if (generation != self.navigationGeneration || webView != self.webView) return;
    [self cancelNavigationWatchdog];
    if ([self isSafeAppURL:webView.URL]) self.lastSafeURL = webView.URL;
    [self hideNavigationFailure];
    [self.window makeFirstResponder:webView];
}

- (void)verifyMainPresentationForGeneration:(NSUInteger)generation
                                     attempt:(NSUInteger)attempt
                                    deadline:(NSTimeInterval)deadline {
    WKWebView *mainView = self.webView;
    if (mainView == nil || generation != self.navigationGeneration) return;
    __weak typeof(self) weakSelf = self;
    [mainView evaluateJavaScript:[self visibleRootHealthScript] completionHandler:^(id result, NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil || generation != self.navigationGeneration || mainView != self.webView) return;
        NSDictionary *health = [result isKindOfClass:NSDictionary.class] ? result : nil;
        BOOL healthy = error == nil && [health[@"ready"] boolValue] && [health[@"visible"] boolValue]
            && [health[@"textLength"] integerValue] > 0 && [health[@"width"] doubleValue] > 80 && [health[@"height"] doubleValue] > 80;
        if (!healthy) {
            BOOL withinDeadline = NSProcessInfo.processInfo.systemUptime < deadline;
            OptionHelperPresentationDecision decision = OptionHelperPresentationDecisionForAttempt(NO, NO, attempt, withinDeadline);
            if (decision == OptionHelperPresentationDecisionRetry) {
                [self scheduleMainPresentationRetryForGeneration:generation attempt:attempt + 1 deadline:deadline];
            } else {
                [self failMainPresentationForGeneration:generation
                                                 message:@"页面内容没有正确呈现。后台任务不会因此取消，可重新打开上一个页面。"];
            }
            return;
        }
        WKSnapshotConfiguration *snapshotConfiguration = [[WKSnapshotConfiguration alloc] init];
        snapshotConfiguration.rect = mainView.bounds;
        snapshotConfiguration.snapshotWidth = @480;
        [mainView takeSnapshotWithConfiguration:snapshotConfiguration completionHandler:^(NSImage *snapshot, NSError *snapshotError) {
            if (generation != self.navigationGeneration || mainView != self.webView) return;
            BOOL snapshotReady = snapshotError == nil && snapshot != nil && snapshot.size.width > 1 && snapshot.size.height > 1;
            BOOL withinDeadline = NSProcessInfo.processInfo.systemUptime < deadline;
            OptionHelperPresentationDecision decision = OptionHelperPresentationDecisionForAttempt(YES, snapshotReady, attempt, withinDeadline);
            if (decision == OptionHelperPresentationDecisionReady) {
                [self completeMainPresentationForWebView:mainView generation:generation];
            } else if (decision == OptionHelperPresentationDecisionRetry) {
                [self scheduleMainPresentationRetryForGeneration:generation attempt:attempt + 1 deadline:deadline];
            } else {
                [self failMainPresentationForGeneration:generation
                                                 message:@"页面没有生成可见画面。后台任务不会因此取消，可重新打开上一个页面。"];
            }
        }];
    }];
}

- (NSView *)ensureSettingsRecoveryViewForContentView:(NSView *)contentView relativeTo:(WKWebView *)webView {
    if (self.settingsRecoveryView != nil) return self.settingsRecoveryView;
    NSView *overlay = [[NSView alloc] initWithFrame:NSZeroRect];
    overlay.translatesAutoresizingMaskIntoConstraints = NO;
    overlay.wantsLayer = YES;
    overlay.layer.backgroundColor = NSColor.windowBackgroundColor.CGColor;
    overlay.accessibilityLabel = @"OptionHelper设置中心状态";

    NSTextField *title = [NSTextField labelWithString:@"正在打开设置中心"];
    title.font = [NSFont systemFontOfSize:20 weight:NSFontWeightSemibold];
    NSTextField *detail = [NSTextField wrappingLabelWithString:@"正在确认设置页面已完整呈现。"];
    detail.font = [NSFont systemFontOfSize:14];
    detail.textColor = NSColor.secondaryLabelColor;
    detail.maximumNumberOfLines = 0;
    self.settingsRecoveryDetail = detail;
    NSButton *retry = [NSButton buttonWithTitle:@"重新打开设置中心" target:self action:@selector(retrySettingsCenter:)];
    retry.bezelStyle = NSBezelStyleRounded;
    NSButton *close = [NSButton buttonWithTitle:@"返回工作台" target:self action:@selector(closeSettingsCenter:)];
    close.bezelStyle = NSBezelStyleRounded;
    NSStackView *actions = [NSStackView stackViewWithViews:@[retry, close]];
    actions.orientation = NSUserInterfaceLayoutOrientationHorizontal;
    actions.alignment = NSLayoutAttributeCenterY;
    actions.spacing = 10;
    NSStackView *panel = [NSStackView stackViewWithViews:@[title, detail, actions]];
    panel.translatesAutoresizingMaskIntoConstraints = NO;
    panel.orientation = NSUserInterfaceLayoutOrientationVertical;
    panel.alignment = NSLayoutAttributeLeading;
    panel.spacing = 14;
    [overlay addSubview:panel];
    [contentView addSubview:overlay positioned:NSWindowAbove relativeTo:webView];
    [NSLayoutConstraint activateConstraints:@[
        [overlay.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
        [overlay.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
        [overlay.topAnchor constraintEqualToAnchor:contentView.topAnchor],
        [overlay.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
        [panel.centerXAnchor constraintEqualToAnchor:overlay.centerXAnchor],
        [panel.centerYAnchor constraintEqualToAnchor:overlay.centerYAnchor],
        [panel.widthAnchor constraintLessThanOrEqualToConstant:520],
        [panel.leadingAnchor constraintGreaterThanOrEqualToAnchor:overlay.leadingAnchor constant:24],
        [panel.trailingAnchor constraintLessThanOrEqualToAnchor:overlay.trailingAnchor constant:-24],
    ]];
    self.settingsRecoveryView = overlay;
    return overlay;
}

- (void)beginSettingsNavigationWatchdog {
    self.settingsNavigationGeneration += 1;
    NSUInteger generation = self.settingsNavigationGeneration;
    __weak typeof(self) weakSelf = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(2.0 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil || generation != self.settingsNavigationGeneration) return;
        [self showSettingsFailure:@"设置页面载入没有完成。工作台和后台任务仍保持原状。"];
    });
}

- (void)cancelSettingsNavigationWatchdog {
    self.settingsNavigationGeneration += 1;
}

- (void)showSettingsFailure:(NSString *)detail {
    self.settingsRecoveryDetail.stringValue = detail ?: @"设置页面暂时无法显示。";
    self.settingsRecoveryView.hidden = NO;
    [self.settingsWindow makeFirstResponder:self.settingsRecoveryView];
}

- (void)hideSettingsFailure {
    self.settingsRecoveryView.hidden = YES;
    [self.settingsWindow makeFirstResponder:self.settingsWebView];
}

- (void)showSettingsCenter:(NSURLRequest *)request {
    NSURL *url = request.URL;
    if (![self isSafeAppURL:url] || ![url.path isEqualToString:@"/settings"] || self.webView == nil) return;
    if (self.settingsWindow == nil || self.settingsWebView == nil) {
        WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
        configuration.websiteDataStore = self.webView.configuration.websiteDataStore;
        [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperTheme"];
        [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperUIScale"];
        WKWebView *settingsView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
        settingsView.translatesAutoresizingMaskIntoConstraints = NO;
        settingsView.navigationDelegate = self;
        settingsView.UIDelegate = self;
        settingsView.pageZoom = self.uiScale;
        settingsView.underPageBackgroundColor = NSColor.windowBackgroundColor;
        settingsView.wantsLayer = YES;
        settingsView.layer.backgroundColor = NSColor.windowBackgroundColor.CGColor;
        NSView *contentView = [[NSView alloc] initWithFrame:NSZeroRect];
        contentView.wantsLayer = YES;
        contentView.layer.backgroundColor = NSColor.windowBackgroundColor.CGColor;
        [contentView addSubview:settingsView];
        [NSLayoutConstraint activateConstraints:@[
            [settingsView.leadingAnchor constraintEqualToAnchor:contentView.leadingAnchor],
            [settingsView.trailingAnchor constraintEqualToAnchor:contentView.trailingAnchor],
            [settingsView.topAnchor constraintEqualToAnchor:contentView.topAnchor],
            [settingsView.bottomAnchor constraintEqualToAnchor:contentView.bottomAnchor],
        ]];
        NSWindow *settingsWindow = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 960, 760)
                                                               styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                                                                 backing:NSBackingStoreBuffered
                                                                   defer:NO];
        settingsWindow.title = @"OptionHelper设置中心";
        settingsWindow.backgroundColor = NSColor.windowBackgroundColor;
        settingsWindow.opaque = YES;
        settingsWindow.delegate = self;
        settingsWindow.contentView = contentView;
        [settingsWindow center];
        self.settingsWindow = settingsWindow;
        self.settingsWebView = settingsView;
        [self ensureSettingsRecoveryViewForContentView:contentView relativeTo:settingsView];
    }
    self.settingsRecoveryDetail.stringValue = @"正在确认设置页面已完整呈现。";
    self.settingsRecoveryView.hidden = NO;
    [self.settingsWindow makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [self beginSettingsNavigationWatchdog];
    [self.settingsWebView loadRequest:request];
}

- (void)retrySettingsCenter:(id)sender {
    NSURL *url = [self isSafeAppURL:self.settingsWebView.URL] && [self.settingsWebView.URL.path isEqualToString:@"/settings"]
        ? self.settingsWebView.URL
        : [self.appOrigin URLByAppendingPathComponent:@"settings"];
    [self showSettingsCenter:[NSURLRequest requestWithURL:url]];
}

- (void)closeSettingsCenter:(id)sender {
    [self cancelSettingsNavigationWatchdog];
    [self.settingsWindow orderOut:nil];
    [self.window makeKeyAndOrderFront:nil];
    [self.window makeFirstResponder:self.webView];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)verifySettingsPresentationForGeneration:(NSUInteger)generation {
    WKWebView *settingsView = self.settingsWebView;
    if (settingsView == nil) return;
    __weak typeof(self) weakSelf = self;
    [settingsView evaluateJavaScript:[self visibleRootHealthScript] completionHandler:^(id result, NSError *error) {
        __strong typeof(weakSelf) self = weakSelf;
        if (self == nil || generation != self.settingsNavigationGeneration || settingsView != self.settingsWebView) return;
        NSDictionary *health = [result isKindOfClass:NSDictionary.class] ? result : nil;
        BOOL healthy = error == nil && [health[@"ready"] boolValue] && [health[@"visible"] boolValue]
            && [health[@"textLength"] integerValue] > 0 && [health[@"width"] doubleValue] > 80 && [health[@"height"] doubleValue] > 80;
        if (!healthy) {
            [self cancelSettingsNavigationWatchdog];
            [self showSettingsFailure:@"设置页面结构已载入，但可见内容没有正确呈现。"];
            return;
        }
        WKSnapshotConfiguration *snapshotConfiguration = [[WKSnapshotConfiguration alloc] init];
        snapshotConfiguration.rect = settingsView.bounds;
        snapshotConfiguration.snapshotWidth = @480;
        [settingsView takeSnapshotWithConfiguration:snapshotConfiguration completionHandler:^(NSImage *snapshot, NSError *snapshotError) {
            if (generation != self.settingsNavigationGeneration || settingsView != self.settingsWebView) return;
            [self cancelSettingsNavigationWatchdog];
            if (snapshotError != nil || snapshot == nil || snapshot.size.width <= 1 || snapshot.size.height <= 1) {
                [self showSettingsFailure:@"设置页面没有生成可见画面。工作台仍保持原状，可重试或返回。"];
                return;
            }
            [self hideSettingsFailure];
        }];
    }];
}

- (BOOL)isCancelledNavigationError:(NSError *)error {
    return [error.domain isEqualToString:NSURLErrorDomain] && error.code == NSURLErrorCancelled;
}

- (void)webView:(WKWebView *)webView didStartProvisionalNavigation:(WKNavigation *)navigation {
    if (webView == self.settingsWebView) {
        [self beginSettingsNavigationWatchdog];
        return;
    }
    if (webView != self.webView) return;
    [self beginNavigationWatchdog];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    if (webView == self.settingsWebView) {
        [self verifySettingsPresentationForGeneration:self.settingsNavigationGeneration];
        return;
    }
    if (webView != self.webView) return;
    [self syncUIScaleToPage];
    [self ensureTitlebarControlsForWindow:self.window];
    [self syncWorkspaceTitlebarControlsForURL:webView.URL];
    [self cancelNavigationWatchdog];
    NSUInteger navigationToken = self.navigationGeneration;
    NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + presentationReadyTimeout;
    [self verifyMainPresentationForGeneration:navigationToken attempt:0 deadline:deadline];
}

- (void)webView:(WKWebView *)webView didCommitNavigation:(WKNavigation *)navigation {
    if (webView != self.webView) return;
    [self syncUIScaleToPage];
}

- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    if (webView == self.settingsWebView && ![self isCancelledNavigationError:error]) {
        [self cancelSettingsNavigationWatchdog];
        [self showSettingsFailure:@"设置页面载入失败。工作台和后台任务仍保持原状。"];
        return;
    }
    if (webView != self.webView || [self isCancelledNavigationError:error]) return;
    [self cancelNavigationWatchdog];
    [self showNavigationFailure:@"页面载入失败。后台任务不会因此取消，可重新打开上一个页面。"];
}

- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation withError:(NSError *)error {
    if (webView == self.settingsWebView && ![self isCancelledNavigationError:error]) {
        [self cancelSettingsNavigationWatchdog];
        [self showSettingsFailure:@"设置页面连接未能建立。请确认App Host仍在运行后重试。"];
        return;
    }
    if (webView != self.webView || [self isCancelledNavigationError:error]) return;
    [self cancelNavigationWatchdog];
    [self showNavigationFailure:@"页面连接未能建立。请确认App Host仍在运行后重试。"];
}

- (void)webViewWebContentProcessDidTerminate:(WKWebView *)webView {
    if (webView == self.settingsWebView) {
        [self cancelSettingsNavigationWatchdog];
        [self showSettingsFailure:@"设置页面进程已结束。工作台和后台任务仍保持原状。"];
        return;
    }
    if (webView != self.webView) return;
    [self cancelNavigationWatchdog];
    [self showNavigationFailure:@"页面进程已结束。任务和后台计算仍保存在App Host中。"];
}

- (BOOL)isAllowedReportArtifactURL:(NSURL *)url {
    if (url == nil || self.appOrigin == nil) return NO;
    BOOL sameOrigin = [url.scheme isEqualToString:self.appOrigin.scheme]
        && [url.host isEqualToString:self.appOrigin.host]
        && ((url.port == nil && self.appOrigin.port == nil) || [url.port isEqualToNumber:self.appOrigin.port]);
    if (!sameOrigin || ![url.path hasPrefix:@"/api/reports/"] || [url.path rangeOfString:@"/artifacts/"].location == NSNotFound) return NO;
    NSArray<NSURLQueryItem *> *queryItems = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO].queryItems;
    for (NSURLQueryItem *item in queryItems ?: @[]) {
        if (![item.name isEqualToString:@"download"] || ![item.value isEqualToString:@"1"]) return NO;
    }
    return YES;
}

- (BOOL)isReportDownloadURL:(NSURL *)url {
    if (url == nil) return NO;
    for (NSURLQueryItem *item in [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO].queryItems ?: @[]) {
        if ([item.name isEqualToString:@"download"] && [item.value isEqualToString:@"1"]) return YES;
    }
    return NO;
}

- (void)showReportDownloadFailure:(NSString *)detail {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.alertStyle = NSAlertStyleWarning;
    alert.messageText = @"报告下载未完成";
    alert.informativeText = detail.length > 0 ? detail : @"未收到报告文件。";
    if (self.window != nil) [alert beginSheetModalForWindow:self.window completionHandler:nil];
    else [alert runModal];
}

- (NSString *)reportDownloadFilename:(NSURL *)url {
    NSString *raw = url.lastPathComponent.stringByRemovingPercentEncoding ?: url.lastPathComponent;
    NSString *lower = raw.lowercaseString;
    NSString *suffix = [lower hasSuffix:@".pdf"] ? @".pdf" : ([lower hasSuffix:@".html"] ? @".html" : @"");
    if (url.fragment.length > 0) {
        NSURLComponents *fragmentComponents = [NSURLComponents componentsWithString:
            [@"https://optionhelper.invalid/?" stringByAppendingString:url.fragment]];
        NSString *publicName = nil;
        for (NSURLQueryItem *item in fragmentComponents.queryItems ?: @[]) {
            if ([item.name isEqualToString:@"display_name"]) {
                publicName = item.value;
                break;
            }
        }
        if (publicName.length > 0 && publicName.length <= 160) {
            NSString *cleaned = [[publicName componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]
                componentsJoinedByString:@" "];
            cleaned = [[[cleaned stringByReplacingOccurrencesOfString:@"/" withString:@"-"]
                stringByReplacingOccurrencesOfString:@"\\" withString:@"-"]
                stringByReplacingOccurrencesOfString:@":" withString:@"-"];
            cleaned = [cleaned stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
            if (cleaned.length > 0) {
                return suffix.length == 0 || [cleaned.lowercaseString hasSuffix:suffix]
                    ? cleaned
                    : [cleaned stringByAppendingString:suffix];
            }
        }
    }
    if ([lower containsString:@"multicard"]) return [@"多结构研究简报" stringByAppendingString:suffix];
    if ([lower containsString:@"multireport"]) return [@"多结构完整研究报告" stringByAppendingString:suffix];
    return raw;
}

- (BOOL)cookie:(NSHTTPCookie *)cookie appliesToURL:(NSURL *)url {
    NSString *host = url.host.lowercaseString;
    NSString *domain = cookie.domain.lowercaseString;
    if (host.length == 0 || domain.length == 0) return NO;
    BOOL domainMatches = NO;
    if ([domain hasPrefix:@"."]) {
        NSString *suffix = [domain substringFromIndex:1];
        domainMatches = [host isEqualToString:suffix] || [host hasSuffix:[@"." stringByAppendingString:suffix]];
    } else {
        domainMatches = [host isEqualToString:domain];
    }
    NSString *path = cookie.path.length > 0 ? cookie.path : @"/";
    BOOL pathMatches = [url.path isEqualToString:path]
        || ([url.path hasPrefix:path]
            && ([path hasSuffix:@"/"] || (url.path.length > path.length && [url.path characterAtIndex:path.length] == '/')));
    BOOL schemeAllowsCookie = !cookie.secure || [url.scheme.lowercaseString isEqualToString:@"https"];
    return domainMatches && pathMatches && schemeAllowsCookie;
}

- (void)downloadReportArtifact:(NSURLRequest *)request {
    if (request.URL == nil || self.webView == nil || self.window == nil) return;
    NSSavePanel *panel = [NSSavePanel savePanel];
    panel.canCreateDirectories = YES;
    panel.nameFieldStringValue = [self reportDownloadFilename:request.URL];
    __weak typeof(self) weakSelf = self;
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response) {
        if (response != NSModalResponseOK || panel.URL == nil) return;
        NSURL *destination = panel.URL;
        OptionHelperAppDelegate *strongSelf = weakSelf;
        if (strongSelf == nil) return;
        [strongSelf.webView.configuration.websiteDataStore.httpCookieStore getAllCookies:^(NSArray<NSHTTPCookie *> *cookies) {
            NSMutableURLRequest *authorized = [request mutableCopy];
            NSURLComponents *requestComponents = [NSURLComponents componentsWithURL:request.URL resolvingAgainstBaseURL:NO];
            requestComponents.fragment = nil;
            if (requestComponents.URL != nil) authorized.URL = requestComponents.URL;
            NSMutableArray<NSHTTPCookie *> *applicableCookies = [NSMutableArray array];
            for (NSHTTPCookie *cookie in cookies) {
                if ([strongSelf cookie:cookie appliesToURL:authorized.URL]) [applicableCookies addObject:cookie];
            }
            NSDictionary<NSString *, NSString *> *headers = [NSHTTPCookie requestHeaderFieldsWithCookies:applicableCookies];
            [headers enumerateKeysAndObjectsUsingBlock:^(NSString *name, NSString *value, BOOL *stop) {
                [authorized setValue:value forHTTPHeaderField:name];
            }];
            NSURLSessionConfiguration *sessionConfiguration = [NSURLSessionConfiguration ephemeralSessionConfiguration];
            sessionConfiguration.HTTPShouldSetCookies = NO;
            OptionHelperReportDownloadDelegate *sessionDelegate = [[OptionHelperReportDownloadDelegate alloc] init];
            sessionDelegate.allowsURL = ^BOOL(NSURL *redirectedURL) {
                OptionHelperAppDelegate *current = weakSelf;
                return current != nil && [current isAllowedReportArtifactURL:redirectedURL];
            };
            NSURLSession *session = [NSURLSession sessionWithConfiguration:sessionConfiguration delegate:sessionDelegate delegateQueue:nil];
            NSURLSessionDownloadTask *task = [session downloadTaskWithRequest:authorized
                completionHandler:^(NSURL *temporaryURL, NSURLResponse *response, NSError *error) {
                    [session finishTasksAndInvalidate];
                    NSHTTPURLResponse *httpResponse = [response isKindOfClass:NSHTTPURLResponse.class]
                        ? (NSHTTPURLResponse *)response
                        : nil;
                    NSString *mimeType = response.MIMEType.lowercaseString ?: @"";
                    NSSet<NSString *> *allowedMIMETypes = [NSSet setWithArray:
                        @[@"text/html", @"application/xhtml+xml", @"application/pdf"]];
                    NSNumber *fileSize = nil;
                    if (temporaryURL != nil) [temporaryURL getResourceValue:&fileSize forKey:NSURLFileSizeKey error:nil];
                    NSString *validationError = error.localizedDescription;
                    if (validationError == nil && httpResponse == nil) {
                        validationError = @"报告服务未返回HTTP响应。";
                    } else if (validationError == nil && ![strongSelf isAllowedReportArtifactURL:httpResponse.URL]) {
                        validationError = @"报告服务跳转到了不受信任的位置。";
                    } else if (validationError == nil && (httpResponse.statusCode < 200 || httpResponse.statusCode >= 300)) {
                        validationError = [NSString stringWithFormat:@"报告服务返回HTTP %ld。", (long)httpResponse.statusCode];
                    } else if (validationError == nil && ![allowedMIMETypes containsObject:mimeType]) {
                        validationError = @"报告服务返回了不受支持的文件类型。";
                    } else if (validationError == nil && (temporaryURL == nil || fileSize.unsignedLongLongValue == 0)) {
                        validationError = @"报告文件为空。";
                    }
                    dispatch_async(dispatch_get_main_queue(), ^{
                        OptionHelperAppDelegate *current = weakSelf;
                        if (current == nil) return;
                        if (validationError != nil || temporaryURL == nil) {
                            [current showReportDownloadFailure:validationError ?: @"未收到报告文件。"];
                            return;
                        }
                        NSFileManager *manager = [NSFileManager defaultManager];
                        NSError *fileError = nil;
                        if ([manager fileExistsAtPath:destination.path]) [manager removeItemAtURL:destination error:&fileError];
                        if (fileError == nil) [manager copyItemAtURL:temporaryURL toURL:destination error:&fileError];
                        if (fileError != nil) [current showReportDownloadFailure:fileError.localizedDescription];
                    });
                }];
            [task resume];
        }];
    }];
}

- (void)showReportPreview:(NSURLRequest *)request configuration:(WKWebViewConfiguration *)configuration {
    if (self.reportPreviewWebView == nil || self.reportPreviewWindow == nil) {
        WKWebViewConfiguration *isolatedConfiguration = [[WKWebViewConfiguration alloc] init];
        isolatedConfiguration.websiteDataStore = configuration.websiteDataStore;
        isolatedConfiguration.preferences.javaScriptCanOpenWindowsAutomatically = NO;
        self.reportPreviewWebView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:isolatedConfiguration];
        self.reportPreviewWebView.navigationDelegate = self;
        self.reportPreviewWebView.UIDelegate = self;
        self.reportPreviewWebView.allowsMagnification = YES;
        self.reportPreviewWindow = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 980, 760)
                                                               styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                                                                 backing:NSBackingStoreBuffered
                                                                   defer:NO];
        self.reportPreviewWindow.title = @"OptionHelper报告预览";
        self.reportPreviewWindow.delegate = self;
        self.reportPreviewWindow.contentView = self.reportPreviewWebView;
        [self.reportPreviewWindow center];
    }
    [self.reportPreviewWebView loadRequest:request];
    [self.reportPreviewWindow makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (WKWebView *)webView:(WKWebView *)webView
 createWebViewWithConfiguration:(WKWebViewConfiguration *)configuration
    forNavigationAction:(WKNavigationAction *)navigationAction
         windowFeatures:(WKWindowFeatures *)windowFeatures {
    if (navigationAction.targetFrame != nil) return nil;
    if (![self isAllowedReportArtifactURL:navigationAction.request.URL]) {
        NSBeep();
        return nil;
    }
    if ([self isReportDownloadURL:navigationAction.request.URL]) {
        [self downloadReportArtifact:navigationAction.request];
        return nil;
    }
    [self showReportPreview:navigationAction.request configuration:configuration];
    return nil;
}

- (void)webView:(WKWebView *)webView
runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters
initiatedByFrame:(WKFrameInfo *)frame
completionHandler:(void (^)(NSArray<NSURL *> * _Nullable URLs))completionHandler {
    if (webView != self.webView) {
        completionHandler(nil);
        return;
    }
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseFiles = YES;
    panel.canChooseDirectories = parameters.allowsDirectories;
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection;
    panel.canCreateDirectories = NO;
    panel.resolvesAliases = YES;
    void (^finish)(NSModalResponse) = ^(NSModalResponse response) {
        completionHandler(response == NSModalResponseOK ? panel.URLs : nil);
    };
    NSWindow *hostWindow = webView.window ?: self.window;
    if (hostWindow != nil) {
        [panel beginSheetModalForWindow:hostWindow completionHandler:finish];
    } else {
        [panel beginWithCompletionHandler:finish];
    }
}

- (void)webView:(WKWebView *)webView
 decidePolicyForNavigationAction:(WKNavigationAction *)navigationAction
 decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
    NSURL *url = navigationAction.request.URL;
    if (webView == self.webView && [self isSafeAppURL:url] && [url.path isEqualToString:@"/settings"]) {
        [self showSettingsCenter:navigationAction.request];
        decisionHandler(WKNavigationActionPolicyCancel);
        return;
    }
    if (webView == self.settingsWebView) {
        if ([self isSafeAppURL:url] && [url.path isEqualToString:@"/settings"]) {
            decisionHandler(WKNavigationActionPolicyAllow);
        } else if ([self isSafeAppURL:url]) {
            [self closeSettingsCenter:nil];
            decisionHandler(WKNavigationActionPolicyCancel);
        } else {
            decisionHandler(WKNavigationActionPolicyCancel);
        }
        return;
    }
    if (webView != self.reportPreviewWebView) {
        decisionHandler(WKNavigationActionPolicyAllow);
        return;
    }
    decisionHandler(([url.scheme isEqualToString:@"about"] || [self isAllowedReportArtifactURL:url])
        ? WKNavigationActionPolicyAllow
        : WKNavigationActionPolicyCancel);
}

- (void)windowDidResize:(NSNotification *)notification {
    if (notification.object != self.window) return;
    [self ensureTitlebarControlsForWindow:self.window];
    [self syncWorkspaceTitlebarControlsForURL:self.webView.URL];
}

- (void)windowDidBecomeKey:(NSNotification *)notification {
    if (notification.object != self.window) return;
    [self ensureTitlebarControlsForWindow:self.window];
    [self syncWorkspaceTitlebarControlsForURL:self.webView.URL];
}

- (BOOL)windowShouldClose:(NSWindow *)sender {
    if (sender == self.settingsWindow) {
        [self closeSettingsCenter:nil];
        return NO;
    }
    if (sender == self.reportPreviewWindow) {
        [sender orderOut:nil];
        return NO;
    }
    if (sender != self.window || self.closeAfterInterrupt || ([self.activeOperationState isEqualToString:@"ready"] && self.activeOperationCount == 0)) return YES;
    NSAlert *alert = [[NSAlert alloc] init];
    BOOL runtimeReady = [self.activeOperationState isEqualToString:@"ready"];
    alert.messageText = runtimeReady ? @"仍有任务正在运行" : @"正在确认后台任务状态";
    alert.informativeText = runtimeReady ? @"退出会停止这些运行，并将它们标记为中断。" : @"当前无法确认活动Operation数量。退出仍会请求停止后台运行。";
    [alert addButtonWithTitle:@"退出并停止"];
    [alert addButtonWithTitle:@"取消"];
    if ([alert runModal] != NSAlertFirstButtonReturn) return NO;
    self.closeAfterInterrupt = YES;
    [self.webView evaluateJavaScript:@"fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).catch(()=>{}).finally(()=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'shutdown_complete'}));" completionHandler:nil];
    return NO;
}

- (void)windowWillClose:(NSNotification *)notification {
    if (notification.object == self.window) [NSApp terminate:nil];
}

- (void)userContentController:(WKUserContentController *)userContentController didReceiveScriptMessage:(WKScriptMessage *)message {
    if ([message.name isEqualToString:@"optionhelperSession"] && [message.body isKindOfClass:[NSDictionary class]]
        && [message.body[@"type"] isEqualToString:@"signed_out"]) {
        [self cancelSettingsNavigationWatchdog];
        [self.settingsWebView stopLoading];
        [self.settingsWebView loadHTMLString:@"" baseURL:nil];
        [self.settingsWindow orderOut:nil];
        [self.reportPreviewWebView stopLoading];
        [self.reportPreviewWebView loadHTMLString:@"" baseURL:nil];
        [self.reportPreviewWindow orderOut:nil];
        return;
    }
    if ([message.name isEqualToString:@"optionhelperUIScale"] && [message.body isKindOfClass:[NSDictionary class]]) {
        NSDictionary *value = message.body;
        NSNumber *requestedScale = value[@"scale"];
        NSNumber *acceptedScale = [value[@"type"] isEqualToString:@"set_ui_scale"] && [requestedScale isKindOfClass:[NSNumber class]]
            ? [self validatedUIScale:requestedScale.doubleValue]
            : nil;
        if (acceptedScale != nil) [self applyUIScale:acceptedScale.doubleValue persist:YES notifyPage:YES];
        return;
    }
    if ([message.name isEqualToString:@"optionhelperRuntime"] && [message.body isKindOfClass:[NSDictionary class]]) {
        NSDictionary *runtime = (NSDictionary *)message.body;
        if ([runtime[@"type"] isEqualToString:@"active_operations"]) {
            NSString *state = [runtime[@"state"] isKindOfClass:NSString.class] ? runtime[@"state"] : nil;
            if ([runtime[@"count"] isKindOfClass:NSNumber.class] && [runtime[@"count"] integerValue] >= 0 && [state isEqualToString:@"ready"]) {
                self.activeOperationCount = MAX(0, [runtime[@"count"] integerValue]);
                self.activeOperationState = @"ready";
            } else {
                self.activeOperationState = @"recovering";
            }
        } else if ([runtime[@"type"] isEqualToString:@"shutdown_complete"]) {
            self.activeOperationCount = 0;
            self.activeOperationState = @"ready";
            [self.window performClose:nil];
        }
        return;
    }
    if (![message.name isEqualToString:@"optionhelperTheme"] || ![message.body isKindOfClass:[NSDictionary class]]) return;
    NSDictionary *value = (NSDictionary *)message.body;
    NSString *theme = value[@"theme"];
    NSString *preference = value[@"preference"];
    if (![@[@"light", @"dark", @"auto"] containsObject:preference] || ![@[@"light", @"dark"] containsObject:theme]) return;
    self.themePreference = preference;
    if ([preference isEqualToString:@"auto"]) {
        self.window.appearance = nil;
        self.settingsWindow.appearance = nil;
        NSAppearanceName appearance = [NSApp.effectiveAppearance bestMatchFromAppearancesWithNames:@[NSAppearanceNameDarkAqua, NSAppearanceNameAqua]];
        [self applyDockIcon:[appearance isEqualToString:NSAppearanceNameDarkAqua] ? @"dark" : @"light"];
        dispatch_async(dispatch_get_main_queue(), ^{
            [self.webView evaluateJavaScript:@"window.OptionHelperTheme?.refreshSystemTheme?.();" completionHandler:nil];
        });
    } else {
        self.window.appearance = [NSAppearance appearanceNamed:[theme isEqualToString:@"dark"] ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
        self.settingsWindow.appearance = self.window.appearance;
        [self applyDockIcon:theme];
    }
}

- (void)observeValueForKeyPath:(NSString *)keyPath
                      ofObject:(id)object
                        change:(NSDictionary<NSKeyValueChangeKey, id> *)change
                       context:(void *)context {
    if ([keyPath isEqualToString:@"effectiveAppearance"] && object == NSApp) {
        if (![self.themePreference isEqualToString:@"auto"]) return;
        NSAppearanceName appearance = [NSApp.effectiveAppearance bestMatchFromAppearancesWithNames:@[NSAppearanceNameDarkAqua, NSAppearanceNameAqua]];
        [self applyDockIcon:[appearance isEqualToString:NSAppearanceNameDarkAqua] ? @"dark" : @"light"];
        return;
    }
    [super observeValueForKeyPath:keyPath ofObject:object change:change context:context];
}

- (void)applyDockIcon:(NSString *)theme {
    if (![theme isEqualToString:@"dark"]) {
        NSApp.applicationIconImage = nil;
        return;
    }
    NSURL *path = [[[NSBundle mainBundle] resourceURL] URLByAppendingPathComponent:@"assets/icons/optionhelper-app-icon-tile-dark.icns"];
    NSApp.applicationIconImage = [[NSImage alloc] initWithContentsOfURL:path];
}

- (void)installMainMenu {
    NSMenu *mainMenu = [[NSMenu alloc] initWithTitle:@""];
    NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"OptionHelper"];
    NSMenuItem *appMenuItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    appMenuItem.submenu = appMenu;
    [appMenu addItemWithTitle:@"退出OptionHelper" action:@selector(terminate:) keyEquivalent:@"q"];
    [mainMenu addItem:appMenuItem];

    NSMenu *editMenu = [[NSMenu alloc] initWithTitle:@"编辑"];
    NSMenuItem *editMenuItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    editMenuItem.submenu = editMenu;
    [self addEditItem:@"撤销" action:@selector(undo:) key:@"z" modifiers:NSEventModifierFlagCommand toMenu:editMenu];
    [self addEditItem:@"重做" action:@selector(redo:) key:@"z" modifiers:(NSEventModifierFlagCommand | NSEventModifierFlagShift) toMenu:editMenu];
    [editMenu addItem:[NSMenuItem separatorItem]];
    [self addEditItem:@"剪切" action:@selector(cut:) key:@"x" modifiers:NSEventModifierFlagCommand toMenu:editMenu];
    [self addEditItem:@"复制" action:@selector(copy:) key:@"c" modifiers:NSEventModifierFlagCommand toMenu:editMenu];
    [self addEditItem:@"粘贴" action:@selector(paste:) key:@"v" modifiers:NSEventModifierFlagCommand toMenu:editMenu];
    [self addEditItem:@"全选" action:@selector(selectAll:) key:@"a" modifiers:NSEventModifierFlagCommand toMenu:editMenu];
    [mainMenu addItem:editMenuItem];

    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"显示"];
    NSMenuItem *viewMenuItem = [[NSMenuItem alloc] initWithTitle:@"" action:nil keyEquivalent:@""];
    viewMenuItem.submenu = viewMenu;
    self.zoomInMenuItem = [[NSMenuItem alloc] initWithTitle:@"放大" action:@selector(increaseUIScale:) keyEquivalent:@"+"];
    self.zoomInMenuItem.keyEquivalentModifierMask = NSEventModifierFlagCommand;
    self.zoomInMenuItem.target = self;
    [viewMenu addItem:self.zoomInMenuItem];
    self.zoomOutMenuItem = [[NSMenuItem alloc] initWithTitle:@"缩小" action:@selector(decreaseUIScale:) keyEquivalent:@"-"];
    self.zoomOutMenuItem.keyEquivalentModifierMask = NSEventModifierFlagCommand;
    self.zoomOutMenuItem.target = self;
    [viewMenu addItem:self.zoomOutMenuItem];
    [viewMenu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *actualSize = [[NSMenuItem alloc] initWithTitle:@"实际大小" action:@selector(resetUIScale:) keyEquivalent:@"0"];
    actualSize.keyEquivalentModifierMask = NSEventModifierFlagCommand;
    actualSize.target = self;
    [viewMenu addItem:actualSize];
    [mainMenu addItem:viewMenuItem];
    NSApp.mainMenu = mainMenu;
    [self updateScaleMenuAvailability];
}

- (NSArray<NSNumber *> *)uiScaleSteps {
    return @[@0.8, @0.9, @1.0, @1.1, @1.25, @1.4];
}

- (NSNumber *)validatedUIScale:(double)value {
    for (NSNumber *step in [self uiScaleSteps]) {
        if (fabs(step.doubleValue - value) < 0.0001) return step;
    }
    return nil;
}

- (CGFloat)loadUIScale {
    NSData *data = self.uiPreferencesURL == nil ? nil : [NSData dataWithContentsOfURL:self.uiPreferencesURL];
    if (data == nil) return 1.0;
    NSDictionary *object = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![object isKindOfClass:[NSDictionary class]] || [object[@"schema_version"] integerValue] != 1) return 1.0;
    NSNumber *accepted = [self validatedUIScale:[object[@"ui_scale"] doubleValue]];
    return accepted == nil ? 1.0 : accepted.doubleValue;
}

- (void)persistUIScale {
    if (self.uiPreferencesURL == nil) return;
    NSDictionary *object = @{@"schema_version": @1, @"ui_scale": @(self.uiScale)};
    NSData *data = [NSJSONSerialization dataWithJSONObject:object options:NSJSONWritingPrettyPrinted error:nil];
    [data writeToURL:self.uiPreferencesURL options:NSDataWritingAtomic error:nil];
}

- (void)applyUIScale:(CGFloat)scale persist:(BOOL)persist notifyPage:(BOOL)notifyPage {
    NSNumber *accepted = [self validatedUIScale:scale];
    if (accepted == nil) return;
    self.uiScale = accepted.doubleValue;
    self.webView.pageZoom = self.uiScale;
    self.settingsWebView.pageZoom = self.uiScale;
    if (persist) [self persistUIScale];
    [self updateScaleMenuAvailability];
    if (notifyPage) [self syncUIScaleToPage];
}

- (void)syncUIScaleToPage {
    if (self.webView == nil && self.settingsWebView == nil) return;
    NSString *script = [NSString stringWithFormat:@"document.documentElement.dataset.uiScale='%.4f';document.documentElement.style.setProperty('--native-titlebar-height','%.4fpx');document.documentElement.style.setProperty('--native-titlebar-collapsed-leading-safe-area','%.4fpx');window.OptionHelperUIScale?.syncFromNative?.(%.4f);", self.uiScale, 36.0 / self.uiScale, 166.0 / self.uiScale, self.uiScale];
    [self.webView evaluateJavaScript:script completionHandler:nil];
    [self.settingsWebView evaluateJavaScript:script completionHandler:nil];
}

- (void)changeUIScaleBy:(NSInteger)offset {
    NSArray<NSNumber *> *steps = [self uiScaleSteps];
    NSInteger current = [steps indexOfObject:@(self.uiScale)];
    if (current == NSNotFound) current = [steps indexOfObject:@1.0];
    NSInteger next = MAX(0, MIN((NSInteger)steps.count - 1, current + offset));
    [self applyUIScale:steps[next].doubleValue persist:YES notifyPage:YES];
}

- (void)updateScaleMenuAvailability {
    NSArray<NSNumber *> *steps = [self uiScaleSteps];
    self.zoomOutMenuItem.enabled = self.uiScale > steps.firstObject.doubleValue;
    self.zoomInMenuItem.enabled = self.uiScale < steps.lastObject.doubleValue;
}

- (void)increaseUIScale:(id)sender { [self changeUIScaleBy:1]; }
- (void)decreaseUIScale:(id)sender { [self changeUIScaleBy:-1]; }
- (void)resetUIScale:(id)sender { [self applyUIScale:1.0 persist:YES notifyPage:YES]; }

- (void)addEditItem:(NSString *)title action:(SEL)action key:(NSString *)key modifiers:(NSEventModifierFlags)modifiers toMenu:(NSMenu *)menu {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:action keyEquivalent:key];
    item.keyEquivalentModifierMask = modifiers;
    item.target = nil;
    [menu addItem:item];
}

- (void)showFailure:(NSString *)text {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"OptionHelper无法启动";
    alert.informativeText = text;
    [alert addButtonWithTitle:@"退出"];
    [alert runModal];
    [NSApp terminate:nil];
}

@end

#if defined(OPTIONHELPER_NATIVE_RECOVERY_PROBE)
static NSArray<NSButton *> *OptionHelperRecoveryButtons(NSView *root) {
    NSMutableArray<NSButton *> *buttons = [NSMutableArray array];
    if ([root isKindOfClass:NSButton.class]) [buttons addObject:(NSButton *)root];
    for (NSView *child in root.subviews) [buttons addObjectsFromArray:OptionHelperRecoveryButtons(child)];
    return buttons.copy;
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        OptionHelperAppDelegate *delegate = [[OptionHelperAppDelegate alloc] init];
        NSView *contentView = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 900, 640)];
        delegate.window = [[NSWindow alloc] initWithContentRect:contentView.bounds
                                                     styleMask:NSWindowStyleMaskTitled
                                                       backing:NSBackingStoreBuffered
                                                         defer:NO];
        delegate.window.contentView = contentView;
        delegate.webView = [[WKWebView alloc] initWithFrame:contentView.bounds];
        [contentView addSubview:delegate.webView];
        delegate.appOrigin = [NSURL URLWithString:@"http://127.0.0.1:61000/"];
        delegate.lastSafeURL = [NSURL URLWithString:@"http://127.0.0.1:61000/optdesk"];
        [delegate showNavigationFailure:@"probe-detail"];
        if (delegate.navigationRecoveryView == nil || delegate.navigationRecoveryView.hidden) return 2;
        if (![delegate.navigationRecoveryDetail.stringValue isEqualToString:@"probe-detail"]) return 3;
        NSSet<NSString *> *titles = [NSSet setWithArray:[OptionHelperRecoveryButtons(delegate.navigationRecoveryView) valueForKey:@"title"]];
        if (![titles containsObject:@"重新打开上一个页面"] || ![titles containsObject:@"返回工作台"]) return 4;
        if (delegate.navigationRecoveryView.superview != contentView) return 5;
        puts("native-navigation-recovery=ok");
        NSString *healthScript = [delegate visibleRootHealthScript];
        if ([healthScript rangeOfString:@"body.login-page"].location == NSNotFound
            || [healthScript rangeOfString:@"elementFromPoint"].location != NSNotFound) return 6;
        puts("native-login-root-health=ok");
        OptionHelperPresentationDecision firstCheck = OptionHelperPresentationDecisionForAttempt(NO, NO, 0, YES);
        OptionHelperPresentationDecision laterCheck = OptionHelperPresentationDecisionForAttempt(YES, YES, 1, YES);
        OptionHelperPresentationDecision exhausted = OptionHelperPresentationDecisionForAttempt(NO, NO, presentationMaxAttempts - 1, YES);
        OptionHelperPresentationDecision timedOut = OptionHelperPresentationDecisionForAttempt(NO, NO, 0, NO);
        if (firstCheck != OptionHelperPresentationDecisionRetry
            || laterCheck != OptionHelperPresentationDecisionReady
            || exhausted != OptionHelperPresentationDecisionFailed
            || timedOut != OptionHelperPresentationDecisionFailed) return 7;
        puts("native-presentation-retry=ok");
        NSURL *settingsURL = [NSURL URLWithString:@"http://127.0.0.1:61000/settings?return_to=%2Foptdesk"];
        [delegate showSettingsCenter:[NSURLRequest requestWithURL:settingsURL]];
        if (delegate.settingsWindow == nil || delegate.settingsWebView == nil) return 8;
        if (delegate.settingsWebView == delegate.webView || !delegate.settingsWindow.opaque) return 9;
        if (delegate.settingsRecoveryView == nil || delegate.settingsRecoveryView.hidden) return 10;
        if (![delegate.settingsRecoveryDetail.stringValue containsString:@"确认设置页面"]) return 11;
        puts("native-settings-isolation=ok");
        [delegate hideSettingsFailure];
        if (!delegate.settingsRecoveryView.hidden || delegate.settingsWindow.firstResponder != delegate.settingsWebView) return 12;
        puts("native-settings-success=ok");
    }
    return 0;
}
#else
int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        OptionHelperAppDelegate *delegate = [[OptionHelperAppDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
#endif
