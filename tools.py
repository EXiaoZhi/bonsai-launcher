#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具注册、执行与 tool_call 解析。"""
import json

from core import *
from workspace import ws_list, ws_read, ws_write, ws_delete
from mcp import mcp_load_cfg, mcp_session

def builtin_tool_specs():
    return [
        {"name": "list_files", "desc": "列出文件操作空间中的所有文件（名称、大小、修改时间）",
         "schema": {"type": "object", "properties": {}, "required": []}},
        {"name": "read_file", "desc": "读取文件操作空间中某个文本文件的内容",
         "schema": {"type": "object", "properties": {"name": {"type": "string", "description": "文件名"}},
                    "required": ["name"]}},
        {"name": "write_file", "desc": "向文件操作空间写入/覆盖一个文本文件，用于生成报告、代码、数据等成果文件",
         "schema": {"type": "object", "properties": {"name": {"type": "string", "description": "文件名"},
                                                       "content": {"type": "string", "description": "完整文件内容"}},
                    "required": ["name", "content"]}},
        {"name": "delete_file", "desc": "删除文件操作空间中的一个文件",
         "schema": {"type": "object", "properties": {"name": {"type": "string", "description": "文件名"}},
                    "required": ["name"]}},
    ]


def collect_tools():
    """返回 [(tool_id, spec, runner)]，tool_id 形如 read_file 或 mcp.服务器名.工具名"""
    tools = [(t["name"], t, None) for t in builtin_tool_specs()]
    for name, cfg in mcp_load_cfg().items():
        if not cfg.get("enabled", True):
            continue
        try:
            s = mcp_session(name, cfg)
            for t in s.tools():
                tid = "mcp." + name + "." + t["name"]
                tools.append((tid, {"name": tid, "desc": "[" + name + "] " + t["desc"], "schema": t["schema"]}, (s, t["name"])))
        except Exception as e:
            tools.append(("mcp." + name + ".!", {"name": name, "desc": "(连接失败: " + str(e) + ")", "schema": {}}, "error"))
    return tools

def execute_tool(tool_id, spec, runner, arguments):
    args = arguments if isinstance(arguments, dict) else {}
    if tool_id == "list_files":
        return json.dumps(ws_list(), ensure_ascii=False, indent=1)
    if tool_id == "read_file":
        return ws_read(args.get("name", ""))
    if tool_id == "write_file":
        n = ws_write(args.get("name", ""), args.get("content", ""))
        return f"已写入 {n} 字节。"
    if tool_id == "delete_file":
        ws_delete(args.get("name", ""))
        return "已删除。"
    if runner == "error":
        raise RuntimeError(spec["desc"])
    session, real_name = runner
    res = session.request("tools/call", {"name": real_name, "arguments": args}, timeout=180)
    content = (res or {}).get("content", [])
    texts = [c.get("text", "") for c in content if isinstance(c, dict)]
    if (res or {}).get("isError"):
        raise RuntimeError("\n".join(texts) or "MCP 工具返回错误")
    return "\n".join(texts)


TOOL_PROMPT_TMPL = """你可以调用以下工具来完成用户的要求。需要工具时，把工具调用作为回复的全部内容，输出且仅输出一个 JSON 对象（不要输出其他文字、不要用代码块）：
{{"tool_call":{{"name":"工具id","arguments":{{参数}}}}}}
可用工具：
{specs}
规则：
- 调用前可先用 list_files 查看文件空间已有哪些文件，成果文件用 write_file 写入（文件名用英文或中文均可，不要带路径）。
- read_file 一次最多读 10 万字符，大文件先看再分段读。
- 工具结果会以 system 消息返回给你，拿到结果后给出最终回答。
- 连续调用最多 6 次；不需要工具时直接正常回答，不要输出 tool_call。"""


def _extract_braced(s, start):
    """从 s[start]=='{' 处做花括号匹配，返回 (obj_text, end)"""
    depth = 0
    in_str = False
    esc2 = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc2:
                esc2 = False
            elif c == "\\":
                esc2 = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1], i + 1
    return None, None


def parse_tool_call(text):
    """从模型回复中解析 tool_call JSON；返回 (tool_id, arguments, json_start, json_end) 或 None"""
    if not text or "tool_call" not in text:
        return None
    start = 0
    while True:
        start = text.find("{", start)
        if start < 0:
            return None
        frag, end = _extract_braced(text, start)
        if frag:
            try:
                obj = json.loads(frag)
            except Exception:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("tool_call"), dict):
                tc = obj["tool_call"]
                name = str(tc.get("name", "")).strip()
                args = tc.get("arguments", {})
                if name:
                    return name, (args if isinstance(args, dict) else {}), start, end
        start += 1