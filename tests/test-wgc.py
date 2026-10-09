# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""WGC 抓图后端的离线单元测试。

真机 WGC 只能在正常权限（Medium IL）下跑，本会话的 shell 是低完整性沙箱，
抓任何窗口都是全黑 —— 所以这里用假的 windows_capture 模块把 WgcGrabber /
WxVision 的 WGC 分支完整跑一遍（帧率、BGRA→RGB、超时、构造失败、降级缓存）。
"""
import sys
import time
import types

import numpy as np
import win32gui

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, HERE)

PASS = []
FAIL = []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 假 WGC 模块
class FakeFrame:
    def __init__(self, buf):
        self.frame_buffer = buf


class FakeCapture:
    """mode: 'frames' 收到帧 / 'silent' 永不送帧 / 'raise' 构造即失败"""

    instances = []

    def __init__(self, window_hwnd=None, cursor_capture=None, draw_border=None, mode="frames"):
        self.hwnd = window_hwnd
        self.mode = mode
        self._events = {}
        self.stopped = False
        FakeCapture.instances.append(self)
        if mode == "raise":
            raise Exception("Failed to convert item to GraphicsCaptureItem")

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    def start_free_threaded(self):
        if self.mode == "silent":
            return self
        buf = np.zeros((4, 5, 4), dtype=np.uint8)   # 4x5 BGRA
        buf[:, :, 0] = 10      # B
        buf[:, :, 1] = 20      # G
        buf[:, :, 2] = 30      # R
        buf[:, :, 3] = 255     # A
        for name, fn in self._events.items():
            if name == "on_frame_arrived":
                fn(FakeFrame(buf), None)
        return self

    def stop(self):
        self.stopped = True


def install_fake(mode="frames"):
    mod = types.ModuleType("windows_capture")

    def factory(**kw):
        return FakeCapture(mode=mode, **kw)

    mod.WindowsCapture = factory
    sys.modules["windows_capture"] = mod
    return mod


def fresh_vision(hwnd, backend="auto"):
    import wxvision as W
    W.WGC_LIB_OK = None          # 让 wgc_available() 重新判定（走假模块）
    v = W.WxVision.__new__(W.WxVision)
    v.pid = 0
    v.hwnd = hwnd
    v.backend = backend
    v.last_backend = None
    v._grabber = None
    v._wgc_dead = False
    v._wgc_dead_at = 0.0
    v._wgc_fails = 0
    return v


print("== WGC 抓图后端离线测试 ==")

import wxvision as W

# 找一个真 hwnd 当靶子（内容不重要，WGC 走的是假模块）
hwnds = []


def cb(h, _):
    if win32gui.IsWindowVisible(h):
        hwnds.append(h)


win32gui.EnumWindows(cb, None)
hwnd = hwnds[0]
print("  靶窗口 hwnd =", hwnd)

# 1) 真库在不在
check("真 windows-capture 可导入", W.wgc_available() is True, W.wgc_error())

# 2) 帧的 BGRA -> RGB 转换
install_fake("frames")
g = W.WgcGrabber(hwnd)
img = g.grab(timeout=2.0)
ok = img is not None and img.size == (5, 4)
check("WgcGrabber.grab 拿到帧且尺寸正确(5x4)", ok, None if img is None else img.size)
if ok:
    px = img.convert("RGB").getpixel((0, 0))
    check("BGRA→RGB 转换正确 (BGR=30,20,10 → RGB=30,20,10)",
          px == (30, 20, 10), px)

# 3) 没帧时超时返回 None（不抛异常）
install_fake("silent")
g2 = W.WgcGrabber(hwnd)
t0 = time.time()
img2 = g2.grab(timeout=0.6)
dt = time.time() - t0
check("永不送帧 → grab 超时返回 None 且不抛异常", img2 is None and dt >= 0.5, "%.2fs" % dt)

# 4) 构造抛异常 → 记进 .error，不往上炸
install_fake("raise")
g3 = W.WgcGrabber(hwnd)
check("构造失败被吞下并记进 error", g3.error is not None and "GraphicsCaptureItem" in g3.error, g3.error)

# 5) capture(backend='auto') 走 WGC 并标记 last_backend
install_fake("frames")
v = fresh_vision(hwnd)
img3 = v.capture()
check("capture(auto) 用上了 WGC", v.last_backend == "wgc" and img3 is not None, v.last_backend)

# 6) 偶发抓空不降级；连续 3 次才降级；降级后不再建 WGC 会话
install_fake("silent")
v2 = fresh_vision(hwnd)
img4 = v2.capture()                      # 第 1 次：WGC 超时 → 退 PrintWindow，但不该降级
check("单次抓空只兜底、不永久降级", v2.last_backend == "printwindow" and v2._wgc_dead is False,
      "_wgc_dead=%s fails=%s" % (v2._wgc_dead, v2._wgc_fails))
v2.capture()                             # 第 2 次
v2.capture()                             # 第 3 次 → 这时才降级
check("连续 3 次抓空后才降级", v2._wgc_dead is True and v2._wgc_fails == 3,
      "_wgc_dead=%s fails=%s" % (v2._wgc_dead, v2._wgc_fails))
t0 = time.time()
n_before = len(FakeCapture.instances)
v2.capture()                             # 降级后：不该再建 WGC
dt2 = time.time() - t0
check("降级已缓存：之后不再试 WGC（很快）",
      len(FakeCapture.instances) == n_before and dt2 < 0.5, "%.2fs" % dt2)

# 6b) 冷却期结束后会自动再试一次 WGC
v2._wgc_dead_at = time.time() - 3600     # 假装已经过了 1 小时
install_fake("frames")
img6 = v2.capture()
check("冷却结束后 WGC 自动复活", v2.last_backend == "wgc" and img6 is not None, v2.last_backend)

# 6c) 构造直接抛异常 → 立刻降级，不等 3 次
install_fake("raise")
v4 = fresh_vision(hwnd)
img7 = v4.capture()
check("WGC 不支持该窗口 → 立刻降级", v4._wgc_dead is True and v4.last_backend == "printwindow", v4.last_backend)

# 7) backend='printwindow' 强制不走 WGC
install_fake("frames")
v3 = fresh_vision(hwnd, backend="printwindow")
n0 = len(FakeCapture.instances)
img5 = v3.capture()
check("backend=printwindow 时不建 WGC 会话", len(FakeCapture.instances) == n0 and v3.last_backend == "printwindow", v3.last_backend)

# 8) 低完整性下 PrintWindow 全黑 → is_blank 能判出来
import PIL.Image as I
blank = I.new("RGB", (60, 40), (0, 0, 0))
noisy = I.new("RGB", (60, 40), (200, 200, 200))
check("is_blank 认全黑", W.is_blank(blank) is True)
check("is_blank 不误判正常画面", W.is_blank(noisy) is False)

print()
print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：" + "；".join(FAIL))
    sys.exit(1)
