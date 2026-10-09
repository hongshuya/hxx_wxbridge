# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
# -*- coding: utf-8 -*-
"""微信小号 ←→ DSH agent 桥。

架构：
  小号微信窗口（Qt Quick，无 UIA 树）
    → PrintWindow 截图 + RapidOCR 读会话列表/消息
    → PostMessage 键盘/鼠标后台操作（点行、上下键、回车、输入、发送）
    → 消息交给隔离的 DSH headless profile（F:\\node\\dsh-wechat-home）
    → 回复文本打进输入框并回车发回该会话

运行模式：
  python wxbridge.py --list             只列会话（不点、不发）
  python wxbridge.py --calibrate        向「文件传输助手」发一条自测消息，标定气泡左右对齐
  python wxbridge.py --dry              轮询并打印检测结果，绝不发送
  python wxbridge.py                    正式跑（只回白名单联系人；需要 config 文件）

配置：wxbridge-config.json
  { "whitelist": ["联系人昵称A", "联系人昵称B"], "pollSeconds": 3, "rateLimitPerMinute": 8 }
"""
import ctypes
import hashlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
from contextlib import contextmanager

if sys.stdout is None:
    # pythonw.exe（无控制台）直接双击启动时 stdout 是 None：自己接到日志文件，
    # 否则下面 sys.stdout.buffer 会 AttributeError 直接崩掉。
    sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wxbridge.log"),
                      "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = sys.stdout
elif not getattr(sys.stdout, "_dsh_utf8", False):
    # 只包一次：同一个 buffer 被包两次时，先被回收的 wrapper 会关掉 buffer，
    # 后续 print 报 "ValueError: I/O operation on closed file"。
    _out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                            line_buffering=True, write_through=True)
    _out._dsh_utf8 = True
    sys.stdout = _out

sys.path.insert(0, HERE)

import wxvision as W  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "wxbridge-config.json")
STATE_PATH = os.path.join(HERE, "wxbridge-state.json")
PID_PATH = os.path.join(HERE, "wxbridge.pid")

DSH_HOME = os.environ.get("WXBRIDGE_DSH_HOME", os.path.join(HERE, "dsh-home"))
DSH_BIN = os.path.join(HERE, r"dsh-asar\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js")
NODE = shutil.which("node") or "node"
PROFILE = "wechat"
# 微信侧 agent 的专用临时目录（必须在它自己的工作目录之外，见 DshAgent.ask）
AGENT_TMP = os.path.join(os.environ.get("TEMP", HERE), "wxbridge-agent")

VK_DOWN = 0x28
VK_UP = 0x26
VK_RETURN = 0x0D
VK_A = 0x41
VK_V = 0x56
VK_DELETE = 0x2E

# 多窗口模式下每隔多久也扫一遍会话列表（找「窗口没开」的群聊未读）
LIST_SCAN_SECONDS = 30.0
# 界面自带文字（按钮 / 悬浮提示），OCR 读到了也不算「消息」。
# 踩坑记录：鼠标停在输入框工具栏上会弹出「按住鼠标语音输入文字」这个 tooltip，
# 曾被当成对方的新消息，桥就拿着它去问 agent 并回了一条。
UI_LABELS = {
    "发送", "发送(S)", "发送(s)", "按住鼠标语音输入文字", "按住说话", "松开发送",
    "表情", "文件", "截图", "聊天记录", "语音输入", "语音消息", "取消", "更多",
    "微信", "通讯录", "收藏", "朋友圈", "小程序", "视频号", "看一看", "搜一搜",
}

# ---------- 表情包库（和桌面端 send_meme 用同一批图）----------
def _first_dir(*paths):
    """挑第一个真实存在的目录（表情库位置会随 DSH_HOME 变，写死路径已经翻过一次车）。"""
    for p in paths:
        if p and os.path.isdir(p):
            return p
    return paths[0]


MEME_PACKS = _first_dir(
    os.environ.get("DSH_MEME_PACKS") or "",
    os.path.expanduser("~/.dsh/meme-packs"),
    os.path.join(DSH_HOME, "meme-packs"),
    os.path.expanduser("~/.dsh/meme-packs"),
)
# 描述里出现这些词就落到对应情绪目录（数据库没有精确命中时的兜底）
EMOTION_HINTS = [
    ("无语 无语了 汗 尴尬 郁闷 懵 迷惑 问号 不懂 疑惑", "confused"),
    ("气 生气 怒 火大 烦躁 烦 不爽 凶 打人 拍桌 催", "angry"),
    ("难过 哭 委屈 伤心 呜 惨 沮丧 叹气", "sad"),
    ("羞 害羞 脸红 害臊 不好意思 娇羞", "shy"),
    ("开心 高兴 笑 赞 棒 好耶 喜欢 爱 贴贴 摸头 抱", "happy"),
    ("睡 困 上班 摸鱼 摆烂 日常 干活 加班 吃 饭", "daily"),
]
MEME_MARKER = re.compile(r"[\[【]\s*(?:表情|meme|图)\s*[:：]\s*([^\]】]+?)\s*[\]】]", re.I)
# 截图像素没变就不重复 OCR；但每隔这么久强制重认一次（兜底）
FRAME_RECHECK_SECONDS = 20.0


def safe_name(name):
    """把会话名变成文件名安全的形式。"""
    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(name))[:40] or "win"


def arr_mean(img):
    """图像平均亮度（用 PIL 转 numpy）。"""
    import numpy as np
    return float(np.asarray(img.convert("L"), dtype="float32").mean())


def meme_pick(query=None, tag=None):
    """在表情包库里找一张图 → 绝对路径。

    先按 keywords/caption 做中文子串打分，没命中就用 tag（或从描述猜情绪）随机取一张。
    """
    import sqlite3
    q = (query or "").strip()
    toks = [t for t in re.split(r"[\s,，、。！!？?~～]+", q) if t]
    best, best_score = [], -1
    for pack in sorted(os.listdir(MEME_PACKS)):
        db = os.path.join(MEME_PACKS, pack, "index.db")
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(db)
            rows = list(con.execute("SELECT path, tag, caption, keywords FROM memes"))
            con.close()
        except Exception:  # noqa: BLE001
            continue
        for path, rtag, caption, keywords in rows:
            full = os.path.join(MEME_PACKS, pack, path)
            if not os.path.isfile(full):
                continue
            if tag and rtag != tag:
                continue
            score = 0
            hay = "%s %s" % (caption or "", keywords or "")
            if q and q in hay:
                score += 6
            for t in toks:
                if len(t) >= 2 and t in hay:
                    score += 3
                elif len(t) >= 2 and any(t[i:i + 2] in hay for i in range(len(t) - 1)):
                    score += 1
            if not q and not tag:
                score = 0
            if score > best_score:
                best, best_score = [full], score
            elif score == best_score:
                best.append(full)
    if best and best_score > 0:
        return random.choice(best)
    # 兜底：按 tag / 描述猜的情绪随机取；猜不出就先给鲸鱼娘图库的 daily（人格合适），
    # 再不行才全库随机 —— 全库随机偶尔会抽到完全不搭的梗图。
    if not tag and q:
        for words, t in EMOTION_HINTS:
            if any(w in q for w in words.split()):
                tag = t
                break
    def _pool(tag_filter=None, pack_filter=None):
        out = []
        for pack in sorted(os.listdir(MEME_PACKS)):
            if pack_filter and pack != pack_filter:
                continue
            root = os.path.join(MEME_PACKS, pack, "memes")
            if not os.path.isdir(root):
                continue
            for t in os.listdir(root):
                if tag_filter and t != tag_filter:
                    continue
                d = os.path.join(root, t)
                if os.path.isdir(d):
                    out += [os.path.join(d, f) for f in os.listdir(d)
                            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))]
        return out
    pool = _pool(tag) if tag else []
    if not pool:
        pool = _pool("daily", "meme-pack")
    if not pool:
        pool = _pool()
    return random.choice(pool) if pool else None

# 右侧消息面板在「帧」里的起点（必须与下面的 LIST_X1_RATIO 保持一致 = 0.35）。
# 早先写 395/894 是按客户区标定的，乘到 1102 宽的帧上变成 486，而右栏所有文字块的 x0 落在
# 397–478（标题 397 / 气泡 397–478）→ 被整块切掉，OCR 读不到任何群消息（只剩 18:34 与「发送」）。
PANE_X_RATIO = 0.35
TITLE_Y_RATIO = 95 / 647.0
# 底部输入区（发送按钮那一条）占窗口高度比例：读消息时要把它排除掉
BOTTOM_Y_RATIO = 45 / 647.0
# 抢前台发消息之前，先等用户手离开键盘这么久（秒）——否则他正在敲的字会混进我们的回复。
USER_IDLE_BEFORE_SEND = 2.5
# 最多等他这么久；超了就这一轮不发，下轮再来（消息不会丢，尾巴指纹还在）。
USER_IDLE_MAX_WAIT = 25.0
# 会话列表：标题行下沿 / 行高（同样按截图高度比例缩放，WGC 帧比 PrintWindow 大 25%）
# 会话列表区的上沿：搜索框在列表上方，它的占位文字「搜索」不能被当成一行。
# 实测两种帧（1102x801 / 1102x904）里搜索框都在 y≈59~81、第一行名字在 y≈116 以下，
# 所以按帧高的 10.5% 切一刀；早先按 90/647 缩放，904 高的帧切到 126，把第一行名字切掉了。
LIST_TOP_RATIO = 0.105
ROW_H_RATIO = 87 / 647.0
FIRST_ROW_RATIO = 130 / 647.0
# 会话列表每行右侧的时间戳（`01:15` / `昨天22:34` / `星期一 9:05`）：不是名字，读行时要剔掉
TIME_RE = re.compile(r"^(昨天|前天|星期[一二三四五六日]|上午|下午|中午)?\s*\d{1,2}:\d{2}$")
# 未读红点里的纯数字（`82`）会被 OCR 当成一个文本块，且它的 y 常常比行名高几个像素，
# 不剔掉就会顶替掉真正的名字（实测 `Alice` 那一行读成 `82`）。
BADGE_RE = re.compile(r"^\d{1,3}$")
# 会话列表与右侧聊天区的分界线（帧里的比例）：实测 887x730 窗口的帧是 0.354w，
# 写死 0.45w 会把聊天区里的气泡文字读进「预览」，行内容永远在变。
LIST_X1_RATIO = 0.35
# 图片消息文本形如 `[图片] F:\...\img-xxxx.png`：做尾巴指纹时要把路径抹掉（路径每轮都变）
IMG_PATH_RE = re.compile(r"\[图片\]\s*\S+")
# 「N 条新消息」折叠横幅（`（共4条）>`）：不是消息，而且箭头与条数每帧都在抖
SUMMARY_RE = re.compile(r"共\s*\d+\s*条")
# 指纹归一：OCR 每轮都会有半角/全角括号、引号、省略号之类的抖动。不归一的话「同一段消息」
# 每轮都算一条新消息 → 群里的 5% 自发率会被反复重掷（实测 2 分钟掷了 6 次 ≈ 必回）。
SIG_NOISE_RE = re.compile(r"[\s（）()［］\[\]「」『』【】“”\"'‘’!！,，。.…~～?？:：;；>＜<>]+")
# 微信会话列表自己的「有人@我」标记（预览形如 `[有人@我]乐:小了`）。这是最可靠的点名信号：
# 比按昵称匹配强得多 —— 改一次微信昵称，写死的 selfNickname 就失效了。
MENTION_FLAG_RE = re.compile(r"有人\s*@\s*我")
# 英数昵称（bob / alice / abc_1）：从被 @ 的文本里学新昵称用
ASCII_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_\-]{1,24})")


def sig_norm(text):
    """把一段消息文本归一成稳定的指纹（去掉图片路径与标点抖动）。"""
    return SIG_NOISE_RE.sub("", IMG_PATH_RE.sub("[图片]", text or ""))[:60]


