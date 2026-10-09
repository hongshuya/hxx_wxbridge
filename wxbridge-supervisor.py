# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
"""微信桥守护进程：保证「恰好一个 wxbridge.py 在跑」。

- 每 CHECK_SECONDS 秒检查一次；桥不在（或进程死了）就拉起来。
- 小号微信没开时只记一行提示，不反复刷屏。
- 单实例：只看 wxbridge-supervisor.pid + OpenProcess 探活。

诊断（本机 WMI 不可靠、沙箱里也看不到别的进程，所以一切都落文件）：
  wxbridge-supervisor.out    逐行日志（stdout + stderr + 未捕获异常的 traceback）
  wxbridge-supervisor.state  每轮一行状态：「时间 小号pid 桥pid 动作」

用法：
  python -u wxbridge-supervisor.py            # 常驻（开机自启跑的就是它）
  python -u wxbridge-supervisor.py --once     # 只检查一次（排错用）
"""
import json
import os
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "wxbridge-supervisor.out")
STATE_PATH = os.path.join(HERE, "wxbridge-supervisor.state")
SUP_PID = os.path.join(HERE, "wxbridge-supervisor.pid")
BRIDGE = os.path.join(HERE, "wxbridge.py")
BRIDGE_LOG = os.path.join(HERE, "wxbridge.log")
DIAG_FLAG = os.path.join(HERE, "wxbridge-diag.flag")   # 放这个文件 → 下一轮打一次详细诊断
DIAG_FLAG2 = os.path.join(HERE, "wxbridge-diag2.flag")  # 同上；新名字专给「带 OCR 的诊断」用
CMD_JSON = os.path.join(HERE, "wxbridge-cmd.json")      # 放这个文件 → 下一轮以本进程权限跑一条命令
CHECK_SECONDS = 45
SUP_PID_FRESH_SECONDS = 180.0               # pid 文件超过这么久没被摸过 = 残留
DETACHED = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

# ---------- 先接日志，再做任何可能失败的事 ----------
# pythonw.exe 下没有 stdout/stderr，而且这里必须「行缓冲」：块缓冲会把崩溃前的输出全丢掉
# （已经踩过一次：.out 0 字节，完全不知道它为什么死）。
_log = open(OUT_PATH, "a", encoding="utf-8", buffering=1, errors="replace")
sys.stdout = sys.stderr = _log


def log(msg):
    stamp = time.strftime("%H:%M:%S")
    try:
        _log.write("[%s] %s\n" % (stamp, msg))
    except Exception:  # noqa: BLE001
        pass


def _excepthook(etype, value, tb):
    log("!! 未捕获异常:\n" + "".join(traceback.format_exception(etype, value, tb)))


sys.excepthook = _excepthook

sys.path.insert(0, HERE)

import wxbridge as B          # noqa: E402   （复用 pid 探活/配置读取）
import wxvision as W          # noqa: E402   （只为了找小号微信）


def write_my_pid():
    try:
        with open(SUP_PID, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception as exc:  # noqa: BLE001
        log("写 pid 文件失败: %s" % exc)


def touch_my_pid():
    """刷新 pid 文件的修改时间（心跳）。

    为什么要心跳：Windows 会复用 pid，`pid_alive(旧 pid)` 可能是 True（实测
    2026-10-09 18:35 那次：pid 文件里 27488 早死了，探活却返回 True，于是新起的
    supervisor 直接「已有 supervisor 在跑」退出，桥就永远起不来）。
    现在把「文件时间戳」也算进存活判据：活的 supervisor 每 45 秒摸一次自己的
    pid 文件，超过 SUP_PID_FRESH_SECONDS 没动过就当作残留文件。
    """
    try:
        os.utime(SUP_PID, None)
    except OSError:
        write_my_pid()


def pid_file_fresh():
    try:
        return (time.time() - os.path.getmtime(SUP_PID)) < SUP_PID_FRESH_SECONDS
    except OSError:
        return False


def my_peers():
    """其它 supervisor 进程（不含自己）。

    **不用 WMI**：本机 WMI 返回的进程表经常不全，而且在守护进程里 WMI 调用有挂死的风险
    （实测 supervisor 起来后立刻消失、日志 0 字节）。只看 pid 文件 + OpenProcess 探活
    + 文件时间戳（防 pid 复用误判）。
    """
    me = os.getpid()
    peers = []
    if os.path.exists(SUP_PID):
        try:
            pid = int(open(SUP_PID, encoding="utf-8").read().strip())
            if pid != me and B.pid_alive(pid) and pid_file_fresh():
                peers.append(pid)
            elif pid != me and B.pid_alive(pid):
                log("pid 文件 %s 里的 %s 还活着但心跳已过期（%s）→ 当作残留，接管"
                    % (os.path.basename(SUP_PID), pid,
                       time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(SUP_PID)))))
        except Exception:  # noqa: BLE001
            pass
    return peers


