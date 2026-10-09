# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""验证「画面没变就不重复 OCR」的帧指纹短路。

背景：桥每 pollSeconds 秒截一次图、每张跑一次 OCR（~1 秒）。多窗口时这是主要开销，
而且内容根本没变的时候重认毫无意义。现在用 md5(像素) 做指纹，没变就跳过 OCR，
但每隔 FRAME_RECHECK_SECONDS 秒强制重认一次兜底。

用法: python test-frame-cache.py
"""
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, HERE)

import wxbridge as B  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location("trl", os.path.join(HERE, r"test-reply-logic.py"))
trl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trl)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [ok]   " if cond else "  [FAIL] ") + name + (("  " + str(extra)) if extra else ""))


def main():
    cfg = json.load(open(os.path.join(HERE, r"wxbridge-config.json"), encoding="utf-8"))
    cfg["statePath"] = os.path.join(HERE, r"test-state.json")      # 别污染真桥的去重表
    try:
        os.remove(cfg["statePath"])                            # 上一轮跑留下的记录会让去重用例假失败
    except OSError:
        pass
    bridge = B.Bridge(cfg)
    bridge.vision_for = lambda hwnd: bridge.v
    bridge.reply_to = lambda *a, **k: None

    calls = {"n": 0}
    real_messages = bridge.messages

    def counting_messages(*a, **k):
        calls["n"] += 1
        return real_messages(*a, **k)

    bridge.messages = counting_messages

    img_a = trl.build("text")
    img_b = trl.build("out")          # 内容不同 → 像素不同
    win = {"hwnd": 1, "name": "Alice", "kind": "direct"}

    bridge.capture = lambda: img_a
    r1 = bridge.poll_chat(win, dry=True, kind="direct")
    check("第 1 次：画面新 → 跑一次 OCR", calls["n"] == 1, calls["n"])

    r2 = bridge.poll_chat(win, dry=True, kind="direct")
    check("第 2 次：同一张图 → 跳过 OCR", calls["n"] == 1, calls["n"])
    check("跳过时返回缓存状态", any(s[2] == "chat-cached" for s in r2), r2)
    check("缓存状态里带的是上一次的尾巴文本",
          r2[0][1] == r1[0][1], "%r vs %r" % (r2[0][1], r1[0][1]))

    bridge.capture = lambda: img_b
    bridge.poll_chat(win, dry=True, kind="direct")
    check("画面变了 → 重新 OCR", calls["n"] == 2, calls["n"])

    # 强制重认兜底：把记录时间推回过去
    bridge.capture = lambda: img_b
    bridge._frame_fp["Alice"]["t"] = time.time() - (B.FRAME_RECHECK_SECONDS + 5)
    bridge.poll_chat(win, dry=True, kind="direct")
    check("超过 %g 秒强制重认一次" % B.FRAME_RECHECK_SECONDS, calls["n"] == 3, calls["n"])

    # 短路不能影响「该不该回」：同一张图重复轮询仍然只回一次
    calls["n"] = 0
    cfg2 = dict(cfg)
    b2 = B.Bridge(cfg2)
    b2.vision_for = lambda hwnd: b2.v
    sent = []

    def fake_reply(name, text, dry=True, sig=None, kind="direct"):
        sent.append(text)
        st = b2.state.setdefault(name, {})
        now = time.time()
        rep = [list(p) for p in st.get("replied", []) if now - p[1] < 600]
        rep.append([sig, now])
        st["replied"] = rep[-5:]

    b2.reply_to = fake_reply
    b2.capture = lambda: img_a
    for _ in range(3):
        b2.poll_chat(win, dry=False, kind="direct")
        bridge._frame_fp.clear()      # 强制每次都真 OCR（不然第二次就短路了）
        b2._frame_fp.clear()
    check("同一段消息轮询 3 次只回 1 次（短路不影响去重）", len(sent) == 1, len(sent))

    print()
    print("通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
