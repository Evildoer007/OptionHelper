#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <math.h>

@interface GeometryProbe : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@property(nonatomic,strong) NSURL *baseURL;
@property(nonatomic,strong) NSURL *outputDirectory;
@property(nonatomic,strong) NSWindow *window;
@property(nonatomic,strong) WKWebView *webView;
@property(nonatomic,assign) BOOL signedIn;
@property(nonatomic,strong) NSDictionary *chatGeometry;
@property(nonatomic,strong) NSDictionary *deskGeometry;
@property(nonatomic,strong) NSDictionary *returnChatGeometry;
@property(nonatomic,strong) NSDictionary *collapsedChat;
@property(nonatomic,strong) NSDictionary *collapsedDesk;
@property(nonatomic,strong) NSMutableDictionary *moduleAudit;
@property(nonatomic,assign) int exitCode;
@end

@implementation GeometryProbe

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [[NSFileManager defaultManager] createDirectoryAtURL:self.outputDirectory withIntermediateDirectories:YES attributes:nil error:nil];
    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    configuration.websiteDataStore = [WKWebsiteDataStore nonPersistentDataStore];
    NSRect frame = NSMakeRect(0, 0, 1440, 900);
    self.webView = [[WKWebView alloc] initWithFrame:frame configuration:configuration];
    self.webView.navigationDelegate = self;
    self.window = [[NSWindow alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskBorderless backing:NSBackingStoreBuffered defer:NO];
    self.window.contentView = self.webView;
    [self.window setFrameOrigin:NSMakePoint(80, 80)];
    [self.window orderFront:nil];
    [self.webView loadRequest:[NSURLRequest requestWithURL:self.baseURL]];
}

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    NSString *path = webView.URL.path ?: @"";
    if ([path isEqualToString:@"/"] && !self.signedIn) {
        self.signedIn = YES;
        NSString *script = @"fetch('/api/auth/local',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:'admin',principal_label:'WebKit QA'})}).then(response=>{if(!response.ok)throw new Error('login failed');location.assign('/optchat');});";
        [webView evaluateJavaScript:script completionHandler:nil];
    } else if ([path isEqualToString:@"/optchat"]) {
        [self waitForWorkspace:0];
    }
}

- (void)waitForWorkspace:(NSInteger)attempt {
    NSString *ready = @"Boolean(document.querySelector('button[data-mode=\\\"desk\\\"]'))&&document.querySelector('[data-workspace-shell]')?.dataset.layoutReady==='true'";
    [self.webView evaluateJavaScript:ready completionHandler:^(id value, NSError *error) {
        if ([value boolValue]) [self captureChat];
        else if (attempt < 80) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self waitForWorkspace:attempt + 1]; });
        else [self diagnoseWorkspace];
    }];
}

- (void)diagnoseWorkspace {
    NSString *script = @"JSON.stringify({url:location.href,title:document.title,deskButtons:document.querySelectorAll('button[data-mode=\\\"desk\\\"]').length,shellDataset:document.querySelector('[data-workspace-shell]')?.dataset?Object.fromEntries(Object.entries(document.querySelector('[data-workspace-shell]').dataset)):{},body:document.body.innerText.slice(0,500)})";
    [self.webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        NSString *detail = [value isKindOfClass:NSString.class] ? value : (error.localizedDescription ?: @"unknown");
        [self finishWithPayload:nil error:[NSString stringWithFormat:@"workspace did not become ready: %@", detail]];
    }];
}

- (void)readGeometry:(void (^)(NSDictionary *))completion {
    NSString *script = @"JSON.stringify(Object.fromEntries(Object.entries({rail:'.workspace-rail',modeSwitch:'[data-mode-switch]',newTask:'[data-new-task]'}).map(([key,selector])=>{const element=document.querySelector(selector),rect=element.getBoundingClientRect(),style=getComputedStyle(element);return[key,{x:rect.x,y:rect.y,width:rect.width,height:rect.height,style:{backgroundColor:style.backgroundColor,color:style.color,borderTopColor:style.borderTopColor,borderRightColor:style.borderRightColor,borderBottomColor:style.borderBottomColor,borderLeftColor:style.borderLeftColor,borderRadius:style.borderRadius,paddingTop:style.paddingTop,paddingRight:style.paddingRight,paddingBottom:style.paddingBottom,paddingLeft:style.paddingLeft,opacity:style.opacity}}];})))";
    [self.webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        NSData *data = [value isKindOfClass:NSString.class] ? [value dataUsingEncoding:NSUTF8StringEncoding] : nil;
        NSDictionary *result = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (!result || error) [self finishWithPayload:nil error:[NSString stringWithFormat:@"geometry read failed: %@", error.localizedDescription ?: @"unknown"]];
        else completion(result);
    }];
}

