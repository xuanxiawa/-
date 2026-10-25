"""浏览器刷 B 站。

- 一个常驻的 Chromium（持久化 profile，登录状态可保留）
- 工具：bili_search / bili_open / bili_scroll / bili_watch
- 每次动作后截屏，交给智谱 glm-4v-flash 看懂页面，把描述返回给对话模型
- 只读浏览：不点赞、不投币、不发评论、不发弹幕
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import time
from datetime import datetime

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

_PROFILE = r"D:\AstrBot\browser-profile"
_SHOT_DIR = r"D:\AstrBot\browser-shots"
_SCREEN_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_screen_eye_config.json"
_SOUL_STATE = r"D:\AstrBot\bot\data\plugin_data\dafeiyu_soul\state.json"
_DIARY = r"D:\AstrBot\browser-diary.jsonl"
_HISTORY = r"D:\AstrBot\browser-history.json"
_LAST_UMO = r"D:\AstrBot\browser-last-umo.txt"

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", r"D:\AstrBot\ms-playwright")

_playwright = None
_context = None
_page = None
_lock = asyncio.Lock()

_VISION_PROMPT = (
    "这是浏览器里 B 站页面的截图。用中文说明：这是哪个页面（首页/搜索/视频/动态）、"
    "视频标题、UP 主、当前画面在演什么；"
    "如果画面或字幕里出现**数字**（价格、数量、年份、型号），必须原样写出来。"
    "120 字以内，只写看到的，不猜。"
)


def _vision_key() -> str:
    try:
        with io.open(_SCREEN_CFG, encoding="utf-8-sig") as fh:
            return str(json.load(fh).get("api_key") or "")
    except Exception:  # noqa: BLE001
        return ""


def _describe(jpeg: bytes) -> str:
    key = _vision_key()
    if not key:
        return "（没配视觉模型，只有文字信息）"
    b64 = base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": "glm-4v-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }
        ],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    import urllib.request

    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"browser vision failed: {exc}")
        return f"（看图失败：{exc}）"


async def _ensure_page():
    """惰性启动浏览器，返回 page。"""
    global _playwright, _context, _page
    if _page is not None and not _page.is_closed():
        return _page
    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()
    os.makedirs(_PROFILE, exist_ok=True)
    if _context is None or not _context.pages:
        # 方案 A：窗口放到屏幕外（headless 需要额外下载 chromium-headless-shell，
        # 这台机器没装；离屏窗口同样不弹窗、不抢焦点）
        _context = await _playwright.chromium.launch_persistent_context(
            _PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-position=-10000,-10000",
                "--window-size=1280,800",
                "--mute-audio",
            ],
        )
    _page = _context.pages[0] if _context.pages else await _context.new_page()
    return _page


async def _shot(page) -> tuple[bytes, str]:
    os.makedirs(_SHOT_DIR, exist_ok=True)
    path = os.path.join(_SHOT_DIR, f"shot_{int(time.time())}.jpg")
    await page.screenshot(path=path, type="jpeg", quality=70)
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        os.remove(path)
    except Exception:  # noqa: BLE001
        pass
    return data


async def _page_text(page, limit: int = 900) -> str:
    try:
        text = await page.inner_text("body")
    except Exception:  # noqa: BLE001
        text = ""
    text = " ".join((text or "").split())
    return text[:limit]


async def _look(page) -> str:
    jpeg = await _shot(page)
    desc = await asyncio.to_thread(_describe, jpeg)
    title = await page.title()
    url = page.url
    text = await _page_text(page)
    return f"页面标题：{title}\n网址：{url}\n画面：{desc}\n页面文字（节选）：{text}"


@register(
    "browser_bili",
    "dafeiyu",
    "开真实浏览器刷 B 站，看页面、截图、读懂内容（只读）",
    "1.0.0",
)
class BrowserBili(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @llm_tool(name="bili_search")
    async def bili_search(self, event: AstrMessageEvent, keyword: str) -> str:
        """在 B 站搜索关键词，返回搜索结果里的视频标题列表。

        当用户说「去B站搜一下XX」「B站有没有关于XX的视频」时调用。

        Args:
            keyword(string): 搜索关键词
        """
        async with _lock:
            try:
                page = await _ensure_page()
                url = "https://search.bilibili.com/all?keyword=" + urllib_quote(keyword)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.5)
                items = await page.eval_on_selector_all(
                    "a[href*='/video/BV']",
                    "els => els.map(e => ({t: (e.innerText||'').trim(), h: e.href})).filter(x => x.t.length > 4)",
                )
                seen, out = set(), []
                for it in items:
                    t = " ".join(it["t"].split())[:60]
                    if t and t not in seen:
                        seen.add(t)
                        out.append(f"- {t} ｜ {it['h']}")
                    if len(out) >= 10:
                        break
                if not out:
                    return "没搜到结果（可能被风控或页面没加载出来）。"
                return "B站搜索「%s」的结果：\n%s" % (keyword, "\n".join(out))
            except Exception as exc:  # noqa: BLE001
                logger.exception("bili_search failed")
                return f"搜索失败：{exc}"

    @llm_tool(name="bili_open")
    async def bili_open(self, event: AstrMessageEvent, url: str) -> str:
        """打开一个 B 站页面（视频/首页/任意链接），截图并读懂当前画面。

        当用户给出 B 站链接、或者让你「点开第一个」「去看这个视频」时调用。

        Args:
            url(string): 要打开的网址，或 BV 号
        """
        async with _lock:
            try:
                page = await _ensure_page()
                if url.startswith("BV"):
                    url = f"https://www.bilibili.com/video/{url}"
                if not url.startswith("http"):
                    url = "https://" + url
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3)
                return await _look(page)
            except Exception as exc:  # noqa: BLE001
                logger.exception("bili_open failed")
                return f"打开失败：{exc}"

    @llm_tool(name="bili_scroll")
    async def bili_scroll(self, event: AstrMessageEvent) -> str:
        """把当前页面往下滚三屏，看看还有什么内容。

        当用户说「往下翻翻」「还有别的吗」时调用。
        """
        async with _lock:
            try:
                page = await _ensure_page()
                for _ in range(3):
                    await page.mouse.wheel(0, 900)
                    await asyncio.sleep(0.8)
                await asyncio.sleep(1.5)
                return await _look(page)
            except Exception as exc:  # noqa: BLE001
                return f"滚动失败：{exc}"

    @llm_tool(name="bili_watch")
    async def bili_watch(self, event: AstrMessageEvent, url: str) -> str:
        """打开一个视频并播放十几秒，然后说说画面上在演什么。

        当用户说「你看这个视频」「帮我看下这个视频讲了啥」时调用。

        Args:
            url(string): 视频链接或 BV 号
        """
        async with _lock:
            try:
                page = await _ensure_page()
                if url.startswith("BV"):
                    url = f"https://www.bilibili.com/video/{url}"
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)
                try:
                    await page.click(".bpx-player-ctrl-play", timeout=5000)
                except Exception:  # noqa: BLE001
                    try:
                        await page.click("video", timeout=3000)
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(14)
                return await _look(page)
            except Exception as exc:  # noqa: BLE001
                return f"看视频失败：{exc}"

    @filter.command("刷b站")
    async def browse_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        """发「/刷b站 关键词」，机器人会搜一下并点开第一个结果。"""
        async with _lock:
            try:
                page = await _ensure_page()
                if keyword:
                    url = "https://search.bilibili.com/all?keyword=" + urllib_quote(keyword)
                else:
                    url = "https://www.bilibili.com"
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3)
                jpeg = await _shot(page)
                desc = await asyncio.to_thread(_describe, jpeg)
                path = os.path.join(_SHOT_DIR, f"cmd_{int(time.time())}.jpg")
                os.makedirs(_SHOT_DIR, exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(jpeg)
                yield event.chain_result([Plain(desc), Image.fromFileSystem(path)])
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"刷不动：{exc}")


    @filter.on_llm_request(priority=-120)
    async def inject_video_notes(self, event: AstrMessageEvent, req):
        """聊天时把看过的、相关的视频笔记塞进去。"""
        try:
            text = req.prompt or event.message_str or ""
            notes = _match_video_notes(str(text))
            if notes:
                block = "【你看过的视频，可以自然提起】\n" + "\n".join(f"- {n}" for n in notes)
                req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        except Exception:  # noqa: BLE001
            logger.debug("browser_bili: 注入视频笔记失败", exc_info=True)

    @filter.command("b站登录")
    async def bili_login_cmd(self, event: AstrMessageEvent):
        """在它自己的浏览器里打开 B 站登录页，扫码后就能拉字幕了。"""
        async with _lock:
            try:
                page = await _ensure_page()
                await page.goto(
                    "https://passport.bilibili.com/login",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                yield event.plain_result(
                    "浏览器里已经打开 B 站登录页，扫码登录后我会自动检测（最多等 3 分钟）…"
                )
                for _ in range(36):
                    await asyncio.sleep(5)
                    try:
                        cookies = await _context.cookies()
                        if any(c.get("name") == "DedeUserID" for c in cookies):
                            yield event.plain_result(
                                "登录成功，之后拉字幕、看推荐都会用这个账号。"
                            )
                            return
                    except Exception:  # noqa: BLE001
                        continue
                yield event.plain_result("还没检测到登录，稍后再发一次 /b站登录。")
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"打开登录页失败：{exc}")

    # ---------- 自主浏览：按兴趣和心情自己找视频 ----------

    @filter.on_astrbot_loaded()
    async def start_auto_explore(self):
        try:
            asyncio.create_task(self._explore_loop())
            logger.info("browser_bili: 自主浏览任务已启动")
        except Exception:  # noqa: BLE001
            logger.exception("browser_bili: 自主浏览任务启动失败")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def remember_session(self, event: AstrMessageEvent):
        """记住最近的私聊会话，方便主动发消息。"""
        try:
            umo = event.unified_msg_origin
            if umo:
                with open(_LAST_UMO, "w", encoding="utf-8") as fh:
                    fh.write(umo)
        except Exception:  # noqa: BLE001
            pass

    async def _explore_loop(self):
        await asyncio.sleep(90)
        while True:
            try:
                await asyncio.sleep(20 * 60)
                if not _should_explore():
                    continue
                if _lock.locked():
                    continue
                async with _lock:
                    result = await _explore_once()
                if not result:
                    continue
                logger.info(f"browser_bili: 自主看视频 {result.get('title')}")
                await self._notify(result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("browser_bili: 自主浏览出错")

    async def _notify(self, result: dict):
        """刷完视频，用她自己的口吻说一句（不再贴原始描述）。"""
        try:
            umo = _read_umo()
            if not umo:
                logger.info("browser_bili: 没有 umo，跳过通知")
                return
            title = result.get("title") or ""
            desc = (result.get("desc") or "")[:300]
            text = ""
            try:
                provider = await self.context.get_using_provider_async(umo)
                if provider:
                    comments = result.get("comments") or []
                    cm = "；".join(c[:30] for c in comments[:4]) if comments else "（没拉到评论）"
                    prompt = (
                        f"你刚在主人的屏幕上刷到一个 B 站视频：《{title}》。\n"
                        f"画面：{desc}\n"
                        f"评论区在说：{cm}\n"
                        "发 QQ 消息跟他分享。必须说清三件事，缺一不可：\n"
                        "1) 这是什么——谁/什么视频/什么片段（一句话点明对象）\n"
                        "2) 里面发生了什么——具体到动作或台词，让他能想象出画面\n"
                        "3) 你的看法——吐槽/惊讶/联想，带口癖和语气词\n"
                        "总共 60 字以内，可以分两三句。不要只喊情绪不交代对象，不要报标题清单，不要『好看吗』。"
                        "只输出消息本身。"
                    )
                    resp = await provider.text_chat(prompt=prompt)
                    text = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')
            except Exception:  # noqa: BLE001
                logger.debug("browser_bili: 评论生成失败", exc_info=True)
            if not text:
                text = f"刚刷到个《{title}》 有点意思"
            await self.context.send_message(
                session=umo, message_chain=MessageChain([Plain(text[:200])])
            )
            logger.info(f"browser_bili: 主动通知 -> {text[:40]}")
            try:
                with io.open(r"D:\AstrBot\recent-outgoing.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "text": text[:200]}, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            logger.debug("browser_bili: 主动通知失败", exc_info=True)

    @llm_tool(name="bili_explore")
    async def bili_explore_tool(self, event: AstrMessageEvent) -> str:
        """自己按兴趣和当前心情，去 B 站随便找一个视频看十几秒，然后说说看到了什么。

        当用户说「你自己找点看的」「无聊就自己刷会儿」「随便看看」时调用。
        """
        async with _lock:
            try:
                result = await _explore_once()
                if not result:
                    return "翻了半天没找到合适的视频。"
                return (
                    f"我自己挑了个视频：「{result.get('title')}」（搜的是「{result.get('keyword')}」）。"
                    f"画面：{result.get('desc')}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("bili_explore failed")
                return f"自己刷失败了：{exc}"

    @filter.command("兴趣")
    async def interests_cmd(self, event: AstrMessageEvent):
        """看她现在的兴趣排行、今天刷了几次。"""
        try:
            data = {}
            try:
                with io.open(_INTERESTS, encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except Exception:  # noqa: BLE001
                data = {}
            today, count = "", 0
            try:
                with io.open(_EXPLORE_LOG, encoding="utf-8") as fh:
                    d = json.load(fh) or {}
                today = str(d.get("date") or "")
                count = int(d.get("count") or 0)
            except Exception:  # noqa: BLE001
                pass
            s = _read_soul()
            head = (
                f"今天已经看了 {count} 个（上限 4）\n"
                f"好奇 {float(s.get('curiosity') or 0):.2f}｜"
                f"无聊 {float(s.get('boredom') or 0):.2f}｜"
                f"心情 {float(s.get('mood') or 0):.2f}"
            )
            items = sorted(
                ((k, float(v)) for k, v in data.items() if float(v) > 0),
                key=lambda x: x[1],
                reverse=True,
            )[:8]
            if not items:
                yield event.plain_result(head + "\n\n还没有稳定的兴趣，都在随便看。")
                return
            lines = []
            for k, v in items:
                bar = "▮" * max(1, int(round(v)))
                lines.append(f"{k} {bar} {v:.1f}")
            yield event.plain_result(head + "\n\n喜欢看：\n" + "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"看不了：{exc}")

    @filter.command("看过的")
    async def watched_cmd(self, event: AstrMessageEvent):
        """最近刷过的视频（从她自己的记录里读）。"""
        try:
            rows = []
            with io.open(_DIARY, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        it = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    title = it.get("title") or ""
                    if title:
                        rows.append(f"{it.get('ts','')[:10]} {title[:28]}")
            if not rows:
                yield event.plain_result("还没刷过视频。")
                return
            yield event.plain_result("最近看的：\n" + "\n".join(rows[-6:]))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"读不到：{exc}")

    @filter.command("自己刷")
    async def self_browse_cmd(self, event: AstrMessageEvent):
        """让它按当前心情自己去找个视频看。"""
        async with _lock:
            try:
                result = await _explore_once()
                if not result:
                    yield event.plain_result("翻了半天没找到合适的视频。")
                    return
                desc = str(result.get("desc") or "")
                desc = desc.splitlines()[0][:80] if desc else ""
                yield event.plain_result(
                    f"我去看了「{result.get('title')}」，搜的是「{result.get('keyword')}」。{desc}"
                )
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"刷不动：{exc}")


# ================= 模块级辅助：兴趣/心情/历史 =================

_TOPIC_BY_MOOD = {
    "bored": ["搞笑", "整活", "沙雕", "抽象", "离谱"],
    "curious": ["科普", "冷知识", "AI", "数码", "动漫"],
    "tired": ["白噪音", "助眠", "慢节奏", "日常", "治愈"],
    "default": ["游戏", "动漫", "音乐", "猫", "装机"],
}

# 不碰的题材：标题里出现这些词就跳过
_NSFW_WORDS = [
    "灰丝", "黑丝", "白丝", "丝袜", "擦边", "软色情", "福利向", "福利视频",
    "内衣", "情趣", "裸", "湿身", "诱惑", "私房", "写真", "腿控", "玉足",
]


def _read_soul() -> dict:
    try:
        with io.open(_SOUL_STATE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _pick_topic() -> tuple[str, str]:
    """挑搜索词：以她自己攒的兴趣为主，偶尔突然想看点别的。

    权重：
      55%  从她兴趣分最高的几个主题里挑
      30%  从当前心情对应的池子里挑（探索新东西）
      15%  完全随机（突然兴起）
    """
    s = _read_soul()
    boredom = float(s.get("boredom") or 0)
    curiosity = float(s.get("curiosity") or 0)
    energy = float(s.get("energy", 0.6) or 0)
    if boredom >= 0.6:
        tag = "bored"
    elif curiosity >= 0.75:
        tag = "curious"
    elif energy <= 0.3:
        tag = "tired"
    else:
        tag = "default"

    mood_pool = list(_TOPIC_BY_MOOD[tag])
    all_pool = [k for v in _TOPIC_BY_MOOD.values() for k in v]

    # 她自己的兴趣：分数 > 0 的，按分数加权
    interests = {}
    try:
        with io.open(_INTERESTS, encoding="utf-8") as fh:
            interests = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        interests = {}
    liked = {k: float(v) for k, v in interests.items() if float(v) > 0}
    # 同时把心情池里的词也算一点基础兴趣，避免"没记录就完全随机"
    for k in mood_pool:
        liked.setdefault(k, 0.8)

    roll = random.random()
    if liked and roll < 0.55:
        keys = list(liked.keys())
        weights = [max(0.1, liked[k]) for k in keys]
        return random.choices(keys, weights=weights, k=1)[0], tag
    if roll < 0.85:
        return random.choice(mood_pool), tag
    return random.choice(all_pool), tag


def _load_history() -> list[str]:
    try:
        with io.open(_HISTORY, encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_history(items: list[str]) -> None:
    try:
        with io.open(_HISTORY, "w", encoding="utf-8") as fh:
            json.dump(items[-300:], fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _read_umo() -> str:
    try:
        with io.open(_LAST_UMO, encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def _should_explore() -> bool:
    """判断现在该不该自己去看视频（每天 4 次 / 间隔 3 小时）。"""
    now = datetime.now()
    if not (9 <= now.hour < 24):
        return False
    ok, _why = _explore_budget()
    if not ok:
        return False
    s = _read_soul()
    boredom = float(s.get("boredom") or 0)
    curiosity = float(s.get("curiosity") or 0)
    # 频率由 _explore_budget 管（每天 4 次、间隔 3 小时）
    if boredom >= 0.45 or curiosity >= 0.8:
        return True
    # 心情平平的时候也偶尔自发去看（每次检查 15% 概率）
    return random.random() < 0.15


def _diary_append(result: dict) -> None:
    try:
        with io.open(_DIARY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


async def _explore_once() -> dict | None:
    """按心情搜一个词，挑一个没看过的视频，播十几秒，回来描述。"""
    page = await _ensure_page()
    keyword, tag = _pick_topic()
    url = "https://search.bilibili.com/all?keyword=" + urllib_quote(keyword)
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(2.5)

    items = await page.eval_on_selector_all(
        "a[href*='/video/BV']",
        "els => els.map(e => ({t: (e.innerText||'').trim(), h: e.href})).filter(x => x.t.length > 6)",
    )
    seen = set(_load_history())
    candidates = []
    for it in items:
        href = str(it.get("h") or "").split("?")[0]
        if "/video/BV" not in href or href in seen:
            continue
        candidates.append(it)
        if len(candidates) >= 3:
            break
    if not candidates:
        return None

    picked = None
    title = ""
    video_url = ""
    for cand in candidates:
        video_url = cand["h"]
        try:
            await page.goto(video_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.5)
            body = await _page_text(page, 200)
            if len(body) < 40:
                continue
            real_title = (await page.title()).strip()
            real_title = re.sub(r"[_\-]?\s*哔哩哔哩.*$", "", real_title).strip()
            title = (real_title or " ".join(cand["t"].split()))[:80]
            low = (title + " " + body[:300]).lower()
            if any(w in low for w in _NSFW_WORDS):
                _diary_append({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "keyword": keyword,
                               "skipped": "nsfw", "title": title})
                continue
            picked = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if not picked:
        return None

    try:
        await page.click(".bpx-player-ctrl-play", timeout=4000)
    except Exception:  # noqa: BLE001
        try:
            await page.click("video", timeout=2000)
        except Exception:  # noqa: BLE001
            pass
    # 多点采样：开头 / 中段 / 靠后各看一眼，把三帧的细节合起来
    await asyncio.sleep(4)
    frames: list[str] = []
    for wait in (0, 8, 8):
        if wait:
            await asyncio.sleep(wait)
        try:
            jpeg = await _shot(page)
            one = await asyncio.to_thread(_describe, jpeg)
            if one and one.strip():
                frames.append(one.strip())
        except Exception:  # noqa: BLE001
            continue
    parts = []
    for i, f in enumerate(frames):
        tag = ["开头", "中段", "后段"][i] if i < 3 else f"第{i}帧"
        parts.append(f"[{tag}] {f[:150]}")
    desc = "；".join(parts) if parts else ""
    if not desc:
        jpeg = await _shot(page)
        desc = await asyncio.to_thread(_describe, jpeg)

    # 学一下这个视频讲了什么（有字幕才学，每天有上限）
    cookie = ""
    try:
        if _context is not None:
            raw = await _context.cookies()
            cookie = "; ".join(
                f"{c['name']}={c['value']}"
                for c in raw
                if "bilibili" in str(c.get("domain", ""))
            )
    except Exception:  # noqa: BLE001
        cookie = ""
    learned = await asyncio.to_thread(_learn_video, video_url, title, cookie)

    history = _load_history()
    history.append(video_url.split("?")[0])
    _save_history(history)

    comments = await asyncio.to_thread(_fetch_comments, _bvid_of(video_url) or "")
    result = {
        "comments": comments,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keyword": keyword,
        "mood": tag,
        "title": title,
        "url": video_url,
        "desc": desc,
        "learned": learned,
    }
    try:
        pos = sum(1 for c in comments if any(w in c for w in ("哈", "笑", "好耶", "泪目", "牛", "绝")))
        neg = sum(1 for c in comments if any(w in c for w in ("烂", "差", "骗", "无语", "尬")))
        liked = pos >= neg
        _soul_adjust(-0.04 if liked else 0.02, -0.08 if liked else 0.05, 0.05 if liked else -0.03)
        _interest_bump(keyword, liked)
        _explore_mark()
    except Exception:  # noqa: BLE001
        pass
    _diary_append(result)
    # 把"正在看"写进去，聊天时会作为背景注入，让她接得上
    try:
        with io.open(_WATCHING, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "title": title,
                    "scene": (desc or "")[:200],
                    "detail": (desc or "")[:120],
                    "ts": time.time(),
                    "from": "browser_bili",
                },
                fh,
                ensure_ascii=False,
            )
    except Exception:  # noqa: BLE001
        pass
    return result


# ================= 学习：拉字幕 + 总结 =================

_VIDEO_NOTES = r"D:\AstrBot\video-notes.jsonl"
_WATCHING = r"D:\AstrBot\watching.json"
_DAILY_VIDEO_LIMIT = 10
_MAX_SUB_CHARS = 8000
_BILI_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


def _bvid_of(url: str) -> str | None:
    m = re.search(r"(BV[0-9A-Za-z]{10})", str(url or ""))
    return m.group(1) if m else None


def _http_json(url: str, timeout: int = 20):
    import urllib.request

    req = urllib.request.Request(url, headers=_BILI_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _fetch_subtitle(bvid: str, cookie: str = "") -> tuple[str, str]:
    """返回 (标题, 字幕全文)；拿不到字幕返回 ('', '')。需要登录 Cookie 才能拿到 AI 字幕。"""
    try:
        headers = dict(_BILI_UA)
        if cookie:
            headers["Cookie"] = cookie

        def _get(url: str):
            import urllib.request

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))

        view = _get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        data = view.get("data") or {}
        aid, cid = data.get("aid"), data.get("cid")
        title = data.get("title") or ""
        if not aid or not cid:
            return title, ""
        player = _get(f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}")
        subs = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        if not subs:
            return title, ""
        # 优先中文
        subs.sort(key=lambda s: 0 if str(s.get("lan", "")).startswith("zh") else 1)
        url = subs[0].get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return title, ""
        sub = _get(url)
        body = sub.get("body") or []
        text = "".join(str(seg.get("content") or "") for seg in body)
        return title, text[:_MAX_SUB_CHARS]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"拉字幕失败 {bvid}: {exc}")
        return "", ""


def _deepseek_cfg() -> tuple[str, str, str]:
    try:
        with io.open(r"D:\AstrBot\bot\data\cmd_config.json", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        key = api = model = ""
        for src in cfg.get("provider_sources", []):
            if src.get("id") == "deepseek_source":
                keys = src.get("key") or []
                key = keys[0] if keys else ""
                api = src.get("api_base") or "https://api.deepseek.com/v1"
        for p in cfg.get("provider", []):
            if p.get("id") == "deepseek":
                model = p.get("model") or "deepseek-chat"
        return key, api, model
    except Exception:  # noqa: BLE001
        return "", "", ""


def _today_learned() -> int:
    try:
        n = 0
        today = datetime.now().strftime("%Y-%m-%d")
        with io.open(_VIDEO_NOTES, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if str(item.get("ts", "")).startswith(today) and item.get("summary"):
                    n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def _learn_video(url: str, title: str, cookie: str = "") -> list[str]:
    """拉字幕 → 让 DeepSeek 总结成 3~5 条，写进 video-notes.jsonl。"""
    try:
        if _today_learned() >= _DAILY_VIDEO_LIMIT:
            logger.info("今天学习额度已满，跳过")
            return []
        bvid = _bvid_of(url)
        if not bvid:
            return []
        real_title, text = _fetch_subtitle(bvid, cookie)
        if len(text) < 200:
            logger.info(f"{bvid} 没有可用字幕，跳过学习")
            return []
        title = real_title or title

        key, api, model = _deepseek_cfg()
        if not key:
            return []

        prompt = (
            f"下面是 B 站视频《{title}》的字幕。\n"
            "用第一人称写 3~5 条「我看完记住了什么」：每条一句话，口语，具体，"
            "讲清楚谁做了什么、为什么、结论是什么。不要空话套话。\n"
            '只输出 JSON 字符串数组，例如 ["...","..."]，不要多余文字。\n\n'
            + text
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个认真做笔记的人，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 600,
        }
        import urllib.request

        req = urllib.request.Request(
            api.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        try:
            summary = json.loads(raw)
        except Exception:  # noqa: BLE001
            summary = [ln.strip("-• ") for ln in raw.splitlines() if ln.strip()][:5]
        if not isinstance(summary, list):
            summary = [str(summary)]
        summary = [str(s).strip() for s in summary if str(s).strip()][:5]

        with io.open(_VIDEO_NOTES, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "bvid": bvid,
                        "title": title,
                        "url": url,
                        "chars": len(text),
                        "summary": summary,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        logger.info(f"学完一个视频 {bvid}，笔记 {len(summary)} 条")
        return summary
    except Exception:  # noqa: BLE001
        logger.exception("学习视频失败")
        return []


def _match_video_notes(text: str, k: int = 2) -> list[str]:
    try:
        if not os.path.exists(_VIDEO_NOTES):
            return []
        low = (text or "").lower()
        grams = {low[i : i + 2] for i in range(max(0, len(low) - 1))}
        scored = []
        with io.open(_VIDEO_NOTES, encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                title = str(item.get("title") or "")
                summary = item.get("summary") or []
                blob = title + "".join(summary)
                score = sum(1 for g in grams if g and g in blob.lower())
                if score >= 2:
                    scored.append((score, item.get("ts", ""), title, summary))
        scored.sort(key=lambda x: (-x[0], str(x[1])), reverse=False)
        out = []
        for _, _, title, summary in scored[:k]:
            if summary:
                out.append(f"《{title}》" + "；".join(summary[:3]))
        return out
    except Exception:  # noqa: BLE001
        return []


def urllib_quote(s: str) -> str:
    import urllib.parse

    return urllib.parse.quote(str(s or ""))

# ================= A+C+E 改造：评论区 / 预算 / 心情联动 =================

def _fetch_comments(bvid: str, limit: int = 15) -> list[str]:
    """匿名拉评论区，拿不到就返回空列表。"""
    try:
        info = _http_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        aid = ((info.get("data") or {}).get("aid")) or 0
        if not aid:
            return []
        data = _http_json(
            f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps=20&pn=1"
        )
        replies = ((data.get("data") or {}).get("replies") or [])[: limit * 2]
        out = []
        for r in replies:
            msg = ((r.get("content") or {}).get("message") or "").strip()
            msg = re.sub(r"\s+", " ", msg)
            if len(msg) < 4:
                continue
            out.append(f"[{r.get('like', 0)}赞] {msg[:60]}")
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


_SOUL_STATE = r"D:\AstrBot\bot\data\plugin_data\dafeiyu_soul\state.json"
_INTERESTS = r"D:\AstrBot\bili-interests.json"
_EXPLORE_LOG = r"D:\AstrBot\bili-explore-log.json"


_SOUL_DELTA = r"D:\AstrBot\soul-delta.json"
_HEART_DELTA = r"D:\AstrBot\heart-delta.json"


def _soul_adjust(d_curiosity: float, d_boredom: float, d_mood: float) -> None:
    """直接合并进 soul 的 state.json（soul 每次请求前会 refresh_state 重读）。"""
    try:
        with io.open(_SOUL_STATE, encoding="utf-8-sig") as fh:
            s = json.load(fh)
        s["curiosity"] = max(0.0, min(1.0, float(s.get("curiosity", 0.6)) + d_curiosity))
        s["boredom"] = max(0.0, min(1.0, float(s.get("boredom", 0.3)) + d_boredom))
        s["mood"] = max(0.0, min(1.0, float(s.get("mood", 0.6)) + d_mood))
        s["updated_ts"] = time.time()
        with io.open(_SOUL_STATE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
        logger.info(f"browser_bili: 心情联动 curiosity{d_curiosity:+.2f} boredom{d_boredom:+.2f} mood{d_mood:+.2f}")
    except Exception:  # noqa: BLE001
        pass
    # 顺便给 heart 一份（它管的是"心情文字"和精力）
    try:
        with io.open(_HEART_DELTA, "w", encoding="utf-8") as fh:
            json.dump(
                {"mood_score": d_mood * 1.2, "energy": -0.02 if d_boredom < 0 else 0.01, "ts": time.time()},
                fh,
                ensure_ascii=False,
            )
    except Exception:  # noqa: BLE001
        pass


def _interest_bump(keyword: str, liked: bool) -> None:
    try:
        data = {}
        if os.path.exists(_INTERESTS):
            with io.open(_INTERESTS, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        k = (keyword or "").strip()
        if not k:
            return
        cur = float(data.get(k, 0)) + (1.0 if liked else -0.5)
        data[k] = max(-2.0, min(5.0, cur))  # 单个主题封顶 5 分
        with io.open(_INTERESTS, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _explore_budget() -> tuple[bool, str]:
    """E：每天最多 4 次，间隔至少 3 小时。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with io.open(_EXPLORE_LOG, encoding="utf-8") as fh:
            d = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        d = {}
    if d.get("date") != today:
        d = {"date": today, "count": 0, "last": 0}
    if int(d.get("count", 0)) >= 4:
        return False, "today full"
    if time.time() - float(d.get("last", 0)) < 3 * 3600:
        return False, "too soon"
    return True, ""


def _explore_mark() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with io.open(_EXPLORE_LOG, encoding="utf-8") as fh:
            d = json.load(fh) or {}
    except Exception:  # noqa: BLE001
        d = {}
    if d.get("date") != today:
        d = {"date": today, "count": 0}
    d["count"] = int(d.get("count", 0)) + 1
    d["last"] = time.time()
    try:
        with io.open(_EXPLORE_LOG, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
