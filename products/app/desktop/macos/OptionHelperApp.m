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

@interface OptionHelperAppDelegate : NSObject <NSApplicationDelegate, WKScriptMessageHandler>
@property(nonatomic, strong) NSTask *backend;
@property(nonatomic, strong) NSPipe *startupPipe;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic) BOOL loadedURL;
@property(nonatomic, strong) NSMutableString *startupOutput;
@property(nonatomic, copy) NSString *themePreference;
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
    NSURL *support = [[[NSFileManager defaultManager] URLsForDirectory:NSApplicationSupportDirectory
                                                               inDomains:NSUserDomainMask] firstObject];
    support = [[support URLByAppendingPathComponent:@"OptionHelper" isDirectory:YES]
                   URLByAppendingPathComponent:@"local-state" isDirectory:YES];
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
    WKUserScript *startupScript = [[WKUserScript alloc] initWithSource:@"if (location.pathname === '/') document.documentElement.classList.add('login-boot');"
                                                         injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                      forMainFrameOnly:YES];
    [configuration.userContentController addUserScript:startupScript];
    [configuration.userContentController addScriptMessageHandler:self name:@"optionhelperTheme"];
    WKWebView *webView = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    self.window = [[OptionHelperWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1320, 860)
                                               styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                                                 backing:NSBackingStoreBuffered
                                                   defer:NO];
    self.window.title = @"OptionHelper";
    self.window.contentView = webView;
    [self.window makeFirstResponder:webView];
    [self.window center];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    [webView loadRequest:[NSURLRequest requestWithURL:startupURL]];
}

- (void)userContentController:(WKUserContentController *)userContentController didReceiveScriptMessage:(WKScriptMessage *)message {
    if (![message.name isEqualToString:@"optionhelperTheme"] || ![message.body isKindOfClass:[NSDictionary class]]) return;
    NSDictionary *value = (NSDictionary *)message.body;
    NSString *theme = value[@"theme"];
    NSString *preference = value[@"preference"];
    if (![@[@"light", @"dark", @"auto"] containsObject:preference] || ![@[@"light", @"dark"] containsObject:theme]) return;
    self.themePreference = preference;
    [self applyDockIcon:theme];
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
