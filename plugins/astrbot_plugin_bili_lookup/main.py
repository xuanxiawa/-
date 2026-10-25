"""B 站链接解析插件：把视频的基本信息、热评、弹幕读给 AI。

只做读取，不对 B 站做任何写操作（不发评论、不发弹幕）。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
import zlib

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_URL_RE = re.compile(r"https?://[^\s，。；！？、）)】\"']+", re.IGNORECASE)


def _http_get(url: str, raw: bool = False, timeout: int = 15):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if raw:
        return data
    return json.loads(data.decode("utf-8", "ignore"))


def _expand_short_link(url: str) -> str:
    if "b23.tv" not in url:
        return url
    try:
        req = urllib.request.Request(url, headers=_HEADERS, method="GET")
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.geturl()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"b23 短链展开失败: {exc}")
        return url


def _extract_bvid(text: str) -> str | None:
    m = _BV_RE.search(text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]bvid=(BV[0-9A-Za-z]{10})", text)
    return m.group(1) if m else None


def _danmaku(cid: int, limit: int = 40) -> list[str]:
    try:
        raw = _http_get(f"https://api.bilibili.com/x/v1/dm/list.so?oid={cid}", raw=True)
        try:
            xml = zlib.decompress(raw, -15).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            xml = raw.decode("utf-8", "ignore")
        items = re.findall(r"<d [^>]*>(.*?)</d>", xml)
        clean = []
        for text in items:
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in clean:
                clean.append(text)
            if len(clean) >= limit:
                break
        return clean
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"弹幕获取失败: {exc}")
        return []


def _comments(aid: int, limit: int = 5) -> list[str]:
    try:
        data = _http_get(
            f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps={limit}"
        )
        replies = (data.get("data") or {}).get("replies") or []
        out = []
        for reply in replies[:limit]:
            name = (reply.get("member") or {}).get("uname", "?")
            msg = ((reply.get("content") or {}).get("message") or "").replace("\n", " ")
            if msg:
                out.append(f"{name}: {msg[:80]}")
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"评论获取失败: {exc}")
        return []


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _lookup(text: str) -> str:
    url = None
    m = _URL_RE.search(text)
    if m:
        url = m.group(0)
    elif "bilibili.com" in text:
        url = text.strip()

    bvid = None
    if url:
        bvid = _extract_bvid(_expand_short_link(url))
    if not bvid:
        bvid = _extract_bvid(text)
    if not bvid:
        return "没找到 BV 号。请把完整的 B 站视频链接再发一次。"

    view = _http_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
    if view.get("code") != 0:
        return f"B 站接口返回错误：{view.get('message')}（BV 号 {bvid}）"
    data = view["data"]

    title = data.get("title", "")
    owner = (data.get("owner") or {}).get("name", "")
    desc = (data.get("desc") or "").replace("\n", " ")[:300]
    stat = data.get("stat") or {}
    cid = data.get("cid")
    aid = data.get("aid")

    lines = [
        f"标题：{title}",
        f"UP主：{owner}｜时长：{_fmt_duration(data.get('duration'))}｜"
        f"播放：{stat.get('view')}｜点赞：{stat.get('like')}｜弹幕：{stat.get('danmaku')}",
        f"简介：{desc or '（无）'}",
    ]
    if data.get("tname"):
        lines.append(f"分区：{data['tname']}")

    comments = _comments(aid) if aid else []
    if comments:
        lines.append("热评：\n" + "\n".join(f"- {c}" for c in comments))

    dan = _danmaku(cid) if cid else []
    if dan:
        lines.append("弹幕样本：" + " ".join(dan[:25]))

    return "\n".join(lines)


@register(
    "bili_lookup",
    "dafeiyu",
    "读取 B 站视频信息（标题/简介/热评/弹幕），只读不改",
    "1.0.0",
)
class BiliLookup(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @llm_tool(name="bili_video_info")
    async def bili_video_info(self, event: AstrMessageEvent, url: str) -> str:
        """读取 B 站视频信息：标题、UP 主、时长、播放量、简介、热评、弹幕样本。

        当用户发来 B 站链接（bilibili.com/video/BV... 或 b23.tv 短链）或者直接说
        某个 BV 号时，调用这个工具拿到视频内容，再用自己的口吻跟用户聊，不要照抄标题。
        这是只读工具，不要声称自己去点赞、投币、发评论或发弹幕。

        Args:
            url(string): 用户发的 B 站链接、短链，或 BV 号（如 BV1pMJv6WEmr）
        """
        try:
            return await asyncio.to_thread(_lookup, url or "")
        except urllib.error.URLError as exc:
            logger.warning(f"B 站请求失败: {exc}")
            return f"网络请求失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("B 站解析出错")
            return f"解析失败：{exc}"