def row_split_threshold(y0s, h):
    """算「行首聚类」的阈值：同行内几行字的间距，和跨行的间距之间那一跳。

    实测两套帧差别很大：894x647 那套同行 ≤52 / 跨行 ≥79，887x730 那套同行 ≤32 / 跨行 ≥50
    （帧高、缩放不同，写死一个数必有一边翻车：写 67 时 887x730 那套会把「腾讯新闻」的
    预览和下一行的名字并进同一行，名字与预览整列错位）。所以从数据里找间隔序列最大的一跳，
    取中点当阈值；数据里看不出明显跳变时退回 0.55 倍行高。
    """
    fallback = 0.55 * ROW_H_RATIO * h
    gaps = sorted(b - a for a, b in zip(y0s, y0s[1:]) if b > a)
    if len(gaps) < 3 or gaps[-1] - gaps[0] < 8:
        return fallback
    # 一维两簇 k-means：间隔自然分成「同一行内的几行字」与「跨行」，阈值取两簇边界
    lo, hi = float(gaps[0]), float(gaps[-1])
    for _ in range(20):
        mid = (lo + hi) / 2.0
        small = [g for g in gaps if g < mid]
        large = [g for g in gaps if g >= mid]
        if not small or not large:
            return fallback
        nlo, nhi = sum(small) / float(len(small)), sum(large) / float(len(large))
        if abs(nlo - lo) < 0.01 and abs(nhi - hi) < 0.01:
            lo, hi = nlo, nhi
            break
        lo, hi = nlo, nhi
    return max(30.0, min((lo + hi) / 2.0, 0.16 * h))


def list_rows_from_image(img):
    """从一张会话列表截图里切出每一行，返回 [{row, name, preview, y_name, x_name, unread}]。

    行由「名字列 OCR 的纵向聚类」得到：同一行的名字/时间/预览挨得近，跨行的间距大得多。
    把这块从 Bridge 里拆出来是为了能拿存下来的 PNG 直接跑（低完整性沙箱里截不了图，
    但 OCR 一张旧图没问题）。
    """
    w, h = img.size
    k = h / 647.0                       # 截图相对 894x647 标定的比例（WGC 帧更大）
    # 名字列从左栏头像右边开始。WGC 抓的是客户区、含最左侧图标栏（名字起点约
    # 0.11w），老的 PrintWindow 帧没有图标栏（约 0.16w）—— 起点取 0.085w 两种都覆盖。
    # 早先写死 0.16w，在 WGC 帧里正好把名字切掉，只剩右边的『时间』被读进来，
    # 于是会话列表看着只有 `01:11 / 昨天22:34` 这种行，群消息永远发现不了。
    boxes = [b for b in W.ocr_boxes(img, x0=int(w * 0.085), x1=int(w * LIST_X1_RATIO))
             if b["y0"] >= h * LIST_TOP_RATIO]
    boxes.sort(key=lambda b: b["y0"])
    pitch = row_split_threshold([b["y0"] for b in boxes], h)
    starts, cur0 = [], None
    for b in boxes:
        if cur0 is None or b["y0"] - cur0 >= pitch:
            starts.append(b["y0"])
            cur0 = b["y0"]
    px = img.convert("RGB").load()
    out = []
    for i, sy in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else 10 ** 9
        row_boxes = [b for b in boxes if sy <= b["y0"] < end]
        row_boxes.sort(key=lambda b: (b["y0"], b["x0"]))    # 行首那行在上，同一行里名字在左
        parts = [b for b in row_boxes
                 if not TIME_RE.match((b["text"] or "").strip())
                 and not BADGE_RE.match((b["text"] or "").strip())]
        if not parts:
            continue                    # 整行只有时间戳/红点（没读到名字），跳过
        name = parts[0]["text"]
        preview = " ".join(b["text"] for b in parts[1:])
        y_name = int(parts[0]["y0"])
        # 未读红点扫头像那一条（头像在名字左边约 0.075w~0.012w 处，随名字位置浮动）
        ax1, ax0 = int(parts[0]["x0"] - w * 0.012), int(parts[0]["x0"] - w * 0.075)
        unread = False
        for y in range(max(0, y_name - int(20 * k)), min(h, y_name + int(30 * k))):
            for x in range(max(0, ax0), max(1, ax1)):
                r, g, b = px[x, y]
                if r > 185 and g < 95 and b < 95:
                    unread = True
                    break
            if unread:
                break
        out.append({"row": i, "name": name, "preview": preview, "y_name": y_name,
                    "x_name": int(parts[0]["x0"]), "unread": unread})
    return out


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class DshAgent:
    """调用隔离 profile 的 headless agent，维持一条会话。

    权限（危险操作拦截的第一层，硬约束）：DSH 的 shell 沙箱由环境变量
    `DSH_PERMISSION_MODE` 控制（`dsh-base/cordis.patch.yml` 里
    `sandbox-policy.config.mode`），我们用 `workspace-write` + 一个**专用工作目录**
    （默认 `F:\\node\\dsh-wechat-home\\workspace`）把它能改的东西圈在里面：
    删除/覆盖工作目录外的文件会被系统 ACL 拦住，它没法碰用户真实的项目文件。
    """

    def __init__(self, session_id=None, timeout=300, permission=None, workdir=None):
        self.session_id = session_id
        self.timeout = timeout
        self.permission = permission or "workspace-write"
        self.workdir = workdir or os.path.join(DSH_HOME, "workspace")

    def ask(self, text):
        cmd = [NODE, DSH_BIN, "--profile", PROFILE, "--json"]
        if self.session_id:
            cmd += ["--session-id", self.session_id]
        cmd.append(text)
        env = dict(os.environ)
        env["DSH_HOME"] = DSH_HOME
        env["DSH_NO_UPDATE_CHECK"] = "1"
        env["DSH_PERMISSION_MODE"] = self.permission
        try:
            os.makedirs(self.workdir, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        # 把临时目录挪到一个**工作目录之外**的专用位置：DSH 沙箱要用 %TEMP%\dsh-acl-locks\*.lock，
        # 而系统 Temp 下那个目录是别的高完整性进程建的，沙箱里的 agent 建不了锁文件
        # （CreateFileW failed (Win32 5)），连 pwsh/grep 都起不来。
        # 注意：不能放在工作目录里面 —— 沙箱会报
        # 「Windows ACL temp root must be outside the workspace」。
        tmp = AGENT_TMP
        try:
            os.makedirs(tmp, exist_ok=True)
            env["TEMP"] = env["TMP"] = tmp
        except Exception:  # noqa: BLE001
            pass
        cwd = self.workdir if os.path.isdir(self.workdir) else None
        reply, sid, err, out = self._run(cmd, env, cwd)
        # 会话 id 与工作目录绑死：旧 id 是在别的 cwd 下建的，续用会直接报错
        # （实测 `session "..." was recorded in "<旧工作目录>", not "...<新工作目录>"`）。
        # 遇到这种情况丢掉旧 id、用当前工作目录重开一条会话再试一次。
        if sid and ("was recorded in" in (reply or "") or "was recorded in" in err):
            print("  ! 旧会话的工作目录对不上，重开一条会话（丢掉 %s）" % sid)
            self.session_id = None
            cmd2 = [c for c in cmd if c != "--session-id" and c != sid]
            reply, sid, err, out = self._run(cmd2, env, cwd)
        self.session_id = sid
        # 把出错的原文留在日志里，绝不返回给调用方去发进微信（人格里也写了这条规矩）
        if reply and reply.strip().startswith('{"type":"error"'):
            print("  ! agent 报错（不发给微信）: %s" % reply.strip()[:300])
            return "", sid, err
        return (reply or "").strip(), sid, err

    def _run(self, cmd, env, cwd):
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, cwd=cwd, timeout=self.timeout,
        )
        reply, sid = None, self.session_id
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "session" and evt.get("sessionId"):
                sid = evt["sessionId"]
            elif evt.get("type") == "final":
                reply = evt.get("text")
        if reply is None:
            reply = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        return reply, sid, (proc.stderr or "").strip(), proc.stdout or ""


