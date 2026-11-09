"""模型配置加载与校验。

契约来源：models-config.json（schema 见 docs/adr/0001-architecture.md）。
原则：gateway 不猜默认值——config 缺字段直接报错，避免"跑起来但行为不对"。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "models-config.json")

EFFORT_VALUES = ("none", "low", "medium", "high")


class ConfigError(Exception):
    pass


@dataclass
class Upstream:
    mode: str                 # direct | router
    base_url: str
    model_id: str


@dataclass
class Thinking:
    supported: bool
    efforts: List[str]
    default: str
    transport: str = "chat_template_kwargs"   # chat_template_kwargs | none

    def supports(self, effort: str) -> bool:
        return effort in self.efforts


@dataclass
class Context:
    window: int
    kv_kib_per_token: float = 16.0
    output_reserve: int = 2048
    thinking_reserve: int = 0

    def budget(self, thinking: bool) -> int:
        reserve = self.output_reserve + (self.thinking_reserve if thinking else 0)
        return max(1024, self.window - reserve)


@dataclass
class Model:
    id: str
    label: str
    subtitle: str
    enabled: bool
    runtime: str
    path: str
    quant: str
    upstream: Upstream
    sampling: Dict[str, Any]
    thinking: Thinking
    context: Context
    binary: Optional[str] = None
    launch: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_local_file(self) -> bool:
        return bool(self.path) and self.path.endswith(".gguf")


@dataclass
class Config:
    raw: Dict[str, Any]
    gateway: Dict[str, Any]
    router: Dict[str, Any]
    compression: Dict[str, Any]
    models: Dict[str, Model]

    def model(self, mid: str) -> Model:
        m = self.models.get(mid)
        if not m:
            raise KeyError(mid)
        return m

    def enabled_models(self) -> List[Model]:
        return [m for m in self.models.values() if m.enabled]


def _req(d: Dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"{where} 缺少必填字段 '{key}'")
    return d[key]


def _model(raw: Dict[str, Any], idx: int) -> Model:
    where = f"models[{idx}]"
    mid = str(_req(raw, "id", where))
    where = f"model '{mid}'"

    up = _req(raw, "upstream", where)
    mode = str(_req(up, "mode", where + ".upstream"))
    if mode not in ("direct", "router"):
        raise ConfigError(f"{where}.upstream.mode 只能是 direct 或 router，当前 '{mode}'")

    th = raw.get("thinking") or {"supported": False, "efforts": [], "default": "none"}
    efforts = list(th.get("efforts") or [])
    bad = [e for e in efforts if e not in EFFORT_VALUES]
    if bad:
        raise ConfigError(f"{where}.thinking.efforts 含非法档位 {bad}，允许 {EFFORT_VALUES}")
    default = str(th.get("default", "none"))
    if efforts and default not in efforts:
        raise ConfigError(f"{where}.thinking.default '{default}' 不在 efforts {efforts} 内")
    if not efforts and default != "none":
        raise ConfigError(f"{where} 未声明任何 effort 却设了 default='{default}'")

    ctx = _req(raw, "context", where)
    window = int(_req(ctx, "window", where + ".context"))
    if window < 2048:
        raise ConfigError(f"{where}.context.window 过小：{window}")

    return Model(
        id=mid,
        label=str(raw.get("label", mid)),
        subtitle=str(raw.get("subtitle", "")),
        enabled=bool(raw.get("enabled", True)),
        runtime=str(raw.get("runtime", "llama.cpp-standard")),
        path=str(raw.get("path", "")),
        quant=str(raw.get("quant", "")),
        binary=raw.get("binary"),
        launch=raw.get("launch") or {},
        sampling=raw.get("sampling") or {},
        upstream=Upstream(mode=mode, base_url=str(_req(up, "base_url", where + ".upstream")).rstrip("/"),
                          model_id=str(up.get("model_id") or mid)),
        thinking=Thinking(supported=bool(th.get("supported", bool(efforts))), efforts=efforts,
                          default=default, transport=str(th.get("transport", "chat_template_kwargs"))),
        context=Context(window=window,
                        kv_kib_per_token=float(ctx.get("kv_kib_per_token", 16)),
                        output_reserve=int(ctx.get("output_reserve", 2048)),
                        thinking_reserve=int(ctx.get("thinking_reserve", 0))),
    )


def load(path: str = None) -> Config:
    path = path or os.environ.get("BONSAI_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if int(raw.get("version", 0)) != 1:
        raise ConfigError(f"不支持的配置版本：{raw.get('version')}（当前只支持 1）")

    models: Dict[str, Model] = {}
    for i, m in enumerate(raw.get("models") or []):
        mo = _model(m, i)
        if mo.id in models:
            raise ConfigError(f"模型 id 重复：{mo.id}")
        models[mo.id] = mo
    if not models:
        raise ConfigError("models 为空")

    gw = raw.get("gateway") or {}
    for k in ("port",):
        if k not in gw:
            raise ConfigError(f"gateway 缺少 '{k}'")
    return Config(raw=raw, gateway=gw, router=raw.get("router") or {},
                  compression=raw.get("compression") or {}, models=models)
