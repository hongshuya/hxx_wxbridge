# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
"""探针：这次进程跑在什么完整性级别？能不能截到微信窗口？

把结果写进 probe-il.txt（沙箱里跑也能读文件看结果）。
用法：python -u probe-il.py
"""
import ctypes
import datetime
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, HERE)

OUT = os.path.join(HERE, r"probe-il.txt")


def integrity_of(pid=None):
    h = (
        ctypes.windll.kernel32.GetCurrentProcess()
        if pid is None
        else ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    )
    if not h:
        return "OpenProcess 失败 err=%s" % ctypes.get_last_error()
    try:
        import win32con
        import win32security

        tok = win32security.OpenProcessToken(h, win32con.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(tok, win32security.TokenIntegrityLevel)
        return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
    except Exception as exc:  # noqa: BLE001
        return "ERR %r" % (exc,)
    finally:
        if pid is not None:
            ctypes.windll.kernel32.CloseHandle(h)


def bright(img):
    px = img.convert("RGB").load()
    w, h = img.size
    vals = [sum(px[x, y]) for y in range(0, h, 17) for x in range(0, w, 17)]
    return round(sum(1 for t in vals if t > 30) / len(vals), 3)


def main():
    lines = ["时间 %s" % datetime.datetime.now().isoformat(timespec="seconds"),
             "自己 pid %s 完整性=%s" % (__import__("os").getpid(), integrity_of())]
    try:
        import wxvision as W

        wins = [w for w in W.all_windows(120) if w["cls"] == W.WECHAT_CLS]
        for w in wins:
            lines.append("窗口 %s %r %s pid=%s" % (w["hwnd"], w["title"], w["rect"], w["pid"]))
        hint = [w for w in wins if w["title"]]
        if hint:
            v = W.WxVision(hwnd=hint[0]["hwnd"])
            img = v.capture()
            b = bright(img)
            lines.append("截图 %r 亮像素比=%s" % (hint[0]["title"], b))
            if b > 0.05:
                boxes = W.ocr_boxes(img, 0, img.width, 0, img.height, 0.5)
                lines.append("OCR 行数=%d" % len(boxes))
                for bx in boxes[-12:]:
                    lines.append("  %s (x0=%s y0=%s)" % (bx["text"], bx["x0"], bx["y0"]))
                img.save(os.path.join(HERE, r"probe-il.png"))
    except Exception as exc:  # noqa: BLE001
        lines.append("探针异常 %r" % (exc,))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
