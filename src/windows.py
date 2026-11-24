import ctypes as ct
from ctypes import wintypes as w
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import subprocess
import threading


user = ct.WinDLL("user32", use_last_error=True)
shell = ct.WinDLL("shell32", use_last_error=True)
application = ct.WinDLL("kernel32", use_last_error=True)
imm = ct.WinDLL("imm32", use_last_error=True)
CALLBACK = ct.WINFUNCTYPE(ct.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
TRAY_MESSAGE = 0x8001
HOTKEY = 1
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
MONITOR_DEFAULTTONEAREST = 2
APP_ASSETS = Path(__file__).resolve().parents[1] / "assets"


class NotifyIcon(ct.Structure):
    _fields_ = [
        ("cbSize", w.DWORD), ("hwnd", w.HWND), ("identifier", w.UINT),
        ("flags", w.UINT), ("callback", w.UINT), ("icon", w.HICON),
        ("tip", w.WCHAR * 128), ("state", w.DWORD), ("state_mask", w.DWORD),
        ("info", w.WCHAR * 256), ("timeout", w.UINT),
        ("info_title", w.WCHAR * 64), ("info_flags", w.DWORD),
        ("guid", ct.c_ubyte * 16), ("balloon_icon", w.HICON),
    ]


class LogFont(ct.Structure):
    _fields_ = [
        ("height", w.LONG), ("width", w.LONG), ("escapement", w.LONG), ("orientation", w.LONG),
        ("weight", w.LONG), ("italic", w.BYTE), ("underline", w.BYTE), ("strike", w.BYTE),
        ("charset", w.BYTE), ("out_precision", w.BYTE), ("clip_precision", w.BYTE),
        ("quality", w.BYTE), ("pitch_family", w.BYTE), ("face", w.WCHAR * 32),
    ]


class WindowClass(ct.Structure):
    _fields_ = [
        ("style", w.UINT), ("procedure", CALLBACK), ("class_extra", ct.c_int), ("window_extra", ct.c_int),
        ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
        ("menu", w.LPCWSTR), ("name", w.LPCWSTR),
    ]


class MonitorInfo(ct.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]


class MonitorInfoEx(ct.Structure):
    _fields_ = MonitorInfo._fields_ + [("device", w.WCHAR * 32)]


user.GetAncestor.argtypes = (w.HWND, w.UINT)
user.GetAncestor.restype = w.HWND
user.GetForegroundWindow.restype = w.HWND
user.GetDpiForWindow.argtypes = (w.HWND,)
user.GetDpiForWindow.restype = w.UINT
user.MonitorFromPoint.argtypes = (w.POINT, w.DWORD)
user.MonitorFromPoint.restype = w.HANDLE
user.GetMonitorInfoW.argtypes = (w.HANDLE, ct.c_void_p)
user.GetMonitorInfoW.restype = w.BOOL
user.GetWindowThreadProcessId.argtypes = (w.HWND, ct.POINTER(w.DWORD))
user.RegisterHotKey.argtypes = (w.HWND, ct.c_int, w.UINT, w.UINT)
user.UnregisterHotKey.argtypes = (w.HWND, ct.c_int)
user.PeekMessageW.argtypes = (ct.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
user.GetMessageW.argtypes = (ct.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
user.PostThreadMessageW.argtypes = (w.DWORD, w.UINT, w.WPARAM, w.LPARAM)
user.DispatchMessageW.argtypes = (ct.POINTER(w.MSG),)
user.DispatchMessageW.restype = ct.c_ssize_t
user.DefWindowProcW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
user.DefWindowProcW.restype = ct.c_ssize_t
user.RegisterClassW.argtypes = (ct.POINTER(WindowClass),)
user.RegisterClassW.restype = w.ATOM
user.CreateWindowExW.argtypes = (
    w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ct.c_int, ct.c_int, ct.c_int, ct.c_int,
    w.HWND, w.HMENU, w.HINSTANCE, ct.c_void_p,
)
user.CreateWindowExW.restype = w.HWND
user.DestroyWindow.argtypes = (w.HWND,)
user.UnregisterClassW.argtypes = (w.LPCWSTR, w.HINSTANCE)
user.LoadIconW.argtypes = (w.HINSTANCE, ct.c_void_p)
user.LoadIconW.restype = w.HICON
user.LoadImageW.argtypes = (w.HINSTANCE, w.LPCWSTR, w.UINT, ct.c_int, ct.c_int, w.UINT)
user.LoadImageW.restype = w.HICON
user.DestroyIcon.argtypes = (w.HICON,)
user.DestroyIcon.restype = w.BOOL
shell.Shell_NotifyIconW.argtypes = (w.DWORD, ct.POINTER(NotifyIcon))
application.CreateMutexW.argtypes = (ct.c_void_p, w.BOOL, w.LPCWSTR)
application.CreateMutexW.restype = w.HANDLE
application.CloseHandle.argtypes = (w.HANDLE,)
application.GetCurrentThreadId.restype = w.DWORD
application.GetModuleHandleW.argtypes = (w.LPCWSTR,)
application.GetModuleHandleW.restype = w.HMODULE
imm.ImmGetContext.argtypes = (w.HWND,)
imm.ImmGetContext.restype = w.HANDLE
imm.ImmReleaseContext.argtypes = (w.HWND, w.HANDLE)
imm.ImmSetCompositionFontW.argtypes = (w.HANDLE, ct.POINTER(LogFont))
imm.ImmGetCompositionStringW.argtypes = (w.HANDLE, w.DWORD, ct.c_void_p, w.DWORD)
imm.ImmGetCompositionStringW.restype = w.LONG


def enable_high_dpi():
    set_context = user.SetProcessDpiAwarenessContext
    set_context.argtypes = (ct.c_void_p,)
    set_context.restype = w.BOOL
    context = ct.c_void_p(-4)
    if not set_context(context):
        set_thread = user.SetThreadDpiAwarenessContext
        set_thread.argtypes = (ct.c_void_p,)
        set_thread.restype = ct.c_void_p
        set_thread(context)


def monitor_work_area(x, y):
    monitor = user.MonitorFromPoint(w.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    info = MonitorInfo(ct.sizeof(MonitorInfo))
    if monitor and user.GetMonitorInfoW(monitor, ct.byref(info)):
        area = info.work
        return area.left, area.top, area.right, area.bottom
    return None


def monitor_info(handle):
    info = MonitorInfoEx(ct.sizeof(MonitorInfoEx))
    if handle and user.GetMonitorInfoW(handle, ct.byref(info)):
        rect = lambda value: (value.left, value.top, value.right, value.bottom)
        return dict(id=info.device, bounds=rect(info.monitor), work=rect(info.work), primary=bool(info.flags & 1))
    return None


def monitor_at(x, y):
    return monitor_info(user.MonitorFromPoint(w.POINT(round(x), round(y)), MONITOR_DEFAULTTONEAREST))


def monitors():
    result = []
    callback_type = ct.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, ct.POINTER(w.RECT), w.LPARAM)
    @callback_type
    def collect(handle, _dc, _rect, _data):
        info = monitor_info(handle)
        if info:
            result.append(info)
        return True
    user.EnumDisplayMonitors.argtypes = (w.HDC, ct.c_void_p, callback_type, w.LPARAM)
    user.EnumDisplayMonitors(None, None, collect, 0)
    return sorted(result, key=lambda value: (not value['primary'], value['id']))


def window_dpi(hwnd):
    return user.GetDpiForWindow(hwnd) or 96


def single_instance():
    handle = application.CreateMutexW(None, False, r"Local\DesktopAI.DeepSeek")
    return handle, ct.get_last_error() != 183


def open_edge(url, profile=None):
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ["LOCALAPPDATA"]),
    ]
    edge = next((base / "Microsoft/Edge/Application/msedge.exe" for base in candidates
                 if (base / "Microsoft/Edge/Application/msedge.exe").is_file()), None)
    if edge is None:
        raise FileNotFoundError("未找到 Microsoft Edge，请先安装 Edge 后重试。")
    command = [str(edge)]
    if profile:
        command.append("--profile-directory=" + profile)
    subprocess.Popen([*command, url])


def open_chrome(url):
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ["LOCALAPPDATA"]),
    ]
    chrome = next((base / "Google/Chrome/Application/chrome.exe" for base in candidates
                   if (base / "Google/Chrome/Application/chrome.exe").is_file()), None)
    if chrome is None:
        raise FileNotFoundError("未找到 Google Chrome，请安装后再连接 ChatGPT。")
    subprocess.Popen([str(chrome), "--new-tab", url])