class Bridge:
    def __init__(self, config):
        self.cfg = config
        # 状态文件路径可被 config 覆盖：测试用临时文件，免得把测试的回信记录写进真桥的
        # 去重表（那会让真桥误以为「这条已经回过了」而漏回用户消息）。
        self.state_path = config.get("statePath") or STATE_PATH
        self.state = load_json(self.state_path, {})
        self.v = None
        self.pid = None
        self._resolve()                      # 找小号 pid + 一个可用的窗口
        self.sessions = {k: v.get("session_id") for k, v in self.state.items()}
        self._last_preview = {}
        self._last_send = 0.0
        self._cap_logged = None
        self._visions = {}
        self._frame_fp = {}          # 每窗口的「上一帧像素指纹」，用来跳过没变化的 OCR
        self._known_groups = set()   # 会话列表里认出来的群名（窗口标题可能不带成员数）
        self._primed = set()         # 本次运行里「刚接手、只登记不回」过的群
        self._list_mentions = {}     # 群名 → 最近一次在会话列表里看到「有人@我」的时间

    def _resolve(self):
        """找小号进程与窗口。

        pid 用 wxvision.find_small_pid(白名单标题) 反推——**不要用 WMI 命令行**：
        本机 WMI 进程枚举时灵时不灵（实测只返回十几个进程、看不到 Weixin.exe）。
        """
        wl = list(self.cfg.get("whitelist", [])) + list(self.cfg.get("groups", []))
        pid = W.find_small_pid(wl)
        wins = W.list_windows(pid)
        if not wins:
            raise RuntimeError("小号 pid=%s 没有可用窗口" % pid)
        target = None
        for w in wins:                        # 优先白名单聊天窗（chat 模式）
            if w["title"] in wl:
                target = w
                break
        if target is None:                    # 否则用小号的主窗口（list 模式）
            for w in wins:
                if w["title"] == "微信":
                    target = w
                    break
        if target is None:
            target = wins[0]
        self.pid, self.v = pid, W.WxVision(pid=pid, hwnd=target["hwnd"], backend=self.capture_backend())


    # ---------- 窗口渲染 ----------
    def capture_backend(self):
        """抓图后端：配置 capture=auto|wgc|printwindow（auto = 先 WGC 再 PrintWindow）。"""
        return str(self.cfg.get("capture", "auto")).lower()

    def capture(self):
        """带后端日志的截图；全黑时可选「临时提层」再抓一次。

        全黑的成因之一是被别的窗口完全遮住（系统就不再给它重绘）——WGC 抓图不受遮挡影响，
        但 WGC 起不来时只剩 PrintWindow。这时把窗口**提层**（不抢焦点）往往能让它重新渲染，
        抓完不动它：config 的 autoRaise=true 才启用（会短暂盖住别的窗口，默认关）。
        """
        img = self.v.capture()
        if W.is_blank(img) and bool(self.cfg.get("autoRaise", False)):
            try:
                self.v.raise_window()
                time.sleep(0.6)
                img2 = self.v.capture()
                if not W.is_blank(img2):
                    if not getattr(self, "_raised_logged", False):
                        self._raised_logged = True
                        print("[cap] 窗口被遮挡 → 提层后读到了内容（autoRaise=true）")
                    img = img2
            except Exception as exc:  # noqa: BLE001
                print("! 提层失败: %s %s" % (type(exc).__name__, exc))
        b = getattr(self.v, "last_backend", None)
        if b and self._cap_logged != b:
            self._cap_logged = b
            print("[cap] 抓图后端 = %s" % b)
        return img

    def pane_rect(self):
        img = self.capture()
        w, h = img.size
        return img, int(w * PANE_X_RATIO), int(h * TITLE_Y_RATIO), w, h

    def ensure_rendered(self):
        """Qt Quick 的聊天面板在窗口未激活时可能整块不渲染：必要时激活一次再截。

        只有在这张图**整块空白**（真的没渲染）时才激活 —— 用户反馈过被抢前台很烦，
        所以不能因为「标题区没 OCR 到字」就激活（那多半是裁剪/OCR 的问题，不是没渲染）。
        """
        img, px, ty, w, h = self.pane_rect()
        if W.ocr_boxes(img, x0=px, x1=w, y0=0, y1=ty):
            return img, px, ty, w, h
        if not W.is_blank(img):
            return img, px, ty, w, h
        prev = self.v.activate()
        time.sleep(1.2)
        img, px, ty, w, h = self.pane_rect()
        if prev:
            self.v.restore_foreground(prev)
        return img, px, ty, w, h

    # ---------- 列表 ----------
    @staticmethod
    def list_bands(img):
        """用行分隔线（浅灰横线）切出会话行区间。返回 [(y_top, y_bottom)]。"""
        px = img.convert("RGB").load()
        w, h = img.size
        xa, xb = int(w * 0.17), int(w * 0.40)
        seps = []
        for y in range(int(h * 0.13), h - 2):
            grey = 0
            for x in range(xa, xb):
                r, g, b = px[x, y]
                if max(r, g, b) - min(r, g, b) < 14 and 200 <= (r + g + b) / 3 <= 249:
                    grey += 1
            if grey >= (xb - xa) * 0.75:
                seps.append(y)
        # 合并相邻分隔线
        merged = []
        for y in seps:
            if merged and y - merged[-1][-1] <= 2:
                merged[-1].append(y)
            else:
                merged.append([y])
        seps = [g[0] for g in merged]
        bands = []
        if not seps:
            return bands
        top = seps[0]
        for nxt in seps[1:]:
            if nxt - top >= 46:
                bands.append((top, nxt))
            top = nxt
        if h - top >= 46:
            bands.append((top, h))
        return bands

    def main_vision(self):
        """带「会话列表」的那个主窗口。

        独立聊天窗（标题是联系人名）**没有会话列表**：对它跑 rows() 会把聊天区
        的文字读成「行」（实测读到 `[0] 名字 "在的在的，一直在" 预览 "刚刚是在群里…"`），
        于是 poll_list 认出的「会话」全是消息正文，点开会话更是点错地方。
        """
        try:
            wins = W.list_windows(self.pid)
        except Exception:  # noqa: BLE001
            wins = []
        pick = None
        for x in wins:
            if (x["title"] or "").strip() in ("微信", "Weixin"):
                pick = x
                break
        if pick is None and wins:
            pick = max(wins, key=lambda x: x["size"][0] * x["size"][1])
        if pick is None:
            return self.v
        if getattr(self, "_vlist_hwnd", None) != pick["hwnd"]:
            self._vlist = W.WxVision(pid=self.pid, hwnd=pick["hwnd"], backend=self.capture_backend())
            self._vlist_hwnd = pick["hwnd"]
        return self._vlist

    def rows(self, v=None):
        """返回 (行列表, 截图)。切行逻辑在模块级 list_rows_from_image()（好拿旧 PNG 复现）。"""
        v = v or self.v
        img = v.capture()
        return list_rows_from_image(img), img

    def open_row(self, index, v=None, xy=None, real=False, double=True):
        """点开会话列表里的第 index 行。

        `xy`：该行在**截图坐标**里的位置（由 rows() 量出来）。有它就直接点那一行，
        没有就退回老的「点第一行 + 方向键下移」办法。

        默认 `real=False`（PostMessage 点击）：posted 点击不动用户的鼠标、不抢前台，
        实测已经足够让右栏切换会话（clickscan 标定：posted 点中行 → 右栏出 13 个正文块）。
        `real=True` 才用真键鼠（SetCursorPos + mouse_event），只在 posted 点不开时兜底 ——
        它会把窗口顶到前台，用户明确说过这样很影响他正常使用。
        """
        v = v or self.v
        if xy:
            cw, ch = v.client_size
            fw, fh = getattr(v, "last_frame_size", None) or (cw, ch)
            cx = int(round(int(xy[0]) * cw / float(fw)))
            cy = int(round(int(xy[1]) * ch / float(fh)))
            if real and double:
                v.real_double_click_client(cx, cy)
            elif real:
                v.real_click_client(cx, cy)
            else:
                v.click_client(cx, cy, double=double)
            time.sleep(2.2)
            return
        # 点第 0 行用客户区比例（标定值 250/130 出自 894x647 主窗口）
        cw, ch = v.client_size
        v.click_client(int(cw * (250 / 894.0)), int(ch * (130 / 647.0)))
        time.sleep(0.6)
        for _ in range(index):
            v.key(VK_DOWN)
            time.sleep(0.3)
        v.key(VK_RETURN)
        time.sleep(2.2)

    def open_row_in_pane(self, r, mv=None, tries=3):
        """把会话行在主窗口右栏打开，返回右栏截图（失败也返回最后一张）。

        先走 posted 点击（不动鼠标、不抢前台）；`tries` 次都没让右栏出现聊天内容，
        才用一次「激活 + 真双击」兜底（会短暂抢前台，日志里会写清楚）。
        判据：OCR 右栏标题区，有文字就说明右栏真的显示出聊天了。
        """
        mv = mv or self.main_vision()
        if mv is None:
            return None
        xy = (r["x_name"] + 40, r["y_name"] + 30)
        cw, ch = mv.client_size
        fw, fh = getattr(mv, "last_frame_size", None) or (cw, ch)
        cx = int(round(int(xy[0]) * cw / float(fw)))
        cy = int(round(int(xy[1]) * ch / float(fh)))

        def shown(img):
            w, h = img.size
            return bool(W.ocr_boxes(img, x0=int(w * PANE_X_RATIO), x1=w, y0=0,
                                    y1=int(h * TITLE_Y_RATIO)))

        img = None
        for _ in range(max(1, tries)):
            mv.click_client(cx, cy)              # posted 单点：选中该行
            time.sleep(1.8)
            img = mv.capture()
            if shown(img):
                return img
        print("  · posted 点了 %d 次右栏都没内容，兜底用一次真双击（会短暂抢前台）" % tries)
        with self.activated():
            mv.real_double_click_client(cx, cy)
            time.sleep(1.8)
            img = mv.capture()
        return img

    def current_title(self):
        img, px, ty, w, _h = self.ensure_rendered()
        boxes = W.ocr_boxes(img, x0=px, x1=w, y0=0, y1=ty)
        return " ".join(b["text"] for b in boxes).strip(), img, px, ty, w

    def messages(self, img, px, ty, w, img_regions=None):
        """右侧消息区 OCR：返回 [{text, y, x0, x1, side}]，side 由横向位置粗判。

        `img_regions`：图片气泡的区域。**图片里的文字要整块跳过** —— 表情包上写着字，
        OCR 会把它当成一条条消息读进来（实测把表情包上的「好的，现在 / 我是 / 大肥鱼了」
        读成了三条消息，其中「我是」因为不够靠右还被判成对方发的）。
        """
        out = []
        h = img.size[1]
        regions = img_regions or []
        for b in W.ocr_boxes(img, x0=px, x1=w, y0=ty):
            t = b["text"].strip()
            if not t:
                continue
            if any(r[0] - 4 <= b["cx"] <= r[2] + 4 and r[1] - 4 <= b["cy"] <= r[3] + 4
                   for r in regions):
                continue
            # 界面文字不是消息：底部输入区的「发送」，以及**鼠标悬停弹出的 tooltip**
            # （实测踩过：光标停在输入框工具栏上时弹出「按住鼠标语音输入文字」，被 OCR
            # 当成对方发来的新消息 → 桥拿它去问 agent → 白回一条）。
            if b["y0"] > h * 0.82 and t.replace(" ", "") in UI_LABELS:
                continue
            if t.replace(" ", "") in UI_LABELS:
                continue
            if SUMMARY_RE.search(t):
                continue
            # 时间戳：形如 22:34 / 昨天 / 上午 10:01，且大致居中
            if len(t) <= 6 and (t.count(":") == 1 or t in ("昨天", "前天", "今天")):
                side = "time"
            elif t.startswith("撤回") or "撤回了一条消息" in t:
                side = "sys"
            elif b["cx"] > px + (w - px) * 0.45:
                side = "out"       # 靠右 = 自己发的
            else:
                side = "in" if b["x0"] < px + (w - px) * 0.35 else "in"
            out.append({"text": t, "y": int(b["y0"]), "x0": int(b["x0"]),
                        "x1": int(b["x1"]), "cx": int(b["cx"]), "side": side})
        return out

    def grouped(self, msgs, w):
        """把逐行 OCR 合并成一条条消息。

        气泡会换行：同一气泡的行间距 ≤32px；自己发的靠右（x1 接近窗口右边），
        别人发的靠左（x1 只有 y 轴中线附近）。按**组内最大 x1** 判左右，
        避免把「自己气泡的最后一行」误判成别人发来的（那会导致自己回自己）。
        """
        groups = []
        for m in msgs:
            if m["side"] in ("time", "sys"):
                groups.append({"kind": "time", "text": m["text"], "y": m["y"], "side": "time"})
                continue
            if groups and groups[-1].get("kind") != "time" and \
                    m["y"] - groups[-1]["y1"] <= 32:
                g = groups[-1]
                g["parts"].append(m["text"])
                g["y1"] = m["y"]
                g["x0"] = min(g["x0"], m["x0"])
                g["x1"] = max(g["x1"], m["x1"])
            else:
                groups.append({"kind": "msg", "parts": [m["text"]], "y": m["y"], "y1": m["y"],
                               "x0": m["x0"], "x1": m["x1"]})
        for g in groups:
            if g.get("kind") == "msg":
                g["text"] = " ".join(g["parts"])
                # 自己发的气泡**右对齐**：即使内容很短，右边也贴着窗口右缘（实测 x1≈604-612 / w=613）；
                # 别人发的左边起（x0≈109-115）且 x1 最多到 0.7w。所以用「x1 是否贴右」判左右，
                # 用 x0 判会在别人发长消息时判错（实测 x1=428 > 0.6w 被误判成自己发的）。
                g["side"] = "out" if g["x1"] > w * 0.80 else "in"
        return [g for g in groups if g.get("text")]

    def last_incoming(self, msgs):
        incoming = [m for m in msgs if m["side"] == "in"]
        return incoming[-1] if incoming else None

    # ---------- 发送 ----------
    def send_text(self, text):
        """发一条文字。优先「无焦点」路线：PostMessage 点输入框 → 打字 → 回车。

        为什么先试无焦点：真键鼠路线必须把窗口提到前台，会抢用户键盘、还会把小号窗口
        顶到最上层（用户反馈「每次回消息都把窗口拉到最上面，很影响使用」）。无焦点路线
        把鼠标/键盘都当消息发给窗口，不动用户的鼠标、不抢前台；发完截图验证有没有真发出去。
        """
        before_n = self._msg_count()
        self._send_focusfree(text=text)
        if self._verify_sent(text, tries=4):
            return True
        # 自检没认出来时**先补一个回车**，别急着重打：字多半已经打进输入框、只是没提交
        # （或截图那一刻还没渲染出来）。补回车最多是空回车（微信不会发空消息），比整条重打
        # 安全得多 —— 实测重打就是「同一条消息发两遍」的来源。
        where = self._needle_where(text)
        if where == "input":
            self.v.key(VK_RETURN)
            if self._verify_sent(text, tries=4):
                return True
            return True                     # 提交过了：字不在输入框里了，别再重打
        if where == "chat":
            return True
        # 聊天区里既没看到这句话、字也不在输入框 —— 大概率是白打了（截图那一刻的新气泡
        # 可能还没渲染，所以这里也可能只是「没看见」）。用一个便宜的旁证再确认一次：
        # 聊天区里的消息条数有没有变多。变多了就当已经发出去，避免重复发送。
        if self._msg_count() > before_n:
            print("  ! 自检没找到这句话，但聊天区多了一条消息 —— 按已发送处理，不重发")
            return True
        # 中文有时 WM_CHAR 不被 Qt 接受 → 换剪贴板 + WM_PASTE 再试一次（仍然不抢前台）
        self._send_focusfree(text=text, use_paste=True)
        if self._verify_sent(text, tries=4):
            return True
        print("  ! 无焦点发送没被确认，最后一次机会：真键鼠（只在窗口已经在前台时才用）")
        # 关键保险在 _send_guarded 里：窗口不是前台窗口时绝不敲键盘 —— 实测否则内容会打进
        # 用户正在用的别的程序（浏览器）里。而且**不再**自动把窗口提到最上层：用户两次投诉
        # 「又把窗口拉到上层了」，宁可这条不发，也不打断他。
        return self._send_guarded(lambda: W.set_clipboard(text), settle=0.5)

    def _activate_ok(self, tries=2):
        """确认窗口**已经是**前台窗口；不是就返回 False。

        故意**不**调用 `activate()`：那是把窗口提到最上层，会打断用户（实测两次投诉
        「又拉到上层了」）。真键鼠路线只在窗口本来就处于前台（用户正看着它）时才走，
        不需要也不应该改 Z 序。
        """
        import win32gui
        for i in range(tries):
            if win32gui.GetForegroundWindow() == self.v.hwnd:
                return True
            time.sleep(0.4)
        return False

    def _send_focusfree(self, text=None, image=None, settle=0.6, use_paste=False):
        """不抢前台、不动鼠标：点输入框（三连击全选 + Delete）→ 打字 → 回车。

        全程 PostMessage：`click_client` 是 posted 点击（不动光标、不激活窗口），
        `type_text` 是逐字符 WM_CHAR，`key(VK_RETURN)` 是 posted 回车。实测微信 4.1.15
        的输入框收这些消息（早先标定时 `type_text('测试abc')` + `enter()` 就是这么发出去的）。
        中文偶尔不被 WM_CHAR 接受时，退回剪贴板 + WM_PASTE（`use_paste=True`），仍然不抢前台。
        """
        rate = float(self.cfg.get("rateLimitPerMinute", 8))
        elapsed = time.time() - self._last_send
        if elapsed < 60.0 / rate:
            time.sleep(60.0 / rate - elapsed)
        cw, ch = self.v.client_size
        px, py = int(cw * 0.58), int(ch * 0.88)      # 输入框正中（实测标定，客户区坐标）
        self.v.remember_cursor()
        # 关键一步：先把「焦点」给窗口（不抢前台、不动 Z 序）。Qt 只处理有焦点时的键盘
        # 消息，少了这句 posted 打字全丢，就会退回真键鼠把窗口顶到最上面。
        self.v.focus_only()
        time.sleep(0.15)
        self.v.click_client(px, py, double=True)     # 三连击 = 选中输入框里的内容
        self.v.click_client(px, py)
        time.sleep(0.25)
        self.v.key(VK_DELETE)
        time.sleep(0.2)
        if image is not None:
            # 图片只能走剪贴板 + 粘贴。注意：不能再用 `key(VK_V, ctrl=True)` —— 实测 Qt
            # 不认这种 posted 的 Ctrl+V，只把 `v` 当普通字符打进去（聊天里出现一串孤零零的
            # "v"、图却没了），所以改发一条 WM_PASTE。
            W.set_clipboard_image(image)
            time.sleep(0.35)
            self.v.post_paste()
            time.sleep(0.35)
            self.v.post_paste()          # 有些控件第一次 WM_PASTE 只用来「取得焦点」
        elif use_paste:
            W.set_clipboard(text or "")
            self.v.post_paste()
        else:
            self.v.type_text(text or "")             # 逐字符 WM_CHAR
        time.sleep(settle)
        self.v.key(VK_RETURN)
        time.sleep(0.9)
        self.v.park_hover()                          # 别让 tooltip 留在屏幕上当「消息」
        self._last_send = time.time()

    def _verify_sent(self, text, tries=3):
        """截图确认「这条真的发出去了」：尾巴里能找到我发的那句话的开头。

        比的是 `sig_norm` 归一化后的文本（抹掉标点/空格）+ 前 10 个字符 —— OCR 读中文
        长句必然有错字，直接拿原文做子串匹配会假阴性（实测：消息明明发出去了，检查没过，
        于是又用真键鼠重发一遍 → 同一条消息发两次）。归一化 + 只取开头，容错大得多。
        """
        needle = sig_norm(text)[:10]
        if not needle:
            return True
        last = ""
        for i in range(tries):
            time.sleep(1.0 + i * 0.8)
            img = self.capture()
            if W.is_blank(img):
                return False
            w, h = img.size
            ty = int(h * TITLE_Y_RATIO)
            # 自检的下边界放宽到 0.97h：刚发出的那条气泡常常被输入框压掉一半，
            # 用 poll 用的 1-BOTTOM_Y_RATIO 会把它整条切掉 → 假阴性 → 又发一遍。
            ymax = int(h * 0.97)
            try:
                regs = [d["box"] for d in W.find_images(img, 0, ty, w, ymax)]
            except Exception:  # noqa: BLE001
                regs = []
            groups = self.grouped([m for m in self.messages(img, 0, ty, w, img_regions=regs)
                                   if m["y"] < ymax], w)
            tail = groups[-4:] if len(groups) > 4 else groups
            last = " | ".join(sig_norm(g.get("text") or "")[:24] for g in tail)
            for g in groups[-4:]:
                if needle in sig_norm(g.get("text") or ""):
                    return True
        # 没认出来时把「截图里最后几条归一化文本」打进日志（只进日志，不进微信），
        # 这样下次假阴性一眼就能看出是 OCR 读歪了还是真没发出去。
        print("  ! 发送自检没认出来，需要找 %r；截图里最后几条是: %s" % (needle, last))
        return False

    def _needle_where(self, text):
        """这句话现在在哪儿？返回 'chat'（聊天区，＝发出去了）/ 'input'（还躺在输入框里，
        ＝没提交）/ None（两处都没有）。

        分界就是 poll 用的同一条底线：y > (1-BOTTOM_Y_RATIO)*h 的那条带是输入框。
        这个判断比「聊天区里能不能找到」稳得多 —— 刚发出的气泡有时还没渲染，但输入框里
        「有没有残留字」几乎总是准的，靠它能做到「不重打、不重复发」。
        """
        needle = sig_norm(text)[:10]
        if not needle:
            return None
        try:
            img = self.capture()
            if W.is_blank(img):
                return None
            w, h = img.size
            ty = int(h * TITLE_Y_RATIO)
            ymax = int(h * 0.97)
            try:
                regs = [d["box"] for d in W.find_images(img, 0, ty, w, ymax)]
            except Exception:  # noqa: BLE001
                regs = []
            groups = self.grouped([m for m in self.messages(img, 0, ty, w, img_regions=regs)
                                   if m["y"] < ymax], w)
        except Exception:  # noqa: BLE001
            return None
        y_input = h * (1 - BOTTOM_Y_RATIO)
        for g in groups:
            if needle in sig_norm(g.get("text") or ""):
                return "input" if g["y"] > y_input else "chat"
        return None

    def _msg_count(self):
        """聊天区里现在有几条消息（发送前后比一比，作为「是不是发出去过」的旁证）。读不到返回 -1。"""
        try:
            img = self.capture()
            if W.is_blank(img):
                return -1
            w, h = img.size
            ty = int(h * TITLE_Y_RATIO)
            ymax = int(h * 0.97)
            try:
                regs = [d["box"] for d in W.find_images(img, 0, ty, w, ymax)]
            except Exception:  # noqa: BLE001
                regs = []
            return len(self.grouped([m for m in self.messages(img, 0, ty, w, img_regions=regs)
                                     if m["y"] < ymax], w))
        except Exception:  # noqa: BLE001
            return -1

    def send_image(self, path):
        """发一张图片：剪贴板塞 DIB → Ctrl+V（微信会当成图片消息发出去）。

        优先 posted Ctrl+V（不抢前台、不动鼠标）；确认不到时再走真键鼠 + `_activate_ok`
        保险：`SetForegroundWindow` 在前台锁下会「调用成功但前台没变」，这时 Ctrl+V 的图
        会贴进用户正在用的别的窗口（实测图片就是这么发丢的 —— 日志写着「已发图」，聊天里什么都没有）。
        """
        before = self._count_images()
        # 先试无焦点路线：剪贴板塞 DIB → posted Ctrl+V（不动鼠标、不抢前台）。
        # 核对要给够时间：微信收图要传/渲染一两秒，早先只等 1.2 秒就判失败 → 又真键鼠发一遍
        # （用户看到的是同一个表情发两次，而且窗口被顶上来）。
        self._send_focusfree(image=path, settle=1.5)
        if self._wait_image_sent(before):
            return True
        if not self._send_guarded(lambda: W.set_clipboard_image(path), settle=1.2):
            return False
        if before < 0:
            return True                     # 截图读不到（全黑），没法核对，当成功
        for i in range(3):
            time.sleep(1.2 + i * 0.8)
            now = self._count_images()
            if now > before:
                return True
        # 核对只是「尽力而为」：实测图片明明发出去了（截图里两张 meme 都在），
        # 这里的计数也可能因为图片太大没被 find_images 认出来。所以只提醒、不当失败。
        print("  ! 图片发送没被确认（图可能还是发出去了，这里只是核不到）")
        return True

    def _wait_image_sent(self, before, tries=5):
        """等聊天区的图片数涨上来（比之前多 = 这张发出去了）。before<0 表示读不到，直接当成功。"""
        if before < 0:
            return True
        for i in range(tries):
            time.sleep(1.0 + i * 0.5)
            if self._count_images() > before:
                return True
        return False

    def _count_images(self):
        """聊天区里现在能看到几张图片（用来核对「图真的发出去了」）。全黑返回 -1。"""
        try:
            img = self.capture()
            if W.is_blank(img):
                return -1
            w, h = img.size
            ty = int(h * TITLE_Y_RATIO)
            ymax = int(h * (1 - BOTTOM_Y_RATIO))
            return len(W.find_images(img, 0, ty, w, ymax))
        except Exception:  # noqa: BLE001
            return -1

    def _wait_user_idle(self, need=USER_IDLE_BEFORE_SEND, budget=USER_IDLE_MAX_WAIT):
        """等用户手离开键盘再抢前台。

        真键鼠发送必须把小号窗口抢到前台，而抢过来之后用户正在敲的字会落进小号的
        输入框，再被我们的回车一起发出去（实测混出过「我是大肥鱼了」「现在 好的，」
        这种碎片）。所以抢之前先看他有没有在打字。
        """
        t0 = time.time()
        while time.time() - t0 < budget:
            idle = W.system_idle_seconds()
            if idle >= need:
                return True
            time.sleep(0.7)
        print("  ! 用户一直在敲键盘（已等 %.0f 秒），这次不抢前台、不发" % budget)
        return False

    def _send_guarded(self, paste, settle=0.5):
        """真键鼠发送 + 前台保险：只有窗口**真的**成了前台窗口才敲键盘。"""
        if not self._wait_user_idle():
            return False
        if not self._activate_ok():
            print("  ! 窗口不在前台（也不想抢前台），这次不发（免得把内容打进别的窗口）")
            return False
        print("  → 窗口本来就在前台，走真键鼠路线")
        self._send(paste, settle=settle)
        return True

    def _send(self, paste, settle=0.5):
        rate = float(self.cfg.get("rateLimitPerMinute", 8))
        elapsed = time.time() - self._last_send
        if elapsed < 60.0 / rate:
            time.sleep(60.0 / rate - elapsed)
        prev = self.v.activate()
        self.v.remember_cursor()
        try:
            time.sleep(0.5)
            # 点击点必须用**客户区**尺寸算，并且走 real_click_client（`real_click` 会减边框偏移，
            # 对「客户区坐标」是错的）。0.58/0.88 出自实测标定（输入框在客户区 y≈504~632）。
            cw, ch = self.v.client_size
            self.v.real_click_client(int(cw * 0.58), int(ch * 0.88))
            time.sleep(0.3)
            self.v.real_key(VK_A, ctrl=True)
            time.sleep(0.2)
            self.v.real_key(VK_DELETE)
            time.sleep(0.2)
            paste()
            time.sleep(0.3)
            self.v.real_key(VK_V, ctrl=True)
            time.sleep(settle)                     # 图片要等微信把图读进来
            self.v.real_key(VK_RETURN)
            time.sleep(0.8)
        finally:
            self.v.park_hover()                    # 悬停挪走 + 光标还回去（别留 tooltip 当消息）
            self.v.restore_foreground(prev)
        self._last_send = time.time()

    # ---------- 主循环 ----------
    @staticmethod
    def norm_title(title):
        """「兼职群(78)」→「兼职群」；群名后面会带成员数。"""
        return re.sub(r"[（(]\d+[)）]\s*$", "", (title or "").strip()).strip()

    def name_matches(self, title, names):
        """名字匹配（用于白名单/群名单）。

        精确相等之外还认「包含关系」：用户那个群叫 `某群`，
        配置里只写 `某群` 也要能匹配上（OCR 出来的标点/成员数不稳定，
        写死全名很容易因为一个字符就永远匹配不上）。
        """
        t = self.norm_title(title)
        for n in names:
            n = self.norm_title(n)
            if not n:
                continue
            if t == n or n in t or t in n:
                return True
        return False

    def targets(self):
        """要盯的聊天窗清单（私聊 + 群聊）。

        微信 4.x 给每个会话开一个独立顶层窗口，所以「盯多个会话」= 盯多个窗口。
        返回 [{hwnd,title,name,kind}]，kind = direct（私聊）| group（群聊）。
        """
        wl = [str(x) for x in self.cfg.get("whitelist", [])]
        gp = [str(x) for x in self.cfg.get("groups", [])]
        try:
            pid = W.find_small_pid(wl + gp)
        except Exception:  # noqa: BLE001
            pid = self.pid
        wins = W.list_windows(pid)
        if not wins:                            # 微信重启/窗口都没了 → 重新解析一次
            try:
                self._resolve()
                pid = self.pid
                wins = W.list_windows(pid)
            except Exception as exc:  # noqa: BLE001
                print("! 重新找窗口失败:", type(exc).__name__, exc)
                return []
        out = []
        auto_groups = bool(self.cfg.get("autoGroups", True))
        for w in wins:
            t = (w["title"] or "").strip()
            base = self.norm_title(t)
            if self.name_matches(t, wl):
                kind = "direct"
            elif self.name_matches(t, gp):
                kind = "group"
            elif (auto_groups and t != "微信"
                  and (looks_like_group(t) or self.name_matches(t, self._known_groups))):
                # 自动认群：用户被拉进新群时不用改配置也能生效
                # （群里也不是全回：有人 @ 才回，没人 @ 按 groupSpontaneousRate 掷骰子）
                # `_known_groups`：会话列表里见过的群名。有些群的窗口标题带坐标/成员数，
                # 只靠 looks_like_group（要求以「(数字)」结尾）会漏掉。
                kind = "group"
            else:
                kind = None
            if kind:
                out.append({"hwnd": w["hwnd"], "title": t, "name": base, "kind": kind})
        if pid != self.pid:
            self.pid = pid
        return out

    def vision_for(self, hwnd):
        """按窗口缓存 WxVision（WGC 会话是按窗口建的，不能每次重建）。"""
        v = self._visions.get(hwnd)
        if v is None or v.hwnd != hwnd:
            v = W.WxVision(pid=self.pid, hwnd=hwnd, backend=self.capture_backend())
            self._visions[hwnd] = v
        return v

    def poll_once(self, dry=True):
        tg = self.targets()
        if not tg:                              # 没有独立聊天窗 → 退回「会话列表」模式
            return self.poll_list(dry)
        out = []
        for t in tg:
            try:
                out += self.poll_chat(t, dry, kind=t["kind"])
            except Exception as exc:  # noqa: BLE001
                print("! 轮询 %s 出错: %s %s" % (t["name"], type(exc).__name__, exc))
        # 顺手扫一眼会话列表：微信 4.x 只给**打开过的**会话开独立窗口，群里有人 @ 我时
        # 那个群的窗口根本不存在 —— 不看列表就永远发现不了。所以每 LIST_SCAN_SECONDS
        # 也扫一遍列表，发现有未读的群就点开（点开会新开一个窗口，下一轮就轮到上面
        # 的多窗口逻辑接管了）。
        now = time.time()
        if now - getattr(self, "_last_list_scan", 0) > LIST_SCAN_SECONDS:
            self._last_list_scan = now
            try:
                out += self.poll_list(dry)
            except Exception as exc:  # noqa: BLE001
                print("! 扫会话列表出错: %s %s" % (type(exc).__name__, exc))
        return out

    def blank_guard(self, img, name):
        """截图全黑 → 读不到内容。返回一条状态；必要时（10 分钟一次）解释原因。

        全黑的两个已知成因：① 窗口被完全遮挡（系统停止重绘）；② 本进程跑在
        低完整性沙箱里（读不到别的进程窗口）。后者是硬限制，必须换正常权限跑。
        """
        if not W.is_blank(img):
            return None
        now = time.time()
        if now - getattr(self, "_blank_logged", 0) > 600:
            self._blank_logged = now
            print("[blank] %s 截图全黑，读不到内容 —— %s" % (name, W.integrity_hint()))
            print("        抓图后端 = %s（auto 会先试 WGC 再退 PrintWindow）"
                  % getattr(self.v, "last_backend", "?"))
            print("        窗口 %s rect=%s" % (self.v.hwnd, self.v.rect))
        return [(name, "(窗口不可读)", "blank")]

    def poll_chat(self, win, dry=True, kind="direct", pane=False):
        """独立聊天窗：整窗就是消息区，直接读（实测不激活也能读到，别抢用户焦点）。

        去重不用「最后一条入消息」的文本/y：那个方案在两种情况下会翻车——
          ① 用户连发两条一模一样的话（文本相同 → 漏读）；
          ② 面板滚动后同一条消息的 y 变了（→ 重复回）。
        改用**可见消息尾巴的指纹**（最后 8 条的 side:文本 拼接）+ 10 分钟内回过去重：
        只有「尾巴最后一条是别人发的」才需要回，回完把指纹记进 state。

        群聊（kind='group'）：被 @ 必回；没人 @ 时按 groupSpontaneousRate 掷一次骰子
        （同一段消息只掷一次并记进 state，否则每 3 秒重掷一次=必回）。
        """
        name = win["name"] if isinstance(win, dict) else win["title"]
        self.v = self.vision_for(win["hwnd"])
        img = self.capture()
        blank = self.blank_guard(img, name)
        if blank:
            return blank
        # 像素完全没变 = 画面内容没变 → 跳过 OCR（一张图 ~1 秒，多窗口时省很多）。
        # 每 FRAME_RECHECK_SECONDS 秒强制重认一次，防止「画面没变但其实有变化」的极端情况。
        fp = hashlib.md5(img.tobytes()).hexdigest()
        cache = self._frame_fp.setdefault(name, {})
        if cache.get("fp") == fp and time.time() - cache.get("t", 0) < FRAME_RECHECK_SECONDS:
            return [(name, cache.get("text", "(画面未变)"), "chat-cached")]
        w, h = img.size
        # pane=True：主窗口右栏模式。微信 4.1.15 实测「双击会话行」不一定开独立窗口
        # （有时只是把聊天显示在主窗口右栏），那就在主窗口上读/回 —— 右栏本身就是完整的
        # 聊天视图，px 是列表区/聊天区的分界（按比例算，WGC 帧与客户区尺寸不同也适用）。
        px = int(w * PANE_X_RATIO) if pane else 0
        # 标题行与底部输入区按**比例**算：WGC 帧（客户区 ×1.25）与 PrintWindow 帧尺寸不同，
        # 写死的 95/h-45 会让 WGC 下读到输入框里的「发送」而不是消息（实测踩过）。
        ty = int(h * TITLE_Y_RATIO)
        ymax = int(h * (1 - BOTTOM_Y_RATIO))
        # 图片区域只算一次：既用来产出 `[图片]` 伪消息，也用来把**图上的字**从 OCR 结果里剔掉。
        try:
            regions = W.find_images(img, px, ty, w, ymax)
        except Exception as exc:  # noqa: BLE001
            print("! 图片识别出错: %s %s" % (type(exc).__name__, exc))
            regions = []
        groups = self.grouped([m for m in self.messages(img, px, ty, w, img_regions=[d["box"] for d in regions])
                               if m["y"] < ymax], w)
        msgs = [g for g in groups if g.get("side") in ("in", "out")]
        msgs += self.image_items(img, w, h, regions=regions, px=px)   # 图片气泡也算一条消息
        msgs.sort(key=lambda g: g["y"])
        if not msgs:
            return [(name, "(没有收到消息)", "chat")]
        tail = msgs[-8:]
        # 尾巴指纹：图片气泡带上的是**路径**，而每次截图裁出来的图内容有一两个像素的抖动，
        # 内容哈希就变了（实测同一张图每轮都存成新文件名 img-xxxx.png）→ 指纹每轮都不同，
        # 「这段回过了 / 掷过骰子了」的记忆全部失效。所以指纹里只留 `[图片]` 不带路径。
        tail_sig = "|".join("%s:%s" % (g["side"], sig_norm(g["text"]))
                            for g in tail)
        cache["fp"] = fp
        cache["t"] = time.time()
        cache["text"] = tail[-1]["text"]
        st = self.state.setdefault(name, {})
        st["last_tail"] = tail_sig
        st["last_seen_y"] = tail[-1]["y"]
        save_json(self.state_path, self.state)
        if tail[-1]["side"] == "out":
            return [(name, tail[-1]["text"], "chat")]          # 最后一条是我们回的，等对方开口
        pending = []
        for g in reversed(tail):
            if g["side"] == "out":
                break
            pending.append(g["text"])
        pending.reverse()
        text = "\n".join(pending)
        now = time.time()
        recent = [p for p in st.get("replied", []) if now - p[1] < 600]
        st["replied"] = recent
        for sig, _ts in recent:
            if sig == tail_sig:
                print("[skip] %s 这段 10 分钟内已经回过了" % name)
                return [(name, text, "chat")]
        # 群里刚接手时不要「补课」：开机/重启后看到的往往是一堆旧消息（还带着旧的 @），
        # 全回一遍既刷屏又尴尬。第一次看到这个群只登记尾巴，之后只回新 @ 与 5% 自发。
        if kind == "group" and name not in self._primed:
            self._primed.add(name)
            print("[init] %s 刚接手，群里已有的消息不补课（只回新 @ 与 %.0f%% 自发）: %s"
                  % (name, float(self.cfg.get("groupSpontaneousRate", 0.05)) * 100,
                     text.replace("\n", " / ")[:60]))
            return [(name, text, "chat")]
        print("[hit] %s 新消息: %s" % (name, text.replace("\n", " / ")))
        if kind == "group":
            mentioned = self.is_mentioned(text, name)
            if not mentioned and time.time() - self._list_mentions.get(name, 0) < 600:
                # 微信列表说「有人@我」，但按昵称没匹配上：多半是小号昵称改了。
                # 顺手学一下：正文里 @ 后面若是英数名（bob / alice）就记进这个群，下次直接匹配。
                who = ASCII_MENTION_RE.search(text)
                if who:
                    learned = st.setdefault("learned_names", [])
                    if who.group(1) not in learned:
                        learned.append(who.group(1))
                        save_json(self.state_path, self.state)
                        print("[群] 列表提示「有人@我」但昵称对不上 → 学到新昵称 @%s" % who.group(1))
                mentioned = True
                print("[群] %s 会话列表显示「有人@我」→ 当成被点名" % name)
            if mentioned:
                print("[群] %s 有人 @ 我，必回" % name)
            else:
                rate = float(self.cfg.get("groupSpontaneousRate", 0.05))
                dec = st.setdefault("decisions", {}).get(tail_sig)
                if dec is None:                 # 同一段消息只掷一次骰子
                    dec = "reply" if random.random() < rate else "skip"
                    decs = st["decisions"]
                    decs[tail_sig] = dec
                    for k in list(decs)[:-20]:
                        decs.pop(k, None)
                    save_json(self.state_path, self.state)
                if dec == "skip":
                    print("[群] %s 没人 @ 我，这次不掺和（自发率 %.0f%%）" % (name, rate * 100))
                    return [(name, text, "chat")]
                print("[群] %s 没人 @ 我，但这次想说话（自发率 %.0f%%）" % (name, rate * 100))
        self.reply_to(name, text, dry=dry, sig=tail_sig, kind=kind)
        return [(name, text, "chat")]

    def image_items(self, img, w, h, regions=None, px=0):
        """聊天区里的图片气泡 → 伪消息（形如 `[图片] 路径`），参与尾巴指纹与回复。

        裁出来的图存到 mediaDir（文件名是内容哈希，同一张图每次都是同一个文件），
        DSH 侧可以用 read_image 看它。
        """
        outdir = self.cfg.get("mediaDir") or os.path.join(HERE, "media")
        items = []
        ty = int(h * TITLE_Y_RATIO)
        ymax = int(h * (1 - BOTTOM_Y_RATIO))
        try:
            for d in (regions if regions is not None else W.find_images(img, px, ty, w, ymax)):
                path = W.save_bubble(img, d["box"], outdir)
                items.append({"side": "out" if d["box"][2] > w * 0.80 else "in",
                              "text": "[图片] %s" % path, "y": d["box"][1], "kind": "image"})
        except Exception as exc:  # noqa: BLE001
            print("! 图片识别出错: %s %s" % (type(exc).__name__, exc))
        return items

    def is_mentioned(self, text, name=None):
        """群里被点名了吗：配置关键词 + 小号自己的昵称 + 学到的昵称。

        「@昵称」或直接出现昵称都算。`name` 给群名时会把它学到的昵称（learned_names）
        也算上 —— 用户改一次微信昵称，写死的 selfNickname 就失效了（踩过：小号改叫
        bob，群里 @bob 了，桥还拿 alice 去比 → 判「没人@我」不回）。
        """
        kws = [str(x) for x in self.cfg.get("mentionKeywords", [])]
        nick = str(self.cfg.get("selfNickname", "") or "").strip()
        if nick:
            kws.append(nick)
        if name:
            kws += [str(x) for x in (self.state.get(name, {}) or {}).get("learned_names", [])]
        for k in kws:
            k = str(k).strip()
            if not k:
                continue
            if ("@" + k) in text or k in text:
                return True
        return False

    def reply_to(self, name, text, dry=True, sig=None, kind="direct"):
        st = self.state.setdefault(name, {})
        if kind == "group":
            prompt = ("【群聊 · %s】下面是这个群里最近的几条消息（行首可能是发言人名字）：\n%s\n"
                      "回一句发到群里。有人 @ 我就优先回应那个人；没有的话就顺着话题说一句，"
                      "别 @ 错人、别复述谁说了什么。" % (name, text))
        else:
            prompt = text
        if "[图片]" in text:
            prompt += ("\n\n（上面有图片：先用 read_image 看清图上内容，再决定怎么回。）")
        prompt += ("\n\n（想斗图就单独占一行写 `[表情: 简短描述]`，桥会从表情库挑一张发出去；"
                   "描述用中文短词，比如「无语」「摸头」「害羞」。）")
        if dry:
            print("  [dry] 本应回复：%s" % text)
            return
        agent = DshAgent(self.sessions.get(name),
                         permission=self.cfg.get("agentPermission"),
                         workdir=self.cfg.get("agentWorkspace"))
        reply, sid, err = agent.ask(prompt)
        self.sessions[name] = sid
        st["session_id"] = sid
        save_json(self.state_path, self.state)
        if not reply:
            print("  ! agent 没给回复（stderr: %s）" % err[:300])
            return
        print("  → %s: %s" % (name, reply.replace("\n", " / ")[:150]))
        self.send_reply(reply)
        if sig:                                  # 记下「这段尾巴回过了」，防止重启/滚动后重回
            now = time.time()
            rep = [list(p) for p in st.get("replied", []) if now - p[1] < 600]
            rep.append([sig, now])
            st["replied"] = rep[-5:]
            st["last_replied_sig"] = sig
            save_json(self.state_path, self.state)

    def poll_list(self, dry=True):
        """会话列表模式：没有独立聊天窗时的兜底。

        命中白名单/群聊且预览变化或有未读红点 → 点开会话 → 之后交给 poll_chat
        （微信点开会话会新开一个独立窗口，所以下一次轮询就轮到多窗口逻辑接管了）。
        """
        wl = {self.norm_title(x) for x in self.cfg.get("whitelist", [])}
        gp = {self.norm_title(x) for x in self.cfg.get("groups", [])}
        v = self.main_vision()          # 会话列表只在主窗口里
        rows, _img = self.rows(v)
        if W.is_blank(_img):
            blank = self.blank_guard(_img, "会话列表")
            return blank or []
        seen = []
        for r in rows:
            # 返回格式要和 poll_chat 一致（name, 文本, 状态）：主循环拿它比对状态、
            # 打印日志。早先这里直接塞 (行号, 名字, 预览, 未读) 四元组，日志里就冒出
            # 一串莫名其妙的 "(0, '呜..', '那图上那位确实可爱', False)"。
            seen.append((r["name"], r["preview"], "list%s" % ("+unread" if r["unread"] else "")))
            name = r["name"]
            key = self.norm_title(name)
            # 见过像是群的名字（「校友群(23)」）就记住它是群：等它的独立窗口出来时，
            # 哪怕标题只写「校友群」也能认出来（见 targets()）。
            if name and key and (looks_like_group(name) or self.name_matches(name, gp)):
                self._known_groups.add(key)
            # 群里那一行 OCR 出来常常带成员数（「兼职群(78)」），要先归一化/模糊再比名单，
            # 否则群永远匹配不上、@ 也就永远不生效。
            if not name or not (self.name_matches(name, wl) or self.name_matches(name, gp)):
                continue
            # 微信自己的「有人@我」标记：最可靠的点名信号，且不受昵称改动影响。
            if MENTION_FLAG_RE.search(r["preview"] or ""):
                self._list_mentions[key] = time.time()
                print("[群] 会话列表显示「有人@我」: %s（%r）" % (name, r["preview"]))
            prev = self._last_preview.get(key)
            self._last_preview[key] = r["preview"]
            if prev is None:
                print("[init] %s 当前最后一条预览: %s" % (name, r["preview"]))
                continue
            if r["preview"] == prev and not r["unread"]:
                continue
            # 已经有它的独立窗口了就别再点（重复双击会开出一堆同样的窗口）
            if any(self.name_matches(t.get("title") or "", [name]) for t in self.targets()):
                continue
            print("[hit] %s 有变化（badge=%s）: %r → %r"
                  % (name, r["unread"], prev, r["preview"]))
            # 点开会话要抢前台（双击），同一条没读成功的行每轮都点会一直弹窗、很吵。
            # 同一个会话 90 秒内只尝试开一次。
            tried = getattr(self, "_open_tried", None)
            if tried is None:
                tried = self._open_tried = {}
            now = time.time()
            if now - tried.get(key, 0) < 90:
                continue
            tried[key] = now
            self.handle_contact(r["row"], name, dry=dry, v=v,
                                xy=(r["x_name"] + 40, r["y_name"] + 30))
        return seen

    @contextmanager
    def activated(self):
        """Qt Quick 的聊天面板只在窗口激活时渲染：读消息期间短暂激活，读完还回前台。"""
        prev = self.v.activate()
        time.sleep(0.9)
        try:
            yield
        finally:
            self.v.restore_foreground(prev)

    def kind_of(self, name):
        """这个名字是群还是单独联系人：群名单 / 记住过的群 / 标题带成员数括号 → group。"""
        norm = self.norm_title(name)
        if norm in self._known_groups:
            return "group"
        gp = [str(x) for x in (self.cfg.get("groups") or []) if str(x).strip()]
        if gp and self.name_matches(name, gp):
            return "group"
        return "group" if looks_like_group(name) else "direct"

    def handle_contact(self, index, name, dry=True, v=None, xy=None):
        """点开会话（posted 点击，不抢前台），然后交给 poll_chat 去读+回。"""
        if v is not None and v is not self.v:
            self.v = v               # 点列表、读标题都得在会话列表那个窗口上做
        self.open_row(index, v, xy=xy)          # posted 点击：不动鼠标、不抢前台
        title, _img, _px, _ty, _w = self.current_title()
        # OCR 读出来的行名常被截断（`某群(38°41'N,11..`），用包含关系比而不是相等
        if not self.name_matches(title, [name]) and self.norm_title(name) not in title:
            print("  ! 打开的会话 %r 与目标 %r 不符，跳过" % (title, name))
            return
        # 双击之后微信要几十毫秒到一两秒才把独立窗口画出来；早先只等 0.8 秒，
        # 窗口还没出现就被判「没找到对应聊天窗」，于是这条消息永远读不到也回不了。
        hit = None
        for _ in range(6):
            time.sleep(0.5)
            for t in self.targets():
                if t["name"] == self.norm_title(name) or t["hwnd"] == self.v.hwnd:
                    hit = t
                    break
            if hit:
                break
        if hit:
            self.poll_chat(hit, dry, kind=hit["kind"])
            return
        # 微信 4.1.15 实测：双击会话行**常常不开独立窗口**，只是把聊天显示在主窗口右栏
        # （用户截图里就是这样）。那就不等独立窗口了 —— 主窗口右栏本身就是完整聊天视图，
        # 直接以 pane=True 在主窗口上读+回（发消息的输入框也在右栏底部，坐标同一个）。
        try:
            main = self.main_vision()
        except Exception as exc:  # noqa: BLE001
            print("  ! 拿不到主窗口: %s %s" % (type(exc).__name__, exc))
            main = None
        if main is not None:
            print("  · %r 没有独立窗口，改用主窗口右栏读/回" % name)
            self.v = main
            kind = self.kind_of(name)
            if xy:
                # posted 点击重试 + 真双击兜底（详见 open_row_in_pane 的注释）
                self.open_row_in_pane({"row": index, "x_name": xy[0] - 40, "y_name": xy[1] - 30},
                                      main)
            title, _img, _px, _ty, _w = self.current_title()
            if self.name_matches(title, [name]) or self.norm_title(name) in title:
                self.poll_chat({"name": self.norm_title(name), "hwnd": main.hwnd,
                                "title": title, "kind": kind}, dry, kind=kind, pane=True)
                return
            print("  ! 右栏显示的是 %r，不是 %r，放弃这一轮" % (title, name))
        print("  ! 打开 %r 之后没找到对应聊天窗（可能还在同一个窗口里）" % name)
        try:
            wins = W.list_windows(self.pid)
            print("     当前该进程的窗口: %s"
                  % " | ".join("%s %s %s" % (w["hwnd"], w["title"] or "(无标题)", w["size"])
                               for w in wins[:8]))
        except Exception as exc:  # noqa: BLE001
            print("     列窗口也失败了: %s %s" % (type(exc).__name__, exc))

    def send_meme(self, desc):
        """按描述从表情包库挑一张发出去。"""
        path = meme_pick(desc)
        if not path:
            print("  ! 表情库里没找到图，跳过 [表情: %s]" % desc)
            return None
        print("  [表情] %s → %s" % (desc, os.path.basename(path)))
        self.send_image(path)
        return path

    def send_reply(self, reply):
        """把 agent 的回复发出去：正文按段发，`[表情: 描述]` 变成一张表情图。

        顺序保持原样（先说的话先发，图还在原来的位置上发）。
        """
        pos = 0
        for m in MEME_MARKER.finditer(reply or ""):
            before = reply[pos:m.start()]
            if before.strip():
                for chunk in self.split_reply(before):
                    self.send_text(chunk)
            self.send_meme(m.group(1).strip())
            pos = m.end()
        tail = (reply or "")[pos:]
        if tail.strip():
            for chunk in self.split_reply(tail):
                self.send_text(chunk)

    @staticmethod
    def split_reply(text, limit=900):
        """微信里长回复分段发；空行也作为分段符号。"""
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        chunks, cur = [], ""
        for p in parts:
            if len(cur) + len(p) + 1 > limit:
                if cur:
                    chunks.append(cur)
                cur = p
            else:
                cur = (cur + "\n" + p) if cur else p
        if cur:
            chunks.append(cur)
        return chunks or [text.strip()]


