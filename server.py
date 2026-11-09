#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 服务与路由。"""
import json
import os
import threading
import urllib.request
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler

from core import *
from models import (gw_ensure, gw_refresh, gw_owns, host, catalog,
                    load_models_cfg, PARAM_SPECS, EH)
from workspace import (ws_list, _safe_ws_name, workspace_dir, ws_config_load,
                       ws_config_save, ws_write, ws_delete,
                       hist_load, hist_list, hist_save, hist_delete, hist_auto_title,
                       perms_load, perms_clear_always)
from mcp import (mcp_load_cfg, mcp_save_cfg, mcp_session, mcp_list_cfg)
from tools import execute_tool, collect_tools
from agent import run_agent
from webui import load_panel

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gw_proxy(self, method):
        """把请求转给内嵌聊天后端；SSE 用 read1 逐块透传，保证流式不退化。"""
        gw = gw_ensure()
        if not gw:
            self._json({"error": {"code": "chat_backend_unavailable",
                                  "message": GW_STATE["err"] or "聊天后端未就绪"}}, 503)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (GW_PORT, self.path),
                                     data=body, method=method)
        for k in ("Content-Type", "Accept", "Cookie", "Authorization"):
            v = self.headers.get(k)
            if v:
                req.add_header(k, v)
        try:
            resp = _DIRECT_OPENER.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type") or "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            self._json({"error": {"code": "chat_backend_unavailable",
                                  "message": str(e)}}, 503)
            return
        try:
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() in ("connection", "transfer-encoding", "content-length",
                                 "content-encoding"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if gw_owns("GET", self.path):
            return self._gw_proxy("GET")
        if self.path == "/" or self.path.startswith("/?"):
            try:
                boot = {"ok": True, "slots": host().store.load(), "running": host().status(),
                        "gateway_port": GW_PORT, "gpu": _cached("vram", 3, vram_info),
                        "ram": _cached("ram", 3, ram_info),
                        "model_dir": os.path.join(BASE, "model"),
                        "types": [{"id": t, "label": EH.TYPE_LABEL[t]} for t in EH.TYPES],
                        "llama_exe": host().llama_exe}
            except Exception:
                boot = {"ok": False, "slots": [], "running": []}
            body = load_panel().replace("__SL_BOOT__", json.dumps(boot, ensure_ascii=False)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 关键：禁止缓存，否则改版后浏览器仍显示旧页面
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/chat" or self.path.startswith("/chat?"):
            gw = gw_ensure()
            page = ""
            try:
                with open(os.path.join(BASE, "web", "index.html"), "r", encoding="utf-8") as f:
                    page = f.read()
            except Exception as e:
                print("[launcher] web/index.html 读取失败：", e)
            if not page:
                self.send_error(500, "web/index.html missing")
                return
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            if gw:
                # 本地单用户：进聊天页即种 HttpOnly Cookie，免手动粘贴令牌（不走 URL token）
                self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                                 % (GW_STATE["cookie"], gw.token))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/status"):
            # 只回答「启动器 + 网关」的状态；模型实例细节统一走 /api/slots。
            # （旧版这里堆了单引擎的 running/ready/vram/think_level，重构后没有意义）
            try:
                inst = host().status()
            except Exception:
                inst = []
            with STATE["lock"]:
                seq = STATE["log_seq"]
            self._json({
                "build": BUILD,
                "running": any(i["running"] for i in inst),
                "ready": any(i["ready"] for i in inst),
                "models": len(inst),
                "gpu": _cached("vram", 3, vram_info),
                "ram": ram_info(),
                "gateway_port": GW_PORT,
                "log_seq": seq,
                "ascii_path": all(ord(c) < 128 for c in BASE),
            })
        elif self.path.startswith("/api/params"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            eng = (q.get("engine", ["kvmem"])[0] or "kvmem").strip()
            eng = "llama" if eng == "llama" else "kvmem"
            if not PARAM_SPECS:
                self._json({"ok": False, "error": "param_specs.py 缺失"}, 500)
                return
            skip = {"model", "host", "port", "alias"}
            specs = [x for x in PARAM_SPECS.specs_for(eng) if x["key"] not in skip]
            self._json({"ok": True, "engine": eng,
                        "specs": specs,
                        "engine_label": EH.ENGINE_LABEL[eng],
                        "exe": host().engine_exe(eng),
                        # 预设与说明都由后端给，避免前端再抄一份容易走样；
                        # 预设只含与设备无关的项，显存/内存类一律留空
                        "preset_note": getattr(PARAM_SPECS, "PRESET_NOTE", {}).get(eng, ""),
                        "type_presets": getattr(PARAM_SPECS, "TYPE_PRESET", {}).get(eng, {}),
                        "presets": PARAM_SPECS.presets(eng) if hasattr(PARAM_SPECS, "presets") else {}})
        elif self.path == "/api/slots" or self.path.startswith("/api/slots?"):
            slots = host().store.load()
            self._json({"ok": True, "slots": slots, "running": host().status(),
                        "gateway_port": GW_PORT,
                        "gpu": _cached("vram", 3, vram_info),
                        "ram": _cached("ram", 3, ram_info),
                        "types": [{"id": t, "label": EH.TYPE_LABEL[t]} for t in EH.TYPES],
                        "engines": [{"id": k, "label": v} for k, v in EH.ENGINE_LABEL.items()],
                        "llama_exe": host().llama_exe,
                        "model_dir": os.path.join(BASE, "model"),
                        "suggest_port": EH.free_port(18200)})
        elif self.path.startswith("/api/catalog"):
            # 只列文件清单，不再带 router 开关；显存提示改成与设备无关的说法
            self._json({"models": catalog(),
                        "vram_hint": "每个实例都会各占一份权重显存；同时开几个，先看你面板顶部的显存余量"})
        elif self.path.startswith("/api/mcp/list"):
            self._json({"ok": True, "servers": mcp_list_cfg()})
        elif self.path.startswith("/api/logs"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            after = 0
            for kv in qs.split("&"):
                if kv.startswith("after="):
                    try:
                        after = int(kv.split("=", 1)[1])
                    except ValueError:
                        pass
            with STATE["lock"]:
                items = [(s, l) for s, l in STATE["lines"] if s > after]
                seq = STATE["log_seq"]
            self._json({"next": seq, "lines": [l for _, l in items]})
        elif self.path.startswith("/api/files"):
            self._json({"files": ws_list()})
        elif self.path.startswith("/api/file?") or self.path == "/api/file":
            from urllib.parse import urlparse, parse_qs, unquote, quote
            q = parse_qs(urlparse(self.path).query)
            name = _safe_ws_name(unquote(q.get("name", [""])[0]))
            fp = os.path.join(workspace_dir(), name)
            if not os.path.isfile(fp):
                self._json({"ok": False, "error": "文件不存在"}, 404)
                return
            with open(fp, "rb") as f:
                data = f.read()
            force_dl = bool(q.get("download"))
            if not force_dl:
                try:
                    text = data.decode("utf-8")
                    self._json({"ok": True, "name": name, "text": text})
                    return
                except UnicodeDecodeError:
                    pass
            # 二进制或显式下载：以附件方式交付
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/history"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            cid = q.get("id", [""])[0]
            if cid:
                try:
                    self._json(hist_load(cid))
                except Exception:
                    self._json({"ok": False, "error": "对话不存在"}, 404)
            else:
                self._json({"items": hist_list()})
        elif self.path.startswith("/api/settings"):
            self._json({"dir": ws_config_load(), "root": workspace_dir()})
        elif self.path.startswith("/api/fs/browse"):
            from urllib.parse import urlparse, parse_qs, unquote
            q = parse_qs(urlparse(self.path).query)
            path = unquote(q.get("path", [""])[0]).strip()
            kind = (q.get("kind", ["gguf"])[0] or "gguf").lower()
            if not path:
                drives = [d + ":\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(d + ":\\")]
                self._json({"path": "", "parent": "", "dirs": drives})
                return
            path = os.path.abspath(path)
            if not os.path.isdir(path):
                self._json({"ok": False, "error": "目录不存在或不可访问"}, 404)
                return
            dirs, files = [], []
            exts = {"exe": (".exe", ".bat", ".cmd"),
                    "ini": (".ini", ".conf", ".cfg", ".txt"),
                    "gguf": (".gguf",)}
            want = exts.get(kind, ())
            try:
                for fn in sorted(os.listdir(path)):
                    fp = os.path.join(path, fn)
                    if os.path.isdir(fp):
                        dirs.append(fn)
                        continue
                    low = fn.lower()
                    if kind == "dir":
                        continue
                    if kind == "any" or (want and low.endswith(want)):
                        size = os.path.getsize(fp)
                        files.append({"name": fn,
                                      "size_gb": round(size / 1073741824, 2),
                                      "size_kb": int(size / 1024)})
            except Exception as e:
                self._json({"ok": False, "error": "无法读取该目录：" + str(e)}, 403)
                return
            self._json({"path": path, "parent": os.path.dirname(path),
                        "dirs": dirs, "files": files})
        elif self.path.startswith("/api/perms"):
            self._json({"always": sorted(perms_load())})
        elif self.path.startswith("/api/mcp"):
            servers = []
            for name, cfg in mcp_load_cfg().items():
                entry = {"name": name, "command": cfg.get("command", ""),
                         "args": cfg.get("args", []), "enabled": bool(cfg.get("enabled", True)),
                         "ok": False, "error": None, "tools": []}
                if not entry["enabled"]:
                    servers.append(entry)
                    continue
                try:
                    s = mcp_session(name, cfg)
                    entry["tools"] = [t["name"] for t in s.tools()]
                    entry["ok"] = True
                except Exception as e:
                    entry["error"] = str(e)[:300]
                servers.append(entry)
            self._json({"servers": servers})
        else:
            self.send_error(404)

    def do_PATCH(self):
        # 聊天页的「改标题 / 置顶 / 切档位 / 换模型」都走 PATCH；
        # 以前没实现这个方法，浏览器拿到 501，前端只能提示"保存失败"（2026-11-03 修）
        if gw_owns("PATCH", self.path):
            return self._gw_proxy("PATCH")
        self.send_error(404)

    def do_DELETE(self):
        if gw_owns("DELETE", self.path):
            return self._gw_proxy("DELETE")
        self.send_error(404)

    def do_POST(self):
        if gw_owns("POST", self.path):
            return self._gw_proxy("POST")
        if self.path == "/api/chat":
            # 内置聊天页代理：由启动器直连引擎，绕开浏览器 CORS 与系统代理问题
            body = self._read_body()
            port = first_engine_port()
            payload = {
                "model": "local",
                "messages": body.get("messages", []),
                "temperature": float(body.get("temperature", 0.7)),
                "stream": False,
            }
            if body.get("max_tokens") and int(body["max_tokens"]) > 0:
                payload["max_tokens"] = int(body["max_tokens"])
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=data, headers={"Content-Type": "application/json"})
            try:
                with _DIRECT_OPENER.open(req, timeout=600) as r:
                    self._json(json.loads(r.read().decode("utf-8", "replace")))
            except Exception as e:
                self._json({"ok": False,
                            "error": f"连不上引擎（127.0.0.1:{port}）：{e}。请确认面板状态灯变绿、agent/聊天用的端口与此一致。"},
                           502)
        elif self.path == "/api/slots/save":
            body = self._read_body()
            try:
                clean = host().store.save(body.get("slots") or [])
                _CACHE.clear()
                self._json({"ok": True, "slots": clean})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/slots/start":
            body = self._read_body()
            sid = str(body.get("id") or "").strip()
            try:
                inst = host().start(sid)
                _CACHE.clear()
                self._json({"ok": True, "instance": inst})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/slots/stop":
            body = self._read_body()
            try:
                done = host().stop(int(body.get("port") or 0))
                _CACHE.clear()
                self._json({"ok": True, "stopped": bool(done)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/slots/logs":
            body = self._read_body()
            self._json({"ok": True, "lines": host().logs(int(body.get("port") or 0),
                                                         int(body.get("n") or 120))})
        elif self.path == "/api/open-chat":
            # 引擎 exe 实际不带 webui（GET / 返回 404 空包），聊天页由启动器自带
            webbrowser.open(f"http://127.0.0.1:{LAUNCHER_PORT}/chat")
            self._json({"ok": True})
        elif self.path == "/api/chat/stream":
            body = self._read_body()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(obj):
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()

            try:
                emit({"type": "begin"})
                run_agent(emit, body.get("messages", []), body)
                emit({"type": "end"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    emit({"type": "error", "text": "服务内部错误：" + str(e)})
                except Exception:
                    pass
        elif self.path == "/api/chat/respond":
            body = self._read_body()
            pid = str(body.get("id", ""))
            with _pending_lock:
                item = PENDING.get(pid)
            if item:
                item[1]["approved"] = bool(body.get("approved"))
                item[1]["scope"] = str(body.get("scope", "once"))
                item[0].set()
            self._json({"ok": True})
        elif self.path == "/api/history/save":
            body = self._read_body()
            cid = str(body.get("id", "")).strip()
            if not cid:
                self._json({"ok": False, "error": "缺少对话 id"}, 400)
                return
            d = hist_save(cid, str(body.get("title", "")), body.get("messages", []))
            need_title = (not body.get("title")) and (not d.get("titled")) and any(
                m.get("role") == "user" for m in d.get("messages", []))
            self._json({"ok": True})
            if need_title:
                threading.Thread(target=hist_auto_title, args=(cid,), daemon=True).start()
        elif self.path == "/api/history/delete":
            body = self._read_body()
            hist_delete(str(body.get("id", "")))
            self._json({"ok": True})
        elif self.path == "/api/file/save":
            body = self._read_body()
            try:
                name = _safe_ws_name(body.get("name", ""))
                if body.get("base64"):
                    import base64
                    n = ws_write(name, base64.b64decode(body["base64"]))
                else:
                    n = ws_write(name, str(body.get("content", "")))
                self._json({"ok": True, "bytes": n})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/file/delete":
            body = self._read_body()
            try:
                ws_delete(str(body.get("name", "")))
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/mcp/add":
            body = self._read_body()
            name = str(body.get("name", "")).strip()[:40]
            command = str(body.get("command", "")).strip()
            args = body.get("args", [])
            if isinstance(args, str):
                import shlex
                try:
                    args = shlex.split(args)
                except Exception:
                    args = args.split()
            if not name or not command:
                self._json({"ok": False, "error": "名称和命令不能为空"}, 400)
                return
            if any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in name):
                self._json({"ok": False, "error": "名称含非法字符"}, 400)
                return
            cfg = mcp_load_cfg()
            if name in cfg:
                with _MCP_LOCK:
                    s = _MCP_SESSIONS.pop(name, None)
                if s:
                    s.close()
            cfg[name] = {"command": command, "args": args, "env": body.get("env", {})}
            mcp_save_cfg(cfg)
            self._json({"ok": True})
        elif self.path == "/api/perms/clear":
            body = self._read_body()
            perms_clear_always(str(body.get("tool", "")))
            self._json({"ok": True, "always": sorted(perms_load())})
        elif self.path == "/api/mcp/test":
            body = self._read_body()
            name = str(body.get("name", ""))
            cfg = mcp_load_cfg().get(name)
            if not isinstance(cfg, dict):
                self._json({"ok": False, "error": "没有这个服务器"}, 404)
                return
            try:
                s = mcp_session(name, cfg)
                tools = [t.get("name") for t in (s.tools() or [])]
                self._json({"ok": True, "tools": tools})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif self.path == "/api/mcp/toggle":
            body = self._read_body()
            name = str(body.get("name", ""))
            cfg = mcp_load_cfg()
            if name in cfg:
                cfg[name]["enabled"] = bool(body.get("enabled"))
                mcp_save_cfg(cfg)
                if not cfg[name]["enabled"]:
                    with _MCP_LOCK:
                        s_ = _MCP_SESSIONS.pop(name, None)
                    if s_:
                        s_.close()
            self._json({"ok": True})
        elif self.path == "/api/settings":
            body = self._read_body()
            d = str(body.get("dir", "")).strip()
            if not d:
                self._json({"ok": False, "error": "目录不能为空"}, 400)
                return
            # 只允许选目录，不允许指向文件
            ap = d if os.path.isabs(d) else os.path.join(BASE, d)
            ap = os.path.abspath(ap)
            if os.path.isfile(ap):
                self._json({"ok": False, "error": "该路径是文件，请选择文件夹"}, 400)
                return
            ws_config_save(d)
            workspace_dir()
            self._json({"ok": True, "root": workspace_dir()})
        elif self.path == "/api/mcp/remove":
            body = self._read_body()
            name = str(body.get("name", ""))
            cfg = mcp_load_cfg()
            if name in cfg:
                del cfg[name]
                mcp_save_cfg(cfg)
            with _MCP_LOCK:
                s = _MCP_SESSIONS.pop(name, None)
            if s:
                s.close()
            self._json({"ok": True})
        else:
            self.send_error(404)
