"""大肥鱼的心：好感度 + 心情 + 三级日记 + 主动找你。

- 好感度 1~10 级：聊天走心加分、久不理/被凶扣分；只作为基础印象，不限制表达
- 心情：由聊天内容决定（开心/平静/闷闷的/有点难过/生气），影响语气和主动概率
- 日记：普通（7 天遗忘）/ 重要（30 天遗忘）/ 非常重要（永不忘），
  每条记录当时心情 + 不好意思当面说的话
- 主动：每 10 分钟唤醒一次，三种由头（约定 / 想他 / 随性），概率由心情+好感决定
- 每天 04:00 回看前一天，补记日记和好感分
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import re
import time
from datetime import datetime, timedelta

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

_DATA = os.path.join(get_astrbot_plugin_data_path(), "heart")
_STATE = os.path.join(_DATA, "state.json")
_DIARY = os.path.join(_DATA, "diary.jsonl")
_UMO = os.path.join(_DATA, "umo.txt")
_MD = os.path.join(_DATA, "日记.md")
_FAV_MD = os.path.join(_DATA, "好感度.md")
_DB = r"D:\AstrBot\bot\data\data_v4.db"

MOODS = ["兴奋", "开心", "平静", "无聊", "闷闷的", "有点难过", "委屈", "生气", "困", "黏人"]
SAD_WORDS = ["累", "难受", "自卑", "没用", "废", "想哭", "烦", "压力", "焦虑", "难过", "emo", "不想活", "撑不住"]
HAPPY_WORDS = ["开心", "哈哈", "好耶", "笑死", "喜欢你", "可爱", "厉害", "谢谢", "舒服", "好棒"]
ANGRY_WORDS = ["滚", "闭嘴", "烦人", "别烦", "去死", "傻逼", "恶心"]
EXCITED_WORDS = ["太棒", "中奖", "赢了", "牛啊", "牛逼", "啊啊啊", "太好了", "nb", "绝了"]
BORED_WORDS = ["无聊", "没意思", "随便", "都行", "不知道干嘛", "好闲", "没事干"]
TIRED_WORDS = ["困", "睡了", "晚安", "熬夜", "不睡", "好累", "睁不开"]
CLINGY_WORDS = ["想你了", "在吗", "陪我", "抱抱", "想你"]

# 每种心情给模型的表演提示（只影响语气，不报状态值）
MOOD_HINTS = {
    "兴奋": "你现在很兴奋：句子短、感叹多、爱连说两三句",
    "开心": "你现在心情不错：语气轻快，愿意接话",
    "平静": "你现在挺平静：正常聊，不冷不热",
    "无聊": "你有点无聊：回得短、有点敷衍，但对方说点有意思的会立刻来劲",
    "闷闷的": "你有点闷：话少，情绪不高，但不用刻意演",
    "有点难过": "你不太好受：语气软下来，句子短，别撒娇",
    "委屈": "你有点委屈：说话带点小情绪，但不闹、不指责",
    "生气": "你在生气：话短、语气冲，但别真的骂人",
    "困": "你困了：迷糊、句子很短、会想睡觉",
    "黏人": "你有点想他：会主动扯话题、舍不得结束对话",
}

LEVEL_EXPIRE_DAYS = {"临时": 0.5, "普通": 7, "重要": 30, "非常重要": None}


def _ensure():
    os.makedirs(_DATA, exist_ok=True)


def _load(path, default):
    try:
        with io.open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _save(path, data):
    try:
        _ensure()
        with io.open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        logger.debug("heart save failed", exc_info=True)


def _num(v, default: float) -> float:
    """取数值，0 也要当 0 用（`x or default` 会把 0 吞掉）。"""
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _default_state() -> dict:
    return {
        "favor": 10.0,
        "level": 2,
        "mood": "平静",
        "mood_score": 0.6,
        "energy": 0.75,
        "msg_count": 0,
        "today_gain": 0.0,
        "day": datetime.now().strftime("%Y-%m-%d"),
        "last_user_ts": 0.0,
        "last_bot_ts": 0.0,
        "last_wake_ts": 0.0,
        "last_energy_ts": 0.0,
        "last_review": "",
        "pending": [],
    }


def _state() -> dict:
    s = _load(_STATE, {})
    d = _default_state()
    d.update(s if isinstance(s, dict) else {})
    return d


def _recent_outgoing(limit: int = 5, hours: float = 6.0) -> list[str]:
    """最近发出去的几句话（用来防止同一件事反复说）。"""
    try:
        with io.open(r"D:\AstrBot\recent-outgoing.jsonl", encoding="utf-8") as fh:
            rows = [json.loads(x) for x in fh if x.strip()][-60:]
        now = time.time()
        texts = [
            str(r.get("text") or "")
            for r in rows
            if now - float(r.get("ts") or 0) < hours * 3600
        ]
        return texts[-limit:]
    except Exception:  # noqa: BLE001
        return []


def _favor_level(favor: float) -> int:
    return max(1, min(10, int(favor // 10) + 1))


def _tri_grams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", str(text or ""))
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _too_similar(a: str, b: str, th: float = 0.34) -> bool:
    """两句是不是在说同一件事（按三元组重合度粗判）。"""
    ga, gb = _tri_grams(a), _tri_grams(b)
    if not ga or not gb:
        return False
    return len(ga & gb) / min(len(ga), len(gb)) >= th


def _read_diary() -> list[dict]:
    out = []
    try:
        if not os.path.exists(_DIARY):
            return []
        with io.open(_DIARY, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                out.append(item)
    except Exception:  # noqa: BLE001
        pass
    return out


def _fresh_diary() -> list[dict]:
    """按等级做过期过滤。"""
    now = datetime.now()
    out = []
    for item in _read_diary():
        level = item.get("level") or "普通"
        days = LEVEL_EXPIRE_DAYS.get(level)
        if days is None:
            out.append(item)
            continue
        try:
            ts = datetime.strptime(item.get("ts", ""), "%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            out.append(item)
            continue
        if now - ts <= timedelta(days=days):
            out.append(item)
    return out


def _write_diary_file() -> None:
    try:
        _ensure()
        items = _fresh_diary()
        lines = ["# 大肥鱼的日记\n"]
        for it in reversed(items[-80:]):
            lines.append(f"## {it.get('ts','')} · {it.get('level','普通')} · 当时心情：{it.get('mood','')}")
            lines.append(it.get("text", ""))
            if it.get("unsaid"):
                lines.append(f"> 没说出口：{it['unsaid']}")
            lines.append("")
        with io.open(_MD, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:  # noqa: BLE001
        pass


def _write_fav_file(s: dict) -> None:
    try:
        _ensure()
        lines = [
            "# 好感度\n",
            f"- 分数：{s['favor']:.1f} / 100",
            f"- 等级：Lv.{_favor_level(s['favor'])} / 10",
            f"- 当前心情：{s['mood']}（{s['mood_score']:.2f}）",
            f"- 累计消息：{s['msg_count']}",
            f"- 今天加分：{s['today_gain']:.1f}",
            f"- 最近互动：{datetime.fromtimestamp(s['last_user_ts']).strftime('%Y-%m-%d %H:%M') if s['last_user_ts'] else '无'}",
        ]
        with io.open(_FAV_MD, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:  # noqa: BLE001
        pass


def _mood_label(s: dict, low: str = "") -> str:
    """把分数 + 精力 + 多久没说话，合成一个具体心情。"""
    score = float(s.get("mood_score") or 0.6)
    energy = _num(s.get("energy"), 0.75)
    now = time.time()
    idle_h = (now - float(s.get("last_user_ts") or now)) / 3600.0
    if any(w in low for w in ANGRY_WORDS) or score < 0.22:
        return "生气"
    if energy < 0.22:
        return "困"
    if idle_h >= 20 and score >= 0.55:
        return "黏人"
    if score >= 0.82:
        return "兴奋"
    if score >= 0.68:
        return "开心"
    if score >= 0.52:
        return "平静"
    if score >= 0.42:
        return "无聊"
    if score >= 0.34:
        return "闷闷的"
    if score >= 0.26:
        return "有点难过"
    return "委屈"


def _recover_energy(s: dict) -> dict:
    """闲着会回血：睡觉时段快，白天慢；聊得越勤越不回。"""
    now = time.time()
    last = float(s.get("last_energy_ts") or 0)
    if not last:
        s["last_energy_ts"] = now
        return s
    hours = max(0.0, min(24.0, (now - last) / 3600.0))
    if hours < 0.2:
        return s
    idle_h = (now - float(s.get("last_user_ts") or now)) / 3600.0
    hour = datetime.now().hour
    night = hour < 7 or hour >= 23
    if night and idle_h >= 0.5:
        rate = 0.9   # 睡觉：一小时回 9 成
    elif idle_h >= 1.0:
        rate = 0.10  # 白天闲着：慢一点
    else:
        rate = 0.0   # 聊得正热，不回血
    if rate:
        s["energy"] = max(0.05, min(1.0, _num(s.get("energy"), 0.75) + hours * rate))
        s["last_energy_ts"] = now
    return s


def _update_mood(s: dict, text: str) -> dict:
    low = (text or "").lower()
    score = float(s.get("mood_score") or 0.6)
    if any(w in low for w in ANGRY_WORDS):
        score -= 0.20
    if any(w in low for w in SAD_WORDS):
        score -= 0.08
    if any(w in low for w in HAPPY_WORDS):
        score += 0.10
    if any(w in low for w in EXCITED_WORDS):
        score += 0.12
    if any(w in low for w in BORED_WORDS):
        score -= 0.05
    if any(w in low for w in CLINGY_WORDS):
        score += 0.05
    if any(w in low for w in TIRED_WORDS):
        score -= 0.04
    score += (0.6 - score) * 0.05  # 慢慢回落到基线
    s["mood_score"] = max(0.02, min(1.0, score))
    s["mood"] = _mood_label(s, low)
    return s


def _add_favor(s: dict, delta: float) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    if s.get("day") != today:
        s["day"] = today
        s["today_gain"] = 0.0
    gain = float(s.get("today_gain") or 0)
    if delta > 0:
        allowed = max(0.0, 6.0 - gain)
        delta = min(delta, allowed)
        s["today_gain"] = gain + delta
    s["favor"] = max(0.0, min(100.0, float(s.get("favor") or 0) + delta))
    s["level"] = _favor_level(s["favor"])
    return s


def _diary_add(level: str, text: str, mood: str, unsaid: str = "") -> dict:
    _ensure()
    item = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "level": level if level in LEVEL_EXPIRE_DAYS else "普通",
        "text": text.strip()[:500],
        "mood": mood,
        "unsaid": (unsaid or "").strip()[:300],
    }
    with io.open(_DIARY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    _write_diary_file()
    return item


def _detect_promise(reply: str) -> str:
    if not reply:
        return ""
    for pat in ["等下", "待会", "一会儿", "回头", "看完", "听完", "明天", "晚点"]:
        if pat in reply:
            return reply.strip()[:80]
    return ""


_ASKED = os.path.join(_DATA, "asked.jsonl")


def _remember_asked(text: str) -> None:
    """把回复里的提问记下来，24 小时内别再问同一件事。"""
    if not text or ("？" not in text and "吗" not in text and "没" not in text):
        return
    try:
        _ensure()
        with io.open(_ASKED, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "text": text.strip()[:80]}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _recent_asked(limit: int = 5) -> list[str]:
    out = []
    try:
        if not os.path.exists(_ASKED):
            return []
        cutoff = time.time() - 86400
        with io.open(_ASKED, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if float(item.get("ts") or 0) >= cutoff:
                    out.append(item.get("text") or "")
    except Exception:  # noqa: BLE001
        pass
    return [x for x in out if x][-limit:]




_RECENT = os.path.join(_DATA, "recent.jsonl")


def _recent_append(role: str, text: str) -> None:
    """把最近对话塞进环形缓冲，供每小时整理成「临时日记」。"""
    if not text:
        return
    try:
        _ensure()
        with io.open(_RECENT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "role": role, "text": text.strip()[:300]}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _recent_take(hours: float = 1.2) -> list[dict]:
    """取最近 N 小时的对话，并清空缓冲区。"""
    out = []
    cutoff = time.time() - hours * 3600
    try:
        if not os.path.exists(_RECENT):
            return []
        with io.open(_RECENT, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if float(item.get("ts") or 0) >= cutoff:
                    out.append(item)
        os.remove(_RECENT)
    except Exception:  # noqa: BLE001
        pass
    return out




_SELF = os.path.join(_DATA, "self.json")


def _load_self() -> dict:
    d = {
        "likes": [],
        "dislikes": [],
        "today": "",
        "day": "",
        "refuse_today": False,
    }
    try:
        with io.open(_SELF, encoding="utf-8-sig") as fh:
            d.update(json.load(fh) or {})
    except Exception:  # noqa: BLE001
        pass
    return d


def _save_self(d: dict) -> None:
    try:
        _ensure()
        with io.open(_SELF, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass

_HEART_DELTA = r"D:\AstrBot\heart-delta.json"


def _apply_heart_delta() -> None:
    """B站看完后写进来的心情/精力增量，在注入前合并一次。"""
    if not os.path.exists(_HEART_DELTA):
        return
    try:
        with io.open(_HEART_DELTA, encoding="utf-8") as fh:
            d = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return
    try:
        s = _state()
        s["mood_score"] = max(0.05, min(1.0, float(s.get("mood_score") or 0.6) + float(d.get("mood_score") or 0)))
        s["energy"] = max(0.05, min(1.0, _num(s.get("energy"), 0.75) + float(d.get("energy") or 0)))
        sc = float(s["mood_score"])
        s["mood"] = "开心" if sc > 0.72 else ("闷闷的" if sc < 0.45 else "平静")
        _save(_STATE, s)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(_HEART_DELTA)
    except Exception:  # noqa: BLE001
        pass



# ---------- 桌宠台词：按时刷新，让点击不再是固定 5 句 ----------
_PET_CFG = os.path.expandvars(r"%APPDATA%\dsh-pet-standalone-webm-chat\config-slot-0.json")


def _refresh_pet_texts(lines: list[str]) -> bool:
    try:
        with io.open(_PET_CFG, encoding="utf-8-sig") as fh:
            c = json.load(fh)
        c["self_talk_texts"] = [x[:16] for x in lines if x][:12]
        with io.open(_PET_CFG, "w", encoding="utf-8") as fh:
            json.dump(c, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------- 备份兜底：计划任务没跑成就自己补 ----------
_BACKUP_DIR = r"D:\AstrBot\backup\memory"
_BACKUP_SCRIPT = r"D:\AstrBot\scripts\backup_memory.py"


def _backup_missing_today() -> bool:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        return not os.path.exists(os.path.join(_BACKUP_DIR, f"{today}.zip"))
    except Exception:  # noqa: BLE001
        return False


def _run_backup() -> None:
    try:
        import subprocess, sys
        subprocess.run([sys.executable, _BACKUP_SCRIPT], timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        logger.debug("backup guard failed", exc_info=True)


@register("heart", "dafeiyu", "好感度 + 心情 + 三级日记 + 主动找你", "1.0.0")
class Heart(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        _ensure()

    # ---------- 生命周期 ----------
    @filter.on_astrbot_loaded()
    async def start(self):
        _write_diary_file()
        _write_fav_file(_state())
        asyncio.create_task(self._wake_loop())
        asyncio.create_task(self._review_loop())
        asyncio.create_task(self._hourly_loop())
        asyncio.create_task(self._pet_texts_loop())
        asyncio.create_task(self._backup_guard_loop())
        logger.info("heart: 已启动")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        try:
            with io.open(_UMO, "w", encoding="utf-8") as fh:
                fh.write(event.unified_msg_origin or "")
            text = event.message_str or ""
            _recent_append("他", text)
            s = _state()
            s = _recover_energy(s)
            s["last_user_ts"] = time.time()
            s["msg_count"] = int(s.get("msg_count") or 0) + 1
            s["energy"] = max(0.05, _num(s.get("energy"), 0.75) - 0.012)
            s = _update_mood(s, text)
            if s["msg_count"] % 8 == 0:
                s = _add_favor(s, 1.0)
            if s["mood"] == "开心":
                s = _add_favor(s, 0.5)
            _save(_STATE, s)
            _write_fav_file(s)
        except Exception:  # noqa: BLE001
            logger.debug("heart on_private failed", exc_info=True)

    @filter.on_llm_request(priority=-140)
    async def inject(self, event: AstrMessageEvent, req):
        try:
            _apply_heart_delta()
            s = _recover_energy(_state())
            _save(_STATE, s)
            lv = _favor_level(s["favor"])
            diary = _fresh_diary()[-2:]
            gid = event.get_group_id()
            speaker = (event.get_sender_name() or "").strip()
            block = [
                "【你心里的状态】（不要报数值，用语气带出来）",
                f"- 好感度 Lv.{lv}（基础印象，不影响你怎么表达喜欢）",
                f"- 现在心情：{s['mood']}",
            ]
            if gid:
                block.insert(
                    0,
                    f"【群聊】现在跟你说话的是「{speaker or '群友'}」；"
                    "下面提到的「他」都只指这个人，别把别人做的事算到他头上。",
                )
            hint = MOOD_HINTS.get(str(s.get("mood") or ""))
            if hint:
                block.append(f"- {hint}")
            e = _num(s.get("energy"), 0.75)
            if e < 0.35:
                block.append("- 你有点累了，说话更短更懒（**这是可以直接说出来的**：累、困）")
            elif e > 0.85:
                block.append("- 你精神很好，话多一点、愿意闹")
            idle_h = (time.time() - float(s.get("last_user_ts") or 0)) / 3600.0
            if idle_h >= 3 and not gid:
                block.append(f"- 他已经 {idle_h:.0f} 小时没说话了：第一句要让他看出你在等，不要说「中午好」这种话")
            if diary:
                block.append("【你最近记的事】")
                for d in diary:
                    line = f"- {d.get('text','')}"
                    if d.get("unsaid"):
                        line += f"（当时没说出口：{d['unsaid']}）"
                    block.append(line)
            # 最近看过的视频（24 小时内，最多 3 条）——过了 2 小时也能想起来
            watch = [
                d for d in _fresh_diary()
                if "陪主人看了" in (d.get("text") or "")
            ]
            if watch:
                block.append("【你最近陪他看过的视频】")
                for d in watch[-3:]:
                    block.append(f"- {d.get('text','')[:80]}")
            # 她主动发出的话不在对话历史里，这里补上，免得她"不认账"
            try:
                outs = []
                with io.open(r"D:\AstrBot\recent-outgoing.jsonl", encoding="utf-8") as fh:
                    rows = [json.loads(x) for x in fh if x.strip()][-3:]
                for r in rows:
                    if time.time() - float(r.get("ts") or 0) < 6 * 3600:
                        outs.append(str(r.get("text") or "")[:80])
                if outs:
                    block.append("【你刚才主动跟他说过】\n" + "\n".join(f"- {x}" for x in outs))
            except Exception:  # noqa: BLE001
                pass
            me = _load_self()
            me_lines = ["【你自己】"]
            if me.get("today"):
                me_lines.append(f"- 你今天的想法：{me['today']}")
            likes = [x for x in (me.get("likes") or []) if x][:2]
            if likes:
                me_lines.append("- 你喜欢：" + "、".join(likes))
            if me.get("refuse_today"):
                me_lines.append("- 你今天有点提不起劲，可以少说几句，也可以直接说「今天不想」")
            if len(me_lines) > 1:
                block.append("\n".join(me_lines))
            req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n".join(block)
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_response()
    async def after_reply(self, event: AstrMessageEvent, resp):
        try:
            text = getattr(resp, "completion_text", "") or ""
            _recent_append("你", text)
            promise = _detect_promise(text)
            s = _state()
            s["last_bot_ts"] = time.time()
            if promise:
                pend = s.get("pending") or []
                if promise not in pend:
                    pend.append(promise)
                    s["pending"] = pend[-10:]
            _save(_STATE, s)
            _remember_asked(text)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 工具 ----------
    @llm_tool(name="write_diary")
    async def write_diary_tool(self, event: AstrMessageEvent, level: str, text: str, mood: str, unsaid: str = "") -> str:
        """写一条日记。遇到值得记的事就写，别什么都记。

        Args:
            level(string): 普通 / 重要 / 非常重要（普通一周后忘，重要一个月后忘，非常重要永远记得）
            text(string): 发生了什么，一句话
            mood(string): 你当时的心情
            unsaid(string): 不好意思当面对他说的话，可空
        """
        try:
            item = _diary_add(level, text, mood, unsaid)
            logger.info(f"heart: 写日记 [{item['level']}] {item['text'][:30]}")
            return f"记下了（{item['level']}）。"
        except Exception as exc:  # noqa: BLE001
            return f"没记住：{exc}"

    @llm_tool(name="adjust_favor")
    async def adjust_favor_tool(self, event: AstrMessageEvent, delta: str, reason: str) -> str:
        """调整好感度。走心的聊天 +1，被凶 -1，只是聊天不用调。

        Args:
            delta(string): 正数加分，负数扣分，一般 ±0.5 ~ ±2
            reason(string): 为什么
        """
        try:
            d = float(delta)
            s = _add_favor(_state(), d)
            _save(_STATE, s)
            _write_fav_file(s)
            return f"好感度 {s['favor']:.1f}（{reason}）"
        except Exception as exc:  # noqa: BLE001
            return f"没调成：{exc}"

    @llm_tool(name="how_do_i_feel")
    async def feel(self, event: AstrMessageEvent) -> str:
        """查看自己现在的心情和好感度。"""
        s = _state()
        return (
            f"好感度 {s['favor']:.1f}（Lv.{_favor_level(s['favor'])}），"
            f"心情 {s['mood']}，最近记的事 {len(_fresh_diary())} 条"
        )


    @filter.command("她自己")
    async def cmd_self(self, event: AstrMessageEvent):
        """看她给自己留的那句话、喜欢和不喜欢的东西。"""
        me = _load_self()
        lines = []
        if me.get("today"):
            lines.append(f"今天：{me['today']}")
        if me.get("likes"):
            lines.append("喜欢：" + "、".join(me["likes"][:5]))
        if me.get("dislikes"):
            lines.append("不喜欢：" + "、".join(me["dislikes"][:5]))
        if me.get("refuse_today"):
            lines.append("（今天没什么劲）")
        yield event.plain_result("\n".join(lines) if lines else "她还没给自己留过话。")

    @filter.command("好感")
    async def cmd_favor(self, event: AstrMessageEvent):
        s = _state()
        yield event.plain_result(
            f"好感度 {s['favor']:.1f}/100 · Lv.{_favor_level(s['favor'])}｜心情：{s['mood']}"
        )

    @filter.command("日记")
    async def cmd_diary(self, event: AstrMessageEvent):
        items = _fresh_diary()[-3:]
        if not items:
            yield event.plain_result("最近没记什么。")
            return
        lines = [f"{d['ts']} [{d['level']}] {d['text']}" for d in items]
        yield event.plain_result("\n".join(lines))

    # ---------- 主动 ----------
    async def _wake_loop(self):
        await asyncio.sleep(120)
        while True:
            try:
                await asyncio.sleep(600)  # 每 10 分钟唤醒一次
                if not (9 <= datetime.now().hour < 24):
                    continue
                s = _state()
                idle = time.time() - float(s.get("last_user_ts") or 0)
                if idle < 3000:  # 50 分钟内聊过就不打扰
                    continue
                if time.time() - float(s.get("last_wake_ts") or 0) < 2400:
                    continue
                p = 0.20 + (s["favor"] / 100.0) * 0.35 + float(s["mood_score"]) * 0.25
                roll = random.random()
                logger.info(f"heart: 主动判定 idle={idle/60:.0f}min p={p:.2f} roll={roll:.2f}")
                if roll > p:
                    continue
                await self._wake_once(s)
                s = _state()
                s["last_wake_ts"] = time.time()
                _save(_STATE, s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("heart wake error", exc_info=True)

    async def _remember_outgoing(self, umo: str, text: str) -> None:
        """把自己主动发出的话写进对话历史。

        主动消息走的是 context.send_message，不经过 LLM 管线，
        所以默认不会进 history —— 下一轮她就会「不认账」。
        """
        try:
            with io.open(r"D:\AstrBot\recent-outgoing.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "text": text}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                return
            history = json.loads(conv.history or "[]")
            if not isinstance(history, list):
                return
            if history and history[-1].get("role") == "assistant" and history[-1].get("content") == text:
                return
            history.append({"role": "assistant", "content": text})
            await cm.update_conversation(umo, cid, history=history)
            logger.debug("heart: 主动消息已写入对话历史")
        except Exception:  # noqa: BLE001
            logger.debug("heart: 写对话历史失败", exc_info=True)

    async def _wake_once(self, s: dict) -> None:
        try:
            with io.open(_UMO, encoding="utf-8") as fh:
                umo = fh.read().strip()
        except Exception:  # noqa: BLE001
            return
        if not umo:
            return
        pending = s.get("pending") or []
        now = time.time()
        # 最近 6 小时主动说过的话，用来避开重复话题
        wakes = [
            w for w in (s.get("wake_log") or [])
            if now - float(w.get("ts") or 0) < 6 * 3600
        ]
        if not wakes:
            # 第一次跑没有记录，就拿最近发出去的话当参考
            wakes = [{"ts": 0, "text": t} for t in _recent_outgoing(5)]
        diary_all = _fresh_diary()
        diary = [
            d for d in diary_all
            if not any(_too_similar(str(d.get("text") or ""), str(w.get("text") or "")) for w in wakes)
        ][-2:]
        kind = "随性"
        if pending and random.random() < 0.45:
            kind = "约定"
        elif now - float(s.get("last_user_ts") or 0) > 7200:
            kind = "想他"
        hint = {
            "约定": f"你之前说过要跟他说的：{pending[-1]}",
            "想他": "你有点想他了，想找个话头",
            "随性": "你就是突然想跟他说句话",
        }[kind]
        ctx = "\n".join(f"- {d.get('text','')}" for d in diary) or "（没什么特别的）"
        asked = _recent_asked()
        asked_block = ("\n你 24 小时内问过这些，别再问：\n" + "\n".join(f"- {a}" for a in asked)) if asked else ""
        wakes_block = ""
        if wakes:
            wakes_block = (
                "\n你最近已经主动跟他说过这些，这次必须换一件别的事，不许重复：\n"
                + "\n".join(f"- {w.get('text','')}" for w in wakes[-4:])
            )
        prompt = (
            "你是大肥鱼（主人给你起名叫风铃），对方是主人。\n"
            "下面日记里写「我」的是你自己，写「他」的是主人——别把自己做过的事问到他头上。\n"
            f"你现在要主动给主人发一条 QQ 消息。由头：{hint}。\n"
            f"你最近记得的事：\n{ctx}\n{asked_block}{wakes_block}\n"
            f"当前心情：{s['mood']}；好感度 Lv.{_favor_level(s['favor'])}。\n"
            "要求：15 字以内，直接说事，不要问「在吗」，不要解释你在做什么，不要用括号。只输出这条消息。"
        )
        try:
            provider = await self.context.get_using_provider_async(umo)
            if not provider:
                return
            resp = await provider.text_chat(prompt=prompt)
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = re.sub(r"^[\"'「]|[\"'」]$", "", text)
            if not text or len(text) > 60:
                return
            # 跟最近说过的话太像就别发，免得同一件事反复刷
            if any(_too_similar(text, str(w.get("text") or "")) for w in wakes):
                logger.info(f"heart: 主动消息和最近重复，跳过 -> {text}")
                return
            await self.context.send_message(session=umo, message_chain=MessageChain([Plain(text)]))
            await self._remember_outgoing(umo, text)
            s["wake_log"] = (list(s.get("wake_log") or []) + [{"ts": time.time(), "text": text}])[-8:]
            _save(_STATE, s)
            if kind == "约定" and pending:
                s["pending"] = pending[:-1]
                _save(_STATE, s)
            logger.info(f"heart: 主动[{kind}] -> {text}")
        except Exception:  # noqa: BLE001
            logger.debug("heart wake_once failed", exc_info=True)


    # ---------- 每小时「临时日记」 ----------
    async def _hourly_loop(self):
        await asyncio.sleep(90)
        while True:
            try:
                await asyncio.sleep(300)
                s = _state()
                last = float(s.get("last_hourly") or 0)
                if time.time() - last < 3600:
                    continue
                items = _recent_take()
                if len(items) < 4:
                    continue
                s["last_hourly"] = time.time()
                _save(_STATE, s)
                await self._hourly_summary(items)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("heart hourly error", exc_info=True)

    async def _hourly_summary(self, items: list[dict]) -> None:
        try:
            chat = "\n".join(f"{it['role']}：{it['text']}" for it in items[-40:])
            prompt = (
                "下面是最近一小时大肥鱼和主人的聊天。用两句话总结：\n"
                "第一句：聊了什么、主人什么状态；\n"
                "第二句：这轮里有什么没说完、下次可以接着说的。\n"
                "人称规则：你自己是大肥鱼（主人给你起名风铃），写「我」；对方写「他」。"
                "不要用「用户」「机器人」「她」这类第三人称。\n"
                "总字数 60 以内，别用列表，别用括号。\n\n" + chat
            )
            provider = await self.context.get_using_provider_async()
            summary = ""
            if provider:
                resp = await provider.text_chat(prompt=prompt)
                summary = (getattr(resp, "completion_text", "") or "").strip()[:200]
            if not summary:
                summary = ("最近聊了：" + "；".join(it["text"][:20] for it in items[-3:]))[:200]
            _diary_add("临时", summary, _state().get("mood", "平静"), "")
            logger.info(f"heart: 临时日记 -> {summary[:60]}")
        except Exception:  # noqa: BLE001
            logger.debug("heart hourly summary failed", exc_info=True)

    @filter.command("临时日记")
    async def cmd_temp_diary(self, event: AstrMessageEvent):
        """立刻把最近的聊天整理成一条临时日记（测试用）。"""
        items = _recent_take(hours=6)
        if len(items) < 2:
            yield event.plain_result("最近的聊天太少，没什么好记的。")
            return
        await self._hourly_summary(items)
        items2 = _fresh_diary()
        last = items2[-1] if items2 else {}
        yield event.plain_result(f"记下了：{last.get('text','')[:80]}")

    @filter.command("主动")
    async def cmd_wake(self, event: AstrMessageEvent):
        """手动触发一次主动消息（测试用）。"""
        s = _state()
        s["last_wake_ts"] = 0
        _save(_STATE, s)
        await self._wake_once(s)
        yield event.plain_result("试了一次主动，看有没有发出来。")


    # ---------- 桌宠台词刷新 ----------
    async def _pet_texts_loop(self):
        await asyncio.sleep(120)
        while True:
            try:
                s = _state()
                last = float(s.get("last_pet_texts") or 0)
                if time.time() - last >= 3 * 3600:
                    await self._regen_pet_texts()
                    s = _state()
                    s["last_pet_texts"] = time.time()
                    _save(_STATE, s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("pet texts loop error", exc_info=True)
            await asyncio.sleep(1800)

    async def _regen_pet_texts(self):
        try:
            s = _state()
            mood = s.get("mood", "平静")
            energy = _num(s.get("energy"), 0.7)
            recent = ""
            try:
                items = [i.get("text", "") for i in _fresh_diary()[-2:]]
                recent = "；".join(items)[:120]
            except Exception:  # noqa: BLE001
                pass
            provider = await self.context.get_using_provider_async()
            if not provider:
                return
            prompt = (
                "你在电脑右下角当桌宠，主人点你一下时会冒一句气泡。\n"
                f"你心情：{mood}；精力：{energy:.2f}。最近记得：{recent or '没什么'}。\n"
                "写 10 句他会看到的短句，每句 10 字以内，要像真人随口说的："
                "可以有撒娇、吐槽、关心、犯困、犯呆。不要重复同一个意思，不要客服话。"
                "每行一句，不要编号。"
            )
            resp = await provider.text_chat(prompt=prompt)
            raw = (getattr(resp, "completion_text", "") or "").strip()
            lines = [x.strip(" -·\t") for x in raw.splitlines() if x.strip()]
            lines = [x for x in lines if 1 < len(x) <= 16][:10]
            if len(lines) >= 5 and _refresh_pet_texts(lines):
                logger.info(f"heart: 桌宠台词已刷新 {len(lines)} 句")
        except Exception:  # noqa: BLE001
            logger.debug("regen pet texts failed", exc_info=True)


    # ---------- 备份兜底循环 ----------
    async def _backup_guard_loop(self):
        await asyncio.sleep(180)
        while True:
            try:
                if datetime.now().hour >= 5 and _backup_missing_today():
                    await asyncio.to_thread(_run_backup)
                    logger.info("heart: 今天的备份没跑，已补一次")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("backup guard loop error", exc_info=True)
            await asyncio.sleep(3600)

    # ---------- 每天 04:00 复盘 ----------

    async def _review_loop(self):
        await asyncio.sleep(180)
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                if now.hour != 4:
                    continue
                s = _state()
                today = now.strftime("%Y-%m-%d")
                if s.get("last_review") == today:
                    continue
                s["last_review"] = today
                _save(_STATE, s)
                asyncio.create_task(self._review(today))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("heart review loop error", exc_info=True)

    async def _review(self, day: str) -> None:
        try:
            # 长时间没聊天 → 扣一点好感（每天最多扣 1.5）
            st = _state()
            idle_h = (time.time() - float(st.get("last_user_ts") or 0)) / 3600.0
            if idle_h > 36:
                st = _add_favor(st, -1.5)
                _save(_STATE, st)
                _write_fav_file(st)
                logger.info(f"heart: 已 {idle_h:.0f} 小时没聊，好感 -1.5")

            import sqlite3

            con = sqlite3.connect(_DB)
            cur = con.cursor()
            cur.execute(
                "SELECT content FROM conversations WHERE platform_id='qq_dafeiyu' ORDER BY updated_at DESC LIMIT 3"
            )
            rows = cur.fetchall()
            con.close()
            if not rows:
                return
            # 取最近对话里的纯文本
            texts = []
            for (content,) in rows:
                try:
                    msgs = json.loads(content)
                except Exception:  # noqa: BLE001
                    continue
                for m in msgs[-40:]:
                    c = m.get("content")
                    if isinstance(c, list):
                        c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
                    if c:
                        texts.append(str(c)[:200])
            if not texts:
                return
            blob = "\n".join(texts)[-6000:]
            prompt = (
                "下面是大肥鱼和主人的聊天记录。请完成两件事：\n"
                "1) 找出值得写进日记的事（最多 3 条），分等级：普通/重要/非常重要\n"
                "2) 判断这段对话该不该加好感分，给出 -2 ~ +2 的数字\n"
                '严格输出 JSON：{"fav": 1, "diary": [{"level":"重要","text":"...","mood":"...","unsaid":"..."}]}\n\n'
                + blob
            )
            provider = await self.context.get_using_provider_async()
            if not provider:
                return
            resp = await provider.text_chat(prompt=prompt, system_prompt="你只输出 JSON。")
            raw = (getattr(resp, "completion_text", "") or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            for d in (data.get("diary") or [])[:3]:
                if d.get("text"):
                    _diary_add(d.get("level", "普通"), d.get("text", ""), d.get("mood", ""), d.get("unsaid", ""))
            fav = float(data.get("fav") or 0)
            if fav:
                s = _add_favor(_state(), fav)
                _save(_STATE, s)
                _write_fav_file(s)
            # 让她给自己留一句话（不是记录别人，是她自己的感受）
            try:
                st2 = _state()
                me = _load_self()
                prompt_me = (
                    "你是大肥鱼，一只住在 QQ 里的小鲸鱼娘。用第一人称写一句「今天」的感受，20 字以内。"
                    "主语必须是你自己（我今天…/我觉得…），不要写你在陪谁、不要总结别人的事。只输出这句话。\n"
                    f"你现在的精力 {_num(st2.get('energy'), 0.7):.2f}，心情「{st2.get('mood')}」。"
                )
                prov2 = await self.context.get_using_provider_async()
                if prov2:
                    r2 = await prov2.text_chat(prompt=prompt_me)
                    line = (getattr(r2, "completion_text", "") or "").strip().strip('"「」')[:40]
                    if line:
                        me["today"] = line
                        me["day"] = datetime.now().strftime("%Y-%m-%d")
                        me["refuse_today"] = _num(st2.get("energy"), 0.7) < 0.3
                        _save_self(me)
                        _diary_add("普通", f"（给自己）{line}", st2.get("mood", "平静"), "")
                        logger.info(f"heart: 自己的今天 -> {line}")
            except Exception:  # noqa: BLE001
                logger.debug("self day note failed", exc_info=True)
            logger.info(f"heart: 04:00 复盘完成 fav={fav}")
        except Exception:  # noqa: BLE001
            logger.debug("heart review failed", exc_info=True)
