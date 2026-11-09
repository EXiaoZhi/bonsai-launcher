"""鉴权、限流、日志脱敏。

安全底线（对应验收"安全"一栏）
- 令牌：环境变量 BONSAI_TOKEN 优先；否则落盘 gateway/data/token.txt（首次自动生成 32 字节）。
- 传输：只走 Authorization 头或 HttpOnly+SameSite=Strict Cookie，**不接受 URL query token**。
- 比对：hmac.compare_digest，避免时序侧信道。
- 脱敏：日志流里不得出现 API key / Bearer / 绝对路径 / 请求体原文。
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from typing import Dict, Tuple

COOKIE_NAME = "bonsai_gw"

_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[^\s\",}]{6,}"), r"\1***"),
    (re.compile(r"\b[A-H]:\\[^\s\"'<>|]+"), "<path>"),
    (re.compile(r"(?<![\w])/(?:home|Users)/[^\s\"']+"), "<path>"),
    (re.compile(r"(?i)(prompt|content)\"\s*:\s*\"[^\"]{24,}"), r'\1":"<已脱敏>"'),
    (re.compile(r"[A-Za-z0-9+/]{120,}={0,2}"), "<blob>"),
]
_BODY_HINT = re.compile(r'"(messages|input|prompt)"\s*:')


def redact(line: str) -> str:
    if not line:
        return line
    if _BODY_HINT.search(line):
        # 整行看起来是序列化请求体：直接丢弃内容，只保留标记，避免 prompt 原文泄漏
        return "[已脱敏：请求体]"
    out = line
    for pat, rep in _PATTERNS:
        out = pat.sub(rep, out)
    return out


def load_token(data_dir: str) -> str:
    env = os.environ.get("BONSAI_TOKEN", "").strip()
    if env:
        return env
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "token.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    with open(path, "w", encoding="utf-8") as f:
        f.write(tok + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return tok


def check_token(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)


class RateLimiter:
    """简单令牌桶：per_minute 匀速补充，burst 决定瞬时上限。"""

    def __init__(self, per_minute: int = 120, burst: int = 20):
        self.rate = max(1.0, per_minute / 60.0)
        self.burst = max(1, burst)
        self._lock = threading.Lock()
        self._buckets: Dict[str, Tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.time()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - cost, now)
            return True
