#pragma once

@class CocoaPyShareRequest;
static NSMutableSet<CocoaPyShareRequest *> *CocoaPyActiveShares;

#if COCOA_PY_UIKIT
@interface CocoaPyShareItem : NSObject <UIActivityItemSource>
@property(nonatomic, strong) id value;
@property(nonatomic, weak) CocoaPyShareRequest *owner;
@end
@interface CocoaPyShareRequest : CocoaPyRequest <UIAdaptivePresentationControllerDelegate>
#else
@interface CocoaPyShareRequest : CocoaPyRequest <NSSharingServicePickerDelegate, NSSharingServiceDelegate, NSWindowDelegate>
#endif
{
    std::vector<std::shared_ptr<CocoaPyFileAccess>> _access;
}
@property(nonatomic, strong) NSArray *items;
@property(nonatomic) BOOL selected;
#if COCOA_PY_UIKIT
@property(nonatomic, strong) UIActivityViewController *controller;
#else
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSSharingServicePicker *picker;
@property(nonatomic, strong) NSSharingService *service;
@property(nonatomic, strong) NSButton *button;
- (void)showPicker:(id)sender;
#endif
- (BOOL)addFile:(NSURL *)url;
- (void)complete:(BOOL)completed activity:(NSString *)activity error:(NSError *)error;
- (void)cleanup;
@end

#if COCOA_PY_UIKIT
@implementation CocoaPyShareItem
- (id)activityViewControllerPlaceholderItem:(UIActivityViewController *)controller { return self.value; }
- (id)activityViewController:(UIActivityViewController *)controller itemForActivityType:(UIActivityType)type {
    if (type) self.owner.selected = YES;
    return self.value;
}
@end
#endif

@implementation CocoaPyShareRequest
- (BOOL)addFile:(NSURL *)url {
    auto token = std::make_shared<CocoaPyFileAccess>(url);
    if (![NSFileManager.defaultManager fileExistsAtPath:url.path]) {
        [self fail:@"file_not_found" message:[NSString stringWithFormat:@"The shared file does not exist: %@", url.path]];
        return NO;
    }
    _access.push_back(std::move(token)); return YES;
}
- (void)complete:(BOOL)completed activity:(NSString *)activity error:(NSError *)error {
    if (error) [self fail:@"os" message:error.localizedDescription];
    else [self finish:@{ @"completed": @(completed), @"activity": activity ?: NSNull.null }];
    [self cleanup];
}
- (void)cleanup {
#if COCOA_PY_UIKIT
    self.controller.completionWithItemsHandler = nil;
    self.controller.presentationController.delegate = nil;
    [self.controller dismissViewControllerAnimated:YES completion:nil]; self.controller = nil;
#else
    self.picker.delegate = nil; [self.picker close]; self.picker = nil;
    self.service.delegate = nil; self.service = nil;
    self.window.delegate = nil; [self.window close]; self.window = nil; self.button = nil;
#endif
    _access.clear(); self.items = nil; [CocoaPyActiveShares removeObject:self];
}
- (void)close {
    [super close];
    // Once a service starts copying a file, keep its access tokens alive until
    // the service's completion callback, even if Python stops waiting.
    if (!self.selected) [self cleanup];
}
#if COCOA_PY_UIKIT
- (void)presentationControllerDidDismiss:(UIPresentationController *)controller {
    if (!self.selected) [self complete:NO activity:nil error:nil];
}
#else
- (void)showPicker:(id)sender {
    [self.picker showRelativeToRect:self.button.bounds ofView:self.button preferredEdge:NSRectEdgeMinY];
}
- (BOOL)windowShouldClose:(NSWindow *)window {
    if (!self.selected) [self complete:NO activity:nil error:nil];
    return NO;
}
- (void)sharingServicePicker:(NSSharingServicePicker *)picker didChooseSharingService:(NSSharingService *)service {
    if (!service) { [self complete:NO activity:nil error:nil]; return; }
    self.selected = YES; self.service = service;
}
- (id<NSSharingServiceDelegate>)sharingServicePicker:(NSSharingServicePicker *)picker
                         delegateForSharingService:(NSSharingService *)service {
    return self;
}
- (void)sharingService:(NSSharingService *)service didShareItems:(NSArray *)items {
    [self complete:YES activity:service.title error:nil];
}
- (void)sharingService:(NSSharingService *)service didFailToShareItems:(NSArray *)items error:(NSError *)error {
    [self complete:NO activity:service.title error:error];
}
- (NSWindow *)sharingService:(NSSharingService *)service sourceWindowForShareItems:(NSArray *)items
        sharingContentScope:(NSSharingContentScope *)scope { return self.window; }
