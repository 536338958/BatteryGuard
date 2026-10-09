"""原生 Win32 系统托盘实现（Shell_NotifyIconW）。

不依赖 pystray / Pillow：直接用 ctypes 调 user32 / shell32，
自建隐藏窗口 + 消息循环，图标通过 Shell_NotifyIconW 注册到通知区域。

用法：
    tray = TrayIcon(icon_path="app.ico", tooltip="电池充电守护")
    tray.set_menu([(1, "打开控制面板"), (2, "退出")], on_command=handler)
    if tray.start(timeout=5):
        tray.notify("已启动", title="电池充电守护")
    ...
    tray.stop()
"""

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_USER = 0x0400
WM_TRAY = WM_USER + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIF_SHOWTIP = 0x00000080

NIIF_NONE = 0x00000000
NIIF_INFO = 0x00000001

NOTIFYICON_VERSION_3 = 3
NOTIFYICON_VERSION_4 = 4

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
LR_SHARED = 0x8000

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
MF_ENABLED = 0x00000000
MF_GRAYED = 0x00000001

TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0010
TPM_RETURNCMD = 0x0100

CLASS_NAME = "BatteryGuardTrayWindow"
WINDOW_NAME = "BatteryGuardTray"


# ---------------------------------------------------------------- 结构体
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),      # union: uTimeout / uVersion
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_ubyte * 16),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,      # LRESULT
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


# 结构体可选尺寸（不同 Windows 版本兼容）
_SZ_V1 = NOTIFYICONDATAW.dwState.offset       # 仅 tip
_SZ_V2 = NOTIFYICONDATAW.szInfo.offset        # 含气泡
_SZ_V3 = NOTIFYICONDATAW.guidItem.offset      # 含 dwInfoFlags
_SZ_V4 = ctypes.sizeof(NOTIFYICONDATAW)       # 完整（含 GUID）

# ---------------------------------------------------------------- 原型
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE

user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadIconW.restype = wintypes.HICON

user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong

user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.CreatePopupMenu.restype = wintypes.HANDLE
user32.AppendMenuW.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HANDLE]

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER),
                                            ctypes.POINTER(wintypes.RECT)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long   # HRESULT


