"""A.I.R Z：上下文装配器 + 记忆模糊化。

解决两个问题：
  1. 长对话里历史无限增长，注意力被稀释 —— 每次请求重新装配上下文：
     人格(已有 system_prompt) + 长期记忆(由 soul 注入) + 更早对话摘要 + 最近 12 轮原文
  2. 太旧的记忆要么被硬删、要么原样塞给模型 —— 给快淡忘的记忆加「模糊化」：
     权重掉到 0.1 以下时只给前 26 个字，标成碎片

装配发生在 on_llm_request 的末尾（priority 很低，排在所有注入之后），
所以它统计到的 token 数包含了 soul / heart / 人格卡的完整注入量。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

_DATA = Path(r"D:\AstrBot\bot\data\plugin_data\airz")
_STATE = _DATA / "state.json"
_ARCHIVE = _DATA / "archive.jsonl"
_SOUL_NOTES = Path(r"D:\AstrBot\bot\data\plugin_data\dafeiyu_soul\notes.jsonl")

# ---- 可调参数 ----
MAX_TURNS = 12          # 最近保留多少轮原文（一轮 = 一问一答）
SUMMARY_CHARS = 1200    # 摘要上限（字）
TOKEN_WARN = 10000      # 注入超过这个 token 数就告警
FUZZY_PREFIX = 26       # 模糊记忆只留前几个字
FUZZY_MAX = 4           # 一次最多塞几条碎片

_HALF_LIFE = {"普通": 30.0, "重要": 90.0, "非常重要": 3650.0}


def _tokens(text: str) -> int:
    """粗估 token：中文约 0.75 token/字，英文数字约 1 token/3.5 字符。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * 0.75 + other / 3.5) + 1


