"""大肥鱼的心智：情绪状态 + 长期记忆 + 梗库。

- 情绪：好奇 / 无聊 / 精力 / 心情，会随时间和聊天变化
- 记忆：记住用户说过的事（笔记）
- 梗库：遇到新梗会自己查/自己学，聊天时优先用会的梗接话

数据文件（沿用原有格式）：
  state.json          情绪状态
  memes_learned.json  [{"term","meaning","example"}]
  notes.jsonl         {"ts","kind","text"}
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
from pathlib import Path

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    _DATA_BASE = Path(get_astrbot_plugin_data_path())
except Exception:  # noqa: BLE001
    _DATA_BASE = Path.cwd() / "data" / "plugin_data"

_DIR = _DATA_BASE / "dafeiyu_soul"
_STATE = _DIR / "state.json"
_MEMES = _DIR / "memes_learned.json"
_NOTES = _DIR / "notes.jsonl"

_DEFAULT_STATE = {
    "curiosity": 0.65,
    "boredom": 0.25,
    "energy": 0.8,
    "mood": 0.6,
    "last_message_ts": 0.0,
    "last_reply_ts": 0.0,
    "ignored_streak": 0,
    "msg_count": 0,
    "updated_ts": 0.0,
}

_QUESTION_HINTS = ("?", "？", "吗", "呢", "为什么", "咋", "怎么", "什么", "是不是")
_HAPPY_HINTS = ("哈哈", "笑死", "好耶", "nice", "牛", "6", "爽", "开心")
_SAD_HINTS = ("累", "难受", "烦", "哭了", "无语", "烦死", "困")

# 高频虚词二元组：做「证据」匹配时要滤掉，否则「什么」「今天」会把什么都捞上来
_STOP_GRAMS = frozenset(
    """
    今天 明天 昨天 现在 什么 怎么 可以 这个 那个 我们 你们 他们 她们 自己 一个 没有
    就是 还是 知道 觉得 时候 已经 因为 所以 但是 如果 然后 这样 那样 一下 有点 真的
    不是 记得 还记 得我 得你 的吗 了吗 是不 好像 应该 可能 一直 其实 比较 非常 特别
    东西 事情 地方 问题 感觉 起来 出来 回来 过来 是的 了你 了吧 我觉 我感 有没 就好
    """.split()
)

_LEARN_PROMPT = """你是一个中文网络用语分析器。根据下面这段聊天，抽取：
1) 出现的网络梗/黑话/流行语，给出简短含义（没有就给空数组）；
2) 关于主人、值得长期记住的事实（称呼、作息、喜好、正在做的事；没有就给空数组）。
   人称规则：你自己是「我」，对方是「主人」。不要写「用户」「机器人」「她」。
   level 怎么填：称呼、约定、长期不变的喜好 → 非常重要；近期发生的事、临时状态 → 普通；
   介于两者之间（习惯、偏好、正在做的项目）→ 重要。
