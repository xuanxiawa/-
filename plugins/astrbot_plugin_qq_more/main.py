"""给大肥鱼更多「自己的账号」权限：在线状态、戳一戳、群头衔、点赞、改名。

QQ空间的活已经交给 astrbot_plugin_diary_qzone（走空间网页协议），这里不再重复实现。

NapCat 扩展接口：
- send_qzone_msg        {content, images[], ugc_right, target_uins[]}
- set_online_status     {status}
- send_poke             {user_id, group_id?}
- set_group_special_title {group_id, user_id, special_title}
"""

from __future__ import annotations

import io
import time
import json
import os
import random

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_qq_avatar_config.json"
_AVATARS = r"D:\AstrBot\avatars"


def _owner() -> str:
    try:
        with io.open(_CFG, encoding="utf-8-sig") as fh:
            return str(json.load(fh).get("owner") or "3448098168")
    except Exception:  # noqa: BLE001
        return "3448098168"



_LIKE_FILE = r"D:\AstrBot\like-count.json"


_LIKE_DAILY = 10  # 每人每天最多点几个（QQ 名片赞的上限就是 10）


def _like_allow(uid: str, want: int) -> int:
    """每人每天最多点 _LIKE_DAILY 个；返回今天还能点的次数。"""
    import time as _t
    today = _t.strftime("%Y-%m-%d")
    data = {}
    try:
        with io.open(_LIKE_FILE, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        data = {}
    if data.get("date") != today:
        data = {"date": today, "counts": {}}
    counts = data.setdefault("counts", {})
    used = int(counts.get(uid, 0))
    left = max(0, _LIKE_DAILY - used)
    take = min(left, max(0, want))
    if take > 0:
        counts[uid] = used + take
        try:
            with io.open(_LIKE_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return take


def _like_undo(uid: str, n: int) -> None:
    """点赞失败了，把刚才扣掉的额度还回去，别白白浪费一天的名额。"""
    if n <= 0:
        return
    try:
        import time as _t
        today = _t.strftime("%Y-%m-%d")
        with io.open(_LIKE_FILE, encoding="utf-8") as fh:
            data = json.load(fh) or {}
        if data.get("date") != today:
            return
        counts = data.get("counts") or {}
        counts[uid] = max(0, int(counts.get(uid, 0)) - int(n))
        data["counts"] = counts
        with io.open(_LIKE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


async def _do_rename(bot, name: str) -> str:
    """改机器人自己的 QQ 昵称。不同 OneBot 实现叫法不一样，挨个试。"""
    name = (name or "").strip()
    if not 1 <= len(name) <= 12:
        return "名字要 1~12 个字。"
    errors = []
    for action, kw in (
        ("set_qq_profile", {"nickname": name}),
        ("set_qq_nickname", {"nickname": name}),
        (
            "set_qq_profile",
            {"nickname": name, "company": "", "email": "", "college": "", "personal_note": ""},
        ),
    ):
        try:
            await bot.call_action(action, **kw)
            logger.info(f"qq_more: 改名 -> {name}（{action}）")
            return f"改好了，我现在叫「{name}」。"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{action}: {exc}")
    return "改不了：" + "；".join(errors)[:140]



# 关键词兜底默认关闭：让她自己判断要不要用工具。
# 需要临时恢复兜底：建一个空文件 D:\AstrBot\bot\data\plugin_data\keyword_fallback.on
_FALLBACK_FLAG = r"D:\AstrBot\bot\data\plugin_data\keyword_fallback.on"


def _keyword_fallback_on() -> bool:
    try:
        return os.path.exists(_FALLBACK_FLAG)
    except Exception:  # noqa: BLE001
        return False


@register("qq_more", "dafeiyu", "在线状态 / 戳一戳 / 群头衔 / 点赞 / 改名", "1.1.0")
class QQMore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @llm_tool(name="set_my_status")
    async def set_my_status(self, event: AstrMessageEvent, status: str) -> str:
        """设置自己的在线状态。

        Args:
            status(string): 在线 / 离开 / 忙碌 / 隐身（也接受 1-4 的数字）
        """
        if str(event.get_sender_id()) != _owner():
            return "只有主人能改我的状态。"
        mapping = {"在线": 1, "离开": 2, "忙碌": 3, "隐身": 4,
                   "1": 1, "2": 2, "3": 3, "4": 4}
        code = mapping.get(str(status).strip())
        if code is None:
            return "状态只支持：在线 / 离开 / 忙碌 / 隐身。"
        try:
            await event.bot.call_action("set_online_status", status=code)
            return f"切换到「{status}」了。"
        except Exception as exc:  # noqa: BLE001
            return f"改状态失败：{exc}"

    @llm_tool(name="poke_user")
    async def poke_user(self, event: AstrMessageEvent, user_id: str = "") -> str:
        """戳一戳对方（不填 user_id 就戳当前说话的人）。

        Args:
            user_id(string): QQ 号，留空戳当前聊天对象
        """
        uid = (user_id or "").strip() or str(event.get_sender_id())
        try:
            kwargs = {"user_id": int(uid)}
            gid = event.get_group_id()
            if gid:
                kwargs["group_id"] = int(gid)
            await event.bot.call_action("send_poke", **kwargs)
            return "戳了一下。"
        except Exception as exc:  # noqa: BLE001
            return f"戳不动：{exc}"

    @llm_tool(name="set_group_title")
    async def set_group_title(self, event: AstrMessageEvent, group_id: str, user_id: str, title: str) -> str:
        """给群里某人设专属头衔（需要机器人是管理员）。

        Args:
            group_id(string): 群号
            user_id(string): 目标 QQ
            title(string): 头衔，6 字以内
        """
        if str(event.get_sender_id()) != _owner():
            return "只有主人能改头衔。"
        try:
            await event.bot.call_action(
                "set_group_special_title",
                group_id=int(group_id),
                user_id=int(user_id),
                special_title=(title or "").strip()[:12],
            )
            return "头衔设好了。"
        except Exception as exc:  # noqa: BLE001
            return f"设头衔失败：{exc}"

    @llm_tool(name="like_user")
    async def like_user(self, event: AstrMessageEvent, user_id: str = "", times: str = "10") -> str:
        """给好友点赞（一次最多 10 个）。

        Args:
            user_id(string): 对方 QQ 号；留空就赞当前说话的人
            times(string): 次数 1~10，默认 10
        """
        uid = (user_id or "").strip() or str(event.get_sender_id())
        try:
            n = max(1, min(10, int(float(times or 10))))
        except Exception:  # noqa: BLE001
            n = 10
        # 心情影响点数：开心点满，闷的时候少点
        try:
            with io.open(r"D:\AstrBot\bot\data\plugin_data\heart\state.json", encoding="utf-8-sig") as fh:
                mood = str((json.load(fh) or {}).get("mood") or "平静")
            if mood == "开心":
                n = 10
            elif mood in ("闷闷的", "有点难过", "生气"):
                n = min(n, 3)
        except Exception:  # noqa: BLE001
            pass
        n = _like_allow(uid, n)
        if n == 0:
            return f"今天已经给 {uid} 点满 {_LIKE_DAILY} 个了，明天再说。"
        try:
            await event.bot.call_action("send_like", user_id=int(uid), times=n)
            logger.info(f"qq_more: 给 {uid} 点赞 x{n}")
            try:
                with io.open(r"D:\AstrBot\heart-delta.json", "w", encoding="utf-8") as fh:
                    json.dump({"mood_score": 0.02, "energy": 0.0, "ts": time.time()}, fh, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
            return f"给 {uid} 点了 {n} 个赞。"
        except Exception as exc:  # noqa: BLE001
            _like_undo(uid, n)
            return f"点赞失败：{exc}"

    @llm_tool(name="change_my_name")
    async def change_my_name(self, event: AstrMessageEvent, name: str) -> str:
        """改你自己的 QQ 昵称（主人给你起名、让你改名时用）。

        Args:
            name(string): 新昵称，1~12 个字
        """
        if str(event.get_sender_id()) != _owner():
            return "只有主人能让我改名。"
        return await _do_rename(event.bot, name)

    @filter.command("改名")
    async def cmd_rename(self, event: AstrMessageEvent, name: str = ""):
        """改机器人自己的 QQ 昵称：/改名 风铃"""
        if str(event.get_sender_id()) != _owner():
            yield event.plain_result("只有主人能让我改名。")
            return
        if not name:
            yield event.plain_result("要改成什么？发「/改名 风铃」")
            return
        yield event.plain_result(await _do_rename(event.bot, name))

    @filter.command("点赞")
    async def cmd_like(self, event: AstrMessageEvent, times: str = "10"):
        """发「/点赞」给自己点满，或「/点赞 7」指定次数。"""
        try:
            n = max(1, min(10, int(float(times or 10))))
        except Exception:  # noqa: BLE001
            n = 10
        n2 = _like_allow(str(event.get_sender_id()), n)
        if n2 == 0:
            yield event.plain_result(f"今天已经给你点满 {_LIKE_DAILY} 个了，明天再点。")
            return
        try:
            await event.bot.call_action("send_like", user_id=int(event.get_sender_id()), times=n2)
            yield event.plain_result(f"给你点了 {n2} 个赞。")
        except Exception as exc:  # noqa: BLE001
            _like_undo(str(event.get_sender_id()), n2)
            yield event.plain_result(f"点赞失败：{exc}")

    @filter.regex(r"点(个)?赞|赞我|赞一下|给我赞")
    async def auto_like(self, event: AstrMessageEvent):
        """说到点赞就直接点，不依赖模型记得调工具。"""
        if not _keyword_fallback_on():
            return
        text = (event.message_str or "").strip()
        if len(text) > 30:
            return
        left = _like_allow(str(event.get_sender_id()), _LIKE_DAILY)
        if left == 0:
            yield event.plain_result("今天给你点满了，明天再点哈")
            return
        try:
            resp = await event.bot.call_action("send_like", user_id=int(event.get_sender_id()), times=left)
            logger.info(f"qq_more: 关键词触发点赞 x{left} -> {event.get_sender_id()}，返回 {resp}")
            yield event.plain_result(f"给你点了 {left} 个赞，够不够")
        except Exception as exc:  # noqa: BLE001
            _like_undo(str(event.get_sender_id()), left)
            logger.exception("auto like failed")
            yield event.plain_result(f"点不了诶：{exc}")
