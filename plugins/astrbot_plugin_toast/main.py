"""QQ → 桌面气泡：私聊里大肥鱼的回复，会同时弹一个 Windows 托盘气泡。"""

from __future__ import annotations

import io

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_QUEUE = r"D:\AstrBot\toast-queue.txt"


@register("toast", "dafeiyu", "把 QQ 回复弹到桌面右下角", "1.0.0")
class Toast(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.on_llm_response()
    async def on_reply(self, event: AstrMessageEvent, resp):
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            if event.is_group_message():
                return
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return
            text = text.replace("\n", " / ")[:160]
            with io.open(_QUEUE, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("toast enqueue failed", exc_info=True)
