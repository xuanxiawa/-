"""让私聊有"缓冲时间"：每条消息先等一下再交给模型。

原理：AstrBot 在 agent 运行期间会把新到的消息作为 follow-up 合并进同一轮。
只要这一轮不立刻结束，连发的几条（包括图）就会被并成一轮，她就能"看完再说"。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_DELAY = 4.0  # 秒；连发窗口


@register("batch_delay", "dafeiyu", "私聊缓冲：连发的消息合并成一轮再回", "1.0.0")
class BatchDelay(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.on_llm_request(priority=200)
    async def hold(self, event: AstrMessageEvent, req):
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            if event.is_group_message():
                return
            # 只对真人消息生效，命令不延迟
            text = (event.message_str or "").strip()
            if text.startswith("/"):
                return
            logger.info(f"batch_delay: 缓冲 {_DELAY}s 等待可能连发的消息")
            await asyncio.sleep(_DELAY)
        except Exception:  # noqa: BLE001
            logger.debug("batch delay failed", exc_info=True)
