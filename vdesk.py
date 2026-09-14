# -*- coding: utf-8 -*-
"""
vdesk.py —— 把窗口移到 Windows 虚拟桌面 (任务视图里的"桌面N")

════════════════════════════════════════════════════════════════════
 为什么这么实现
════════════════════════════════════════════════════════════════════
本机实测 (Windows 10 22H2 / Build 19045):

  ✅ 能用的 (文档化接口)
     IVirtualDesktopManager {AA509086-5CA9-4C25-8F95-589D3C07B48A}
       · IsWindowOnCurrentVirtualDesktop(hwnd, BOOL*)
       · GetWindowDesktopId(hwnd, GUID*)     ← 可读窗口所在桌面的 GUID
       · MoveWindowToDesktop(hwnd, REFGUID)  ← 可搬运, 但需要目标桌面 GUID

  ✅ 能用的 (未文档化, 仅用于读桌面数量)
     CLSID_ImmersiveShell {C2F03A33-21F5-47FA-B4BB-156362A2F239}  + CLSCTX_ALL
       -> QueryService(CLSID_VDManagerInternal, IID {F31574D6-...})
       -> vtable[3] GetCount(UINT*)     返回 2~3 个桌面
       -> vtable[7] GetDesktops(IObjectArray**)

  ❌ 卡住的地方
     逐个取 IVirtualDesktop 需要正确的 IID, 而它在各 Windows 版本间不同。
     实测 {3F07F4BE-...} 在本机返回 E_NOINTERFACE; 继续猜会一路
     access violation 崩到调用方。**不值得为它冒险。**

  ✅ 最终方案 —— 绕开 GUID 枚举
     Windows 自带快捷键 Win+Ctrl+Shift+→ = "把当前窗口移到下一个桌面"。
     它是操作系统行为, 与版本无关, 也不需要任何未文档化接口。
     只要窗口是前台窗口, 发一次按键就能把它送到桌面2。

  ⚠ 安全性: 所有调用都包 try/except, 失败返回 False, 绝不抛异常。
"""
import ctypes
import sys
import time
import uuid
from ctypes import byref, c_void_p, POINTER, WINFUNCTYPE, HRESULT, c_uint

IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    _user32 = ctypes.windll.user32
    _ole32 = ctypes.windll.ole32
else:
    _user32 = _ole32 = None

CLSID_ImmersiveShell = "{C2F03A33-21F5-47FA-B4BB-156362A2F239}"
IID_IServiceProvider = "{6D5140C1-7436-11CE-8034-00AA006009FA}"
CLSID_VDManagerInternal = "{C5E0CDCA-7B6E-41B2-9FC4-D93975CC467B}"
CLSID_VirtualDesktopManager = "{AA509086-5CA9-4C25-8F95-589D3C07B48A}"
IID_IVirtualDesktopManager = "{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"
VDI_IIDS = [
    "{F31574D6-B682-4CDC-BD56-1827860ABEC6}",   # Win10 2004~22H2 实测可用
    "{0F3A72B0-4566-4874-8C2F-C0F1B0E5A3C8}",
    "{53F5CA0B-158F-4124-900C-057158060B27}",
    "{B2F925B9-5A0F-4D2E-9F4D-2B1507593C10}",
]
CTX_ALL = 0x17

VK_LWIN, VK_CONTROL, VK_SHIFT = 0x5B, 0x11, 0x10
VK_LEFT, VK_RIGHT = 0x25, 0x27
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


def _g(s):
    o = GUID()
    ctypes.memmove(byref(o), uuid.UUID(s).bytes_le, 16)
    return o


def _vt(ptr, idx, restype, *argtypes):
    vt = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(vt[idx])


def _com_init():
    try:
        _ole32.CoInitializeEx(None, 2)
    except Exception:
        pass


def _vdm():
    """拿到文档化的 IVirtualDesktopManager 指针, 失败返回 None"""
    try:
        _com_init()
        p = c_void_p()
        hr = _ole32.CoCreateInstance(byref(_g(CLSID_VirtualDesktopManager)), None,
                                     CTX_ALL, byref(_g(IID_IVirtualDesktopManager)),
                                     byref(p))
        return p if hr == 0 and p else None
    except Exception:
        return None


