# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。
@echo off
echo marker %DATE% %TIME% > F:\node\scratch\probe-via-cmd.txt
whoami /groups | findstr /C:"Mandatory Level" >> F:\node\scratch\probe-via-cmd.txt
"C:\Users\86152\AppData\Local\Programs\Python\Python313\python.exe" -u F:\node\scratch\probe-il.py
