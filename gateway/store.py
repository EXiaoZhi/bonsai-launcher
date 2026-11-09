"""持久化层（sqlite3，标准库）。

设计要点
- 一条连接/线程（threading.local）+ WAL，避免 http.server 多线程下的共享连接问题。
- 会话 / 消息 / 摘要块 / 片段库 / 用量 / 审计 六张表，均按 user_id 或 session_id 隔离。
- 消息永不物理删除（archived 标记），压缩只做"视图折叠"，满足"可逆 + 原始消息仍在存储"的验收要求。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  effort TEXT,
  compress INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, pinned DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  thinking TEXT NOT NULL DEFAULT '',
  think_ms INTEGER NOT NULL DEFAULT 0,
  effort TEXT NOT NULL DEFAULT '',
  versions TEXT NOT NULL DEFAULT '[]',
  v_index INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS blocks (
  block_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  parent_ids TEXT NOT NULL DEFAULT '[]',
  token_count INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  source_range TEXT NOT NULL DEFAULT '[]',
  sha256 TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_session ON blocks(session_id, created_at);

CREATE TABLE IF NOT EXISTS snippets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, model)
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""


def _decode_msg(row) -> Dict[str, Any]:
    """行 → 字典，content / versions 反序列化；versions 缺失时按单版本补齐。"""
    d = dict(row)
    try:
        d["content"] = json.loads(d["content"])
    except Exception:
        pass
    try:
        vs = json.loads(d.get("versions") or "[]")
        d["versions"] = vs if isinstance(vs, list) else []
    except Exception:
        d["versions"] = []
    if not d["versions"]:
        d["versions"] = [{"content": d.get("content", ""), "thinking": d.get("thinking", ""),
                          "think_ms": int(d.get("think_ms") or 0), "effort": d.get("effort", ""),
                          "created_at": d.get("created_at") or 0}]
    d["v_index"] = int(d.get("v_index") or 0)
    return d


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._local = threading.local()
        self._init()

    # ---------------------------------------------------------------- 连接
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init(self):
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self):
        """老库补列：思考内容/耗时/档位/多版本 + 会话的上下文优化开关（老数据不动）。"""
        try:
            scols = {r[1] for r in self._exec("PRAGMA table_info(sessions)").fetchall()}
            if "compress" not in scols:
                self._exec("ALTER TABLE sessions ADD COLUMN compress INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            cols = {r[1] for r in self._exec("PRAGMA table_info(messages)").fetchall()}
            for name, ddl in (("thinking", "TEXT NOT NULL DEFAULT ''"),
                              ("think_ms", "INTEGER NOT NULL DEFAULT 0"),
                              ("effort", "TEXT NOT NULL DEFAULT ''"),
                              ("versions", "TEXT NOT NULL DEFAULT '[]'"),
                              ("v_index", "INTEGER NOT NULL DEFAULT 0")):
                if name not in cols:
                    self._exec(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
        except Exception:
            pass

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        return self.db.execute(sql, args)

    # ---------------------------------------------------------------- 会话
    def create_session(self, user_id: str, model: str, effort: str, title: str = "新对话") -> Dict[str, Any]:
        sid = "s_" + uuid.uuid4().hex[:16]
        ts = now()
        self._exec("INSERT INTO sessions(id,user_id,title,pinned,model,effort,created_at,updated_at)"
                   " VALUES(?,?,?,0,?,?,?,?)", (sid, user_id, title, model, effort, ts, ts))
        return self.get_session(sid, user_id)

    def get_session(self, sid: str, user_id: str) -> Optional[Dict[str, Any]]:
        r = self._exec("SELECT * FROM sessions WHERE id=? AND user_id=?", (sid, user_id)).fetchone()
        return dict(r) if r else None

    def list_sessions(self, user_id: str, query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        if query:
            q = f"%{query}%"
            rows = self._exec("SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msg_count"
                              " FROM sessions s WHERE s.user_id=? AND s.title LIKE ?"
                              " ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?", (user_id, q, limit)).fetchall()
        else:
            rows = self._exec("SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS msg_count"
                              " FROM sessions s WHERE s.user_id=?"
                              " ORDER BY s.pinned DESC, s.updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def update_session(self, sid: str, user_id: str, **fields) -> bool:
        allowed = {"title", "pinned", "model", "effort", "compress"}
        sets, args = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            args.append(int(v) if k in ("pinned", "compress") else v)
        if not sets:
            return False
        sets.append("updated_at=?")
        args.append(now())
        args += [sid, user_id]
        cur = self._exec(f"UPDATE sessions SET {', '.join(sets)} WHERE id=? AND user_id=?", tuple(args))
        return cur.rowcount > 0

    def delete_sessions(self, ids: List[str], user_id: str) -> int:
        """批量删除：逐个删（每条都要清 messages/blocks/usage），返回真正删掉的数量。"""
        n = 0
        for sid in ids or []:
            if self.delete_session(str(sid), user_id):
                n += 1
        return n

    def delete_session(self, sid: str, user_id: str) -> bool:
        if not self.get_session(sid, user_id):
            return False
        self._exec("DELETE FROM messages WHERE session_id=?", (sid,))
        self._exec("DELETE FROM blocks WHERE session_id=?", (sid,))
        self._exec("DELETE FROM usage WHERE session_id=?", (sid,))
        self._exec("DELETE FROM sessions WHERE id=? AND user_id=?", (sid, user_id))
        return True

    def touch_session(self, sid: str):
        self._exec("UPDATE sessions SET updated_at=? WHERE id=?", (now(), sid))

    # ---------------------------------------------------------------- 消息
    def append_message(self, sid: str, role: str, content: Any, tokens: int = 0,
                       archived: bool = False, thinking: str = "",
                       think_ms: int = 0, effort: str = "", versions: Optional[List] = None,
                       v_index: int = 0) -> Dict[str, Any]:
        seq = (self._exec("SELECT COALESCE(MAX(seq),0)+1 AS n FROM messages WHERE session_id=?",
                          (sid,)).fetchone()["n"])
        ts = now()
        if versions is None:
            versions = [{"content": content, "thinking": thinking or "",
                         "think_ms": int(think_ms or 0), "effort": effort or "", "created_at": ts}]
        v_index = max(0, min(int(v_index or 0), len(versions) - 1))
        cur = self._exec("INSERT INTO messages(session_id,seq,role,content,tokens,archived,created_at,"
                         "thinking,think_ms,effort,versions,v_index) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, seq, role, json.dumps(content, ensure_ascii=False), tokens, int(archived), ts,
                          thinking or "", int(think_ms or 0), effort or "",
                          json.dumps(versions, ensure_ascii=False), v_index))
        self.touch_session(sid)
        return {"id": cur.lastrowid, "session_id": sid, "seq": seq, "role": role,
                "content": content, "tokens": tokens, "archived": archived, "created_at": ts,
                "thinking": thinking or "", "think_ms": int(think_ms or 0), "effort": effort or "",
                "versions": versions, "v_index": v_index}

    def get_message(self, mid: int) -> Optional[Dict[str, Any]]:
        r = self._exec("SELECT * FROM messages WHERE id=?", (int(mid),)).fetchone()
        return _decode_msg(r) if r else None

    def update_message(self, mid: int, *, content: Any, thinking: str, think_ms: int,
                       effort: str, versions: List, v_index: int) -> Optional[Dict[str, Any]]:
        """整行覆盖（用于"重新生成"：追加一个版本并切到它）。"""
        self._exec("UPDATE messages SET content=?, thinking=?, think_ms=?, effort=?, versions=?, "
                   "v_index=? WHERE id=?",
                   (json.dumps(content, ensure_ascii=False), thinking or "", int(think_ms or 0),
                    effort or "", json.dumps(versions, ensure_ascii=False), int(v_index), int(mid)))
        return self.get_message(mid)

    def set_message_version(self, mid: int, v_index: int) -> Optional[Dict[str, Any]]:
        """切换到第 N 个版本：content/thinking/effort 全部跟着换，保证"上下文=当前版本"。"""
        m = self.get_message(mid)
        if not m:
            return None
        vs = m.get("versions") or []
        if not vs:
            return m
        i = max(0, min(int(v_index), len(vs) - 1))
        v = vs[i]
        return self.update_message(mid, content=v.get("content", ""), thinking=v.get("thinking", ""),
                                   think_ms=v.get("think_ms", 0), effort=v.get("effort", ""),
                                   versions=vs, v_index=i)

    def list_messages(self, sid: str, include_archived: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE session_id=?"
        if not include_archived:
            sql += " AND archived=0"
        rows = self._exec(sql + " ORDER BY seq ASC", (sid,)).fetchall()
        out = []
        for r in rows:
            d = _decode_msg(r)
            try:
                d["content"] = json.loads(d["content"])
            except Exception:
                pass
            out.append(d)
        return out

    def archive_messages(self, ids: List[int]) -> int:
        if not ids:
            return 0
        q = ",".join("?" * len(ids))
        cur = self._exec(f"UPDATE messages SET archived=1 WHERE id IN ({q})", tuple(ids))
        return cur.rowcount

    def messages_by_ids(self, ids: List[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self._exec(f"SELECT * FROM messages WHERE id IN ({q}) ORDER BY seq ASC", tuple(ids)).fetchall()
        out = []
        for r in rows:
            d = _decode_msg(r)
            try:
                d["content"] = json.loads(d["content"])
            except Exception:
                pass
            out.append(d)
        return out

    # ---------------------------------------------------------------- 摘要块
    def save_block(self, sid: str, block: Dict[str, Any]) -> Dict[str, Any]:
        self._exec("INSERT OR REPLACE INTO blocks(block_id,session_id,tier,parent_ids,token_count,content,"
                   "source_range,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (block["block_id"], sid, block["tier"],
                    json.dumps(block.get("parent_ids", []), ensure_ascii=False),
                    int(block.get("token_count", 0)),
                    json.dumps(block.get("content", {}), ensure_ascii=False),
                    json.dumps(block.get("source_range", []), ensure_ascii=False),
                    block.get("sha256", ""), int(block.get("created_at", now()))))
        return block

    def get_block(self, sid: str, block_id: str) -> Optional[Dict[str, Any]]:
        r = self._exec("SELECT * FROM blocks WHERE session_id=? AND block_id=?", (sid, block_id)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("parent_ids", "content", "source_range"):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
        return d

    def list_blocks(self, sid: str) -> List[Dict[str, Any]]:
        rows = self._exec("SELECT * FROM blocks WHERE session_id=? ORDER BY created_at ASC, rowid ASC",
                          (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("parent_ids", "content", "source_range"):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
            out.append(d)
        return out

    # ---------------------------------------------------------------- 片段库
    def snippets(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self._exec("SELECT * FROM snippets WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def save_snippet(self, user_id: str, sid: Optional[str], title: str, body: str) -> Dict[str, Any]:
        ts = now()
        if sid:
            self._exec("UPDATE snippets SET title=?, body=?, updated_at=? WHERE id=? AND user_id=?",
                       (title, body, ts, sid, user_id))
        else:
            sid = "sn_" + uuid.uuid4().hex[:12]
            self._exec("INSERT INTO snippets(id,user_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (sid, user_id, title, body, ts, ts))
        return {"id": sid, "title": title, "body": body, "updated_at": ts}

    def delete_snippet(self, user_id: str, sid: str) -> bool:
        return self._exec("DELETE FROM snippets WHERE id=? AND user_id=?", (sid, user_id)).rowcount > 0

    # ---------------------------------------------------------------- 用量
    def add_usage(self, sid: str, model: str, prompt_tokens: int, completion_tokens: int,
                  reasoning_tokens: int = 0) -> Dict[str, Any]:
        ts = now()
        self._exec("INSERT INTO usage(session_id,model,prompt_tokens,completion_tokens,reasoning_tokens,requests,updated_at)"
                   " VALUES(?,?,?,?,?,1,?)"
                   " ON CONFLICT(session_id,model) DO UPDATE SET"
                   " prompt_tokens=prompt_tokens+excluded.prompt_tokens,"
                   " completion_tokens=completion_tokens+excluded.completion_tokens,"
                   " reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens,"
                   " requests=requests+1, updated_at=excluded.updated_at",
                   (sid, model, prompt_tokens, completion_tokens, reasoning_tokens, ts))
        return self.usage(sid, model)

    def usage(self, sid: str, model: str) -> Dict[str, Any]:
        r = self._exec("SELECT * FROM usage WHERE session_id=? AND model=?", (sid, model)).fetchone()
        return dict(r) if r else {"session_id": sid, "model": model, "prompt_tokens": 0,
                                  "completion_tokens": 0, "reasoning_tokens": 0, "requests": 0}

    def usage_all(self, sid: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._exec("SELECT * FROM usage WHERE session_id=?", (sid,)).fetchall()]

    # ---------------------------------------------------------------- 审计
    def audit(self, actor: str, action: str, detail: str = ""):
        self._exec("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                   (now(), actor, action, detail[:2000]))

    def audit_tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._exec(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