class InputMethod:
    def __init__(self, root, editor, size, family):
        self.font_size, self.family = size, family
        self.windows = tuple(dict.fromkeys((editor.winfo_id(), root.winfo_id())))
        self.composing = False

    def font(self, hwnd):
        return LogFont(height=-round(self.font_size * window_dpi(hwnd) / 72), weight=400,
                       charset=1, quality=5, face=self.family)

    def apply(self):
        # IMM can send a synchronous notification to Tk's window procedure.
        # Delay Tcl event servicing until Python has returned to Tk.
        service_mode = ct.CDLL("tcl86t.dll").Tcl_SetServiceMode
        service_mode.argtypes = (ct.c_int,)
        service_mode.restype = ct.c_int
        previous = service_mode(0)  # TCL_SERVICE_NONE
        try:
            for hwnd in self.windows:
                context = imm.ImmGetContext(hwnd)
                if context:
                    try:
                        imm.ImmSetCompositionFontW(context, ct.byref(self.font(hwnd)))
                    finally:
                        imm.ImmReleaseContext(hwnd, context)
        finally:
            service_mode(previous)

    def refresh(self, focused=True):
        was_composing = self.composing
        self.composing = False
        if focused:
            for hwnd in self.windows:
                context = imm.ImmGetContext(hwnd)
                if context:
                    try:
                        self.composing = imm.ImmGetCompositionStringW(context, 0x0008, None, 0) > 0  # GCS_COMPSTR
                    finally:
                        imm.ImmReleaseContext(hwnd, context)
                    if self.composing:
                        break
        return self.composing != was_composing


