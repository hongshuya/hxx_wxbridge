# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""微信小号视觉驱动（Vision Driver）——不读控件树，靠截图 + PostMessage。

设计要点：
- 只认「命令行不含 -autorun 且不含 --type=」的那个 Weixin.exe（小号），绝不碰主号。
- 截图用 PrintWindow(PW_RENDERFULLCONTENT)，窗口被遮挡也能截。
- 点击/按键全用 PostMessage，鼠标不动、焦点不抢。
- 像素坐标以截图为坐标系（截图 = 窗口 rect 大小），映射到 client 坐标后再发消息。

命令行自测：
    python wxvision.py shot  out.png
    python wxvision.py click 165 130
    python wxvision.py rows
"""
import ctypes
import json
import os
import subprocess
import sys
import time

import win32clipboard
import win32con
import win32gui
import win32process
import win32ui

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import numpy as np
except Exception:  # noqa: BLE001
    np = None

user32 = ctypes.windll.user32


def is_blank(img, threshold=0.02, step=17):
    """截图是不是「全黑」：亮像素（R+G+B>30）占比低于阈值。

    全黑 = 这个窗口当前根本没在渲染，或者我们的进程在被限制的沙箱里
    （低完整性进程读不到别的进程窗口内容，PrintWindow 只会给黑图）。
    """
    px = img.convert("RGB").load()
    w, h = img.size
    vals = [sum(px[x, y]) for y in range(0, h, step) for x in range(0, w, step)]
    if not vals:
        return True
    return (sum(1 for v in vals if v > 30) / len(vals)) < threshold


def my_integrity():
    """本进程的完整性级别（4096=Low 沙箱、8192=Medium 正常）。取不到返回 None。"""
    try:
        import win32api
        import win32security

        h = win32api.GetCurrentProcess()
        tok = win32security.OpenProcessToken(h, 0x0008)  # TOKEN_QUERY
        sid, _ = win32security.GetTokenInformation(tok, win32security.TokenIntegrityLevel)
        return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
    except Exception:  # noqa: BLE001
        return None


def system_idle_seconds():
    """系统空闲秒数（最后一次键盘/鼠标输入到现在）。

    用途：真键鼠发送必须抢前台，而抢过来之后**用户正在敲的字会落进小号的输入框**，
    再被我们的回车一起发出去（实测出现过「我是大肥鱼了」混进回复里）。所以发送前
    先看用户手是不是还在键盘上。
    """
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 999.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001
        return 999.0


def integrity_hint():
    lvl = my_integrity()
    if lvl is None:
        return "完整性级别未知"
    if lvl < 0x2000:
        return ("本进程跑在低完整性沙箱里（完整性=%s）→ 系统禁止它读取别的程序窗口，"
                "截图必然全黑；需要以正常权限（Medium）运行" % lvl)
    return "完整性=%s（正常，具备跨进程截图能力）" % lvl


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_PASTE = 0x0302
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
VK_DELETE = 0x2E
VK_BACK = 0x08
VK_DOWN = 0x28
VK_UP = 0x26
VK_TAB = 0x09
VK_ESC = 0x1B
VK_SHIFT = 0x10
VK_MENU = 0x12                                     # Alt：用来骗过 Windows 前台锁

WECHAT_CLS = "Qt51514QWindowIcon"                  # 微信 4.x 的窗口类名
SMALL_PID_PATH = os.path.join(HERE, r"wx-small.pid")   # 记住小号 pid（WMI 不可靠时的主判据）
# WGC（被遮挡也能抓）的降级参数：连续抓空多少次算坏、坏了多久后再试
WGC_FAILS_BEFORE_DEAD = 3
WGC_RETRY_AFTER = 600.0


def _weixin_procs():
    """列出 Weixin.exe 进程。

    优先用进程内 WMI（win32com）——**不要用 subprocess 调 powershell**：本机沙箱下
    子进程管道 stdio 会被拒（EPERM）或直接挂住，导致桥脚本起来后一句话都不打印。
    """
    try:
        import win32com.client as _wc

        wmi = _wc.GetObject("winmgmts:")
        q = "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='Weixin.exe'"
        return [{"ProcessId": int(p.ProcessId), "CommandLine": p.CommandLine or ""}
                for p in wmi.ExecQuery(q)]
    except Exception:  # noqa: BLE001  兜底：外部 powershell（仅在交互式前台可用）
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='Weixin.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        d = json.loads(ps.stdout or "[]")
        return d if isinstance(d, list) else [d]


def _norm_text(s):
    """只留字母数字并小写：OCR 会把「Alice」认成「.missu（粉毛？）」，必须这样比。"""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


_LAST_OCR_ID_TRY = 0.0
OCR_ID_INTERVAL = 60.0        # OCR 认人最多每 60 秒试一次（每次要截图+OCR，约 2-4 秒）


def identify_by_ocr(win, hints, force=False):
    """截图 + OCR 会话列表，判断这个微信窗口是不是小号。

    判据：小号的会话列表里一定会出现白名单联系人（如「Alice」）。
    主号的列表里只有小号（如「hong」），所以这个判据不会把主号认成小号。
    """
    global _LAST_OCR_ID_TRY
    hs = [_norm_text(h) for h in (hints or ()) if h]
    if not hs:
        return None
    now = time.time()
    if not force and now - _LAST_OCR_ID_TRY < OCR_ID_INTERVAL:
        return None
    _LAST_OCR_ID_TRY = now
    try:
        v = WxVision(pid=win["pid"], hwnd=win["hwnd"])
        img = v.capture()
        if is_blank(img):
            return None
        boxes = ocr_boxes(img, x0=0, x1=int(img.size[0] * 0.45), y0=60)
        text = _norm_text(" ".join(b["text"] for b in boxes))
    except Exception:  # noqa: BLE001
        return None
    for h in hs:
        if h and h in text:
            return win["pid"]
    return None


def find_small_pid(title_hints=()):
    """找小号的 pid。

    **不要依赖 WMI 的命令行**：本机（沙箱/pwsh 环境）WMI 的进程枚举时灵时不灵，
    实测会只返回十来个进程、看不到 Weixin.exe（但 win32gui.EnumWindows 一直正常）。
    所以主判据改成「从窗口反推 pid」：
      1) 标题命中白名单（= 小号正开着的那个联系人聊天窗）；
      2) 记录过的 wx-small.pid 且它还有微信窗口；
      3) OCR 会话列表认人（列表里出现白名单联系人 = 这是小号）；
      4) 有非「微信」标题的 Qt 窗口的进程（独立聊天窗，弱判据，垫底）；
      5) 实在不行才回退 WMI（无 -autorun 的那个命令行）。

    注意第 5 条：小号**也可能带 -autorun 启动**（2026-10-09 实测：开机自启把上一次用的号
    拉了起来，那个号正是小号），所以命令行判据不能当主判据，只能垫底。
    """
    hints = {h for h in (title_hints or ()) if h}
    wins = [w for w in all_windows(120) if w["cls"] == WECHAT_CLS]

    for w in wins:
        if w["title"] in hints:
            _remember_small_pid(w["pid"])
            return w["pid"]

    if os.path.exists(SMALL_PID_PATH):
        try:
            pid = int(open(SMALL_PID_PATH, encoding="utf-8").read().strip())
            if any(w["pid"] == pid for w in wins):
                return pid
        except Exception:  # noqa: BLE001
            pass

    big = [w for w in wins if w["size"][0] >= 300 and w["title"] == "微信"]
    for w in big:
        hit = identify_by_ocr(w, hints)
        if hit:
            _remember_small_pid(hit)
            return hit

    for w in wins:
        if w["title"] and w["title"] != "微信":
            _remember_small_pid(w["pid"])
            return w["pid"]

    for p in _weixin_procs():
        cli = (p.get("CommandLine") or "")
        if cli.strip().endswith("Weixin.exe\"") and "--type=" not in cli and "-autorun" not in cli:
            _remember_small_pid(p["ProcessId"])
            return p["ProcessId"]

    raise RuntimeError("没找到小号 Weixin.exe 进程（窗口反推、OCR 与 WMI 都没命中）")


def _remember_small_pid(pid):
    try:
        with open(SMALL_PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(pid))
    except Exception:  # noqa: BLE001
        pass


def all_windows(min_side=100):
    """全部可见顶层窗口（不靠 WMI）：[{hwnd,title,cls,rect,size,pid}]，按面积降序。"""
    out = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if w < min_side or h < min_side:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            out.append({"hwnd": hwnd, "title": title, "cls": win32gui.GetClassName(hwnd),
                        "rect": rect, "size": (w, h), "pid": pid})
        except Exception:  # noqa: BLE001
            pass

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: x["size"][0] * x["size"][1], reverse=True)
    return out



def list_windows(pid, min_side=150):
    """列出某进程下所有够大的顶层窗口：[{hwnd, title, cls, rect, size}]。"""
    wins = []

    def cb(hwnd, _):
        try:
            _, p = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        if p == pid and win32gui.IsWindowVisible(hwnd):
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) > min_side and (b - t) > min_side:
                wins.append({"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd),
                             "cls": win32gui.GetClassName(hwnd), "rect": (l, t, r, b),
                             "size": (r - l, b - t)})
        return True

    win32gui.EnumWindows(cb, None)
    wins.sort(key=lambda w: -w["size"][0] * w["size"][1])
    return wins


# ---------- 抓图后端：WGC（Windows.Graphics.Capture）优先，PrintWindow 兜底 ----------
# WGC 能抓「被遮挡」的窗口；PrintWindow 在窗口被完全盖住时只会给黑图。
# windows-capture 装在非默认目录（默认路径不可写），所以手工加进 sys.path。
PYLIBS = os.path.join(HERE, r"pylibs")
if os.path.isdir(PYLIBS) and PYLIBS not in sys.path:
    sys.path.insert(0, PYLIBS)

_WGC_ERR = None
WGC_LIB_OK = None


def wgc_available():
    """windows-capture 能不能导入（真正能不能抓窗口要跑一次才知道）。"""
    global _WGC_ERR, WGC_LIB_OK
    if WGC_LIB_OK is None:
        try:
            from windows_capture import WindowsCapture  # noqa: F401
            WGC_LIB_OK = True
        except Exception as e:  # noqa: BLE001
            WGC_LIB_OK = False
            _WGC_ERR = str(e)
    return WGC_LIB_OK


def wgc_error():
    return _WGC_ERR


# ---------- 图片气泡识别（用户发来的图 / 表情包） ----------
def _components(mask, bw, bh):
    """8 连通标记（块网格）。mask 是布尔列表，返回 [(bx0,by0,bx1,by1,块数)]。"""
    seen = [False] * (bw * bh)
    comps = []
    for i0 in range(bw * bh):
        if not mask[i0] or seen[i0]:
            continue
        stack = [i0]
        seen[i0] = True
        minx = maxx = i0 % bw
        miny = maxy = i0 // bw
        n = 0
        while stack:
            i = stack.pop()
            n += 1
            x, y = i % bw, i // bw
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < bw and 0 <= ny < bh:
                        j = ny * bw + nx
                        if mask[j] and not seen[j]:
                            seen[j] = True
                            stack.append(j)
        comps.append((minx, miny, maxx, maxy, n))
    return comps


def find_images(img, x0=0, y0=95, x1=None, y1=None, block=4, thr=0.60,
                min_blocks=200, min_w=70, min_h=50, solid=0.45):
    """在聊天区里找「图片气泡」。

    判据：把区域按 block×block 分块，块内「像图素」（饱和度高或很暗）的比例 > thr 才算图块；
    图块做 8 连通，取面积够大、长宽像图、且比较实心（不是文字行）的团块。
    背景是浅灰白（低饱和、亮），纯文字笔画太细（块内占比上不去），所以两者都落选。
    """
    if np is None:
        return []
    # 注意：必须用 int32——int16 下 255*299 会溢出成负数，整块会被误判成「很暗」。
    arr = np.asarray(img.convert("RGB"), dtype=np.int32)
    H, W = arr.shape[0], arr.shape[1]
    if x1 is None:
        x1 = W
    if y1 is None:
        y1 = H
    sub = arr[max(0, y0):min(H, y1), max(0, x0):min(W, x1)]
    if sub.size == 0:
        return []
    mx = sub.max(axis=2)
    mn = sub.min(axis=2)
    luma = (sub[:, :, 0] * 299 + sub[:, :, 1] * 587 + sub[:, :, 2] * 114) // 1000
    imageish = ((mx - mn) > 50) | (luma < 130)
    h, w = imageish.shape
    bh, bw = h // block, w // block
    if bh < 4 or bw < 4:
        return []
    frac = imageish[:bh * block, :bw * block].reshape(bh, block, bw, block).mean(axis=(1, 3))
    mask = (frac > thr).flatten().tolist()
    out = []
    for bx0, by0, bx1, by1, n in _components(mask, bw, bh):
        if n < min_blocks:
            continue
        px0 = x0 + bx0 * block
        py0 = y0 + by0 * block
        px1 = x0 + (bx1 + 1) * block
        py1 = y0 + (by1 + 1) * block
        if (px1 - px0) < min_w or (py1 - py0) < min_h:
            continue
        box_blocks = (bx1 - bx0 + 1) * (by1 - by0 + 1)
        if n / float(box_blocks) < solid:
            continue
        # 「像照片」过滤：纯色气泡（比如自己发的绿色文字泡）也是饱和色块，会被上面
        # 的判据整块选中，于是把一条文字消息误判成图片。照片的特点是颜色数极多：
        # 实测合成图里照片气泡 64x64 采样有 4085 种颜色，绿文字泡只有 36 种、
        # 白文字泡 38 种 → 阈值取 150 就能干净分开。
        crop = img.crop((min(px0, x1), min(py0, y1), min(px1, x1), min(py1, y1)))
        sample = crop.resize((min(64, crop.width), min(64, crop.height)))
        px = np.asarray(sample, dtype=np.int32)
        colors = len(set(map(tuple, px.reshape(-1, 3).tolist())))
        spread = float(px.std(axis=(0, 1)).mean())
        if colors < 150 and spread < 25:
            continue
        out.append({"box": (px0, py0, px1, py1), "area": n * block * block,
                    "colors": colors, "spread": round(spread, 1)})
    out.sort(key=lambda d: d["box"][1])
    return out


def save_bubble(img, box, outdir, pad=3):
    """裁出气泡存成 PNG；文件名用内容哈希（同一张图每次轮询都是同一个文件）。"""
    import hashlib
    os.makedirs(outdir, exist_ok=True)
    x0, y0, x1, y1 = box
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    crop = img.crop((x0, y0, x1 + pad, y1 + pad))
    buf = crop.tobytes()
    name = "img-%s.png" % hashlib.md5(buf).hexdigest()[:12]
    path = os.path.join(outdir, name)
    if not os.path.exists(path):
        crop.save(path)
    return path


class WgcGrabber:
    """用 WGC 持续抓某个窗口的最新帧（后台线程），窗口被遮挡也能抓到。"""

    def __init__(self, hwnd):
        import threading
        from windows_capture import WindowsCapture

        self.hwnd = hwnd
        self._frame = None
        self._closed = False
        self._err = None
        self._lock = threading.Lock()
        self._cap = None
        try:
            cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, draw_border=False)

            @cap.event
            def on_frame_arrived(frame, control):  # noqa: ANN001
                with self._lock:
                    self._frame = frame.frame_buffer.copy()

            @cap.event
            def on_closed():
                self._closed = True

            self._cap = cap
            cap.start_free_threaded()
        except Exception as e:  # noqa: BLE001
            self._err = "%s: %s" % (type(e).__name__, e)

    @property
    def error(self):
        return self._err

    def grab(self, timeout=4.0):
        """等一帧，返回 PIL.Image（RGB）；超时或出错返回 None。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                buf = self._frame
            if buf is not None:
                arr = buf[:, :, :3][:, :, ::-1]  # BGRA -> RGB
                return Image.fromarray(np.ascontiguousarray(arr)).convert("RGB")
            if self._err or self._closed:
                return None
            time.sleep(0.05)
        return None

    def stop(self):
        try:
            if self._cap is not None:
                self._cap.stop()
        except Exception:  # noqa: BLE001
            pass