- (void)captureChat {
    [self readGeometry:^(NSDictionary *geometry) {
        self.chatGeometry = geometry;
        [self snapshot:@"workspace-chat.png" completion:^{
            [self.webView evaluateJavaScript:@"document.querySelector('button[data-mode=\\\"desk\\\"]').click()" completionHandler:nil];
            [self waitForDesk:0];
        }];
    }];
}

- (void)waitForDesk:(NSInteger)attempt {
    NSString *ready = @"document.querySelector('[data-workspace-shell]')?.dataset.mode==='desk'&&document.querySelector('[data-workspace-shell]')?.dataset.switching==='false'";
    [self.webView evaluateJavaScript:ready completionHandler:^(id value, NSError *error) {
        if ([value boolValue]) [self captureDesk];
        else if (attempt < 80) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self waitForDesk:attempt + 1]; });
        else [self finishWithPayload:nil error:@"desk transition did not finish"];
    }];
}

- (void)captureDesk {
    [self readGeometry:^(NSDictionary *geometry) {
        self.deskGeometry = geometry;
        [self snapshot:@"workspace-desk.png" completion:^{
            self.moduleAudit = [NSMutableDictionary dictionary];
            [self auditModuleAtIndex:0];
        }];
    }];
}

- (void)auditModuleAtIndex:(NSInteger)index {
    NSArray<NSDictionary *> *modules = @[
        @{@"name": @"payoffer", @"selector": @"#productSelect"},
        @{@"name": @"pricer", @"selector": @"#productId"},
        @{@"name": @"backtester", @"selector": @"#productId"},
        @{@"name": @"reporter", @"selector": @".settings-panel .choice input"}
    ];
    if (index >= modules.count) {
        [self.webView evaluateJavaScript:@"document.querySelector('button[data-mode=\"chat\"]').click()" completionHandler:nil];
        [self waitForChatReturn:0];
        return;
    }
    NSDictionary *module = modules[index];
    NSString *name = module[@"name"];
    NSString *click = [NSString stringWithFormat:@"document.querySelector('[data-module=\"%@\"]')?.click()", name];
    [self.webView evaluateJavaScript:click completionHandler:^(id value, NSError *error) {
        if (error) { [self finishWithPayload:nil error:[NSString stringWithFormat:@"%@ module click failed", name]]; return; }
        [self waitForModule:module index:index attempt:0];
    }];
}

- (void)waitForModule:(NSDictionary *)module index:(NSInteger)index attempt:(NSInteger)attempt {
    NSString *name = module[@"name"];
    NSString *selector = module[@"selector"];
    NSString *ready = [NSString stringWithFormat:
        @"(()=>{const f=document.querySelector('iframe[src*=\"/%@/%@.html\"]');const d=f?.contentDocument;const e=d?.querySelector('%@');return Boolean(d?.readyState==='complete'&&e);})()",
        name, name, selector];
    [self.webView evaluateJavaScript:ready completionHandler:^(id value, NSError *error) {
        if ([value boolValue]) [self readModuleAudit:module index:index];
        else if (attempt < 80) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self waitForModule:module index:index attempt:attempt + 1]; });
        else [self finishWithPayload:nil error:[NSString stringWithFormat:@"%@ module did not become ready", name]];
    }];
}

