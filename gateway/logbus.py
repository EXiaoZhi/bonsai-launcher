"""日志总线：ring buffer + 订阅队列 + 文件跟随（tail -f 风格）。

上游进程由 deploy/ 启动，stdout/stderr 重定向到文件；网关只读文件、脱敏后转 SSE。
这样"网关不启动进程"的边界得以保持，同时仍能把 Router/实例日志送到前端。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .security import redact


class LogBus:
    def __init__(self, maxlen: int = 2000):
        self._lines: Deque[Dict] = deque(maxlen=maxlen)
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, source: str, raw: str):
        line = redact(str(raw).rstrip("\r\n"))
        if line == "":
            return
        with self._lock:
            self._seq += 1
            item = {"id": self._seq, "ts": int(time.time()), "source": source, "line": line}
            self._lines.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except Exception:
                    pass

    def tail(self, n: int = 200, after_id: int = 0) -> List[Dict]:
        with self._lock:
            items = [x for x in self._lines if x["id"] > after_id]
        return items[-n:]

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


class FileTailer:
    """按 inode/size 增量读取；被截断或轮转时从头续读。"""

    def __init__(self, bus: LogBus, name: str, path: str, poll: float = 0.5):
        self.bus, self.name, self.path, self.poll = bus, name, path, poll
        self._pos = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        open(self.path, "a", encoding="utf-8").close()
        self._pos = os.path.getsize(self.path)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                size = os.path.getsize(self.path)
                if size < self._pos:
                    self._pos = 0
                if size > self._pos:
                    with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self._pos)
                        chunk = f.read()
                        self._pos = f.tell()
                    for line in chunk.splitlines():
                        self.bus.publish(self.name, line)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            self._stop.wait(self.poll)


def start_tailers(bus: LogBus, files: List[Dict]) -> List[FileTailer]:
    out = []
    for item in files or []:
        t = FileTailer(bus, str(item.get("name", "log")), str(item.get("path", "")))
        t.start()
        out.append(t)
    return out