def pid_alive(pid):
    """OpenProcess 探活（比 WMI 可靠：本机 WMI 的进程枚举时灵时不灵）。"""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))
        if h:
            k.CloseHandle(h)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


CMD_PATH = os.path.join(HERE, "wxbridge-cmd.txt")
CMD_OUT = os.path.join(HERE, "wxbridge-cmd.out")


def run_command(bridge, line):
    """执行一条文件命令（见 poll_commands 的说明）。

    桥跑在正常权限下、而写命令的人（DSH 会话）跑在低完整性沙箱里，两边没法直接通信，
    于是用「约定文件」传命令，结果写进 wxbridge-cmd.out。
    """
    head, _, rest = line.partition(" ")
    head, rest = head.strip().lower(), rest.strip()

    def pick(name):
        # 用模糊匹配：群窗口标题是 `某群(38°41′N,116°7′E版)`，命令里写 `某群` 就该命中
        for t in bridge.targets():
            if t["name"] == name or bridge.name_matches(t.get("title") or "", [name]):
                bridge.v = bridge.vision_for(t["hwnd"])
                return t
        return None

    if head in ("selfcheck", "check"):
        integ = W.my_integrity() or 0
        out = ["完整性 = %s（%s）" % (integ, "正常权限" if integ >= 0x2000 else "低完整性沙箱：读不到别的窗口"),
               "抓图后端 = %s" % getattr(bridge.v, "last_backend", "?"),
               "小号 pid = %s" % bridge.pid]
        for t in bridge.targets():
            v = bridge.vision_for(t["hwnd"])
            img = v.capture()
            out.append("  %-16s %-6s hwnd=%s rect=%s 全黑=%s"
                       % (t["name"], t["kind"], t["hwnd"], v.rect, W.is_blank(img)))
        return "\n".join(out)

    if head == "windows":
        out = []
        for w in W.all_windows(min_side=100):
            if bridge.pid and w.get("pid") == bridge.pid:
                w2, h2 = w["size"] if isinstance(w.get("size"), tuple) else (0, 0)
                out.append("  hwnd=%-10s %-24s %sx%s" % (w["hwnd"], (w.get("title") or "")[:24], w2, h2))
        return "\n".join(out) or "  （没看到小号的窗口）"

    if head in ("pasteonly", "pastein"):
        # 诊断用：只做「点输入框 + WM_PASTE」，不发回车；然后把输入框区域 OCR 出来，
        # 用来判断「posted 输入到底能不能进到微信输入框」。
        name, sep, text = rest.partition(":")
        if not sep:
            name, sep, text = rest.partition("：")
        name, text = name.strip(), text.strip()
        t = pick(name) if name else None
        if t:
            t = t
        else:
            ts = bridge.targets()
            if not ts:
                return "没有匹配的聊天窗"
            t = ts[0]
            bridge.v = bridge.vision_for(t["hwnd"])
        cw, ch = bridge.v.client_size
        px, py = int(cw * 0.58), int(ch * 0.88)
        bridge.v.click_client(px, py, double=True)
        bridge.v.click_client(px, py)
        time.sleep(0.3)
        if text:
            W.set_clipboard(text)
        bridge.v.post_paste()
        time.sleep(1.2)
        img = bridge.v.capture()
        h = img.size[1]
        boxes = W.ocr_boxes(img, y0=int(h * 0.72), y1=h)
        outdir = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
        os.makedirs(outdir, exist_ok=True)
        p = os.path.join(outdir, "pasteonly-%s.png" % safe_name(t["name"]))
        img.save(p)
        return ("点击点(客户区) = (%s, %s)；输入框区域 OCR: %s\n截图: %s"
                % (px, py, " | ".join(b["text"] for b in boxes) or "（在读到的区域里没看到文字）", p))

    if head in ("send", "meme", "sendtest", "imgtest"):
        name, sep, text = rest.partition(":")
        if not sep:
            name, sep, text = rest.partition("：")
        name, text = name.strip(), text.strip()
        if not name or not text:
            return "用法: %s <会话名>: <内容>" % head
        t = pick(name)
        if not t:
            return "找不到会话 %r；现有: %s" % (name, [x["name"] for x in bridge.targets()])
        # 关键：把 bridge.v 切到**目标那个窗口**再发。早先没切，发送与「发出去没有」的
        # 截图自检都在另一个窗口上做 → 明明发成功了，自检看不到 → 又用真键鼠重发一遍
        # （实测聊天里同一条消息出现两次，而且第二次把窗口顶到了最上面）。
        prev_v, bridge.v = bridge.v, W.WxVision(hwnd=t["hwnd"])
        try:
            if head == "meme":
                path = meme_pick(text)
                if not path:
                    return "表情库里没找到图（描述 %r）" % text
                bridge.send_image(path)
                return "已发图: %s（描述 %r → %s）" % (os.path.basename(path), text, t["name"])
            if head == "sendtest":
                # 只走 posted 路线（不退回真键鼠），然后截图自检 —— 用来分辨
                # 「posted 根本没发出去」还是「发出去了但自检没认出来」。
                bridge._send_focusfree(text=text)
                time.sleep(2.5)
                seen = bridge._verify_sent(text, tries=1)
                img = bridge.v.capture()
                shots = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
                os.makedirs(shots, exist_ok=True)
                path = os.path.join(shots, "sendtest-%s.png" % safe_name(t["name"]))
                img.save(path)
                return ("posted-only 发送 → %s(hwnd=%s)：自检 seen=%s；截图 %s（%dx%d）"
                        % (t["name"], t["hwnd"], seen, path, img.size[0], img.size[1]))
            if head == "imgtest":
                ipp = meme_pick(text)
                if not ipp:
                    return "表情库里没找到图（描述 %r）" % text
                before = bridge._count_images()
                bridge._send_focusfree(image=ipp, settle=1.5)
                time.sleep(3.5)
                after = bridge._count_images()
                img = bridge.v.capture()
                shots = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
                os.makedirs(shots, exist_ok=True)
                path = os.path.join(shots, "imgtest-%s.png" % safe_name(t["name"]))
                img.save(path)
                return ("posted-only 发图 %s（%r）→ %s：图数 %s → %s；截图 %s"
                        % (os.path.basename(ipp), text, t["name"], before, after, path))
            bridge.send_reply(text)
            return "已发送到 %s: %s" % (t["name"], text.replace("\n", "\\n")[:120])
        finally:
            bridge.v = prev_v

    if head == "read":
        targets = bridge.targets()
        if rest:
            t = pick(rest)
            targets = [t] if t else []
        if rest and not targets:
            # 没有独立窗口（微信 4.1.15 双击常常不开窗）→ 点开那一行后直接读主窗口右栏。
            try:
                mv = bridge.main_vision()
                rows, _img = bridge.rows(mv)
                hit = [r for r in rows if bridge.name_matches(r["name"], [rest])]
                if not hit:
                    return "会话列表里找不到 %r；现在读到的行: %s" % (
                        rest, [(r["row"], r["name"]) for r in rows])
                r = hit[0]
                prev_v, bridge.v = bridge.v, mv
                try:
                    shots = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
                    os.makedirs(shots, exist_ok=True)
                    before = mv.capture()
                    before.save(os.path.join(shots, "pane-before.png"))
                    # posted 点击优先（不抢前台）；open_row_in_pane 内部会重试，
                    # 只有 posted 完全点不开时它才用一次真双击兜底
                    img = bridge.open_row_in_pane(r, mv)
                    img.save(os.path.join(shots, "pane-after.png"))
                    pw, ph = img.size
                    pxx = int(pw * PANE_X_RATIO)
                    tyy = int(ph * TITLE_Y_RATIO)
                    ymaxx = int(ph * (1 - BOTTOM_Y_RATIO))
                    head_boxes = W.ocr_boxes(img, x0=pxx, x1=pw, y0=0, y1=tyy)
                    groups = bridge.grouped(
                        [m for m in bridge.messages(img, pxx, tyy, pw) if m["y"] < ymaxx], pw)
                    try:
                        same = int(sum(abs(a - b) for a, b in
                                       zip(before.convert("L").resize((64, 64)).getdata(),
                                           img.convert("L").resize((64, 64)).getdata())) / 4096)
                    except Exception:  # noqa: BLE001
                        same = -1
                finally:
                    bridge.v = prev_v
                lines = ["  主窗口右栏（标题 %r，px=%s，点击前后平均亮度差 %s）："
                         % (" | ".join(b["text"] for b in head_boxes), pxx, same),
                         "    点的是 [%d] %r（帧坐标 %d,%d）；两张图 %s\\pane-before.png / pane-after.png"
                         % (r["row"], r["name"], r["x_name"] + 40, r["y_name"] + 30, shots)]
                for g in groups[-8:]:
                    lines.append("    [%-3s] %s" % (g.get("side"), g.get("text")))
                return "\n".join(lines)
            except Exception as exc:  # noqa: BLE001
                return "右栏读取失败: %s: %s" % (type(exc).__name__, exc)
        out = []
        for t in targets:
            v = bridge.vision_for(t["hwnd"])
            bridge.v = v
            # 别激活：激活＝把窗口顶到最上层，用户会被打断（截图不需要窗口在前台，
            # 遮挡时 WGC 也能抓到内容）。早先这里包了 activated()，正是「看消息也抢前台」的来源。
            img = v.capture()
            if W.is_blank(img):
                out.append("  %s: 截图全黑（读不到）" % t["name"])
                continue
            w, h = img.size
            ty = int(h * TITLE_Y_RATIO)
            ymax = int(h * (1 - BOTTOM_Y_RATIO))
            groups = bridge.grouped([m for m in bridge.messages(img, 0, ty, w)
                                     if m["y"] < ymax], w)
            out.append("  %s（%s）:" % (t["name"], t["kind"]))
            for g in groups[-8:]:
                out.append("    [%-3s] %s" % (g.get("side"), g.get("text")))
        return "\n".join(out) or "（没有匹配的聊天窗）"

    if head in ("list", "rows"):
        """把桥从会话列表里读到的行原样打出来（排查「为什么没认出来/认错」）。

        `list boxes` 额外把该窗口里的每个 OCR 文字块（x0/y0/x1/text）打出来，
        行聚类不对时用它看真实坐标。
        """
        v = bridge.main_vision()
        if rest.strip() in ("boxes", "raw"):
            img = v.capture()
            w, h = img.size
            bs = W.ocr_boxes(img, x0=int(w * 0.085), x1=int(w * 0.45))
            return "截图 %sx%s，文字块 %d 个：\n%s" % (
                w, h, len(bs),
                "\n".join("  y0=%4d x0=%4d x1=%4d %r" % (b["y0"], b["x0"], b["x1"], b["text"])
                          for b in bs))
        rows, img = bridge.rows(v)
        try:
            outdir = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
            os.makedirs(outdir, exist_ok=True)
            p = os.path.join(outdir, "list-main.png")
            img.save(p)
            head_line = "会话列表窗口 hwnd=%s title=%r 截图 %sx%s → %s" % (
                v.hwnd, getattr(v, "title", ""), img.size[0], img.size[1], p)
        except Exception as exc:  # noqa: BLE001
            head_line = "（存图失败: %s）" % exc
        body = "\n".join("  [%2d]%s 名字 %r | 预览 %r"
                         % (r["row"], " ●未读" if r["unread"] else "     ", r["name"], r["preview"])
                         for r in rows) or "（会话列表里没读到行）"
        return head_line + "\n" + body

    if head == "clickscan":
        """标定用：扫一串客户区 y，看哪个位置点下去能让右栏真的切换会话。

        背景：双击/单击会话行在微信 4.1.15 上有时候「点了没反应」——右栏还是空的。
        可能是坐标映射（WGC 帧 → 客户区）偏了，也可能是 posted 点击不够。这个命令
        把两种点击（posted / 真键鼠）都试一遍，每点一次就 OCR 右栏标题，直接看结果。
        """
        import wxvision as WV
        v = bridge.main_vision()
        cw, ch = v.client_size
        img = v.capture()
        fw, fh = img.size
        rows0, _ = bridge.rows(v)
        out = ["主窗口 hwnd=%s rect=%s 客户区=%sx%s 帧=%sx%s 后端=%s"
               % (v.hwnd, v.rect, cw, ch, fw, fh, getattr(v, "last_backend", "?")),
               "读到 %d 行: %s" % (len(rows0), [(r["row"], r["name"], r["y_name"]) for r in rows0])]
        # 候选：按「行名 y」换算出的客户区位置，以及它上下各偏 20 像素
        if not rows0:
            return "\n".join(out + ["（没读到行，无法标定）"])
        r = rows0[0] if not rest else (rows0[int(rest)] if rest.isdigit() and int(rest) < len(rows0) else rows0[0])
        base_y = int(round((r["y_name"] + 30) * ch / float(fh)))
        base_x = int(round((r["x_name"] + 40) * cw / float(fw)))
        out.append("以行 [%d] %r 为基准：客户区基准点 (%d, %d)" % (r["row"], r["name"], base_x, base_y))
        for mode in ("posted", "real"):
            for dy in (-40, -20, 0, 20, 40):
                cy = base_y + dy
                with bridge.activated():
                    if mode == "real":
                        v.real_click_client(base_x, cy)
                    else:
                        v.click_client(base_x, cy)
                    time.sleep(1.4)
                    shot = v.capture()
                w2, h2 = shot.size
                px2 = int(w2 * PANE_X_RATIO)
                ty2 = int(h2 * TITLE_Y_RATIO)
                boxes = W.ocr_boxes(shot, x0=px2, x1=w2, y0=0, y1=ty2)
                body = W.ocr_boxes(shot, x0=px2, x1=w2, y0=ty2, y1=int(h2 * (1 - BOTTOM_Y_RATIO)))
                out.append("  %-6s cy=%4d → 右栏标题 %r，正文块 %d"
                           % (mode, cy, " ".join(b["text"] for b in boxes), len(body)))
        return "\n".join(out)

    if head == "open":
        """按关键词点开会话列表里的一行（不会触发回复），用来把群窗口先拉出来。"""
        if not rest:
            return "用法: open <会话名关键词>"
        v = bridge.main_vision()
        rows, _img = bridge.rows(v)
        hit = [r for r in rows if bridge.name_matches(r["name"], [rest])]
        if not hit:
            return "会话列表里找不到 %r；现在读到的行: %s" % (
                rest, [(r["row"], r["name"]) for r in rows])
        r = hit[0]
        prev_v, bridge.v = bridge.v, v
        try:
            # posted 点击（不抢前台）：只是让会话显示出来，不需要窗口在前台
            bridge.open_row(r["row"], v, xy=(r["x_name"] + 40, r["y_name"] + 30))
        finally:
            bridge.v = prev_v
        time.sleep(1.2)
        wins = W.list_windows(bridge.pid)
        return "已点开 [%d] %r（预览 %r）；现在微信的窗口:\n%s" % (
            r["row"], r["name"], r["preview"],
            "\n".join("  hwnd=%s %r %sx%s" % (x["hwnd"], x["title"], x["size"][0], x["size"][1])
                      for x in wins))

    if head in ("shot", "save"):
        """把当前截图存成 PNG（每个后端各一张），供人工/视觉检查几何是否对得上。"""
        targets = bridge.targets()
        if rest:
            t = pick(rest)
            targets = [t] if t else []
        outdir = os.path.join(bridge.cfg.get("mediaDir") or HERE, "shots")
        os.makedirs(outdir, exist_ok=True)
        out = []
        for t in targets:
            v = bridge.vision_for(t["hwnd"])
            bridge.v = v
            out.append("  %s（%s）hwnd=%s rect=%s" % (t["name"], t["kind"], t["hwnd"], v.rect))
            for backend in ("wgc", "printwindow"):
                try:
                    img = v.capture(backend=backend)
                except Exception as exc:  # noqa: BLE001
                    out.append("    %-12s 失败: %s" % (backend, exc))
                    continue
                p = os.path.join(outdir, "shot-%s-%s.png" % (safe_name(t["name"]), backend))
                img.save(p)
                out.append("    %-12s %sx%s 均值=%.1f → %s" % (backend, img.size[0], img.size[1], arr_mean(img), p))
        return "\n".join(out) or "（没有匹配的聊天窗）"

    if head in ("fg", "foreground"):
        import win32gui
        fg = win32gui.GetForegroundWindow()
        return "前台窗口 hwnd=%s title=%r（桥的目标 hwnd=%s）" % (
            fg, win32gui.GetWindowText(fg), bridge.v.hwnd)

    if head in ("activate", "focus"):
        import win32gui
        t = pick(rest)
        if t:
            bridge.v = bridge.vision_for(t["hwnd"])
        prev = bridge.v.activate()
        time.sleep(0.6)
        fg = win32gui.GetForegroundWindow()
        return "activate 返回 prev=%s；现在前台 hwnd=%s title=%r（目标 hwnd=%s）" % (
            prev, fg, win32gui.GetWindowText(fg), bridge.v.hwnd)

    if head in ("reload", "restart"):
        # 需要改代码后热加载时用：往 wxbridge-cmd.txt 写一行 reload 即可，不用麻烦用户。
        print("[cmd] 收到 reload：本进程退出，等守护进程用新代码重启")
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)

    return "未知命令: %r（可用: selfcheck / windows / list / open / read / send / meme / shot / pasteonly / fg / activate / reload）" % line


