"""从两个引擎的 --help 生成参数面板的规格表（param_specs.py）。

为什么要生成：参数是引擎能力的事实来源（llama-server 的 help / kvmem 的 help），
手抄容易漏、容易写错 flag 名。这里解析一次，产出前端可直接渲染的元数据：
  key(稳定 id) / flag(命令行) / kind / choices / hint / group / label / default
  preset(推荐初值) / help(中文详解)

两条原则（2026-11-04 修订）：
1. **预设只描述行为，不描述硬件**。
   preset 里不出现「8G 显存适合多少」「16G 内存够不够」这类与设备绑定的数字；
   显存/内存相关的项一律留空（= 引擎默认），把「按你的设备算」的方法写进 help。
   只给设备无关的行为/容量项：Jinja 模板、FA=auto、上下文长度、生成预留。
2. **help 要能独立看懂**：说明「管什么 / 默认值 / 什么时候该改 / 有什么约束」，
   并给出可代入手头数字的公式，而不是给一个别人的配置。

llama 取「常用实用」白名单；kvmem 取引擎全量（只隐藏别名与无效项）。
"""
from __future__ import annotations

import json
import os
import pprint
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KV_HELP_PATH = os.path.join(BASE, ".tmp", "kvmem_help.txt")
LLAMA_HELP_PATH = os.path.join(BASE, ".tmp", "llama_help.txt")
OUT = os.path.join(BASE, "param_specs.py")


# ---------------------------------------------------------------- 解析 help
def parse_help(path):
    """返回 [{flag, short, hint, desc, group}]，保持文件顺序。"""
    items = []
    group = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("-----") and line.endswith("-----"):
                group = line.strip("- ").strip()
                if group.endswith("params"):
                    group = group[: -len("params")].strip()
                continue
            stripped = line.lstrip()
            if re.match(r"^(Aliases|Environment|Sampling defaults|usage):", stripped):
                continue          # 说明行，不是参数
            # 参数行：左边是「短别名 / 长别名 / 占位符」，右边（2 空格以上）是描述。
            # 注意 help 里有 `-fa,   --flash-attn [on|off|auto]` 这种「短别名与长别名之间
            # 也是 2+ 空格」的写法，直接按第一个空档切会把长别名切掉、参数整条丢失。
            parts = re.split(r"\s{2,}", stripped)
            head, i = parts[0], 1
            while not re.search(r"--[a-z0-9][a-z0-9-]*", head) and i < len(parts):
                head = head + "  " + parts[i]
                i += 1
            desc = "  ".join(parts[i:]) if i < len(parts) else ""
            flags = re.findall(r"--[a-z0-9][a-z0-9-]*", head)
            if flags:
                flag = flags[0]
                sm = re.match(r"^(-[A-Za-z0-9]+),", head)
                short = sm.group(1) if sm else ""
                hint = head.split(flag, 1)[1].strip().strip(",").strip() if flag in head else ""
                items.append({"short": short, "flag": flag, "flags": flags,
                              "hint": hint, "desc": desc, "group": group})
            elif items and line.strip() and (len(line) - len(stripped)) >= 20:
                items[-1]["desc"] += " " + line.strip()
    return items


# help 里的默认值有一半是「loaded from model」「template taken from model's metadata」
# 这类说明，直接当占位符会被误读成一个可选值，这里过滤掉。
JUNK_DEFAULT = re.compile(r"model|template|device|whatever|random", re.I)


def default_of(desc: str) -> str:
    # 两种写法都要认：llama 用 "default: 2048"，kvmem 用 "(default 2048)"
    # \b 很重要：`(defaults to --threads)` 里的 "defaults" 不能被当成默认值
    m = re.search(r"default\b\s*:?\s*([^,)]+)", desc)
    if not m:
        return ""
    d = m.group(1).strip()
    d = d.split("=")[0].strip().strip("'\"")   # "0 = model training..." → "0"；"'auto'" → auto
    if JUNK_DEFAULT.search(d):
        return ""
    if re.match(r"^-?\d+\.0+$", d):       # "-1.00" → "-1"
        d = str(int(float(d)))
    return d


def kind_of(hint: str):
    """由 help 里的占位符推断控件类型与可选项。"""
    h = hint.strip()
    m = re.match(r"^\{(.+)\}$", h)
    if m:
        return "select", [x.strip() for x in m.group(1).split(",") if x.strip()]
    m = re.match(r"^\[(.+)\]$", h)
    if m:
        return "select", [x.strip() for x in m.group(1).split("|") if x.strip()]
    if h and re.match(r"^-?\d+(\.\d+)?$", h):
        return "number", []
    if any(k in h.upper() for k in ("N", "SIZE", "TOKENS", "GB", "RATIO", "STEPS")):
        return "number", []
    return "text", []


