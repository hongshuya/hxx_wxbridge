# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""再开一个微信（第二个实例），并告诉你哪个窗口是哪个号。

微信 4.x 在 Windows 上默认只允许一个实例：双击图标只会把已有窗口提到前面。
但只要用「分离进程」的方式再拉起一次 Weixin.exe，就能开第二个（本机实测可行，
2026-10-09：主号 pid 16080 与小号 pid 20644 同时在线）。

用法：
    python wx-second.py --dry     # 只看现在有几个实例、分别是谁（不动任何东西）
    python wx-second.py           # 没有两个就再拉一个起来

认人方法：截会话列表做 OCR —— 列表里有「Alice」的是小号（alice），
列表里有「alice」的是主号。认不出来基本就是还没登录的登录窗。
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wxvision as W  # noqa: E402

WEIXIN = os.environ.get("WECHAT_EXE", ros.environ.get("WECHAT_EXE", r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"))
DETACHED = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
SMALL_HINT = "Alice"                       # 小号的会话列表里一定有主号
MAIN_HINT = "alice"                           # 主号的会话列表里一定有这个小号


def instances():
    """当前正在跑的微信主窗口（一个实例一个主窗口）。"""
    return [w for w in W.all_windows(120)
            if w.get("cls") == W.WECHAT_CLS and w.get("title") == "微信"
            and (w.get("size") or (0, 0))[0] >= 300]


def who(win):
    """靠 OCR 会话列表认人。返回 (标签, 依据)。"""
    try:
        img = W.WxVision(pid=win["pid"], hwnd=win["hwnd"]).capture()
        if W.is_blank(img):
            return "认不出（截图全黑）", ""
        text = W._norm_text(" ".join(
            b["text"] for b in W.ocr_boxes(img, x0=0, x1=int(img.size[0] * 0.45), y0=60)))
    except Exception as exc:  # noqa: BLE001
        return "认不出（%s）" % type(exc).__name__, ""
    if W._norm_text(SMALL_HINT) in text:
        return "小号（alice）", "列表里有「%s」" % SMALL_HINT
    if W._norm_text(MAIN_HINT) in text:
        return "主号", "列表里有「%s」" % MAIN_HINT
    return "认不出（可能还没登录）", ""


def report(wins):
    for w in wins:
        label, why = who(w)
        print("  pid=%-7s %-22s %s" % (w["pid"], label, why))


def main():
    dry = "--dry" in sys.argv
    print("微信安装：%s" % WEIXIN)
    wins = instances()
    print("现在有 %d 个微信实例：" % len(wins))
    report(wins)

    if len(wins) >= 2:
        print("\n两个都在跑了，不用再开。")
        return 0
    if dry:
        if wins:
            print("\n（--dry：只看看，不新开。去掉 --dry 就会再拉一个实例起来。）")
        else:
            print("\n（--dry：只看看。现在一个都没跑。）")
        return 0

    before = {w["pid"] for w in wins}
    print("\n正在再拉一个微信实例起来……")
    try:
        subprocess.Popen([WEIXIN], creationflags=DETACHED, close_fds=True)
    except Exception as exc:  # noqa: BLE001
        print("启动失败：%s: %s" % (type(exc).__name__, exc))
        return 1

    for _ in range(60):
        time.sleep(1)
        now = [w for w in instances() if w["pid"] not in before]
        if now:
            print("新实例起来了：")
            report(now)
            break
    else:
        print("等了 60 秒没看到新窗口（可能被已有实例抢了前面，或加载较慢）。")
        return 1

    print("\n接下来：在新窗口里点「登录」（多数情况要扫码或用手机确认）。")
    print("登录完成后回到这边，鲸鱼娘（桥）自己会认人——它靠会话列表认小号，")
    print("所以不用担心哪个窗口是哪个号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
