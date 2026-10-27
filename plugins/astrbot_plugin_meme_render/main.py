"""把 DSH 的 nekowhale-stickers 模板制图搬到 QQ。

底层调用插件自带的 CLI（作者说明就是给别的语言的 bot shell out 用的）：
  node lib/cli.js render <模板> <文字> -o out.gif
"""

from __future__ import annotations

import os
import random
import subprocess
import time

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

_DSH = r"D:\大肥鱼\DeepSeek Harness.exe"
_CLI = os.path.expandvars(
    r"%USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-nekowhale-stickers\lib\cli.js"
)
_OUT = r"D:\AstrBot\stickers\renders"

_TEMPLATES = {
    "举牌": "大肥鱼举牌",
    "牌": "大肥鱼举牌",
    "想": "大肥鱼想",
    "上联": "大肥鱼上联",
    "下联": "大肥鱼下联",
    "对联": "大肥鱼上联",
    "葱卷": "大肥鱼葱卷",
    "抱": "大肥鱼抱",
}


def _normalize(tpl: str) -> str:
    t = (tpl or "").strip()
    if t in _TEMPLATES:
        return _TEMPLATES[t]
    for k, v in _TEMPLATES.items():
        if k in t:
            return v
    return t or "大肥鱼举牌"


_LAST_IMG = r"D:\AstrBot\stickers\last.png"


def _render(template: str, text: str, image: str = "") -> str | None:
    os.makedirs(_OUT, exist_ok=True)
    out = os.path.join(_OUT, f"{int(time.time())}_{random.randint(100, 999)}.gif")
    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    if template == "大肥鱼抱":
        img = image or (_LAST_IMG if os.path.exists(_LAST_IMG) else "")
        if not img:
            return None
        cmd = [_DSH, _CLI, "render", template, "", "--image", img, "-o", out]
    else:
        cmd = [_DSH, _CLI, "render", template, text or "……", "-o", out]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90, env=env)
        if r.returncode != 0:
            logger.warning(f"meme_render cli failed: {r.stderr.decode('utf-8', 'ignore')[:200]}")
            return None
        return out if os.path.exists(out) else None
    except Exception:  # noqa: BLE001
        logger.exception("meme_render cli error")
        return None


@register("meme_render_qq", "dafeiyu", "QQ 里的模板表情：举牌 / 想 / 上联 / 葱卷", "1.0.0")
class MemeRenderQQ(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        os.makedirs(_OUT, exist_ok=True)

    @llm_tool(name="render_meme")
    async def render_meme(self, event: AstrMessageEvent, template: str, text: str) -> str:
        """做一张大肥鱼模板表情发给他：举牌 / 想 / 上联 / 下联 / 葱卷。

        Args:
            template(string): 模板名，比如 举牌、想、上联、下联、葱卷
            text(string): 写在表情上的字，12 个字以内
        """
        tpl = _normalize(template)
        t = (text or "").strip()[:14]
        path = await __import__("asyncio").to_thread(_render, tpl, t)
        if tpl == "大肥鱼抱" and not path:
            return "要抱的话得先给我一张图（他头像那种）。"
        if not path:
            return "图没画出来，模板服务可能没起来。"
        try:
            await self.context.send_message(
                session=event.unified_msg_origin,
                message_chain=MessageChain([Image.fromFileSystem(path)]),
            )
            return f"画好了（{tpl}）。"
        except Exception as exc:  # noqa: BLE001
            return f"画出来了但发不出去：{exc}"

    @filter.regex(r"做\s*(?:个|一个|一|一张|张)?\s*表情|来\s*(?:个|一个|一|一张|张)?\s*表情|画\s*(?:个|一个|一|一张|张)?|举牌|上联|下联|对联|葱卷|抱(一下|你一下|这个|我)|来(个)?抱|要抱")
    async def auto_render(self, event: AstrMessageEvent):
        """自然语言要表情时直接做，不依赖模型记得调工具。"""
        text = (event.message_str or "").strip()
        if len(text) > 50:
            return
        # 只处理"要表情/指定模板"的，不拦普通聊天
        want = any(k in text for k in ("表情", "举牌", "上联", "下联", "对联", "葱卷", "抱"))
        if not want:
            return
        # 他是在要"你存的那张"，不是要做图
        if __import__("re").search(r"用你(存|收)|发你(存|收)|把你(存|收)|你(现在)?有(的|哪些)|发你(现在)?有|发你自己的|你的表情包|发那个(表情|图)", text):
            return
        # 解析模板
        tpl = ""
        for k in ("举牌", "上联", "下联", "对联", "葱卷", "想"):
            if k in text:
                tpl = k
                break
        if not tpl:
            tpl = "举牌"
        # 解析文字：引号里 / 「写XXX」/ 逗号后面
        content = ""
        m = __import__("re").search(r"[「『\"\'](.+?)[」』\"\']", text)
        if m:
            content = m.group(1)
        else:
            m = __import__("re").search(r"写\s*[:：]?\s*(.+)$", text)
            if m:
                content = m.group(1)
            else:
                m = __import__("re").search(r"[，,、]\s*(.+)$", text)
                if m:
                    content = m.group(1)
        content = (content or "").strip()[:14]
        if not content:
            content = "摸鱼中"
        full = _normalize(tpl)
        path = await __import__("asyncio").to_thread(_render, full, content)
        if full == "大肥鱼抱" and not path:
            yield event.plain_result("先发我一张图，我才好抱")
            return
        if not path:
            yield event.plain_result("图没画出来…")
            return
        yield event.chain_result([Image.fromFileSystem(path)])

    @filter.command("做表情")
    async def cmd_render(self, event: AstrMessageEvent, template: str = "举牌", text: str = ""):
        """发「/做表情 举牌 摸鱼中」做一张模板表情。"""
        tpl = _normalize(template)
        path = await __import__("asyncio").to_thread(_render, tpl, (text or "").strip()[:14])
        if tpl == "大肥鱼抱" and not path:
            yield event.plain_result("要抱的话先发我一张图。")
            return
        if not path:
            yield event.plain_result("图没画出来。")
            return
        yield event.chain_result([Image.fromFileSystem(path)])
