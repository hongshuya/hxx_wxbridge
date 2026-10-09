# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。

HERE = os.path.dirname(os.path.abspath(__file__))
@echo off
rem ===== 微信<->DSH 桥的启动器（必须由你双击/Win+R 运行，跑在 DSH 沙箱之外）=====
rem 真正的逻辑在同目录的 start-wxbridge.ps1（UTF-8 带 BOM，pwsh 7 与 Windows PowerShell 5.1 都能正确读）。
rem 本文件故意只用 ASCII：cmd.exe 按控制台代码页(936)读 .cmd，写中文路径会变成乱码导致找不到文件。
chcp 65001 >nul
set PS=powershell
where pwsh >nul 2>nul
if not errorlevel 1 set PS=pwsh
"%PS%" -NoProfile -ExecutionPolicy Bypass -File os.path.join(HERE, r"start-wxbridge.ps1") %*
