"""发送前：把模型漏出来的「工具调用」标记清掉，能救的顺手真的执行掉。

背景：有些调用路径下，模型不老老实实走原生函数调用，而是把工具调用当正文写：
    <tool_call>{"name":"render_meme","arguments":"{\\"template\\":\\"举牌\\",...}"}</tool_call>
AstrBot 会把它当普通文字发出去（用户看到一堆 JSON），更糟的是**工具其实没执行**，
于是「答应了要做图，图却没出来」。

这个插件在 result 装饰阶段（发送前）做两件事：
  1. 把这些标记从正文里抠掉；
  2. 如果名字在白名单里（做图/发表情这类无副作用或本来就要发东西的工具），
     就解析参数、真的调一次工具——图就发出去了。

白名单之外的工具（发说说、改昵称、戳一戳……）只清文字、不补执行：
模型幻觉出一个写操作就去真的执行，风险太大。
"""

from __future__ import annotations

import asyncio
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

_OPEN = re.compile(r"<\s*tool_call\s*>", re.I)
_CLOSE = re.compile(r"</\s*(?:tool_call|function_call)\s*>", re.I)
_LEFTOVER = re.compile(r"<\s*/?\s*(?:tool_call|function_call)\s*>", re.I)
# DeepSeek 漏出来的内部标记：</｜｜DSML｜｜ invoke> 之类
_DSML = re.compile(r"<[^<>\n]*?DSML[^<>\n]*>", re.I)
# DSML 的调用块开头：<｜｜DSML｜｜ invoke name="render_meme">（也可能叫 tool_call）
_DSML_OPEN = re.compile(
    r"<\s*[^\s<>]*DSML[^\s<>]*\s*(?:invoke|tool_call|tool_calls)\b([^<>]*)>", re.I
)
_DSML_CLOSE = re.compile(
    r"</\s*[^\s<>]*DSML[^\s<>]*\s*(?:invoke|tool_call|tool_calls)\s*>", re.I
)
# <｜｜DSML｜｜ parameter name="template">举牌</｜｜DSML｜｜ parameter>
_DSML_PARAM = re.compile(
    r"<\s*[^\s<>]*DSML[^\s<>]*\s*parameter\b([^<>]*)>(.*?)"
    r"</\s*[^\s<>]*DSML[^\s<>]*\s*parameter\s*>",
    re.I | re.S,
)
_ATTR_NAME = re.compile(r"""name\s*=\s*["']([^"']+)["']""", re.I)
_LOOSE = re.compile(
    r'\n?\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"(?:arguments|args|parameters)"\s*:.*$', re.S
)

# 只补执行这些：要么产出图片直接发给用户，要么本身就是「发东西」
_SALVAGE = {"render_meme", "send_sticker", "save_sticker"}
_MAX_CALLS = 2  # 一条消息最多补执行几次，防手滑


def _in_code_block(text: str, pos: int) -> bool:
    """这段是不是在 ``` 代码块里（在讲代码/讲格式的时候别动手）。"""
    try:
        return text.count("```", 0, pos) % 2 == 1
    except Exception:  # noqa: BLE001
        return False


def _norm_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        for _ in range(2):  # 常见是双重编码："{\"text\": ...}"
            try:
                raw = json.loads(raw)
            except Exception:  # noqa: BLE001
                return {}
            if isinstance(raw, dict):
                return raw
        return {}
    return {}


