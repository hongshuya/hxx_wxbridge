# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""探针 15：把 pid 下**所有** HWND（顶层+子窗口，含隐藏）都过一遍 UIA 树，
用「深度 30、上限 500」的真递归，找出藏起来的可访问内容。
"""
import io
import json
import subprocess
import sys

import win32gui
import win32process

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from wx4py.core import uiautomation as uia  # noqa: E402

uia.SetGlobalSearchTimeout(4)


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
print("小号 pid:", pids)
pid = pids[0]

allh = []


def cb(hwnd, _):
    _, p = win32process.GetWindowThreadProcessId(hwnd)
    if p == pid:
        allh.append(hwnd)
    return True


win32gui.EnumWindows(cb, None)
# 每个顶层窗口的子窗口也枚举进来
extra = []
for h in list(allh):
    try:
        for c in win32gui.EnumChildWindows(h, lambda w, _l: _l.append(w), []):
            pass
    except Exception:
        pass


def kids(h):
    out = []
    try:
        win32gui.EnumChildWindows(h, lambda w, _l: _l.append(w), out)
    except Exception:
        pass
    return out


for h in list(allh):
    extra.extend(kids(h))
allh = list(dict.fromkeys(allh + extra))

rows = []
for h in allh:
    try:
        cls = win32gui.GetClassName(h)
        title = win32gui.GetWindowText(h)
        vis = bool(win32gui.IsWindowVisible(h))
        rect = list(win32gui.GetWindowRect(h))
    except Exception:
        continue
    try:
        root = uia.ControlFromHandle(h)
        nodes = []
        stack = [(root, 0)]
        while stack and len(nodes) < 500:
            node, d = stack.pop()
            nodes.append((d, node.ControlTypeName, node.ClassName, (node.Name or "")[:40], node.AutomationId))
            if d >= 30:
                continue
            try:
                for ch in node.GetChildren():
                    stack.append((ch, d + 1))
            except Exception:
                pass
        rows.append({"hwnd": h, "class": cls, "title": title, "visible": vis, "rect": rect,
                     "n": len(nodes),
                     "rows": [f"d{d} {t} cls={c} id={aid} :: {n}" for d, t, c, n, aid in nodes[:40]]})
    except Exception as e:
        rows.append({"hwnd": h, "class": cls, "title": title, "err": repr(e)[:80]})

rows.sort(key=lambda r: -r.get("n", 0))
print(json.dumps(rows[:12], ensure_ascii=False, indent=1, default=str)[:8000])
