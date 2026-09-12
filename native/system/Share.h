#pragma once

#include "SystemRequest.h"

@class CocoaPyShareRequest;
static NSMutableSet<CocoaPyShareRequest *> *CocoaPyActiveShares;

#if COCOA_PY_UIKIT
@interface CocoaPyShareRequest : CocoaPyRequest <UIAdaptivePresentationControllerDelegate>
#else
@interface CocoaPyShareRequest : CocoaPyRequest <NSSharingServicePickerDelegate, NSSharingServiceDelegate, NSWindowDelegate>
#endif
{
    std::vector<std::shared_ptr<CocoaPyFileAccess>> _access;
}
@property(nonatomic, strong) NSArray *items;
@property(nonatomic, strong) NSURL *temporaryDirectory;
@property(nonatomic) BOOL dismissalPending;
#if COCOA_PY_UIKIT
@property(nonatomic, strong) UIActivityViewController *controller;
@property(nonatomic) BOOL presentationPending;
- (void)presentationDidFinish;
#else
@property(nonatomic) BOOL selected;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSSharingServicePicker *picker;
@property(nonatomic, strong) NSSharingService *service;
@property(nonatomic, strong) NSButton *button;
- (void)showPicker:(id)sender;
#endif
- (BOOL)addFile:(NSURL *)url;
- (NSURL *)writeAttachment:(NSData *)data name:(NSString *)name index:(NSUInteger)index;
- (void)complete:(BOOL)completed activity:(NSString *)activity error:(NSError *)error;
- (void)closeWithDismissal:(void (^)(dispatch_block_t completion))dismiss;
- (void)cleanup;
@end