# ---------------------------------------------------------------- 托盘类
class TrayIcon:
    """原生 Win32 托盘图标。"""

    def __init__(self, icon_path=None, tooltip="BatteryGuard",
                 on_click=None, on_double_click=None, on_command=None):
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.on_click = on_click
        self.on_double_click = on_double_click
        self.on_command = on_command

        self.hwnd = None
        self.hicon = None
        self.added = False
        self.cbsize = _SZ_V3
        self.ready = threading.Event()
        self.error = None
        self._wndproc_ref = None
        self._thread = None
        self._lock = threading.Lock()
        self._pending = None        # 待执行的「单击」定时器
        self._suppress_until = 0.0  # 双击后的静默期，避免第二个 UP 再开一次

    # ------------------------------------------------------------ 单击/双击防抖
    def _cancel_pending(self):
        if self._pending is not None:
            try:
                self._pending.cancel()
            except Exception:
                pass
            self._pending = None

    def _fire_click(self):
        self._pending = None
        handler = self.on_double_click or self.on_click
        if handler:
            try:
                handler()
            except Exception:
                pass

    def _schedule_click(self):
        """单击：延迟 0.35 秒执行；若期间收到双击则取消，保证双击只打开一次。"""
        self._cancel_pending()
        t = threading.Timer(0.35, self._fire_click)
        t.daemon = True
        self._pending = t
        t.start()

    # ------------------------------------------------------------ 图标资源
    def _load_icon(self):
        path = self.icon_path
        if path and os.path.isfile(path):
            h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return wintypes.HICON(h)
        # 兜底：系统预定义图标（IDI_INFORMATION = 32516）
        try:
            res = ctypes.cast(ctypes.c_void_p(32516), wintypes.LPCWSTR)
            h = user32.LoadIconW(None, res)
            if h:
                return wintypes.HICON(h)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------ 数据结构
    def _data(self, tip=None, info=None, info_title=None):
        d = NOTIFYICONDATAW()
        d.cbSize = self.cbsize
        d.hWnd = self.hwnd
        d.uID = 1
        d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        d.uCallbackMessage = WM_TRAY
        d.hIcon = self.hicon
        d.szTip = (tip if tip is not None else self.tooltip)[:127]
        if info is not None:
            d.uFlags |= NIF_INFO
            d.szInfo = info[:255]
            d.szInfoTitle = (info_title or self.tooltip)[:63]
            d.dwInfoFlags = NIIF_INFO
            d.uTimeout = 8000
        return d

    # ------------------------------------------------------------ 注册/更新
    def _add(self):
        last_err = 0
        for size in (_SZ_V3, _SZ_V2, _SZ_V1):
            self.cbsize = size
            d = self._data()
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(d))
            if ok:
                self.added = True
                # 切到 V3 行为（更好的消息/焦点处理），失败也不影响
                try:
                    v = NOTIFYICONDATAW()
                    v.cbSize = _SZ_V3
                    v.hWnd = self.hwnd
                    v.uID = 1
                    v.uTimeout = NOTIFYICON_VERSION_3
                    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(v))
                except Exception:
                    pass
                return True
            last_err = ctypes.get_last_error()
        self.error = f"Shell_NotifyIconW(NIM_ADD) 失败，GetLastError={last_err}"
        return False

    def update(self, tip=None, info=None, info_title=None):
        if not self.added:
            return False
        with self._lock:
            if tip is not None:
                self.tooltip = tip[:127]
            d = self._data(tip=tip, info=info, info_title=info_title)
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(d)))

    def notify(self, info, title=None):
        return self.update(info=info, info_title=title)

    def _remove(self):
        if not self.added:
            return
        try:
            d = NOTIFYICONDATAW()
            d.cbSize = self.cbsize
            d.hWnd = self.hwnd
            d.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(d))
        except Exception:
            pass
        self.added = False

    # ------------------------------------------------------------ 菜单
    def show_menu(self, items):
        """items: [(id, label), ...]；id 为 None 时插入分隔线。"""
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        for item in items:
            cid, label = item
            if cid is None:
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(hmenu, MF_STRING | MF_ENABLED, cid, label)
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu,
            TPM_LEFTALIGN | TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
            pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd and self.on_command:
            try:
                self.on_command(int(cmd))
            except Exception:
                pass

    # ------------------------------------------------------------ 窗口过程
    def _wndproc_cb(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                evt = int(lparam) & 0xFFFF
                if evt == WM_LBUTTONDBLCLK:
                    # 双击：取消待执行的单击，立即打开一次，并压掉紧随其后的 UP
                    self._cancel_pending()
                    self._suppress_until = time.time() + 0.8
                    self._fire_click()
                    return 0
                if evt == WM_LBUTTONUP:
                    if time.time() < self._suppress_until:
                        return 0
                    self._schedule_click()
                    return 0
                if evt in (WM_RBUTTONUP, WM_RBUTTONDOWN, WM_CONTEXTMENU):
                    if evt != WM_RBUTTONDOWN:
                        self.show_menu(self.menu_items())
                    return 0
            elif msg == WM_COMMAND:
                if self.on_command:
                    self.on_command(int(wparam) & 0xFFFF)
                return 0
            elif msg == WM_CLOSE:
                self._remove()
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def menu_items(self):
        provider = getattr(self, "menu_provider", None)
        if callable(provider):
            try:
                return provider()
            except Exception:
                pass
        return getattr(self, "_menu_items", [(1, "打开"), (2, "退出")])

    def set_menu(self, items):
        self._menu_items = items

    def set_menu_provider(self, provider):
        self.menu_provider = provider

    # ------------------------------------------------------------ 线程主体
    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc_cb)

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME
            user32.RegisterClassExW(ctypes.byref(wc))  # 已存在时返回 0，忽略

            self.hwnd = user32.CreateWindowExW(
                0, CLASS_NAME, WINDOW_NAME, 0,
                0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd:
                self.error = f"CreateWindowExW 失败，GetLastError={ctypes.get_last_error()}"
                self.ready.set()
                return

            self.hicon = self._load_icon()
            ok = self._add()
            if ok:
                try:
                    import battery_guard_panel  # 仅用于记录日志，失败无妨
                    battery_guard_panel.STATE.log("托盘图标已添加到系统通知区域")
                except Exception:
                    pass
            self.ready.set()
            if not ok:
                return

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self.error = f"托盘线程异常：{e}"
            self.ready.set()

    # ------------------------------------------------------------ 对外接口
    def start(self, timeout=5.0):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.ready.wait(timeout)
        return self.added

    def stop(self):
        try:
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        self._remove()

    def rect(self):
        """查询图标在通知区域的屏幕矩形，用于验证是否真的显示出来。"""
        if not self.added:
            return None
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        ident.hWnd = self.hwnd
        ident.uID = 1
        r = wintypes.RECT()
        hr = shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(r))
        if hr != 0:
            return None
        return (r.left, r.top, r.right, r.bottom)


def find_icon_file(names=("app.ico",)):
    """查找与程序同目录（或 PyInstaller 临时目录）中的图标文件。"""
    cands = []
    if getattr(sys, "frozen", False):
        try:
            cands.append(os.path.join(sys._MEIPASS, "app.ico"))
        except Exception:
            pass
        cands.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "app.ico"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
    cands.append(os.path.join(os.getcwd(), "app.ico"))
    for p in cands:
        if os.path.isfile(p):
            return p
    return None
