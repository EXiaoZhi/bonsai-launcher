"""上游客户端：与 llama.cpp 系服务通信（direct 端点或 Router 端点）。

事实依据
- F1/F2：PrismML 量化只能由 KVMem fork 单实例服务，Router 只承载标准 GGUF。
  因此端点解析分两态：direct（端口直连）与 router（同端点 + 不同 model 字段）。
- F7：上游 model id 由文件名决定，必须由配置显式映射，不能靠别名。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import Model

# 本地回环一律绕过系统代理（HTTP_PROXY 会把 127.0.0.1 也拐去代理）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class UpstreamError(Exception):
    def __init__(self, message: str, code: str = "upstream_error", status: int = 502,
                 retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


def endpoint_of(model: Model) -> Tuple[str, bool]:
    """返回 (base_url, 是否需要在上游 body 里带 model 字段)——两种模式都要带。"""
    return model.upstream.base_url, True


def health(model: Model, timeout: float = 2.5) -> bool:
    base, _ = endpoint_of(model)
    try:
        with _OPENER.open(f"{base}/models", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def rerank(url: str, payload: Dict[str, Any], timeout: float = 120) -> Dict[str, Any]:
    """调用上游 rerank 端点（llama.cpp router 的 /v1/rerank，Cohere 风格）。

    返回 {"ok", "status", "data"}；失败时 data 里尽量带上游错误体，便于前端显示。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return {"ok": True, "status": r.status,
                    "data": json.loads(r.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as e:
        body: Dict[str, Any] = {}
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        msg = ""
        if isinstance(body.get("error"), dict):
            msg = str(body["error"].get("message") or "")
        return {"ok": False, "status": e.code, "data": body,
                "error": msg or ("HTTP %d" % e.code)}
    except Exception as e:
        return {"ok": False, "status": 502, "data": {}, "error": str(e)}


def load_model(base_url: str, model: str, timeout: float = 180) -> Dict[str, Any]:
    """让 Router 按需加载某个模型（POST /models/load）。

    Router 若带 --no-models-autoload，第一次 rerank 会回 "model is not loaded"，
    这里补一次显式加载再重试，避免用户还要手动去点加载。
    """
    data = json.dumps({"model": model}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/models/load", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "error": "HTTP %d %s" % (e.code, detail).strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def wait_loaded(base_url: str, model: str, timeout: float = 180) -> bool:
    """等 Router 把某个模型加载完（/v1/models 里 status 变成 loaded）。"""
    import time as _t
    base = base_url.rstrip("/")
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        try:
            with _OPENER.open(base + "/v1/models", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8") or "{}")
            for m in data.get("data") or []:
                if str(m.get("id")) == model:
                    st = m.get("status") or {}
                    val = str(st.get("value") if isinstance(st, dict) else st).lower()
                    if val in ("loaded", "ready", "sleeping"):
                        return True
        except Exception:
            pass
        _t.sleep(1.0)
    return False


def list_models(base_url: str, timeout: float = 5.0) -> List[str]:
    """列出上游当前暴露的模型 id（Router 的 /v1/models）。失败返回空列表。"""
    try:
        with _OPENER.open(base_url.rstrip("/") + "/v1/models", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        return [str(x.get("id")) for x in (data.get("data") or []) if x.get("id")]
    except Exception:
        return []


def complete(model: Model, body: Dict[str, Any], timeout: float = 120) -> Dict[str, Any]:
    """非流式补全（摘要用）。失败抛 UpstreamError。"""
    base, _ = endpoint_of(model)
    req = urllib.request.Request(f"{base}/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise UpstreamError(f"上游 HTTP {e.code}: {detail}", "upstream_http", 502, e.code >= 500)
    except socket.timeout:
        raise UpstreamError("上游超时", "upstream_timeout", 504, True)
    except Exception as e:
        raise UpstreamError(f"上游不可达：{e}", "upstream_unreachable", 503, True)


def stream(model: Model, body: Dict[str, Any], timeout: float = 600) -> Iterator[Dict[str, Any]]:
    """流式解析上游 SSE，产出规范化事件：
    {"type":"delta","role":"assistant|thinking","text":...}
    {"type":"tool_call","name":...,"arguments":...}
    {"type":"usage","usage":{...}}
    """
    base, _ = endpoint_of(model)
    req = urllib.request.Request(f"{base}/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "text/event-stream"})
    try:
        resp = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise UpstreamError(f"上游 HTTP {e.code}: {detail}", "upstream_http", 502, e.code >= 500)
    except Exception as e:
        raise UpstreamError(f"上游不可达：{e}", "upstream_unreachable", 503, True)

    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            usage = obj.get("usage")
            if usage:
                yield {"type": "usage", "usage": usage}
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("reasoning_content"):
                    yield {"type": "delta", "role": "thinking", "text": d["reasoning_content"]}
                if d.get("content"):
                    yield {"type": "delta", "role": "assistant", "text": d["content"]}
                for tc in d.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        yield {"type": "tool_call", "name": fn.get("name"),
                               "arguments": fn.get("arguments", "")}
