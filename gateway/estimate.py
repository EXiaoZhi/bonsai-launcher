"""token 估算。

上游只在响应体里给 usage，压缩决策必须"先"知道大小，所以需要一个本地估算器。
经验系数（中文为主，含代码/英文）：
- CJK 字符 ≈ 1 token / 字
- ASCII 约 3.6 字符 / token（代码与英文混排实测区间 3.2~4.0，取中位）
误差 ~±12%，足以驱动 70%/90% 阈值；对外展示一律标注"估算"。
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WS = re.compile(r"[ \t]{2,}")


def estimate_text(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return max(1, int(math.ceil(cjk * 1.0 + rest / 3.6)))


def content_to_text(content: Any) -> str:
    """OpenAI content 可能是 str 或 [{'type':'text'|'image_url', ...}]"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif p.get("type") == "image_url":
                parts.append("[图片]")
        return "\n".join(parts)
    return str(content)


def message_tokens(msg: Dict[str, Any]) -> int:
    # 每条消息固定开销（role + 分隔符）
    return 4 + estimate_text(content_to_text(msg.get("content"))) + estimate_text(str(msg.get("name") or ""))


def messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in messages)


def squash_whitespace(text: str) -> str:
    """无损清理：多空格/多空行归一，视觉不变、token 变少。"""
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
