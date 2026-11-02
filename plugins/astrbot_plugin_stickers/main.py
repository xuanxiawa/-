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
    try:  # 写之前先轮转备份，最多留 5 份，改坏了随时能翻回去
        import shutil
        if os.path.exists(_INDEX):
            for i in range(4, 0, -1):
                src = f"{_INDEX}.bak{i}"
                if os.path.exists(src):
                    os.replace(src, f"{_INDEX}.bak{i + 1}")
            shutil.copy2(_INDEX, f"{_INDEX}.bak1")
    except Exception:  # noqa: BLE001
        pass
    with io.open(_INDEX, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)


def _sender_key(event) -> str:
    try:
        sid = re.sub(r"\W", "", str(event.get_sender_id() or ""))
    except Exception:  # noqa: BLE001
        sid = ""
    return sid or "unknown"


def _last_for(event) -> str:
    """优先用「这个人自己最后发的图」，避免群里别人的图把他盖掉。"""
    p = os.path.join(_ROOT, f"last_{_sender_key(event)}.img")
    if os.path.exists(p):
        return p
    return _LAST


def _log(event_tag: str, **kwargs) -> None:
    """决策日志：每次存/发都记一行，出问题时能直接回溯是哪一步错的。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "op": event_tag}
        rec.update(kwargs)
        with io.open(os.path.join(_ROOT, "decisions.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass



def _norm(tags: str) -> list[str]:
    """标签清洗：只留短词，去掉整句、标点和重复。"""
    parts = re.split(r"[,，\s、/|]+", tags or "")
    out: list[str] = []
    for p in parts:
        w = re.sub(r"[^\w\u4e00-\u9fff]", "", p.strip())
        if not w or len(w) > 8:      # 超过 8 个字多半是整句，不是标签
            continue
        if any(ch in w for ch in "。！？!?~…"):
            continue
        if w not in out:
            out.append(w)
    return out[:6]


def _tokens(query: str) -> list[str]:
    return [t.lower() for t in re.split(r"[,，\s、/|]+", query or "") if t.strip()]


def _score(item: dict, tokens: list[str]) -> int:
    """给一张表情打分：标签精确命中最高，其次标签包含，最后描述包含。"""
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    desc = str(item.get("desc") or "").lower()
    tagline = " ".join(tags)
    best = 0
    for tk in tokens:
        s = 0
        if tk in tags:
            s = 12
        elif tk and tk in tagline:
            s = 6
        elif tk and tk in desc:
            s = 3
        if s == 0 and len(tk) >= 2:
            # 拆成两字片段再碰一次（「鲸叫哈气」这种连写）
            for i in range(len(tk) - 1):
                frag = tk[i:i + 2]
                if frag in tagline:
                    s = max(s, 4)
                elif frag in desc:
                    s = max(s, 2)
        best += s
    return best


def _search(query: str) -> tuple[dict | None, int]:
    """在库里找最匹配的一张，返回（条目, 分数）。分数 0 就是没找到。"""
    items = _load()
    tokens = _tokens(query)
    if not items or not tokens:
        return None, 0
    best, best_score = None, 0
    for it in reversed(items):   # 新存的优先
        sc = _score(it, tokens)
        if sc > best_score:
            best, best_score = it, sc
    return best, best_score






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


def _pack_candidates(query: str, limit: int = 12, strict: bool = False) -> list[tuple[str, str]]:
    """从固定表情包里按类别描述找几张，返回 [(路径, 说明)]。strict=True 时找不到就返回空。"""
    import glob
    toks = _tokens(query)
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
        if toks and any(t in hay for t in toks):
            hit_cats.append(cat)
    for cat in hit_cats:
        for path in glob.glob(os.path.join(_PACK_ROOT, "*", "memes", cat, "*.gif"))[:4]:
            out.append((path, f"{cat}"))
        if len(out) >= limit:
            return out[:limit]
    if hit_cats:
        return out[:limit]
    if strict:
        return []
    # 没有命中就随便给几张
    for path in glob.glob(os.path.join(_PACK_ROOT, "*", "memes", "*", "*.gif"))[:limit]:
        out.append((path, os.path.basename(os.path.dirname(path))))
    return out[:limit]



# 关键词兜底默认关闭：让她自己判断要不要用工具。
# 需要临时恢复兜底：建一个空文件 D:\AstrBot\bot\data\plugin_data\keyword_fallback.on
_FALLBACK_FLAG = r"D:\AstrBot\bot\data\plugin_data\keyword_fallback.on"


def _keyword_fallback_on() -> bool:
    try:
        return os.path.exists(_FALLBACK_FLAG)
    except Exception:  # noqa: BLE001
        return False


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
                        with open(path, "rb") as src:
                            data = src.read()
                        with open(_LAST, "wb") as dst:
                            dst.write(data)
                        with open(os.path.join(_ROOT, f"last_{_sender_key(event)}.img"), "wb") as dst:
                            dst.write(data)
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
        src_last = _last_for(event)
        if not os.path.exists(src_last):
            return "他刚没发图，我没什么可存的。"
        try:
            if time.time() - os.path.getmtime(src_last) > 900:
                return "刚才那张图有点久了，你重新发一次我再存。"
            with open(src_last, "rb") as fh:
                data = fh.read()
            if len(data) < 300:
                return "这张太小了，存不了。"
            h = hashlib.md5(data).hexdigest()[:10]
            items = _load()
            old = next((it for it in items if it.get("id") == h), None)
            if old is not None:  # 同一张图重复存：只补标签，不新增
                merged = list(dict.fromkeys((old.get("tags") or []) + _norm(tags)))[:8]
                old["tags"] = merged
                if desc and len(str(old.get("desc") or "")) < len(desc):
                    old["desc"] = desc[:80]
                _save(items)
                _log("save_dup", id=h, tags=merged)
                return f"这张之前存过，标签补齐了：{'/'.join(merged)}"
            ext = ".png"
            if data[:3] == b"GIF":
                ext = ".gif"
            elif data[:2] == b"\xff\xd8":
                ext = ".jpg"
            name = f"{int(time.time())}_{h}{ext}"
            _ensure()
            with open(os.path.join(_ROOT, name), "wb") as fh:
                fh.write(data)
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
            _log("save_new", id=h, tags=_norm(tags), desc=(desc or "")[:30])
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
        hit, score = _search(query)
        if hit is None or score <= 0:
            # 自己库里没有，就去固定包里找；都没有才认输，绝不瞎发
            cands = _pack_candidates(query, strict=True)
            if cands:
                ppath, cat = cands[0]
                try:
                    await self.context.send_message(
                        session=event.unified_msg_origin,
                        message_chain=MessageChain([Image.fromFileSystem(ppath)]),
                    )
                    return f"自己库里没有，发了固定包里的（{cat}类）。"
                except Exception as exc:  # noqa: BLE001
                    return f"发不出去：{exc}"
            _log("send_refused", query=query)
            return (
                f"库里没有跟「{query}」对得上的，固定包也没有，别硬发。"
                "换个说法，或者让我现做一张。"
            )
        path = os.path.join(_ROOT, hit["file"])
        if not os.path.exists(path):
            return "这张图找不到了。"
        try:
            await self.context.send_message(
                session=event.unified_msg_origin,
                message_chain=MessageChain([Image.fromFileSystem(path)]),
            )
            tags = "/".join(hit.get("tags") or [])
            _log("send", query=query, id=hit.get("id"), score=score, tags=tags)
            return f"发了「{hit.get('desc','')[:20]}」（{tags}）。"
        except Exception as exc:  # noqa: BLE001
            return f"发不出去：{exc}"

    @filter.regex(r"加(这个|一下)?(表情|表情包)|存(这个|一下)|收(这个|下)|记住这个")
    async def auto_save(self, event: AstrMessageEvent):
        """他说「加这个表情包」就直接存，不依赖模型调工具。"""
        if not _keyword_fallback_on():
            return
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

    @filter.regex(r"(?<![没别不想要会])使用\s*[^\s，,。！!？?、]{1,10}")
    async def auto_use(self, event: AstrMessageEvent):
        """「风铃使用顶飞」这种明说的，直接发，不靠模型记得调工具。

        只认短命令：长句、问句、否定句里的「使用」不算，否则「我还没使用寿来呢」
        会被当成命令，直接把回复截断成一张表情包。
        """
        text = (event.message_str or "").strip()
        if len(text) > 20 or "？" in text or "?" in text:
            return
        m = re.search(r"(?<![没别不想要会])使用\s*([^\s，,。！!？?、]{1,10})", text)
        if not m:
            return
        kw = m.group(1).strip()
        if kw in ("技能", "表情", "表情包", "这个", "那个", "图", "一下", "它"):
            return
        hit, score = _search(kw)
        if hit is None or score <= 0:
            return  # 库里没有就交给模型/固定包去处理，别抢话
        path = os.path.join(_ROOT, hit["file"])
        if not os.path.exists(path):
            return
        logger.info(f"stickers: 关键词直发 -> {kw}（{hit.get('desc','')[:16]}）")
        _log("auto_use", kw=kw, id=hit.get("id"), score=score)
        _recent(f"你按他的要求发了「{kw}」那张表情")
        event.stop_event()
        yield event.chain_result([
            Plain(f"{kw}～！"),
            Image.fromFileSystem(path),
        ])


    @filter.regex(r"用你(存|收)|发你(存|收)|把你(存|收)|你(现在)?有(的|哪些)|发你(现在)?有|发你自己的|你的表情包|发那个(表情|图)|发出来|发我看看|给我看看|看看你的|发来看看")
    async def auto_send(self, event: AstrMessageEvent):
        """他说「用你存的那个」，就把自己库里的发出来。"""
        if not _keyword_fallback_on():
            return
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
        hit, score = _search(query)
        if hit is not None and score > 0:
            path = os.path.join(_ROOT, hit["file"])
            if os.path.exists(path):
                await self.context.send_message(
                    session=event.unified_msg_origin,
                    message_chain=MessageChain([Image.fromFileSystem(path)]),
                )
                return f"发了一张自己存的：{hit.get('desc','')[:20]}"
        cands = _pack_candidates(query, strict=True)
        if not cands:
            return f"两个库里都没找到跟「{query}」对得上的。"
        path, cat = cands[0]
        await self.context.send_message(
            session=event.unified_msg_origin,
            message_chain=MessageChain([Image.fromFileSystem(path)]),
        )
        return f"发了一张固定包里的（{cat}类）。"

    @filter.command("表情包自检")
    async def cmd_check(self, event: AstrMessageEvent):
        """检查库的完整性：缺文件、重复 id、空标签、脏标签。"""
        items = _load()
        if not items:
            yield event.plain_result("库里还没有表情包。")
            return
        missing, dup, notag, dirty = [], [], [], []
        seen = set()
        for it in items:
            name = str(it.get("file") or "")
            desc = str(it.get("desc") or "")[:14]
            if not name or not os.path.exists(os.path.join(_ROOT, name)):
                missing.append(desc or name or "?")
            if it.get("id") in seen:
                dup.append(desc)
            seen.add(it.get("id"))
            tags = it.get("tags") or []
            if not tags:
                notag.append(desc)
            for t in tags:
                if len(str(t)) > 8 or any(c in str(t) for c in "。！？!?~…"):
                    dirty.append(str(t)[:12])
        lines = [f"共 {len(items)} 张"]
        if missing:
            lines.append(f"缺文件 {len(missing)}：{'；'.join(missing[:3])}")
        if dup:
            lines.append(f"重复 id {len(dup)}：{'；'.join(dup[:3])}")
        if notag:
            lines.append(f"没标签 {len(notag)}：{'；'.join(notag[:3])}")
        if dirty:
            lines.append(f"脏标签 {len(dirty)}：{'；'.join(dirty[:3])}")
        if not (missing or dup or notag or dirty):
            lines.append("全项正常 ✅")
        yield event.plain_result("\n".join(lines))

    @filter.command("我的表情包")
    async def cmd_list(self, event: AstrMessageEvent):
        """看她自己收了多少张。"""
        items = _load()
        if not items:
            yield event.plain_result("还没有自己的表情包，发张图给我存呗。")
            return
        lines = [f"{i+1}. {it.get('desc','')[:18]}（{'/'.join(it.get('tags') or [])}）" for i, it in enumerate(items[-8:])]
        yield event.plain_result(f"我有 {len(items)} 张：\n" + "\n".join(lines))
