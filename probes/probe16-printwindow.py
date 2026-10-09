# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""探针 16：测试能不能截到小号主窗口的画面（决定 OCR 路线可行性）。
用 PrintWindow(PW_RENDERFULLCONTENT=2)，Qt Quick/OpenGL 窗口常常只能这样截到。
"""
import ctypes
import io
import json
import subprocess
import sys
from ctypes import wintypes

import win32gui
import win32process
import win32ui

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

user32 = ctypes.windll.user32
out = {"shots": []}


def weixin_procs():
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"name='Weixin.exe'\" | "
                         "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    d = json.loads(ps.stdout or "[]")
    return d if isinstance(d, list) else [d]


pids = [p["ProcessId"] for p in weixin_procs()
        if (p.get("CommandLine") or "").strip().endswith("Weixin.exe\"")
        and "--type=" not in (p.get("CommandLine") or "")]
pid = pids[0]
out["pid"] = pid

targets = []


def cb(hwnd, _):
    _, p = win32process.GetWindowThreadProcessId(hwnd)
    if p == pid and win32gui.IsWindowVisible(hwnd):
        r = win32gui.GetWindowRect(hwnd)
        if (r[2] - r[0]) > 300 and (r[3] - r[1]) > 300:
            targets.append(hwnd)
    return True


win32gui.EnumWindows(cb, None)
out["targets"] = targets


def shot(hwnd, path):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRA", 0, 1)
    img.save(path)
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    # 统计非黑像素比例
    px = img.convert("RGB").resize((80, 60)).getdata()
    nonblack = sum(1 for p in px if sum(p) > 40)
    colors = len(set(px))
    return {"printwindow_ret": ok, "size": [w, h], "nonblack_ratio": round(nonblack / len(px), 3),
            "distinct_colors": colors, "path": path}


for i, hwnd in enumerate(targets[:2]):
    p = rfos.path.join(HERE, r"wx-shot-{i}.png")
    try:
        out["shots"].append(shot(hwnd, p))
    except Exception as e:
        out["shots"].append({"hwnd": hwnd, "err": repr(e)[:200]})

print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
