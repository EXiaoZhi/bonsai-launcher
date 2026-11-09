#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 服务器（JSON-RPC over stdio，标准库实现）。"""
import json
import os
import subprocess
import threading
import time

from core import *

class MCPSession:
    """一个 MCP stdio 服务器的持久会话"""

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.proc = None
        self.req_lock = threading.Lock()
        self.pending = {}      # rid -> [Event, box]
        self.rid = 0
        self.tools_cache = None
        self.error = None
        self._last_spawn_fail = 0.0   # 上次启动失败的时间戳，用于失败冷却

    # 启动失败后的冷却期（秒）：这段时间内 request() 不再重复拉起进程，
    # 避免「命令写错 / npx 拉不到包」时模型每重试一次就拉起一个 cmd 窗口。
    SPAWN_COOLDOWN = 15.0

    def _spawn(self):
        cmd = [self.cfg.get("command", "")] + [str(a) for a in self.cfg.get("args", [])]
        if not cmd[0]:
            raise RuntimeError("命令为空")
        now = time.time()
        if self._last_spawn_fail and (now - self._last_spawn_fail) < self.SPAWN_COOLDOWN:
            raise RuntimeError("MCP 启动失败冷却中（%.0fs 后重试）" %
                               (self.SPAWN_COOLDOWN - (now - self._last_spawn_fail)))
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.cfg.get("env", {}).items()})
        # DETACHED_PROCESS 已在 SUB_FLAGS 里：cmd /c npx 这类写法也不会继承控制台窗口
        self.proc = subprocess.Popen(cmd, cwd=BASE, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=1,
                                     creationflags=SUB_FLAGS)
        threading.Thread(target=self._reader, daemon=True).start()
        # 给进程 200ms 确认它没有立刻退出（命令写错/包不存在时会秒退）
        time.sleep(0.2)
        if self.proc.poll() is not None:
            self._last_spawn_fail = time.time()
            self.proc = None
            raise RuntimeError("MCP 进程启动即退出（命令或参数可能写错）")

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method, params, timeout=90):
        if not self.proc or self.proc.poll() is not None:
            self._spawn()
            try:
                init = self.request("initialize", {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "bonsai-launcher", "version": "1.0"}}, timeout=30)
            except Exception:
                self._last_spawn_fail = time.time()
                raise
            if init is None:
                self._last_spawn_fail = time.time()
                raise RuntimeError("MCP initialize 无响应")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        with self.req_lock:
            self.rid += 1
            rid = self.rid
            ev = threading.Event()
            box = {}
            self.pending[rid] = [ev, box]
            try:
                self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            except Exception as e:
                self.pending.pop(rid, None)
                raise RuntimeError(f"MCP 写入失败: {e}")
        if not ev.wait(timeout):
            self.pending.pop(rid, None)
            raise RuntimeError(f"MCP {method} 超时")
        self.pending.pop(rid, None)
        if "error" in box:
            raise RuntimeError(str(box["error"].get("message", box["error"])))
        return box.get("result")

    def _reader(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if msg.get("id") in self.pending:
                    ev, box = self.pending[msg["id"]]
                    box.update(msg)
                    ev.set()
        except Exception:
            pass
        finally:
            for ev, box in list(self.pending.values()):
                box.setdefault("error", {"message": "MCP 进程已退出"})
                ev.set()

    def tools(self):
        if self.tools_cache is None:
            res = self.request("tools/list", {})
            tools = (res or {}).get("tools", [])
            self.tools_cache = [{"name": t.get("name", "?"),
                                 "desc": t.get("description", "") or "",
                                 "schema": t.get("inputSchema", {})} for t in tools]
        return self.tools_cache

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None
        self.tools_cache = None
        self._last_spawn_fail = 0.0   # 手动关闭后重置冷却，允许下次正常重启


def mcp_load_cfg():
    try:
        with open(MCP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def mcp_save_cfg(d):
    tmp = MCP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MCP_FILE)


def mcp_list_cfg():
    """MCP 服务器清单（启动页用）：只报「已配置 + 会话是否还活着」，不主动拉起进程。"""
    out = []
    for name, cfg in mcp_load_cfg().items():
        if not isinstance(cfg, dict):
            continue
        with _MCP_LOCK:
            s = _MCP_SESSIONS.get(name)
        alive = bool(s and getattr(s, "proc", None) and s.proc.poll() is None)
        tools = []
        if alive:
            try:
                tools = [t.get("name") for t in (s.tools() or [])]
            except Exception:
                tools = []
        args = cfg.get("args") or []
        out.append({
            "name": name,
            "command": str(cfg.get("command") or ""),
            "args": args if isinstance(args, list) else [str(args)],
            "enabled": bool(cfg.get("enabled", True)),
            "connected": alive,
            "tools": tools,
        })
    return out


def mcp_session(name, cfg):
    with _MCP_LOCK:
        s = _MCP_SESSIONS.get(name)
        if s and s.cfg != cfg:
            s.close()
            s = None
        if not s:
            s = MCPSession(name, cfg)
            _MCP_SESSIONS[name] = s
        return s


def mcp_shutdown_all():
    with _MCP_LOCK:
        for s in _MCP_SESSIONS.values():
            s.close()
        _MCP_SESSIONS.clear()