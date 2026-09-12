#pragma once

#include "SystemRequest.h"

#if COCOA_PY_UIKIT
using CocoaPyPasteboard = UIPasteboard;
using CocoaPyClipboardImage = UIImage;
#else
using CocoaPyPasteboard = NSPasteboard;
using CocoaPyClipboardImage = NSImage;
#endif

static BOOL CocoaPyClipboardHasURL(NSArray<NSString *> *types) {
    return [types containsObject:@"public.url"] || [types containsObject:@"public.file-url"];
}

static NSString *CocoaPyClipboardReadURL(NSArray<NSString *> *types, id (^readValue)(NSString *)) {
    // UIKit's URL convenience getter can infer a URL from plain text. Only
    // fetch explicitly advertised URL representations, including file URLs.
    for (NSString *type in @[@"public.url", @"public.file-url"]) {
        if (![types containsObject:type]) continue;
        id value = readValue(type);
        if ([value isKindOfClass:NSURL.class]) return [value absoluteString];
        if ([value isKindOfClass:NSString.class]) return value;
        if ([value isKindOfClass:NSData.class]) {
            NSString *text = [[NSString alloc] initWithData:value encoding:NSUTF8StringEncoding];
            if (text) return text;
        }
    }
    return nil;
}

// Clipboard operations run on the main thread and contain no Python objects.
static NSData *CocoaPyClipboardPNG(CocoaPyClipboardImage *image) {
    if (!image || image.size.width <= 0 || image.size.height <= 0) return nil;
#if COCOA_PY_UIKIT
    // Drawing applies UIImage orientation before encoding a still PNG.
    UIGraphicsImageRendererFormat *format = [UIGraphicsImageRendererFormat preferredFormat];
    format.scale = image.scale;
    format.opaque = NO;
    UIGraphicsImageRenderer *renderer = [[UIGraphicsImageRenderer alloc] initWithSize:image.size format:format];
    UIImage *upright = [renderer imageWithActions:^(UIGraphicsImageRendererContext *context) {
        [image drawInRect:(CGRect){CGPointZero, image.size}];
    }];
    return UIImagePNGRepresentation(upright);
#else
    // NSImage applies encoded orientation; rasterize its displayed representation.
    CGImageRef pixels = [image CGImageForProposedRect:nullptr context:nil hints:nil];
    if (!pixels) return nil;
    NSBitmapImageRep *bitmap = [[NSBitmapImageRep alloc] initWithCGImage:pixels];
    return [bitmap representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
#endif
}

static CocoaPyRequest *CocoaPyClipboardWrite(CocoaPyPasteboard *board,
                                            NSDictionary<NSString *, id> *item,
                                            BOOL localOnly, double expiresIn) {
#if COCOA_PY_UIKIT
    NSMutableDictionary *options = [NSMutableDictionary dictionary];
    options[UIPasteboardOptionLocalOnly] = @(localOnly);
    if (expiresIn > 0)
        options[UIPasteboardOptionExpirationDate] = [NSDate dateWithTimeIntervalSinceNow:expiresIn];
    [board setItems:@[item] options:options];
#else
    if (expiresIn > 0)
        return CocoaPyFailure(@"not_implemented", @"Clipboard expiration requires iOS.");
    NSPasteboardItem *entry = [NSPasteboardItem new];
    // Prepare every representation before clearing the user's previous contents.
    for (NSString *type in item) {
        id value = item[type];
        BOOL accepted = [value isKindOfClass:NSData.class]
            ? [entry setData:value forType:type] : [entry setString:value forType:type];
        if (!accepted) return CocoaPyFailure(@"os", @"The clipboard rejected a representation.");
    }
    [board prepareForNewContentsWithOptions:localOnly ? NSPasteboardContentsCurrentHostOnly : 0];
    if (![board writeObjects:@[entry]])
        return CocoaPyFailure(@"os", @"The clipboard rejected the item.");
#endif
    return CocoaPyValue(nil);
}

static CocoaPyRequest *CocoaPyClipboard(CocoaPyPasteboard *board, NSString *operation,
                                        id payload, BOOL localOnly, double expiresIn) {
    if ([operation isEqual:@"change_count"]) return CocoaPyValue(@(board.changeCount));
    if ([operation isEqual:@"clear"]) {
#if COCOA_PY_UIKIT
        board.items = @[];
#else
        [board clearContents];
#endif
        return CocoaPyValue(nil);
    }
    if ([operation hasPrefix:@"has_"]) {
        if ([operation isEqual:@"has_urls"]) {
#if COCOA_PY_UIKIT
            for (NSArray<NSString *> *types in [board pasteboardTypesForItemSet:nil])
                if (CocoaPyClipboardHasURL(types)) return CocoaPyValue(@YES);
#else
            for (NSPasteboardItem *item in board.pasteboardItems)
                if (CocoaPyClipboardHasURL(item.types)) return CocoaPyValue(@YES);
#endif
            return CocoaPyValue(@NO);
        }
#if COCOA_PY_UIKIT
        if ([operation isEqual:@"has_text"]) return CocoaPyValue(@(board.hasStrings));
        if ([operation isEqual:@"has_image"]) return CocoaPyValue(@(board.hasImages));
#else
        Class kind = [operation isEqual:@"has_text"] ? NSString.class : NSImage.class;
        return CocoaPyValue(@([board canReadObjectForClasses:@[kind] options:nil]));
#endif
    }
    if ([operation isEqual:@"write_item"])
        return CocoaPyClipboardWrite(board, payload, localOnly, expiresIn);
    if ([operation isEqual:@"write_text"])
        return CocoaPyClipboardWrite(board, @{@"public.utf8-plain-text": payload}, localOnly, expiresIn);
    if ([operation isEqual:@"write_url"]) {
        // Reject relative and malformed inputs instead of silently percent-encoding
        // them. A custom scheme or a file URL does not cause I/O here.
        NSURL *url = [NSURL URLWithString:payload encodingInvalidCharacters:NO];
        if (!url.scheme.length)
            return CocoaPyFailure(@"value", @"url must be a valid absolute URL.");
        NSMutableDictionary *item = [@{@"public.utf8-plain-text": url.absoluteString,
                                      @"public.url": url.absoluteString} mutableCopy];
        if (url.isFileURL) item[@"public.file-url"] = url.absoluteString;
#if COCOA_PY_UIKIT
        item[@"public.url"] = url;
        if (url.isFileURL) item[@"public.file-url"] = url;
#endif
        return CocoaPyClipboardWrite(board, item, localOnly, expiresIn);
    }
    if ([operation isEqual:@"write_image"]) {
#if COCOA_PY_UIKIT
        UIImage *image = [UIImage imageWithData:payload];
#else
        NSImage *image = [[NSImage alloc] initWithData:payload];
#endif
        NSData *png = CocoaPyClipboardPNG(image);
        if (!png) return CocoaPyFailure(@"value", @"image must contain a decodable image.");
        return CocoaPyClipboardWrite(board, @{@"public.png": png}, localOnly, expiresIn);
    }
#if COCOA_PY_UIKIT
    if ([operation isEqual:@"types"]) return CocoaPyValue(board.pasteboardTypes ?: @[]);
    if ([operation isEqual:@"read_text"]) return CocoaPyValue(board.string);
    if ([operation isEqual:@"read_url"])
        return CocoaPyValue(CocoaPyClipboardReadURL(board.pasteboardTypes, ^id(NSString *type) {
            return [board valueForPasteboardType:type];
        }));
    if ([operation isEqual:@"read_bytes"]) return CocoaPyValue([board dataForPasteboardType:payload]);
    if ([operation isEqual:@"read_image"]) {
        UIImage *image = board.image;
#else
    NSPasteboardItem *first = board.pasteboardItems.firstObject;
    if ([operation isEqual:@"types"]) return CocoaPyValue(first.types ?: @[]);
    if ([operation isEqual:@"read_text"]) return CocoaPyValue([first stringForType:NSPasteboardTypeString]);
    if ([operation isEqual:@"read_url"])
        return CocoaPyValue(CocoaPyClipboardReadURL(first.types, ^id(NSString *type) {
            return [first stringForType:type];
        }));
    if ([operation isEqual:@"read_bytes"]) return CocoaPyValue([first dataForType:payload]);
    if ([operation isEqual:@"read_image"]) {
        NSImage *image = nil;
        for (NSString *type in NSImage.imageTypes) {
            if (![first.types containsObject:type]) continue;
            NSData *data = [first dataForType:type];
            if (data) image = [[NSImage alloc] initWithData:data];
            if (image) break;
        }
#endif
        if (!image) return CocoaPyValue(nil);
        NSData *png = CocoaPyClipboardPNG(image);
        return png ? CocoaPyValue(png) : CocoaPyFailure(@"os", @"The clipboard image could not be encoded.");
    }
    return CocoaPyFailure(@"value", @"Unknown clipboard operation.");
}
