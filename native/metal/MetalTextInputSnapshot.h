#pragma once

static PyObject *metal_text_input_snapshot(PyObject *, PyObject *args) {
    long long handle;
    double scale;
    if (!PyArg_ParseTuple(args, "Ld", &handle, &scale)) return nullptr;
    if (!std::isfinite(scale) || scale <= 0) {
        PyErr_SetString(PyExc_ValueError, "Snapshot scale must be finite and positive."); return nullptr;
    }
    if (!ensureMetalContext()) { PyErr_SetString(PyExc_RuntimeError, "Metal is unavailable."); return nullptr; }
    __block id<MTLTexture> texture;
    __block NSUInteger width = 0, height = 0;
    __block NSString *error;
    PyObject *result = inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
        if (input.headless) { error = @"Input sessions have no native appearance to capture."; return nil; }
        NSDictionary *o = input.options;
        CGFloat logicalWidth = [o[@"width"] doubleValue], logicalHeight = [o[@"height"] doubleValue];
        double w = ceil(logicalWidth * scale), h = ceil(logicalHeight * scale);
        if (!std::isfinite(w) || !std::isfinite(h) || w > 16384 || h > 16384) {
            error = @"Text input snapshot exceeds the maximum texture dimensions."; return nil;
        }
        width = MAX(1.0, w); height = MAX(1.0, h);
        error = textureAllocationError(width, height, 4);
        if (error) return nil;
        CGColorSpaceRef colorSpace = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
        CGContextRef bitmap = CGBitmapContextCreate(nullptr, width, height, 8, width * 4, colorSpace,
            kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little);
        CGColorSpaceRelease(colorSpace);
        if (!bitmap) { error = @"Failed to allocate a text input snapshot."; return nil; }
        CGContextTranslateCTM(bitmap, 0, height);
        CGContextScaleCTM(bitmap, width / logicalWidth, -(CGFloat)height / logicalHeight);
#if COCOA_PY_UIKIT
        UIGraphicsPushContext(bitmap);
#else
        [NSGraphicsContext saveGraphicsState];
        NSGraphicsContext.currentContext = [NSGraphicsContext graphicsContextWithCGContext:bitmap flipped:YES];
#endif
        @try {
            CGRect bounds = CGRectMake(0, 0, logicalWidth, logicalHeight);
            CGFloat radius = MIN([o[@"corner_radius"] doubleValue], MIN(logicalWidth, logicalHeight) / 2);
            CGPathRef outline = CGPathCreateWithRoundedRect(bounds, radius, radius, nullptr);
            CGContextAddPath(bitmap, outline);
            CGContextClip(bitmap);
            CGContextSetFillColorWithColor(bitmap, inputColor(o[@"background"]).CGColor);
            CGContextFillRect(bitmap, bounds);
            CGFloat border = MIN([o[@"border_width"] doubleValue], MIN(logicalWidth, logicalHeight) / 2);
            if (border > 0) {
                CGPathRef path = CGPathCreateWithRoundedRect(CGRectInset(bounds, border / 2, border / 2),
                    MAX(0, radius - border / 2), MAX(0, radius - border / 2), nullptr);
                CGContextAddPath(bitmap, path);
                CGContextSetStrokeColorWithColor(bitmap, inputColor(o[@"border_color"]).CGColor);
                CGContextSetLineWidth(bitmap, border);
                CGContextStrokePath(bitmap);
                CGPathRelease(path);
            }
            CGPathRelease(outline);
            NSString *text = input.textValue;
            BOOL placeholder = text.length == 0;
            if (placeholder) text = o[@"placeholder"];
            else if ([o[@"secure"] boolValue]) {
                NSMutableString *masked = [NSMutableString new];
                [text enumerateSubstringsInRange:NSMakeRange(0, text.length)
                    options:NSStringEnumerationByComposedCharacterSequences
                    usingBlock:^(NSString *, NSRange, NSRange, BOOL *) { [masked appendString:@"\u2022"]; }];
                text = masked;
            }
            CGFloat size = [o[@"font_size"] doubleValue];
            CocoaFont *font = [o[@"font"] isKindOfClass:NSString.class] ? [CocoaFont fontWithName:o[@"font"] size:size] : nil;
            font = font ?: [CocoaFont systemFontOfSize:size];
            NSArray *padding = o[@"padding"];
            CGRect inner = CGRectMake([padding[1] doubleValue], [padding[0] doubleValue],
                MAX(1, logicalWidth - [padding[1] doubleValue] - [padding[3] doubleValue]),
                MAX(1, logicalHeight - [padding[0] doubleValue] - [padding[2] doubleValue]));
            CGContextClipToRect(bitmap, inner);
            NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
            paragraph.alignment = inputAlignment(o[@"alignment"]);
            BOOL multiline = [o[@"multiline"] boolValue];
            paragraph.lineBreakMode = multiline ? NSLineBreakByWordWrapping : NSLineBreakByClipping;
            NSDictionary *attributes = @{NSFontAttributeName:font, NSParagraphStyleAttributeName:paragraph,
                NSForegroundColorAttributeName:inputColor(o[placeholder ? @"placeholder_color" : @"text_color"])};
            if (!multiline) {
                CGFloat lineHeight = [@"Mg" sizeWithAttributes:attributes].height;
                inner.origin.y += MAX(0, (inner.size.height - lineHeight) / 2);
                inner.size.height = MAX(inner.size.height, lineHeight);
            } else if (!placeholder) {
                // Preserve the document viewport without changing focus, marked
                // text, selection, the undo stack, or the editor's layout size.
#if COCOA_PY_UIKIT
                inner.origin.y -= input.textView.contentOffset.y;
#else
                inner.origin.y -= input.scrollView.contentView.bounds.origin.y;
#endif
                inner.size.height = 1e7;
            }
            [text drawWithRect:inner options:NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingUsesFontLeading attributes:attributes context:nil];
            MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                width:width height:height mipmapped:NO];
            descriptor.usage = MTLTextureUsageShaderRead;
            texture = [gDevice newTextureWithDescriptor:descriptor];
            if (texture) [texture replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                withBytes:CGBitmapContextGetData(bitmap) bytesPerRow:width * 4];
        } @finally {
#if COCOA_PY_UIKIT
            UIGraphicsPopContext();
#else
            [NSGraphicsContext restoreGraphicsState];
#endif
            CGContextRelease(bitmap);
        }
        return nil;
    });
    if (!result) return nullptr;
    Py_DECREF(result);
    if (error) { PyErr_SetString(PyExc_ValueError, error.UTF8String); return nullptr; }
    if (!texture) { PyErr_SetString(PyExc_RuntimeError, "Failed to rasterize text input."); return nullptr; }
    long long textureHandle = nextHandle();
    PyObject *description = Py_BuildValue("{s:L,s:(II)}", "handle", textureHandle, "size", (unsigned int)width, (unsigned int)height);
    if (!description) return nullptr;
    { std::lock_guard<std::mutex> lock(gStateMutex);
        gTextures.emplace(textureHandle, TextureRecord{textureHandle, texture, width, height, MTLPixelFormatBGRA8Unorm});
    }
    return description;
}