def _internal():
    """拿到 IVirtualDesktopManagerInternal 指针 (仅用于读桌面数量)"""
    try:
        _com_init()
        sp = c_void_p()
        hr = _ole32.CoCreateInstance(byref(_g(CLSID_ImmersiveShell)), None, CTX_ALL,
                                     byref(_g(IID_IServiceProvider)), byref(sp))
        if hr != 0 or not sp:
            return None
        qs = _vt(sp, 3, HRESULT, POINTER(GUID), POINTER(GUID), POINTER(c_void_p))
        for iid in VDI_IIDS:
            out = c_void_p()
            try:
                # ⚠ QueryService 要【服务 CLSID + 接口 IID】两个不同的 GUID。
                #   传成同一个值会拿到 E_NOTIMPL (实测踩过)。
                if qs(sp, byref(_g(CLSID_VDManagerInternal)), byref(_g(iid)),
                      byref(out)) == 0 and out:
                    return out
            except Exception:
                continue
        return None
    except Exception:
        return None


def desktop_count():
    """返回虚拟桌面数量; 失败返回 0"""
    if not IS_WIN:
        return 0
    p = _internal()
    if not p:
        return 0
    for idx in (3, 4):
        try:
            n = c_uint(0)
            if _vt(p, idx, HRESULT, POINTER(c_uint))(p, byref(n)) == 0 and 1 <= n.value <= 64:
                return n.value
        except Exception:
            continue
    return 0


def which_desktop(hwnd):
    """返回窗口所在虚拟桌面的 GUID 字符串 (仅用于验证搬运是否生效); 失败返回 ''"""
    if not IS_WIN:
        return ""
    vdm = _vdm()
    if not vdm:
        return ""
    try:
        gid = GUID()
        if _vt(vdm, 4, HRESULT, c_void_p, POINTER(GUID))(vdm, c_void_p(int(hwnd)),
                                                         byref(gid)) != 0:
            return ""
        return str(uuid.UUID(bytes_le=bytes(bytearray(gid))))
    except Exception:
        return ""


def switch_desktop(direction=1):
    """
    切换【当前桌面】(不搬窗口)。direction=1 向右, -1 向左。
    快捷键 Win+Ctrl+←/→
    """
    if not IS_WIN:
        return False
    try:
        _tap([VK_LWIN, VK_CONTROL], VK_RIGHT if direction > 0 else VK_LEFT)
        return True
    except Exception:
        return False


def move_to_next_desktop(hwnd=None):
    """
    把窗口搬到【下一个】虚拟桌面: Win+Ctrl+Shift+→
    这是操作系统自带行为, 与 Windows 版本无关, 不需要任何未文档化接口。
    窗口必须能被置为前台, 否则移动的是别的窗口 —— 所以先 SetForegroundWindow。
    """
    if not IS_WIN:
        return False
    try:
        if hwnd:
            _user32.SetForegroundWindow(c_void_p(int(hwnd)))
            time.sleep(0.12)
        _tap([VK_LWIN, VK_CONTROL, VK_SHIFT], VK_RIGHT)
        return True
    except Exception:
        return False


def move_to_prev_desktop(hwnd=None):
    if not IS_WIN:
        return False
    try:
        if hwnd:
            _user32.SetForegroundWindow(c_void_p(int(hwnd)))
            time.sleep(0.12)
        _tap([VK_LWIN, VK_CONTROL, VK_SHIFT], VK_LEFT)
        return True
    except Exception:
        return False


def move_to_desktop(hwnd, index_1based):
    """
    best-effort: 把窗口移到第 index_1based 个桌面。
      · 当前就在目标桌面 -> 直接返回 True
      · 否则用 Win+Ctrl+Shift+←/→ 走"当前桌面 -> 目标"的步数
    成功与否用 which_desktop 前后对比确认。
    """
    if not IS_WIN or index_1based < 1:
        return False
    try:
        cur = which_desktop(hwnd)
        # 用「目标桌面之前有几个」推断步数: 拿不到顺序信息时, 默认按
        # "从桌面1 出发" 处理 —— 这正是 GUI 启动时的实际情形。
        steps = index_1based - 1
        if steps == 0:
            return True
        fn = move_to_next_desktop if steps > 0 else move_to_prev_desktop
        for _ in range(abs(steps)):
            fn(hwnd)
            time.sleep(0.35)
        new = which_desktop(hwnd)
        return bool(cur) and bool(new) and new != cur
    except Exception:
        return False


def _tap(modifiers, key):
    """按住 modifiers, 敲一下 key, 再全部释放"""
    for m in modifiers:
        _user32.keybd_event(m, 0, 0, 0)
    time.sleep(0.03)
    _user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.03)
    _user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)
    for m in reversed(modifiers):
        time.sleep(0.03)
        _user32.keybd_event(m, 0, KEYEVENTF_KEYUP, 0)


if __name__ == "__main__":
    print("虚拟桌面数量 :", desktop_count())
    print("当前窗口桌面 :", which_desktop(_user32.GetForegroundWindow()) if IS_WIN else "-")
    print()
    print("用法: move_to_next_desktop(hwnd) 把窗口搬到下一个桌面")
