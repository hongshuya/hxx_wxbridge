# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""探针 13：换更彻底的环境变量强制软件渲染后重启小号，检查主窗口是否出现控件树。
安全线：只操作命令行不含 -autorun / --type= 的那个顶层 Weixin.exe（小号）。
"""
import io
import json
import os
import subprocess
import sys
import time

import win32gui
import win32process

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from wx4py.core import uiautomation as uia  # noqa: E402
from wx4py.core.uia_wrapper import UIAWrapper  # noqa: E402
from wx4py.core.window import _count_uia_descendants  # noqa: E402

uia.SetGlobalSearchTimeout(5)
WECHAT = os.environ.get("WECHAT_EXE", ros.environ.get("WECHAT_EXE", r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"))
out = {}

# 1) 安装目录里的 GL 相关 dll
dlls = []
for root, _dirs, files in os.walk(os.path.dirname(WECHAT)):
    for f in files:
        if any(k in f.lower() for k in ("opengl", "libgles", "libegl", "d3dcompiler", "swiftshader", "vulkan")):
            dlls.append(os.path.join(root, f).replace(os.path.dirname(WECHAT) + "\\", ""))
out["gl_dlls"] = sorted(set(dlls))[:40]


def weixin_procs():
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"name='Weixin.exe'\" | "
                         "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    data = json.loads(ps.stdout or "[]")
    return data if isinstance(data, list) else [data]


procs = weixin_procs()
small = [p for p in procs if (p.get("CommandLine") or "").strip().endswith("Weixin.exe\"")
         and "--type=" not in (p.get("CommandLine") or "")]
out["small_before"] = [{"pid": p["ProcessId"], "cli": p["CommandLine"][:80]} for p in small]
if not small:
    out["abort"] = "没找到小号进程（命令行无参数的 Weixin.exe）"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    raise SystemExit(0)
old_pid = small[0]["ProcessId"]

# 2) 杀掉小号进程树（只杀它）
subprocess.run(["taskkill", "/PID", str(old_pid), "/T", "/F"], capture_output=True, text=True)
time.sleep(3)

# 3) 用更彻底的环境变量重启
env = dict(os.environ)
env.update({
    "QT_ANGLE_PLATFORM": "software",
    "QT_OPENGL": "software",
    "QT_QUICK_BACKEND": "software",
    "QSG_RENDER_LOOP": "basic",
})
DETACHED = 0x00000008 | 0x00000200
subprocess.Popen([WECHAT], env=env, creationflags=DETACHED,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
out["relaunched_env"] = {k: env[k] for k in ("QT_ANGLE_PLATFORM", "QT_OPENGL", "QT_QUICK_BACKEND", "QSG_RENDER_LOOP")}

# 4) 等待主窗口出现并检查树
snapshots = []
for i in range(10):
    time.sleep(6)
    procs = weixin_procs()
    small = [p for p in procs if (p.get("CommandLine") or "").strip().endswith("Weixin.exe\"")
             and "--type=" not in (p.get("CommandLine") or "")]
    if not small:
        snapshots.append({"t": i, "state": "小号进程还没起来"})
        continue
    pid = small[0]["ProcessId"]
    wins = []

    def cb(hwnd, _):
        _, p = win32process.GetWindowThreadProcessId(hwnd)
        if p == pid and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            wins.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    info = []
    for h in wins:
        try:
            n = _count_uia_descendants(UIAWrapper(h).root)
        except Exception as e:
            n = repr(e)[:60]
        c0 = uia.ControlFromHandle(h)
        info.append({"hwnd": h, "class": win32gui.GetClassName(h), "title": win32gui.GetWindowText(h),
                     "rect": list(win32gui.GetWindowRect(h)), "nodes": n,
                     "children": [c.ClassName for c in c0.GetChildren()][:6]})
    snapshots.append({"t": i, "pid": pid, "wins": info})
    if any(isinstance(w["nodes"], int) and w["nodes"] >= 8 for w in info):
        break

out["after"] = snapshots
print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
