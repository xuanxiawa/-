"""发出去之前，把模型漏出来的「工具调用」标记清掉。

有些模型（或某些调用路径）会把工具调用写成正文里的 XML：
    <tool_call>{"name":"save_sticker","arguments":"..."}</tool_call>
AstrBot 会当普通文字发出去，用户就看到一堆 JSON。

这个插件在 result 装饰阶段（发送前）清掉这些标记。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_PATTERNS = (
    re.compile(r"<tool_call>.*?</tool_call>", re.S | re.I),
    re.compile(r"<tool_call\b[^>]*>.*?</tool_call>", re.S | re.I),
    re.compile(r"<function_call>.*?</function_call>", re.S | re.I),
    re.compile(r"<\|tool_call\|>.*?<\|/tool_call\|>", re.S),
    re.compile(r"<\|function_call\|>.*?<\|/function_call\|>", re.S),
)

# 只漏了前半截的情况：单独一行 {"name": "...", "arguments":
_LOOSE_JSON = re.compile(r'\n?\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:.*$', re.S)


@register(
    "output_guard",
    "dafeiyu",
    "发送前清掉模型漏出来的工具调用标记",
    "1.0.0",
)
class OutputGuard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    # 优先级放最后，保证在别的装饰器改完之后再清
    @filter.on_decorating_result(priority=-1000)
    async def clean(self, event: AstrMessageEvent):
        try:
            res = event.get_result()
            if res is None or not res.chain:
                return
            changed = False
            kept = []
            for comp in res.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and text:
                    new = text
                    for pat in _PATTERNS:
                        new = pat.sub("", new)
                    new = _LOOSE_JSON.sub("", new).strip()
                    if new != text:
                        changed = True
                        try:
                            comp.text = new
                        except Exception:  # noqa: BLE001
                            pass
                    if not new:
                        continue
                kept.append(comp)
            if changed:
                try:
                    res.chain = kept
                except Exception:  # noqa: BLE001
                    pass
                logger.info("output_guard: 清掉了漏出来的工具调用标记")
        except Exception:  # noqa: BLE001
            logger.debug("output_guard: 清理失败", exc_info=True)
