# 微信桥 · WeChat Bridge

把一个**真实的 PC 微信**接进自己的 AI Agent：不注入、不碰微信协议、不登录任何第三方服务，
只用 **窗口截图 + 本地 OCR 读消息**，用 **PostMessage / WM_PASTE + 无焦点输入** 回消息。

> 起因：想给微信里塞一个有人格的 AI（会吐槽、会发表情包、会自己调工具），
> 但微信 4.1.15 的 PC 客户端既没有可用的自动化协议库，也**根本没有 UI 控件树**（见
> [`docs/NOTES.md`](docs/NOTES.md)）。于是干脆把它当成一个"只能看画面的程序"来驱动。

## 它长什么样

```
┌──────────────────────────┐         ┌────────────────────────────┐
│ 微信（第二个实例 = 小号） │  截图   │  wxvision.py 视觉层         │
│  主窗口：会话列表         │ ──────► │   WGC / PrintWindow 抓帧    │
│  独立窗口 / 右侧栏：聊天   │         │   RapidOCR 离线识别         │
└──────────────────────────┘         │   行切分 / 未读红点 / 气泡    │
          ▲                           └────────────┬───────────────┘
          │ PostMessage / WM_PASTE                 │ 文本
          │ （不抢前台、不动你的鼠标键盘）           ▼
┌──────────────────────────┐         ┌────────────────────────────┐
│  wxbridge.py 桥          │ ◄────── │  Agent（任意 CLI/HTTP 后端） │
│   轮询 → 判是否该回 → 发送 │  回复   │   本项目默认接 DeepSeek      │
│   白名单 / 限速 / 群里 @    │         │   Harness (dsh) headless    │
└──────────────────────────┘         └────────────────────────────┘
          ▲
          │ 45 秒没心跳就拉起来
┌──────────────────────────┐
│ wxbridge-supervisor.py   │  ← 计划任务 / 启动项
└──────────────────────────┘
```

## 能干什么

- **私聊**：白名单内的联系人，收到消息就叫醒 agent，把回复发回去（长回复自动分段）。
- **群聊**：只在**有人 @ 它**（支持改名后自动学新昵称）或按 `groupSpontaneousRate`
  掷骰子命中时插话；群里的旧消息**不补课**（接手时先登记、只回接手之后的新消息）。
- **表情包**：agent 回复里写 `[表情: 描述]`，桥从本地表情包图库里挑一张真图发出去。
- **图片/文件**：能读会话里的图片气泡（存到本地）、能给会话发表情包图片。
- **人格**：桥只负责"听懂/说话"，人格与工具调用都在 agent 侧，改动不用碰微信。

## 环境要求

- Windows 10/11（用 `Windows.Graphics.Capture` + Win32 消息）
- Python 3.10+，依赖：`pywin32`、`Pillow`、`rapidocr-onnxruntime`（离线 OCR，无需联网）
- 一个**第二个微信实例**（本仓库 `start-second-wechat.cmd` 就是用不同命令行参数起第二个实例；
  也可以拿另一台机器/另一个账号）
