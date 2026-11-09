#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bonsai 本地启动器入口。"""
import json
import socket
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer

from core import BASE, BUILD, LAUNCHER_PORT, _DIRECT_OPENER, kill_by_port
from models import host
from server import Handler


def _running_build():
    """探测 18300 上已有进程的构建号；连不上返回 None"""
    try:
        with _DIRECT_OPENER.open(f"http://127.0.0.1:{LAUNCHER_PORT}/api/status", timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("build")
    except Exception:
        return None


def main():
    # 已在运行？若跑的是旧构建，直接接管（否则用户会一直看到旧页面）
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LAUNCHER_PORT))
    except OSError:
        s.close()
        old = _running_build()
        if old and old != BUILD:
            print(f"[launcher] 检测到旧启动器（构建 {old}），正在接管并重启为 {BUILD}")
            kill_by_port(LAUNCHER_PORT)
            time.sleep(1.5)
        else:
            print(f"[launcher] 已有一个启动器在运行，直接打开面板 http://127.0.0.1:{LAUNCHER_PORT}")
            webbrowser.open(f"http://127.0.0.1:{LAUNCHER_PORT}")
            return
    else:
        s.close()

    if not all(ord(c) < 128 for c in BASE):
        print("[launcher] [WARN] 路径含非 ASCII 字符，引擎可能报'系统找不到指定的路径。'")

    httpd = ThreadingHTTPServer(("127.0.0.1", LAUNCHER_PORT), Handler)
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{LAUNCHER_PORT}")).start()
    print(f"[launcher] 面板已打开: http://127.0.0.1:{LAUNCHER_PORT}  (Ctrl+C 退出，退出时自动停止引擎)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            host().stop_all()
        except Exception:
            pass


if __name__ == "__main__":
    main()