只输出 JSON，不要解释：
{"memes":[{"term":"","meaning":"","example":""}],"notes":[{"text":"","level":"普通"}]}
"""



_STOP_TERMS = set("""上次 过去 自己 别人 其实 可能 真的 这个 那个 什么 怎么 因为 所以 但是 如果 已经 现在 今天 明天 昨天 感觉 觉得 知道 时候 东西 事情""".split())

def _is_real_meme(term: str) -> bool:
    """挡掉"上次/自己/过去"这种常用词，它们不是梗。"""
    t = (term or "").strip()
    if len(t) < 2:
        return False
    if t in _STOP_TERMS:
        return False
    # 纯汉字且长度 2-8 才可能是网络用语
    if not all("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in t):
        return False
    return True


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


class Soul:
    def __init__(self) -> None:
        self.state = dict(_DEFAULT_STATE)
        self.memes: list[dict] = []
        self.notes: list[dict] = []
        self.lock = asyncio.Lock()
        self.last_learn_ts = 0.0
        self.last_prune_ts = 0.0
        self._injected: set[str] = set()
        self._load()
        self._migrate_meme_meta()
        self.prune_memes()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        _DIR.mkdir(parents=True, exist_ok=True)
        try:
            if _STATE.exists():
                data = json.loads(_STATE.read_text("utf-8"))
                self.state.update({k: data[k] for k in _DEFAULT_STATE if k in data})
            if _MEMES.exists():
                self.memes = json.loads(_MEMES.read_text("utf-8")) or []
            if _NOTES.exists():
                for line in _NOTES.read_text("utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            self.notes.append(json.loads(line))
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            logger.warning("心智状态读取失败，使用默认值", exc_info=True)

    def save(self) -> None:
        _DIR.mkdir(parents=True, exist_ok=True)
        try:
            _STATE.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8"
            )
            _MEMES.write_text(
                json.dumps(self.memes, ensure_ascii=False, indent=2), "utf-8"
            )
            _NOTES.write_text(
                "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in self.notes),
                "utf-8",
            )
        except Exception:  # noqa: BLE001
            logger.warning("心智状态保存失败", exc_info=True)

    # ---------- 情绪演化 ----------
    def decay(self) -> None:
        now = time.time()
        last = self.state.get("updated_ts") or now
        hours = max(0.0, min(48.0, (now - last) / 3600.0))
        if hours <= 0:
            return
        self.state["boredom"] = _clamp(
            self.state["boredom"] + hours * 0.22
        )  # 越久没人说话越无聊
        # 精力：睡着（夜里且安静）恢复最快，白天闲着慢，正聊着不回
        hour = time.localtime(now).tm_hour
        idle_h = (now - float(self.state.get("last_message_ts") or 0)) / 3600.0
        night = hour < 7 or hour >= 23
        if night and idle_h >= 0.5:
            rate = 0.9
        elif idle_h >= 1.0:
            rate = 0.10
        else:
            rate = 0.0
        if rate:
            self.state["energy"] = _clamp(self.state["energy"] + hours * rate)
        self.state["curiosity"] = _clamp(self.state["curiosity"] - hours * 0.10)
        self.state["mood"] += (0.5 - self.state["mood"]) * min(1.0, hours * 0.15)
        self.state["mood"] = _clamp(self.state["mood"])
        self.state["updated_ts"] = now

    def on_message(self, text: str) -> None:
        now = time.time()
        self.decay()
        self.state["boredom"] = _clamp(self.state["boredom"] - 0.12)
        self.state["energy"] = _clamp(self.state["energy"] - 0.015)
        bump = 0.10 if any(h in text for h in _QUESTION_HINTS) else 0.04
        self.state["curiosity"] = _clamp(self.state["curiosity"] + bump)
        if any(h in text for h in _HAPPY_HINTS):
            self.state["mood"] = _clamp(self.state["mood"] + 0.06)
        if any(h in text for h in _SAD_HINTS):
            self.state["mood"] = _clamp(self.state["mood"] - 0.03)
        self.state["last_message_ts"] = now
        self.state["msg_count"] = int(self.state.get("msg_count", 0)) + 1
        self.state["updated_ts"] = now

    def on_reply(self) -> None:
        self.state["last_reply_ts"] = time.time()
        self.state["ignored_streak"] = 0

    # ---------- 提示词 ----------

    # ---------- 梗的用进废退 ----------
    def _migrate_meme_meta(self) -> None:
        """给旧数据补上时间字段。"""
        now = time.time()
        changed = False
        for m in self.memes:
            if "added" not in m:
                m["added"] = now
                changed = True
            if "hits" not in m:
                m["hits"] = 0
                changed = True
            if "last_used" not in m:
                m["last_used"] = m["added"]
                changed = True
        if changed:
            self.save()

    def _meme_weight(self, m: dict) -> float:
        now = time.time()
        hits = int(m.get("hits") or 0)
        last = float(m.get("last_used") or m.get("added") or now)
        age_days = max(0.0, (now - last) / 86400.0)
        # 30 天内用过才有力气；越久不用越轻
        if age_days > 30 and hits == 0:
            return 0.0
        recency = max(0.0, 1.0 - age_days / 30.0)
        return hits * 2.0 + recency

    def prune_memes(self) -> None:
        """60 天没用过、也从没命中过的梗，删掉。"""
        now = time.time()
        if now - self.last_prune_ts < 6 * 3600:
            return
        self.last_prune_ts = now
        before = len(self.memes)
        keep = []
        for m in self.memes:
            last = float(m.get("last_used") or m.get("added") or now)
            age_days = (now - last) / 86400.0
            hits = int(m.get("hits") or 0)
            if age_days > 60 and hits == 0:
                continue
            keep.append(m)
        if len(keep) != before:
            self.memes = keep
            self.save()
            logger.info(f"心智学习：清理久不用的梗 {before} -> {len(keep)}")

    def record_usage(self, bot_text: str) -> None:
        """她这次回复里用到了哪些梗，记一次命中。"""
        if not bot_text or not self.memes:
            return
        now = time.time()
        changed = False
        for m in self.memes:
            term = str(m.get("term") or "")
            if term and term in bot_text:
                m["hits"] = int(m.get("hits") or 0) + 1
                m["last_used"] = now
                changed = True
        if changed:
            self.save()

    def top_memes(self, n: int = 10) -> list[dict]:
        scored = sorted(self.memes, key=self._meme_weight, reverse=True)
        return [m for m in scored if self._meme_weight(m) > 0][:n]


    def refresh_state(self) -> None:
        """从磁盘重读数值状态（B站等外部写入后，让内存跟上）。"""
        try:
            if _STATE.exists():
                data = json.loads(_STATE.read_text("utf-8"))
                self.state.update({k: data[k] for k in _DEFAULT_STATE if k in data})
        except Exception:  # noqa: BLE001
            pass

    # ---------- 外部心情增量（B站看完后写进来的） ----------
    def apply_external_delta(self) -> None:
        path = r"D:\AstrBot\soul-delta.json"
        if not os.path.exists(path):
            return
        try:
            with io.open(path, encoding="utf-8") as fh:
                d = json.load(fh) or {}
        except Exception:  # noqa: BLE001
            return
        try:
            s = self.state
            s["curiosity"] = max(0.0, min(1.0, float(s.get("curiosity", 0.6)) + float(d.get("curiosity") or 0)))
            s["boredom"] = max(0.0, min(1.0, float(s.get("boredom", 0.3)) + float(d.get("boredom") or 0)))
            s["mood"] = max(0.0, min(1.0, float(s.get("mood", 0.6)) + float(d.get("mood") or 0)))
            self.save()
            logger.info(f"心智：合并外部心情增量 {d}")
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass



    # ---------- 笔记的半衰期记忆 ----------
    # 鲜活度按重要度分档：普通 30 天一衰，重要 90 天，非常重要基本不淡
    _NOTE_HALF_LIFE = {"普通": 30.0, "重要": 90.0, "非常重要": 3650.0}
    _NOTE_HALF_LIFE_DAYS = 30.0

    def _note_hl(self, n: dict) -> float:
        lv = str(n.get("level") or "普通")
        return float(self._NOTE_HALF_LIFE.get(lv, self._NOTE_HALF_LIFE_DAYS))

    def _note_weight(self, n: dict) -> float:
        now = time.time()
        base = float(n.get("w") or 1.0)
        last = float(n.get("last_hit") or n.get("ts") or now)
        age_days = max(0.0, (now - last) / 86400.0)
        return base * (0.5 ** (age_days / self._note_hl(n)))

    def decay_notes(self) -> None:
        """把太轻的笔记删掉；每次请求只跑一次，成本可忽略。"""
        now = time.time()
        before = len(self.notes)
        keep = []
        for n in self.notes:
            if str(n.get("level") or "普通") == "非常重要":
                keep.append(n)
                continue
            w = self._note_weight(n)
            ts = float(n.get("ts") or now)
            if w < 0.12 and (now - ts) > 90 * 86400:
                continue
            keep.append(n)
        if len(keep) != before:
            self.notes = keep
            self.save()

    def boost_notes(self, text: str) -> None:
        """他说的话里出现了笔记里的词，就把那条弹回来。"""
        if not text or not self.notes:
            return
        now = time.time()
        changed = False
        for n in self.notes:
            nt = str(n.get("text") or "")
            if not nt:
                continue
            chars = [c for c in nt if "\u4e00" <= c <= "\u9fff"]
            if len(chars) < 3:
                continue
            hit = any("".join(chars[i:i+3]) in text for i in range(0, min(len(chars)-2, 12)))
            if hit:
                n["w"] = min(2.0, float(n.get("w") or 1.0) + 0.5)
                n["last_hit"] = now
                changed = True
        if changed:
            self.save()

    @staticmethod
    def _grams(text: str) -> set[str]:
        """切中文二元组 + 英数词，做「证据」匹配，不依赖分词库。"""
        out: set[str] = set()
        t = str(text or "").lower()
        cjk = "".join(c if "\u4e00" <= c <= "\u9fff" else " " for c in t)
        for seg in cjk.split():
            for i in range(len(seg) - 1):
                out.add(seg[i:i + 2])
        for w in re.findall(r"[a-z0-9_]{2,}", t):
            out.add(w)
        out -= _STOP_GRAMS
        return out

    def _idf(self) -> tuple[dict[str, float], dict[str, int], int]:
        n_docs = len(self.notes)
        df: dict[str, int] = {}
        for n in self.notes:
            for g in self._grams(n.get("text") or ""):
                df[g] = df.get(g, 0) + 1
        idf = {
            g: math.log((n_docs + 1.0) / (c + 1.0)) + 1.0 for g, c in df.items()
        }
        return idf, df, n_docs

    def recall(self, query: str, k: int = 6, min_evidence: float = 1.6) -> list[dict]:
        """按他刚说的话挑记忆：命中词越特别、越新、越重要，越容易被想起来。"""
        if not query or not self.notes:
            return []
        qg = self._grams(query)
        if not qg:
            return []
        idf, df, n_docs = self._idf()
        common = max(3, int(n_docs * 0.3))  # 太常见的词不算证据
        now = time.time()
        scored = []
        for n in self.notes:
            ng = self._grams(n.get("text") or "")
            matched = [g for g in (qg & ng) if df.get(g, 0) <= common]
            if not matched:
                continue
            evidence = sum(idf.get(g, 1.0) for g in matched)
            if evidence < min_evidence:
                continue
            age = max(0.0, (now - float(n.get("ts") or now)) / 86400.0)
            fresh = 0.5 ** (age / self._note_hl(n))
            imp = min(2.0, float(n.get("w") or 1.0)) / 2.0
            scored.append((evidence * (0.6 * fresh + 0.4 * imp), n))
        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [n for _, n in scored[:k]]
        self._last_recall = [(str(n.get("text") or "")[:30], round(s, 2)) for s, n in scored[:k]]

        # 长期事实（非常重要）没被挑到也带上，最多两条
        durable = [
            n for n in self.notes
            if str(n.get("level") or "") == "非常重要" and n not in picked
        ]
        durable.sort(key=self._note_weight, reverse=True)
        picked.extend(durable[:2])

        # 一条都没命中就退回最鲜活的几条，别让她一片空白
        if not picked:
            self._last_recall = []
            picked = sorted(self.notes, key=self._note_weight, reverse=True)[:3]

        for n in picked:
            n["last_hit"] = now
            n["w"] = min(2.0, float(n.get("w") or 1.0) + 0.15)
        return picked[: max(k, 3)]

    @staticmethod
    def _fmt_note(n: dict) -> str:
        ts = float(n.get("ts") or 0)
        day = time.strftime("%m-%d", time.localtime(ts)) if ts else ""
        txt = str(n.get("text") or "").strip()
        return f"{day} {txt}".strip()

    def weighted_notes(self, k: int = 6) -> list[str]:
        scored = sorted(self.notes, key=self._note_weight, reverse=True)
        out = []
        for n in scored:
            txt = str(n.get("text") or "").strip()
            if txt:
                out.append(txt)
            if len(out) >= k:
                break
        return out

    def prompt_block(self, query: str = "") -> str:
        s = self.state
        lines = [
            "【你的当前状态 · 只影响语气，不要说出这些数字或属性】",
            f"好奇 {s['curiosity']:.2f}｜无聊 {s['boredom']:.2f}｜"
            f"精力 {s['energy']:.2f}",
        ]
        if s["boredom"] > 0.65:
            lines.append("- 你现在有点无聊，回话会更短、更爱敷衍，但对方说到有意思的会立刻来劲。")
        if s["curiosity"] > 0.7:
            lines.append("- 你现在很好奇，会忍不住多追问一句细节。")
        if s["energy"] < 0.3:
            lines.append("- 你有点累了，说话更懒，句子更短。")
        if s["mood"] > 0.75:
            lines.append("- 你心情很好，话多一点，爱开玩笑。")
        elif s["mood"] < 0.35:
            lines.append("- 你心情一般，不太想多聊，回得比较淡。")
        if int(s.get("ignored_streak", 0)) >= 2:
            lines.append("- 他已经连续几次没接你的话了，别再追问，安静点。")

        if self.memes:
            recent = self.top_memes(8)
            self._injected = {str(m.get("term") or "") for m in recent}
            items = "；".join(
                f"{m.get('term','')}={str(m.get('meaning',''))[:30]}" for m in recent
            )
            lines.append(f"【你会的梗】{items}")
            lines.append(
                "接梗原则：听懂了就顺着用、别解释梗；真想不起来什么意思，就回一句「什么梗」让对方解释。"
            )

        if self.notes:
            self.decay_notes()
            picked = self.recall(query, 6) if query else []
            if picked:
                lines.append(
                    "【你记得的事 · 按他刚说的话挑的】"
                    "（这些事里「主人」「用户」都是他；「机器人」「我」都是你自己）"
                    + "；".join(self._fmt_note(n) for n in picked)
                )
                lines.append(
                    "（没挑上的不是忘了，只是这次没想起来。自然地用，别报「我记得…」的清单。）"
                )
            else:
                recent_notes = self.weighted_notes(4)
                if recent_notes:
                    lines.append("【你记得的事】" + "；".join(recent_notes))
        return "\n".join(lines)


_SOUL: Soul | None = None


def _get_soul() -> Soul:
    global _SOUL
    if _SOUL is None:
        _SOUL = Soul()
    return _SOUL


def _deepseek_targets() -> tuple[str, str, str]:
    """从 AstrBot 配置里取 DeepSeek 的 key / api_base / model。"""
    try:
        cfg_path = Path.cwd() / "data" / "cmd_config.json"
        cfg = json.loads(cfg_path.read_text("utf-8-sig"))
        sources = {s.get("id"): s for s in cfg.get("provider_sources", [])}
        for provider in cfg.get("provider", []):
            src = sources.get(provider.get("provider_source_id")) or {}
            api_base = src.get("api_base") or ""
            keys = src.get("key") or []
            if api_base and keys and "deepseek" in api_base:
                return api_base.rstrip("/"), keys[0], provider.get("model") or "deepseek-chat"
    except Exception:  # noqa: BLE001
        pass
    return "", "", ""


async def _extract(user_text: str, bot_text: str) -> dict:
    api_base, key, model = _deepseek_targets()
    if not api_base or not key:
        return {}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LEARN_PROMPT},
            {"role": "user", "content": f"用户：{user_text}\n机器人：{bot_text}"},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            ) as resp:
                data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"心智学习调用失败: {exc}")
        return {}


@register(
    "dafeiyu_soul",
    "dafeiyu",
    "情绪状态 + 长期记忆 + 梗库，让机器人会好奇、会无聊、会接梗",
    "1.0.0",
)
class DafeiyuSoul(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.soul = _get_soul()

    # ---------- 每次请求注入状态 ----------
    @filter.on_llm_request()
    async def inject_soul(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            # 先重读磁盘（B站等外部可能改过），再做本次的状态更新，避免互相覆盖
            self.soul.refresh_state()
            text = (event.message_str or "").strip()
            if text:
                self.soul.boost_notes(text)
                self.soul.on_message(text)
                self.soul.save()
            self.soul.apply_external_delta()
            block = self.soul.prompt_block(text)
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        except Exception:  # noqa: BLE001
            logger.warning("注入心智状态失败", exc_info=True)

    # ---------- 回复后：记账 + 学习 ----------
    @filter.on_llm_response()
    async def after_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        try:
            self.soul.on_reply()
            self.soul.save()
            bot_text0 = (getattr(response, "completion_text", "") or "").strip()
            self.soul.record_usage(bot_text0)
            self.soul.prune_memes()
        except Exception:  # noqa: BLE001
            logger.debug("更新回复时间失败", exc_info=True)

        now = time.time()
        if now - self.soul.last_learn_ts < 45:
            return
        user_text = (event.message_str or "").strip()
        bot_text = (getattr(response, "completion_text", "") or "").strip()
        if len(user_text) < 4 or not bot_text:
            return

        # ---- 攒批：攒够 8 条或空闲 10 分钟再学，省 token ----
        buf = getattr(self, "_learn_buf", None)
        if buf is None:
            buf = self._learn_buf = []
        buf.append((user_text, bot_text))
        if len(buf) < 8:
            if getattr(self, "_learn_timer", None) is None or self._learn_timer.done():
                self._learn_timer = asyncio.create_task(self._flush_later())
            return
        batch = buf[:]
        self._learn_buf = []
        user_text = "\n".join(f"用户：{u}\n机器人：{b}" for u, b in batch)[-3000:]
        bot_text = "（合并批次）"

        async def _learn() -> None:
            result = await _extract(user_text, bot_text)
            if not isinstance(result, dict):
                return
            changed = False
            for meme in result.get("memes") or []:
                term = str(meme.get("term") or "").strip()
                meaning = str(meme.get("meaning") or "").strip()
                if not term or not meaning:
                    continue
                if not _is_real_meme(term):
                    continue
                if any(m.get("term") == term for m in self.soul.memes):
                    continue
                _now = time.time()
                self.soul.memes.append(
                    {
                        "term": term,
                        "meaning": meaning,
                        "example": meme.get("example", ""),
                        "added": _now,
                        "last_used": _now,
                        "hits": 0,
                    }
                )
                changed = True
            for note in result.get("notes") or []:
                if isinstance(note, dict):
                    note_text = str(note.get("text") or "").strip()
                    level = str(note.get("level") or "普通").strip()
                else:
                    note_text, level = str(note).strip(), "普通"
                if level not in ("普通", "重要", "非常重要"):
                    level = "普通"
                if not note_text or len(note_text) > 120:
                    continue
                if any(n.get("text") == note_text for n in self.soul.notes):
                    continue
                self.soul.notes.append(
                    {"ts": time.time(), "kind": "note", "text": note_text, "level": level}
                )
                changed = True
            if len(self.soul.memes) > 200:
                self.soul.memes = self.soul.memes[-200:]
            if len(self.soul.notes) > 200:
                self.soul.notes = self.soul.notes[-200:]
            if changed:
                self.soul.save()
                logger.info(
                    f"心智学习：梗 {len(self.soul.memes)} 条 / 笔记 {len(self.soul.notes)} 条"
                )

        self.soul.last_learn_ts = now
        asyncio.create_task(_learn())


    async def _flush_later(self):
        """空闲 10 分钟后把攒的这几条一次学掉。"""
        await asyncio.sleep(600)
        buf = getattr(self, "_learn_buf", None) or []
        if not buf:
            return
        self._learn_buf = []
        text = "\n".join(f"用户：{u}\n机器人：{b}" for u, b in buf)[-3000:]
        try:
            result = await _extract(text, "（合并批次）")
            if isinstance(result, dict):
                self._apply_learn(result)
                logger.info(f"心智学习（攒批 {len(buf)} 条）")
        except Exception:  # noqa: BLE001
            logger.debug("flush learn failed", exc_info=True)

    def _apply_learn(self, result: dict) -> None:
        changed = False
        for meme in result.get("memes") or []:
            term = str(meme.get("term") or "").strip()
            meaning = str(meme.get("meaning") or "").strip()
            if not term or not meaning or not _is_real_meme(term):
                continue
            if any(m.get("term") == term for m in self.soul.memes):
                continue
            _now = time.time()
            self.soul.memes.append({"term": term, "meaning": meaning,
                                    "example": meme.get("example", ""),
                                    "added": _now, "last_used": _now, "hits": 0})
            changed = True
        for note in result.get("notes") or []:
            note = str(note).strip()
            if not note or len(note) > 120:
                continue
            if any(n.get("text") == note for n in self.soul.notes):
                continue
            self.soul.notes.append({"ts": time.time(), "kind": "note", "text": note})
            changed = True
        if len(self.soul.memes) > 200:
            self.soul.memes = self.soul.memes[-200:]
        if len(self.soul.notes) > 200:
            self.soul.notes = self.soul.notes[-200:]
        if changed:
            self.soul.save()

    # ---------- 工具 ----------
    @llm_tool(name="learn_meme")
    async def learn_meme(self, event: AstrMessageEvent, term: str, meaning: str) -> str:
        """把用户教给你的新梗记进梗库，以后可以拿来接梗。

        Args:
            term(string): 梗本身，比如「栓Q」「典」
            meaning(string): 这个梗是什么意思、什么场合用
        """
        term = (term or "").strip()
        meaning = (meaning or "").strip()
        if not term or not meaning:
            return "没听清，梗和意思都要有。"
        for meme in self.soul.memes:
            if meme.get("term") == term:
                meme["meaning"] = meaning
                self.soul.save()
                return f"「{term}」我更新了。"
        self.soul.memes.append({"term": term, "meaning": meaning, "example": ""})
        self.soul.save()
        return f"学会了，「{term}」是吧。"

    @llm_tool(name="remember_note")
    async def remember_note(
        self, event: AstrMessageEvent, text: str, level: str = "普通"
    ) -> str:
        """记下关于用户的一件小事，以后聊天可以用上。

        Args:
            text(string): 要记住的事实，一句话，比如「他周三下午有课」
            level(string): 重要度，普通 / 重要 / 非常重要（约定、喜好、长期不变的事实用「非常重要」）
        """
        text = (text or "").strip()
        if not text or len(text) > 120:
            return "这句太长或者空了，记不住。"
        level = (level or "普通").strip()
        if level not in ("普通", "重要", "非常重要"):
            level = "普通"
        self.soul.notes.append(
            {"ts": time.time(), "kind": "note", "text": text, "level": level}
        )
        self.soul.notes = self.soul.notes[-200:]
        self.soul.save()
        return "记住了。"

    # ---------- 命令 ----------

    @filter.command("soul")
    async def soul_cmd(self, event: AstrMessageEvent):
        """看她学了哪些梗（按用到次数排序）。"""
        memes = self.soul.top_memes(12)
        if not memes:
            yield event.plain_result("还没学到梗。")
            return
        lines = [
            f"{m.get('term','')} ×{int(m.get('hits') or 0)} —— {str(m.get('meaning',''))[:20]}"
            for m in memes
        ]
        yield event.plain_result("还记得的梗：\n" + "\n".join(lines))

    @filter.command("笔记")
    async def notes_cmd(self, event: AstrMessageEvent):
        """看她记了关于你的哪些事，以及上一次她想起了哪几条。"""
        notes = [n.get("text", "") for n in self.soul.notes[-10:] if n.get("text")]
        if not notes:
            yield event.plain_result("还没记什么。")
            return
        out = "关于你：\n" + "\n".join(f"- {x[:40]}" for x in notes)
        last = getattr(self.soul, "_last_recall", None)
        if last:
            out += "\n\n上一次她想起的是：\n" + "\n".join(
                f"- {t}（{s}）" for t, s in last
            )
        yield event.plain_result(out)

    @filter.command("忘掉")
    async def forget_cmd(self, event: AstrMessageEvent, term: str = ""):
        """忘掉一条梗：/忘掉 yyds"""
        key = term.strip()
        if not key:
            yield event.plain_result("要忘掉哪个？")
            return
        before = len(self.soul.memes)
        self.soul.memes = [m for m in self.soul.memes if str(m.get("term") or "") != key]
        if len(self.soul.memes) == before:
            yield event.plain_result(f"没找到「{key}」。")
            return
        self.soul.save()
        yield event.plain_result(f"忘掉「{key}」了。")

    @filter.command("心情")
    async def mood_command(self, event: AstrMessageEvent):
        """看看它现在的心情状态。"""
        self.soul.refresh_state()
        self.soul.decay()
        self.soul.save()
        s = self.soul.state
        label = ""
        try:
            hp = Path(r"D:\AstrBot\bot\data\plugin_data\heart\state.json")
            if hp.exists():
                label = json.loads(hp.read_text("utf-8")).get("mood") or ""
        except Exception:  # noqa: BLE001
            label = ""
        mood_text = f"{label}（{s['mood']:.2f}）" if label else f"{s['mood']:.2f}"
        text = (
            f"好奇 {s['curiosity']:.2f}｜无聊 {s['boredom']:.2f}｜"
            f"精力 {s['energy']:.2f}｜心情 {mood_text}\n"
            f"聊过 {s.get('msg_count', 0)} 句，攒了 {len(self.soul.memes)} 个梗、"
            f"{len(self.soul.notes)} 条记忆"
        )
        yield event.plain_result(text)

    # ---------- 从 B 站热评挖梗 ----------
    def _miner(self):
        if getattr(self, "_miner_mod", None) is None:
            import importlib.util

            path = Path(__file__).with_name("bili_miner.py")
            spec = importlib.util.spec_from_file_location("dafeiyu_bili_miner", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._miner_mod = module
        return self._miner_mod

    @filter.on_astrbot_loaded()
    async def start_miner(self):
        try:
            module = self._miner()
            asyncio.create_task(module.background_loop(self.soul))
            logger.info("[挖梗] 后台任务已启动（每 8 小时一轮）")
        except Exception:  # noqa: BLE001
            logger.warning("[挖梗] 启动失败", exc_info=True)

    @filter.command("挖梗")
    async def mine_command(self, event: AstrMessageEvent):
        """立刻去 B 站热评挖一轮梗。"""
        try:
            module = self._miner()
            yield event.plain_result("去看看 B 站最近在玩什么梗…")
            added = await module.mine_once(self.soul)
            if added:
                yield event.plain_result(
                    f"挖到 {added} 个新梗，现在一共 {len(self.soul.memes)} 个。"
                )
            else:
                yield event.plain_result("这轮没挖到新梗，可能都是会的。")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"挖梗失败：{exc}")
