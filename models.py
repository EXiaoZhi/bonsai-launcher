#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型清单、引擎宿主与网关桥接。"""
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from core import *

# 引擎参数规格 + 引擎宿主实现
try:
    import param_specs as PARAM_SPECS
except Exception as _e:
    PARAM_SPECS = None
    print("[launcher] 参数规格表加载失败：", _e)
import engine_host as EH

HOST = None


def gw_ensure():
    """惰性启动内嵌聊天后端；失败只影响聊天页，不影响启动面板。"""
    if GW_STATE["gw"] is not None:
        return GW_STATE["gw"]
    with GW_STATE["lock"]:
        if GW_STATE["gw"] is not None:
            return GW_STATE["gw"]
        try:
            if BASE not in sys.path:
                sys.path.insert(0, BASE)
            from gateway import app as gwapp
            from gateway import security as gwsec
            gw = gwapp.Gateway(MODELS_CFG)
            srv = ThreadingHTTPServer(("127.0.0.1", GW_PORT), gwapp.Handler)
            srv.gw = gw
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            GW_STATE.update(srv=srv, gw=gw, err=None, cookie=gwsec.COOKIE_NAME)
            # 网关可能在引擎启动后才被首次创建（先点开聊天页）：建好立刻对齐真实实例
            gw_refresh()
            print("[launcher] 聊天后端已就绪 127.0.0.1:%d" % GW_PORT)
        except Exception as e:
            GW_STATE["err"] = str(e)
            print("[launcher] 聊天后端启动失败：", e)
        return GW_STATE["gw"]


def gw_sync_port(port, instances=None):
    """把引擎真实端口回写给「按文件匹配」的配置模型。

    坑（2026-11-02 实测）：旧实现用 `base_url.rsplit(":", 1)[0] + ":" + port` 重写地址，
    会把路径 `/v1` 一起切掉 → 网关请求 http://127.0.0.1:18200/models（缺 /v1）拿 404。
    现在只改端口、保留路径；且**只改文件名真的对得上的模型**，不再"匹配不上就套第一个端口"
    （那会把 27B 的地址指到 9B 的端口上，聊天页因此把没启动的 27B 显示成可用）。
    """
    gw = GW_STATE["gw"]
    if not gw:
        return
    insts = [it for it in (instances or []) if it.get("port")]
    if not insts:
        return
    from urllib.parse import urlsplit, urlunsplit
    rt = getattr(gw, "_runtime_models", None) or {}
    for m in list(gw.cfg.models.values()):
        u = getattr(m, "upstream", None)
        if not u or getattr(u, "mode", "") != "direct":
            continue
        if m.id in rt:
            continue          # 运行时模型按自己的端口注册，不能被"第一个实例"覆盖
        mbase = os.path.basename(getattr(m, "path", "") or "").lower()
        if not mbase:
            mbase = os.path.basename(getattr(u, "model_id", "") or "").lower()
        hit = None
        for it in insts:
            ibase = os.path.basename(it.get("path") or "").lower()
            tag = (it.get("id") or "").strip().lower()
            if (mbase and ibase and mbase == ibase) or (tag and tag == mbase.rsplit(".gguf", 1)[0]):
                hit = it
                break
        if hit is None:
            continue
        try:
            sp = urlsplit(str(u.base_url))
            host = sp.hostname or "127.0.0.1"
            path = sp.path if sp.path and sp.path != "/" else "/v1"
            u.base_url = urlunsplit((sp.scheme or "http", "%s:%d" % (host, int(hit["port"])),
                                     path, sp.query, sp.fragment))
        except Exception as e:
            print("[launcher] 同步聊天后端上游端口失败：", e)


