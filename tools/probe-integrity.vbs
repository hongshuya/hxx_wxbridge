# 本文件由 pack-github.py 从工作副本导出；路径已改为可配置，私人信息已脱敏。

HERE = os.path.dirname(os.path.abspath(__file__))
' 用 explorer（Medium 完整性）拉起探针，绕开低完整性沙箱
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = HERE
sh.Run """C:\Users\86152\AppData\Local\Programs\Python\Python313\python.exe"" -u "os.path.join(HERE, r"probe-il.py")"", 0, False
