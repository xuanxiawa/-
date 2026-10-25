"""把她自己发出的每一条消息都记进对话历史。

AstrBot 只在「LLM 跑过」的那一轮写历史，下面两条路都不会记：
  1. 命令 / 事件回复：`yield event.plain_result(...)`，直接出结果，LLM 没跑
  2. 主动消息：`context.send_message(...)`，绕开整条管线
结果就是她说的话自己看不见，下一句接不上。

这个插件放在发送层统一补记：
  - `after_message_sent` 钩子：接住管线发出的消息（命令回复、正常回复都覆盖）
  - 包一层 `context.send_message`：接住所有主动消息
跟 LLM 已经写进历史的那条自动去重，不会重复。
"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_MAX_LEN = 300  # 单条最多记多少字，免得命令的整屏输出把上下文撑爆
_OUTGOING_LOG = r"D:\AstrBot\recent-outgoing.jsonl"


def _plain(msg: dict) -> str:
    """把一条历史消息里的文字抽出来（内容可能是字符串，也可能是分段列表）。"""
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = []
        for seg in c:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str(seg.get("text") or ""))
            elif isinstance(seg, str):
                parts.append(seg)
        return "".join(parts).strip()
    return str(c or "").strip()


@register(
    "history_bridge",
    "dafeiyu",
    "把她自己发出的每条消息都记进对话历史，避免下一句接不上",
    "1.0.0",
)
class HistoryBridge(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._orig_send = None
        self._patched = False

    @filter.on_astrbot_loaded()
    async def start(self):
        # 包一层 send_message，接管所有主动消息
        if self._patched:
            return
        try:
            self._orig_send = self.context.send_message
            bridge = self

            async def send_message(session, message_chain):
                ok = await bridge._orig_send(session=session, message_chain=message_chain)
                try:
                    text = (message_chain.get_plain_text() or "").strip()
                    if text:
                        await bridge._append(str(session), text)
                except Exception:  # noqa: BLE001
                    logger.debug("history_bridge: 记录主动消息失败", exc_info=True)
                return ok

            self.context.send_message = send_message
            self._patched = True
            logger.info("history_bridge: 已接管 send_message")
        except Exception:  # noqa: BLE001
            logger.warning("history_bridge: 接管 send_message 失败", exc_info=True)

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent):
        """管线发完消息后补记一条（命令回复走这里）。"""
        try:
            res = event.get_result()
            chain = getattr(res, "chain", None) if res else None
            if not chain:
                return
            text = (chain.get_plain_text() or "").strip()
            if not text:
                return
            umo = event.unified_msg_origin
            got = await self._load(umo)
            if not got:
                return
            cid, history = got
            if history and history[-1].get("role") == "assistant" and _plain(history[-1]) == text:
                return  # LLM 那边已经记过了
            user = (event.message_str or "").strip()
            if user:
                history.append({"role": "user", "content": user[:200]})
            history.append({"role": "assistant", "content": text[:_MAX_LEN]})
            await self._save(umo, cid, history)
            self._log_outgoing(text)
        except Exception:  # noqa: BLE001
            logger.debug("history_bridge: 记录发送失败", exc_info=True)

    # ---------- 内部 ----------
    async def _append(self, umo: str, text: str) -> None:
        got = await self._load(umo)
        if not got:
            return
        cid, history = got
        if history and history[-1].get("role") == "assistant" and _plain(history[-1]) == text:
            return
        history.append({"role": "assistant", "content": text[:_MAX_LEN]})
        await self._save(umo, cid, history)
        self._log_outgoing(text)

    @staticmethod
    def _log_outgoing(text: str) -> None:
        try:
            import time

            with open(_OUTGOING_LOG, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps({"ts": time.time(), "text": text[:_MAX_LEN]}, ensure_ascii=False)
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            pass

    async def _load(self, umo: str):
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await cm.get_conversation(umo, cid)
        if not conv:
            return None
        try:
            history = json.loads(conv.history or "[]")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(history, list):
            return None
        return cid, history

    async def _save(self, umo: str, cid: str, history: list) -> None:
        await self.context.conversation_manager.update_conversation(
            umo, cid, history=history
        )