class NativeEvents:
    def __init__(self):
        self.events = SimpleQueue()
        self.ready = threading.Event()
        self.registered = False
        self.tray_added = False
        self.thread_id = None
        self.callback = CALLBACK(self.window_proc)
        self.thread = threading.Thread(target=self.listen, name="Desktop AI native events", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=1)

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == TRAY_MESSAGE:
            if lparam == 0x0203:
                self.events.put("show")
            elif lparam == 0x0205:
                self.events.put("menu")
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    def listen(self):
        message = w.MSG()
        user.PeekMessageW(ct.byref(message), None, 0, 0, 0)
        self.thread_id = application.GetCurrentThreadId()
        name = f"DesktopAiEvents_{os.getpid()}_{self.thread_id}"
        instance = application.GetModuleHandleW(None)
        window_class = WindowClass(procedure=self.callback, instance=instance, name=name)
        atom = user.RegisterClassW(ct.byref(window_class))
        hwnd = user.CreateWindowExW(0x80, name, name, 0, 0, 0, 0, 0, None, None, instance, None) if atom else None
        dpi = user.GetDpiForWindow(hwnd) if hwnd else 96
        icon_size = max(16, round(16 * dpi / 96))
        custom_icon = user.LoadImageW(None, str(APP_ASSETS / "desktop-ai.ico"),
                                      IMAGE_ICON, icon_size, icon_size, LR_LOADFROMFILE)
        tray = NotifyIcon(
            cbSize=ct.sizeof(NotifyIcon), hwnd=hwnd, identifier=1, flags=1 | 2 | 4,
            callback=TRAY_MESSAGE, icon=custom_icon or user.LoadIconW(None, 32512), tip="Desktop AI",
        )
        try:
            self.tray_added = bool(hwnd and shell.Shell_NotifyIconW(0, ct.byref(tray)))
            self.registered = bool(user.RegisterHotKey(None, HOTKEY, 0x4006, ord("I")))
            self.ready.set()
            while user.GetMessageW(ct.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == HOTKEY:
                    self.events.put("show")
                elif message.hWnd:
                    user.DispatchMessageW(ct.byref(message))
        finally:
            if self.registered:
                user.UnregisterHotKey(None, HOTKEY)
            if self.tray_added:
                shell.Shell_NotifyIconW(2, ct.byref(tray))
            if custom_icon:
                user.DestroyIcon(custom_icon)
            if hwnd:
                user.DestroyWindow(hwnd)
            if atom:
                user.UnregisterClassW(name, instance)

    def close(self):
        if self.thread.is_alive() and self.thread_id is not None:
            user.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
            self.thread.join(timeout=1)


class Integration:
    def __init__(self, root, show, menu):
        self.root, self.show, self.menu = root, show, menu
        self.hwnd = user.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
        self.listener = NativeEvents()
        self.hotkey = self.listener.registered
        self.tray_added = self.listener.tray_added

    def foreground(self):
        foreground = user.GetForegroundWindow()
        owner = w.DWORD()
        if foreground:
            user.GetWindowThreadProcessId(foreground, ct.byref(owner))
        return owner.value == os.getpid()

    def poll(self):
        while True:
            try:
                event = self.listener.events.get_nowait()
            except Empty:
                return
            if event == "show":
                self.show()
            elif event == "menu":
                self.menu()

    def close(self):
        self.listener.close()
