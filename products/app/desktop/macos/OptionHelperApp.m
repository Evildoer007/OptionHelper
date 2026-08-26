#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

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

@interface OptionHelperAppDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate>
@property(nonatomic, strong) NSTask *backend;
@property(nonatomic, strong) NSPipe *startupPipe;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) OptionHelperTitlebarDragView *titlebarDragView;
@property(nonatomic, strong) NSButton *railToggle;
@property(nonatomic, strong) NSButton *reportToggle;
@property(nonatomic) BOOL loadedURL;
@property(nonatomic, strong) NSMutableString *startupOutput;
@property(nonatomic, copy) NSString *themePreference;
@property(nonatomic) NSInteger activeOperationCount;
@property(nonatomic) BOOL closeAfterInterrupt;
@end

@implementation OptionHelperAppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [self installMainMenu];
    self.startupOutput = [NSMutableString string];
    self.themePreference = @"light";
    [NSApp addObserver:self
            forKeyPath:@"effectiveAppearance"
               options:NSKeyValueObservingOptionNew
               context:NULL];
    [self startBackend];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    [NSApp removeObserver:self forKeyPath:@"effectiveAppearance"];
    if (self.backend.running) {
        [self.backend terminate];
    }
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

    self.backend = [[NSTask alloc] init];
    self.backend.executableURL = [NSURL fileURLWithPath:backendPath];
    self.backend.currentDirectoryURL = [NSURL fileURLWithPath:[backendPath stringByDeletingLastPathComponent]];
    self.backend.arguments = @[@"--host", @"127.0.0.1", @"--port", @"0", @"--data-dir", support.path,
                               @"--resource-dir", resources.path];
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
    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    // Show the splash before the login page's module graph is evaluated.  The
    // login module owns the timer and always removes this class again.
    WKUserScript *startupScript = [[WKUserScript alloc] initWithSource:@"document.documentElement.dataset.nativeShell='macos';if (location.pathname === '/') document.documentElement.classList.add('login-boot');"
                                                         injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                      forMainFrameOnly:YES];
    WKUserScript *runtimeStatusScript = [[WKUserScript alloc] initWithSource:@"(()=>{const post=(count)=>window.webkit?.messageHandlers?.optionhelperRuntime?.postMessage({type:'active_operations',count});const refresh=()=>fetch('/api/runtime/active-operations').then((response)=>response.ok?response.json():{active_count:0}).then((value)=>post(Number(value.active_count)||0)).catch(()=>post(0));addEventListener('pageshow',refresh);setInterval(refresh,1400);refresh();})();"
                                                              injectionTime:WKUserScriptInjectionTimeAtDocumentEnd
                                                           forMainFrameOnly:YES];
    [configuration.userContentController addUserScript:startupScript];
    [configuration.userContentController addUserScript:runtimeStatusScript];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperTheme"];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperRuntime"];
    WKWebView *webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    webView.navigationDelegate = self;
    self.window = [[OptionHelperWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1320, 860)
                                               styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable | NSWindowStyleMaskFullSizeContentView)
                                                 backing:NSBackingStoreBuffered
                                                   defer:NO];
    self.window.title = @"OptionHelper";
    self.window.titleVisibility = NSWindowTitleHidden;
    self.window.titlebarAppearsTransparent = YES;
    self.window.titlebarSeparatorStyle = NSTitlebarSeparatorStyleNone;
    self.window.movableByWindowBackground = YES;
    self.window.delegate = self;
    self.window.contentView = webView;
    [self.window makeFirstResponder:webView];
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    self.webView = webView;
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
    self.railToggle.hidden = !showsWorkspaceControls;
    self.reportToggle.hidden = !showsWorkspaceControls;
    [self layoutTitlebarControls];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    [self ensureTitlebarControlsForWindow:self.window];
    [self syncWorkspaceTitlebarControlsForURL:webView.URL];
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
    if (sender != self.window || self.closeAfterInterrupt || self.activeOperationCount == 0) return YES;
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"仍有任务正在运行";
    alert.informativeText = @"退出会停止这些运行，并将它们标记为中断。";
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
    if ([message.name isEqualToString:@"optionhelperRuntime"] && [message.body isKindOfClass:[NSDictionary class]]) {
        NSDictionary *runtime = (NSDictionary *)message.body;
        if ([runtime[@"type"] isEqualToString:@"active_operations"] && [runtime[@"count"] isKindOfClass:NSNumber.class]) {
            self.activeOperationCount = MAX(0, [runtime[@"count"] integerValue]);
        } else if ([runtime[@"type"] isEqualToString:@"shutdown_complete"]) {
            self.activeOperationCount = 0;
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
        NSAppearanceName appearance = [NSApp.effectiveAppearance bestMatchFromAppearancesWithNames:@[NSAppearanceNameDarkAqua, NSAppearanceNameAqua]];
        [self applyDockIcon:[appearance isEqualToString:NSAppearanceNameDarkAqua] ? @"dark" : @"light"];
        dispatch_async(dispatch_get_main_queue(), ^{
            [self.webView evaluateJavaScript:@"window.OptionHelperTheme?.refreshSystemTheme?.();" completionHandler:nil];
        });
    } else {
        self.window.appearance = [NSAppearance appearanceNamed:[theme isEqualToString:@"dark"] ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
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
    NSApp.mainMenu = mainMenu;
}

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

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        OptionHelperAppDelegate *delegate = [[OptionHelperAppDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