def gw_refresh(instances=None):
    """把「当前真在跑的引擎」同步给聊天后端：注册成可聊模型 + 只让它们出现在列表里。

    判据只看**(文件, 端口)**，绝不按端口去套配置里的模型 —— 2026-11-03 的故障就是
    9B 起在 18200，被当成配置里的 27B，于是聊天页显示 27B、真正的 9B 反而消失。
    同一个文件起多个实例时，后几个用 `<名字>#<端口>` 作独立 id，聊天页能分别选到。
    """
    gw = GW_STATE["gw"]
    if not gw:
        return
    if instances is None:
        # 新架构：实例全部由 EngineHost 管理（每个模型一个独立进程）
        instances = [{"port": i["port"], "id": i["alias"], "kind": i["engine"],
                      "slot": 0, "path": i["path"], "ctx": i["ctx"] or 32768,
                      "type": i["type"]}
                     for i in host().status() if i.get("ready")]

    rt = getattr(gw, "_runtime_models", None)
    if not isinstance(rt, dict):
        rt = {}
        gw._runtime_models = rt

    counts = {}
    for it in instances:
        b = os.path.basename(it.get("path") or "").lower()
        if b:
            counts[b] = counts.get(b, 0) + 1

    live_ids, live_ports, used = set(), set(), set()
    for it in instances:
        try:
            port = int(it.get("port") or 0)
            if not port:
                continue
            path = it.get("path") or ""
            base = os.path.basename(path)
            # 网关里的模型名要和启动器/代理一致：优先用槽位别名，其次文件名
            stem = ((it.get("id") or "").strip()
                    or (base[:-5] if base.lower().endswith(".gguf") else base)
                    or ("runtime-%d" % port))

            # ① 配置里已有同一个文件 → 复用那条配置（端口改成实际端口）
            cfg_hit = None
            for m in gw.cfg.models.values():
                if m.id in rt:
                    continue
                mp = os.path.basename(getattr(m, "path", "") or "")
                if base and mp.lower() == base.lower():
                    cfg_hit = m
                    break
            if cfg_hit is not None and cfg_hit.id not in used:
                u = getattr(cfg_hit, "upstream", None)
                if u is not None:
                    u.mode = "direct"
                    u.base_url = "http://127.0.0.1:%d/v1" % port
                cfg_hit.enabled = True     # 正在跑就一定能选，不看配置文件里的 enabled
                orig = getattr(cfg_hit, "_orig_label", None) or cfg_hit.label
                cfg_hit._orig_label = orig
                cfg_hit.label = ("%s (:%d)" % (orig, port)) if counts.get(base.lower(), 0) > 1 else orig
                live_ids.add(cfg_hit.id)
                live_ports.add(port)
                used.add(cfg_hit.id)
                continue

            # ② 之前已经为「同一个文件 + 同一个端口」注册过 → 复用 id
            key = (base.lower(), port)
            rid = next((mid for mid, meta in rt.items() if tuple(meta) == key), None)
            if rid is None:
                dup = bool(base) and any(
                    os.path.basename(getattr(x, "path", "") or "").lower() == base.lower()
                    for x in gw.cfg.models.values())
                rid = stem
                n = 2
                while rid in gw.cfg.models or rid in used:
                    rid = ("%s#%d" % (stem, port)) if n == 2 else ("%s#%d-%d" % (stem, port, n))
                    n += 1
                multi = counts.get(base.lower(), 0) > 1 or dup
                label = ("%s (:%d)" % (base, port)) if multi else (base or stem)
                gw.register_runtime(rid, path, port, int(it.get("ctx") or 32768), label=label)
                rt[rid] = key
            else:
                # 先起的那个当时可能只有一个实例（标签没带端口），后面多开了要补上
                m0 = gw.cfg.models.get(rid)
                if m0 is not None:
                    m0.label = ("%s (:%d)" % (base, port)) if counts.get(base.lower(), 0) > 1 else (base or stem)
            live_ids.add(rid)
            live_ports.add(port)
            used.add(rid)
        except Exception as e:
            print("[launcher] 注册运行时模型失败：", e)

    try:
        gw.live_ids = live_ids
        gw.live_ports = live_ports
    except Exception:
        pass
    gw_sync_port(None, instances)


# gateway 独占的路径（与 launcher 自己的 /api/status、/api/catalog 等不冲突）
GW_EXACT = {
    "GET": ("/favicon.ico", "/api/me", "/api/models", "/api/health", "/api/logs/stream",
            "/api/rerank", "/v1/models", "/models"),
    "POST": ("/api/login", "/api/chat", "/v1/rerank", "/rerank",
             "/v1/chat/completions", "/chat/completions",
             "/v1/embeddings", "/embeddings"),
}
GW_PREFIX = ("/assets/", "/api/sessions", "/api/snippets", "/api/messages")


def gw_owns(method, path):
    p = path.split("?", 1)[0]
    if p in GW_EXACT.get(method, ()):
        return True
    return any(p == x.rstrip("/") or p.startswith(x) for x in GW_PREFIX)


def load_models_cfg():
    try:
        with open(MODELS_CFG, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_models_cfg(cfg):
    tmp = MODELS_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MODELS_CFG)


import engine_host as EH

HOST = None


def _host_log(line):
    with STATE["lock"]:
        STATE["log_seq"] += 1
        STATE["lines"].append((STATE["log_seq"], "[host] " + str(line)))


def _host_changed():
    """启停后：同步网关模型列表 + 把 rerank 指向正在跑的重排实例。"""
    try:
        gw_ensure()
        gw_refresh()
    except Exception:
        pass
    try:
        cfg = load_models_cfg()
        cur = dict(cfg.get("rerank") or {})
        rr = next((i for i in host().status() if i["type"] == "rerank" and i["ready"]), None)
        if rr:
            cur.update({"enabled": True, "model": rr["alias"],
                        "base_url": "http://127.0.0.1:%d" % rr["port"]})
        else:
            cur["base_url"] = ""
        cfg["rerank"] = cur
        save_models_cfg(cfg)
    except Exception:
        pass


def host():
    """单模型引擎宿主（懒加载）。"""
    global HOST
    if HOST is None:
        HOST = EH.EngineHost(EH.SlotStore(MODELS_CFG, load_models_cfg, save_models_cfg),
                             PARAM_SPECS, log=_host_log, on_change=_host_changed)
    return HOST


def catalog():
    """扫描 model/ 下的主模型 GGUF（排除 mmproj），并按 models-config.json 补充端口/参数。"""
    cfg = load_models_cfg()
    by_file = {}
    for m in (cfg.get("models") or []):
        name = os.path.basename(str(m.get("path", "")))
        if name:
            by_file[name] = m
    out = []
    try:
        names = sorted(os.listdir(os.path.join(BASE, "model")))
    except Exception:
        names = []
    for fn in names:
        if not fn.lower().endswith(".gguf") or "mmproj" in fn.lower():
            continue
        fp = os.path.join(BASE, "model", fn)
        m = by_file.get(fn) or {}
        launch = m.get("launch") or {}
        out.append({
            "file": fn,
            "path": fp,
            "id": m.get("id") or fn[:-5],
            "label": m.get("label") or fn[:-5],
            "port": int(launch.get("port") or (DEFAULT_ENGINE_PORT + len(out))),
            "exists": os.path.exists(fp),
            "size_gb": round(os.path.getsize(fp) / 1073741824, 2) if os.path.exists(fp) else 0,
            "quant": m.get("quant") or "",
            "launch": launch,
            "thinking": (m.get("thinking") or {}).get("default"),
        })
    return out