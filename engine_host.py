"""单模型引擎宿主：每个模型一个独立进程，对外暴露一个「聚合代理」端口。

设计（2026-11-04 重构）
----------------------
- 两个引擎走**完全相同**的流程，只是 exe 与参数集不同：
  * kvmem  engine/llama-kvmem-server.exe（三元量化，参数全量）
  * llama   外部 llama-server.exe（标准 GGUF，常用参数）
- 不再有 router 托管 / 按需加载 / 闲置卸载。模型一启动就常驻，停止才退出。
- 每个槽位对外端口 = 用户填的 port；引擎真实端口是内部空闲端口。
  对外端口由本模块的代理服务接管：
    GET  /v1/models      → 返回**全部**在跑模型（含类型/端口），任一端口都一样
    GET  /health /props  → 转发本槽位引擎
    其它请求             → 按 body.model 找到对应槽位再转发（SSE 原样透传）
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")
KV_EXE = os.path.join(ENGINE_DIR, "llama-kvmem-server.exe")
# llama.cpp 引擎：优先用 engine/ 目录下的 llama-server.exe（用户自行放入），
# 其次读 models-config.json 里的 llama_exe 配置；不再硬编码任何绝对路径。
LLAMA_EXE_DEFAULT = os.path.join(ENGINE_DIR, "llama-server.exe")

TYPES = ("chat", "rerank", "embed")
TYPE_LABEL = {"chat": "对话", "rerank": "重排", "embed": "嵌入"}
ENGINE_LABEL = {"kvmem": "KVMem", "llama": "llama.cpp"}


# ------------------------------------------------------------------ 小工具
def tcp_open(port: int, timeout: float = 0.25) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def free_port(start: int = 19000, end: int = 19999) -> int:
    for p in range(start, end):
        if not tcp_open(p, 0.05):
            return p
    raise RuntimeError("没有空闲端口可用（19000-19999）")


def guess_type(path: str, alias: str = "") -> str:
    """按名字猜模型类型；用户可以在面板里改。"""
    s = (alias + " " + os.path.basename(path or "")).lower()
    if any(k in s for k in ("rerank", "reranker", "cross-encoder", "bge-rerank")):
        return "rerank"
    if any(k in s for k in ("embed", "embedding", "bge-m3", "gte-", "e5-", "jina-embeddings")):
        return "embed"
    return "chat"


def gpu_used_mib() -> int:
    """整卡已用显存（MiB）；WDDM 下拿不到每进程，只能用差值法估。"""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=4,
                           encoding="utf-8", errors="replace")
        first = (r.stdout or "").strip().splitlines()[0]
        return int(float(first.strip()))
    except Exception:
        return 0


def proc_mem(pid: int) -> Dict[str, int]:
    """返回该进程自己占的显存/内存（MiB）；拿不到就是 0。"""
    out = {"vram": 0, "ram": 0}
    if not pid:
        return out
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000 | 0x0400, False, int(pid))  # QUERY_LIMITED | VM_READ
        if h:
            try:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if ctypes.WinDLL("psapi").GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    out["ram"] = int(pmc.WorkingSetSize / 1048576)
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass
    try:
        r = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=4,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) == int(pid):
                out["vram"] = int(float(parts[1]))
                break
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ 槽位
class SlotStore:
    """槽位配置的读写（models-config.json → slots）。"""

    def __init__(self, cfg_path: str, load_cfg, save_cfg):
        self.path = cfg_path
        self._load = load_cfg
        self._save = save_cfg
        self.lock = threading.Lock()

    def load(self) -> List[Dict[str, Any]]:
        cfg = self._load()
        slots = cfg.get("slots")
        if not isinstance(slots, list) or not slots:
            slots = self._migrate(cfg)
        out = []
        for i, s in enumerate(slots):
            if not isinstance(s, dict):
                continue
            path = str(s.get("path") or "").strip()
            alias = str(s.get("alias") or "").strip() or (
                os.path.basename(path)[:-5] if path.lower().endswith(".gguf") else f"slot{i+1}")
            out.append({
                "id": str(s.get("id") or f"s{i+1}"),
                "alias": alias,
                "path": path,
                "engine": "llama" if str(s.get("engine")) == "llama" else "kvmem",
                "type": s.get("type") if s.get("type") in TYPES else guess_type(path, alias),
                "port": int(s.get("port") or (18200 + i)),
                "enabled": bool(s.get("enabled", True)),
                "params": dict(s.get("params") or {}),
                "extra_args": str(s.get("extra_args") or ""),
            })
        return out

    def _migrate(self, cfg) -> List[Dict[str, Any]]:
        """把旧的 models[]（以及 router 映射）迁成槽位，只做一次、不写盘。"""
        slots = []
        for i, m in enumerate(cfg.get("models") or []):
            path = str(m.get("path") or "")
            if not path:
                continue
            slots.append({"id": f"s{i+1}", "alias": os.path.basename(path)[:-5], "path": path,
                          "engine": "kvmem", "type": guess_type(path),
                          "port": 18200 + len(slots), "enabled": bool(m.get("enabled", True)),
                          "params": {}, "extra_args": ""})
        return slots

    def save(self, slots: List[Dict[str, Any]]):
        with self.lock:
            cfg = self._load()
            clean = []
            for i, s in enumerate(slots):
                path = str(s.get("path") or "").strip()
                if not path:
                    continue
                clean.append({
                    "id": str(s.get("id") or f"s{i+1}"),
                    "alias": str(s.get("alias") or "").strip() or os.path.basename(path)[:-5],
                    "path": os.path.normpath(path),
                    "engine": "llama" if str(s.get("engine")) == "llama" else "kvmem",
                    "type": s.get("type") if s.get("type") in TYPES else guess_type(path),
                    "port": int(s.get("port") or (18200 + i)),
                    "enabled": bool(s.get("enabled", True)),
                    "params": {str(k): v for k, v in (s.get("params") or {}).items()
                               if v not in (None, "", [])},
                    "extra_args": str(s.get("extra_args") or "").strip(),
                })
            cfg["slots"] = clean
            self._save(cfg)
            return clean


# ------------------------------------------------------------------ 宿主
class EngineHost:
    def __init__(self, slots: SlotStore, specs, log=print, on_change=None):
        self.store = slots
        self.specs = specs              # param_specs 模块
        self.log = log
        self.on_change = on_change or (lambda: None)
        self.inst: Dict[int, Dict[str, Any]] = {}   # 对外端口 -> 实例
        self.lock = threading.RLock()
        self.llama_exe = self._find_llama()
        # 实例从「启动中」变「就绪」时没有任何事件：靠这个巡检把网关模型列表补上
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        last = {}
        while True:
            time.sleep(2.0)
            try:
                cur = {p: self.alive(i) for p, i in list(self.inst.items())}
                if cur != last:
                    last = cur
                    self.on_change()
            except Exception:
                pass

    # -------------------------------------------------- 引擎路径
    def _find_llama(self) -> str:
        """llama.cpp 引擎路径：配置里的 llama_exe → 旧版的 router.exe → 常见位置。"""
        cfg = self.store._load()
        cand = str(cfg.get("llama_exe") or "").strip()
        if not cand:
            cand = str((cfg.get("router") or {}).get("exe") or "").strip()   # 旧配置兼容
        for p in (cand, LLAMA_EXE_DEFAULT, os.path.join(ENGINE_DIR, "llama-server.exe")):
            if p and os.path.isfile(p):
                return p
        return LLAMA_EXE_DEFAULT

    def engine_exe(self, engine: str) -> str:
        return self.llama_exe if engine == "llama" else KV_EXE

    @staticmethod
    def _resolve_model_path(path: str) -> str:
        """模型路径：绝对路径原样用；相对路径基于项目根目录解析（可移植）。"""
        path = str(path or "").strip()
        if not path:
            return path
        if os.path.isabs(path):
            return path
        # 相对路径：基于本项目根（engine_host.py 的上一级）
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.abspath(os.path.join(root, path))

    # -------------------------------------------------- 参数拼装
    def build_args(self, slot: Dict[str, Any], engine_port: int) -> List[str]:
        engine = slot["engine"]
        exe = self.engine_exe(engine)
        if not os.path.isfile(exe):
            raise RuntimeError("%s 引擎不存在：%s" % (ENGINE_LABEL[engine], exe))
        path = self._resolve_model_path(slot.get("path") or "")
        if not os.path.isfile(path):
            raise RuntimeError("模型文件不存在：%s" % path)
        args = [exe, "--model", path, "--host", "127.0.0.1", "--port", str(engine_port)]
        flags = self.specs.flag_map(engine)
        params = dict(slot.get("params") or {})
        # 槽位上的端口/上下文以面板为准，避免两处打架
        params.pop("port", None)
        if engine == "llama":
            params.pop("model", None)
            params.pop("host", None)
        self._validate(engine, params)
        # 「none」是部分下拉框的合法取值（--pooling none / --load-mode none / --split-mode none /
        # --rope-scaling none）；其余字段里的 "none" 仍按「未设置」丢弃
        none_ok = {sp["flag"] for sp in self.specs.specs_for(engine)
                   if sp.get("kind") == "select" and "none" in (sp.get("choices") or [])}
        for key, val in params.items():
            flag = flags.get(key) or ("--" + key.replace("_", "-"))
            # 面板会把空输入存成 ""，JSON 里也可能是 null —— 都当作「不传，用引擎默认」
            if val is None or val is False or val == "" or val == []:
                continue
            if isinstance(val, bool):
                args.append(flag)          # 开关型（True 才出现）
                continue
            sval = str(val).strip()
            if sval == "":
                continue
            if sval.lower() == "false":
                continue
            if sval.lower() == "true":
                args.append(flag)          # 前端 checkbox 传上来的字符串
                continue
            # pooling 的 "none" 是有效取值（不池化），不能当成"未设置"丢掉
            if sval.lower() == "none" and flag not in none_ok:
                continue
            args += [flag, sval]
        # llama 的 V 量化必须开 FA；KVMem 引擎自带处理，不用管
        if engine == "llama":
            kvv = str(params.get("cache_type_v") or "").lower()
            has_fa = any(a == "--flash-attn" for a in args)
            if kvv and kvv not in ("f16", "f32", "bf16") and not has_fa:
                args += ["--flash-attn", "on"]
        extra = str(slot.get("extra_args") or "").strip()
        if extra:
            import shlex
            bad = [x for x in ("--port", "-m", "--model")
                   if x in shlex.split(extra, posix=False)]
            if bad:
                raise RuntimeError("额外参数里不要写 %s（端口/模型路径归面板管）" % "、".join(bad))
            args += shlex.split(extra, posix=False)
        return args

    @staticmethod
    def _validate(engine: str, p: Dict[str, Any]):
        def num(k):
            try:
                return int(float(p.get(k)))
            except Exception:
                return None

        if engine == "kvmem":
            sink, budget, ctx = num("kvmem_sink_tokens"), num("kvmem_budget"), num("ctx_size")
            if sink is not None and sink < 4096:
                raise RuntimeError("sink-tokens 必须 ≥4096（引擎硬约束，否则请求会 500）")
            if sink is not None and budget is not None and sink >= budget:
                raise RuntimeError("sink-tokens 必须小于 budget（sink = budget 时每个请求都 500）")
            if ctx is not None and ctx < 4096:
                raise RuntimeError("上下文至少 4096")
            if budget is not None and budget <= 0:
                raise RuntimeError("budget 必须为正数")
        else:
            ctx = num("ctx_size")
            if ctx is not None and ctx < 0:
                raise RuntimeError("上下文不能为负（0 = 用模型自带）")

    # -------------------------------------------------- 启停
    def start(self, slot_id: str) -> Dict[str, Any]:
        slots = {s["id"]: s for s in self.store.load()}
        slot = slots.get(slot_id)
        if not slot:
            raise RuntimeError("找不到槽位 %s" % slot_id)
        port = int(slot["port"])
        with self.lock:
            cur = self.inst.get(port)
            if cur and cur["proc"].poll() is None:
                raise RuntimeError("端口 %d 上已有实例在跑（%s）" % (port, cur["alias"]))
            if tcp_open(port):
                raise RuntimeError("端口 %d 已被别的程序占用，换一个端口或先释放" % port)
            if any(i["proc"].poll() is None and i["alias"] == slot["alias"] for i in self.inst.values()):
                raise RuntimeError("「%s」已经在跑，先停掉再启动" % slot["alias"])

            gpu_base = gpu_used_mib()
            engine_port = free_port()
            args = self.build_args(slot, engine_port)
            env = os.environ.copy()
            env["PATH"] = ENGINE_DIR + os.pathsep + env.get("PATH", "")
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(args, cwd=ENGINE_DIR, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0, **kw)
            inst = {"proc": proc, "alias": slot["alias"], "engine": slot["engine"],
                    "type": slot["type"], "path": slot["path"], "port": port,
                    "engine_port": engine_port, "argv": args, "started_at": time.time(),
                    "ctx": self._ctx_of(slot), "slot_id": slot_id,
                    "log": [], "proxy": None, "gpu_base": gpu_base, "vram_est": None}
            self.inst[port] = inst
            threading.Thread(target=self._pump, args=(inst,), daemon=True).start()
            threading.Thread(target=self._proxy_up, args=(inst,), daemon=True).start()
            self.log("[%s] 启动 %s → :%d（引擎 :%d）" %
                     (ENGINE_LABEL[slot["engine"]], slot["alias"], port, engine_port))
        self.on_change()
        return self.view(inst)

    def _ctx_of(self, slot) -> int:
        try:
            return int(float((slot.get("params") or {}).get("ctx_size") or 0))
        except Exception:
            return 0

    def stop(self, port: int) -> bool:
        with self.lock:
            inst = self.inst.get(int(port))
            if not inst:
                return False
            try:
                if inst["proc"].poll() is None:
                    inst["proc"].terminate()
                    try:
                        inst["proc"].wait(timeout=8)
                    except Exception:
                        inst["proc"].kill()
            except Exception:
                pass
            px = inst.get("proxy")
            if px:
                try:
                    px.shutdown()
                    px.server_close()
                except Exception:
                    pass
            self.inst.pop(int(port), None)
        self.log("已停止 :%d（%s）" % (port, inst.get("alias")))
        self.on_change()
        return True

    def stop_all(self) -> int:
        n = 0
        for p in list(self.inst.keys()):
            if self.stop(p):
                n += 1
        return n

    def _pump(self, inst):
        proc = inst["proc"]
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else str(raw)
                if not line:
                    continue
                inst["log"].append(line)
                if len(inst["log"]) > 400:
                    del inst["log"][:100]
                self.log(line)
        except Exception:
            pass
        rc = proc.poll()
        self.log("%s 进程退出（code=%s）" % (inst.get("alias"), rc))

    # -------------------------------------------------- 代理（每个对外端口一个）
    def _proxy_up(self, inst):
        host = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "BonsaiAggregate/1.0"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _do(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                p = self.path.split("?")[0]
                if method == "GET" and p in ("/v1/models", "/models"):
                    return self._json(host.models_payload())
                if method == "GET" and p in ("/health", "/v1/health"):
                    return self._json({"status": "ok" if host.alive(inst) else "loading"})
                model = ""
                if body:
                    try:
                        model = str(json.loads(body.decode("utf-8", "replace")).get("model") or "")
                    except Exception:
                        model = ""
                target = host.resolve(model, default=inst)
                if not target:
                    return self._json({"error": {"code": "model_not_found",
                                                 "message": "没有这个模型在跑：%s" % model}}, 404)
                host.forward(self, method, target, body)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", inst["port"]), Handler)
            srv.daemon_threads = True
            inst["proxy"] = srv
            srv.serve_forever()
        except Exception as e:
            self.log("代理端口 %d 起不来：%s" % (inst["port"], e))

    def forward(self, handler, method, inst, body):
        """把请求转发给引擎内部端口，流式原样回传。"""
        url = "http://127.0.0.1:%d%s" % (inst["engine_port"], handler.path)
        headers = {k: v for k, v in handler.headers.items()
                   if k.lower() not in ("host", "content-length", "connection")}
        req = urllib.request.Request(url, data=body if method != "GET" else None,
                                     headers=headers, method=method)
        try:
            up = _OPENER.open(req, timeout=600)
        except urllib.error.HTTPError as e:
            up = e
        except Exception as e:
            return handler._json({"error": {"code": "upstream_unreachable",
                                            "message": str(e)}}, 502)
        try:
            status = getattr(up, "status", None) or up.code
            handler.send_response(status)
            ctype = up.headers.get("Content-Type", "application/json")
            handler.send_header("Content-Type", ctype)
            handler.send_header("Access-Control-Allow-Origin", "*")
            stream = "text/event-stream" in ctype
            if not stream and up.headers.get("Content-Length"):
                handler.send_header("Content-Length", up.headers["Content-Length"])
            elif stream:
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("Connection", "close")
            else:
                handler.send_header("Connection", "close")
            handler.end_headers()
            while True:
                chunk = up.read1(8192) if hasattr(up, "read1") else up.read(8192)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except Exception:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    # -------------------------------------------------- 查询
    def alive(self, inst) -> bool:
        proc = inst.get("proc")
        if not proc or proc.poll() is not None:
            return False
        return tcp_open(inst["engine_port"], 0.2)

    def resolve(self, model: str, default=None):
        """按模型名找在跑实例；空名 → default。"""
        with self.lock:
            items = [i for i in self.inst.values() if self.alive(i)]
            if not model:
                return default if (default and self.alive(default)) else (items[0] if items else None)
            for i in items:
                if i["alias"] == model or ("%s#%d" % (i["alias"], i["port"])) == model:
                    return i
            for i in items:
                if os.path.basename(i["path"]) == model:
                    return i
        return None

    def models_payload(self) -> Dict[str, Any]:
        data = []
        for inst in sorted(self.inst.values(), key=lambda x: x["port"]):
            up = self.alive(inst)
            data.append({
                "id": inst["alias"],
                "object": "model",
                "owned_by": "local",
                "created": int(inst["started_at"]),
                "status": {"value": "loaded" if up else "loading",
                           "args": inst["argv"]},
                "meta": {"engine": inst["engine"], "type": inst["type"],
                         "port": inst["port"], "ctx": inst["ctx"],
                         "running": up, "path": inst["path"]},
            })
        return {"object": "list", "data": data,
                "gateway_port": 0}

    @staticmethod
    def vram_from_log(inst) -> int:
        """WDDM 下 nvidia-smi 不给每进程显存，从引擎日志的 buffer size 累加。"""
        import re as _re
        seen: Dict[str, float] = {}
        for line in inst.get("log") or []:
            m = _re.search(r"([A-Za-z0-9_ ]{0,40}?buffer size)\s*=\s*([\d.]+)\s*MiB", line)
            if not m:
                continue
            key = m.group(1).strip()
            try:
                seen[key] = float(m.group(2))
            except Exception:
                pass
        return int(sum(seen.values()))

    def view(self, inst) -> Dict[str, Any]:
        mem = proc_mem(inst["proc"].pid if inst.get("proc") else 0)
        vram = mem["vram"] or self.vram_from_log(inst)
        if not vram and inst.get("gpu_base") and self.alive(inst):
            # 显存是异步提交的，而且整卡 only 有总量：只在「自己是最新启动的那个」
            # 且启动后 12 秒内采样，之后冻结，避免后启动的实例污染先启动的数字。
            live = [i for i in self.inst.values() if i["proc"].poll() is None]
            latest = max(live, key=lambda x: x["started_at"]) if live else None
            est = max(0, gpu_used_mib() - int(inst["gpu_base"]))
            if inst.get("vram_est") is None:
                inst["vram_est"] = est
            if latest is inst and (time.time() - inst["started_at"]) < 12:
                inst["vram_est"] = max(int(inst["vram_est"]), est)
            vram = int(inst.get("vram_est") or 0)
        return {"port": inst["port"], "alias": inst["alias"], "engine": inst["engine"],
                "engine_label": ENGINE_LABEL.get(inst["engine"], inst["engine"]),
                "type": inst["type"], "type_label": TYPE_LABEL.get(inst["type"], inst["type"]),
                "path": inst["path"], "pid": inst["proc"].pid if inst.get("proc") else 0,
                "running": inst.get("proc") is not None and inst["proc"].poll() is None,
                "ready": self.alive(inst), "ctx": inst["ctx"],
                "engine_port": inst["engine_port"], "argv": inst["argv"],
                "vram": vram, "ram": mem["ram"],
                "uptime": int(time.time() - inst["started_at"])}

    def status(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [self.view(i) for i in sorted(self.inst.values(), key=lambda x: x["port"])]

    def logs(self, port: int, n: int = 120) -> List[str]:
        inst = self.inst.get(int(port))
        return (inst or {}).get("log", [])[-n:]
