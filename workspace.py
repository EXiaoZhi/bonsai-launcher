#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作区文件操作、工具授权、对话历史。"""
import json
import os
import re
import time
import urllib.request

from core import *

def ws_config_load():
    try:
        with open(WS_CONFIG, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and str(d.get("dir", "")).strip():
            return str(d["dir"]).strip()
    except Exception:
        pass
    return "chat-workspace"   # 默认：包内 chat-workspace 目录


def ws_config_save(d):
    tmp = WS_CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"dir": d}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WS_CONFIG)


def workspace_dir():
    """文件操作空间根目录（用户可选择授权），返回绝对路径并确保存在"""
    d = ws_config_load()
    root = d if os.path.isabs(d) else os.path.join(BASE, d)
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    return root


def perms_load():
    try:
        with open(PERMS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("always"), list):
            return set(d["always"])
    except Exception:
        pass
    return set()


def perms_add_always(tool_id):
    a = perms_load()
    a.add(tool_id)
    tmp = PERMS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"always": sorted(a)}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PERMS_FILE)


def perms_clear_always(tool_id):
    a = perms_load()
    if tool_id in a:
        a.discard(tool_id)
        tmp = PERMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"always": sorted(a)}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERMS_FILE)


def _safe_ws_name(name):
    """文件操作空间内的安全文件名：不允许任何路径分隔/穿越"""
    name = str(name or "").strip()
    if not name or name in (".", ".."):
        raise ValueError("文件名不能为空")
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name) or ".." in name:
        raise ValueError("文件名包含非法字符")
    return name


def ws_list():
    out = []
    try:
        for fn in sorted(os.listdir(workspace_dir())):
            fp = os.path.join(workspace_dir(), fn)
            if os.path.isfile(fp):
                st = os.stat(fp)
                out.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
    except Exception:
        pass
    return out


def ws_read(name, maxchars=100000):
    name = _safe_ws_name(name)
    fp = os.path.join(workspace_dir(), name)
    if not os.path.isfile(fp):
        raise FileNotFoundError(name)
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        return f.read(maxchars)


def ws_write(name, content):
    name = _safe_ws_name(name)
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = bytes(content)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("文件超过 20MB 上限")
    fp = os.path.join(workspace_dir(), name)
    with open(fp, "wb") as f:
        f.write(data)
    return len(data)


def ws_delete(name):
    name = _safe_ws_name(name)
    fp = os.path.join(workspace_dir(), name)
    if os.path.isfile(fp):
        os.remove(fp)


# ---------------- 对话历史（落盘 chat-history/*.json）
def _hist_path(cid):
    cid = re.sub(r"[^A-Za-z0-9_-]", "", str(cid))[:64]
    if not cid:
        raise ValueError("bad id")
    return os.path.join(CHAT_DIR, cid + ".json")


def _first_user_text(messages):
    """取对话里第一条用户文本，用于给"新对话"补一个可读标题"""
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            t = c
        elif isinstance(c, list):
            t = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text")
        else:
            t = ""
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    return ""


def hist_list():
    items = []
    try:
        for fn in os.listdir(CHAT_DIR):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(CHAT_DIR, fn), "r", encoding="utf-8") as f:
                        d = json.load(f)
                    title = str(d.get("title") or "").strip()
                    if title in ("", "新对话", "未命名"):
                        title = _first_user_text(d.get("messages", []))[:24] or "新对话"
                    items.append({"id": d.get("id", fn[:-5]), "title": title,
                                  "updated": d.get("updated", 0), "count": len(d.get("messages", []))})
                except Exception:
                    continue
    except Exception:
        pass
    items.sort(key=lambda x: -x["updated"])
    return items


def hist_save(cid, title, messages):
    old = {}
    try:
        with open(_hist_path(cid), "r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass
    now = int(time.time())
    d = {"id": cid, "title": title or old.get("title") or "新对话",
         "created": old.get("created", now), "updated": now, "messages": messages}
    for k, v in old.items():
        if k not in d:
            d[k] = v   # 保留 titled 等标记字段
    tmp = _hist_path(cid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _hist_path(cid))
    return d


def hist_load(cid):
    with open(_hist_path(cid), "r", encoding="utf-8") as f:
        return json.load(f)


def hist_delete(cid):
    try:
        os.remove(_hist_path(cid))
    except Exception:
        pass


def hist_auto_title(cid):
    """后台线程：用户在新对话中第一次提问后，由 AI 生成标题（整个对话仅一次）"""
    try:
        d = hist_load(cid)
        if d.get("titled"):
            return
        msgs = d.get("messages", [])
        first_user = next((m for m in msgs if m.get("role") == "user"), None)
        if not first_user:
            return
        c = first_user.get("content", "")
        text = c if isinstance(c, str) else " ".join(
            x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        port = first_engine_port()
        if not engine_ready(port):
            port = None
        if not port:
            return   # 引擎未就绪，下次保存时重试（titled 未置位）
        conv = [{"role": "user", "content":
                 "用不超过12个字概括以下用户问题的主题，作为对话标题。只输出标题本身，不要标点、引号或任何解释：\n"
                 + str(text)[:500]}]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps({"model": "local", "messages": conv, "temperature": 0.1,
                             "max_tokens": 32}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with _DIRECT_OPENER.open(req, timeout=120) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        t = (j["choices"][0]["message"].get("content") or "").strip().strip('"“”').splitlines()[0][:24]
        if t:
            hist_save(cid, t, msgs)
            d2 = hist_load(cid)
            d2["titled"] = True
            tmp = _hist_path(cid) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d2, f, ensure_ascii=False)
            os.replace(tmp, _hist_path(cid))
    except Exception:
        pass