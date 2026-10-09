# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。

HERE = os.path.dirname(os.path.abspath(__file__))
@echo off
chcp 65001 >nul
title open a second WeChat (hong / whale girl)

set "PY=C:\Users\86152\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -u os.path.join(HERE, r"wx-second.py") %*

echo.
echo (press any key to close)
pause >nul
