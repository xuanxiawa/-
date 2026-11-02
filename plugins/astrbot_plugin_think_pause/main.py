"""回话前先愣一下：按主人这句话的长短，算一个「读 + 想」的停顿。

挂在 on_llm_request（每次真正要调模型前，priority=200 最早），
按**他发来的话**停 0.7~2.5 秒，再让模型开始生成。
每条消息只停一次（用 event extra 防重入）。

- 短句（在吗 / 哈哈）：~0.8s
- 普通一句（20 字）：~1.5s
- 长句 / 带问号：2~2.5s（封顶）
- 命令（/reset 这类）不拦
"""

from __future__ import annotations

import asyncio
import random
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

PLATFORM_ID = "qq_dafeiyu"

BASE = 0.7          # 基础停顿
PER_CHAR = 0.04     # 每多一个字加多少秒
MIN_DELAY = 0.7
MAX_DELAY = 2.5
QUESTION_BONUS = 0.35
JITTER = 0.15       # ±15% 随机

# 去掉 [At:xxx] / [图片] / [表情:1] 这类占位，按真正的字数算
_PLACEHOLDER = re.compile(r"\[[^\]]{0,60}\]")


@register(
    "think_pause",
    "dafeiyu",
    "回话前先想一下：按主人这句话的长短，停 0.7~2.5 秒再让模型开口",
    "1.0.1",
)
class ThinkPause(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.on_llm_request(priority=200)
    async def pause(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            if event.get_extra("_think_pause_done"):
                return
            event.set_extra("_think_pause_done", True)

            raw = (event.message_str or "").strip()
            if raw.startswith("/"):
                return
            user = _PLACEHOLDER.sub("", raw).strip()
            n = len(user)
            if n == 0:
                return

            delay = BASE + PER_CHAR * n
            if "？" in user or "?" in user:
                delay += QUESTION_BONUS
            delay = max(MIN_DELAY, min(MAX_DELAY, delay))
            delay *= random.uniform(1 - JITTER, 1 + JITTER)

            logger.info(f"think_pause: 先想 {delay:.1f}s（他的话 {n} 字）")
            await asyncio.sleep(delay)
        except Exception:  # noqa: BLE001
            logger.debug("think_pause: 停顿失败", exc_info=True)
