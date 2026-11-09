#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎流式调用与 Agent 主循环。"""
import json
import threading
import urllib.request

from core import *
from tools import collect_tools, parse_tool_call, execute_tool, TOOL_PROMPT_TMPL
from workspace import perms_load, perms_add_always

def llm_stream(port, messages, temperature=0.7, max_tokens=0):
    """流式请求引擎，逐 token yield (kind, text)；kind 为 text 或 think（思考内容）"""
    payload = {"model": "local", "messages": messages,
               "temperature": float(temperature), "stream": True}
    if max_tokens and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=data, headers={"Content-Type": "application/json"})
    with _DIRECT_OPENER.open(req, timeout=1200) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload_s = line[5:].strip()
            if payload_s == "[DONE]":
                return
            try:
                j = json.loads(payload_s)
            except Exception:
                continue
            choices = j.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            tok = delta.get("content")
            if tok:
                yield "text", tok
                continue
            rt = delta.get("reasoning_content")
            if rt:
                yield "think", rt


def run_agent(emit, messages, opts):
    """Agent 主循环：流式输出 + 工具调用 + 权限确认。emit(event_dict)。返回最终回复文本。"""
    port = first_engine_port()
    temperature = float(opts.get("temperature", 0.7))
    max_tokens = int(opts.get("max_tokens", 0) or 0)
    want_tools = bool(opts.get("use_tools"))
    permission = bool(opts.get("permission", True))

    tools = []
    if want_tools:
        try:
            tools = [t for t in collect_tools() if t[2] != "error"]
        except Exception as e:
            emit({"type": "warn", "text": "工具加载失败：" + str(e)})
    # 渐进式披露：内置文件工具很小，保留完整 schema；MCP 工具只进「目录」（名称+一句话说明），
    # 参数格式用 mcp_help 按需查、执行统一走 mcp_call —— 避免每个请求都背全部 JSON Schema。
    mcp_meta = [
        {"name": "mcp_help", "desc": "查看某个 MCP 工具的完整参数格式（首次用该工具前先调它）",
         "schema": {"type": "object",
                    "properties": {"server": {"type": "string", "description": "MCP 服务器名"},
                                   "tool": {"type": "string", "description": "工具名"}},
                    "required": ["server", "tool"]}},
        {"name": "mcp_call", "desc": "调用某个 MCP 工具；arguments 传该工具的参数对象（格式先问 mcp_help）",
         "schema": {"type": "object",
                    "properties": {"server": {"type": "string", "description": "MCP 服务器名"},
                                   "tool": {"type": "string", "description": "工具名"},
                                   "arguments": {"type": "object", "description": "该工具的参数"}},
                    "required": ["server", "tool"]}},
    ]
    builtin = [t for t in tools if not t[0].startswith("mcp.")]
    mcp = [t for t in tools if t[0].startswith("mcp.")]
    by_server = {}
    for tid, spec, runner in mcp:
        parts = tid.split(".")
        if len(parts) >= 3 and runner != "error":
            by_server.setdefault(parts[1], []).append((tid, spec))
    catalog_lines = []
    for srv, items in by_server.items():
        catalog_lines.append("[" + srv + "]")
        for tid, spec in items:
            catalog_lines.append("- " + tid + ": " + str(spec.get("desc") or "").strip())
    specs_text = "\n".join(
        "- {name}: {desc} 参数JSONSchema: {schema}".format(
            name=t[0], desc=t[1]["desc"], schema=json.dumps(t[1]["schema"], ensure_ascii=False))
        for t in builtin)
    if catalog_lines:
        specs_text += ("\nMCP 工具目录（先 mcp_help 查参数格式，再用 mcp_call 执行；"
                       "也可以直接拿 mcp.服务器.工具 当 name 调用）：\n" + "\n".join(catalog_lines))
    tools = builtin + (mcp + [(m["name"], m, "meta") for m in mcp_meta] if mcp else [])

    sys_parts = []
    if str(opts.get("system", "")).strip():
        sys_parts.append(str(opts["system"]).strip())
    if tools:
        sys_parts.append(TOOL_PROMPT_TMPL.format(specs=specs_text))
    conv = ([{"role": "system", "content": "\n\n".join(sys_parts)}] if sys_parts else []) + list(messages)

    conv_id = str(opts.get("conv_id", ""))
    allowed_here = _SESSION_ALLOWED.setdefault(conv_id, set()) if conv_id else set()
    always_allowed = perms_load()

    def _norm(t):
        return "".join(str(t).split())

    def _raw_cut(buf, nchars):
        """buf 中规范化字符数达到 nchars 时的原始下标"""
        cnt = 0
        for i, ch in enumerate(buf):
            if not ch.isspace():
                cnt += 1
                if cnt >= nchars:
                    return i + 1
        return 0

    reply = ""
    for iteration in range(30):   # 取消低次数限制；30 次仅作失控保护
        parts = []
        visible = []          # 实际下发到输出栏的文本（去重后）
        think_acc = ""        # 本段推理累计
        buf = ""              # 待判定的正文缓冲
        try:
            for kind, tok in llm_stream(port, conv, temperature, max_tokens):
                parts.append(tok)   # 原文进引擎上下文
                if kind == "think":
                    if buf and '"tool_call"' not in buf:   # 推理回归：放出缓冲正文
                        emit({"type": "token", "text": buf, "kind": "text"})
                        visible.append(buf)
                    buf = ""
                    think_acc += tok
                    emit({"type": "token", "text": tok, "kind": "think"})
                    continue
                buf += tok
                nb = buf.strip()
                if '"tool_call"' in buf:
                    continue        # 工具 JSON 整体抑制，绝不进输出栏
                if think_acc:
                    # 工具 JSON 判定优先：不随推理去重逻辑碎片化发出
                    if '"tool_call"' in buf:
                        continue
                    if '{"tool_call"'.startswith(buf.strip()):
                        continue    # 疑似工具 JSON 前缀，等待
                    # 有前置推理：正文很可能在重复推理文本，前缀匹配抑制
                    ta = _norm(think_acc)
                    nbn = _norm(buf)
                    if ta.startswith(nbn):
                        continue    # 仍是推理的重复前缀，抑制
                    if nbn.startswith(ta) and ta:
                        rest = buf[_raw_cut(buf, len(ta)):]
                    else:
                        lcp = 0
                        for a, b in zip(ta, nbn):
                            if a != b:
                                break
                            lcp += 1
                        rest = buf[_raw_cut(buf, lcp):] if lcp else buf
                    emit({"type": "token", "text": rest, "kind": "text"})
                    visible.append(rest)
                    buf = ""
                    think_acc = ""  # 本段去重结束
                else:
                    JSON_MARK = '{"tool_call"'
                    if '"tool_call"' in nb:
                        continue        # 确认是工具 JSON，整体吞咽
                    if JSON_MARK.startswith(nb):
                        continue        # 疑似工具 JSON 前缀，继续等待判定
                    emit({"type": "token", "text": buf, "kind": "text"})
                    visible.append(buf)
                    buf = ""
            if buf and '"tool_call"' not in buf and \
                    not (think_acc and _norm(think_acc).startswith(_norm(buf))):
                emit({"type": "token", "text": buf, "kind": "text"})
                visible.append(buf)
        except Exception as e:
            emit({"type": "error", "text": "引擎请求失败：{}".format(e)})
            return reply
        reply = "".join(parts)
        visible_text = "".join(visible)
        conv.append({"role": "assistant", "content": reply})
        if not tools:
            return reply
        call = parse_tool_call(reply)
        if not call:
            return reply
        tool_id, arguments, js, je = call
        # 关键：输出栏只保留已清洁正文（去重后），JSON 本身绝不下发
        json_text = reply[js:je]
        if visible_text.endswith(json_text):
            clean = visible_text[:-len(json_text)].rstrip()
        else:
            clean = visible_text
        emit({"type": "seg", "text": clean})
        tool = next((t for t in tools if t[0] == tool_id), None)
        if not tool:
            conv.append({"role": "user",
                         "content": "[系统] 工具 {} 不存在。可用工具：{}".format(tool_id, ", ".join(t[0] for t in tools))})
            emit({"type": "tool_done", "name": tool_id, "ok": False, "result": "工具不存在"})
            continue
        # 渐进式披露元工具：mcp_help 只读元数据，免授权；mcp_call 改写成真实工具 id 后走正常授权/执行
        if tool[2] == "meta":
            srv = str(arguments.get("server") or "").strip()
            tname = str(arguments.get("tool") or "").strip()
            if tool_id == "mcp_help":
                target = next((t for t in mcp if t[0] == "mcp.%s.%s" % (srv, tname)), None)
                if not target:
                    known = "；".join("[%s] %s" % (s, ", ".join(x[0].split(".", 2)[2] for x in items))
                                      for s, items in by_server.items()) or "（无 MCP 服务器）"
                    help_text = "没有工具 mcp.%s.%s。当前目录：%s" % (srv, tname, known)
                    ok = False
                else:
                    help_text = "{}\n参数JSONSchema: {}".format(
                        target[1].get("desc") or "", json.dumps(target[1].get("schema") or {}, ensure_ascii=False))
                    ok = True
                emit({"type": "tool_done", "name": "mcp_help", "ok": ok, "result": help_text[:800]})
                conv.append({"role": "user", "content": "[工具 mcp_help 返回]\n" + help_text[:8000]})
                continue
            # mcp_call
            real = "mcp.%s.%s" % (srv, tname)
            target = next((t for t in mcp if t[0] == real), None)
            if not target:
                msg = "没有工具 %s。用 mcp_help 或目录确认服务器名和工具名。" % real
                conv.append({"role": "user", "content": "[系统] " + msg})
                emit({"type": "tool_done", "name": "mcp_call", "ok": False, "result": msg})
                continue
            tool_id, tool, arguments = real, target, (arguments.get("arguments") or {})
        emit({"type": "tool_start", "name": tool_id, "args": arguments})
        # 授权判定：一直允许 > 本对话已允许 > 弹窗询问
        pre_authorized = (not permission) or tool_id in always_allowed or tool_id in allowed_here
        if not pre_authorized:
            with _pending_lock:
                _pending_seq[0] += 1
                pid = str(_pending_seq[0])
                ev = threading.Event()
                box = {}
                PENDING[pid] = [ev, box]
            emit({"type": "permission", "id": pid, "name": tool_id, "args": arguments})
            granted = ev.wait(600)
            with _pending_lock:
                PENDING.pop(pid, None)
            if not granted:
                conv.append({"role": "user", "content": "[系统] 工具调用未获用户确认（超时），已取消。"})
                emit({"type": "tool_done", "name": tool_id, "ok": False, "result": "用户未确认（超时）"})
                return reply
            if not box.get("approved"):
                conv.append({"role": "user", "content": f"[系统] 用户拒绝了工具 {tool_id} 的调用。请直接告知用户，不要重试该工具。"})
                emit({"type": "tool_done", "name": tool_id, "ok": False, "result": "用户拒绝"})
                continue
            scope = str(box.get("scope", "once"))
            if scope == "always":
                perms_add_always(tool_id)
                always_allowed.add(tool_id)
                emit({"type": "tool_note", "text": f"已记住：此后始终允许 {tool_id}（可在面板清除）"})
            elif scope == "session":
                allowed_here.add(tool_id)
                emit({"type": "tool_note", "text": f"已允许 {tool_id} 用于本对话"})
        try:
            result = execute_tool(tool[0], tool[1], tool[2], arguments)
            ok = True
        except Exception as e:
            result = "工具执行出错: " + str(e)
            ok = False
        preview = str(result)
        if len(preview) > 800:
            preview = preview[:800] + " …(已截断)"
        emit({"type": "tool_done", "name": tool_id, "ok": ok, "result": preview})
        conv.append({"role": "user", "content": "[工具 {} 返回]\n{}".format(tool_id, str(result)[:8000])})
    emit({"type": "warn", "text": "已达工具调用保护上限（30 次），停止继续调用。"})
    return reply