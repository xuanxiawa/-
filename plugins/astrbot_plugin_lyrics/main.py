"""歌词小工具：搜歌 + 取歌词（网易云公开接口）。"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from astrbot.api import llm_tool
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://music.163.com/",
}


def _get(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


@register("lyrics", "dafeiyu", "搜歌 + 歌词", "1.0.0")
class Lyrics(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @llm_tool(name="search_song")
    async def search_song(self, event: AstrMessageEvent, keyword: str) -> str:
        """搜一首歌，返回前几条结果（歌名 - 歌手 + 歌曲 id）。

        Args:
            keyword(string): 歌名或「歌名 歌手」
        """
        try:
            q = urllib.parse.quote(str(keyword or ""))
            data = _get(
                f"https://music.163.com/api/search/get/web?csrf_token=&s={q}&type=1&offset=0&total=true&limit=5"
            )
            songs = ((data.get("result") or {}).get("songs") or [])[:5]
            if not songs:
                return "没搜到诶。"
            lines = [f"{s.get('name')} - {', '.join(a.get('name','') for a in (s.get('artists') or []))} (id={s.get('id')})" for s in songs]
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"搜歌失败了：{exc}"

    @llm_tool(name="get_lyrics")
    async def get_lyrics(self, event: AstrMessageEvent, song_id: str) -> str:
        """按歌曲 id 取歌词（先用 search_song 拿 id）。

        Args:
            song_id(string): 歌曲 id
        """
        try:
            data = _get(f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1")
            text = ((data.get("lrc") or {}).get("lyric") or "").strip()
            if not text:
                return "这首没歌词诶。"
            lines = [ln for ln in text.splitlines() if ln.strip() and "[" not in ln[:2] or "]" in ln][:60]
            clean = []
            for ln in text.splitlines():
                if "]" in ln:
                    ln = ln.split("]", 1)[1].strip()
                if ln:
                    clean.append(ln)
                if len(clean) >= 60:
                    break
            return "\n".join(clean) if clean else text[:1500]
        except Exception as exc:  # noqa: BLE001
            return f"取歌词失败了：{exc}"