def poll_commands(bridge):
    """每轮检查一次 F:\\node\\scratch\\wxbridge-cmd.txt：

    文件在 → 逐行执行 → 结果追加进 wxbridge-cmd.out → 删掉命令文件。
    支持的命令：selfcheck / windows / read [名字] / send <名字>: <正文> / meme <名字>: <描述>
    """
    if not os.path.exists(CMD_PATH):
        return
    try:
        raw = open(CMD_PATH, encoding="utf-8", errors="replace").read()
        os.remove(CMD_PATH)
    except OSError:
        return
    lines, n = [], 0
    for line in [l.strip() for l in raw.splitlines()
                 if l.strip() and not l.strip().startswith("#")]:
        n += 1
        lines.append("%s > %s" % (time.strftime("%H:%M:%S"), line))
        try:
            res = run_command(bridge, line)
        except Exception as exc:  # noqa: BLE001
            res = "! %s: %s" % (type(exc).__name__, exc)
        lines.append(res if isinstance(res, str) else repr(res))
    try:
        with open(CMD_OUT, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print("! 写 %s 失败: %s" % (CMD_OUT, exc))
    print("[cmd] 执行了 %d 条命令（结果写 %s）" % (n, CMD_OUT))


def bridge_pids(exclude_supervisor=True):
    """列出正在跑的 wxbridge.py 进程（含自己）。

    **不用 WMI**：本机 WMI 返回的进程表经常不全，而且在常驻进程里有挂死的风险
    （实测 supervisor 起来后立刻消失、日志 0 字节）。只看 PID 文件 + OpenProcess 探活。
    """
    out = []
    if os.path.exists(PID_PATH):
        try:
            old = int(open(PID_PATH, encoding="utf-8").read().strip())
            if old != os.getpid() and pid_alive(old):
                out.append(old)
        except Exception:  # noqa: BLE001
            pass
    return out


def looks_like_group(title):
    """群聊标题的常见形状：`群名(78)` —— 带成员数的括号（微信给每个群都加）。

    只认这个形状，避免把「文件传输助手」「微信团队」这类当成群。
    """
    t = (title or "").strip()
    return bool(re.search(r"[（(]\d+[）)]\s*$", t))


def probe(bridge):
    """抓图能力自检：能不能在「窗口被挡在后面 / 不在屏幕上」时读到内容。

    对应用户的要求（m02102）：别老是要窗口显示在屏幕上。
    逐个后端试 WGC（Windows.Graphics.Capture，抓窗口自己的合成表面，被遮挡也能拿到）
    与 PrintWindow（被完全遮挡时全黑），再和真实屏幕（dxcam 抓该区域）对比。
    """
    import win32gui as _wg

    print("== 抓图自检 ==")
    print("本进程完整性 =", W.my_integrity(), "（4096=低完整性沙箱，8192=正常）")
    print("WGC 可用 =", W.wgc_available(), (W.wgc_error() or "").strip()[:200])
    print("配置 capture =", bridge.cfg.get("capture", "auto"), " autoRaise =", bridge.cfg.get("autoRaise"))
    try:
        tg = bridge.targets()
    except Exception as exc:  # noqa: BLE001
        print("! 找不到小号窗口:", type(exc).__name__, exc)
        return 1
    if not tg:
        print("! 没有匹配白名单/群聊的窗口（小号没开或联系人不对）")
        return 1
    import numpy as np
    for t in tg:
        v = bridge.vision_for(t["hwnd"])
        l, tp, r, b = v.rect
        print("\n--- %s（%s）hwnd=%s rect=%s 尺寸=%sx%s 可见=%s 最小化=%s"
              % (t["name"], t["kind"], t["hwnd"], v.rect, r - l, b - tp,
                 bool(_wg.IsWindowVisible(t["hwnd"])), bool(_wg.IsIconic(t["hwnd"]))))
        for be in ("wgc", "printwindow"):
            try:
                img = v.capture(backend=be)
            except Exception as exc:  # noqa: BLE001
                print("    %-11s 失败: %s %s" % (be, type(exc).__name__, exc))
                continue
            if img is None:
                print("    %-11s 失败: 返回空" % be)
                continue
            a = np.asarray(img.convert("RGB"), dtype=np.uint8)
            print("    %-11s %sx%s 均值=%.1f 全黑=%s"
                  % (be, img.size[0], img.size[1], float(a.mean()), W.is_blank(img)))
        try:
            import dxcam
            cam = dxcam.create(output_idx=0, output_color="RGB")
            f = None
            for _ in range(10):
                f = cam.grab(region=v.rect)
                if f is not None:
                    break
                time.sleep(0.15)
            if f is not None:
                print("    屏幕上该区域 均值=%.1f（和哪个后端接近，就说明那个后端抓的是当前屏幕内容）"
                      % float(np.asarray(f).mean()))
            else:
                print("    屏幕上该区域：抓不到")
        except Exception as exc:  # noqa: BLE001
            print("    dxcam 不可用: %s" % exc)
    print("\n结论：WGC 正常且非全黑 → 窗口被挡在后面也能读（满足「不用显示在屏幕上」）；"
          "只有 PrintWindow 能读 → 窗口必须在屏幕上可见。")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load_json(CONFIG_PATH, {"whitelist": [], "pollSeconds": 3, "rateLimitPerMinute": 8})
    bridge = Bridge(cfg)

    if mode == "--list":
        rows, _ = bridge.rows()
        for i, r in enumerate(rows):
            print("%2d  y=%-4d %s" % (i, r["y_name"], r["name"]))
        return 0

    if mode == "--calibrate":
        bridge.open_row(0)
        title, img, px, ty, w = bridge.current_title()
        print("标题:", title)
        bridge.send_text("桥自测 %s" % time.strftime("%H:%M:%S"))
        title, img, px, ty, w = bridge.current_title()
        print("=== 消息区 ===")
        for m in bridge.messages(img, px, ty, w):
            print("  y=%-4d x0=%-4d cx=%-4d side=%-4s %s" % (m["y"], m["x0"], m["cx"], m["side"], m["text"]))
        return 0

    if mode == "--probe":
        return probe(bridge)

    if mode in ("--meme", "--img"):
        # 自测用：把一张表情/图片发到当前聊天窗（会短暂抢前台）
        arg = sys.argv[2] if len(sys.argv) > 2 else ""
        if not arg:
            print("用法: python wxbridge.py --meme 无语   /   --img <图片路径>")
            return 2
        path = arg if mode == "--img" else meme_pick(arg)
        if not path:
            print("没找到图（描述: %r）" % arg)
            return 1
        print("发送:", path)
        bridge.send_image(path)
        print("已发送")
        return 0

    # 单实例保护：两个桥同时轮询会互相抢剪贴板/焦点，还会重复回消息
    mine = os.getpid()
    others = [p for p in bridge_pids() if p != mine]
    if others:
        print("已有桥在跑（pid %s），本进程退出。跑 --list/--calibrate 请先停掉它。" % others)
        return 1
    write_text(PID_PATH, str(mine))

    dry = mode != ""
    print("配置:", cfg)
    print("桥已启动 pid=%s%s" % (mine, "（dry：只看不回）" if dry else "（live：会真发消息）"))
    print("环境自检:", W.integrity_hint())
    if W.my_integrity() is not None and W.my_integrity() < 0x2000:
        print("!! 本进程在受限沙箱里：读不到微信窗口内容，桥会一直报「窗口不可读」。"
              "请用正常权限（非沙箱）启动本脚本。")
    last_line, last_print = None, 0.0
    while True:
        try:
            poll_commands(bridge)
            seen = bridge.poll_once(dry=dry)
            line = repr(seen)
            now = time.time()
            if line != last_line or now - last_print > 600:      # 只在状态变化或 10 分钟心跳时打日志
                print("[%s] %s" % (time.strftime("%H:%M:%S"), seen))
                last_line, last_print = line, now
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001
            print("! 轮询异常:", type(exc).__name__, exc)
        time.sleep(float(cfg.get("pollSeconds", 3)))



if __name__ == "__main__":
    sys.exit(main())
