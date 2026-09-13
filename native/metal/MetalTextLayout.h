// Shared CoreText measurement and rasterization for scene labels. CoreText
// objects stay on this call's thread; no native delegate invokes Python.
#import <CoreText/CoreText.h>

static PyObject *metal_text_layout(PyObject *, PyObject *args) {
    PyObject *textObject;
    const char *fontName, *alignment, *overflow, *wrap;
    double fontSize, width, spacing, scale;
    long long maxLines;
    int render;
    if (!PyArg_ParseTuple(args, "UdzdsdLssdp", &textObject, &fontSize, &fontName,
                          &width, &alignment, &spacing, &maxLines, &overflow,
                          &wrap, &scale, &render)) return nullptr;
    if (!isfinite(fontSize) || fontSize <= 0 || !isfinite(width) || width < 0 ||
        !isfinite(spacing) || spacing < 0 || maxLines < 0 ||
        !isfinite(scale) || scale <= 0 || fontSize * scale > 16384 ||
        (strcmp(alignment, "left") && strcmp(alignment, "center") && strcmp(alignment, "right")) ||
        (strcmp(overflow, "clip") && strcmp(overflow, "ellipsis")) ||
        (strcmp(wrap, "word") && strcmp(wrap, "char") && strcmp(wrap, "none"))) {
        PyErr_SetString(PyExc_ValueError, "Invalid text layout options or raster scale.");
        return nullptr;
    }
    Py_ssize_t byteCount;
    const char *utf8 = PyUnicode_AsUTF8AndSize(textObject, &byteCount);
    if (!utf8) return nullptr;
    @autoreleasepool {
        NSString *text = [[NSString alloc] initWithBytes:utf8 length:byteCount encoding:NSUTF8StringEncoding];
        text = [[text stringByReplacingOccurrencesOfString:@"\r\n" withString:@"\n"]
                      stringByReplacingOccurrencesOfString:@"\r" withString:@"\n"];
        text = [[text stringByReplacingOccurrencesOfString:@"\u2028" withString:@"\n"]
                      stringByReplacingOccurrencesOfString:@"\u2029" withString:@"\n"];
        CTFontRef font = fontName && *fontName
            ? CTFontCreateWithName((__bridge CFStringRef)[NSString stringWithUTF8String:fontName], fontSize, nullptr)
            : CTFontCreateUIFontForLanguage(kCTFontUIFontSystem, fontSize, nullptr);
        if (!font) {
            PyErr_SetString(PyExc_RuntimeError, "Failed to create the label font.");
            return nullptr;
        }
        CGFloat baseAscent = CTFontGetAscent(font), baseDescent = CTFontGetDescent(font);
        CGFloat baseLeading = CTFontGetLeading(font);
        CGColorRef white = CGColorCreateGenericRGB(1, 1, 1, 1);
        NSDictionary *attributes = @{(__bridge NSString *)kCTFontAttributeName: (__bridge id)font,
            (__bridge NSString *)kCTForegroundColorAttributeName: (__bridge id)white};
        CGColorRelease(white);
        CFRelease(font);
        NSAttributedString *attributed = [[NSAttributedString alloc] initWithString:text attributes:attributes];
        CTTypesetterRef typesetter = CTTypesetterCreateWithAttributedString((__bridge CFAttributedStringRef)attributed);
        if (!typesetter) {
            PyErr_SetString(PyExc_RuntimeError, "Failed to create the label typesetter.");
            return nullptr;
        }
        NSMutableArray *lines = [NSMutableArray array];
        std::vector<double> ascents, heights;
        NSUInteger start = 0, length = text.length;
        bool truncated = false;
        double naturalWidth = 0, height = 0;
        // An empty string still has one line of font metrics, but draws no ink.
        while (start <= length) {
            NSRange newline = [text rangeOfString:@"\n" options:0 range:NSMakeRange(start, length - start)];
            NSUInteger end = newline.location == NSNotFound ? length : newline.location;
            NSUInteger count = end - start;
            if (width > 0 && strcmp(wrap, "none") && count > 0) {
                CFIndex suggested = !strcmp(wrap, "char")
                    ? CTTypesetterSuggestClusterBreak(typesetter, start, width)
                    : CTTypesetterSuggestLineBreak(typesetter, start, width);
                if (suggested <= 0)
                    suggested = [text rangeOfComposedCharacterSequenceAtIndex:start].length;
                count = std::min(count, (NSUInteger)suggested);
            }
            NSUInteger next = start + count;
            bool explicitBreak = next == end && newline.location != NSNotFound;
            if (explicitBreak) ++next;
            bool more = next < length || (explicitBreak && next == length);
            bool last = maxLines > 0 && (long long)lines.count + 1 == maxLines;
            // A zero-length CTTypesetter range means "the remaining string".
            // Create truly empty lines independently instead.
            CTLineRef line = count ? CTTypesetterCreateLine(typesetter, CFRangeMake(start, count))
                : CTLineCreateWithAttributedString((__bridge CFAttributedStringRef)
                    [[NSAttributedString alloc] initWithString:@"" attributes:attributes]);
            double advance = CTLineGetTypographicBounds(line, nullptr, nullptr, nullptr)
                             - CTLineGetTrailingWhitespaceWidth(line);
            bool tooWide = width > 0 && advance > width + 1e-6;
            if ((last && more) || tooWide) {
                truncated = true;
                if (!strcmp(overflow, "ellipsis")) {
                    NSString *remaining = last && more ? [text substringWithRange:NSMakeRange(start, end - start)]
                                                      : [text substringWithRange:NSMakeRange(start, count)];
                    remaining = [remaining stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceCharacterSet]];
                    CTLineRef source = CTLineCreateWithAttributedString((__bridge CFAttributedStringRef)
                        [[NSAttributedString alloc] initWithString:[remaining stringByAppendingString:@"…"] attributes:attributes]);
                    CTLineRef token = CTLineCreateWithAttributedString((__bridge CFAttributedStringRef)
                        [[NSAttributedString alloc] initWithString:@"…" attributes:attributes]);
                    double limit = width > 0 ? width : CTLineGetTypographicBounds(source, nullptr, nullptr, nullptr);
                    CTLineRef shortened = CTLineCreateTruncatedLine(source, limit, kCTLineTruncationEnd, token);
                    CFRelease(line);
                    // If even the ellipsis does not fit, clipping it is preferable
                    // to reintroducing an arbitrarily wide untruncated line.
                    line = shortened ? shortened : (CTLineRef)CFRetain(token);
                    CFRelease(token);
                    CFRelease(source);
                }
            }
            CGFloat ascent = 0, descent = 0, leading = 0;
            advance = CTLineGetTypographicBounds(line, &ascent, &descent, &leading)
                      - CTLineGetTrailingWhitespaceWidth(line);
            ascent = std::max(ascent, baseAscent);
            descent = std::max(descent, baseDescent);
            leading = std::max(leading, baseLeading);
            double lineHeight = ascent + descent + leading;
            naturalWidth = std::max(naturalWidth, advance);
            ascents.push_back(ascent);
            heights.push_back(lineHeight);
            if (lines.count) height += spacing;
            height += lineHeight;
            [lines addObject:(__bridge_transfer id)line];
            if (height * scale > 16384 || lines.count > 100000) break;
            if ((last && more) || (!more && next >= length)) break;
            start = next;
        }
        CFRelease(typesetter);
        if (lines.count > 100000) {
            PyErr_SetString(PyExc_ValueError, "Label layout exceeds 100000 lines.");
            return nullptr;
        }
        double logicalWidth = width > 0 ? width : naturalWidth;
        // Small symmetric padding preserves italic overhangs in unconstrained
        // labels, without including raster padding in layout or hit bounds.
        double padding = std::max(2.0 / scale, fontSize * .2);
        double pixelWidth = ceil((logicalWidth + 2 * padding) * scale);
        double pixelHeight = ceil((height + 2 * padding) * scale);
        if (!isfinite(pixelWidth) || !isfinite(pixelHeight) || pixelWidth > 16384 || pixelHeight > 16384 ||
            pixelWidth * pixelHeight > 64 * 1024 * 1024) {
            PyErr_SetString(PyExc_ValueError, "Label layout exceeds the raster size limit.");
            return nullptr;
        }
        NSUInteger pw = (NSUInteger)std::max(1.0, pixelWidth), ph = (NSUInteger)std::max(1.0, pixelHeight);
        double drawWidth = pw / scale, drawHeight = ph / scale;
        long long handle = 0;
        if (render) {
            if (!ensureMetalContext()) {
                PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
                return nullptr;
            }
            CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
            CGContextRef bitmap = CGBitmapContextCreate(nullptr, pw, ph, 8, pw * 4, colorSpace,
                kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little);
            CGColorSpaceRelease(colorSpace);
            if (!bitmap) return PyErr_NoMemory();
            // CoreText uses y-up coordinates. The bitmap's first stored row
            // already corresponds to the top sampled by the texture shader.
            CGContextScaleCTM(bitmap, scale, scale);
            CGContextSetTextMatrix(bitmap, CGAffineTransformIdentity);
            double originX = (drawWidth - logicalWidth) / 2;
            double originY = (drawHeight - height) / 2;
            if (width > 0) CGContextClipToRect(bitmap, CGRectMake(originX, 0, logicalWidth, drawHeight));
            double top = 0;
            for (NSUInteger index = 0; index < lines.count; ++index) {
                CTLineRef line = (__bridge CTLineRef)lines[index];
                double flush = !strcmp(alignment, "right") ? 1 : !strcmp(alignment, "center") ? .5 : 0;
                double x = CTLineGetPenOffsetForFlush(line, flush, logicalWidth);
                CGContextSetTextPosition(bitmap, originX + x, drawHeight - originY - top - ascents[index]);
                CTLineDraw(line, bitmap);
                top += heights[index] + spacing;
            }
            auto *raw = (uint8_t *)CGBitmapContextGetData(bitmap);
            MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:pw height:ph mipmapped:NO];
            descriptor.usage = MTLTextureUsageShaderRead;
            id<MTLTexture> texture = [gDevice newTextureWithDescriptor:descriptor];
            if (texture) [texture replaceRegion:MTLRegionMake2D(0, 0, pw, ph) mipmapLevel:0
                                      withBytes:raw bytesPerRow:pw * 4];
            CGContextRelease(bitmap);
            if (!texture) {
                PyErr_SetString(PyExc_RuntimeError, "Failed to allocate the label texture.");
                return nullptr;
            }
            handle = nextHandle();
            std::lock_guard<std::mutex> lock(gStateMutex);
            gTextures.emplace(handle, TextureRecord{handle, texture, pw, ph, MTLPixelFormatBGRA8Unorm});
        }
        PyObject *result = Py_BuildValue("{s:L,s:(KK),s:(dd),s:(dd),s:K,s:O}",
            "handle", handle, "size", (unsigned long long)pw, (unsigned long long)ph,
            "logical_size", logicalWidth, height, "draw_size", drawWidth, drawHeight,
            "line_count", (unsigned long long)lines.count, "truncated", truncated ? Py_True : Py_False);
        if (!result && handle) {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gTextures.erase(handle);
        }
        return result;
    }
}