- (void)readModuleAudit:(NSDictionary *)module index:(NSInteger)index {
    NSString *name = module[@"name"];
    NSString *script = nil;
    if ([name isEqual:@"reporter"]) {
        script = @"(()=>{const d=document.querySelector('iframe[src*=\"/reporter/reporter.html\"]').contentDocument;const noMotion=d.createElement('style');noMotion.textContent='.settings-panel .choice input{transition:none!important}';d.head.append(noMotion);const audit=t=>{const e=d.querySelector(`.settings-panel .choice input[type=${t}]`),l=e.closest('.choice'),read=()=>{void e.offsetWidth;const s=getComputedStyle(e);return{appearance:s.appearance,webkitAppearance:s.webkitAppearance,width:s.width,height:s.height,borderColor:s.borderColor,backgroundColor:s.backgroundColor,boxShadow:s.boxShadow,outlineStyle:s.outlineStyle,outlineColor:s.outlineColor,cursor:s.cursor,checked:e.checked,disabled:e.disabled};};e.checked=false;e.disabled=false;e.removeAttribute('aria-invalid');delete l.dataset.visualState;const base=read();l.dataset.visualState='hover';const hover=read();l.dataset.visualState='focus';const focus=read();delete l.dataset.visualState;e.checked=true;const checked=read();e.checked=false;e.disabled=true;const disabled=read();e.disabled=false;e.setAttribute('aria-invalid','true');const error=read();e.removeAttribute('aria-invalid');return{base,hover,focus,checked,disabled,error};};const result={radio:audit('radio'),checkbox:audit('checkbox')};noMotion.remove();return JSON.stringify(result);})()";
    } else {
        NSString *selector = module[@"selector"];
        script = [NSString stringWithFormat:@"(()=>{const d=document.querySelector('iframe[src*=\"/%@/%@.html\"]').contentDocument;const e=d.querySelector('%@');return JSON.stringify({value:e.value,firstText:e.options[0]?.textContent.trim(),firstValue:e.options[0]?.value,optionCount:e.options.length});})()", name, name, selector];
    }
    [self.webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        NSData *data = [value isKindOfClass:NSString.class] ? [value dataUsingEncoding:NSUTF8StringEncoding] : nil;
        NSDictionary *result = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (!result || error) { [self finishWithPayload:nil error:[NSString stringWithFormat:@"%@ audit failed", name]]; return; }
        if (![name isEqual:@"reporter"]) {
            self.moduleAudit[name] = result;
            [self auditModuleAtIndex:index + 1];
            return;
        }
        [self readReporterChoiceGeometry:^(NSDictionary *geometry) {
            NSMutableDictionary *combined = [result mutableCopy];
            combined[@"geometry"] = geometry;
            self.moduleAudit[name] = combined;
            [self snapshot:@"workspace-reporter-controls.png" completion:^{ [self auditModuleAtIndex:index + 1]; }];
        }];
    }];
}

- (void)readReporterChoiceGeometry:(void (^)(NSDictionary *))completion {
    NSString *script = @"(()=>{try{const d=document.querySelector('iframe[src*=\"/reporter/reporter.html\"]')?.contentDocument;const rows=d?[...d.querySelectorAll('.settings-panel .choice')]:[];const one=(row)=>{const input=row.querySelector('input'),text=row.querySelector('span'),rr=row.getBoundingClientRect(),ir=input.getBoundingClientRect(),tr=text.getBoundingClientRect(),s=getComputedStyle(row);return{display:s.display,columns:s.gridTemplateColumns,gap:s.columnGap,minHeight:s.minHeight,rowHeight:rr.height,inputWidth:ir.width,inputHeight:ir.height,centerDelta:Math.abs((ir.top+ir.height/2)-(rr.top+rr.height/2)),textDelta:Math.abs((tr.top+tr.height/2)-(rr.top+rr.height/2))};};const input=rows[0]?.querySelector('input');if(!input)return JSON.stringify({rows:[],states:{}});const base=one(rows[0]);input.checked=!input.checked;const checked=one(rows[0]);input.disabled=true;const disabled=one(rows[0]);input.disabled=false;input.setAttribute('aria-invalid','true');const error=one(rows[0]);input.removeAttribute('aria-invalid');return JSON.stringify({rows:rows.map(one),states:{base,checked,disabled,error}})}catch(error){return JSON.stringify({error:String(error)})}})()";
    [self.webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        NSData *data = [value isKindOfClass:NSString.class] ? [value dataUsingEncoding:NSUTF8StringEncoding] : nil;
        NSDictionary *geometry = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (!geometry || error) [self finishWithPayload:nil error:[NSString stringWithFormat:@"reporter choice geometry read failed: %@", error.localizedDescription ?: @"invalid result"]];
        else if (geometry[@"error"]) [self finishWithPayload:nil error:[NSString stringWithFormat:@"reporter choice geometry script failed: %@", geometry[@"error"]]];
        else completion(geometry);
    }];
}