@implementation CocoaPyShareRequest
- (NSURL *)writeAttachment:(NSData *)data name:(NSString *)name index:(NSUInteger)index {
    NSFileManager *manager = NSFileManager.defaultManager;
    if (!self.temporaryDirectory) {
        NSString *pattern = [NSTemporaryDirectory() stringByAppendingPathComponent:@"cocoa_py_share_XXXXXX"];
        const char *source = pattern.fileSystemRepresentation;
        std::vector<char> path(source, source + strlen(source) + 1);
        if (!mkdtemp(path.data())) {
            [self fail:@"os" message:@"Cannot create the temporary sharing directory."]; return nil;
        }
        self.temporaryDirectory = [NSURL fileURLWithFileSystemRepresentation:path.data()
                                                               isDirectory:YES relativeToURL:nil];
    }
    // Separate directories preserve names even on a case-insensitive volume.
    NSURL *directory = [self.temporaryDirectory URLByAppendingPathComponent:[NSString stringWithFormat:@"%lu", (unsigned long)index]
                                                               isDirectory:YES];
    NSError *error = nil;
    if (![manager createDirectoryAtURL:directory withIntermediateDirectories:NO
                           attributes:@{NSFilePosixPermissions: @0700} error:&error]) {
        [self fail:@"os" message:error.localizedDescription]; return nil;
    }
    NSURL *url = [directory URLByAppendingPathComponent:name isDirectory:NO];
    if (![data writeToURL:url options:NSDataWritingAtomic error:&error]) {
        [self fail:@"os" message:error.localizedDescription]; return nil;
    }
    return url;
}
- (BOOL)addFile:(NSURL *)url {
    auto token = std::make_shared<CocoaPyFileAccess>(url);
    if (![NSFileManager.defaultManager fileExistsAtPath:url.path]) {
        [self fail:@"file_not_found" message:[NSString stringWithFormat:@"The shared file does not exist: %@", url.path]];
        return NO;
    }
    _access.push_back(std::move(token)); return YES;
}
- (void)complete:(BOOL)completed activity:(NSString *)activity error:(NSError *)error {
    if ([error.domain isEqual:NSCocoaErrorDomain] && error.code == NSUserCancelledError) {
        completed = NO; error = nil;
    }
    if (error) [self fail:@"os" message:error.localizedDescription];
    else [self finish:@{ @"completed": @(completed), @"activity": activity ?: NSNull.null }];
    [self cleanup];
}
- (void)cleanup {
#if COCOA_PY_UIKIT
    UIActivityViewController *controller = self.controller;
    self.controller = nil;
    controller.completionWithItemsHandler = nil;
    controller.presentationController.delegate = nil;
    if (!self.dismissalPending && controller.presentingViewController)
        [controller.presentingViewController dismissViewControllerAnimated:YES completion:nil];
#else
    self.picker.delegate = nil; [self.picker close]; self.picker = nil;
    self.service.delegate = nil; self.service = nil;
    self.window.delegate = nil; [self.window close]; self.window = nil; self.button = nil;
#endif
    _access.clear(); self.items = nil;
    if (self.temporaryDirectory) {
        [NSFileManager.defaultManager removeItemAtURL:self.temporaryDirectory error:nil];
        self.temporaryDirectory = nil;
    }
    [CocoaPyActiveShares removeObject:self];
}
- (void)dealloc {
    // Preparation failures may release the request before any UI exists.
    if (_temporaryDirectory) [NSFileManager.defaultManager removeItemAtURL:_temporaryDirectory error:nil];
}
- (void)close {
#if COCOA_PY_UIKIT
    [super close];
    if (self.presentationPending) return;
    UIActivityViewController *controller = self.controller;
    if (!controller) { [self cleanup]; return; }
    UIViewController *presenter = controller.presentingViewController;
    if (presenter) {
        // Fetching an activity item is not a reliable selection notification.
        // Explicitly cancel visible UI and keep its files through dismissal.
        [self closeWithDismissal:^(dispatch_block_t completion) {
            [presenter dismissViewControllerAnimated:YES completion:completion];
        }];
    }
    // If UIKit already handed off and removed the local sheet, its activity
    // completion callback still owns the end of the resource lifetime.
#else
    [super close];
    // Keep access tokens and temporary attachments until the selected service
    // finishes, even if Python stops waiting or drops its request handle.
    if (!self.selected) [self cleanup];
#endif
}
- (void)closeWithDismissal:(void (^)(dispatch_block_t completion))dismiss {
    [super close];
    if (self.dismissalPending) return;
    self.dismissalPending = YES;
    dismiss(^{
        self.dismissalPending = NO;
        // Programmatic dismissal need not produce an activity result. This
        // completion also covers cancellation before choosing a service.
        [self complete:NO activity:nil error:nil];
    });
}
#if COCOA_PY_UIKIT
- (void)presentationDidFinish {
    self.presentationPending = NO;
    if (self.closed) [self close];
}
- (void)presentationControllerDidDismiss:(UIPresentationController *)controller {
    [self complete:NO activity:nil error:nil];
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

static BOOL CocoaPyPresentShare(CocoaPyShareRequest *request) {
#if COCOA_PY_UIKIT
    UIViewController *presenter = CocoaPyPresenter();
    if (!presenter) { [request fail:@"runtime" message:@"No foreground window is available for sharing."]; return NO; }
    // Check the context before UIKit asynchronously prepares the activity UI.
    // Its presentingViewController need not be set when present returns.
    UIViewController *anchor = presenter;
    while (!anchor.viewIfLoaded.window) {
        // An incoming modal can use its attached presenting context before
        // its own view enters the window during the transition.
        if (!anchor.isBeingPresented || !anchor.presentingViewController) return NO;
        anchor = anchor.presentingViewController;
    }
    request.controller = [[UIActivityViewController alloc] initWithActivityItems:request.items applicationActivities:nil];
    __weak CocoaPyShareRequest *weakRequest = request;
    request.controller.completionWithItemsHandler = ^(UIActivityType activity, BOOL completed, NSArray *returned, NSError *error) {
        CocoaPyRunOnMain(^{ [weakRequest complete:completed activity:activity error:error]; });
    };
    request.controller.popoverPresentationController.sourceView = presenter.view;
    request.controller.popoverPresentationController.sourceRect = CGRectMake(CGRectGetMidX(presenter.view.bounds), CGRectGetMidY(presenter.view.bounds), 1, 1);
    request.controller.popoverPresentationController.permittedArrowDirections = 0;
    request.presentationPending = YES;
    [presenter presentViewController:request.controller animated:YES completion:^{ [weakRequest presentationDidFinish]; }];
    request.controller.presentationController.delegate = request;
#else
    CocoaPyPrepareApplication();
    request.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 360, 150)
                       styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                         backing:NSBackingStoreBuffered defer:NO];
    request.window.releasedWhenClosed = NO; request.window.title = @"Share"; request.window.delegate = request;
    request.button = [NSButton buttonWithTitle:@"Share…" target:request action:@selector(showPicker:)];
    request.button.frame = NSMakeRect(120, 55, 120, 36); [request.window.contentView addSubview:request.button];
    request.picker = [[NSSharingServicePicker alloc] initWithItems:request.items]; request.picker.delegate = request;
    [request.window center]; [request.window makeKeyAndOrderFront:nil];
