"""Bonsai 网关：单入口 OpenAI 兼容 + SSE 事件规范 + 会话/片段/日志/压缩。

职责边界（对应需求 §3）
  ✔ 能力聚合（/v1/models + 静态声明）、effort 校验与转换、压缩状态机
  ✔ 用量统计、日志脱敏代理、SSE 事件规范、会话与片段持久化
  ✘ 不启动上游进程（deploy/ 负责）、不做端口级模型路由（端点解析见 facts.md F1/F2）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid

# 本机回环一律绕过系统代理（HTTP_PROXY 会把 127.0.0.1 也拐去代理）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gateway import compression, config as cfgmod, effort, estimate, security, upstream
    from gateway.logbus import LogBus, start_tailers
    from gateway.store import Store
else:
    from . import compression, config as cfgmod, effort, estimate, security, upstream
    from .logbus import LogBus, start_tailers
    from .store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
        ".woff2": "font/woff2", ".map": "application/json"}


class Gateway:
    def __init__(self, cfg_path: Optional[str] = None):
        self.cfg = cfgmod.load(cfg_path)
        data_dir = os.path.join(ROOT, "gateway", "data")
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        db_path = self.cfg.gateway.get("db") or os.path.join(data_dir, "gateway.db")
        if not os.path.isabs(db_path):
            db_path = os.path.join(ROOT, db_path)
        self.store = Store(db_path)
        self.compressor = compression.Compressor(self.store, self.cfg.compression)
        self.bus = LogBus()
        rl = (self.cfg.gateway.get("rate_limit") or {})
        self.limiter = security.RateLimiter(int(rl.get("per_minute", 120)), int(rl.get("burst", 20)))
        self.token = security.load_token(data_dir)
        self.streams: Dict[str, Dict[str, Any]] = {}
        # None = 独立运行（没有启动器托管），回退成逐个 HTTP 探活；
        # set = 内嵌在启动器里时由启动器写入「真正在监听的端口」，列表只列在跑的模型。
        self.live_ports: Optional[set] = None
        self.live_ids: Optional[set] = None      # 显式白名单（Router 模式没有独立端口）
        self._tailers = start_tailers(self.bus, self.cfg.gateway.get("log_files") or [])
        self.bus.publish("gateway", f"gateway started · models={list(self.cfg.models)}")

    # ---------------------------------------------------------------- 视图
    def _port_of(self, m) -> int:
        try:
            return int(urlparse(m.upstream.base_url).port or 0)
        except Exception:
            return 0

    def is_live(self, m) -> bool:
        """模型是否可用：启动器托管时看白名单（id 或端口），独立运行时才做健康检查。"""
        if not m.enabled:
            return False
        if self.live_ids is None and self.live_ports is None:
            return upstream.health(m)
        if self.live_ids is not None:
            # 启动器托管：只认白名单 id。以前还按端口兜底，同一个端口上"没启动的模型"
            # 也会被判成在线（27B 占了 9B 的端口就会假显示）。
            return m.id in self.live_ids
        return self.live_ports is not None and self._port_of(m) in self.live_ports

    def register_runtime(self, model_id: str, path: str, port: int, window: int = 32768,
                         label: Optional[str] = None, kv_kib: float = 18.0,
                         mode: str = "direct", base_url: Optional[str] = None):
        """把「面板里临时启动的任意 GGUF」注册成可聊模型。

        用途：用户把模型放在 D:\\models 之类的地方、models-config.json 里没有它，
        只要从面板启动，聊天页就能直接选到它——不再要求先手写配置。
        """
        mid = str(model_id or (os.path.basename(path)[:-5] if path else "")) or ("runtime-%d" % port)
        base = os.path.basename(path) if path else mid
        if mode == "router":
            endpoint = (base_url or "").rstrip("/") or "http://127.0.0.1:8330/v1"
        else:
            endpoint = "http://127.0.0.1:%d/v1" % int(port)
        self.cfg.models[mid] = cfgmod.Model(
            id=mid, label=label or base, subtitle="运行时加载 · 由启动器注册",
            enabled=True, runtime="prismml-kvmem", path=path or "", quant="GGUF",
            binary=None, launch={},
            upstream=cfgmod.Upstream(mode, endpoint, base),
            sampling={"temp": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 0},
            thinking=cfgmod.Thinking(True, ["none", "low", "medium", "high"], "medium"),
            context=cfgmod.Context(window=max(2048, int(window)), kv_kib_per_token=float(kv_kib),
                                   output_reserve=4096, thinking_reserve=4096),
        )
        return self.cfg.models[mid]

    def rerank_cfg(self) -> Dict[str, Any]:
        """rerank 是独立的一条链路：固定一个模型 + 一个上游（默认就是 Router）。

        和聊天模型分开选，避免把 reranker 混进 /api/models 的对话列表里。
        """
        raw = self.cfg.raw or {}
        r = dict(raw.get("rerank") or {})
        router = raw.get("router") or {}
        base = str(r.get("base_url") or router.get("endpoint") or "http://127.0.0.1:8330").rstrip("/")
        return {"enabled": bool(r.get("enabled", True)),
                "model": str(r.get("model") or "").strip(),
                "base_url": base,
                "top_n": int(r.get("top_n") or 0),
                "sigmoid": bool(r.get("sigmoid", True)),
                "timeout": float(r.get("timeout") or 120)}

    def find_model(self, spec: str):
        """按 OpenAI 请求里的 model 字段找模型：精确 id → 去 #端口 → 文件名。"""
        spec = str(spec or "").strip()
        if not spec:
            return None
        m = self.cfg.models.get(spec)
        if m is not None:
            return m
        base = spec.split("#")[0].strip()
        for mid, mm in self.cfg.models.items():
            if mid == base:
                return mm
        for mid, mm in self.cfg.models.items():
            path = str(getattr(mm, "path", "") or "")
            if path and os.path.basename(path) == spec:
                return mm
        return None

    def openai_models(self) -> List[Dict[str, Any]]:
        """OpenAI /v1/models 格式：只列真正在跑的（live_ids）。"""
        live = getattr(self, "live_ids", None)
        out = []
        for mid, m in self.cfg.models.items():
            if live is not None and mid not in live:
                continue
            if not getattr(m, "enabled", True):
                continue
            out.append({"id": mid, "object": "model", "created": 0,
                        "owned_by": "local", "root": mid})
        return out

    def model_view(self, m) -> Dict[str, Any]:
        return {
            "id": m.id, "label": m.label, "subtitle": m.subtitle, "quant": m.quant,
            "runtime": m.runtime, "enabled": m.enabled,
            "thinking": {"supported": m.thinking.supported, "efforts": m.thinking.efforts,
                         "default": m.thinking.default},
            "context": {"window": m.context.window,
                        "budget": m.context.budget(False),
                        "budget_thinking": m.context.budget(True)},
            "endpoint_mode": m.upstream.mode,
            "online": self.is_live(m),
        }

    # ---------------------------------------------------------------- 摘要器
    def summarizer(self, model, msgs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        sc = (self.cfg.compression.get("summary") or {})
        target = None
        mid = sc.get("model")
        if mid and mid in self.cfg.models and self.cfg.models[mid].enabled:
            target = self.cfg.models[mid]
        if target is None and model.thinking.supported:
            target = model
        if target is None:
            return None
        eff = sc.get("effort", "low")
        if eff not in (target.thinking.efforts or ["none"]):
            eff = "none"
        body = effort.summarize_body(target, msgs, compression.SUMMARY_INSTRUCTION, eff,
                                     int(sc.get("max_output_tokens", 512)))
        for attempt in range(int(sc.get("retry", 1)) + 1):
            try:
                res = upstream.complete(target, body, timeout=90)
                text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                data = _extract_json(text)
                if isinstance(data, dict):
                    return {k: data.get(k, [] if k != "goal" else "") for k in
                            ("goal", "decisions", "files", "errors", "open_questions")}
            except Exception as e:
                self.bus.publish("compress", f"summary attempt {attempt+1} failed: {e}")
        return None

    # ---------------------------------------------------------------- 会话
    def ensure_session(self, user: str, sid: Optional[str], model_id: str, eff: str) -> Dict[str, Any]:
        if sid:
            s = self.store.get_session(sid, user)
            if s:
                return s
        return self.store.create_session(user, model_id, eff)

    def remember_user_message(self, sid: str, text: str) -> None:
        """客户端每轮都会带完整 messages；只把"新的最后一条用户消息"入库，避免重复。"""
        if not text.strip():
            return
        stored = [m for m in self.store.list_messages(sid) if m["role"] == "user"]
        if stored and estimate.content_to_text(stored[-1]["content"]).strip() == text.strip():
            return
        self.store.append_message(sid, "user", text, estimate.estimate_text(text))

    def stream_slot(self, key: str) -> Dict[str, Any]:
        slot = self.streams.get(key)
        if not slot:
            slot = {"events": [], "seq": 0, "ts": time.time()}
            self.streams[key] = slot
        return slot

    def gc_streams(self, ttl: int = 180):
        now = time.time()
        for k in [k for k, v in self.streams.items() if now - v["ts"] > ttl]:
            self.streams.pop(k, None)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for candidate in (t, (re.search(r"\{.*\}", t, re.S).group(0) if re.search(r"\{.*\}", t, re.S) else "")):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BonsaiGateway/1.0"

    # ---------------------------------------------------------------- 基础
    @property
    def gw(self) -> Gateway:
        return self.server.gw          # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allowed = self.gw.cfg.gateway.get("cors_origins") or []
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def _json(self, obj: Any, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, code: int, err_code: str, message: str, retryable: bool = False, **extra):
        payload = {"error": {"code": err_code, "message": message, "retryable": retryable}}
        payload["error"].update(extra)
        self._json(payload, code)

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        limit = int(self.gw.cfg.gateway.get("max_body_bytes", 4 * 1024 * 1024))
        if n > limit:
            raise ValueError("body too large")
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("bad json")

    # ---------------------------------------------------------------- 鉴权
    def _token_from(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == security.COOKIE_NAME:
                return v
        return ""

    def _authed(self) -> bool:
        return security.check_token(self._token_from(), self.gw.token)

    def _require(self) -> bool:
        if self._authed():
            return True
        self._err(401, "unauthorized", "需要登录令牌（Authorization: Bearer 或 Cookie）")
        return False

    def _authed_openai(self) -> bool:
        """OpenAI 兼容端点的鉴权：真令牌放行；本机回环 + 任意非空 key 也放行。

        原因：Cherry Studio / Open WebUI / IDE 插件这类工具只让填一个 api key，
        而网关的令牌是随机的。网关只绑 127.0.0.1，所以"回环 + 非空 key"不会放大暴露面。
        """
        tok = self._token_from()
        if security.check_token(tok, self.gw.token):
            return True
        if not str(tok or "").strip():
            return False
        try:
            host = self.client_address[0]
        except Exception:
            host = ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        return origin in (self.gw.cfg.gateway.get("cors_origins") or [])

    # ---------------------------------------------------------- OpenAI 兼容
    def _openai_models(self):
        return self._json({"object": "list", "data": self.gw.openai_models()})

    def _openai_forward(self, body, suffix="/chat/completions"):
        """把 /v1/chat/completions 原样转发给对应实例。

        刻意**不做**任何加工：不读会话、不加思考档、不压缩上下文。
        上下文压缩只在 /chat 页面按按钮手动触发，第三方 agent 走这里是纯直通。
        """
        spec = str(body.get("model") or "").strip()
        m = self.gw.find_model(spec)
        if m is None:
            return self._err(404, "model_not_found",
                             "没有这个模型：%s。GET /v1/models 可看当前在跑的列表"
                             % (spec or "(空)"), True)
        base = str(getattr(m.upstream, "base_url", "") or "").rstrip("/")
        if not base:
            return self._err(503, "no_upstream", "模型 %s 没有可用上游地址" % m.id, True)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(base + suffix, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            up = _OPENER.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            up = e
        except Exception as e:
            return self._err(502, "upstream_unreachable", "上游不可达：%s" % e, True)
        try:
            status = getattr(up, "status", None) or getattr(up, "code", 502)
            ctype = up.headers.get("Content-Type", "application/json")
            if "text/event-stream" in ctype:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = up.read1(8192) if hasattr(up, "read1") else up.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            raw = up.read()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)
        except Exception as e:
            try:
                self._err(502, "relay_failed", "转发失败：%s" % e, True)
            except Exception:
                pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _openai_chat(self, body):
        return self._openai_forward(body, "/chat/completions")

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p == "/" or p.startswith("/index"):
            return self._static("index.html")
        if p.startswith("/assets/"):
            return self._static(p[len("/assets/"):])
        if p == "/favicon.ico":
            return self._static("favicon.png")

        if p == "/api/health":
            return self._json({"ok": True, "models": len(self.gw.cfg.enabled_models()),
                               "router": bool((self.gw.cfg.router or {}).get("enabled"))})
        if p == "/api/login":
            return self._err(405, "method_not_allowed", "请用 POST 提交令牌")

        if p in ("/v1/models", "/models"):
            if not self._authed_openai():
                return self._err(401, "unauthorized", "需要 api key（任意非空字符串即可）")
            return self._openai_models()

        if not self._require():
            return

        if p == "/api/me":
            return self._json({"user": "local", "authenticated": True})
        if p == "/api/rerank":
            rc = self.gw.rerank_cfg()
            return self._json({"rerank": rc, "models": upstream.list_models(rc["base_url"])})

        if p == "/api/models":
            views = [self.gw.model_view(m) for m in self.gw.cfg.models.values()]
            if self.gw.live_ports is not None:
                # 聊天页只列「真的在跑」的模型：配置里写了但没下载/没启动的，不出现
                views = [v for v in views if v["online"]]
            return self._json({"models": views, "none_started": not views})
        if p == "/api/sessions":
            kw = (q.get("q") or [""])[0]
            return self._json({"sessions": self.gw.store.list_sessions("local", kw)})
        if p == "/api/snippets":
            return self._json({"snippets": self.gw.store.snippets("local")})
        if p == "/api/logs":
            after = int((q.get("after") or ["0"])[0] or 0)
            return self._json({"lines": self.gw.bus.tail(500, after),
                               "last": self.gw.bus.tail(1)[-1]["id"] if self.gw.bus.tail(1) else 0})
        if p == "/api/logs/stream":
            return self._logs_stream()

        m = re.fullmatch(r"/api/sessions/([^/]+)/messages", p)
        if m:
            sid = unquote(m.group(1))
            if not self.gw.store.get_session(sid, "local"):
                return self._err(404, "not_found", "会话不存在")
            msgs = self.gw.store.list_messages(sid)
            blocks = self.gw.store.list_blocks(sid)
            return self._json({"session": self.gw.store.get_session(sid, "local"),
                               "messages": [{k: v for k, v in x.items() if k != "session_id"} for x in msgs],
                               "blocks": blocks})

        m = re.fullmatch(r"/api/sessions/([^/]+)/blocks", p)
        if m:
            sid = unquote(m.group(1))
            return self._json({"blocks": self.gw.store.list_blocks(sid)})

        m = re.fullmatch(r"/api/sessions/([^/]+)/blocks/([^/]+)/peek", p)
        if m:
            sid, bid = unquote(m.group(1)), unquote(m.group(2))
            try:
                return self._json(self.gw.compressor.peek(sid, bid))
            except KeyError:
                return self._err(404, "not_found", "折叠块不存在")

        m = re.fullmatch(r"/api/sessions/([^/]+)/usage", p)
        if m:
            sid = unquote(m.group(1))
            return self._json({"usage": self.gw.store.usage_all(sid)})

        return self._err(404, "not_found", f"未知路径 {p}")

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/login":
            return self._login()

        if p in ("/v1/chat/completions", "/chat/completions"):
            if not self._authed_openai():
                return self._err(401, "unauthorized", "需要 api key（任意非空字符串即可）")
            if not self._origin_ok():
                return self._err(403, "bad_origin", "Origin 不在白名单")
            if not self.gw.limiter.allow(self.client_address[0]):
                return self._err(429, "rate_limited", "请求过于频繁，请稍后重试", True)
            try:
                body = self._body()
            except ValueError as e:
                return self._err(400, "bad_request", str(e))
            return self._openai_chat(body)

        if p in ("/v1/embeddings", "/embeddings"):
            if not self._authed_openai():
                return self._err(401, "unauthorized", "需要 api key（任意非空字符串即可）")
            if not self._origin_ok():
                return self._err(403, "bad_origin", "Origin 不在白名单")
            if not self.gw.limiter.allow(self.client_address[0]):
                return self._err(429, "rate_limited", "请求过于频繁，请稍后重试", True)
            try:
                body = self._body()
            except ValueError as e:
                return self._err(400, "bad_request", str(e))
            return self._openai_forward(body, "/embeddings")

        if not self._require():
            return
        if not self._origin_ok():
            return self._err(403, "bad_origin", "Origin 不在白名单")
        if not self.gw.limiter.allow(self.client_address[0]):
            return self._err(429, "rate_limited", "请求过于频繁，请稍后重试", True)

        try:
            body = self._body()
        except ValueError as e:
            return self._err(400, "bad_request", str(e))

        if p in ("/v1/rerank", "/rerank"):
            rc = self.gw.rerank_cfg()
            if not rc["enabled"]:
                return self._err(503, "rerank_disabled",
                                 "rerank 未启用（models-config.json → rerank.enabled）")
            model = str(body.get("model") or rc["model"] or "").strip()
            if not model:
                return self._err(400, "rerank_model_unset",
                                 "还没选 rerank 模型：启动器 → ⓪·R Router → Rerank 模型，"
                                 "或在请求体里带 model")
            docs = body.get("documents") or body.get("texts") or []
            if not isinstance(docs, list) or not docs:
                return self._err(400, "bad_request", "documents 不能为空")
            norm = [d.get("text") if isinstance(d, dict) else str(d) for d in docs]
            payload: Dict[str, Any] = {"model": model,
                                       "query": str(body.get("query") or ""),
                                       "documents": norm}
            top_n = body.get("top_n") or rc["top_n"]
            if top_n:
                payload["top_n"] = int(top_n)
            if "return_documents" in body:
                payload["return_documents"] = bool(body["return_documents"])
            res = upstream.rerank(rc["base_url"] + "/v1/rerank", payload, timeout=rc["timeout"])
            # Router 带 --no-models-autoload 时首个请求回 "model is not loaded"：
            # 自动加载一次、等它真的加载完再重试，让 rerank 对调用方是一次性的。
            _err = str(res.get("error") or "").lower()
            if not res["ok"] and ("not loaded" in _err or "loading" in _err):
                ld = upstream.load_model(rc["base_url"], model)
                if ld["ok"]:
                    upstream.wait_loaded(rc["base_url"], model, timeout=180)
                    res = upstream.rerank(rc["base_url"] + "/v1/rerank", payload,
                                          timeout=rc["timeout"])
                else:
                    self.gw.logbus.publish("gateway", "rerank 模型 %s 加载失败：%s"
                                           % (model, ld.get("error")))
            if not res["ok"]:
                code = res["status"] if 400 <= int(res["status"]) < 600 else 502
                return self._err(code, "rerank_upstream_error",
                                 "上游 rerank 失败：%s（模型 %s）" % (res.get("error") or "未知错误", model),
                                 True)
            out = res["data"] if isinstance(res["data"], dict) else {}
            out.setdefault("model", model)
            # bge 这类 cross-encoder 上游给的是原始 logits（可能是 -9 ~ +4），
            # 直接透出会让人以为坏了；归一化到 0~1，排序不变。
            if rc.get("sigmoid", True):
                rows = out.get("results")
                if isinstance(rows, list):
                    nums = [r.get("relevance_score") for r in rows if isinstance(r, dict)]
                    nums = [x for x in nums if isinstance(x, (int, float))]
                    if nums and (min(nums) < 0 or max(nums) > 1):
                        for r in rows:
                            v = r.get("relevance_score")
                            if isinstance(v, (int, float)):
                                r["relevance_score"] = round(1.0 / (1.0 + math.exp(-float(v))), 6)
                        out["score_kind"] = "sigmoid"
            return self._json(out)

        if p == "/api/chat":
            return self._chat(body)
        if p == "/api/sessions/bulk-delete":
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids:
                return self._err(400, "bad_request", "ids 不能为空")
            n = self.gw.store.delete_sessions([str(x) for x in ids][:500], "local")
            self.gw.store.audit("local", "session.bulk_delete", str(n))
            return self._json({"deleted": n})

        if p == "/api/sessions":
            model_id = str(body.get("model") or "")
            if model_id not in self.gw.cfg.models:
                return self._err(400, "unknown_model", f"未知模型 {model_id}")
            eff = str(body.get("reasoning_effort") or self.gw.cfg.models[model_id].thinking.default)
            s = self.gw.store.create_session("local", model_id, eff, str(body.get("title") or "新对话"))
            self.gw.store.audit("local", "session.create", s["id"])
            return self._json({"session": s}, 201)

        if p == "/api/snippets":
            title = str(body.get("title") or "").strip()
            text = str(body.get("body") or "")
            if not title:
                return self._err(400, "bad_request", "标题不能为空")
            return self._json({"snippet": self.gw.store.save_snippet("local", body.get("id"), title, text)}, 201)

        m = re.fullmatch(r"/api/sessions/([^/]+)/compress", p)
        if m:
            sid = unquote(m.group(1))
            sess = self.gw.store.get_session(sid, "local")
            if not sess:
                return self._err(404, "not_found", "会话不存在")
            mid = str(body.get("model") or sess.get("model") or "")
            model = self.gw.cfg.models.get(mid)
            if not model:
                return self._err(400, "unknown_model", f"未知模型 {mid}")
            eff = str(sess.get("effort") or model.thinking.default)
            thinking_on = bool(model.thinking.supported and eff != "none")
            msgs = [{"role": x["role"], "content": x["content"]}
                    for x in self.gw.store.list_messages(sid) if not x.get("archived")]
            if not msgs:
                return self._err(400, "empty_session", "这个对话还没有消息，不用优化")
            # 只估算、不落库：真正生效在下一次发送时（避免点一次按钮就多存一份块）
            comp = self.gw.compressor.build(model, sid, msgs, thinking_on, persist=False)
            self.gw.store.update_session(sid, "local", compress=1)
            saved = max(0, comp["tokens_before"] - comp["tokens_after"])
            self.gw.store.audit("local", "session.compress", f"{sid} saved={saved}")
            return self._json({"ok": True, "compress": 1,
                               "tokens_before": comp["tokens_before"],
                               "tokens_after": comp["tokens_after"],
                               "saved": saved, "budget": comp["budget"],
                               "steps": [e.get("action") for e in comp["events"]]})

        m = re.fullmatch(r"/api/sessions/([^/]+)/expand", p)
        if m:
            sid = unquote(m.group(1))
            ids = body.get("block_ids") or []
            limit = int(self.gw.compressor.cfg.get("expand_budget_per_request", 3))
            if len(ids) > limit:
                return self._json({"ok": False, "code": "expand_limit_exceeded",
                                   "message": f"每轮最多展开 {limit} 个折叠块，本次请求了 {len(ids)} 个",
                                   "block_ids": ids, "limit": limit}, 400)
            return self._json({"ok": True, "block_ids": ids[:limit]})

        return self._err(404, "not_found", f"未知路径 {p}")

    def do_PATCH(self):
        if not self._require():
            return
        p = urlparse(self.path).path
        mm = re.fullmatch(r"/api/messages/(\d+)", p)
        if mm:
            try:
                body = self._body()
            except ValueError as e:
                return self._err(400, "bad_request", str(e))
            msg = self.gw.store.set_message_version(int(mm.group(1)), int(body.get("v_index") or 0))
            if not msg:
                return self._err(404, "not_found", "消息不存在")
            return self._json({"message": msg})
        m = re.fullmatch(r"/api/sessions/([^/]+)", p)
        if not m:
            return self._err(404, "not_found", "未知路径")
        sid = unquote(m.group(1))
        try:
            body = self._body()
        except ValueError as e:
            return self._err(400, "bad_request", str(e))
        fields = {k: body[k] for k in ("title", "pinned", "model", "effort") if k in body}
        if "title" in fields and not str(fields["title"]).strip():
            return self._err(400, "bad_request", "标题不能为空")
        ok = self.gw.store.update_session(sid, "local", **fields)
        if not ok:
            return self._err(404, "not_found", "会话不存在")
        self.gw.store.audit("local", "session.update", json.dumps(fields, ensure_ascii=False))
        return self._json({"session": self.gw.store.get_session(sid, "local")})

    def do_DELETE(self):
        if not self._require():
            return
        p = urlparse(self.path).path
        m = re.fullmatch(r"/api/sessions/([^/]+)/compress", p)
        if m:
            sid = unquote(m.group(1))
            if not self.gw.store.update_session(sid, "local", compress=0):
                return self._err(404, "not_found", "会话不存在")
            return self._json({"ok": True, "compress": 0})
        m = re.fullmatch(r"/api/sessions/([^/]+)", p)
        if m:
            sid = unquote(m.group(1))
            ok = self.gw.store.delete_session(sid, "local")
            if not ok:
                return self._err(404, "not_found", "会话不存在")
            self.gw.store.audit("local", "session.delete", sid)
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/snippets/([^/]+)", p)
        if m:
            ok = self.gw.store.delete_snippet("local", unquote(m.group(1)))
            return self._json({"ok": ok})
        return self._err(404, "not_found", "未知路径")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,Last-Event-ID")
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------------- 登录
    def _login(self):
        try:
            body = self._body()
        except ValueError as e:
            return self._err(400, "bad_request", str(e))
        if not security.check_token(str(body.get("token") or ""), self.gw.token):
            self.gw.store.audit("anon", "login.fail", self.client_address[0])
            return self._err(401, "unauthorized", "令牌不正确")
        self.send_response(204)
        self.send_header("Set-Cookie",
                         f"{security.COOKIE_NAME}={body['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.gw.store.audit("local", "login.ok", self.client_address[0])

    # ---------------------------------------------------------------- 静态
    def _static(self, rel: str):
        rel = rel.replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            return self._err(403, "forbidden", "非法路径")
        path = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(path):
            return self._err(404, "not_found", f"静态资源不存在：{rel}")
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    # ---------------------------------------------------------------- SSE
    def _sse_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

    def _sse_send(self, obj: Dict[str, Any]):
        buf = "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        self.wfile.write(buf.encode("utf-8"))
        self.wfile.flush()

    def _logs_stream(self):
        q = self.gw.bus.subscribe()
        self._sse_head()
        try:
            for item in self.gw.bus.tail(80):
                self._sse_send({"type": "log", **item})
            while True:
                try:
                    item = q.get(timeout=15)
                except Exception:
                    self._sse_send({"type": "ping", "ts": int(time.time())})
                    continue
                self._sse_send({"type": "log", **item})
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.gw.bus.unsubscribe(q)

    # ---------------------------------------------------------------- 聊天
    def _chat(self, body: Dict[str, Any]):
        g = self.gw
        model_id = str(body.get("model") or "")
        model = g.cfg.models.get(model_id)
        if not model:
            return self._err(400, "unknown_model", f"未知模型 {model_id}",
                             supported=list(g.cfg.models))
        if not model.enabled:
            return self._err(503, "model_offline", f"模型 {model_id} 未启用", True)
        try:
            eff, thinking_on = effort.resolve(model, body)
        except effort.EffortError as e:
            return self._json(e.payload(), 400)

        msgs = body.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return self._err(400, "bad_request", "messages 不能为空")

        sid = str(body.get("session_id") or "")
        # 「重新生成」：客户端带上被重答的那条消息 id，服务端把新结果作为它的新版本，不新增一条
        try:
            regen_of = int(body.get("regen_of") or 0) or None
        except (TypeError, ValueError):
            regen_of = None
        session = g.ensure_session("local", sid, model_id, eff)
        sid = session["id"]
        g.store.update_session(sid, "local", model=model_id, effort=eff)

        last_user = ""
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = estimate.content_to_text(m.get("content"))
                break
        g.remember_user_message(sid, last_user)

        if not upstream.health(model):
            return self._err(503, "model_offline",
                             f"上游不可达：{model.upstream.base_url}（模型 {model_id}）", True)

        # 上下文优化默认不自动做（2026-11-03 起）：只有本会话开过开关、或本次请求显式
        # 带 compress=true、或配置 compression.auto=true 时才压缩。否则原样发送，只算 token。
        sess_row = g.store.get_session(sid, "local") or {}
        if "compress" in body:
            want_compress = bool(body.get("compress"))
        else:
            want_compress = bool(sess_row.get("compress"))
        if bool((g.cfg.compression or {}).get("auto")):
            want_compress = True
        if want_compress:
            comp = g.compressor.build(model, sid, msgs, thinking_on,
                                      expand_ids=body.get("expand") or [], summarizer=g.summarizer)
        else:
            n_tok = estimate.messages_tokens(msgs)
            comp = {"messages": msgs, "events": [], "lossless": True,
                    "tokens_before": n_tok, "tokens_after": n_tok,
                    "budget": model.context.budget(thinking_on)}

        if comp["tokens_after"] > comp["budget"]:
            return self._err(409, "context_overflow",
                             f"压缩后仍超出上下文预算（{comp['tokens_after']} > {comp['budget']}）",
                             False, tokens=comp["tokens_after"], budget=comp["budget"])

        max_tokens = int(body.get("max_tokens") or model.launch.get("n") or 0)
        # low/xhigh 档模板会注入英文提示 → 补一句中文要求，避免思考跑成英文（可用 models-config 的
        # thinking.pin_language=false 关掉）
        th_cfg = (g.cfg.raw.get("thinking") or {})
        lang_hint = "" if th_cfg.get("pin_language") is False else str(
            th_cfg.get("hint") or effort.DEFAULT_LANG_HINT)
        up_body = effort.upstream_body(model, comp["messages"], eff, thinking_on, max_tokens,
                                       stream=True, system_hint=lang_hint)

        stream_id = uuid.uuid4().hex[:12]
        self._sse_head()

        def emit(obj: Dict[str, Any]):
            self._sse_send(obj)

        content_parts: List[str] = []
        think_parts: List[str] = []
        usage: Dict[str, Any] = {}
        stop_reason = "stop"
        # 思考耗时：首条思考增量 → 首条正文增量的间隔（与前端口径一致，落库后可跨刷新对比档位）
        t_think0 = t_body0 = 0.0
        try:
            emit({"type": "message_start", "stream_id": stream_id, "session_id": sid,
                  "model": model_id, "effort": eff, "thinking": thinking_on,
                  "tokens_before": comp["tokens_before"], "tokens_after": comp["tokens_after"],
                  "budget": comp["budget"], "lossless": comp["lossless"]})
            for ev in comp["events"]:
                emit(ev)
            try:
                for ev in upstream.stream(model, up_body):
                    if ev["type"] == "delta":
                        if ev["role"] == "thinking":
                            if not t_think0:
                                t_think0 = time.time()
                            think_parts.append(ev["text"])
                        else:
                            if not t_body0:
                                t_body0 = time.time()
                            content_parts.append(ev["text"])
                        emit({"type": "delta", "role": ev["role"], "text": ev["text"]})
                    elif ev["type"] == "tool_call":
                        emit({"type": "tool_call", "name": ev.get("name", ""),
                              "arguments": ev.get("arguments", "")})
                    elif ev["type"] == "usage":
                        usage = ev["usage"]
            except upstream.UpstreamError as e:
                stop_reason = "error"
                emit({"type": "error", "code": e.code, "message": str(e),
                      "retryable": e.retryable})
            except (BrokenPipeError, ConnectionResetError):
                stop_reason = "aborted"

            content = "".join(content_parts)
            thinking = "".join(think_parts)
            p_tok = int(usage.get("prompt_tokens") or comp["tokens_after"])
            c_tok = int(usage.get("completion_tokens") or estimate.estimate_text(content) + estimate.estimate_text(thinking))
            r_tok = int(usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0) if isinstance(usage.get("completion_tokens_details"), dict) else 0
            if not r_tok:
                r_tok = estimate.estimate_text(thinking)

            think_ms = int((t_body0 - t_think0) * 1000) if (t_think0 and t_body0 and t_body0 >= t_think0) else 0
            saved = None
            if content.strip() or thinking.strip():
                old_msg = g.store.get_message(regen_of) if regen_of else None
                if old_msg and old_msg.get("session_id") == sid:
                    vs = list(old_msg.get("versions") or [])
                    vs.append({"content": content, "thinking": thinking, "think_ms": think_ms,
                               "effort": eff, "created_at": int(time.time())})
                    saved = g.store.update_message(old_msg["id"], content=content, thinking=thinking,
                                                   think_ms=think_ms, effort=eff, versions=vs,
                                                   v_index=len(vs) - 1)
                else:
                    saved = g.store.append_message(sid, "assistant", content, c_tok,
                                                   thinking=thinking, think_ms=think_ms, effort=eff)
            u = g.store.add_usage(sid, model_id, p_tok, c_tok, r_tok)
            emit({"type": "usage", "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok,
                                             "reasoning_tokens": r_tok},
                  "session_usage": u})
            emit({"type": "done", "stop_reason": stop_reason, "stream_id": stream_id,
                  "content": content, "thinking": thinking, "thinking_ms": think_ms, "effort": eff,
                  "message_id": (saved or {}).get("id"),
                  "v_index": (saved or {}).get("v_index", 0),
                  "versions": len((saved or {}).get("versions") or []),
                  "tokens_before": comp["tokens_before"], "tokens_after": comp["tokens_after"]})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"type": "error", "code": "gateway_error", "message": str(e), "retryable": True})
            except Exception:
                pass
            self.gw.bus.publish("gateway", f"chat error: {e}\n{traceback.format_exc(limit=3)}")
        finally:
            pass


def serve(gw: Gateway, host: str, port: int, tls: Optional[Tuple[str, str]] = None):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.gw = gw            # type: ignore[attr-defined]
    httpd.daemon_threads = True
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls[0], tls[1])
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    scheme = "https" if tls else "http"
    print(f"[gateway] {scheme}://{host}:{port}  token={gw.token[:6]}…", flush=True)
    print(f"[gateway] 首次访问请用完整令牌登录（保存在 gateway/data/token.txt）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for t in gw._tailers:
            t.stop()


def main():
    ap = argparse.ArgumentParser(description="Bonsai 多模型网关")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--tls-cert", default=None)
    ap.add_argument("--tls-key", default=None)
    args = ap.parse_args()

    gw = Gateway(args.config)
    host = args.host or gw.cfg.gateway.get("host", "127.0.0.1")
    port = args.port or int(gw.cfg.gateway.get("port", 8400))
    tls = (args.tls_cert, args.tls_key) if args.tls_cert and args.tls_key else None
    serve(gw, host, port, tls)


if __name__ == "__main__":
    main()