- (void)waitForChatReturn:(NSInteger)attempt {
    NSString *ready = @"document.querySelector('[data-workspace-shell]')?.dataset.mode==='chat'&&document.querySelector('[data-workspace-shell]')?.dataset.switching==='false'";
    [self.webView evaluateJavaScript:ready completionHandler:^(id value, NSError *error) {
        if ([value boolValue]) [self captureChatReturn];
        else if (attempt < 80) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self waitForChatReturn:attempt + 1]; });
        else [self finishWithPayload:nil error:@"return chat transition did not finish"];
    }];
}

- (void)captureChatReturn {
    [self readGeometry:^(NSDictionary *geometry) {
        self.returnChatGeometry = geometry;
        [self snapshot:@"workspace-chat-return.png" completion:^{
            [self.webView evaluateJavaScript:@"document.querySelector('[data-rail-collapse-toggle]').click()" completionHandler:nil];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 350 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self captureCollapsedChat]; });
        }];
    }];
}

- (void)readCollapsedRail:(void (^)(NSDictionary *))completion {
    NSString *script = @"(()=>{const shell=document.querySelector('[data-workspace-shell]'),rail=document.querySelector('.workspace-rail'),newTask=document.querySelector('[data-new-task]'),read=(node)=>{const r=node.getBoundingClientRect(),s=getComputedStyle(node);return{width:r.width,height:r.height,backgroundColor:s.backgroundColor,borderRadius:s.borderRadius,borderTopColor:s.borderTopColor,borderRightColor:s.borderRightColor,borderBottomColor:s.borderBottomColor,borderLeftColor:s.borderLeftColor};},rs=getComputedStyle(rail);return JSON.stringify({collapsed:shell.dataset.railCollapsed,brandPresent:Boolean(document.querySelector('.workspace-brand')),newTask:read(newTask),railContentWidth:rail.clientWidth-parseFloat(rs.paddingLeft)-parseFloat(rs.paddingRight)})})()";
    [self.webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        NSData *data = [value isKindOfClass:NSString.class] ? [value dataUsingEncoding:NSUTF8StringEncoding] : nil;
        NSDictionary *collapsed = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        if (!collapsed || error) [self finishWithPayload:nil error:@"collapsed state read failed"];
        else completion(collapsed);
    }];
}

- (void)captureCollapsedChat {
    [self readCollapsedRail:^(NSDictionary *collapsed) {
        self.collapsedChat = collapsed;
        [self snapshot:@"workspace-chat-collapsed.png" completion:^{
            [self.webView evaluateJavaScript:@"document.querySelector('button[data-mode=\"desk\"]').click()" completionHandler:nil];
            [self waitForCollapsedDesk:0];
        }];
    }];
}

- (void)waitForCollapsedDesk:(NSInteger)attempt {
    NSString *ready = @"document.querySelector('[data-workspace-shell]')?.dataset.mode==='desk'&&document.querySelector('[data-workspace-shell]')?.dataset.switching==='false'&&document.querySelector('[data-workspace-shell]')?.dataset.railCollapsed==='true'";
    [self.webView evaluateJavaScript:ready completionHandler:^(id value, NSError *error) {
        if ([value boolValue]) [self captureCollapsedDesk];
        else if (attempt < 80) dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [self waitForCollapsedDesk:attempt + 1]; });
        else [self finishWithPayload:nil error:@"collapsed desk transition did not finish"];
    }];
}

