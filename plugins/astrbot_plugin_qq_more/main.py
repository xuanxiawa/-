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


@register("qq_more", "dafeiyu", "空间 / 状态 / 戳一戳 / 群头衔", "1.0.0")
class QQMore(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

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
            return f"点赞失败：{exc}"

    @filter.command("点赞")
    async def cmd_like(self, event: AstrMessageEvent, times: str = "10"):
        """发「/点赞」给自己点满，或「/点赞 7」指定次数。"""
        try:
            n = max(1, min(10, int(float(times or 10))))
        except Exception:  # noqa: BLE001
            n = 10
        try:
            await event.bot.call_action("send_like", user_id=int(event.get_sender_id()), times=n)
            yield event.plain_result(f"点了 {n} 个赞。")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"点赞失败：{exc}")

    @filter.regex(r"点(个)?赞|赞我|赞一下|给我赞")
    async def auto_like(self, event: AstrMessageEvent):
        """说到点赞就直接点，不依赖模型记得调工具。"""
        text = (event.message_str or "").strip()
        if len(text) > 30:
            return
        try:
            await event.bot.call_action("send_like", user_id=int(event.get_sender_id()), times=10)
            logger.info(f"qq_more: 关键词触发点赞 -> {event.get_sender_id()}")
            yield event.plain_result("点啦，十个，够不够")
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto like failed")
            yield event.plain_result(f"点不了诶：{exc}")

    @filter.regex(r"发(一)?(个|条)?(说说|空间|动态)")
    async def auto_qzone(self, event: AstrMessageEvent):
        """他说"发个说说"时，现场生成一条再发（带冷却，防连发）。"""
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

        line = ""
        try:
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            if provider:
                prompt = (
                    "写一条要发到 QQ 空间的说说。你是住在 QQ 里的小鲸鱼娘。\n"
                    f"你现在的心情：{mood}；精力：{energy:.2f}\n"
                    f"你最近喜欢的主题：{liked or '还比较杂'}\n"
                    f"你刚看的视频：《{watch_title or '没看什么'}》{('，画面里：' + watch_fact) if watch_fact else ''}\n"
                    "要求：\n"
                    "1) 只讲**一件事**，不要拼接两个话题\n"
                    "2) 20~40 字，是一个完整的意思\n"
                    "3) 心情好就活泼一点，累了就安静一点\n"
                    "4) 不要出现「戳我」「干嘛」「在吗」这类对话用语\n"
                    "5) 不提你俩的关系，不带话题标签\n"
                    "只输出说说本身。"
                )
                resp = await provider.text_chat(prompt=prompt)
                line = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')
        except Exception:  # noqa: BLE001
            logger.debug("qzone compose failed", exc_info=True)

        # ---- 质量闸：拼凑/太短/对话题材的，退回重写一次 ----
        bad = ("戳我", "干嘛", "在吗", "你说", "他说")
        if not line or len(line) < 10 or any(b in line for b in bad):
            try:
                provider = await self.context.get_using_provider_async(event.unified_msg_origin)
                if provider:
                    retry = (
                        f"重写一条 QQ 空间说说：20~40 字，只讲一件事，心情是「{mood}」。"
                        "不要出现对话用语，不要拼接话题。只输出说说本身。"
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
