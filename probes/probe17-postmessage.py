# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""探针 17：测试能不能用 PostMessage（不移动鼠标、不抢焦点）点开左侧会话。
做法：截图 → 对比点击前后画面差异。
"""
import ctypes
import io
import json
import subprocess
import sys
import time

import win32gui
import win32process
import win32ui

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

user32 = ctypes.windll.user32
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0201, 0x0202, 0x0200
WM_LBUTTONDBLCLK = 0x0203


def weixin_procs():
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"name='Weixin.exe'\" | "
                         "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    d = json.loads(ps.stdout or "[]")
    return d if isinstance(d, list) else [d]


pid = [p["ProcessId"] for p in weixin_procs()
       if (p.get("CommandLine") or "").strip().endswith("Weixin.exe\"")
       and "--type=" not in (p.get("CommandLine") or "")][0]

targets = []


def cb(hwnd, _):
    _, p = win32process.GetWindowThreadProcessId(hwnd)
    if p == pid and win32gui.IsWindowVisible(hwnd):
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if (r - l) > 300 and (b - t) > 300:
            targets.append((hwnd, (l, t, r, b)))
    return True


win32gui.EnumWindows(cb, None)
hwnd, rect = targets[0]
print("hwnd", hwnd, "rect", rect)


def grab():
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hwnd_dc)
    sdc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    sdc.SelectObject(bmp)
    user32.PrintWindow(hwnd, sdc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRA", 0, 1).convert("RGB")
    win32gui.DeleteObject(bmp.GetHandle())
    sdc.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return img


def diff(a, b):
    pa = list(a.resize((120, 90)).getdata())
    pb = list(b.resize((120, 90)).getdata())
    n = sum(1 for x, y in zip(pa, pb) if sum(abs(i - j) for i, j in zip(x, y)) > 30)
    return round(n / len(pa), 3)


before = grab()
before.save(os.path.join(HERE, r"wx-click-before.png"))
out = {"rect": rect, "img": [before.width, before.height], "tries": []}

# 候选行中心（按截图上会话列表的行位置，x=165 是列表文字区）
rows = {"文件传输助手": 217, "腾讯新闻": 304, "兼职群": 391, "微信团队": 478}
for name, y in rows.items():
    sx, sy = rect[0] + 165, rect[1] + y
    cx, cy = win32gui.ScreenToClient(hwnd, (sx, sy))
    lp = (cy << 16) | (cx & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.15)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lp)
    time.sleep(0.08)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
    time.sleep(1.2)
    after = grab()
    d = diff(before, after)
    out["tries"].append({"row": name, "client_xy": [cx, cy], "diff": d})
    after.save(rfos.path.join(HERE, r"wx-click-{name}.png"))
    if d > 0.08:
        out["postmessage_works"] = True
        break

print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
