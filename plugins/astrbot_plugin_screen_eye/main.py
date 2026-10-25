"""屏幕眼睛：截取当前屏幕，交给视觉模型（智谱 glm-4v-flash）看懂。

- 聊天里说「你看我屏幕在干嘛」，模型会调用 look_at_screen 工具
- 也可以直接发指令「/看屏幕」，会把截图和描述一起发回来

注意：截图会上传到智谱的接口做识别。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import tempfile
import time
import urllib.request

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

_DEFAULT_PROMPT = (
    "这是用户当前电脑屏幕的截图。请用中文简要说明：这是什么界面/软件、"
    "用户在做什么、屏幕上的关键信息（标题、主要文字、报错等）。"
    "只抓重点，不要逐字转录，150 字以内。"
)


def _capture_jpeg(max_width: int = 1600) -> bytes:
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    if max_width and img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _describe(jpeg: bytes, api_key: str, model: str, prompt: str) -> str:
    b64 = base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0.4,
        "max_tokens": 512,
    }
    req = urllib.request.Request(
        _API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


@register(
    "screen_eye",
    "dafeiyu",
    "截取屏幕并用视觉模型看懂，让 AI 知道你在干什么",
    "1.0.0",
)
class ScreenEye(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return value if value not in (None, "") else default

    async def _look(self) -> tuple[bytes, str]:
        api_key = self._cfg("api_key", "")
        if not api_key:
            return b"", "还没配置智谱 API Key，没法看屏幕。"
        model = self._cfg("model", "glm-4v-flash")
        prompt = self._cfg("prompt", _DEFAULT_PROMPT)
        max_width = int(self._cfg("max_width", 1600))

        jpeg = await asyncio.to_thread(_capture_jpeg, max_width)
        text = await asyncio.to_thread(_describe, jpeg, api_key, model, prompt)
        return jpeg, text

    @llm_tool(name="look_at_screen")
    async def look_at_screen(self, event: AstrMessageEvent) -> str:
        """看一眼用户当前的电脑屏幕，返回屏幕上是什么内容。

        当用户说「你看我屏幕」「我现在在干嘛」「看看这个界面」「我卡在哪了」
        这类需要知道屏幕内容的话时调用。

        Returns:
            对当前屏幕的中文描述；如果失败会返回错误原因。
        """
        try:
            _, text = await self._look()
            return text
        except Exception as exc:  # noqa: BLE001
            logger.exception("看屏幕失败")
            return f"看屏幕失败：{exc}"

    @filter.command("看屏幕")
    async def look_at_screen_command(self, event: AstrMessageEvent):
        """发 /看屏幕，把当前截图和描述一起发回来。"""
        try:
            jpeg, text = await self._look()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"看屏幕失败：{exc}")
            return

        if not jpeg:
            yield event.plain_result(text)
            return

        path = os.path.join(tempfile.gettempdir(), f"screen_eye_{int(time.time())}.jpg")
        with open(path, "wb") as fh:
            fh.write(jpeg)
        yield event.chain_result([Plain(text), Image.fromFileSystem(path)])
