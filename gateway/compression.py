"""上下文压缩状态机。

分层（成本从低到高，只有最后一层有损）：
  L1 clean        归一空白 / 丢客户端元字段 / 丢空 assistant      —— 无损
  L2 trim_middle  从最旧处按"整轮"裁掉中段，落 T0 块            —— 无损（原文在库，可 peek/expand）
  L3 fold_long    单条超长（工具输出/长引用）折叠为首尾摘录 + T1 块 —— 无损（可 expand）
  L4 aggregate    相邻多个折叠块合并成 T2 块（最近 N 个 T1 受保护）  —— 无损（可 expand）
  L5 summary      仍未降到硬阈值以下才调 LLM 做结构化摘要（有损）   —— 有损

三条硬规则
1) 追加优先：块一旦生成即固定排序（created_at,rowid），后续只追加或合并，不重排历史前缀。
2) 摘要请求必须保前缀：指令追加在对话末尾，逐字复用主请求消息，不另起 system 提示（§6.4）。
3) 摘要失败不阻塞：重试一次后降级为规则摘要（截断 + 关键字段抽取）。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import estimate as est
from .config import Model
from .store import Store

SUMMARY_INSTRUCTION = (
    "请把上面的对话中较早的部分压缩成结构化摘要，供后续轮次继续使用。\n"
    "只输出一个 JSON 对象，不要解释、不要代码块，字段固定为：\n"
    '{"goal":"用户最终想达成什么","decisions":["已确认的决定"],'
    '"files":["涉及的文件/路径"],"errors":["遇到并已解决的问题"],'
    '"open_questions":["尚未解决的问题"]}\n'
    "要求：保留具体数字、命名、约束条件；不复述寒暄；总长不超过 300 字。"
)


class Compressor:
    def __init__(self, store: Store, cfg: Dict[str, Any]):
        self.store = store
        self.cfg = cfg or {}
        self.soft_ratio = float(self.cfg.get("trigger_lossless_ratio", 0.70))
        self.hard_ratio = float(self.cfg.get("trigger_summary_ratio", 0.90))
        self.protect = int(self.cfg.get("recent_t1_blocks_protected", 3))
        self.fold_limit = int(self.cfg.get("max_fold_tokens_per_message", 1200))
        self.keep_tail = int(self.cfg.get("keep_tail_messages", 8))

    # ---------------------------------------------------------------- 块
    def _next_block_id(self, session_id: str) -> str:
        n = len(self.store.list_blocks(session_id)) + 1
        return f"b_{n:04d}"

    def _save_block(self, session_id: str, tier: str, content: Dict[str, Any],
                    token_count: int, source_range: List[int],
                    parent_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
        block = {
            "block_id": self._next_block_id(session_id),
            "tier": tier,
            "parent_ids": parent_ids or [],
            "token_count": int(token_count),
            "content": content,
            "source_range": source_range,
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
        return self.store.save_block(session_id, block)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _norm(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            if role not in ("system", "user", "assistant", "tool"):
                role = "user"
            out.append({"role": role, "content": m.get("content", "")})
        return out

    @staticmethod
    def _squash(msg: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        text = est.content_to_text(msg["content"])
        sq = est.squash_whitespace(text)
        if sq == text:
            return msg, 0
        saved = est.estimate_text(text) - est.estimate_text(sq)
        return {"role": msg["role"], "content": sq}, max(0, saved)

    def _placeholder(self, block_id: str, kind: str, note: str, tokens: int) -> Dict[str, Any]:
        return {"role": "user",
                "content": f"[折叠块 {block_id}｜{kind}｜约 {tokens} tokens] {note}\n"
                           f"（原文已存档；需要细节时调用 expand(\"{block_id}\") 还原，peek 只读不占上下文）"}

    # ---------------------------------------------------------------- 主流程
    def build(self, model: Model, session_id: str, messages: List[Dict[str, Any]],
              thinking_on: bool, expand_ids: Optional[List[str]] = None,
              summarizer: Optional[Callable[[Model, List[Dict[str, Any]]], Optional[Dict[str, Any]]]] = None,
              persist: bool = True
              ) -> Dict[str, Any]:
        """persist=False = 只估算不落库：给「优化上下文」按钮做预览，
        避免点一次就在 blocks 表里多存一份历史（真正生效仍在下一次发送时）。"""
        msgs = self._norm(messages)
        budget = model.context.budget(thinking_on)
        soft, hard = int(budget * self.soft_ratio), int(budget * self.hard_ratio)
        before = est.messages_tokens(msgs)
        events: List[Dict[str, Any]] = []

        def save(tier: str, content: Dict[str, Any], tokens: int, rng: List[int],
                 parents: Optional[List[str]] = None) -> Dict[str, Any]:
            if persist:
                return self._save_block(session_id, tier, content, tokens, rng, parents)
            return {"block_id": "预览", "tier": tier, "content": content,
                    "token_count": int(tokens), "source_range": rng}

        # ---- L1 clean（无损）
        cleaned, saved_clean = [], 0
        for m in msgs:
            if m["role"] == "assistant" and not est.content_to_text(m["content"]).strip():
                continue
            m2, s = self._squash(m)
            cleaned.append(m2)
            saved_clean += s
        if saved_clean > 0:
            events.append({"type": "compression", "tier": "T0", "action": "clean",
                           "lossless": True, "saved_tokens": saved_clean,
                           "tokens_before": before, "tokens_after": est.messages_tokens(cleaned)})
        msgs = cleaned

        # ---- L2 trim_middle（无损，落 T0 块）
        while est.messages_tokens(msgs) > soft and len(msgs) > self.keep_tail + 1:
            head_n = 1 if msgs and msgs[0]["role"] == "system" else 0
            drop_from = head_n
            drop_to = len(msgs) - self.keep_tail
            # 至少留最后一条 user
            if drop_to <= drop_from:
                break
            span = msgs[drop_from:drop_to]
            removed_tokens = est.messages_tokens(span)
            block = save("T0", {"kind": "trimmed", "note": "中段裁剪",
                                "original": span, "message_count": len(span)},
                         removed_tokens, [drop_from, drop_to - 1])
            ph = self._placeholder(block["block_id"], "T0 裁剪",
                                   f"已裁剪 {len(span)} 条较早消息", removed_tokens)
            msgs = msgs[:drop_from] + [ph] + msgs[drop_to:]
            events.append({"type": "compression", "tier": "T0", "action": "trim",
                           "lossless": True, "block_id": block["block_id"],
                           "saved_tokens": removed_tokens,
                           "tokens_before": before, "tokens_after": est.messages_tokens(msgs)})

        # ---- L3 fold_long（无损，落 T1 块）
        folded_any = False
        for i, m in enumerate(msgs):
            mt = est.message_tokens(m)
            if mt <= self.fold_limit:
                continue
            text = est.content_to_text(m["content"])
            head = text[: int(len(text) * 0.15)]
            tail = text[-int(len(text) * 0.10):]
            block = save("T1", {"kind": "fold", "note": "单条超长折叠",
                                "original": [m], "chars": len(text)},
                         mt, [i, i])
            msgs[i] = self._placeholder(block["block_id"], "T1 折叠",
                                        f"首段：{head[:80]}… 末段：{tail[-80:]}", mt)
            folded_any = True
            events.append({"type": "compression", "tier": "T1", "action": "fold",
                           "lossless": True, "block_id": block["block_id"],
                           "saved_tokens": max(0, mt - est.message_tokens(msgs[i])),
                           "tokens_before": before, "tokens_after": est.messages_tokens(msgs)})

        # ---- L4 aggregate（无损，落 T2 块）
        if folded_any:
            blocks = self.store.list_blocks(session_id)
            t1 = [b for b in blocks if b["tier"] == "T1"]
            mergeable = t1[:-self.protect] if len(t1) > self.protect else []
            if len(mergeable) >= 4:
                ids = [b["block_id"] for b in mergeable]
                total = sum(int(b["token_count"]) for b in mergeable)
                parent = save("T2", {"kind": "aggregate", "note": "折叠块聚合",
                                     "children": ids}, total,
                              [min(b["source_range"][0] for b in mergeable if b["source_range"]),
                               max(b["source_range"][-1] for b in mergeable if b["source_range"])],
                              parents=ids)
                events.append({"type": "compression", "tier": "T2", "action": "aggregate",
                               "lossless": True, "block_id": parent["block_id"],
                               "saved_tokens": 0, "tokens_before": before,
                               "tokens_after": est.messages_tokens(msgs),
                               "detail": f"合并 {len(ids)} 个 T1 块"})

        # ---- L5 summary（有损，最后手段）
        after = est.messages_tokens(msgs)
        lossless = True
        can_summarize = summarizer is not None and self.cfg.get("summary", {}).get("enabled", True)
        if after > hard and can_summarize and persist:
            data = None
            try:
                data = summarizer(model, msgs)
            except Exception:
                data = None
            if data is None:
                data = self._rule_summary(msgs)   # 熔断降级：不阻塞
            else:
                lossless = False
            block = save("T1", {"kind": "summary", "note": "LLM 结构化摘要", "content": data},
                         after, [0, len(msgs) - 1])
            head_n = 1 if msgs and msgs[0]["role"] == "system" else 0
            tail = msgs[-self.keep_tail:] if len(msgs) > self.keep_tail else msgs[head_n:]
            body = ("[摘要块 " + block["block_id"] + "｜T1 摘要]\n" +
                    json.dumps(data, ensure_ascii=False, indent=1))
            msgs = msgs[:head_n] + [{"role": "user", "content": body}] + tail
            events.append({"type": "compression", "tier": "T1", "action": "summary",
                           "lossless": lossless, "block_id": block["block_id"],
                           "saved_tokens": max(0, after - est.messages_tokens(msgs)),
                           "tokens_before": before, "tokens_after": est.messages_tokens(msgs),
                           "detail": "LLM 摘要" if not lossless else "规则摘要（摘要模型不可用，已降级）"})
        elif after > hard and can_summarize and not persist:
            # 预览模式不调用模型：用规则摘要估算，避免点一下按钮就等十几秒
            data = self._rule_summary(msgs)
            head_n = 1 if msgs and msgs[0]["role"] == "system" else 0
            body = ("[摘要块 预览｜T1 摘要]\n" + json.dumps(data, ensure_ascii=False, indent=1))
            msgs = msgs[:head_n] + [{"role": "user", "content": body}] + msgs[-self.keep_tail:]
            events.append({"type": "compression", "tier": "T1", "action": "summary",
                           "lossless": True, "block_id": "预览",
                           "saved_tokens": max(0, after - est.messages_tokens(msgs)),
                           "tokens_before": before, "tokens_after": est.messages_tokens(msgs),
                           "detail": "预览（规则摘要估算）"})

        # ---- expand：把指定块的原文放回视图
        if expand_ids:
            msgs = self._apply_expand(session_id, msgs, expand_ids)

        return {"messages": msgs, "events": events, "lossless": all(e.get("lossless", True) for e in events),
                "tokens_before": before, "tokens_after": est.messages_tokens(msgs),
                "budget": budget}

    # ---------------------------------------------------------------- expand / peek
    def _apply_expand(self, session_id: str, view: List[Dict[str, Any]],
                      block_ids: List[str]) -> List[Dict[str, Any]]:
        limit = int(self.cfg.get("expand_budget_per_request", 3))
        restored: List[Dict[str, Any]] = []
        for bid in block_ids[:limit]:
            b = self.store.get_block(session_id, bid)
            if not b:
                continue
            raw = b["content"].get("original")
            if isinstance(raw, list):
                restored += [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in raw]
        if not restored:
            return view
        head_n = 1 if view and view[0]["role"] == "system" else 0
        return view[:head_n] + restored + view[head_n:]

    def peek(self, session_id: str, block_id: str) -> Dict[str, Any]:
        b = self.store.get_block(session_id, block_id)
        if not b:
            raise KeyError(block_id)
        c = b["content"]
        if c.get("kind") == "trimmed":
            texts = [est.content_to_text(m.get("content")) for m in c.get("original", [])]
            body = "\n\n---\n\n".join(texts)
        elif c.get("kind") in ("fold", "summary"):
            body = "\n\n".join(est.content_to_text(m.get("content")) for m in c.get("original", [])) or \
                   json.dumps(c.get("content", {}), ensure_ascii=False, indent=1)
        else:
            body = json.dumps(c, ensure_ascii=False, indent=1)
        digest = hashlib.sha256(json.dumps(c, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"block_id": block_id, "tier": b["tier"], "tokens": b["token_count"],
                "text": body, "sha256_ok": digest == b["sha256"]}

    # ---------------------------------------------------------------- 降级摘要
    @staticmethod
    def _rule_summary(msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
        users = [est.content_to_text(m["content"]) for m in msgs if m["role"] == "user"]
        files = sorted({s for t in users for s in re.findall(r"[\w./\\-]+\.(?:py|js|ts|tsx|md|json|txt|gguf)", t)})
        return {"goal": (users[0][:120] if users else ""),
                "decisions": [u[:80] for u in users[-3:]],
                "files": files[:10], "errors": [], "open_questions": []}
