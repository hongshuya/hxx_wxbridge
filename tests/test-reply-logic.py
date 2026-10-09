# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""离线验证桥的「读 → 判断该不该回 → 回什么」逻辑。

不碰真实微信窗口：合成聊天截图喂给 Bridge.poll_chat，把 reply_to 换成记录器，检查：
  ① 认对左右（in/out）与时间戳；② 对方连续几条合并成一条待回文本；③ 同一张图不重复回；
  ④ 最新的是一条图片消息时，待回文本里带 [图片] <路径>；⑤ 最新的是自己发的就不回。

注意：合成图的内容必须画在 y>=95 以下 —— 桥会跳过顶部 95px（真实窗口那里是标题栏）。
用法: python test-reply-logic.py
"""
import json
import os
import sys
import time

sys.path.insert(0, HERE)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
import numpy as np  # noqa: E402

import wxbridge as B  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

FONT = r"C:\Windows\Fonts\msyh.ttc"
W, H = 613, 647
TOP = 100          # 内容从标题栏下面开始（y>=95 才不会被跳过）


def font(size=16):
    return ImageFont.truetype(FONT, size)


def wrap(draw, text, f, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=f) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def bubble(draw, y, text, side, f, pad_x=10, pad_y=8, line_h=22, max_w=250):
    lines = wrap(draw, text, f, max_w)
    w = int(max(draw.textlength(l, font=f) for l in lines)) + pad_x * 2
    h = line_h * len(lines) + pad_y * 2
    x0 = 110 if side == "in" else W - 9 - w
    draw.rounded_rectangle([x0, y, x0 + w, y + h], 6,
                           fill=(255, 255, 255) if side == "in" else (149, 236, 105))
    for i, line in enumerate(lines):
        draw.text((x0 + pad_x, y + pad_y + i * line_h), line, font=f, fill=(20, 20, 20))
    return y + h + 16


def stamp(draw, y, text="22:20"):
    f = font(13)
    tw = draw.textlength(text, font=f)
    draw.text(((W - tw) / 2, y), text, font=f, fill=(150, 150, 150))
    return y + 26


def photo_at(img, y, seed=11, w=216, h=170):
    rng = np.random.default_rng(seed)
    img.paste(Image.fromarray(rng.integers(0, 255, (h, w, 3), dtype=np.uint8)), (110, y))
    return y + h + 20


def caption_photo(img, y, caption="好的 现在 我是大肥鱼了", seed=23, w=216, h=170):
    """带文字的「表情包」：照片底 + 上面写几行字（专门验「图上的字不算消息」）。"""
    photo_at(img, y, seed=seed, w=w, h=h)
    d = ImageDraw.Draw(img)
    f = font(20)
    d.text((118, y + 20), caption.split(" ")[0], font=f, fill=(255, 255, 255))
    d.text((118, y + 50), caption.split(" ")[1], font=f, fill=(255, 255, 255))
    return y + h + 20


def build(ending):
    """ending: 'text' = 最新一条是对方文字；'photo' = 最新一条是对方图片；'out' = 最新是自己发的。

    注意纵向空间：可用区是 y=95..602（602 = H-45，下面是输入框）。内容画太长会掉到
    602 以下被过滤掉，测试就会误判成「没有新消息」。
    """
    img = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(img)
    f = font(16)
    y = stamp(d, TOP, "22:18")
    y = bubble(d, y, "我在测试，你回我一句", "in", f)
    y = bubble(d, y, "顺便看看图", "in", f)
    y = bubble(d, y, "收到", "out", f)
    if ending == "text":
        y = bubble(d, y, "行，那我等你消息", "in", f)
    elif ending == "photo":
        y = photo_at(img, y, seed=29)
    else:
        y = bubble(d, y, "自言自语一句", "out", f)
    return img


def build_tooltip():
    """界面 tooltip 不能被当成对方的新消息。

    实测踩过：光标停在输入框工具栏上弹出「按住鼠标语音输入文字」，它是浮在最上层的
    一小块深色文字，OCR 会把它读成聊天区里的 incoming 消息 —— 桥于是拿这句界面文字
    去问 agent 并白回一条。这里合成同样的东西：尾巴本来是自己发的，加一块 tooltip。
    """
    img = build("out")
    d = ImageDraw.Draw(img)
    d.rectangle([110, 555, 386, 590], fill=(45, 45, 45))
    d.text((122, 562), "按住鼠标语音输入文字", font=font(15), fill=(255, 255, 255))
    return img


def build_caption_meme():
    """带字的表情包：**图上的字不能被当成聊天消息**。

    实测踩过：桥发出去的一张表情包上写着「好的，现在 / 我是 / 大肥鱼了」，OCR 把这三行
    读成了三条消息，其中「我是」因为不够靠右被判成对方发的 —— 尾巴于是被污染。
    """
    img = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(img)
    f = font(16)
    y = stamp(d, TOP, "22:19")
    y = bubble(d, y, "给你看个图", "in", f)
    y = caption_photo(img, y)
    return img


def run(bridge, sent, img, label, expect):
    bridge.capture = lambda: img
    sent.clear()
    bridge.poll_chat({"hwnd": 1, "name": "Alice", "kind": "direct"}, dry=False, kind="direct")
    ok = (len(sent) == expect)
    print("\n=== %s ===\n  期望回 %d 次，实际 %d 次  %s" % (label, expect, len(sent), "✓" if ok else "✗"))
    for s in sent:
        print("  待回文本:\n    %s" % s["text"].replace("\n", "\n    "))
    return ok


def main():
    cfg = json.load(open(os.path.join(HERE, r"wxbridge-config.json"), encoding="utf-8"))
    # 用临时状态文件：测试的回信记录绝不写进真桥的去重表（否则真桥会漏回用户消息）
    cfg["statePath"] = os.path.join(HERE, r"test-state.json")
    try:
        os.remove(cfg["statePath"])
    except OSError:
        pass
    bridge = B.Bridge(cfg)
    bridge.vision_for = lambda hwnd: bridge.v
    sent = []

    def fake_reply(name, text, dry=True, sig=None, kind="direct"):
        """替身 reply_to：只记录不真发。

        必须照抄真 reply_to 的记账动作（`st["replied"]`，保留 5 条 / 600 秒窗口），
        否则「同一段尾巴不重复回」的去重永远测不出来。
        """
        sent.append({"name": name, "text": text, "kind": kind, "sig": sig})
        st = bridge.state.setdefault(name, {})
        now = time.time()
        rep = [list(p) for p in st.get("replied", []) if now - p[1] < 600]
        rep.append([sig, now])
        st["replied"] = rep[-5:]

    bridge.reply_to = fake_reply

    img_text = build("text")
    img_text.save(os.path.join(HERE, r"synth-chat-text.png"))
    img_photo = build("photo")
    img_photo.save(os.path.join(HERE, r"synth-chat-photo.png"))
    img_out = build("out")
    img_meme = build_caption_meme()
    img_meme.save(os.path.join(HERE, r"synth-chat-meme.png"))
    img_tip = build_tooltip()
    img_tip.save(os.path.join(HERE, r"synth-chat-tooltip.png"))

    results = [
        run(bridge, sent, img_text, "最新一条是对方文字", 1),
        run(bridge, sent, img_text, "同一张图再来一遍（去重）", 0),
        run(bridge, sent, img_photo, "最新一条是对方发的图片", 1),
        run(bridge, sent, img_out, "最新一条是自己发的", 0),
        run(bridge, sent, img_tip, "聊天区里有界面 tooltip（不该当消息）", 0),
        run(bridge, sent, img_meme, "带字的表情包（图上的字不该当消息）", 1),
    ]
    # 表情包那条：待回文本里应该有 [图片]，并且**不能**混进图上的字
    txt = sent[0]["text"] if sent else ""
    leaked = [w for w in ("好的", "现在", "大肥鱼", "我是") if w in txt]
    ok_clean = ("[图片]" in txt) and not leaked
    results.append(ok_clean)
    print("\n=== 表情包上的字没漏进来 ===\n  %s（漏进来的词: %s）"
          % ("✓" if ok_clean else "✗", leaked or "无"))
    print("\n结果: %d/%d 通过" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