def _extract(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """抠掉工具调用标记，返回（清理后的正文, [(工具名, 参数)]）。"""
    calls: list[tuple[str, dict]] = []
    spans: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()

    for m in _OPEN.finditer(text):
        start = m.start()
        if _in_code_block(text, start):
            continue
        rest = text[m.end():]
        brace = rest.find("{")
        if brace < 0:
            spans.append((start, m.end()))  # 只有开始标签，标签本身也删掉
            continue
        try:
            obj, end = decoder.raw_decode(rest, brace)
        except Exception:  # noqa: BLE001
            spans.append((start, m.end()))
            continue
        end_abs = m.end() + end
        # 后面可能跟着 </tool_call>，也可能是一串 DSML 残片
        tail = text[end_abs:end_abs + 300]
        cut = end_abs
        cm = _CLOSE.search(tail)
        if cm:
            cut = end_abs + cm.end()
        else:
            dm = _DSML.search(tail)
            if dm and dm.start() < 40:
                cut = end_abs + dm.end()
        spans.append((start, cut))

        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = _norm_args(
                obj.get("arguments", obj.get("args", obj.get("parameters", {})))
            )
            calls.append((obj["name"], args))

    # 没有标签、只有裸 JSON 行的情况（先把已经抠掉的位置糊成空格，避免重复命中）
    masked = list(text)
    for a, b in spans:
        for i in range(a, b):
            masked[i] = " "
    masked = "".join(masked)
    m = _LOOSE.search(masked)
    if m and not _in_code_block(text, m.start()):
        pos = m.start()
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        try:
            obj, end = decoder.raw_decode(text, pos)
        except Exception:  # noqa: BLE001
            obj, end = None, 0
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = _norm_args(
                obj.get("arguments", obj.get("args", obj.get("parameters", {})))
            )
            calls.append((obj["name"], args))
            spans.append((pos, pos + end))

    # DSML 形态（DeepSeek 的另一种漏法）：标签里带 name，参数可能是 JSON 体，
    # 也可能是 <｜｜DSML｜｜ parameter name="x">值</...>。
    # 原来这种只清标签、不补执行，于是"答应了做图，图却没出来"。这里把调用抠出来。
    for m in _DSML_OPEN.finditer(text):
        start = m.start()
        name_m = _ATTR_NAME.search(m.group(1) or "")
        if not name_m:
            continue
        name = (name_m.group(1) or "").strip()
        if not name:
            continue
        body_start = m.end()
        cm = _DSML_CLOSE.search(text, body_start)
        body_end = cm.start() if cm else min(len(text), body_start + 4000)
        body = text[body_start:body_end]

        args: dict = {}
        brace = body.find("{")
        if brace >= 0:
            try:
                obj, _ = decoder.raw_decode(body, brace)
                if isinstance(obj, dict):
                    # 兼容 {"name":..,"arguments":"<json 字符串>"} 这种套娃
                    raw_args = obj.get(
                        "arguments", obj.get("args", obj.get("parameters", obj))
                    )
                    args = _norm_args(raw_args)
                    name = str(obj.get("name") or name)
            except Exception:  # noqa: BLE001
                args = {}
        if not args:
            for pm in _DSML_PARAM.finditer(body):
                km = _ATTR_NAME.search(pm.group(1) or "")
                if km:
                    args[km.group(1)] = pm.group(2).strip()

        calls.append((name, args))
        spans.append((start, cm.end() if cm else body_end))

    if not spans:
        return text, []

    # 去重（同一句里重复写的调一次就够）
    uniq: list[tuple[str, dict]] = []
    seen = set()
    for name, args in calls:
        key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        uniq.append((name, args))
    calls = uniq

    spans.sort()
    out, cursor = [], 0
    for a, b in spans:
        if a < cursor:  # 重叠就并起来
            cursor = max(cursor, b)
            continue
        out.append(text[cursor:a])
        cursor = b
    out.append(text[cursor:])
    cleaned = "".join(out)

    # 兜底：标签残片、DSML 残片、多余空行
    cleaned = _LEFTOVER.sub("", cleaned)
    cleaned = _DSML.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls


@register(
    "output_guard",
    "dafeiyu",
    "发送前清掉模型漏出来的工具调用标记，能救的顺手真的执行",
    "1.1.0",
)
class OutputGuard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._seq = 0

    # 优先级放最后，保证在别的装饰器（meme_manager 等）改完之后再清
    @filter.on_decorating_result(priority=-1000)
    async def clean(self, event: AstrMessageEvent):
        try:
            res = event.get_result()
            if res is None or not res.chain:
                return

            cleaned_texts: list[str] = []
            all_calls: list[tuple[str, dict]] = []
            changed = False
            kept = []

            for comp in res.chain:
                text = getattr(comp, "text", None)
                if isinstance(text, str) and text:
                    new, calls = _extract(text)
                    if new != text:
                        changed = True
                        try:
                            comp.text = new
                        except Exception:  # noqa: BLE001
                            pass
                    all_calls.extend(calls)
                    cleaned_texts.append(new)
                    if not new:
                        continue
                kept.append(comp)

            if changed:
                try:
                    res.chain = kept
                except Exception:  # noqa: BLE001
                    pass

            did = await self._salvage_all(event, all_calls)
            if changed or did:
                logger.info(
                    f"output_guard: 清掉 {len(all_calls)} 个漏出来的工具调用标记，"
                    f"补执行 {did} 个"
                )
        except Exception:  # noqa: BLE001
            logger.warning("output_guard: 清理失败", exc_info=True)

    async def _salvage_all(
        self, event: AstrMessageEvent, calls: list[tuple[str, dict]]
    ) -> int:
        done, seen = 0, set()
        for name, args in calls:
            if done >= _MAX_CALLS:
                break
            if name not in _SALVAGE:
                logger.info(f"output_guard: {name} 不在白名单，只清文字不补执行")
                continue
            key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            if await self._run_tool(event, name, args):
                done += 1
        return done

    async def _run_tool(self, event: AstrMessageEvent, name: str, args: dict) -> bool:
        try:
            self._seq += 1
            mgr = self.context.get_llm_tool_manager()
            tool = mgr.get_func(name)
            if tool is None:
                logger.warning(f"output_guard: 正文里写了 {name}，但工具没注册，跳过")
                return False
            ctx = None
            try:
                from astrbot.core.astr_agent_context import (
                    AgentContextWrapper,
                    AstrAgentContext,
                )

                ctx = AgentContextWrapper(
                    context=AstrAgentContext(context=self.context, event=event)
                )
            except Exception:  # noqa: BLE001
                ctx = None

            if ctx is not None:
                await tool.call(ctx, **(args or {}))
                return True

            handler = getattr(tool, "handler", None)
            if handler is None:
                logger.warning(f"output_guard: {name} 没有可调用的 handler，跳过")
                return False
            ret = handler(event, **(args or {}))
            if hasattr(ret, "__aiter__"):  # async generator
                async for _ in ret:
                    pass
            elif asyncio.iscoroutine(ret):
                await ret
            return True
        except Exception:  # noqa: BLE001
            logger.exception(f"output_guard: 补执行 {name} 失败")
            return False