#endif
@end

static CocoaPyRequest *CocoaPyShare(NSString *name, NSDictionary *args) {
    if (![name isEqual:@"share.present"]) return CocoaPyFailure(@"value", @"Unknown share operation.");
    if (![args[@"files"] isKindOfClass:NSArray.class] || ![args[@"urls"] isKindOfClass:NSArray.class] ||
        (args[@"text"] != NSNull.null && !CocoaPyString(args, @"text", YES)))
        return CocoaPyFailure(@"value", @"Share requires text, file paths, or URL strings.");
    CocoaPyShareRequest *request = [CocoaPyShareRequest new];
    NSMutableArray *items = [NSMutableArray array];
    if (args[@"text"] != NSNull.null) [items addObject:args[@"text"]];
    for (id path in args[@"files"]) {
        if (![path isKindOfClass:NSString.class] || ![path length]) return CocoaPyFailure(@"value", @"File paths must be nonempty strings.");
        NSURL *url = [NSURL fileURLWithPath:path];
        if (![request addFile:url]) return request;
        [items addObject:url];
    }
    for (id value in args[@"urls"]) {
        if (![value isKindOfClass:NSString.class]) return CocoaPyFailure(@"value", @"URLs must be strings.");
        NSURL *url = [NSURL URLWithString:value];
        if (!url.scheme || url.isFileURL) return CocoaPyFailure(@"value", @"Use an absolute non-file URL, or put local paths in files.");
        [items addObject:url];
    }
    if (!items.count) return CocoaPyFailure(@"value", @"At least one item is required.");
    request.items = items;
    if (!CocoaPyActiveShares) CocoaPyActiveShares = [NSMutableSet set];
#if COCOA_PY_UIKIT
    UIViewController *presenter = CocoaPyPresenter();
    if (!presenter) return CocoaPyFailure(@"runtime", @"No foreground window is available for sharing.");
    NSMutableArray *sources = [NSMutableArray array];
    for (id item in items) {
        CocoaPyShareItem *source = [CocoaPyShareItem new]; source.value = item; source.owner = request;
        [sources addObject:source];
    }
    request.controller = [[UIActivityViewController alloc] initWithActivityItems:sources applicationActivities:nil];
    __weak CocoaPyShareRequest *weakRequest = request;
    request.controller.completionWithItemsHandler = ^(UIActivityType activity, BOOL completed, NSArray *returned, NSError *error) {
        CocoaPyRunOnMain(^{ [weakRequest complete:completed activity:activity error:error]; });
    };
    request.controller.popoverPresentationController.sourceView = presenter.view;
    request.controller.popoverPresentationController.sourceRect = CGRectMake(CGRectGetMidX(presenter.view.bounds), CGRectGetMidY(presenter.view.bounds), 1, 1);
    request.controller.popoverPresentationController.permittedArrowDirections = 0;
    [CocoaPyActiveShares addObject:request];
    [presenter presentViewController:request.controller animated:YES completion:nil];
    request.controller.presentationController.delegate = request;
#else
    CocoaPyPrepareApplication();
    request.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 360, 150)
                       styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                         backing:NSBackingStoreBuffered defer:NO];
    request.window.releasedWhenClosed = NO; request.window.title = @"Share"; request.window.delegate = request;
    request.button = [NSButton buttonWithTitle:@"Share…" target:request action:@selector(showPicker:)];
    request.button.frame = NSMakeRect(120, 55, 120, 36); [request.window.contentView addSubview:request.button];
    request.picker = [[NSSharingServicePicker alloc] initWithItems:items]; request.picker.delegate = request;
    [CocoaPyActiveShares addObject:request];
    [request.window center]; [request.window makeKeyAndOrderFront:nil];
#endif
    return request;
}
