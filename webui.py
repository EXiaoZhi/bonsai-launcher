#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动面板前端加载：从 webui/panel.html 读取，注入 __SL_BOOT__。"""
import json
import os
from core import BASE

_PANEL_CACHE = [None, 0.0]   # [html, mtime]

def load_panel():
    """读取启动面板 HTML；失败时返回空串。按 mtime 缓存，改版后自动刷新。"""
    path = os.path.join(BASE, "webui", "panel.html")
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return ""
    if _PANEL_CACHE[0] and _PANEL_CACHE[1] == mtime:
        return _PANEL_CACHE[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        _PANEL_CACHE[0] = html
        _PANEL_CACHE[1] = mtime
        return html
    except Exception:
        return ""