- (void)captureCollapsedDesk {
    [self readCollapsedRail:^(NSDictionary *collapsed) {
        self.collapsedDesk = collapsed;
        [self snapshot:@"workspace-desk-collapsed.png" completion:^{
            double maxDelta = [self maximumGeometryDelta];
            NSArray *styleDrift = [self persistentStyleDrift];
            NSDictionary *payload = @{
                @"chat": self.chatGeometry ?: @{},
                @"desk": self.deskGeometry ?: @{},
                @"chatReturn": self.returnChatGeometry ?: @{},
                @"maximumGeometryDelta": @(maxDelta),
                @"persistentStyleDrift": styleDrift,
                @"moduleAudit": self.moduleAudit ?: @{},
                @"collapsedChat": self.collapsedChat ?: @{},
                @"collapsedDesk": self.collapsedDesk ?: @{},
                @"screenshots": @[
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-chat.png"] path],
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-desk.png"] path],
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-chat-return.png"] path],
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-chat-collapsed.png"] path],
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-desk-collapsed.png"] path],
                    [[self.outputDirectory URLByAppendingPathComponent:@"workspace-reporter-controls.png"] path]
                ]
            };
            BOOL compactValid = [self collapsedRailIsValid:self.collapsedChat] && [self collapsedRailIsValid:self.collapsedDesk];
            NSString *gateError = nil;
            if (maxDelta > 0.1) gateError = [NSString stringWithFormat:@"persistent rail geometry moved by %.3fpx", maxDelta];
            else if (!compactValid) gateError = @"collapsed rail did not preserve the circular mark and continuous new-task target";
            else gateError = [self moduleAuditError];
            [self finishWithPayload:payload error:gateError];
        }];
    }];
}

- (BOOL)collapsedRailIsValid:(NSDictionary *)collapsed {
    NSDictionary *newTask = collapsed[@"newTask"] ?: @{};
    double taskWidth = [newTask[@"width"] doubleValue];
    return [collapsed[@"collapsed"] isEqual:@"true"]
        && ![collapsed[@"brandPresent"] boolValue]
        && fabs(taskWidth - [collapsed[@"railContentWidth"] doubleValue]) < .6
        && [newTask[@"backgroundColor"] isEqual:@"rgba(0, 0, 0, 0)"];
}

- (NSString *)moduleAuditError {
    for (NSString *name in @[@"payoffer", @"pricer", @"backtester"]) {
        NSDictionary *result = self.moduleAudit[name];
        if (![result[@"value"] isEqual:@""] || ![result[@"firstValue"] isEqual:@""] || ![result[@"firstText"] isEqual:@"请选择产品"])
            return [NSString stringWithFormat:@"%@ selected a product before user input", name];
    }
    NSDictionary *reporter = self.moduleAudit[@"reporter"];
        for (NSString *kind in @[@"radio", @"checkbox"]) {
        NSDictionary *states = reporter[kind];
        NSDictionary *control = states[@"base"];
        NSString *appearance = control[@"webkitAppearance"] ?: control[@"appearance"];
        if (![appearance isEqual:@"none"]) return [NSString stringWithFormat:@"reporter %@ still uses native appearance", kind];
        if ([states[@"hover"][@"borderColor"] isEqual:control[@"borderColor"]]) return [NSString stringWithFormat:@"reporter %@ hover state is not visible", kind];
        if ([states[@"focus"][@"outlineStyle"] isEqual:@"none"]) return [NSString stringWithFormat:@"reporter %@ focus state is not visible", kind];
        if ([states[@"checked"][@"backgroundColor"] isEqual:control[@"backgroundColor"]]) return [NSString stringWithFormat:@"reporter %@ checked state is not visible", kind];
        if (![states[@"disabled"][@"cursor"] isEqual:@"not-allowed"]) return [NSString stringWithFormat:@"reporter %@ disabled state is not visible", kind];
        if ([states[@"error"][@"borderColor"] isEqual:control[@"borderColor"]]) return [NSString stringWithFormat:@"reporter %@ error state is not visible", kind];
        }
    NSDictionary *geometry = reporter[@"geometry"];
    for (NSDictionary *row in geometry[@"rows"] ?: @[]) {
        if (![row[@"display"] isEqual:@"grid"] || [row[@"inputWidth"] doubleValue] != 16 || [row[@"inputHeight"] doubleValue] != 16)
            return @"reporter delivery control geometry is not fixed at 16px";
        if (fabs([row[@"gap"] doubleValue] - 8) > .1 || [row[@"rowHeight"] doubleValue] < 32 || [row[@"centerDelta"] doubleValue] > .6 || [row[@"textDelta"] doubleValue] > .6)
            return @"reporter delivery control and label are not vertically aligned";
    }
    NSDictionary *states = geometry[@"states"];
    NSDictionary *base = states[@"base"];
    for (NSDictionary *state in @[states[@"checked"] ?: @{}, states[@"disabled"] ?: @{}, states[@"error"] ?: @{}]) {
        if (fabs([state[@"rowHeight"] doubleValue] - [base[@"rowHeight"] doubleValue]) > .1 || fabs([state[@"centerDelta"] doubleValue] - [base[@"centerDelta"] doubleValue]) > .1)
            return @"reporter delivery control jumps between states";
    }
    return nil;
}

