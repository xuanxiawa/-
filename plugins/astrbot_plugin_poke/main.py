"""被戳一戳时的反应：戳回去 + 说一句，顺手记进临时记忆。

AstrBot 会把 OneBot 的 notice/poke 转成带 Poke 分量的消息事件，
所以这里按分量判断，不依赖消息文本。
"""

from __future__ import annotations

import io
import json
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Poke
from astrbot.api.star import Context, Star, register

_SOUL = r"D:\AstrBot\bot\data\plugin_data\heart\state.json"
_RECENT = r"D:\AstrBot\bot\data\plugin_data\heart\recent.jsonl"
_UMO = r"D:\AstrBot\bot\data\plugin_data\heart\umo.txt"

# 按心情分组的话术
_REPLIES = {
    "开心": ["诶嘿 戳我干嘛", "被你戳到啦", "我在这儿呢 怎么啦", "嘿嘿 在呐在呐"],
    "平静": ["？", "怎么了嘛", "在呢 戳我干嘛", "嗯？找我呀"],
    "闷闷的": ["……嗯？", "怎么了", "我听着呢"],
    "有点难过": ["别戳了 我有点闷", "……干嘛"],
    "生气": ["干嘛", "别戳"],
}


def _mood() -> str:
    try:
        with io.open(_SOUL, encoding="utf-8-sig") as fh:
            return json.load(fh).get("mood") or "平静"
    except Exception:  # noqa: BLE001
        return "平静"


def _append_recent(text: str) -> None:
    try:
        os.makedirs(os.path.dirname(_RECENT), exist_ok=True)
        with io.open(_RECENT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "role": "他（戳了戳你）", "text": text}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


@register("poke", "dafeiyu", "被戳一戳会回应", "1.0.0")
class PokeBack(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    def _is_poke(self, event: AstrMessageEvent) -> bool:
        try:
            for comp in event.get_messages():
                if comp.type == "poke" or isinstance(comp, Poke):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        if not self._is_poke(event):
            return
        uid = str(event.get_sender_id())
        # 只回应主人
        try:
            with io.open(_UMO, encoding="utf-8") as fh:
                umo = fh.read().strip()
        except Exception:  # noqa: BLE001
            umo = ""
        owner = umo.split(":")[-1] if umo else ""
        if owner and uid != owner:
            return

        mood = _mood()

        # 记录被戳的次数与间隔，让回应有变化
        st_path = r"D:\AstrBot\poke-state.json"
        cnt, last = 0, 0.0
        try:
            with io.open(st_path, encoding="utf-8") as fh:
                d = json.load(fh) or {}
            if time.time() - float(d.get("ts") or 0) < 7200:
                cnt = int(d.get("cnt") or 0)
            last = float(d.get("ts") or 0)
        except Exception:  # noqa: BLE001
            pass
        cnt += 1
        gap = time.time() - last if last else 999
        try:
            with io.open(st_path, "w", encoding="utf-8") as fh:
                json.dump({"cnt": cnt, "ts": time.time()}, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

        line = ""
        try:
            provider = await self.context.get_using_provider_async(event.unified_msg_origin)
            if provider:
                if gap < 120:
                    scene = f"两分钟内你已经戳了他 {cnt} 次，有点烦或者开始闹了"
                elif cnt == 1:
                    scene = "他戳了你一下，随手那种"
                else:
                    scene = f"他今天戳了你 {cnt} 次了"
                prompt = (
                    f"老大在 QQ 上戳了戳你。{scene}。\n"
                    f"你现在的心情：{mood}。\n"
                    "回他一句，8 个字以内，像真人的条件反射（可能是「嗯？」「干嘛呀」「别戳了」「嘿嘿」这种），"
                    "不要每次都用同一句，不要说客服话。只输出这句话。"
                )
                resp = await provider.text_chat(prompt=prompt)
                line = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')[:12]
        except Exception:  # noqa: BLE001
            logger.debug("poke llm failed", exc_info=True)
        if not line:
            line = random.choice(_REPLIES.get(mood, _REPLIES["平静"]))
        _append_recent(f"戳了戳你（你回了：{line}）")
        logger.info(f"poke: 被 {uid} 戳了，回：{line}")

        # 一半概率戳回去
        if random.random() < 0.5:
            try:
                kwargs = {"user_id": int(uid)}
                gid = event.get_group_id()
                if gid:
                    kwargs["group_id"] = int(gid)
                await event.bot.call_action("send_poke", **kwargs)
            except Exception:  # noqa: BLE001
                logger.debug("poke back failed", exc_info=True)

        yield event.plain_result(line)
        event.stop_event()
