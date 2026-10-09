# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""离线测试套件：把「不需要真微信窗口」的逻辑全部跑一遍。

覆盖：
  ① 表情标记解析（[表情: 无语] / 【表情：摸头】 等写法）
  ② meme_pick 选图（关键词命中 / 情绪 tag / 兜底不空手）
  ③ send_reply 的分段顺序（文字 → 图 → 文字，顺序不乱）
  ④ split_reply 长文分段
  ⑤ is_mentioned（@昵称 / 昵称出现 / mentionKeywords）
  ⑥ 群聊自发率：同一段消息只掷一次骰子（不会被 3 秒一轮的轮询反复掷成「回」）
  ⑦ 限速：两条消息之间的最小间隔 = 60 / rateLimitPerMinute
  ⑧ 窗口路由：白名单私聊、群名带成员数的群、其他窗口一律不理

用法: python test-offline.py      （全过返回 0）
"""
import json
import sys
import time

sys.path.insert(0, HERE)

import wxbridge as B  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class FakeVision:
    """替身窗口对象：只记录调用，不碰真窗口。"""

    def __init__(self):
        self.calls = []
        self.hwnd = 1
        # 客户区尺寸（点击点用它算，不是截图尺寸）
        self.client_size = (598, 640)

    def activate(self):
        self.calls.append("activate")
        return 12345

    def restore_foreground(self, prev):
        self.calls.append("restore(%s)" % prev)

    def real_click(self, x, y):
        self.calls.append("click(%s,%s)" % (x, y))

    def real_click_client(self, x, y):
        self.calls.append("click_client(%s,%s)" % (x, y))

    def click_client(self, x, y, double=False):
        self.calls.append("post_click_client(%s,%s)" % (x, y))

    def post_paste(self):
        self.calls.append("post_paste")

    def type_text(self, text):
        self.calls.append("type_text(%r)" % (text,))

    def post_ctrl_v(self):
        self.calls.append("post_ctrl_v")

    def focus_only(self):
        # 只调 SetFocus（不动 Z 序）、不抢前台 —— 记录成 focus_only，好让「不抢前台」的
        # 断言能看出到底调了什么。
        self.calls.append("focus_only")

    def key(self, vk, ctrl=False):
        self.calls.append("post_key(0x%02X%s)" % (vk, ",ctrl" if ctrl else ""))

    def minimize_restore(self):
        self.calls.append("minimize_restore")

    def remember_cursor(self):
        self.calls.append("remember_cursor")

    def park_hover(self, cx=None, cy=None, restore_cursor=True):
        self.calls.append("park_hover")

    def real_key(self, vk, ctrl=False, shift=False):
        self.calls.append("key(0x%02X%s)" % (vk, ",ctrl" if ctrl else ""))


RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("  %s %s%s" % ("✓" if cond else "✗", name, ("  " + extra) if extra else ""))


def cfg():
    c = json.load(open(os.path.join(HERE, r"wxbridge-config.json"), encoding="utf-8"))
    c["statePath"] = os.path.join(HERE, r"test-state.json")        # 别污染真桥的去重表
    return c


def new_bridge(c):
    b = B.Bridge(c)
    b.v = FakeVision()
    b.capture = lambda *a, **k: __import__("PIL.Image", fromlist=["Image"]).new("RGB", (613, 647), (245, 245, 245))
    b.targets = lambda: []
    return b


print("① 表情标记解析")
m = B.MEME_MARKER
cases = [("[表情: 无语]", "无语"), ("【表情：摸头】", "摸头"), ("[meme: happy ]", "happy"),
         ("[表情:蹲在墙角画圈圈，委屈自闭]", "蹲在墙角画圈圈，委屈自闭"), ("没有标记", None)]
for text, want in cases:
    found = m.search(text)
    got = found.group(1).strip() if found else None
    check("解析 %r → %r" % (text, got), got == want)

print("② meme_pick 选图")
for desc in ["无语", "摸头", "害羞", "开心", "上班摸鱼", "asdfqwer乱码"]:
    p = B.meme_pick(desc)
    check("meme_pick(%r) 有图且存在" % desc, p and __import__("os").path.isfile(p), str(p)[-40:] if p else "None")
p_tag = B.meme_pick(None, tag="happy")
check("meme_pick(tag=happy) 命中 happy 目录", p_tag and "\\happy\\" in p_tag, str(p_tag)[-40:] if p_tag else "None")

print("③ send_reply 顺序（文字 → 图 → 文字）")
b = new_bridge(cfg())
seq = []
b.send_text = lambda t: seq.append(("text", t))
b.send_meme = lambda d: seq.append(("meme", d))
b.send_reply("先说话\n[表情: 无语]\n后说话")
check("三段齐全且顺序正确", seq == [("text", "先说话"), ("meme", "无语"), ("text", "后说话")], repr(seq))
seq.clear()
b.send_reply("只有文字")
check("无标记时只发文字", seq == [("text", "只有文字")], repr(seq))
seq.clear()
b.send_reply("[表情: 开心]")
check("只有标记时只发图", seq == [("meme", "开心")], repr(seq))

print("④ split_reply 分段（Bridge 方法）")
long_text = "\n".join("第%d段" % i + "字" * 300 for i in range(5))
chunks = b.split_reply(long_text)
check("长文被切成多段且每段 <= 900", len(chunks) > 1 and all(len(c) <= 900 for c in chunks), "%d 段" % len(chunks))
check("短文不切", b.split_reply("嗯") == ["嗯"])
check("空行被丢掉", b.split_reply("a\n\n\nb") == ["a\nb"])

print("⑤ is_mentioned")
for text, want in [("@hong 在吗", True), ("hong 你怎么看", True), ("@hong：说说", True),
                   ("@所有人 开会", False), ("今天天气不错", False)]:
    check("%r → %s" % (text, want), b.is_mentioned(text) == want)
c2 = cfg(); c2["mentionKeywords"] = ["鲸鱼娘"]
b2 = new_bridge(c2)
check("mentionKeywords 生效", b2.is_mentioned("鲸鱼娘在不在") is True)

print("⑥ 群聊：有人 @ 必回；没人 @ 时同一段消息只掷一次骰子")
from PIL import Image as _Image  # noqa: E402
from PIL import ImageDraw as _ImageDraw  # noqa: E402

GROUPCFG = cfg()


def group_poll(rate, rnd, mention, rounds=3, primed=True):
    """用真 poll_chat 跑 rounds 轮，返回 (回了几次, random 被调了几次, state)。

    `primed=True` 模拟「这个群早就接手过了」——否则第一轮会走「刚接手不补课」的分支
    （那会直接 return，测不到 @ 与掷骰子的逻辑）；要测补课就传 primed=False。
    """
    c3 = dict(GROUPCFG)
    c3["groupSpontaneousRate"] = rate
    b3 = B.Bridge(c3)
    b3.state = {}
    if primed:
        b3._primed.add("兼职群")
    b3.vision_for = lambda hwnd: FakeVision()
    frames = {"i": 0}

    def make_img():
        """每轮都画得不一样：否则 poll_chat 的「像素没变就跳过 OCR」会直接返回，
        测不到「同一段消息只掷一次骰子」这条逻辑（那才是这个用例要验证的）。"""
        frames["i"] += 1
        im = _Image.new("RGB", (613, 647), (245, 245, 245))
        _ImageDraw.Draw(im).line([(20 + frames["i"], 40), (300, 40)], fill=(30, 30, 30), width=3)
        return im

    b3.capture = lambda *a, **k: make_img()
    text = "@hong 在吗" if mention else "今天天气不错"
    b3.messages = lambda img, x0, y0, w, img_regions=None: [{"y": 300, "x0": 100, "cx": 200, "side": "in", "text": text}]
    b3.grouped = lambda msgs, w: msgs
    b3.image_items = lambda img, w, h, regions=None, px=0: []
    sent, calls = [], {"n": 0}

    def fake_reply(name, txt, dry=True, sig=None, kind="group"):
        """替身 reply_to：必须照抄真 reply_to 的记账（否则去重测不出来）。"""
        sent.append(txt)
        st = b3.state.setdefault(name, {})
        now = time.time()
        rep = [list(p) for p in st.get("replied", []) if now - p[1] < 600]
        rep.append([sig, now])
        st["replied"] = rep[-5:]

    b3.reply_to = fake_reply
    real_random, real_save = B.random.random, B.save_json
    B.random.random = lambda: (calls.__setitem__("n", calls["n"] + 1) or rnd)
    B.save_json = lambda *a, **k: None          # 别污染真 state 文件
    try:
        for _ in range(rounds):
            b3.poll_chat({"hwnd": 1, "name": "兼职群", "kind": "group"}, dry=False, kind="group")
    finally:
        B.random.random, B.save_json = real_random, real_save
    return len(sent), calls["n"], b3.state


n_sent, n_rand, st3 = group_poll(0.0, 0.0, mention=True)
check("@ 必回（自发率 0% 也回），且不掷骰子", n_sent == 1 and n_rand == 0, "回 %d 次 / random %d 次" % (n_sent, n_rand))
n_sent, n_rand, _ = group_poll(1.0, 0.5, mention=False)
check("没人 @、自发率 100% → 3 轮只回 1 次（掷一次骰子）", n_sent == 1 and n_rand == 1,
      "回 %d 次 / random %d 次" % (n_sent, n_rand))
n_sent, n_rand, _ = group_poll(0.05, 0.99, mention=False)
check("没人 @、掷到不回 → 0 次，且只掷一次", n_sent == 0 and n_rand == 1, "回 %d 次 / random %d 次" % (n_sent, n_rand))
n_sent, n_rand, st3 = group_poll(0.05, 0.01, mention=False)
check("没人 @、掷到回 → 1 次", n_sent == 1 and n_rand == 1, "回 %d 次 / random %d 次" % (n_sent, n_rand))
check("decisions 记下了这段尾巴", bool(st3.get("兼职群", {}).get("decisions")), repr(list(st3.get("兼职群", {}).get("decisions", {}))))
n_sent, _, _ = group_poll(1.0, 0.5, mention=True, rounds=1, primed=False)
check("群刚接手 → 旧消息不补课（@ 也不回）", n_sent == 0, "回 %d 次" % n_sent)

print("⑦ 限速")
c4 = cfg(); c4["rateLimitPerMinute"] = 600          # 最小间隔 0.1 秒
b4 = new_bridge(c4)
import wxvision as W  # noqa: E402
W.set_clipboard = lambda t: True
b4._last_send = 0.0
t0 = time.time()
b4.send_text("一")
b4.send_text("二")
elapsed = time.time() - t0
check("两条之间至少等 60/rate 秒", elapsed >= 0.1, "%.2f 秒" % elapsed)

print("⑧ 窗口路由（白名单 / 群 / 其他）")
c5 = cfg()
b5 = B.Bridge(c5)                    # 注意：不能用 new_bridge（它把 targets 换成了空桩）
fake_windows = [
    {"hwnd": 11, "title": "Alice", "cls": "Qt51514QWindowIcon", "rect": (0, 0, 613, 647), "size": (613, 647)},
    {"hwnd": 12, "title": "兼职群(78)", "cls": "Qt51514QWindowIcon", "rect": (0, 0, 613, 647), "size": (613, 647)},
    {"hwnd": 13, "title": "陌生人甲", "cls": "Qt51514QWindowIcon", "rect": (0, 0, 613, 647), "size": (613, 647)},
    {"hwnd": 14, "title": "微信", "cls": "Qt51514QWindowIcon", "rect": (0, 0, 894, 647), "size": (894, 647)},
]
real_find, real_list = W.find_small_pid, W.list_windows
W.find_small_pid = lambda hints=(): 20644
W.list_windows = lambda pid, min_side=150: fake_windows
try:
    tg = {t["name"]: t["kind"] for t in b5.targets()}
finally:
    W.find_small_pid, W.list_windows = real_find, real_list
check("白名单里的人 → direct", tg.get("Alice") == "direct", repr(tg))
check("带成员数的群名 → group（自动认群）", tg.get("兼职群") == "group", repr(tg))
check("白名单外的普通人 → 不理", "陌生人甲" not in tg, repr(tg))
check("微信主窗口 → 不理", "微信" not in tg, repr(tg))

print("⑨ 回复不抢前台（posted 路线）")
c6 = cfg(); c6["rateLimitPerMinute"] = 600
b6 = new_bridge(c6)
b6._last_send = 0.0
b6._verify_sent = lambda t, **kw: True          # 假装截图确认到了
W.set_clipboard = lambda t: True
b6.send_text("你好呀")
calls = b6.v.calls
real_moves = [c for c in calls if c.startswith("click(") or c == "activate"
              or c == "minimize_restore"]
check("发文字全程没有真鼠标/激活", not real_moves, repr(real_moves))
check("发文字用了 posted 点击输入框", any(c.startswith("post_click_client") for c in calls), repr(calls))
check("发文字用了 WM_CHAR 打字", any(c.startswith("type_text") for c in calls), repr(calls))
check("发文字按了回车", "post_key(0x0D)" in calls, repr(calls))

b7 = new_bridge(c6)
b7._last_send = 0.0
b7._verify_sent = lambda t, **kw: True
b7.send_text("中文试试")
check("WM_CHAR 不认时才会用剪贴板", "post_paste" in b7.v.calls or "type_text" in " ".join(b7.v.calls),
      repr(b7.v.calls))

print("⑩ 行点击默认 posted（不顶窗口）")
import inspect  # noqa: E402
sig = inspect.signature(B.Bridge.open_row)
check("open_row 默认 real=False", sig.parameters["real"].default is False, str(sig.parameters["real"].default))

ok = sum(1 for _n, v in RESULTS if v)
print("\n结果: %d/%d 通过" % (ok, len(RESULTS)))
bad = [n for n, v in RESULTS if not v]
if bad:
    print("失败项:")
    for n in bad:
        print("  ✗ " + n)
return_code = 0 if ok == len(RESULTS) else 1
sys.exit(return_code)
