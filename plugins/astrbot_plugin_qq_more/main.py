"""给大肥鱼更多「自己的账号」权限：发空间、在线状态、戳一戳、群头衔。

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


@register("qq_more", "dafeiyu", "空间 / 状态 / 戳一戳 / 群头衔", "1.0.0")
class QQMore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    async def _remember_post(self, event: AstrMessageEvent, text: str) -> None:
        """说说发出去之后记一笔，不然过一会儿她就不认账了。"""
        note = f"我在 QQ 空间发了条动态（说说）：{text[:60]}"
        try:
            with io.open(
                r"D:\AstrBot\bot\data\plugin_data\dafeiyu_soul\notes.jsonl",
                "a",
                encoding="utf-8",
            ) as fh:
                fh.write(
                    json.dumps(
                        {"ts": time.time(), "kind": "note", "text": note, "level": "普通"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:  # noqa: BLE001
            logger.debug("qq_more: 写说说笔记失败", exc_info=True)
        try:
            cm = self.context.conversation_manager
            umo = event.unified_msg_origin
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                return
            history = json.loads(conv.history or "[]")
            if not isinstance(history, list):
                return
            history.append(
                {"role": "assistant", "content": f"（我刚在空间发了条说说：{text[:60]}）"}
            )
            await cm.update_conversation(umo, cid, history=history)
        except Exception:  # noqa: BLE001
            logger.debug("qq_more: 记录说说历史失败", exc_info=True)

    @llm_tool(name="post_qzone")
    async def post_qzone(self, event: AstrMessageEvent, content: str) -> str:
        """发一条 QQ 空间说说。

        Args:
            content(string): 说说正文，60 字以内
        """
        if str(event.get_sender_id()) != _owner():
            return "只有主人能让我发说说。"
        text = (content or "").strip()[:120]
        if not text:
            return "内容是空的。"
        # 冷却：10 分钟内不重复发
        qz = r"D:\AstrBot\qzone-last.json"
        try:
            with io.open(qz, encoding="utf-8") as fh:
                d = json.load(fh) or {}
            if time.time() - float(d.get("ts") or 0) < 600:
                return "刚发过了，等会儿再发吧。"
        except Exception:  # noqa: BLE001
            pass
        if any(b in text for b in ("戳我", "干嘛", "在吗")):
            return "这条不太像说说，换一句吧。"
        try:
            r = await event.bot.call_action(
                "send_qzone_msg", content=text, images=[], ugc_right=1, target_uins=[]
            )
            with io.open(qz, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "line": text, "tid": str(r)}, fh, ensure_ascii=False)
            try:
                with io.open(r"D:\AstrBot\heart-delta.json", "w", encoding="utf-8") as fh:
                    json.dump({"mood_score": 0.04, "energy": -0.01, "ts": time.time()}, fh, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"qq_more: 发说说 {text[:30]} tid={r}")
            await self._remember_post(event, text)
            return "发了，你去空间看看。"
        except Exception as exc:  # noqa: BLE001
            return f"说说没发出去：{exc}"

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

    @filter.regex(r"发(一)?(个|条)?(说说|空间|动态)")
    async def auto_qzone(self, event: AstrMessageEvent):
        """他说"发个说说"时，现场生成一条再发（带冷却，防连发）。"""
        if not _keyword_fallback_on():
            return
        if str(event.get_sender_id()) != _owner():
            return
        text = (event.message_str or "").strip()
        if len(text) > 40:
            return

        # ---- 防重复：10 分钟内不重复发 ----
        qz = r"D:\AstrBot\qzone-last.json"
        last = 0.0
        last_line = ""
        try:
            with io.open(qz, encoding="utf-8") as fh:
                d = json.load(fh) or {}
            last = float(d.get("ts") or 0)
            last_line = str(d.get("line") or "")
        except Exception:  # noqa: BLE001
            pass
        if time.time() - last < 600:
            yield event.plain_result(f"刚发过了诶：「{last_line[:30]}」，等会儿再发吧")
            return

        # ---- 收集上下文：刚看的视频 + 心情 + 兴趣（不再读对话原文，避免串词）----
        watch_title, watch_fact = "", ""
        try:
            with io.open(r"D:\AstrBot\watching-recent.jsonl", encoding="utf-8") as fh:
                rows = [json.loads(x) for x in fh if x.strip()][-1:]
            if rows:
                watch_title = str(rows[-1].get("title") or "")
                watch_fact = str(rows[-1].get("detail") or rows[-1].get("scene") or "")[:60]
        except Exception:  # noqa: BLE001
            pass

        mood, energy = "平静", 0.7
        try:
            with io.open(r"D:\AstrBot\bot\data\plugin_data\heart\state.json", encoding="utf-8-sig") as fh:
                hs = json.load(fh)
            mood = str(hs.get("mood") or "平静")
            energy = float(hs.get("energy") or 0.7)
        except Exception:  # noqa: BLE001
            pass

        liked = ""
        try:
            with io.open(r"D:\AstrBot\bili-interests.json", encoding="utf-8") as fh:
                data = json.load(fh) or {}
            top = sorted(((k, float(v)) for k, v in data.items() if float(v) > 0),
                         key=lambda x: x[1], reverse=True)[:3]
            liked = "、".join(k for k, _ in top)
        except Exception:  # noqa: BLE001
            pass

        # 最近真发生过的事——比抽象的兴趣词靠谱，说说要写具体的小事
        events: list[str] = []
        try:
            with io.open(
                r"D:\AstrBot\bot\data\plugin_data\heart\diary.jsonl", encoding="utf-8"
            ) as fh:
                rows = [json.loads(x) for x in fh if x.strip()][-6:]
            for r in rows:
                t = str(r.get("text") or "").strip()
                if t and t not in events:
                    events.append(t[:60])
        except Exception:  # noqa: BLE001
            pass

        line = ""
        try:
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            if provider:
                scene = (
                    "\n".join(f"- {e}" for e in events[-3:])
                    if events
                    else "- 今天没什么特别的，写点日常感受"
                )
                prompt = (
                    "你要发一条 QQ 空间说说，像普通女生随手发的那种，不是文案。\n"
                    f"今天真发生过的事（只能挑一件写，不许拼接两件）：\n{scene}\n"
                    f"你现在的心情：{mood}；精力：{energy:.2f}\n"
                    "要求：\n"
                    "1) 只写一件具体的小事，要有细节（什么东西、什么时候、什么动作）\n"
                    "2) 第一人称口语，20~45 字，可以带一个语气词\n"
                    "3) 不要总结、不要升华，别用「想起…」「有点…了」这种收尾\n"
                    "4) 不写视频/直播标题，不写游戏术语，不写「戳我」「在吗」「干嘛」\n"
                    "5) 不要像广告，不要 emoji 连发\n"
                    "只输出说说本身。"
                )
                resp = await provider.text_chat(prompt=prompt)
                line = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')
        except Exception:  # noqa: BLE001
            logger.debug("qzone compose failed", exc_info=True)

        # ---- 质量闸：拼凑/太短/对话题材的，退回重写一次 ----
        bad = ("戳我", "干嘛", "在吗", "你说", "他说", "想起", "「", "强化")
        if not line or len(line) < 10 or any(b in line for b in bad):
            try:
                provider = await self.context.get_using_provider_async(event.unified_msg_origin)
                if provider:
                    retry = (
                        f"重写一条 QQ 空间说说：20~45 字，只写一件具体的小事，第一人称口语，"
                        f"心情是「{mood}」。不要总结腔，不要视频标题，不要游戏术语，不要对话用语。"
                        "只输出说说本身。"
                    )
                    resp = await provider.text_chat(prompt=retry)
                    cand = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')
                    if cand and len(cand) >= 10 and not any(b in cand for b in bad):
                        line = cand
            except Exception:  # noqa: BLE001
                pass

        if not line or any(b in line for b in bad):
            yield event.plain_result("这次没想出好句子，等会儿再发吧")
            return

        try:
            plat = self.context.get_platform_inst("qq_dafeiyu")
            bot = getattr(plat, "bot", None)
            if not bot:
                yield event.plain_result("现在连不上 QQ，发不了诶")
                return
            r = await bot.call_action("send_qzone_msg", content=line[:60], images=[], ugc_right=1, target_uins=[])
            with io.open(qz, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "line": line, "tid": str(r)}, fh, ensure_ascii=False)
            # 发完自己心情好一点（写 heart 增量）
            try:
                with io.open(r"D:\AstrBot\heart-delta.json", "w", encoding="utf-8") as fh:
                    json.dump({"mood_score": 0.04, "energy": -0.01, "ts": time.time()}, fh, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"qq_more: 发说说 -> {line[:40]} tid={r}")
            await self._remember_post(event, line)
            yield event.plain_result(f"发好了：「{line}」\n去空间看看")
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto qzone failed")
            yield event.plain_result(f"没发出去：{type(exc).__name__}: {exc}")

    @filter.command("空间删除")
    async def cmd_qzone_delete(self, event: AstrMessageEvent, tid: str = ""):
        """删一条空间说说：/空间删除 <tid>"""
        tid = (tid or "").strip()
        if not tid:
            yield event.plain_result("要删哪条？给我 tid")
            return
        try:
            plat = self.context.get_platform_inst("qq_dafeiyu")
            bot = getattr(plat, "bot", None)
            if not bot:
                yield event.plain_result("拿不到 bot 实例")
                return
            r = await bot.call_action("delete_qzone_msg", tid=tid)
            logger.info(f"qzone delete result: {r}")
            yield event.plain_result(f"删了：{str(r)[:120]}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"失败：{type(exc).__name__}: {exc}")

    @filter.command("空间测试")
    async def cmd_qzone_test(self, event: AstrMessageEvent):
        """调试：直接用平台实例调 send_qzone_msg，看看接口通不通。"""
        try:
            plat = self.context.get_platform_inst("qq_dafeiyu")
            bot = getattr(plat, "bot", None)
            if not bot:
                yield event.plain_result("拿不到 bot 实例")
                return
            r = await bot.call_action(
                "send_qzone_msg",
                content="测试一下 这条来自大肥鱼",
                images=[], ugc_right=1, target_uins=[],
            )
            logger.info(f"qzone test result: {r}")
            yield event.plain_result(f"调用了，返回：{str(r)[:200]}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("qzone test failed")
            yield event.plain_result(f"失败：{type(exc).__name__}: {exc}")

    @filter.command("发说说")
    async def cmd_qzone(self, event: AstrMessageEvent, content: str = ""):
        """发「/发说说 内容」到 QQ 空间。"""
        if str(event.get_sender_id()) != _owner():
            yield event.plain_result("只有主人能让我发说说。")
            return
        text = content.strip()
        if not text:
            yield event.plain_result("要发什么？")
            return
        try:
            await event.bot.call_action("send_qzone_msg", content=text[:120], images=[], ugc_right=1, target_uins=[])
            yield event.plain_result("发了，去空间看看。")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"没发出去：{exc}")
