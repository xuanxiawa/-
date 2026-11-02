"""QQ 视频的眼睛：抽帧 → 视觉模型描述 → 注入本轮提示词。

为什么需要这个插件
------------------
QQ（OneBot / aiocqhttp）发来的视频，到模型这边只剩一段本地文件路径：
  - group_chat_plus 会把消息写成 ``[视频: D:\\...\\media_video_xxx.mp4]``
  - AstrBot 核心会把附件写成 ``[Video Attachment: name xxx, path D:\\...]``
模型读不了 mp4，只能回「我只看见个文件路径」。

这个插件在每次模型请求前扫一遍提示词和历史，发现没看过的视频就用 ffmpeg
抽 4 帧，交给视觉模型（默认 zhipu_vision）描述，再把描述塞进本轮提示词。
结果按「文件路径 + 修改时间」缓存，同一个视频只花一次钱。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# 临时注入块：mark_as_temp 的内容只给模型看，不进对话历史
try:
    from astrbot.core.agent.message import TextPart as _TextPart
except Exception:  # noqa: BLE001
    _TextPart = None

# ---------------------------------------------------------------- 路径 / 常量

_FFMPEG_CANDIDATES = (
    Path(r"D:\大肥鱼\插件\ffmpeg\bin\ffmpeg.exe"),
    Path(r"C:\Users\HONOR\.pyffmpeg\bin\ffmpeg.exe"),
)
_DATA = Path(r"D:\AstrBot\bot\data\plugin_data\video_eye")
_FRAMES = _DATA / "frames"
_CACHE_FILE = _DATA / "cache.json"

VISION_PROVIDER_ID = "zhipu_vision"   # AstrBot 里配的视觉模型
FRAME_COUNT = 4                       # 抽几帧
FRAME_MAX_WIDTH = 900                 # 抽出来的帧宽
VISION_TIMEOUT = 45                   # 一次描述的等待上限（秒）
HOT_TAIL_MESSAGES = 3                 # 视频出现在最近几条消息里才算「正在聊」

_VIDEO_EXT = r"(?:mp4|mov|mkv|avi|flv|webm|m4v|wmv)"
_PATTERNS = (
    re.compile(rf"\[视频[:：]\s*([^\]\n]+?\.{_VIDEO_EXT})\]", re.I),
    re.compile(
        rf"\[Video Attachment(?: in quoted message)?:[^\]]*?path\s+([^\]\n]+?\.{_VIDEO_EXT})\]",
        re.I,
    ),
)
_ASK_WORDS = ("视频", "看看", "链接", "那段视频", "这个视频")

_DESC_PROMPT = (
    "这是同一段视频按时间先后抽的 {n} 帧（覆盖开头到结尾）。请用中文，"
    "只输出一段话：不要分点、不要重复、不要用「我刚看了个视频」这种开头。说清楚：\n"
    "1) 这是什么（游戏/动画/聊天录屏/实拍），画面里在发生什么；\n"
    "2) 画面上的关键文字（标题、字幕、界面提示、角色名）；\n"
    "3) 有角色或人物的话，是谁、在做什么。\n"
    "80-140 字，口语一点。看不清就直说看不清，不要编。"
)


# ---------------------------------------------------------------- 小工具


def _ffmpeg() -> str | None:
    for p in _FFMPEG_CANDIDATES:
        if p.is_file():
            return str(p)
    from shutil import which

    return which("ffmpeg")


def _normalize_path(raw: str) -> str:
    p = (raw or "").strip().strip("<>\"'")
    if p.lower().startswith("file:///"):
        p = p[8:]
    elif p.lower().startswith("file://"):
        p = p[7:]
    p = p.replace("/", "\\")
    return p


def _allowed_path(path: str) -> bool:
    """只处理 AstrBot 自己数据目录里的视频，别让群友写个路径就翻本机文件。"""
    try:
        if not os.path.exists(path):
            return False
        if os.path.getsize(path) > 400 * 1024 * 1024:
            return False
        root = str(_DATA.parent.parent).lower()  # D:\AstrBot\bot\data
        return os.path.abspath(path).lower().startswith(root)
    except OSError:
        return False


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for seg in content:
            if isinstance(seg, str):
                out.append(seg)
            elif isinstance(seg, dict):
                if seg.get("type") == "text":
                    out.append(str(seg.get("text") or ""))
                elif seg.get("type") == "image_url":
                    out.append("[图片]")
        return "".join(out)
    return str(content or "")


def _iter_texts(req: ProviderRequest):
    if isinstance(req.prompt, str) and req.prompt:
        yield req.prompt
    for m in req.contexts or []:
        if isinstance(m, dict):
            yield _text_of(m.get("content"))
    for part in req.extra_user_content_parts or []:
        text = getattr(part, "text", None)
        if text:
            yield str(text)


def _extract_paths(text: str) -> list[str]:
    found: list[str] = []
    for pat in _PATTERNS:
        for raw in pat.findall(text or ""):
            p = _normalize_path(raw)
            if p and p not in found:
                found.append(p)
    return found


def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess:    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _duration(ffmpeg: str, path: str) -> float:
    try:
        proc = _run([ffmpeg, "-hide_banner", "-i", path], timeout=30)
        text = (proc.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"video_eye: 读取时长失败 {path}: {exc}")
    return 0.0


def _timestamps(duration: float) -> list[float]:
    if duration >= 3:
        fractions = (0.06, 0.32, 0.60, 0.88)
        return [round(duration * f, 2) for f in fractions[:FRAME_COUNT]]
    return [0.2]


def _extract_frames(ffmpeg: str, path: str) -> tuple[list[str], float]:
    duration = _duration(ffmpeg, path)
    stem = hashlib.sha1(path.encode("utf-8", "ignore")).hexdigest()[:12]
    out_dir = _FRAMES / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    frames: list[str] = []
    for i, ts in enumerate(_timestamps(duration)):
        out = out_dir / f"{i}.jpg"
        try:
            if not out.exists() or out.stat().st_size < 1024:
                _run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        str(ts),
                        "-i",
                        path,
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale={FRAME_MAX_WIDTH}:-2",
                        "-q:v",
                        "3",
                        "-y",
                        str(out),
                    ],
                    timeout=60,
                )
            if out.exists() and out.stat().st_size >= 1024:
                frames.append(str(out))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"video_eye: 抽帧失败 {path} @{ts}: {exc}")

    # 只留最近 40 个视频的帧，别把磁盘堆满
    try:
        dirs = [p for p in _FRAMES.iterdir() if p.is_dir()]
        if len(dirs) > 50:
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in dirs[40:]:
                shutil.rmtree(old, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return frames, duration


def _clean_desc(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # 模型爱用「我刚看了个视频…」开头，塞进提示词里会像用户说的话，去掉
    text = re.sub(
        r"^我(刚|刚才)?看了(个|一个)?[^，。！？]{0,15}?(视频|片段)[，,。：:！!]?\s*",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text)
    return text[:260].strip()


# ---------------------------------------------------------------- 插件


@register(
    "video_eye",
    "dafeiyu",
    "QQ 视频的眼睛：ffmpeg 抽帧 + 视觉模型描述，让她真的看见视频",
    "1.0.0",
)
class VideoEye(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self._cache: dict = {}
        self._lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._load_cache()

    # ---------- 缓存 ----------
    def _load_cache(self) -> None:
        try:
            self._cache = json.loads(_CACHE_FILE.read_text("utf-8"))
            if not isinstance(self._cache, dict):
                self._cache = {}
        except Exception:  # noqa: BLE001
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            _DATA.mkdir(parents=True, exist_ok=True)
            tmp = _CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, _CACHE_FILE)
        except Exception:  # noqa: BLE001
            logger.debug("video_eye: 保存缓存失败", exc_info=True)

    # ---------- 描述一个视频 ----------
    async def _describe(self, path: str, umo: str) -> tuple[str, float]:
        # 群里连着几条消息一起来时，别把同一个视频描述两遍
        lock = self._locks[path]
        async with lock:
            return await self._describe_locked(path, umo)

    async def _describe_locked(self, path: str, umo: str) -> tuple[str, float]:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return "", 0.0

        cached = self._cache.get(path)
        if (
            isinstance(cached, dict)
            and cached.get("mtime") == mtime
            and cached.get("desc")
        ):
            return str(cached["desc"]), float(cached.get("dur") or 0.0)

        ffmpeg = _ffmpeg()
        if not ffmpeg:
            logger.warning("video_eye: 找不到 ffmpeg，跳过视频理解")
            return "", 0.0

        frames, duration = await asyncio.to_thread(_extract_frames, ffmpeg, path)
        if not frames:
            return "", 0.0

        provider = None
        try:
            provider = self.context.get_provider_by_id(VISION_PROVIDER_ID)
        except Exception:  # noqa: BLE001
            provider = None
        if provider is None:
            logger.warning(f"video_eye: 没找到视觉 provider {VISION_PROVIDER_ID}")
            return "", 0.0

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=_DESC_PROMPT.format(n=len(frames)),
                    image_urls=frames,
                ),
                timeout=VISION_TIMEOUT,
            )
            desc = _clean_desc(getattr(resp, "completion_text", "") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"video_eye: 视觉模型描述失败 {path}: {exc}")
            return "", 0.0

        if not desc:
            return "", 0.0

        async with self._lock:
            self._cache[path] = {
                "mtime": mtime,
                "desc": desc,
                "dur": round(duration, 1),
                "ts": time.time(),
            }
            # 缓存别无限长
            if len(self._cache) > 200:
                keep = sorted(
                    self._cache.items(),
                    key=lambda kv: float((kv[1] or {}).get("ts") or 0),
                    reverse=True,
                )[:150]
                self._cache = dict(keep)
            self._save_cache()
            # key 太多就丢掉没缓存视频的锁
            if len(self._locks) > 200:
                for k in list(self._locks):
                    if k not in self._cache:
                        self._locks.pop(k, None)
        logger.info(f"video_eye: 看完 {os.path.basename(path)} → {desc[:40]}…")
        return desc, duration

    # ---------- 请求前看一眼 ----------
    @filter.on_llm_request(priority=-100)
    async def look(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            prompt = req.prompt if isinstance(req.prompt, str) else ""
            texts = list(_iter_texts(req))
            paths: list[str] = []
            for t in texts:
                for p in _extract_paths(t):
                    if p not in paths:
                        paths.append(p)
            if not paths:
                return

            # 只在「正在聊这个视频」的时候注入，免得每轮都塞一遍
            tail = [prompt]
            tail += [_text_of(m.get("content")) for m in (req.contexts or [])[-HOT_TAIL_MESSAGES:]]
            tail += [
                str(getattr(part, "text", "") or "")
                for part in (req.extra_user_content_parts or [])
            ]
            tail_text = "\n".join(tail)
            # 统一用「解析出来的路径」比较，别被 file:/// 或斜杠方向坑了
            tail_paths: set[str] = set()
            for t in tail:
                tail_paths.update(_extract_paths(t))
            asking = any(w in prompt for w in _ASK_WORDS)

            umo = event.unified_msg_origin or ""
            # 只处理「最近在聊」的视频；问到「那个视频」时退一步看最近提到的那一个
            candidates = [p for p in paths if p in tail_paths]
            if not candidates and asking:
                candidates = paths[-1:]
            parts: list[str] = []
            for path in candidates:
                if len(parts) >= 2:
                    break
                if not _allowed_path(path):
                    logger.warning(f"video_eye: 视频不在 AstrBot 数据目录，跳过 {path}")
                    continue
                desc, dur = await self._describe(path, umo)
                if not desc:
                    continue
                if desc[:30] in prompt or desc[:30] in tail_text:
                    continue
                label = f"{os.path.basename(path)}（{int(dur)}秒）" if dur else os.path.basename(path)
                parts.append(f"- {label}: {desc}")

            if not parts:
                return
            block = (
                "【视频画面 · 插件替你抽帧看过了】\n"
                + "\n".join(parts)
                + "\n（这是真实画面内容，可以直接聊；别再说「只看见文件路径」。）"
            )
            # 挂临时块：只给这次请求看，不写进对话历史
            if _TextPart is not None:
                req.extra_user_content_parts.append(
                    _TextPart(text=block).mark_as_temp()
                )
            else:
                req.prompt = (prompt.rstrip() + "\n\n" + block) if prompt else block
            logger.info(
                f"video_eye: 已注入 {len(parts)} 个视频描述（{umo.split(':')[-1]}）"
            )
        except Exception:  # noqa: BLE001
            logger.warning("video_eye: 视频理解注入失败", exc_info=True)



