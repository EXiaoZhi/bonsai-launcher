#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bonsai 启动器核心：常量、共享状态、系统信息与通用工具函数。"""

import ctypes
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 打包成 exe 时以 exe 所在目录为基准（onefile 会解包到临时目录，__file__ 不可信）
BASE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
ENGINE_DIR = os.path.join(BASE, "engine")
ENGINE_EXE = os.path.join(ENGINE_DIR, "llama-kvmem-server.exe")
LAUNCHER_PORT = 18300
BUILD = "2026-11-07 16:20"      # 唯一来源：页面上的"构建"标识从 /api/status 取，改这里就行
DEFAULT_ENGINE_PORT = 18200

STATE = {
    # 只剩启动器自己的日志缓冲；模型进程与参数由 engine_host.EngineHost 管理
    "lines": deque(maxlen=3000),
    "log_seq": 0,
    "lock": threading.Lock(),
}

# 直连 opener：不走任何代理（环境变量里的 HTTP_PROXY 会把本地请求拐去代理导致失败）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

IS_WIN = os.name == "nt"
# 所有辅助子进程都必须隐藏窗口：否则 pythonw（双击启动）下每个子进程都会弹出控制台闪烁。
# DETACHED_PROCESS 让子进程脱离父进程控制台（即使命令是 cmd/npx 也不继承控制台窗口）；
# CREATE_NEW_PROCESS_GROUP 避免 Ctrl+C 从父进程传导给子进程。二者叠加才能彻底压掉 cmd 弹窗。
SUB_FLAGS = (subprocess.CREATE_NO_WINDOW
             | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.DETACHED_PROCESS) if IS_WIN else 0

_CACHE = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)

import re

MODELS_CFG = os.path.join(BASE, "models-config.json")
MCP_FILE = os.path.join(BASE, "mcp-servers.json")      # MCP 服务器配置（全局，所有模型共用）
CHAT_DIR = os.path.join(BASE, "chat-history")          # 对话历史
WS_CONFIG = os.path.join(BASE, "workspace.json")       # 文件空间目录配置
PERMS_FILE = os.path.join(BASE, "tool-perms.json")     # 工具授权（总是允许列表）
os.makedirs(CHAT_DIR, exist_ok=True)

_BAD_NAME_CHARS = set('\\/:*?"<>|')                  # 服务器名不能带路径/通配字符

# 网关状态（内嵌聊天后端）
GW_PORT = 18401
GW_STATE = {"srv": None, "gw": None, "err": None, "cookie": "bonsai_gw", "lock": threading.Lock()}

# 权限确认挂起: pid -> [Event, {"approved":bool}]
_pending_lock = threading.Lock()
PENDING = {}
_pending_seq = [0]
_SESSION_ALLOWED = {}   # 对话级授权: conv_id -> set(tool_id)

# MCP 会话池
_MCP_SESSIONS = {}
_MCP_LOCK = threading.Lock()


# ---------------------------------------------------------------- 系统信息
def vram_info():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=SUB_FLAGS)
        first = out.stdout.strip().splitlines()[0]
        used, total = [int(x.strip()) for x in first.split(",")]
        return {"used": used, "total": total}
    except Exception:
        return {"used": 0, "total": 0}


def ram_info():
    """Windows: GlobalMemoryStatusEx -> MB"""
    try:
        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MemStatus()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return {"total": st.ullTotalPhys // 1048576, "avail": st.ullAvailPhys // 1048576}
    except Exception:
        pass
    return {"total": 0, "avail": 0}


def first_engine_port():
    """当前有实例就绪时取它的引擎端口；没有就退回默认值（legacy 接口用）。"""
    try:
        from models import host   # 延迟 import 打破 core<->models 循环
        for i in host().status():
            if i.get("ready"):
                return int(i["engine_port"])
    except Exception:
        pass
    return DEFAULT_ENGINE_PORT


def engine_ready(port):
    if not port or not tcp_open(port):
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with _DIRECT_OPENER.open(req, timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def tcp_open(port, timeout=0.35):
    """本机端口是否有人在监听（毫秒级）。

    坑（2026-11-02 实测）：这台机器连没人监听的端口不会立刻回 RST（防火墙静默丢弃），
    urllib 的 connect 会一直等到 timeout——面板每 2 秒轮询一次 /api/status，
    每次卡 2 秒 + PowerShell 查占用再卡 2~5 秒，界面就长期停在「检查中…」。
    这里用非阻塞 connect + select 判活，没人监听最多等 0.35 秒。
    """
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setblocking(False)
    try:
        err = s.connect_ex(("127.0.0.1", int(port)))
        if err == 0:
            return True
        # 10035/WSAEWOULDBLOCK、EINPROGRESS 等：握手还在进行
        if err not in (10035, 10036, 115, 36):
            return False
        _, w, _ = select.select([], [s], [], timeout)
        if not w:
            return False
        return s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0
    except Exception:
        return False
    finally:
        s.close()


def port_owner_pid(port):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue)"
             ".OwningProcess | Sort-Object -Unique"],
            capture_output=True, text=True, timeout=10, creationflags=SUB_FLAGS,
            encoding="utf-8", errors="replace")
        pids = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return pids
    except Exception:
        return []


def kill_by_port(port):
    for pid in port_owner_pid(port):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, creationflags=SUB_FLAGS,
                       encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 参数构建
# 思考强度档位：模板支持的 reasoning_effort 只有 low / medium / xhigh（见 GGUF tokenizer.chat_template），
# 因此这里做成 4 档：关闭 / 轻度(low) / 标准(medium) / 深度(xhigh)，再叠加思考 token 预算。
# 每档 = (是否开启思考, reasoning-effort, 默认思考预算 tokens；-1 表示不限)
THINK_LEVELS = {
    "off":    (False, None,     0),
    "low":    (True,  "low",    1024),
    "medium": (True,  "medium", 3072),
    "high":   (True,  "xhigh",  -1),
}
THINK_ALIASES = {"0": "off", "false": "off", "none": "off", "no": "off",
                 "1": "medium", "true": "medium", "on": "medium", "mid": "medium", "standard": "medium",
                 "xhigh": "high", "max": "high", "deep": "high", "lite": "low", "light": "low"}


# 显式导出清单：让 from core import * 也能带出下划线开头的共享符号
__all__ = [
    # 常量 / 路径
    "BASE", "ENGINE_DIR", "ENGINE_EXE", "LAUNCHER_PORT", "BUILD", "DEFAULT_ENGINE_PORT",
    "MODELS_CFG", "MCP_FILE", "CHAT_DIR", "WS_CONFIG", "PERMS_FILE",
    # 共享状态
    "STATE", "IS_WIN", "SUB_FLAGS", "GW_PORT", "GW_STATE",
    "PENDING", "_pending_lock", "_pending_seq", "_SESSION_ALLOWED",
    "_MCP_SESSIONS", "_MCP_LOCK", "_BAD_NAME_CHARS", "_CACHE", "_DIRECT_OPENER",
    # 工具函数
    "_cached", "vram_info", "ram_info", "first_engine_port", "engine_ready",
    "tcp_open", "port_owner_pid", "kill_by_port",
    # 参数
    "THINK_LEVELS", "THINK_ALIASES",
]