class WxVision:
    def __init__(self, pid=None, min_side=300, hwnd=None, title=None, backend="auto"):
        self.pid = pid or find_small_pid()
        self.hwnd = hwnd or self._find_main_window(min_side, title)
        if not self.hwnd:
            raise RuntimeError(f"pid {self.pid} 下没找到主窗口")
        self.backend = backend          # auto | wgc | printwindow
        self.last_backend = None        # 实际用上的后端
        self._grabber = None
        self._wgc_dead = False
        self._wgc_dead_at = 0.0
        self._wgc_fails = 0
        self._cursor_home = None        # remember_cursor() 记下的真实光标位置

    # ---------- 窗口 ----------
    def _find_main_window(self, min_side, title=None):
        wins = list_windows(self.pid, min_side)
        if title:
            for w in wins:
                if title in w["title"]:
                    return w["hwnd"]
            return None
        return wins[0]["hwnd"] if wins else None

    def refresh(self):
        hwnd = self._find_main_window(300)
        if hwnd:
            self.hwnd = hwnd
        return self.hwnd

    @property
    def rect(self):
        return win32gui.GetWindowRect(self.hwnd)

    @property
    def client_size(self):
        """客户区像素尺寸 (w, h)。

        点击坐标（PostMessage 的 lParam / real_click）一律是**客户区**坐标，
        而截图尺寸随后端变化（WGC 抓的是客户区按 DPI 放大后的帧，实测 598x640 → 750x801），
        所以点击点必须用这里的尺寸算，不能用 img.size。
        """
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        return r - l, b - t

    @property
    def client_origin(self):
        """client 区左上角在屏幕上的坐标。"""
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    # ---------- 截图 ----------
    def _capture_wgc(self, timeout=1.5):
        """WGC 抓图（被遮挡也能抓）。不可用/抓不到就返回 None，由调用方兜底。

        降级策略：偶发抓空（窗口最小化等）不该永久废掉 WGC —— 连续失败
        WGC_FAILS_BEFORE_DEAD 次才降级，且 WGC_RETRY_AFTER 秒后自动再试一次；
        构造会话直接抛异常（WGC 不支持这个窗口）则立刻降级。
        """
        if self.backend == "printwindow":
            return None
        now = time.time()
        if self._wgc_dead:
            if now - self._wgc_dead_at < WGC_RETRY_AFTER:
                return None
            self._wgc_dead = False      # 冷却结束，再给 WGC 一次机会
            self._wgc_fails = 0
        if np is None or not wgc_available():
            self._wgc_dead = True
            self._wgc_dead_at = now
            return None
        if self._grabber is None or getattr(self._grabber, "hwnd", None) != self.hwnd:
            if self._grabber is not None:
                self._grabber.stop()
            self._grabber = WgcGrabber(self.hwnd)
        if self._grabber.error:
            self._grabber.stop()
            self._grabber = None
            self._wgc_fails = WGC_FAILS_BEFORE_DEAD
        else:
            img = self._grabber.grab(timeout)
            if img is not None:
                self._wgc_fails = 0
                return img
            self._wgc_fails += 1
        if self._wgc_fails >= WGC_FAILS_BEFORE_DEAD:
            self._wgc_dead = True
            self._wgc_dead_at = now
            # 会话可能已经卡死（再也收不到帧），拆掉它，等冷却结束后重建
            if self._grabber is not None:
                self._grabber.stop()
                self._grabber = None
        return None

    def capture(self, save=None, backend=None):
        """截窗口。backend=auto 时先用 WGC（可抓被遮挡的窗口），失败退 PrintWindow。"""
        backend = backend or self.backend
        img = None
        if backend in ("auto", "wgc"):
            img = self._capture_wgc()
            if img is not None:
                self.last_backend = "wgc"
        if img is None:
            img = self._capture_printwindow()
            self.last_backend = "printwindow"
        self.last_frame_size = img.size        # 供 click_frame() 换算框架→客户区坐标
        if save:
            img.save(save)
        return img

    def click_frame(self, px, py, double=False):
        """按「截图里的像素坐标」点击。

        截图尺寸随后端变化（WGC 是客户区 ×DPI，PrintWindow 是整窗），而行坐标是
        OCR 在截图上量出来的 —— 所以必须按「帧尺寸 / 客户区尺寸」换算，不能直接拿去点。
        """
        cw, ch = self.client_size
        fw, fh = getattr(self, "last_frame_size", None) or (cw, ch)
        self.click_client(int(round(px * cw / float(fw))), int(round(py * ch / float(fh))),
                          double=double)

    def _capture_printwindow(self):
        l, t, r, b = self.rect
        w, h = r - l, b - t
        hwnd_dc = win32gui.GetWindowDC(self.hwnd)
        mfc = win32ui.CreateDCFromHandle(hwnd_dc)
        sdc = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, w, h)
        sdc.SelectObject(bmp)
        user32.PrintWindow(self.hwnd, sdc.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRA", 0, 1).convert("RGB")
        win32gui.DeleteObject(bmp.GetHandle())
        sdc.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(self.hwnd, hwnd_dc)
        return img

    # ---------- 激活（Qt Quick 的聊天面板只在窗口激活时渲染） ----------
    def raise_window(self):
        """把窗口提到 z 序最前（**不激活、不抢焦点**）。

        被完全遮住的窗口系统会停止重绘，PrintWindow 抓到全黑；提层能让它重新渲染。
        返回 SetWindowPos 的结果（正常权限下 1；低完整性沙箱里会被拒绝、返回 0）。
        """
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        return user32.SetWindowPos(self.hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def activate(self):
        """把窗口拉到前台，返回原来的前台窗口句柄（供 restore_foreground）。

        四条路依次试：**敲一下 Alt 骗过前台锁** + AttachThreadInput + SetForegroundWindow
        → SwitchToThisWindow → ShowWindow(SW_MINIMIZE)+SW_RESTORE 兜底。

        为什么要有 Alt 那一下：Windows 只允许「最近有用户输入的进程」抢前台，
        后台进程直接 SetForegroundWindow 会「调用成功但前台没变」（实测用户在浏览器里
        看视频时 100% 失败 → 回复就发不出去）。敲一下 Alt 相当于告诉系统「我刚有输入」，
        这是官方文档留下的口子，代价只是可能点亮一下菜单栏。
        """
        prev = user32.GetForegroundWindow()
        if prev == self.hwnd:
            return None
        t_fg = user32.GetWindowThreadProcessId(prev, None)
        t_me = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(t_fg, t_me, True)
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)        # 按下 Alt
            user32.keybd_event(VK_MENU, 0, 2, 0)        # 松开 Alt（模拟「用户刚有输入」）
            time.sleep(0.05)
            user32.SetForegroundWindow(self.hwnd)
        finally:
            user32.AttachThreadInput(t_fg, t_me, False)
        time.sleep(0.2)
        if user32.GetForegroundWindow() == self.hwnd:
            return prev
        user32.SwitchToThisWindow(self.hwnd, True)
        time.sleep(0.3)
        if user32.GetForegroundWindow() == self.hwnd:
            return prev
        self.minimize_restore()
        time.sleep(0.3)
        return prev

    def restore_foreground(self, prev):
        """把前台还给之前的窗口。"""
        if not prev:
            return
        t_prev = user32.GetWindowThreadProcessId(prev, None)
        t_me = ctypes.windll.kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(t_prev, t_me, True)
        try:
            user32.SetForegroundWindow(prev)
        finally:
            user32.AttachThreadInput(t_prev, t_me, False)

    # ---------- 坐标映射 ----------
    def to_client(self, px, py):
        """截图像素 → client 坐标。

        截图 = 整窗 rect（含边框），client 原点在 rect 之内右移约 7px，
        所以是 **减去** 边框偏移，不是加。
        """
        l, t = self.rect[0], self.rect[1]
        cx0, cy0 = self.client_origin
        return px - (cx0 - l), py - (cy0 - t)

    # ---------- 输入 ----------
    def click_client(self, cx, cy, double=False):
        """按**客户区**坐标点击（posted 消息，不需要窗口在前台）。

        为什么要单独一个方法：`click()` 收的是「截图/整窗」坐标，会减掉边框偏移；
        而 WGC 截的是应用画布（客户区 ×DPI），坐标空间和整窗根本不是一回事
        （实测把画布坐标喂给 `click()` 会落到客户区之外，微信直接忽略）。
        """
        lp = ((int(cy) & 0xFFFF) << 16) | (int(cx) & 0xFFFF)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
        time.sleep(0.05)
        user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, 1, lp)
        time.sleep(0.06)
        if double:
            user32.PostMessageW(self.hwnd, WM_LBUTTONDBLCLK, 1, lp)
            time.sleep(0.06)
        user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp)

    def real_click_client(self, cx, cy):
        """按**客户区**坐标真点击（先 ClientToScreen 再 SetCursorPos）。"""
        sx, sy = win32gui.ClientToScreen(self.hwnd, (int(cx), int(cy)))
        user32.SetCursorPos(sx, sy)
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP

    def real_double_click_client(self, cx, cy, gap=0.07):
        """按**客户区**坐标真双击。

        为什么要真双击：微信 4.x 的「在新窗口中打开聊天」只认**系统级的双击**
        （两次 down/up 在双击间隔内）。posted 的 WM_LBUTTONDBLCLK 实测不够——
        点了等于没点，独立窗口一直不出来。
        """
        sx, sy = win32gui.ClientToScreen(self.hwnd, (int(cx), int(cy)))
        user32.SetCursorPos(sx, sy)
        time.sleep(0.12)
        for i in range(2):
            user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
            time.sleep(0.03)
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
            if i == 0:
                time.sleep(gap)

    def click(self, px, py, double=False):
        cx, cy = self.to_client(px, py)
        lp = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
        time.sleep(0.05)
        user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, 1, lp)
        time.sleep(0.06)
        if double:
            user32.PostMessageW(self.hwnd, WM_LBUTTONDBLCLK, 1, lp)
            time.sleep(0.06)
        user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp)

    def park_hover(self, cx=None, cy=None, restore_cursor=True):
        """把「鼠标悬停」从输入框工具栏上挪开。

        为什么需要：Qt 的 tooltip（实测「按住鼠标语音输入文字」）会在光标停到按钮上时弹出，
        而它是浮在最上层的一小块文字 —— OCR 会把它读成聊天区里的新消息，桥就会拿着
        这句界面文字去问 agent、白回一条。发完消息把 hover 挪到标题栏，tooltip 自然消失。
        """
        try:
            cw, ch = self.client_size
            if cx is None:
                cx, cy = int(cw * 0.5), int(ch * 0.06)
            lp = ((int(cy) & 0xFFFF) << 16) | (int(cx) & 0xFFFF)
            user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
        except Exception:  # noqa: BLE001
            pass
        if restore_cursor:
            try:
                import win32api
                win32api.SetCursorPos(self._cursor_home)
            except Exception:  # noqa: BLE001
                pass

    def remember_cursor(self):
        """记下真实光标位置（发完消息要还回去，别把用户的鼠标留在输入框上）。"""
        try:
            import win32api
            self._cursor_home = win32api.GetCursorPos()
        except Exception:  # noqa: BLE001
            self._cursor_home = None

    def key(self, vk, ctrl=False):
        if ctrl:
            user32.PostMessageW(self.hwnd, WM_KEYDOWN, VK_CONTROL, 0x001D0001)
            time.sleep(0.05)
        user32.PostMessageW(self.hwnd, WM_KEYDOWN, vk, 0x001E0001)
        time.sleep(0.05)
        user32.PostMessageW(self.hwnd, WM_KEYUP, vk, 0xC01E0001)
        if ctrl:
            time.sleep(0.05)
            user32.PostMessageW(self.hwnd, WM_KEYUP, VK_CONTROL, 0xC01D0001)

    def wheel(self, clicks, px=None, py=None):
        """滚轮。clicks>0 向上、<0 向下。WM_MOUSEWHEEL 的 lParam 用**屏幕**坐标。"""
        if px is None:
            l, t, r, b = self.rect
            px, py = (r - l) // 2, (b - t) // 2
        sx, sy = win32gui.ClientToScreen(self.hwnd, self.to_client(px, py))
        lp = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
        for _ in range(abs(clicks)):
            wparam = ((120 if clicks > 0 else -120) & 0xFFFF) << 16
            user32.PostMessageW(self.hwnd, WM_MOUSEWHEEL, wparam, lp)
            time.sleep(0.05)

    def minimize_restore(self):
        """最小化再恢复：Windows 会把「刚被恢复」的窗口提到前台。

        比单纯 SetForegroundWindow 可靠 —— 后者会被前台锁挡住（实测从桥里调用返回 1，
        但 GetForegroundWindow 还是别的窗口，于是真键鼠全打到了别的程序里）。
        """
        user32.ShowWindow(self.hwnd, 6)      # SW_MINIMIZE
        time.sleep(0.25)
        user32.ShowWindow(self.hwnd, 9)      # SW_RESTORE
        time.sleep(0.45)

    def post_paste(self):
        """剪贴板已放好内容时，只发一条 WM_PASTE（不抢焦点）。"""
        user32.PostMessageW(self.hwnd, WM_PASTE, 0, 0)

    def paste(self, text):
        """把文本写进剪贴板，然后给窗口发 WM_PASTE（不抢焦点）。"""
        set_clipboard(text)
        user32.PostMessageW(self.hwnd, WM_PASTE, 0, 0)

    def type_text(self, text):
        """逐字符 WM_CHAR（Qt 通常接受）。中文可能需要 paste 路线。"""
        for ch in text:
            user32.PostMessageW(self.hwnd, WM_CHAR, ord(ch), 0x001E0001)
            time.sleep(0.02)

    # ---------- 焦点（不抢前台）----------
    def focus_only(self):
        """把键盘焦点给这个窗口，但**不把它提到最前**（不动 Z 序、不换前台窗口）。

        为什么需要：微信 4.x 是 Qt Quick，Qt 只在窗口「有焦点」时才处理键盘消息 ——
        窗口不在前台时，光用 PostMessage 送 WM_CHAR 会被 Qt 直接丢掉（实测：posted
        打字发不出去 → 退回真键鼠 → 窗口被顶到最上层，用户看得见它跳上来）。
        `AttachThreadInput` + `SetFocus` 只动「焦点」：Windows 按前台线程的输入队列
        分发键盘，用户自己那个窗口的焦点与输入都不受影响；不用 SetForegroundWindow，
        Z 序不动 = 窗口不会跳到最上面。
        """
        import win32api
        import win32process
        my_tid = win32api.GetCurrentThreadId()
        tgt_tid = win32process.GetWindowThreadProcessId(self.hwnd)[0]
        attached = False
        try:
            if my_tid != tgt_tid:
                attached = bool(win32process.AttachThreadInput(my_tid, tgt_tid, True))
            win32gui.SetFocus(self.hwnd)
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(my_tid, tgt_tid, False)
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 真输入（需要窗口已在前台；发送回复时用） ----------
    def real_click(self, px, py):
        cx, cy = win32gui.ClientToScreen(self.hwnd, self.to_client(px, py))
        user32.SetCursorPos(cx, cy)
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
        time.sleep(0.05)
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP

    def real_key(self, vk, ctrl=False, shift=False):
        """真按键（keybd_event）。修饰键必须走真事件——PostMessage 的 WM_KEYDOWN
        不会改变 Qt 看到的修饰键状态，所以发 Ctrl+A 会变成打出一个 'a'。"""
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
        if shift:
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk, 0, 2, 0)
        if shift:
            user32.keybd_event(VK_SHIFT, 0, 2, 0)
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, 2, 0)
        time.sleep(0.05)

    def enter(self):
        self.key(VK_RETURN)

    # ---------- 基于 OCR 的语义操作 ----------
    def ocr(self, img=None, region=None, min_score=0.5):
        """对（当前或指定的）截图做 OCR。region=(x0,y0,x1,y1) 是截图坐标。"""
        if img is None:
            img = self.capture()
        r = region or (0, 0, None, None)
        return ocr_boxes(img, x0=r[0], y0=r[1], x1=r[2], y1=r[3], min_score=min_score)

    def click_text(self, target, img=None, region=None, exact=False, index=0):
        """找到文字并点击其中心。返回命中的 OCR 条目或 None。"""
        boxes = self.ocr(img=img, region=region)
        hits = [b for b in boxes if (b["text"] == target if exact else target in b["text"])]
        if not hits:
            return None
        hit = hits[min(index, len(hits) - 1)]
        self.click(hit["cx"], hit["cy"])
        return hit


