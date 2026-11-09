"""思考强度（effort）解析与上游请求体构建。

契约（对客户端的承诺）
- 客户端只说 `reasoning_effort`（none|low|medium|high）+ 可选 `enable_thinking`。
- `enable_thinking:false` 最高优先级，等价 effort="none"。
- 未提供 → 用模型的 thinking.default。
- 模型不支持的档位 → 400 unsupported_effort + supported 列表，**不静默降级**。

事实依据（docs/adr/facts.md）
- F4：`chat_template_kwargs.reasoning_effort` 在请求级生效，none/low/xhigh 可观测差异。
- F5：该二进制同时识别**顶层** reasoning_effort 与 enable_thinking；因此网关必须白名单重建
  请求体，绝不能透传客户端 body，否则会绕过能力校验与审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .config import Model


class EffortError(Exception):
    def __init__(self, requested: str, supported: List[str]):
        super().__init__(f"unsupported effort: {requested}")
        self.requested = requested
        self.supported = supported

    def payload(self) -> Dict[str, Any]:
        return {"error": {"code": "unsupported_effort",
                          "message": f"模型不支持思考强度 '{self.requested}'",
                          "supported_efforts": self.supported,
                          "retryable": False}}


def resolve(model: Model, payload: Dict[str, Any]) -> Tuple[str, bool]:
    """返回 (effort, thinking_on)"""
    raw_enable = payload.get("enable_thinking", None)
    requested = payload.get("reasoning_effort", None)

    if raw_enable is False:
        return "none", False
    if requested is None or str(requested).strip() == "":
        eff = model.thinking.default if model.thinking.supported else "none"
    else:
        eff = str(requested).strip().lower()

    if eff not in ("none", "low", "medium", "high"):
        raise EffortError(eff, model.thinking.efforts or ["none"])

    if not model.thinking.supported:
        # 非思考模型只接受"不思考"，显式要档位一律 400（不静默降级）
        if eff != "none":
            raise EffortError(eff, ["none"])
        return "none", False

    if eff not in model.thinking.efforts:
        raise EffortError(eff, model.thinking.efforts)
    return eff, eff != "none"


# 模板在 low / xhigh 档会注入英文提示（见 facts F12），模型随之用英文思考；
# 补一句"跟着用户语言走"的系统提示来抵消，且只在会注入英文的档位加，避免无谓改动前缀。
DEFAULT_LANG_HINT = ("[语言要求] 用与用户提问相同的语言思考和作答。用户用中文时，"
                     "思考过程也必须用中文；用户用英文时再用英文。")


def _apply_lang_hint(messages: List[Dict[str, Any]], hint: str) -> List[Dict[str, Any]]:
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        msgs[0]["content"] = (msgs[0]["content"] + "\n\n" + hint).strip()
    else:
        msgs.insert(0, {"role": "system", "content": hint})
    return msgs


def upstream_body(model: Model, messages: List[Dict[str, Any]], effort: str,
                  thinking_on: bool, max_tokens: int, stream: bool = True,
                  system_hint: str = "") -> Dict[str, Any]:
    """白名单重建上游请求体：只放这里列出的字段。"""
    msgs = messages
    if system_hint and thinking_on and effort in ("low", "high"):
        msgs = _apply_lang_hint(messages, system_hint)
    body: Dict[str, Any] = {
        "model": model.upstream.model_id,
        "messages": msgs,
        "stream": bool(stream),
        "temperature": float(model.sampling.get("temp", 1.0)),
        "top_p": float(model.sampling.get("top_p", 0.95)),
        "top_k": int(model.sampling.get("top_k", 20)),
    }
    pp = model.sampling.get("presence_penalty", 0)
    if pp:
        body["presence_penalty"] = float(pp)
    fp = model.sampling.get("frequency_penalty", 0)
    if fp:
        body["frequency_penalty"] = float(fp)
    if max_tokens and max_tokens > 0:
        body["max_tokens"] = int(max_tokens)
    if stream:
        body["stream_options"] = {"include_usage": True}

    if model.thinking.supported and model.thinking.transport == "chat_template_kwargs":
        # 注意：只发 kwargs，不发顶层字段（事实 F5）
        body["chat_template_kwargs"] = {"enable_thinking": bool(thinking_on),
                                        "reasoning_effort": effort}
    return body


def summarize_body(model: Model, messages: List[Dict[str, Any]], instruction: str,
                   effort: str, max_tokens: int) -> Dict[str, Any]:
    """摘要请求：逐字沿用主请求的消息前缀，只把指令**追加在末尾**（保前缀缓存）。"""
    msgs = list(messages) + [{"role": "user", "content": instruction}]
    body = upstream_body(model, msgs, effort, effort != "none", max_tokens, stream=False)
    body["stream"] = False
    body.pop("stream_options", None)
    return body
