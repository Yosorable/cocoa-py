#define PY_SSIZE_T_CLEAN
#include <Python.h>
// The installed extension and this fixture coexist in one Python process.
#define CocoaPyRequest CocoaPyClipboardTestRequest
#include "../../native/system/ClipboardBridge.h"

static NSPasteboard *board;
static CocoaPyPasteboard *testBoard() {
    if (!board) board = [NSPasteboard pasteboardWithUniqueName];
    return board;
}
static PyObject *test_clipboard(PyObject *, PyObject *args, PyObject *kwargs) {
    return CocoaPyClipboardCall(args, kwargs, testBoard);
}
static PyObject *test_release(PyObject *, PyObject *) {
    @autoreleasepool { [board releaseGlobally]; board = nil; }
    Py_RETURN_NONE;
}
static PyObject *test_seed_two_items(PyObject *, PyObject *) {
    @autoreleasepool {
        NSPasteboardItem *first = [NSPasteboardItem new], *second = [NSPasteboardItem new];
        [first setString:@"first" forType:NSPasteboardTypeString];
        [second setString:@"https://example.org/" forType:NSPasteboardTypeURL];
        [testBoard() clearContents];
        [testBoard() writeObjects:@[first, second]];
    }
    Py_RETURN_NONE;
}
static PyObject *test_url_type_contract(PyObject *, PyObject *) {
    @autoreleasepool {
        __block NSUInteger reads = 0;
        id (^coercingReader)(NSString *) = ^id(NSString *type) {
            reads++;
            // Model a framework getter that offers a URL even for plain text.
            return [NSURL URLWithString:@"https://example.org/"];
        };
        NSArray *plain = @[@"public.utf8-plain-text"];
        if (CocoaPyClipboardReadURL(plain, coercingReader) || reads || CocoaPyClipboardHasURL(plain)) {
            PyErr_SetString(PyExc_AssertionError, "Plain text must not fetch or advertise inferred URLs.");
            return nullptr;
        }
        NSString *url = CocoaPyClipboardReadURL(@[@"public.url"], coercingReader);
        if (![url isEqual:@"https://example.org/"] || reads != 1) {
            PyErr_SetString(PyExc_AssertionError, "An advertised NSURL representation must be read.");
            return nullptr;
        }
        for (NSString *type in @[@"public.url", @"public.file-url"]) {
            for (id value in @[@"file:///tmp/test", [@"file:///tmp/test" dataUsingEncoding:NSUTF8StringEncoding]]) {
                NSString *text = CocoaPyClipboardReadURL(@[type], ^id(NSString *) { return value; });
                if (![text isEqual:@"file:///tmp/test"] || !CocoaPyClipboardHasURL(@[type])) {
                    PyErr_SetString(PyExc_AssertionError, "String and byte URL representations must be supported.");
                    return nullptr;
                }
            }
        }
        if (CocoaPyClipboardReadURL(@[], coercingReader) ||
            CocoaPyClipboardReadURL(@[@"public.url"], ^id(NSString *) { return NSNull.null; })) {
            PyErr_SetString(PyExc_AssertionError, "Missing or unsupported URL data must return None.");
            return nullptr;
        }
    }
    Py_RETURN_NONE;
}
static PyMethodDef methods[] = {
    {"clipboard", (PyCFunction)test_clipboard, METH_VARARGS | METH_KEYWORDS, nullptr},
    {"release", test_release, METH_NOARGS, nullptr},
    {"seed_two_items", test_seed_two_items, METH_NOARGS, nullptr},
    {"url_type_contract", test_url_type_contract, METH_NOARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_clipboard_fixture", nullptr, -1, methods};
PyMODINIT_FUNC PyInit__clipboard_fixture(void) { return PyModule_Create(&module); }
