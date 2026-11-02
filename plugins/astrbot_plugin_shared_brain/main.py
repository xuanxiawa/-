"""私聊 / 群聊 共享同一份近期记忆。

问题
----
AstrBot 的对话是按会话存的：私聊一个会话，群聊另一个会话。结果她在群里
刚聊完的事，私聊里像没发生过；私聊里答应的约定，群里又想不起来。
长期笔记（soul 的 notes）本来就是全局的，缺的是「最近聊了什么」。

做法
----
1. 旁听：每收到一条 QQ 消息、每发出一条回复，都往
   ``plugin_data/shared_brain/messages.jsonl`` 记一行（频道 / 谁 / 什么时候 / 说了啥）。
2. 注入：每次模型请求前，把「另一个频道」最近 3 小时里的最多 8 句，
   拼成一小块塞进 system_prompt。system_prompt 不进对话历史，所以不会越滚越多。

频道按会话分开记：私聊是 ``private``，群聊是 ``group:<群号>``。
私聊能读到所有群，群只能读到私聊——A 群的话不会漏进 B 群。

群里的内容进私聊是自然的；私聊内容进群会明确标注「别主动说出去」，
免得她当着群友的面把私聊里的话抖出来。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# 往用户消息尾部挂临时块用；mark_as_temp 的内容不会进对话历史
try:
    from astrbot.core.agent.message import TextPart as _TextPart
except Exception:  # noqa: BLE001
    _TextPart = None

try:
    from astrbot.core.message.message_event_result import ResultContentType as _RCT
except Exception:  # noqa: BLE001
    _RCT = None

_LOG = Path(r"D:\AstrBot\bot\data\plugin_data\shared_brain\messages.jsonl")
_CFG = Path(r"D:\AstrBot\bot\data\cmd_config.json")

# 注入预算：和 soul / heart 共用同一本账（模块在 soul 插件目录里）
try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "dafeiyu_prompt_budget",
        Path(__file__).resolve().parent.parent
        / "astrbot_plugin_dafeiyu_soul"
        / "prompt_budget.py",
    )
    _budget = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_budget)
except Exception:  # noqa: BLE001
    _budget = None
    logger.warning("shared_brain: 注入预算模块没加载上，不做总量约束", exc_info=True)

PLATFORM_ID = "qq_dafeiyu"
OWNER_QQ_FALLBACK = "3448098168"

WINDOW_SECONDS = 3 * 60 * 60  # 3 小时：隔几小时回来聊也接得上
MAX_LINES = 8
MAX_CHARS = 100
KEEP_LINES = 1200

_LEAD = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}\s*周[一二三四五六日]\s*\d{2}:\d{2}:\d{2}\]\s*"
    r"[^:\n]{1,40}?\(ID:\d+\)[^:\n]{0,80}?:\s*"
)
_MARKERS = (
    re.compile(r"<system_reminder>.*?</system_reminder>", re.S),
    re.compile(r"<Quoted Message>.*?</Quoted Message>", re.S),
    re.compile(r"<image_caption>.*?</image_caption>", re.S),
    re.compile(r"\[图片内容:.*?\]", re.S),
    re.compile(r"\[引用\s*>>>.*?\](?=\s|$)", re.S),
)


def _clean(text: str) -> str:
    text = text or ""
    for pat in _MARKERS:
        text = pat.sub("", text)
    text = _LEAD.sub("", text)
    text = re.sub(r"\[At:[^\]]*\]", "", text)
    text = re.sub(r"\[表情:[^\]]*\]", "", text)
    text = re.sub(r"【[^】]{0,30}】", "", text)
    text = re.sub(r"\[图片\]|\[图片内容[^\]]*\]", "[图片]", text)
    text = re.sub(r"\s+", " ", text).strip(" :：")
    return text


# ---------- 频道 ----------
def _is_group(channel: str) -> bool:
    return channel == "group" or channel.startswith("group:")


def _channel_of(event: AstrMessageEvent) -> str:
    gid = str(event.get_group_id() or "")
    return f"group:{gid}" if gid else "private"


def _channel_of_umo(umo: str) -> str:
    if ":GroupMessage:" in umo:
        return f"group:{umo.rsplit(':', 1)[-1]}"
    if ":FriendMessage:" in umo:
        return "private"
    return ""


# ---------- 主人 ----------
_OWNERS: dict[str, object] = {"ts": 0.0, "ids": set()}


def _owners() -> set[str]:
    """管理员 QQ 集合；60 秒缓存，换/加管理员不用重启。"""
    now = time.time()
    ids = _OWNERS.get("ids") or set()
    if now - float(_OWNERS.get("ts") or 0) < 60 and ids:
        return ids  # type: ignore[return-value]
    found: set[str] = set()
    try:
        cfg = json.loads(_CFG.read_text("utf-8-sig"))
        for adm in cfg.get("admins_id") or []:
            s = str(adm).strip()
            if not s:
                continue
            found.add(s)
            if ":" in s:  # 兼容 "qq:123456" 这种写法
                found.add(s.rsplit(":", 1)[-1])
    except Exception:  # noqa: BLE001
        pass
    if not found:
        found = {OWNER_QQ_FALLBACK}
    _OWNERS["ts"] = now
    _OWNERS["ids"] = found
    return found


@register(
    "shared_brain",
    "dafeiyu",
    "私聊和群聊共享近期记忆：两个窗口都能读到对方刚聊的事（含她自己说的话）",
    "1.1.0",
)
class SharedBrain(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._lock = asyncio.Lock()
        self._appends = 0
        self._last: tuple[str, str, float] | None = None
        self._patched = False
        self._trim()

    # ---------- 记日志 ----------
    def _trim(self) -> None:
        try:
            if not _LOG.exists():
                return
            lines = _LOG.read_text("utf-8").splitlines()
            if len(lines) > KEEP_LINES:
                tmp = _LOG.with_suffix(".tmp")
                tmp.write_text("\n".join(lines[-KEEP_LINES:]) + "\n", "utf-8")
                os.replace(tmp, _LOG)
        except Exception:  # noqa: BLE001
            logger.debug("shared_brain: 裁剪日志失败", exc_info=True)

    def _append(self, channel: str, sender: str, text: str) -> None:
        text = _clean(text)
        if not text:
            return
        now = time.time()
        # 分段回复 + send_message 补记可能把同一句话记两遍
        if (
            self._last
            and self._last[0] == channel
            and self._last[1] == text
            and now - self._last[2] < 3
        ):
            return
        self._last = (channel, text, now)
        rec = {
            "ts": now,
            "ch": channel,
            "who": (sender or "?")[:24],
            "text": text[:MAX_CHARS],
        }
        try:
            _LOG.parent.mkdir(parents=True, exist_ok=True)
            with _LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._appends += 1
            if self._appends % 60 == 0:
                self._trim()
        except Exception:  # noqa: BLE001
            logger.debug("shared_brain: 写日志失败", exc_info=True)

    # ---------- 接管主动发送 ----------
    @filter.on_astrbot_loaded()
    async def start(self):
        """group_chat_plus 的回复走 context.send_message，不触发 after_message_sent。

        history_bridge 已经包过一层，我们接着包，调用链不会断。
        """
        if self._patched:
            return
        try:
            self._orig_send = self.context.send_message
            brain = self

            async def send_message(session, message_chain):
                ok = await brain._orig_send(
                    session=session, message_chain=message_chain
                )
                try:
                    umo = str(session)
                    if PLATFORM_ID in umo:
                        channel = _channel_of_umo(umo)
                        text = (message_chain.get_plain_text() or "").strip()
                        if channel and text:
                            brain._append(channel, "你", text)
                except Exception:  # noqa: BLE001
                    logger.debug("shared_brain: 记录主动消息失败", exc_info=True)
                return ok

            self.context.send_message = send_message
            self._patched = True
            logger.info("shared_brain: 已接管 send_message")
        except Exception:  # noqa: BLE001
            logger.warning("shared_brain: 接管 send_message 失败", exc_info=True)

    # ---------- 旁听 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def hear(self, event: AstrMessageEvent):
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            sender = str(event.get_sender_id() or "")
            if not sender or sender == str(event.get_self_id() or ""):
                return
            channel = _channel_of(event)
            if channel == "private" and sender not in _owners():
                return
            self._append(
                channel,
                event.get_sender_name() or sender,
                event.get_message_str() or "",
            )
        except Exception:  # noqa: BLE001
            logger.debug("shared_brain: 记录消息失败", exc_info=True)

    @staticmethod
    def _result_text(res) -> str:
        """从 event.result 里取她这次说出口的话。

        注意 res.chain 是 list[组件]，不是 MessageChain，没有 get_plain_text()；
        正确取法是 MessageEventResult 自己的 get_plain_text()。
        """
        try:
            text = (res.get_plain_text() or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            return text
        try:
            chain = getattr(res, "chain", None) or []
            if any("Image" in str(getattr(c, "type", "")) for c in chain):
                return "[图片]"
        except Exception:  # noqa: BLE001
            pass
        return ""

    @filter.after_message_sent()
    async def spoken(self, event: AstrMessageEvent):
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            res = event.get_result()
            if not res:
                return
            text = self._result_text(res)
            if not text:
                return
            # 命令类回复（/心情 这种诊断长文）不是她说的话，别写进共享记忆
            if (
                _RCT is not None
                and len(text) > 120
                and getattr(res, "result_content_type", None) != _RCT.LLM_RESULT
            ):
                return
            channel = _channel_of(event)
            if channel == "private" and str(event.get_sender_id()) not in _owners():
                return
            self._append(channel, "你", text)
        except Exception:  # noqa: BLE001
            logger.debug("shared_brain: 记录回复失败", exc_info=True)

    # ---------- 读取另一边 ----------
    @staticmethod
    def _recent(pred, limit: int = MAX_LINES) -> list[dict]:
        try:
            raw = _LOG.read_text("utf-8").splitlines()[-KEEP_LINES:]
        except FileNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            return []
        now = time.time()
        out: list[dict] = []
        for line in raw:
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not pred(str(rec.get("ch") or "")):
                continue
            if now - float(rec.get("ts") or 0) > WINDOW_SECONDS:
                continue
            if not rec.get("text"):
                continue
            out.append(rec)
        return out[-limit:]

    @filter.on_llm_request(priority=-110)
    async def share(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            if event.get_platform_id() != PLATFORM_ID:
                return
            channel = _channel_of(event)
            if channel == "private" and str(event.get_sender_id()) not in _owners():
                return

            if _is_group(channel):
                # 群里只看私聊；别的群的话不外泄
                lines = self._recent(lambda ch: ch == "private")
                header = (
                    "【共享记忆 · 私聊里刚刚聊的】"
                    "（只当背景；老大没在群里主动提起，就别把私聊内容说给群友）"
                )
            else:
                # 私聊能读到所有群
                lines = self._recent(_is_group)
                header = (
                    "【共享记忆 · 群里刚刚聊的】"
                    "（你在群里也在场；他提起就接得上，别主动硬把话题拐过去）"
                )
            if not lines:
                return

            body = "\n".join(
                f"{time.strftime('%H:%M', time.localtime(float(r['ts'])))} "
                f"{r.get('who') or '?'}: {r.get('text')}"
                for r in lines
            )
            seen = (req.system_prompt or "") + (
                req.prompt if isinstance(req.prompt, str) else ""
            )
            for part in getattr(req, "extra_user_content_parts", None) or []:
                seen += getattr(part, "text", "") or ""
            if header in seen:
                return
            block = f"{header}\n{body}"
            if _budget is not None:
                block = _budget.offer(req, "shared", block, audit_dir=_LOG.parent)
            if not block:
                return
            # 挂用户消息尾部（别动系统提示这个缓存前缀），但要 mark_as_temp：
            # 框架会把「发给模型的消息」原样存进对话历史，直接写 req.prompt
            # 会让这段共享记忆永久留在历史里，越滚越大。
            if _TextPart is not None:
                req.extra_user_content_parts.append(
                    _TextPart(text=block).mark_as_temp()
                )
            else:  # 老版本 AstrBot 没有 extra_user_content_parts 时的退路
                base = req.prompt if isinstance(req.prompt, str) and req.prompt else ""
                req.prompt = f"{base}\n\n{block}" if base else block
        except Exception:  # noqa: BLE001
            logger.warning("shared_brain: 注入共享记忆失败", exc_info=True)