- (NSArray<NSString *> *)persistentStyleDrift {
    NSMutableArray<NSString *> *drift = [NSMutableArray array];
    for (NSDictionary *comparison in @[self.deskGeometry ?: @{}, self.returnChatGeometry ?: @{}]) {
        for (NSString *element in @[@"rail", @"modeSwitch", @"newTask"]) {
            NSDictionary *baseline = self.chatGeometry[element][@"style"] ?: @{};
            NSDictionary *other = comparison[element][@"style"] ?: @{};
            for (NSString *field in baseline) {
                if (![baseline[field] isEqual:other[field]]) [drift addObject:[NSString stringWithFormat:@"%@.%@", element, field]];
            }
        }
    }
    return [[NSOrderedSet orderedSetWithArray:drift] array];
}

- (double)maximumGeometryDelta {
    double maximum = 0;
    for (NSDictionary *comparison in @[self.deskGeometry ?: @{}, self.returnChatGeometry ?: @{}]) {
        for (NSString *element in @[@"rail", @"modeSwitch", @"newTask"]) {
            NSDictionary *chatRect = self.chatGeometry[element];
            NSDictionary *otherRect = comparison[element];
            for (NSString *field in @[@"x", @"y", @"width", @"height"]) {
                double delta = fabs([chatRect[field] doubleValue] - [otherRect[field] doubleValue]);
                maximum = MAX(maximum, delta);
            }
        }
    }
    return maximum;
}

- (void)snapshot:(NSString *)name completion:(void (^)(void))completion {
    [self.webView takeSnapshotWithConfiguration:nil completionHandler:^(NSImage *image, NSError *error) {
        NSData *tiff = image.TIFFRepresentation;
        NSBitmapImageRep *bitmap = tiff ? [[NSBitmapImageRep alloc] initWithData:tiff] : nil;
        NSData *png = [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
        if (!png || error) { [self finishWithPayload:nil error:[NSString stringWithFormat:@"snapshot failed: %@", error.localizedDescription ?: @"unknown"]]; return; }
        [png writeToURL:[self.outputDirectory URLByAppendingPathComponent:name] options:NSDataWritingAtomic error:nil];
        completion();
    }];
}

- (void)finishWithPayload:(NSDictionary *)payload error:(NSString *)error {
    self.exitCode = error ? 1 : 0;
    NSDictionary *result = error ? @{@"error": error, @"evidence": payload ?: @{}} : (payload ?: @{});
    NSData *data = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:nil];
    fwrite(data.bytes, 1, data.length, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    [NSApp terminate:nil];
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fputs("usage: WorkspaceGeometryProbe <base-url> <output-directory>\n", stderr);
            return 2;
        }
        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyProhibited];
        GeometryProbe *delegate = [GeometryProbe new];
        delegate.baseURL = [NSURL URLWithString:[NSString stringWithUTF8String:argv[1]]];
        delegate.outputDirectory = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[2]] isDirectory:YES];
        app.delegate = delegate;
        [app run];
        return delegate.exitCode;
    }
}
