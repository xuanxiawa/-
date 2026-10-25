"""她自己的表情包：主人发来的图，她可以收进自己的库，以后自己发。

- save_sticker：把主人刚发的那张图存起来（带描述/标签）
- send_sticker：按描述/标签找一张发出去
- /我的表情包：看收了多少、都有什么
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

_ROOT = r"D:\AstrBot\stickers"
_INDEX = os.path.join(_ROOT, "index.json")
_LAST = os.path.join(_ROOT, "last.png")


def _ensure() -> None:
    os.makedirs(_ROOT, exist_ok=True)


def _load() -> list[dict]:
    try:
        with io.open(_INDEX, encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items: list[dict]) -> None:
    _ensure()
    with io.open(_INDEX, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)


def _norm(tags: str) -> list[str]:
    parts = re.split(r"[,，\s、]+", tags or "")
    return [p.strip() for p in parts if p.strip()][:8]






def _recent(text: str) -> None:
    try:
        import json as _json
        with io.open(r"D:\\AstrBot\\bot\\data\\plugin_data\\heart\\recent.jsonl", "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({"ts": time.time(), "role": "你（动作）", "text": text}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _describe_image(path: str) -> str:
    """用免费视觉模型看一眼这张图，给个描述（GIF 先转首帧 JPEG，GLM 不吃 GIF）。"""
    try:
        import base64, json as _json, urllib.request
        from PIL import Image
        with io.open(r"D:\\AstrBot\\bot\\data\\config\\astrbot_plugin_screen_eye_config.json", encoding="utf-8-sig") as fh:
            key = (_json.load(fh).get("api_key") or "").strip()
        if not key or not os.path.exists(path):
            return ""
        img = Image.open(path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
        payload = {
            "model": "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                {"type": "text", "text": "这是聊天里的一张表情包。用中文说清楚：画的是什么、什么情绪、适合接什么话。30 字以内。"},
            ]}],
            "temperature": 0.2, "max_tokens": 120,
        }
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            d = _json.loads(resp.read().decode("utf-8", "ignore"))
        return (d["choices"][0]["message"]["content"] or "").strip()[:60]
    except Exception:  # noqa: BLE001
        return ""


def _save_last(desc: str, tags: str) -> str:
    """把 last.png 存进库，返回结果文字。"""
    if not os.path.exists(_LAST):
        return "他最近没发图，我没什么可存的。"
    try:
        with open(_LAST, "rb") as fh:
            data = fh.read()
        if len(data) < 300:
            return "这张太小了。"
        h = hashlib.md5(data).hexdigest()[:10]
        ext = ".gif" if data[:3] == b"GIF" else (".jpg" if data[:2] == b"\xff\xd8" else ".png")
        name = f"{int(time.time())}_{h}{ext}"
        _ensure()
        with open(os.path.join(_ROOT, name), "wb") as fh:
            fh.write(data)
        items = _load()
        items.append({"id": h, "file": name, "desc": desc[:80], "tags": _norm(tags),
                      "added": time.strftime("%Y-%m-%d %H:%M")})
        _save(items)
        try:
            with io.open(r"D:\AstrBot\stickers\just_saved.json", "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "file": name, "desc": desc[:40]}, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        _recent(f"你把他发的表情包收进自己库了（第 {len(items)} 张）：{desc[:30]}")
        return f"存好了，我现在有 {len(items)} 张自己的表情包。"
    except Exception as exc:  # noqa: BLE001
        return f"没存下来：{exc}"



# ---------- 合并检索：她自己的库 + meme_manager 的 157 张 ----------
_PACK_ROOT = r"D:\AstrBot\bot\data\plugin_data\meme_manager\packs"
_PACK_DATA = r"D:\AstrBot\bot\data\plugin_data\meme_manager\packs\blue-fat-fish-meme-pack\memes_data.json"


def _pack_candidates(query: str, limit: int = 12) -> list[tuple[str, str]]:
    """从固定表情包里按类别描述找几张，返回 [(路径, 说明)]。"""
    import glob
    q = (query or "").lower()
    out: list[tuple[str, str]] = []
    try:
        with io.open(_PACK_DATA, encoding="utf-8-sig") as fh:
            cats = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        cats = {}
    # 类别名和描述里命中，优先
    hit_cats = []
    for cat, desc in cats.items():
        hay = (cat + " " + str(desc)).lower()
        if q and (q in hay or any(w in hay for w in q.split())):
            hit_cats.append(cat)
    for cat in hit_cats:
        for path in glob.glob(os.path.join(_PACK_ROOT, "*", "memes", cat, "*.gif"))[:4]:
            out.append((path, f"{cat}"))
        if len(out) >= limit:
            return out[:limit]
    # 没有命中就随便给几张
    for path in glob.glob(os.path.join(_PACK_ROOT, "*", "memes", "*", "*.gif"))[:limit]:
        out.append((path, os.path.basename(os.path.dirname(path))))
    return out[:limit]


@register("stickers", "dafeiyu", "她自己的表情包库", "1.0.0")
class MyStickers(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        _ensure()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_image(self, event: AstrMessageEvent):
        """记住主人最后发来的一张图，供 save_sticker 使用。"""
        try:
            for comp in event.get_messages():
                if isinstance(comp, Image):
                    path = await comp.convert_to_file_path()
                    if path and os.path.exists(path):
                        os.makedirs(_ROOT, exist_ok=True)
                        with open(path, "rb") as src, open(_LAST, "wb") as dst:
                            dst.write(src.read())
                    return
        except Exception:  # noqa: BLE001
            logger.debug("sticker capture failed", exc_info=True)

    @llm_tool(name="save_sticker")
    async def save_sticker(self, event: AstrMessageEvent, desc: str, tags: str = "") -> str:
        """把主人刚发的那张图收进自己的表情包库。

        Args:
            desc(string): 这张图是什么，比如「戴墨镜的黑猫在草地上跑」
            tags(string): 逗号分隔的标签，比如「猫,搞笑,墨镜」
        """
        if not os.path.exists(_LAST):
            return "他刚没发图，我没什么可存的。"
        try:
            with open(_LAST, "rb") as fh:
                data = fh.read()
            if len(data) < 300:
                return "这张太小了，存不了。"
            h = hashlib.md5(data).hexdigest()[:10]
            ext = ".png"
            if data[:3] == b"GIF":
                ext = ".gif"
            elif data[:2] == b"\xff\xd8":
                ext = ".jpg"
            name = f"{int(time.time())}_{h}{ext}"
            _ensure()
            with open(os.path.join(_ROOT, name), "wb") as fh:
                fh.write(data)
            items = _load()
            items.append(
                {
                    "id": h,
                    "file": name,
                    "desc": (desc or "").strip()[:80],
                    "tags": _norm(tags),
                    "added": time.strftime("%Y-%m-%d %H:%M"),
                }
            )
            _save(items)
            logger.info(f"stickers: 收了 1 张 {name} ({desc[:20]})")
            return f"收好了，我现在有 {len(items)} 张自己的表情包。"
        except Exception as exc:  # noqa: BLE001
            return f"没存下来：{exc}"

    @llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, query: str) -> str:
        """从自己的表情包库里找一张发出去。

        Args:
            query(string): 想表达的意思或关键词，比如「猫」「搞笑」
        """
        items = _load()
        if not items:
            return "我还没有自己的表情包。"
        q = (query or "").lower()
        hit = None
        for it in reversed(items):
            hay = (it.get("desc") or "") + " " + " ".join(it.get("tags") or [])
            if q and q in hay.lower():
                hit = it
                break
        if hit is None:
            hit = items[-1]
        path = os.path.join(_ROOT, hit["file"])
        if not os.path.exists(path):
            return "这张图找不到了。"
        try:
            await self.context.send_message(
                session=event.unified_msg_origin,
                message_chain=MessageChain([Image.fromFileSystem(path)]),
            )
            return f"发了「{hit.get('desc','')[:20]}」。"
        except Exception as exc:  # noqa: BLE001
            return f"发不出去：{exc}"

    @filter.regex(r"加(这个|一下)?(表情|表情包)|存(这个|一下)|收(这个|下)|记住这个")
    async def auto_save(self, event: AstrMessageEvent):
        """他说「加这个表情包」就直接存，不依赖模型调工具。"""
        text = (event.message_str or "").strip()
        if len(text) > 40:
            return
        if not os.path.exists(_LAST):
            return
        try:
            if time.time() - os.path.getmtime(_LAST) > 1800:
                return
        except Exception:  # noqa: BLE001
            pass
        desc = _describe_image(_LAST) or "主人发给我的表情"
        result = _save_last(desc, text)
        logger.info(f"stickers: 关键词存图 -> {desc[:20]}")
        yield event.plain_result(result)

    @filter.regex(r"用你(存|收)|发你(存|收)|把你(存|收)|你(现在)?有(的|哪些)|发你(现在)?有|发你自己的|你的表情包|发那个(表情|图)|发出来|发我看看|给我看看|看看你的|发来看看")
    async def auto_send(self, event: AstrMessageEvent):
        """他说「用你存的那个」，就把自己库里的发出来。"""
        items = _load()
        if not items:
            yield event.plain_result("我还没存过表情包。")
            return
        text = (event.message_str or "")
        explicit = any(k in text for k in ("你用", "用你", "你的表情", "发你", "你自己"))
        fresh = False
        try:
            with io.open(r"D:\AstrBot\stickers\just_saved.json", encoding="utf-8") as fh:
                d = json.load(fh) or {}
            fresh = time.time() - float(d.get("ts") or 0) < 900
        except Exception:  # noqa: BLE001
            fresh = False
        if not (explicit or fresh):
            return
        hit = items[-1]
        path = os.path.join(_ROOT, hit["file"])
        if not os.path.exists(path):
            yield event.plain_result("那张图找不到了。")
            return
        _recent(f"你从自己库里挑了一张表情发给他：{hit.get('desc','')[:30]}")
        yield event.chain_result([
            Plain(f"（{len(items)} 张里我选这张：{hit.get('desc','')[:16]}）"),
            Image.fromFileSystem(path),
        ])

    @llm_tool(name="find_sticker")
    async def find_sticker(self, event: AstrMessageEvent, query: str) -> str:
        """在两个库里找表情：先自己的，再固定表情包。

        Args:
            query(string): 想表达的情绪或关键词，比如 摸鱼、害羞、笑
        """
        items = _load()
        q = (query or "").lower()
        for it in reversed(items):
            hay = (it.get("desc") or "") + " " + " ".join(it.get("tags") or [])
            if q and q in hay.lower():
                path = os.path.join(_ROOT, it["file"])
                if os.path.exists(path):
                    await self.context.send_message(
                        session=event.unified_msg_origin,
                        message_chain=MessageChain([Image.fromFileSystem(path)]),
                    )
                    return f"发了一张自己存的：{it.get('desc','')[:20]}"
        cands = _pack_candidates(query)
        if not cands:
            return "两个库里都没找到合适的。"
        path, cat = cands[0]
        await self.context.send_message(
            session=event.unified_msg_origin,
            message_chain=MessageChain([Image.fromFileSystem(path)]),
        )
        return f"发了一张固定包里的（{cat}类）。"

    @filter.command("我的表情包")
    async def cmd_list(self, event: AstrMessageEvent):
        """看她自己收了多少张。"""
        items = _load()
        if not items:
            yield event.plain_result("还没有自己的表情包，发张图给我存呗。")
            return
        lines = [f"{i+1}. {it.get('desc','')[:18]}（{'/'.join(it.get('tags') or [])}）" for i, it in enumerate(items[-8:])]
        yield event.plain_result(f"我有 {len(items)} 张：\n" + "\n".join(lines))
