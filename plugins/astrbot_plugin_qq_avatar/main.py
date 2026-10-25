"""大肥鱼能自己换 QQ 头像 / 昵称 / 个性签名。

NapCat 扩展接口：
- set_qq_avatar      {file: base64:// | 路径 | URL}
- set_qq_profile     {nickname, personal_note?}
- set_self_longnick  {longNick}
"""

from __future__ import annotations

import base64
import io
import os
import random
import urllib.request

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain

_POOL = r"D:\AstrBot\avatars"
_INCOMING = r"D:\AstrBot\avatars\incoming"
_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_qq_avatar_config.json"


def _last_path() -> str:
    return os.path.join(_INCOMING, "last.png")


async def _remember_last_image(event) -> None:
    """把主人刚发来的图片存成 last.png，供「换这张」使用。"""
    try:
        from astrbot.api.message_components import Image

        for comp in event.get_messages():
            if isinstance(comp, Image):
                path = await comp.convert_to_file_path()
                if path and os.path.exists(path):
                    os.makedirs(_INCOMING, exist_ok=True)
                    with open(path, "rb") as src, open(_last_path(), "wb") as dst:
                        dst.write(src.read())
                    logger.info(f"qq_avatar: 收到图片 {os.path.getsize(_last_path())} bytes")
                return
    except Exception:  # noqa: BLE001
        logger.debug("remember image failed", exc_info=True)


def _load_cfg() -> dict:
    import json

    try:
        with io.open(_CFG, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {"owner": "3448098168", "pool": _POOL}


def _is_owner(event: AstrMessageEvent) -> bool:
    cfg = _load_cfg()
    uid = str(event.get_sender_id())
    return uid == str(cfg.get("owner") or "3448098168")


def _to_b64(file_arg: str) -> str | None:
    """把路径 / URL / base64 统一成 base64:// 形式。"""
    try:
        if file_arg.startswith("base64://"):
            return file_arg
        if file_arg.startswith("http://") or file_arg.startswith("https://"):
            req = urllib.request.Request(file_arg, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            return "base64://" + base64.b64encode(data).decode()
        if os.path.exists(file_arg):
            with open(file_arg, "rb") as fh:
                return "base64://" + base64.b64encode(fh.read()).decode()
    except Exception:  # noqa: BLE001
        logger.debug("avatar read failed", exc_info=True)
    return None


def _pick_from_pool() -> str | None:
    try:
        files = [
            os.path.join(_POOL, f)
            for f in os.listdir(_POOL)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
        ]
    except Exception:  # noqa: BLE001
        return None
    return random.choice(files) if files else None


@register("qq_avatar", "dafeiyu", "让她自己换头像 / 昵称 / 签名", "1.0.0")
class QQAvatar(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_image(self, event: AstrMessageEvent):
        """记住主人最后发来的图。"""
        if not _is_owner(event):
            return
        await _remember_last_image(event)

    @llm_tool(name="set_my_avatar")
    async def set_my_avatar(self, event: AstrMessageEvent, image: str = "") -> str:
        """换掉自己的 QQ 头像。主人让你换头像时调用。

        Args:
            image(string): 图片路径或 http 链接；留空则从头像池里随机挑一张
        """
        if not _is_owner(event):
            return "只有主人能让我换头像。"
        file_arg = (image or "").strip()
        # 「这张 / 那张 / last」→ 用他刚发来的图
        if file_arg in ("", "last", "这张", "那张", "这个", "发的那张") and os.path.exists(_last_path()):
            file_arg = _last_path()
        if not file_arg:
            file_arg = _pick_from_pool() or ""
        if not file_arg:
            return "没找到图片，池子空了。"
        b64 = _to_b64(file_arg)
        if not b64:
            return f"这张图读不出来：{file_arg[:60]}"
        try:
            await event.bot.call_action("set_qq_avatar", file=b64)
            logger.info(f"qq_avatar: 已换头像 from {file_arg[:80]}")
            return "换好了，你看看像不像我。"
        except Exception as exc:  # noqa: BLE001
            logger.exception("set_qq_avatar failed")
            return f"换头像失败了：{exc}"

    @llm_tool(name="set_my_nickname")
    async def set_my_nickname(self, event: AstrMessageEvent, nickname: str) -> str:
        """改自己的 QQ 昵称。

        Args:
            nickname(string): 新昵称，别太长
        """
        if not _is_owner(event):
            return "只有主人能改我的昵称。"
        name = (nickname or "").strip()[:24]
        if not name:
            return "昵称不能是空的。"
        try:
            await event.bot.call_action("set_qq_profile", nickname=name)
            return f"以后就叫「{name}」啦。"
        except Exception as exc:  # noqa: BLE001
            return f"改昵称失败：{exc}"

    @llm_tool(name="set_my_signature")
    async def set_my_signature(self, event: AstrMessageEvent, text: str) -> str:
        """改自己的 QQ 个性签名。

        Args:
            text(string): 签名内容，30 字以内
        """
        if not _is_owner(event):
            return "只有主人能改我的签名。"
        t = (text or "").strip()[:60]
        if not t:
            return "签名不能是空的。"
        try:
            await event.bot.call_action("set_self_longnick", longNick=t)
            return f"签名改成「{t}」了。"
        except Exception as exc:  # noqa: BLE001
            return f"改签名失败：{exc}"

    @filter.command("换头像")
    async def cmd_avatar(self, event: AstrMessageEvent, image: str = ""):
        """发「/换头像」随机换，或「/换头像 图片路径」指定。"""
        if not _is_owner(event):
            yield event.plain_result("只有主人能让我换头像。")
            return
        file_arg = image.strip() or (_pick_from_pool() or "")
        if not file_arg:
            yield event.plain_result("头像池空了，先往 D:\\AstrBot\\avatars 里放几张图。")
            return
        b64 = _to_b64(file_arg)
        if not b64:
            yield event.plain_result("这张图读不出来。")
            return
        try:
            await event.bot.call_action("set_qq_avatar", file=b64)
            yield event.plain_result("换好了，像不像我？")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"换头像失败：{exc}")

    @filter.regex(r"换(一下)?头像|把头像换|头像换(一)?(下|成)|换这张|换那张|就这张|用这张|换成这个")
    async def auto_switch(self, event: AstrMessageEvent):
        """自然语言里说到换头像就直接执行，不依赖模型记得调工具。"""
        if not _is_owner(event):
            return
        text = (event.message_str or "").strip()
        if len(text) > 40:
            return
        # 他刚发过图 → 优先用那张
        has_last = os.path.exists(_last_path())
        file_arg = _last_path() if has_last else (_pick_from_pool() or "")
        if not file_arg:
            yield event.plain_result("头像池空了，先往 D:\\AstrBot\\avatars 里放几张图。")
            return
        b64 = _to_b64(file_arg)
        if not b64:
            yield event.plain_result("这张图读不出来。")
            return
        try:
            await event.bot.call_action("set_qq_avatar", file=b64)
            logger.info(f"qq_avatar: 关键词触发换头像 from {file_arg}")
            yield event.plain_result("换好啦，你看像不像我嘛")
        except Exception as exc:  # noqa: BLE001
            logger.exception("avatar keyword failed")
            yield event.plain_result(f"换不了诶：{exc}")