- 任意能被命令行调用的 agent 后端（默认接 [DeepSeek Harness](https://github.com/deepseek-ai) 的
  headless CLI，配置里换成你自己的命令即可）

```bat
pip install pywin32 Pillow rapidocr-onnxruntime
python wxbridge.py --list          :: 只列会话（不点、不发），先确认能看见窗口
python wxbridge.py --dry           :: 只看不回（先跑这个确认能读到消息）
python wxbridge.py                 :: 正式跑
start-bridge.cmd                   :: 后台跑 + 守护进程（推荐）
```

> 桥只负责"听懂/说话"，调用哪个 agent 在 `wxbridge.py` 的 `DshAgent` 类里（默认调
> DeepSeek Harness 的 headless CLI，解析它的 NDJSON 输出；换成别的后端只要改这一个类）。

## 配置 `wxbridge-config.json`

见 [`config.example.json`](config.example.json)：

| 键 | 说明 |
|---|---|
| `whitelist` | **只回**名单里的私聊；名单外一律不理 |
| `groups` / `autoGroups` | 允许参与的群；`autoGroups: true` = 被拉进新群也生效 |
| `groupSpontaneousRate` | 群里没人 @ 时自发插话的概率（默认 0.05，同一段对话只掷一次） |
| `selfNickname` / `mentionKeywords` | 小号在群里的昵称，用来识别 @（改名后会自动学新昵称） |
| `rateLimitPerMinute` | 每分钟最多发几条 |
| `capture` | `auto` / `wgc` / `printwindow`：抓图后端，WGC 在被遮挡时也能读 |
| `autoRaise` | 允许把窗口提到前台（默认 `false`：宁可这条不发，也不打断你） |
| `mediaDir` | 收发的图片存哪儿 |
| `agentPermission` / `agentWorkspace` | 传给 agent 后端的权限与工作目录 |

## 命令通道（调试后门）

往 `wxbridge-cmd.txt` 写一行，桥在下一轮（≤3 秒）执行，结果追加到 `wxbridge-cmd.out`：

```
selfcheck                 环境自检（完整性级别、抓图后端、小号 pid）
windows                   列出微信窗口（hwnd/标题/尺寸）
read Alice                读某个会话的消息（带 out/in/side 标记）
send Alice: 正文           发一条文字（支持 [表情: 描述]）
meme Alice: 害羞           直接发一张表情包
shot Alice                两个后端各存一张截图（看它到底看到什么）
sendtest Alice: 文本       只走无焦点路线、不兜底（诊断重复发送）
imgtest Alice: 描述        只走无焦点发图（诊断图片路线）
reload                    本进程退出，等守护进程用新代码重启
```

## 已知限制（重要，都是踩过的坑）

- **必须有一个窗口能渲染**：窗口被完全遮挡/最小化时，`PrintWindow` 会返回**全黑**
  （WGC 后端能绕过这一点，但个别机器上 WGC 抓不到该窗口，会自动降级）。
- **靠 OCR 读消息**：字体、缩放、深色模式、气泡遮挡都会影响识别率；本项目用"行首切分 +
  两行聚类 + 指纹归一"把误读压到可接受范围，但它不是协议级读取，别拿它做关键业务。
- **发送靠模拟输入**：微信 4.x 是 Qt Quick，**只在窗口有焦点时才处理键盘消息**，所以
  无焦点发送要先用 `AttachThreadInput + SetFocus`（只给焦点、**不动 Z 序**，窗口不会跳到最前面）；
  少数情况下仍会退化成真键鼠（此时会短暂抢前台，且只在窗口本来就是前台时才做）。
- **不碰协议、不注入**，因此也**没有**任何微信官方的支持或稳定性保证；账号风控责任自负。
- 只在一台机器 + 微信 4.1.15 上验证过；换版本可能需要重新标定阈值（都会写在 `docs/NOTES.md`）。

## 目录

```
wxbridge.py              桥主体：轮询、判定、回复、命令通道
wxvision.py              视觉层：抓帧、OCR、行切分、未读检测、点击/输入原语
wxbridge-supervisor.py   守护：心跳过期就拉起桥（单实例、低完整性环境直接退出）
test-offline.py          35+ 条离线单测（假视觉层，不碰微信）
test-reply-logic.py      回复判定逻辑单测
test-wgc.py              WGC 抓帧单测
tools/                   起第二个微信、重启小号、完整性诊断
probes/                  当初用来摸清微信 4.1.15 的几个探针（HWND 枚举 / PrintWindow / PostMessage）
docs/NOTES.md            技术笔记：为什么 UIA 走不通、坐标系怎么换算、重复发送怎么修
```

## 许可

MIT，见 [`LICENSE`](LICENSE)。