def start_bridge():
    # 用 pythonw 起（无控制台窗口，不打扰用户）；父进程若是 python.exe，
    # sys.executable 会是 python.exe，会弹一个黑框，所以在同目录找 pythonw。
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    fh = open(BRIDGE_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen([exe, "-u", BRIDGE], stdout=fh, stderr=fh,
                            stdin=subprocess.DEVNULL, creationflags=DETACHED, close_fds=True)
    return proc.pid


def write_state(line):
    try:
        with open(STATE_PATH, "a", encoding="utf-8", buffering=1) as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except Exception:  # noqa: BLE001
        pass


def log_diag(hints):
    """把「为什么找不到小号」需要的全部事实一次打出来（放 wxbridge-diag.flag 触发）。

    只读取、不写任何状态：诊断本身不能影响桥的行为。
    """
    log("---- 诊断开始 ----")
    try:
        log("完整性: %s" % W.my_integrity())
    except Exception as exc:  # noqa: BLE001
        log("完整性取不到: %s: %s" % (type(exc).__name__, exc))
    try:
        wins = [w for w in W.all_windows(120) if w["cls"] == W.WECHAT_CLS]
        log("微信类窗口 %d 个（title/pid/rect）：" % len(wins))
        for w in wins:
            log("  pid=%-6s title=%r rect=%s" % (w["pid"], w["title"], w.get("rect")))
    except Exception as exc:  # noqa: BLE001
        log("窗口枚举失败: %s: %s" % (type(exc).__name__, exc))
    try:
        procs = W._weixin_procs()
        log("WMI 看到的 Weixin.exe %d 个：" % len(procs))
        for p in procs:
            log("  pid=%-6s cmd=%r" % (p.get("ProcessId"), p.get("CommandLine")))
    except Exception as exc:  # noqa: BLE001
        log("WMI 取进程失败: %s: %s" % (type(exc).__name__, exc))
    try:
        log("wx-small.pid 记录: %s" % (open(W.SMALL_PID_PATH, encoding="utf-8").read().strip()
                                       if os.path.exists(W.SMALL_PID_PATH) else "(无)"))
    except Exception as exc:  # noqa: BLE001
        log("读 wx-small.pid 失败: %s" % exc)
    try:
        log("find_small_pid -> %s" % W.find_small_pid(hints))
    except Exception as exc:  # noqa: BLE001
        log("find_small_pid 失败: %s: %s" % (type(exc).__name__, exc))
    # 截图 + OCR：看清这台机上现在的那个微信实例到底是哪个号 ——
    # 小号的会话列表里有「Alice」（主号），主号的列表里有「hong」（小号）。
    try:
        wins = [w for w in W.all_windows(150)
                if w.get("cls") == W.WECHAT_CLS and (w.get("size") or (0, 0))[0] >= 300]
        for w in wins[:2]:
            v = W.WxVision(pid=w["pid"], hwnd=w["hwnd"])
            shot = os.path.join(HERE, "diag-wx-%s.png" % w["pid"])
            img = v.capture(save=shot)
            log("截图 pid=%s title=%r size=%s blank=%s -> %s"
                % (w["pid"], w.get("title"), img.size, W.is_blank(img), shot))
            boxes = W.ocr_boxes(img, x0=0, x1=int(img.size[0] * 0.45), y0=60)
            log("  列表区 OCR %d 行: %s" % (len(boxes), " | ".join(b["text"] for b in boxes[:16])))
            boxes2 = W.ocr_boxes(img, x0=int(img.size[0] * 0.45), y0=60)
            log("  右侧 OCR %d 行: %s" % (len(boxes2), " | ".join(b["text"] for b in boxes2[:8])))
    except Exception as exc:  # noqa: BLE001
        log("截图/OCR 失败: %s: %s" % (type(exc).__name__, exc))
    log("---- 诊断结束 ----")


def run_remote_cmd():
    """按 wxbridge-cmd.json 的内容跑一条一次性命令，输出落盘。

    为什么要这条通道：我这边的 shell 跑在低完整性沙箱里（截不了别的程序的窗口、
    进程表也是空的），一次性诊断（比如「手动双击会话行的坐标对不对」）必须在正常
    权限下做。把命令写进 wxbridge-cmd.json，supervisor 下一轮替我跑，stdout/stderr
    写到我读得到的文件里。

    文件格式：{"argv": ["wxbridge.py", "--open", "某群"], "out": "...", "timeout": 180}
    """
    try:
        with open(CMD_JSON, "r", encoding="utf-8") as fh:
            req = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log("命令文件读不了: %s: %s" % (type(exc).__name__, exc))
        return
    try:
        os.remove(CMD_JSON)
    except OSError:
        pass
    argv = [str(x) for x in (req.get("argv") or [])]
    if not argv:
        log("命令文件里没有 argv，忽略")
        return
    out_path = str(req.get("out") or os.path.join(HERE, "wxbridge-cmd.out"))
    cmd = [sys.executable] + argv
    log("执行远程命令: %s" % " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=float(req.get("timeout") or 180))
        body = "exit=%s\n%s" % (p.returncode, p.stdout.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        body = "运行失败: %s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc())
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        log("命令输出写不了 %s: %s" % (out_path, exc))
    log("远程命令结束，输出 %s" % out_path)


def main():
    once = "--once" in sys.argv
    allow_low = "--allow-low" in sys.argv
    log("=" * 60)
    log("supervisor 启动 pid=%s，cwd=%s" % (os.getpid(), os.getcwd()))
    try:
        log("环境自检: %s" % W.integrity_hint())
    except Exception as exc:  # noqa: BLE001
        log("完整性自检失败: %s" % exc)
    # 低完整性（DSH 客户端自己的 shell 子进程就是这个级别）读不到任何别的程序的窗口，
    # 它起的桥必然全黑。**必须在占坑之前就退出**：否则会写下一个「看起来活着」的
    # supervisor pid，把 High-IL 计划任务/启动项挡在外面（2026-10-09 实测踩过）。
    if not allow_low:
        try:
            il = W.my_integrity()
        except Exception:  # noqa: BLE001
            il = None
        if il is not None and il < 0x2000:
            log("本进程完整性=%s（低沙箱）→ 直接退出，不占坑。"
                "请用正常权限启动（开机自启/启动项/双击启动器）。" % il)
            return 2
    try:
        peers = my_peers()
    except Exception as exc:  # noqa: BLE001
        log("peer 检查异常（当作没有）：%s" % exc)
        peers = []
    if peers:
        log("已有 supervisor 在跑：%s，本进程退出" % peers)
        write_state("退出：已有 supervisor %s" % peers)
        return 1
    write_my_pid()
    cfg = B.load_json(B.CONFIG_PATH, {"whitelist": []})
    hints = list(cfg.get("whitelist", []))
    log("白名单（用来反推小号 pid）: %s" % hints)
    log("每 %s 秒检查一次；桥日志 %s" % (CHECK_SECONDS, BRIDGE_LOG))
    down_streak = 0
    while True:
        try:
            touch_my_pid()
            if os.path.exists(DIAG_FLAG) or os.path.exists(DIAG_FLAG2):
                for _f in (DIAG_FLAG, DIAG_FLAG2):
                    try:
                        os.remove(_f)
                    except OSError:
                        pass
                log_diag(hints)
            if os.path.exists(CMD_JSON):
                run_remote_cmd()
            try:
                wx = W.find_small_pid(hints)
            except RuntimeError:
                wx = None
            except Exception as exc:  # noqa: BLE001
                log("找小号失败: %s: %s" % (type(exc).__name__, exc))
                wx = None
            bridges = B.bridge_pids()
            if wx is None:
                down_streak += 1
                if down_streak in (1, 20):
                    log("小号微信没在跑，等它上线（桥起来也没用）")
                write_state("小号未运行 桥=%s" % (bridges or "无"))
            else:
                down_streak = 0
                if not bridges:
                    pid = start_bridge()
                    log("桥不在，已拉起 pid=%s" % pid)
                    write_state("小号=%s 拉起桥 pid=%s" % (wx, pid))
                else:
                    if len(bridges) > 1:
                        log("警告：有 %s 个桥同时在跑 %s（请手动留一个）" % (len(bridges), bridges))
                    write_state("小号=%s 桥在跑 %s" % (wx, bridges))
        except Exception as exc:  # noqa: BLE001
            log("! 检查异常: %s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
        if once:
            return 0
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        _excepthook(*sys.exc_info())
        raise
