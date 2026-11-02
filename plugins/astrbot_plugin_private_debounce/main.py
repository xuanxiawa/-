"""私聊连发合并：等他打完再回，而不是看一句回一句；撤回的不回。

做法：
- 第一条私聊进来后，在消息处理阶段先等「安静 QUIET 秒」。
- 这期间又来的消息：只把文字并进缓冲，本人不再触发回复。
- 同一条消息被撤回（friend_recall / group_recall）：从缓冲里删掉；
  全撤了就整轮不回。
- 安静满：把缓冲拼成一段，替换 event.message_str，只生成一次回复。

群聊不合并（多人消息不能拼）。
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLATFORM_ID = "qq_dafeiyu"
QUIET = 5.0      # 最后一条之后安静多久才回
MAX_WAIT = 30.0  # 最多等这么久，防止一直连发卡死


def _raw_get(raw, key: str, default: str = "") -> str:
    """OneBot 的 raw_message 有的版本是 dict，有的是 Event 对象，两种都兜住。"""
    try:
        return str(raw.get(key, default) or default)
    except Exception:  # noqa: BLE001
        try:
            return str(raw[key] or default)
        except Exception:  # noqa: BLE001
            return default


def _notice_type(event: AstrMessageEvent) -> str:
    raw = getattr(event.message_obj, "raw_message", None)
    if not raw:
        return ""
    return _raw_get(raw, "notice_type")


@register(
    "private_debounce",
    "dafeiyu",
    "私聊连发合并：等他打完两句再一起想、一起回；撤回的不回",
    "1.1.0",
)
class PrivateDebounce(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._buf: dict[str, dict] = {}

    # ---------- 撤回 ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=110)
    async def on_recall(self, event: AstrMessageEvent):
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            ntype = _notice_type(event)
            if ntype not in ("friend_recall", "group_recall"):
                return
            raw = event.message_obj.raw_message
            mid = _raw_get(raw, "message_id")
            umo = event.unified_msg_origin
            buf = self._buf.get(umo)
            if not buf or not mid:
                return
            before = len(buf["items"])
            buf["items"] = [it for it in buf["items"] if str(it.get("id")) != mid]
            if len(buf["items"]) == before:
                return
            logger.info(
                f"debounce: 撤回一条，剩 {len(buf['items'])} 条"
                f"（{len(buf['items']) or '全部'}）"
            )
            if not buf["items"]:
                buf["cancelled"] = True
        except Exception:  # noqa: BLE001
            logger.debug("debounce: 处理撤回失败", exc_info=True)

    # ---------- 收集 / 等待 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def collect(self, event: AstrMessageEvent):
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return
            umo = event.unified_msg_origin
            mid = str(getattr(event.message_obj, "message_id", "") or "")
            buf = self._buf.get(umo)

            # 不是本轮的第一条：并进去，自己不再回
            if buf is not None:
                buf["items"].append({"id": mid, "text": text})
                buf["last"] = time.time()
                event.call_llm = True
                event.stop_event()
                logger.info(f"debounce: 并进上一条（本轮第 {len(buf['items'])} 条）")
                return

            buf = {
                "items": [{"id": mid, "text": text}],
                "last": time.time(),
                "start": time.time(),
                "cancelled": False,
            }
            self._buf[umo] = buf
            try:
                while True:
                    if buf["cancelled"] or not buf["items"]:
                        break
                    remain = QUIET - (time.time() - float(buf["last"]))
                    if remain <= 0:
                        break
                    if time.time() - float(buf["start"]) > MAX_WAIT:
                        break
                    await asyncio.sleep(min(0.25, max(0.05, remain)))
                texts = [it["text"] for it in buf["items"]]
            finally:
                if self._buf.get(umo) is buf:
                    self._buf.pop(umo, None)

            if not texts:
                event.call_llm = True
                event.stop_event()
                logger.info("debounce: 消息被撤回 / 清空，这轮不回")
                return

            merged = "\n".join(texts).strip()
            event.message_str = merged
            event.set_extra("_think_pause_done", True)
            logger.info(f"debounce: 合并 {len(texts)} 条，共 {len(merged)} 字，开始生成")
        except Exception:  # noqa: BLE001
            logger.debug("debounce: 合并失败", exc_info=True)