def set_clipboard(text):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def get_clipboard():
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def set_clipboard_image(path_or_img):
    """把一张图片放进剪贴板（CF_DIB）——微信里 Ctrl+V 会直接变成「图片消息」。

    只用 win32clipboard 的话 SetClipboardData 收不了 buffer；这里手工 GlobalAlloc
    一块可移动内存塞 DIB（= BMP 文件去掉 14 字节 BITMAPFILEHEADER），交给
    SetClipboardData 后由系统接管所有权。
    返回 (宽, 高)。
    """
    import io
    im = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "BMP")
    data = buf.getvalue()[14:]          # 去掉 BITMAPFILEHEADER
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    # 必须声明 argtypes：64 位下句柄是 8 字节，不声明会被 ctypes 当成 int32 截断
    # （实测 GlobalLock 报 OverflowError: int too long to convert）。
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    GMEM_MOVEABLE, CF_DIB = 0x0002, 8
    if not u32.OpenClipboard(0):
        raise RuntimeError("OpenClipboard 失败")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise RuntimeError("GlobalAlloc 失败")
        p = k32.GlobalLock(h)
        if not p:
            raise RuntimeError("GlobalLock 失败")
        ctypes.memmove(p, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(CF_DIB, ctypes.c_void_p(h)):
            raise RuntimeError("SetClipboardData 失败")
    finally:
        u32.CloseClipboard()
    return im.size


# ---------- 列表行检测 ----------
ROW_H = 87  # 会话行高（微信 4.x 默认缩放下实测：130/217/304/391...）
FIRST_ROW_CENTER = 130  # 截图坐标里第一行中心 y
BADGE_X_RANGE = (118, 162)  # 头像右上角红点的 x 范围（左栏图标上的红点不在这个范围）
BADGE_DY = (-40, -2)  # 相对行中心的红点 y 范围


def detect_unread(img, first_center=FIRST_ROW_CENTER, row_h=ROW_H, max_rows=14):
    """按「红色未读圆点」检测哪些行有未读。返回行号（0 基）+ 红点位置。"""
    px = img.convert("RGB").load()
    w, h = img.size
    out = []
    for i in range(max_rows):
        yc = first_center + i * row_h
        if yc + row_h // 2 > h:
            break
        found = None
        for y in range(yc + BADGE_DY[0], yc + BADGE_DY[1]):
            if y < 0 or y >= h:
                continue
            for x in range(BADGE_X_RANGE[0], min(BADGE_X_RANGE[1], w)):
                r, g, b = px[x, y]
                if r > 185 and g < 95 and b < 95:
                    found = (x, y)
                    break
            if found:
                break
        out.append({"row": i, "y_center": yc, "unread": bool(found), "badge": found})
    return out


def row_center(row, first_center=FIRST_ROW_CENTER, row_h=ROW_H):
    return first_center + row * row_h


# ---------- OCR（延迟初始化，模型加载约 2s） ----------
_OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def ocr_boxes(img, x0=0, x1=None, y0=0, y1=None, min_score=0.5):
    """对图像（路径或 PIL）做 OCR，返回 [{text, score, x0,y0,x1,y1, cx,cy}]，可按区域过滤。"""
    if isinstance(img, str):
        path = img
    else:
        # 临时文件必须**按进程**命名：桥和测试/别的进程同时 OCR 时会抢同一个文件，
        # 实测报 `LoadImageError: cannot identify image file _ocr_tmp.png`（对方正在写）。
        path = os.path.join(HERE, r"_ocr_tmp_%d.png") % os.getpid()
        img.save(path)
    res, _el = ocr_engine()(path)
    out = []
    for box, txt, sc in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        if x1 is not None and bx0 > x1:
            continue
        if bx0 < x0 or by1 < y0:
            continue
        if y1 is not None and by0 > y1:
            continue
        try:
            score = float(sc)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            continue
        out.append({"text": txt, "score": score, "x0": bx0, "y0": by0, "x1": bx1, "y1": by1,
                    "cx": int((bx0 + bx1) / 2), "cy": int((by0 + by1) / 2)})
    out.sort(key=lambda d: (d["y0"], d["x0"]))
    return out


def detect_rows(img, strip=(112, 150), min_h=24, max_rows=16, right_limit=380):
    """动态找会话行：在竖条里找「头像色块」的连续区间。

    比固定行高稳（窗口大小/DPI 变化时行高会变：894 宽时 87px，1200 宽时 80px）。
    返回 [{row, y_top, y_bottom, y_center, x_avatar_center}]
    """
    px = img.convert("RGB").load()
    w, h = img.size
    x0, x1 = strip
    x1 = min(x1, w)
    runs = []
    cur = None
    for y in range(0, h):
        # 该行是否有“彩色/深色”像素（头像区域不会是纯白）
        vivid = 0
        for x in range(x0, x1):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn > 40 or mx < 200:
                vivid += 1
        hit = vivid >= 8
        if hit and cur is None:
            cur = y
        elif not hit and cur is not None:
            if y - cur >= min_h:
                runs.append((cur, y))
            cur = None
    if cur is not None and h - cur >= min_h:
        runs.append((cur, h))
    out = []
    for i, (a, b) in enumerate(runs[:max_rows]):
        out.append({"row": i, "y_top": a, "y_bottom": b, "y_center": (a + b) // 2,
                    "x_avatar_center": (x0 + x1) // 2})
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "shot"
    v = WxVision()
    print("pid", v.pid, "hwnd", v.hwnd, "rect", v.rect, "client_origin", v.client_origin, file=sys.stderr)
    if cmd == "shot":
        path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, r"wxvision-shot.png")
        v.capture(path)
        print(path)
    elif cmd == "click":
        x, y = int(sys.argv[2]), int(sys.argv[3])
        v.click(x, y)
        print("clicked", x, y)
    elif cmd == "rows":
        img = v.capture()
        for r in detect_unread(img):
            print(r)
