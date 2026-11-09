@echo off
REM Bonsai 本地启动器 - 双击运行，自动打开浏览器控制面板
REM 自动查找 python：优先 pythonw（无窗口），其次 python
set "PY="
where pythonw >nul 2>nul && set "PY=pythonw"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [错误] 未找到 Python。请先安装 Python 3.8+ 并加入 PATH。
  pause
  exit /b 1
)

REM 用 pythonw 无窗口启动；若失败则用 python 前台启动以便看到报错
start "" "%PY%" "%~dp0main.py"
if errorlevel 1 (
  python "%~dp0main.py"
)