#endif
    return YES;
}

// The presentation seam allows native tests to drive real completion callbacks
// and file lifetimes without opening services or sending anything.
static CocoaPyRequest *CocoaPyShare(NSString *name, NSDictionary *args,
                                  BOOL (*present)(CocoaPyShareRequest *) = CocoaPyPresentShare) {
    if (![name isEqual:@"share.present"]) return CocoaPyFailure(@"value", @"Unknown share operation.");
    if (![args[@"files"] isKindOfClass:NSArray.class] || ![args[@"urls"] isKindOfClass:NSArray.class] ||
        ![args[@"images"] isKindOfClass:NSArray.class] || ![args[@"attachments"] isKindOfClass:NSArray.class] ||
        (args[@"text"] != NSNull.null && !CocoaPyString(args, @"text", YES)))
        return CocoaPyFailure(@"value", @"Share requires text, file paths, URLs, images, or named attachments.");
    CocoaPyShareRequest *request = [CocoaPyShareRequest new];
    @try {
        NSMutableArray *items = [NSMutableArray array];
        if (args[@"text"] != NSNull.null) [items addObject:args[@"text"]];
        for (id path in args[@"files"]) {
            if (![path isKindOfClass:NSString.class] || ![path length] || [path rangeOfString:@"\0"].location != NSNotFound) {
                [request fail:@"value" message:@"File paths must be nonempty strings without NUL."]; return request;
            }
            NSURL *url = [NSURL fileURLWithPath:path];
            if (![request addFile:url]) return request;
            [items addObject:url];
        }
        for (id value in args[@"urls"]) {
            if (![value isKindOfClass:NSString.class]) {
                [request fail:@"value" message:@"URLs must be strings."]; return request;
            }
            NSURL *url = [NSURL URLWithString:value];
            if (!url.scheme || url.isFileURL) {
                [request fail:@"value" message:@"Use an absolute non-file URL, or put local paths in files."]; return request;
            }
            [items addObject:url];
        }
        for (NSData *data in args[@"images"]) {
#if COCOA_PY_UIKIT
            UIImage *image = [[UIImage alloc] initWithData:data];
            BOOL valid = image.CGImage != nullptr;
#else
            NSImage *image = [[NSImage alloc] initWithData:data];
            BOOL valid = image && [image CGImageForProposedRect:nullptr context:nil hints:nil] != nullptr;
#endif
            if (!valid || image.size.width <= 0 || image.size.height <= 0) {
                [request fail:@"value" message:@"Each image must contain decodable image data."]; return request;
            }
            [items addObject:image];
        }
        NSUInteger index = 0;
        for (NSDictionary *attachment in args[@"attachments"]) {
            NSURL *url = [request writeAttachment:attachment[@"data"] name:attachment[@"name"] index:index++];
            if (!url) return request;
            [items addObject:url];
        }
        if (!items.count) { [request fail:@"value" message:@"At least one item is required."]; return request; }
        request.items = items;
        if (!CocoaPyActiveShares) CocoaPyActiveShares = [NSMutableSet set];
        [CocoaPyActiveShares addObject:request];
        if (!present(request) && !request.done)
            [request fail:@"runtime" message:@"The system could not present the sharing interface."];
    } @catch (NSException *exception) {
        [request fail:@"runtime" message:exception.reason];
    } @finally {
        if (request.done) [request cleanup];
    }
    return request;
}
