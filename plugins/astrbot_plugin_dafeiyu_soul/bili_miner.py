"""从 B 站热评里挖梗。

流程：
1. 拉热榜视频（或直接拿指定 BV）
2. 取每条的置顶/热门评论
3. 交给模型抽「网络梗 + 含义」，过滤掉已知的
4. 写回 memes_learned.json（source = bili_hot）

只读取 B 站公开接口，不做任何写操作。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from pathlib import Path

import aiohttp

from astrbot.api import logger

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_EXTRACT_PROMPT = """你是一个中文网络梗词典编辑。下面是从 B 站热门视频评论区抓到的一批评论。

请从中挑出真正的「网络梗 / 黑话 / 流行句式」，给出简短准确的含义，并摘一句用法示例。
要求：
- 只收确实在当下网络语境里有特定含义的（比如「绷不住了」「典」「手搓」），不要收普通词语
- 含义 20 字以内，示例直接引用评论原文（可截断）
- 最多 12 条，宁缺毋滥
只输出 JSON：
{"memes":[{"term":"","meaning":"","example":""}]}
"""


def _http_json(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def popular_videos(limit: int = 6) -> list[dict]:
    """B 站综合热门。"""
    out = []
    try:
        data = _http_json(
            "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
        )
        for item in (data.get("data") or {}).get("list") or []:
            out.append({"aid": item.get("aid"), "title": item.get("title", "")})
            if len(out) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"热榜获取失败: {exc}")
    return out


def hot_comments(aid: int, limit: int = 12) -> list[str]:
    """视频热评（按热度排序）。"""
    try:
        data = _http_json(
            f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps={limit}"
        )
        replies = (data.get("data") or {}).get("replies") or []
        texts = []
        for r in replies:
            msg = ((r.get("content") or {}).get("message") or "").strip()
            msg = re.sub(r"\s+", " ", msg)
            if msg and len(msg) <= 120:
                texts.append(msg)
        return texts
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"热评获取失败: {exc}")
        return []


def _llm_target() -> tuple[str, str, str]:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    cfg_path = Path(get_astrbot_data_path()) / "cmd_config.json"
    try:
        cfg = json.loads(cfg_path.read_text("utf-8-sig"))
        sources = {s.get("id"): s for s in cfg.get("provider_sources", [])}
        for provider in cfg.get("provider", []):
            src = sources.get(provider.get("provider_source_id")) or {}
            base = src.get("api_base") or ""
            keys = src.get("key") or []
            if base and keys and "deepseek" in base:
                return base.rstrip("/"), keys[0], provider.get("model") or "deepseek-chat"
    except Exception:  # noqa: BLE001
        pass
    return "", "", ""


async def _extract_memes(samples: list[str]) -> list[dict]:
    base, key, model = _llm_target()
    if not base or not key or not samples:
        return []
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EXTRACT_PROMPT},
            {"role": "user", "content": "\n".join(f"- {s}" for s in samples)},
        ],
        "temperature": 0.3,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{base}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            ) as resp:
                data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        obj = json.loads(content)
        memes = obj.get("memes") or []
        return [m for m in memes if isinstance(m, dict) and m.get("term")]
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"挖梗模型调用失败: {exc}")
        return []


async def mine_once(soul) -> int:
    """跑一轮挖梗，返回新增数量。"""
    videos = await asyncio.to_thread(popular_videos, 6)
    if not videos:
        return 0

    samples: list[str] = []
    for v in videos:
        aid = v.get("aid")
        if not aid:
            continue
        comments = await asyncio.to_thread(hot_comments, aid, 10)
        samples.extend(comments)
        if len(samples) >= 60:
            break

    if not samples:
        return 0

    memes = await _extract_memes(samples[:60])
    known = {str(m.get("term", "")).strip() for m in soul.memes}
    added = 0
    today = time.strftime("%Y-%m-%d")
    for m in memes:
        term = str(m.get("term", "")).strip()
        meaning = str(m.get("meaning", "")).strip()
        if not term or not meaning or term in known:
            continue
        soul.memes.append(
            {
                "term": term,
                "meaning": meaning[:60],
                "example": str(m.get("example", ""))[:80],
                "source": "bili_hot",
                "date": today,
            }
        )
        known.add(term)
        added += 1

    if added:
        soul.memes = soul.memes[-200:]
        soul.save()
        logger.info(f"[挖梗] 从 B 站热评新增 {added} 个梗，共 {len(soul.memes)} 个")
    return added


async def background_loop(soul) -> None:
    """启动后先等一会儿，之后每 8 小时挖一次。"""
    await asyncio.sleep(120)
    while True:
        try:
            if len(soul.memes) < 120:
                await mine_once(soul)
        except Exception:  # noqa: BLE001
            logger.warning("[挖梗] 后台任务异常", exc_info=True)
        await asyncio.sleep(8 * 3600)