def key_of(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


# ================================================================ 预设（与硬件无关）
# 只放「设备无关、对绝大多数模型都成立」的值：
#   - 行为/协议类：Jinja 模板、FlashAttention 交给引擎判断（auto）
#   - 容量类：上下文长度、生成预留（是 token 数，不是显存大小）
# 显存/内存池（budget / cpu-gb / gpu-ratio / nvme-gb）**故意不给预设**：
# 它们必须由使用者的设备决定，写死任何数字换台机器就会失效。
PRESET = {
    "llama": {
        "jinja": True,                 # 标准 GGUF 基本都自带 Jinja 模板
        "flash_attn": "auto",          # 由引擎按设备判断，不假设显卡能力
    },
    "kvmem": {
        "ctx_size": "8192",            # 引擎默认只有 2048，对现在的对话模型太小
        "kvmem_gen_reserve": "2048",   # 引擎默认 256，长回答/思考容易被截断
        "flash_attn": "auto",
    },
}

# 按模型类型给一套**完整、可直接运行**的基本默认参数（面板里选「类型」即生效）。
# 口径：容量类取保守值（小显存/普通内存也能跑起来），行为类给最优默认；
# 线程数、GPU 层数这类强依赖设备的项不给默认（留空=引擎自动）。
# KVMem 的硬约束在生成后由测试锁住：sink ≥ 4096、sink < budget、gen < budget。
TYPE_PRESET = {
    "llama": {
        "chat":   {"jinja": True, "flash_attn": "auto"},
        "rerank": {"rerank": True, "ctx_size": "4096", "flash_attn": "auto",
                   "cache_type_k": "q8_0", "cache_type_v": "q8_0"},
        "embed":  {"embeddings": True, "pooling": "mean", "flash_attn": "auto"},
    },
    "kvmem": {
        "chat":   {"ctx_size": "8192", "kvmem_budget": "8192",
                   "kvmem_sink_tokens": "4096", "kvmem_gen_reserve": "1024",
                   "kvmem_cpu_gb": "2", "kvmem_method": "recency",
                   "kv_dtype": "q8_0", "flash_attn": "auto"},
        "rerank": {"ctx_size": "8192", "kvmem_budget": "8192",
                   "kvmem_sink_tokens": "4096", "kvmem_gen_reserve": "512",
                   "kvmem_cpu_gb": "1", "kv_dtype": "q8_0"},
        "embed":  {"ctx_size": "8192", "kvmem_budget": "8192",
                   "kvmem_sink_tokens": "4096", "kvmem_gen_reserve": "512",
                   "kvmem_cpu_gb": "1", "kv_dtype": "q8_0"},
    },
}

PRESET_NOTE = {
    "llama": (
        "选「类型」后已给一套可直接运行的基本默认（重排自动开 --rerank、嵌入自动开 --embeddings+mean 池化）。"
        "线程数、GPU 层数这类强依赖设备的项留空 = 引擎自动；上下文留空 = 用模型自带窗口。"
        "标「默认」的格子来自这套预设，可改；低频参数在「额外参数」里手填（会随启动项一起保存）。"
    ),
    "kvmem": (
        "选「类型」后已给一套保守但可直接运行的分层池默认（上下文 8192、GPU 工作集 8192、"
        "前缀保留 4096、内存池 2 GiB、KV q8_0）。内存池（cpu-gb）按你的内存余量加大；"
        "线程/GPU 层数留空 = 自动。注意 KVMem 没有 /v1/rerank、/v1/embeddings 服务端点。"
        "标「默认」的格子来自这套预设，可改；低频参数在「额外参数」里手填。"
    ),
}

# KVMem 引擎里的「别名 / 只支持 1 个 / 同名重复」项，留着只会让人填错。
# llama.cpp 那边这些参数名都是独立有效的，不能一起隐藏。
KV_HIDE = {
    "--parallel", "--gpu-layers", "--mmap", "--mlock", "--log-verbosity",
    "--list-devices", "--ui", "--ui-dir", "--no-ui", "--kvmem", "--no-kvmem",
    "--no-kvmem-trace", "--api-key-file", "--no-mmproj-offload", "--webui",
    "--usage", "--predict", "--path", "--no-webui", "--repetition-penalty",
}




# 数值参数的合法范围（min, max）—— 前端 input min/max 与保存校验共用。
# 有意允许负数的：presence/frequency penalty（[-2,2]）、reasoning-budget（-1 不限）、
# n-predict（-1 不限）。其余一律非负。
RANGE = {
    "--ctx-size": (0, 4_000_000),
    "--batch-size": (1, 65_536),
    "--ubatch-size": (1, 65_536),
    "--parallel": (1, 64),
    "--keep": (0, 4_000_000),
    "--cache-reuse": (0, 65_536),
    "--n-gpu-layers": (0, 999),
    "--main-gpu": (0, 63),
    "--temp": (0, 2),
    "--top-p": (0, 1),
    "--top-k": (0, 200),
    "--min-p": (0, 1),
    "--repeat-penalty": (0, 2),
    "--repeat-last-n": (0, 1_048_576),
    "--presence-penalty": (-2, 2),
    "--frequency-penalty": (-2, 2),
    "--seed": (0, 4_294_967_295),
    "--n-predict": (-1, 10_000_000),
    "--reasoning-budget": (-1, 1_000_000),
    "--threads": (0, 512),
    "--threads-batch": (0, 512),
    "--threads-http": (0, 512),
    "--timeout": (1, 86_400),
    "--prio": (-10, 10),
    "--log-verbosity": (0, 5),
    "--verbosity": (0, 5),
    "--kvmem-budget": (0, 100_000_000),
    "--kvmem-block-tokens": (16, 8192),
    "--kvmem-sink-tokens": (4096, 100_000_000),
    "--kvmem-gen-reserve": (0, 100_000_000),
    "--kvmem-recent-tokens": (0, 100_000_000),
    "--kvmem-query-last": (0, 1_000_000),
    "--kvmem-query-max-tokens": (0, 1_000_000),
    "--kvmem-gpu-ratio": (0, 1),
    "--kvmem-cpu-gb": (0, 2048),
    "--kvmem-nvme-gb": (0, 4096),
    "--spec-draft-n-max": (0, 64),
    "--spec-draft-p-min": (0, 1),
    "--image-min-tokens": (0, 65_536),
    "--image-max-tokens": (0, 65_536),
}



# 需要选文件的参数：flag -> (file|dir, 扩展名过滤)。前端在输入框旁给「选择…」按钮。
PATH_FIELDS = {
    "--mmproj": ("file", ".gguf"),
    "--lora": ("file", ".gguf"),
    "--spec-draft-model": ("file", ".gguf"),
    "--chat-template-file": ("file", ".jinja,.txt,.md"),
    "--slot-save-path": ("dir", ""),
    "--kvmem-nvme-dir": ("dir", ""),
}



# 非 chat 类型只保留真正常用的参数（低频项收进「额外参数」手填）。
# 不在表里的参数 = 仅 chat 可见。两个引擎通用。
NON_CHAT_KEEP = {
    # 两类模型都需要的基本项
    "--ctx-size", "--threads", "--timeout", "--api-key", "--flash-attn",
    "--n-gpu-layers", "--alias", "--mmproj",
    # 重排专属
    "--rerank", "--cache-type-k", "--cache-type-v",
    # 嵌入专属
    "--embeddings", "--pooling",
    # KVMem 分层池（跑重排/嵌入实验时同样有用）
    "--kvmem-budget", "--kvmem-sink-tokens", "--kvmem-gen-reserve",
    "--kvmem-cpu-gb", "--kvmem-method", "--kv-dtype",
}

# ================================================================ 中文详解
LLAMA_HELP = {
    "--model": "要加载的 GGUF 文件。一个进程只服务一个模型；换模型就改这里（或新建一个启动项）。",
    "--alias": "模型在 /v1/models 里显示的名字，也是客户端请求时填的 model。留空就用文件名。",
    "--mmproj": "视觉投影文件（多模态才需要）。纯文本模型留空；填了但模型不支持会加载失败。",
    "--lora": "外挂 LoRA 适配器（.gguf 或路径），可叠多个用逗号分隔。不做微调就留空。",
    "--ctx-size": "上下文窗口（token）。0/留空 = 用模型自带的训练窗口，这是最兼容的做法；"
                  "填大只改变「能记多长」，不改变权重占用，但会按这个尺寸分配 KV，显存不够会加载失败。",
    "--batch-size": "逻辑批大小（一次提交多少 token 做 prefill）。默认 2048；调大长提示更快，"
                    "但每一步占的临时显存更多。显存紧张就往下调。",
    "--ubatch-size": "物理批大小（真正进计算图的 microbatch）。默认 512；显存告急时它是第一个该降的旋钮，"
                     "降到 256/128 通常就能跑起来，代价是 prefill 变慢。",
    "--parallel": "并行槽位数量：同时能处理几个并发请求。每个槽位各自分配一份 KV，"
                  "总占用约等于「单份 KV × 槽位数」。单用户 1 就够。",
    "--keep": "上下文滚动时强制保留最前面多少个 token（系统提示/开场）。默认 0。"
              "系统提示很长又不想被滚掉时设为提示长度。",
    "--context-shift": "上下文满了怎么办：on = 丢掉最旧的一段继续（适合长对话），"
                       "off = 直接报错/截断（默认，行为更可预测）。",
    "--cache-reuse": "前缀缓存复用的最小块（token）。相同前缀（同一段系统提示/文档）重复请求时，"
                     "设 256 左右可明显省 prefill；0 = 关闭。",
    "--n-gpu-layers": "放到 GPU 的层数。99 = 全放；显存不足就逐层往下减（会变慢，但能跑起来）。",
    "--split-mode": "多卡切分：none 单卡；layer 按层分（最常用）；row 按行分（慢但省显存）。单卡无所谓。",
    "--tensor-split": "多卡权重比例，如 0.7,0.3。单卡留空。",
    "--main-gpu": "主显卡序号（从 0 开始）。单卡留空。",
    "--flash-attn": "FlashAttention：on 省显存且更快，off 兼容性最好，auto 由引擎判断（预设值）。"
                    "注意 V 量化（cache-type-v 非 f16）时必须开 FA，本面板会自动补上。",
    "--cache-type-k": "K 缓存精度。f16 = 无损（默认）；q8_0 约省一半显存且几乎无损；q4_0 更省但长上下文会掉质量。",
    "--cache-type-v": "V 缓存精度，规则同 K；V 用了量化值时必须同时开 FlashAttention。",
    "--no-kv-offload": "勾选 = KV 缓存不放显存、改放内存。显存极紧张时能用，速度会明显下降。",
    "--swa-full": "滑动窗口注意力（SWA）模型：勾选 = 保留全尺寸缓存。默认只留窗口内、更省显存；"
                  "只有发现「记得住开头、记不住中间」才需要打开。",
    "--rope-scaling": "超长上下文的 RoPE 缩放方式：none 不缩放；linear 线性插值；yarn 质量更好但更慢。"
                      "只有「上下文超过模型训练窗口」时才需要动它。",
    "--rope-scale": "线性缩放倍数，一般 = 目标上下文 ÷ 训练上下文。配合 rope-scaling=linear 使用。",
    "--rope-freq-base": "RoPE 基准频率，默认取模型自带的。改它属于 hack，除非模型文档明确要求。",
    "--rope-freq-scale": "RoPE 频率缩放系数，和 rope-scale 是两种写法。不确定就留空。",
    "--yarn-orig-ctx": "YaRN 的「原始训练窗口」。0 = 自动取模型元数据。"
                       "只有模型训练窗口被元数据写错时才需要手动填。",
    "--yarn-ext-factor": "YaRN 外推系数。-1 = 用库默认。一般不需要动。",
    "--yarn-attn-factor": "YaRN 注意力温度系数。-1 = 库默认。调它通常是为了修「长文注意力发散」。",
    "--yarn-beta-slow": "YaRN 低频补偿边界。-1 = 库默认。细调项，先别动。",
    "--yarn-beta-fast": "YaRN 高频补偿边界。-1 = 库默认。细调项，先别动。",
    "--spec-type": "投机解码：none 关闭（默认）；draft-simple/draft-mtp 用草稿模型或模型自带 MTP 头加速。"
                   "没有草稿模型时收益很小，保持 none。",
    "--spec-draft-model": "草稿模型路径（通常是同系列的小模型）。仅在 spec-type 需要外部草稿模型时填。",
    "--spec-draft-n-max": "每次最多猜几个 token。猜得多收益大、浪费也多；3~8 是常见区间。",
    "--spec-draft-n-min": "至少猜几个 token 才尝试。默认 0（不限制）。",
    "--spec-draft-p-min": "草稿 token 的最低接受概率。调高更保守（几乎不浪费），调低更激进。",
    "--temp": "采样温度：0 = 贪心（可复现）；0.6~0.8 适合问答/对话；0.9~1.1 适合写作。"
              "各模型官方推荐值不同，先看模型卡；不确定就留空用引擎默认。",
    "--top-p": "核采样：每步只从累计概率前 P 的候选里选。0.9~0.95 通用；调低更保守。",
    "--top-k": "每步只保留概率最高的 K 个候选。0 = 关闭。常见 20~40。",
    "--min-p": "相对概率下限：低于「最高概率 × min-p」的候选直接丢弃。0 = 关闭；0.02~0.1 可抑制胡言乱语。",
    "--repeat-penalty": "重复惩罚：>1 抑制复读。对中文/代码容易造成用词变形，非必要保持 1.0。",
    "--repeat-last-n": "重复惩罚回看的 token 数。默认 64；对话很长可适当加大，过大反而压制正常复述。",
    "--presence-penalty": "存在惩罚：只要出现过就压一次。适合「别老提同一个话题」，0 = 关闭。",
    "--frequency-penalty": "频率惩罚：按出现次数压。适合抑制啰嗦，0 = 关闭。",
    "--seed": "随机种子。固定它能复现同一输出（做参数对比实验很有用）；-1/留空 = 每次随机。",
    "--n-predict": "单次生成上限（token）。-1 = 不额外限制。设个值可防模型啰嗦。",
    "--grammar": "GBNF 语法约束，强制输出格式（比如只要 JSON）。填语法文本；不约束就留空。",
    "--json-schema": "JSON Schema 约束，比手写 GBNF 省事。只要结构化输出时用。",
    "--jinja": "用模型自带的 Jinja 聊天模板（预设开启）。关掉会退回简单拼接，"
               "带工具调用/思考链的模型大多会表现异常。",
    "--chat-template": "直接覆盖聊天模板（Jinja 文本），用于模型元数据里的模板损坏或想自定义时。",
    "--chat-template-file": "从文件读聊天模板，内容同上，适合模板很长的情况。",
    "--reasoning-effort": "思考强度：none 关闭思考（最快）；low/medium/high 逐级更慢更稳。"
                          "留空 = 用模型模板的默认档。",
    "--reasoning-budget": "思考过程最多花多少 token。-1 = 让模型自己决定；0 = 立即结束思考；"
                          "N = 超过 N 个思考 token 后强制作答。",
    "--host": "监听地址。面板固定 127.0.0.1（只允许本机访问），一般不用改。",
    "--port": "对外端口，由启动项里的「端口」决定，这里填了也会被面板覆盖。",
    "--threads": "CPU 生成线程数。0/留空 = 用满硬件并发（引擎自动）。风扇太吵可限制到核数的一半左右。",
    "--threads-batch": "prefill 阶段用的线程数。默认跟随 threads；NUMA 机器上分开设有时代价更小。",
    "--timeout": "HTTP 读写超时（秒）。长思考 + 长上下文出现「请求中断」时调大。",
    "--api-key": "给这个模型端口加一道密钥。本机自用一般留空；填了客户端就必须带同样的 key。",
    "--cors-origins": "允许跨域访问的来源。默认 *；只给网页前端用时可以收紧。",
    "--metrics": "暴露 /metrics（Prometheus 格式）。不做监控就留空。",
    "--embeddings": "按「嵌入模型」运行（/v1/embeddings）。embedding 模型必须开；聊天模型别开。",
    "--pooling": "嵌入的池化方式：mean 平均（通用）、cls 取首 token、last 取末 token、rank 用于重排。"
                 "按模型卡选，选错会得到「看起来正常但没法用」的向量。",
    "--rerank": "按「重排模型」运行（/v1/rerank）。cross-encoder 类模型（bge-reranker 等）必须开。",
    "--slot-save-path": "把槽位 KV 状态存盘/读盘的目录，用于「同一段长上下文反复用」。不做就留空。",
    "--load-mode": "权重加载方式：auto = 默认 mmap 映射（权重文件页会驻留在进程内存里）；"
                 "none = 不映射、整个读进内存（相当于 --no-mmap，想压低内存占用/让权重全进显存时选它）；"
                 "mlock = 锁定内存防换页；mmap+mlock、dio 少用。本机显存够、想把模型全放显存时选 none。",
    "--numa": "NUMA 策略：distribute 分散、isolate 隔离、numactl 交给 numactl。单路桌面机留空。",
    "--prio": "进程优先级（小整数，默认 0）。想让它在后台别抢 CPU 就调低。",
    "--log-verbosity": "日志级别（0 静默 … 5 调试）。排错时临时调高，平时留空。",
    "--verbose": "打开详细日志（等价于把日志级别拉满）。排错用，平时别开。",
}

KV_HELP = {
    "--model": "要加载的 GGUF 文件（三元量化 PTQ1_0/PQ2_0 只有本引擎能读）。",
    "--mmproj": "视觉投影文件，多模态才需要。纯文本留空。",
    "--mmproj-offload": "勾选 = 视觉编码器放 GPU（更快、更费显存）；不勾 = 放 CPU（省显存、稍慢）。"
                        "显存不宽裕时保持不勾。",
    "--image-min-tokens": "单张图片最少编码成多少 token。调小省上下文，代价是细节看不清。留空用模型默认。",
    "--image-max-tokens": "单张图片最多编码成多少 token。调大能看清小字，但每张图都吃更多上下文。",
    "--ctx-size": "上下文窗口（token）。引擎默认只有 2048，预设给到 8192；"
                  "它只决定「能记多长」，KV 实际占多少由 budget / cpu-gb 决定，所以调大不等于直接吃满显存。",
    "--n-predict": "单次生成上限（token）。-1 = 不额外限制。",
    "--batch-size": "逻辑批大小（prefill 一次提交多少 token）。默认 512；长文档灌入时调大能快，"
                    "但瞬时显存更高。",
    "--ubatch-size": "物理批大小。显存告急时优先降它（256/128），代价是 prefill 变慢。",
    "--n-gpu-layers": "放到 GPU 的层数。默认 99（全放）。本引擎支持分层 KV，层数一般不用减。",
    "--threads": "CPU 生成线程数。<=0 = 用满硬件并发（默认）。",
    "--threads-batch": "prefill 线程数，默认跟随 --threads。",
    "--flash-attn": "FlashAttention：on/off/auto。预设 auto 表示「由引擎按你的设备判断」，"
                    "不预置具体值以免在不支持的卡上出错。",
    "--alias": "模型在 /v1/models 里显示的名字，客户端请求时填的 model。留空用文件名。",
    "--api-key": "给这个端口加密钥，多个用逗号分隔。本机自用留空。",
    "--load-mode": "权重加载方式：auto 交给引擎；mmap 映射（省内存）；none 全读进内存（首 token 快）；"
                   "mlock 锁定防换页；dio 直接 IO。默认 auto 最稳。",
    "--timeout": "HTTP 读写超时（秒，默认 1800）。长思考/长上下文出现中断时调大。",
    "--threads-http": "HTTP 服务线程数。<=0 = 自动。并发请求多时才有意义。",
    "--device": "指定推理设备，如 CUDA0；none = 纯 CPU。单卡留空。",
    "--main-gpu": "主设备序号（默认 0）。单卡留空。",
    "--split-mode": "多卡模式：none 单卡 / layer 分层。本引擎只支持这两种。",
    "--tensor-split": "多设备权重比例，只接受一个值（本引擎拒绝多段）。单卡留空。",
    "--temp": "采样温度：0 = 贪心；0.6~0.8 问答；0.9+ 创作。引擎按模型档位（思考/非思考）各有一套默认值。",
    "--top-p": "核采样阈值。0.9~0.95 通用；调低更保守。",
    "--top-k": "只保留概率最高的 K 个候选，0 = 关闭。常见 20~40。",
    "--min-p": "相对概率下限，0 = 关闭。0.02~0.1 可抑制胡言乱语。",
    "--presence-penalty": "存在惩罚，抑制「反复提同一个点」。0 = 关闭。",
    "--frequency-penalty": "频率惩罚，按出现次数压制。0 = 关闭。",
    "--repeat-penalty": "重复惩罚，>1 抑制复读；中文/代码容易用词变形，非必要保持 1。",
    "--seed": "随机种子，固定可复现；留空 = 每次随机。",
    "--verbosity": "日志级别：0 静默、1 error、2 warn、3 info（默认）、4 trace、5 debug。排错临时调高。",
    "--kvmem-trace": "输出原始 KVMEM_* 诊断行（分块、命中、下沉都打出来）。"
                     "排查「这段上下文为什么被丢了」时开。",
    "--kvmem-budget": "常驻显存的 KV token 数（GPU 工作集）。0 = 等于 ctx（全放显存，显存不够直接加载失败）。"
                      "按你的卡算：budget ≈ (权重之外的剩余显存 − 生成预留占用) ÷ 每 token 字节数。"
                      "每 token 字节数 = 日志 KVMEM_TIERS 里 slot_bytes ÷ 128。",
    "--kvmem-block-tokens": "KV 分块大小（token）。默认 128；分块小召回更细但管理开销略高。一般不用改。",
    "--kvmem-sink-tokens": "永不淘汰的开头前缀（系统提示、对话开场）。引擎硬约束：必须 ≥4096 且明显小于 budget，"
                           "否则每个请求都会 500。",
    "--kvmem-gen-reserve": "给「本轮生成」预留的 KV 空间（token，含思考）。默认 256 偏小，"
                           "预设 2048 适合长回答；要开深度思考再往上加，但必须小于 budget。",
    "--kvmem-recent-tokens": "选块时额外永远保留「最近 N 个 token」。默认 0。"
                             "长对话里模型「忘了刚说的话」时调到 1024~2048 通常立刻改善。",
    "--kvmem-method": "超出 GPU 工作集时挑哪些块留下：recency = 保留最近的（快、可预测）；"
                      "retrieval = 按当前提问去全部上下文里检索相关块（长文问答更准，但每请求更慢）。"
                      "引擎默认 retrieval。",
    "--kvmem-query-last": "构造检索查询时，从最后一条用户消息里取多少 token。默认 64。问题很长时可加大。",
    "--kvmem-query-max-tokens": "检索查询的 token 上限（默认 512）。问题特别长时它决定「只看多长」。",
    "--kvmem-query-replay": "检索查询的回放方式：auto（默认）按历史自动重建查询；legacy 是旧行为。",
    "--kvmem-query-policy": "检索查询来源：user（默认，用最新用户消息）或 legacy（旧策略）。",
    "--kvmem-mtp-state": "MTP（多 token 预测）状态处理：replay/snapshots/auto。没有 MTP 头就不生效。",
    "--kvmem-gpu-ratio": "KV 池最多占整卡显存的比例上限（默认 0.50），是「别把卡吃干」的第二道保险。"
                         "按你希望给 KV 留的余量设，和显存绝对值无关。",
    "--kvmem-cpu-gb": "KV 的 CPU 下沉池（GiB，0 = 关闭）。**启动即整块预留**，给多少就占多少内存。"
                      "想「全部上下文都留在内存里」，让它 ≥ (ctx − budget) × 每token字节 ÷ 1GiB；"
                      "按你机器的内存总量定，别照抄别人的数字。设大了纯浪费。",
    "--kvmem-nvme-gb": "NVMe 下沉池（GiB，0 = 关闭）。内存也不够时的第三层，会明显变慢，通常只在超大上下文时用。",
    "--kvmem-nvme-dir": "NVMe 下沉文件放哪个目录。默认临时目录；建议指到 SSD 上空间够的位置。",
    "--kvmem-harvest-v": "prefill 时把 V 用原始 K 重算并下传（省显存、略慢）。不知道为什么要它就别开。",
    "--kvmem-raw-k-nvme": "把原始 K 和 V 都落到 NVMe（需要同时设 kvmem-nvme-gb）。极致省内存、速度最慢。",
    "--kv-dtype": "GPU 上 KV 的统一精度：f16 无损；q8_0（默认）约省一半且几乎无损；q5_0/q4_0 更省但长上下文掉质量。",
    "--cache-type-k": "单独指定 K 的精度（覆盖 kv-dtype），用 llama.cpp 的命名。一般不用单独设。",
    "--cache-type-v": "单独指定 V 的精度（必须是量化类型之一）。一般不用单独设。",
    "--spec-type": "投机解码：none 关闭（默认）；draft-mtp 用模型自带 MTP 头。没有 MTP 头就留 none。",
    "--spec-kv-dtype": "MTP 草稿用的 KV 精度，默认 f16。开了投机解码才相关。",
    "--spec-draft-n-max": "每次最多猜几个 token（默认 3）。猜多收益大、浪费也多。",
    "--spec-draft-p-min": "草稿 token 的最低接受概率（默认 0）。",
    "--jinja": "使用模型自带 Jinja 模板（本引擎恒开，留空即可）。",
    "--chat-template": "覆盖聊天模板（Jinja 文本）。模型元数据模板损坏时才需要。",
    "--chat-template-file": "从文件读聊天模板，适合模板很长。",
    "--chat-template-kwargs": "模板默认参数（JSON），例如给模板传额外开关。",
    "--reasoning-effort": "思考强度：none 关闭；low/medium/high 逐级更慢更稳。留空用模板自带档位。",
    "--enable-thinking": "强制打开思考（默认关，请求里还能覆盖）。只在模型支持思考链时有意义。",
    "--no-think": "强制关闭思考。一般用 reasoning-effort=none 代替。",
    "--reasoning-budget": "思考预算：-1 不限；0 立即结束思考；N = 超过 N 个思考 token 后强制作答。",
    "--reasoning-budget-message": "强制结束思考时插入的提示语。一般不用填。",
}

# 两个引擎共用的中文名（kvmem 的 desc 是英文，直接截断会变成看不懂的片段）
COMMON_LABELS = {
    "--model": "模型路径", "--mmproj": "视觉投影文件",
    "--image-min-tokens": "图片最少 token", "--image-max-tokens": "图片最多 token",
    "--verbosity": "日志级别", "--host": "监听地址", "--port": "端口",
    "--ctx-size": "上下文长度", "--n-predict": "单次生成上限",
    "--batch-size": "逻辑批大小", "--ubatch-size": "物理批大小",
    "--n-gpu-layers": "GPU 层数", "--threads": "CPU 线程", "--threads-batch": "批处理线程",
    "--flash-attn": "FlashAttention", "--api-key": "接口密钥", "--timeout": "读超时（秒）",
    "--threads-http": "HTTP 线程", "--seed": "随机种子",
    "--kvmem-budget": "显存 KV 预算", "--kvmem-block-tokens": "KV 块大小",
    "--kvmem-sink-tokens": "前缀保留（sink）", "--kvmem-gen-reserve": "生成预留",
    "--kvmem-recent-tokens": "最近窗口 token", "--kvmem-query-last": "查询回退长度",
    "--kvmem-cpu-gb": "内存下沉池（GB）", "--kvmem-nvme-gb": "NVMe 下沉（GB）",
    "--kvmem-nvme-dir": "NVMe 目录", "--chat-template": "模板内容",
    "--chat-template-file": "模板文件", "--reasoning-budget": "思考预算",
    "--reasoning-budget-message": "强制结束提示语",
    "--kvmem-query-max-tokens": "检索查询上限", "--spec-draft-n-max": "草稿最大 token",
    "--spec-draft-p-min": "草稿最小概率", "--kvmem-harvest-v": "prefill 收割 V",
    "--kvmem-raw-k-nvme": "原始 K 落 NVMe", "--chat-template-kwargs": "模板默认参数",
}

# kvmem 的 help 解析不出类型/标签（占位符写法不统一），这里按语义补齐
KV_META = {
    "--flash-attn": ("select", ["on", "off", "auto"], "FlashAttention"),
    "--alias": ("text", [], "模型别名"),
    "--load-mode": ("select", ["auto", "none", "mmap", "mlock", "mmap+mlock", "dio"], "权重加载方式"),
    "--device": ("text", [], "推理设备"),
    "--split-mode": ("select", ["none", "layer"], "多卡切分方式"),
    "--tensor-split": ("text", [], "各设备权重比例"),
    "--main-gpu": ("number", [], "主设备序号"),
    "--temp": ("number", [], "温度"),
    "--top-p": ("number", [], "top-p"),
    "--top-k": ("number", [], "top-k"),
    "--min-p": ("number", [], "min-p"),
    "--presence-penalty": ("number", [], "存在惩罚"),
    "--frequency-penalty": ("number", [], "频率惩罚"),
    "--repeat-penalty": ("number", [], "重复惩罚"),
    "--kvmem-gpu-ratio": ("number", [], "KV 池显存占比上限"),
    "--kv-dtype": ("select", ["f16", "f32", "q8_0", "q5_0", "q4_0"], "KV 精度（统一）"),
    "--cache-type-k": ("select", ["f16", "q8_0", "q5_0", "q4_0"], "KV 精度 K"),
    "--cache-type-v": ("select", ["f16", "q8_0", "q5_0", "q4_0"], "KV 精度 V"),
    "--spec-type": ("select", ["none", "draft-mtp"], "投机解码类型"),
    "--spec-kv-dtype": ("select", ["f16", "q8_0", "q5_0", "q4_0"], "MTP KV 精度"),
    "--spec-draft-n-max": ("number", [], "草稿最大 token"),
    "--spec-draft-p-min": ("number", [], "草稿最小概率"),
    "--jinja": ("switch", [], "启用 Jinja 模板"),
    "--enable-thinking": ("switch", [], "强制开启思考"),
    "--no-think": ("switch", [], "强制关闭思考"),
    "--reasoning-effort": ("select", ["none", "low", "medium", "high"], "思考强度"),
    "--kvmem-trace": ("switch", [], "KVMEM 诊断日志"),
    "--mmproj-offload": ("switch", [], "视觉编码器放 GPU"),
    "--kvmem-harvest-v": ("switch", [], "prefill 收割 V"),
    "--kvmem-raw-k-nvme": ("switch", [], "原始 K 落 NVMe"),
    "--chat-template-kwargs": ("text", [], "模板默认参数（JSON）"),
    "--kvmem-method": ("select", ["recency", "retrieval"], "取块策略"),
    "--kvmem-query-policy": ("select", ["user", "legacy"], "检索查询来源"),
    "--kvmem-query-replay": ("select", ["auto", "legacy"], "检索查询回放"),
    "--kvmem-mtp-state": ("select", ["auto", "snapshots", "replay"], "MTP 状态处理"),
}

KV_GROUPS = [
    ("模型与上下文", ("mmproj", "image", "mtmd", "-model", "ctx-size", "n-predict",
                  "gpu-layers", "batch", "ubatch", "keep", "alias")),
    ("KV 池与显存", ("kvmem-budget", "kvmem-sink", "kvmem-gen", "kvmem-cpu", "kvmem-gpu",
                 "kvmem-nvme", "kvmem-block", "kvmem-harvest", "kvmem-raw", "kv-dtype",
                 "cache-type", "kv-offload", "swa", "spec-kv", "flash-attn", "load-mode")),
    ("检索与淘汰", ("kvmem-method", "kvmem-query", "kvmem-recent", "kvmem-mtp")),
    ("投机解码", ("spec-",)),
    ("思考与模板", ("thinking", "think", "reasoning", "jinja", "chat-template", "chat-template-kwargs")),
    ("采样", ("temp", "top-", "min-p", "repeat", "presence", "frequency", "seed", "xtc",
            "typical", "mirostat", "dynatemp", "dry-", "grammar", "json-schema", "logit-bias",
            "sampler-seq", "n-predict", "keep")),
    ("服务与网络", ("host", "port", "ui", "api-key", "cors", "timeout", "threads", "batch",
               "ubatch", "slot", "metrics", "embeddings", "pooling", "rerank", "alias",
               "device", "split-mode", "tensor-split", "main-gpu", "webui")),
    ("日志与调试", ("verbosity", "kvmem-trace", "perf", "verbose")),
]


def _group_of(flag: str) -> str:
    for gname, keys in KV_GROUPS:
        if any(k in flag for k in keys):
            return gname
    return "其它"




def _enrich(sp, flag):
    """补 min/max、路径选择、类型可见性。"""
    if flag in RANGE:
        sp["min"], sp["max"] = RANGE[flag]
    if flag in PATH_FIELDS:
        sp["pathkind"], sp["accept"] = PATH_FIELDS[flag]
    if sp.get("kind") == "number":
        sp.setdefault("min", 0)
    sp["types"] = ["chat", "rerank", "embed"] if flag in NON_CHAT_KEEP else ["chat"]
    return sp


def kv_specs():
    rows = parse_help(KV_HELP_PATH)
    used = set()
    out = []
    for it in rows:
        flag = it["flag"]
        if flag in used or flag in KV_HIDE:
            continue
        used.add(flag)
        key = key_of(flag)
        kind, choices = kind_of(it["hint"])
        label = it["desc"][:26] or key
        if flag in KV_META:
            kind, choices, label = KV_META[flag]
        elif flag in COMMON_LABELS:
            label = COMMON_LABELS[flag]
        group = _group_of(flag)
        out.append(_enrich({
            "key": key, "flag": flag, "short": it["short"], "group": group,
            "label": label, "kind": kind, "choices": choices, "hint": it["hint"],
            "default": default_of(it["desc"]), "desc": it["desc"][:160],
            "preset": PRESET["kvmem"].get(key, ""),
            "help": KV_HELP.get(flag, ""),
        }, flag))
    return out


# ---------------------------------------------------------------- llama 常用
# 白名单：flag -> 中文标签（顺序即面板顺序；不在这里的参数仍可用「额外参数」补）
LLAMA_PICK = [
    ("模型", [
        ("--model", "模型路径", "text"),
        ("--alias", "模型别名", "text"),
        ("--mmproj", "视觉投影文件", "text"),
        ("--lora", "LoRA 适配器", "text"),
    ]),
    ("上下文与批处理", [
        ("--ctx-size", "上下文长度", "number"),
        ("--batch-size", "逻辑批大小", "number"),
        ("--ubatch-size", "物理批大小", "number"),
        ("--parallel", "并行槽数", "number"),
        ("--keep", "保留初始 token", "number"),
        ("--context-shift", "上下文滚动", "select"),
        ("--cache-reuse", "前缀复用最少块", "number"),
    ]),
    ("显存与 KV", [
        ("--n-gpu-layers", "GPU 层数", "number"),
        ("--split-mode", "多卡切分方式", "select"),
        ("--tensor-split", "各卡权重比例", "text"),
        ("--main-gpu", "主卡编号", "number"),
        ("--flash-attn", "FlashAttention", "select"),
        ("--cache-type-k", "KV 精度 K", "select"),
        ("--cache-type-v", "KV 精度 V", "select"),
        ("--no-kv-offload", "KV 不放进显存", "switch"),
        ("--swa-full", "SWA 全尺寸缓存", "switch"),
    ]),
    ("长上下文扩展", [
        ("--rope-scaling", "RoPE 缩放方式", "select"),
        ("--rope-scale", "RoPE 缩放倍数", "number"),
        ("--rope-freq-base", "RoPE 基准频率", "number"),
        ("--rope-freq-scale", "RoPE 频率缩放", "number"),
        ("--yarn-orig-ctx", "YaRN 原始上下文", "number"),
        ("--yarn-ext-factor", "YaRN 外推系数", "number"),
        ("--yarn-attn-factor", "YaRN 注意力系数", "number"),
        ("--yarn-beta-slow", "YaRN beta 慢", "number"),
        ("--yarn-beta-fast", "YaRN beta 快", "number"),
    ]),
    ("投机解码", [
        ("--spec-type", "投机类型", "select"),
        ("--spec-draft-model", "草稿模型", "text"),
        ("--spec-draft-n-max", "草稿最大 token", "number"),
        ("--spec-draft-n-min", "草稿最小 token", "number"),
        ("--spec-draft-p-min", "草稿最小概率", "number"),
    ]),
    ("采样", [
        ("--temp", "温度", "number"),
        ("--top-p", "top-p", "number"),
        ("--top-k", "top-k", "number"),
        ("--min-p", "min-p", "number"),
        ("--repeat-penalty", "重复惩罚", "number"),
        ("--repeat-last-n", "重复惩罚窗口", "number"),
        ("--presence-penalty", "存在惩罚", "number"),
        ("--frequency-penalty", "频率惩罚", "number"),
        ("--seed", "随机种子", "number"),
        ("--n-predict", "单次生成上限", "number"),
        ("--grammar", "语法约束（GBNF）", "text"),
        ("--json-schema", "JSON Schema 约束", "text"),
    ]),
    ("思考与模板", [
        ("--jinja", "启用 Jinja 模板", "switch"),
        ("--chat-template", "模板内容", "text"),
        ("--chat-template-file", "模板文件", "text"),
        ("--reasoning-effort", "思考强度", "select"),
        ("--reasoning-budget", "思考预算", "number"),
    ]),
    ("服务与网络", [
        ("--host", "监听地址", "text"),
        ("--port", "端口", "number"),
        ("--threads", "CPU 线程", "number"),
        ("--threads-batch", "批处理线程", "number"),
        ("--timeout", "读超时（秒）", "number"),
        ("--api-key", "接口密钥", "text"),
        ("--cors-origins", "CORS 允许来源", "text"),
        ("--metrics", "暴露 metrics", "switch"),
        ("--embeddings", "启用 embeddings", "switch"),
        ("--pooling", "池化方式", "select"),
        ("--rerank", "按 reranker 运行", "switch"),
        ("--slot-save-path", "槽位存档目录", "text"),
    ]),
    ("性能与调试", [
        ("--load-mode", "权重加载方式", "select"),
        ("--numa", "NUMA 策略", "select"),
        ("--prio", "进程优先级", "number"),
        ("--log-verbosity", "日志级别", "number"),
        ("--verbose", "详细日志", "switch"),
    ]),
]

LLAMA_CHOICES = {
    "--load-mode": ["auto", "none", "mmap", "mlock", "mmap+mlock", "dio"],
    "--flash-attn": ["on", "off", "auto"],
    "--cache-type-k": ["f16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"],
    "--cache-type-v": ["f16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0"],
    "--split-mode": ["none", "layer", "row"],
    "--rope-scaling": ["none", "linear", "yarn"],
    "--spec-type": ["none", "draft-mtp", "draft-simple", "ngram"],
    "--context-shift": ["on", "off"],
    "--pooling": ["none", "mean", "cls", "last", "rank"],
    "--reasoning-effort": ["none", "low", "medium", "high"],
    "--numa": ["", "distribute", "isolate", "numactl"],
}


def llama_specs():
    # help 里一行可能写多个长别名（`-ngl, --gpu-layers, --n-gpu-layers`、
    # `--embedding, --embeddings`），按任意别名都能查到，否则白名单里的名字找不到行、参数整条丢失
    help_rows = {}
    for it in parse_help(LLAMA_HELP_PATH):
        for f in it.get("flags") or [it["flag"]]:
            help_rows.setdefault(f, it)
    out = []
    for group, rows in LLAMA_PICK:
        for flag, label, kind in rows:
            if flag not in help_rows:
                continue
            it = help_rows[flag]
            key = key_of(flag)
            choices = LLAMA_CHOICES.get(flag, [])
            out.append(_enrich({
                "key": key, "flag": flag, "short": it["short"], "group": group,
                "label": label, "kind": ("select" if choices else kind),
                "choices": choices, "hint": it["hint"],
                "default": default_of(it["desc"]), "desc": it["desc"][:160],
                "preset": PRESET["llama"].get(key, ""),
                "help": LLAMA_HELP.get(flag, ""),
            }, flag))
    return out


def main():
    kv = kv_specs()
    lm = llama_specs()
    body = [
        '"""参数面板规格表（由 tools/gen_param_specs.py 从引擎 --help 生成，勿手改）。',
        "",
        "字段：key（前端表单字段名）/ flag（命令行参数）/ kind（number|text|select|switch）",
        "      choices / group（面板分组）/ label / hint / default（引擎默认）/ desc（引擎原文）",
        "      preset（按类型的可运行默认）/ help（中文详解）/ min,max（数值范围）",
        "      pathkind,accept（需要选文件的项）/ types（该参数对哪些模型类型可见）",
        '"""',
        "",
        "KV_SPECS = " + pprint.pformat(kv, width=110, sort_dicts=False),
        "",
        "LLAMA_SPECS = " + pprint.pformat(lm, width=110, sort_dicts=False),
        "",
        "# 按模型类型追加的预设（key -> 值）",
        "TYPE_PRESET = " + pprint.pformat(TYPE_PRESET, width=96, sort_dicts=False),
        "",
        "# 面板顶部说明（为什么有些项是空的）",
        "PRESET_NOTE = " + pprint.pformat(PRESET_NOTE, width=96, sort_dicts=False),
        "",
        "",
        "def specs_for(engine: str):",
        '    return LLAMA_SPECS if engine == "llama" else KV_SPECS',
        "",
        "",
        "def flag_map(engine: str):",
        '    """{表单 key: flag}，后端拼命令行时用。"""',
        "    return {s['key']: s['flag'] for s in specs_for(engine)}",
        "",
        "",
        "def presets(engine: str, mtype: str = ''):",
        '    """该引擎（可选：按模型类型）的推荐初值，只含与设备无关的项。"""',
        "    out = {s['key']: s['preset'] for s in specs_for(engine)",
        "           if s.get('preset') not in ('', None)}",
        "    out.update((TYPE_PRESET.get(engine) or {}).get(mtype) or {})",
        "    return out",
        "",
    ]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    print("写出", OUT)
    print("llama 参数", len(lm), "kvmem 参数", len(kv))
    print("llama 有详解", sum(1 for s in lm if s["help"]), "/", len(lm),
          "kvmem 有详解", sum(1 for s in kv if s["help"]), "/", len(kv))
    miss = [s["flag"] for s in kv if not s["help"]]
    if miss:
        print("kvmem 无详解：", " ".join(miss))
    print("预设 llama", json.dumps(PRESET["llama"], ensure_ascii=False),
          "kvmem", json.dumps(PRESET["kvmem"], ensure_ascii=False))


if __name__ == "__main__":
    main()