def _text_of(content) -> str:
    """历史里的 content 可能是字符串，也可能是分段列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") == "text":
                out.append(str(seg.get("text") or ""))
            elif isinstance(seg, str):
                out.append(seg)
        return "".join(out)
    return str(content or "")


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    except Exception:  # noqa: BLE001
        logger.debug("airz: 保存状态失败", exc_info=True)


def _note_weight(n: dict) -> float:
    now = time.time()
    base = float(n.get("w") or 1.0)
    last = float(n.get("last_hit") or n.get("ts") or now)
    hl = _HALF_LIFE.get(str(n.get("level") or "普通"), 30.0)
    age_days = max(0.0, (now - last) / 86400.0)
    return base * (0.5 ** (age_days / hl))


def _archive(messages: list) -> bool:
    """压缩前把原文存进「全量本」。只搬不删，翻旧账靠它。"""
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with open(_ARCHIVE, "a", encoding="utf-8") as fh:
            for m in messages:
                if not isinstance(m, dict):
                    continue
                fh.write(
                    json.dumps(
                        {"ts": now, "role": m.get("role"), "content": m.get("content")},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return True
    except Exception:  # noqa: BLE001
        logger.warning("airz: 归档写入失败", exc_info=True)
        return False


def _fuzzy_notes(limit: int = FUZZY_MAX) -> list[str]:
    """挑出「快忘了但还没删」的记忆，只给开头几个字。"""
    try:
        rows = [
            json.loads(line)
            for line in _SOUL_NOTES.read_text("utf-8").splitlines()
            if line.strip()
        ]
    except Exception:  # noqa: BLE001
        return []
    picked = []
    for n in rows:
        w = _note_weight(n)
        if not (0.02 <= w < 0.1):
            continue
        text = str(n.get("text") or "").strip()
        if len(text) <= FUZZY_PREFIX:
            continue
        picked.append((w, text))
    picked.sort(key=lambda x: x[0], reverse=True)
    return [f"{t[:FUZZY_PREFIX]}…" for _, t in picked[:limit]]


@register(
    "airz",
    "dafeiyu",
    "A.I.R Z 上下文装配器：摘要压缩 + 最近 12 轮 + 记忆模糊化 + 提示词体积审计",
    "1.0.0",
)
class Airz(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    # 优先级 -300：排在 soul(-140) / heart(-140) 之后，能统计到它们的注入
    @filter.on_llm_request(priority=-300)
    async def assemble(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            if event.get_group_id():
                return  # 群聊由 group_chat_plus 管上下文，这里不插手
            umo = event.unified_msg_origin
            got = await self._load(umo, str(event.get_sender_id()))
            if not got:
                return
            cid, history = got
            state = _load_state()
            if state.get("cid") != cid:
                state = {"cid": cid, "watermark": 0, "summary": ""}

            keep = MAX_TURNS * 2
            watermark = int(state.get("watermark") or 0)
            summary = str(state.get("summary") or "")
            if watermark > len(history):
                # 历史被压短了（压缩后管线只存了窗口内的）：水位归零，但摘要必须留着
                watermark = 0
                state.update({"watermark": 0})
                _save_state(state)

            # 超过 12 轮：先把要丢的原文归档，再压成摘要
            if len(history) - watermark > keep:
                old = history[watermark: len(history) - keep]
                if old and _archive(old):
                    summary = await self._summarize(summary, old, umo)
                    watermark = len(history) - keep
                    state.update({"watermark": watermark, "summary": summary})
                    _save_state(state)
                    logger.info(
                        f"airz: 归档并压缩 {len(old)} 条旧对话 -> 摘要 {len(summary)} 字"
                    )
                elif old:
                    logger.warning("airz: 归档失败，这次不压缩（不能只删不存）")

            # 重新装配：摘要进 system_prompt，原文只留最近 12 轮
            recent = history[watermark:]
            # 原来的上下文里可能有文件解析结果这类 system 消息，别弄丢
            keep_sys = [
                m for m in (req.contexts or [])
                if isinstance(m, dict) and m.get("role") == "system"
            ]
            turns = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in recent
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant", "system")
                and m.get("content") is not None
            ]
            req.contexts = keep_sys + turns
            add = []
            if summary:
                add.append(f"【更早的对话摘要】\n{summary[:SUMMARY_CHARS]}")
            # 已经被 soul 召回、写进提示词的，就别再重复塞一遍
            fuzzy = [x for x in _fuzzy_notes() if x[:8] not in (req.system_prompt or "")]
            if fuzzy:
                add.append(
                    "【模糊的记忆碎片】（记不太清了，别当成确定的事）\n"
                    + "\n".join(f"- {x}" for x in fuzzy)
                )
            if add:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + "\n\n".join(add)

            self._audit(req, len(recent))
        except Exception:  # noqa: BLE001
            logger.warning("airz: 装配上下文失败", exc_info=True)

    # ---------- 内部 ----------
    def _audit(self, req: ProviderRequest, n_msgs: int) -> None:
        sys_tokens = _tokens(req.system_prompt or "")
        ctx_tokens = sum(
            _tokens(_text_of(m.get("content"))) for m in (req.contexts or [])
        )
        total = sys_tokens + ctx_tokens
        if total > TOKEN_WARN:
            logger.warning(
                f"警告：人格卡过胖，挤占当前对话注意力！"
                f"注入合计约 {total} token（系统提示 {sys_tokens} + 对话 {ctx_tokens}），"
                f"阈值 {TOKEN_WARN}。"
            )
        else:
            logger.debug(
                f"airz: 上下文约 {total} token（系统 {sys_tokens} + {n_msgs} 条历史 {ctx_tokens}）"
            )

    async def _load(self, umo: str, sender: str):
        cm = self.context.conversation_manager
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await cm.get_conversation(umo, cid)
        if not conv:
            return None
        try:
            history = json.loads(conv.history or "[]")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(history, list):
            return None
        return cid, history

    async def _summarize(self, old_summary: str, messages: list, umo: str = "") -> str:
        lines = []
        for m in messages:
            role = "他" if m.get("role") == "user" else "我"
            text = _text_of(m.get("content")).strip()
            if text:
                lines.append(f"{role}：{text[:200]}")
        chat = "\n".join(lines)[-4000:]
        if not chat:
            return old_summary
        prompt = (
            "把下面这段更早的对话压成摘要。要求：\n"
            "1. 保留发生了什么、他的状态和情绪、约定、没说完的事；\n"
            "2. 用「他」和「我」，别用「用户」「机器人」；\n"
            "3. 不超过 400 字，不用列表，不评论。\n"
        )
        if old_summary:
            prompt += f"\n已经有一段更早的摘要：\n{old_summary[:600]}\n\n把新旧合并成一段。\n"
        prompt += f"\n对话：\n{chat}"
        try:
            provider = await self.context.get_using_provider_async(umo or None)
            if not provider:
                return old_summary
            resp = await provider.text_chat(prompt=prompt)
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text[:SUMMARY_CHARS] or old_summary
        except Exception:  # noqa: BLE001
            logger.debug("airz: 摘要失败", exc_info=True)
            return old_summary

    # ---------- 看看现在装配成什么样 ----------
    @filter.command("上下文")
    async def ctx_cmd(self, event: AstrMessageEvent):
        """看当前上下文装配情况。"""
        umo = event.unified_msg_origin
        got = await self._load(umo, str(event.get_sender_id()))
        if not got:
            yield event.plain_result("还没有对话记录。")
            return
        cid, history = got
        state = _load_state()
        if state.get("cid") != cid:
            state = {"watermark": 0, "summary": ""}
        keep = MAX_TURNS * 2
        wm = int(state.get("watermark") or 0)
        recent = history[wm:]
        fuzzy = _fuzzy_notes()
        yield event.plain_result(
            f"总历史 {len(history)} 条\n"
            f"装配：摘要 {len(state.get('summary') or '')} 字 + 最近 {len(recent)} 条"
            f"（上限 {keep}）\n"
            f"模糊碎片 {len(fuzzy)} 条"
            + (f"\n下次再聊 {max(0, keep - len(recent)) + 1} 条触发压缩" if len(recent) <= keep else "")
        )
