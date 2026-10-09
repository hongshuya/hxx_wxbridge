# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""重启小号微信（干净环境，不带软件渲染变量），必要时点「登录」，然后截图 + OCR 验证。"""
import io
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, HERE)

import ctypes  # noqa: E402

import win32gui  # noqa: E402
import win32process  # noqa: E402

import wxvision as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

user32 = ctypes.windll.user32
EXE = os.environ.get("WECHAT_EXE", ros.environ.get("WECHAT_EXE", r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"))
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


def small_proc():
    procs = W._weixin_procs()
    for p in procs:
        cli = (p.get("CommandLine") or "")
        if cli.strip().endswith("Weixin.exe\"") and "--type=" not in cli and "-autorun" not in cli:
            return p["ProcessId"], cli
    return None, None


def kill_tree(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    time.sleep(2.0)


def click_real(hwnd, px, py, win_rect, verify_pid):
    """置顶 + 落点校验 + 真点击。px,py 是窗口 rect 坐标。"""
    l, t = win_rect[0], win_rect[1]
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    time.sleep(0.4)
    sx, sy = l + px, t + py
    hit = win32gui.WindowFromPoint((sx, sy))
    if hit:
        _, hp = win32process.GetWindowThreadProcessId(hit)
        if hp != verify_pid:
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            return False, f"落点 pid {hp} != {verify_pid}"
    user32.SetCursorPos(sx, sy)
    time.sleep(0.2)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.6)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    return True, "clicked"


def main():
    pid, cli = small_proc()
    print("旧小号 pid", pid, "cmd", cli)
    if pid:
        kill_tree(pid)
    env = None  # 干净环境：继承当前进程环境，不带 QT_* 覆盖
    subprocess.Popen([EXE], creationflags=0x00000008, env=env)
    print("已启动新实例，等待窗口…")
    new_pid = None
    for _ in range(40):
        time.sleep(1.0)
        new_pid, _cli = small_proc()
        if new_pid and new_pid != pid:
            break
    print("新 pid", new_pid)
    time.sleep(3)
    # 找窗口
    wins = []

    def cb(h, _):
        _, p = win32process.GetWindowThreadProcessId(h)
        if p == new_pid and win32gui.IsWindowVisible(h):
            l, t, r, b = win32gui.GetWindowRect(h)
            if (r - l) > 120 and (b - t) > 120:
                wins.append((h, (l, t, r, b), (r - l) * (b - t)))
        return True

    win32gui.EnumWindows(cb, None)
    wins.sort(key=lambda x: -x[2])
    print("窗口:", [(h, r) for h, r, _ in wins])
    target = wins[0][0]
    # 若窗口偏小（登录窗），尝试 OCR 找「登录」并点击
    for attempt in range(6):
        rect = win32gui.GetWindowRect(target)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w >= 700:
            print("已是主窗口尺寸", rect)
            break
        print(f"[{attempt}] 登录窗 {rect}，OCR 找「登录」…")
        # 用 PrintWindow 截这个窗口
        tmp = W.WxVision.__new__(W.WxVision)
        tmp.pid, tmp.hwnd = new_pid, target
        img = tmp.capture(os.path.join(HERE, r"wx-login.png"))
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        res, _el = ocr(os.path.join(HERE, r"wx-login.png"))
        hit = None
        for box, txt, _sc in (res or []):
            if "登录" in txt:
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                hit = (int(sum(xs) / len(xs)), int(sum(ys) / len(ys)), txt)
                break
        if hit:
            print("  找到", hit)
            ok, msg = click_real(target, hit[0], hit[1], rect, new_pid)
            print("  点击", ok, msg)
        else:
            print("  没找到登录按钮，OCR 文本:", [t for _b, t, _s in (res or [])])
        time.sleep(3)
        # 重新找最大窗口
        wins = []

        def cb2(h, _):
            _, p = win32process.GetWindowThreadProcessId(h)
            if p == new_pid and win32gui.IsWindowVisible(h):
                l, t, r, b = win32gui.GetWindowRect(h)
                if (r - l) > 120 and (b - t) > 120:
                    wins.append((h, (l, t, r, b), (r - l) * (b - t)))
            return True

        win32gui.EnumWindows(cb2, None)
        wins.sort(key=lambda x: -x[2])
        if wins:
            target = wins[0][0]
    print("最终窗口", target, win32gui.GetWindowRect(target))
    tmp = W.WxVision.__new__(W.WxVision)
    tmp.pid, tmp.hwnd = new_pid, target
    tmp.capture(os.path.join(HERE, r"wx-after-restart.png"))
    print("已存 wx-after-restart.png")


if __name__ == "__main__":
    main()
