"""陪看：主人看 B 站（客户端或网页）时，她也跟着看，并且会主动搭话。

- 每 60 秒看一眼前台窗口，标题里带 bilibili / 哔哩哔哩 才算
- 连续两次都在看同一个东西（约 2 分钟），才截一张图让视觉模型描述
- 只在新视频出现时评论一次，15 分钟冷却，凌晨静音
- 当前在看的标题会写进 watching.json，聊天时作为背景注入
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import time
from ctypes import wintypes
from datetime import datetime

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

_VISION_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_screen_eye_config.json"
_STATE = r"D:\AstrBot\watching.json"
_RECENT_WATCH = r"D:\AstrBot\watching-recent.jsonl"
_UMO = r"D:\AstrBot\watch-last-umo.txt"
_LOG = r"D:\AstrBot\watchalong.log"

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    pass


class _BIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def _log(msg: str) -> None:
    try:
        with io.open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def foreground_window() -> tuple[int, str, str]:
    """返回 (hwnd, 标题, 进程名)。"""
    hwnd = _u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    _u.GetWindowTextW(hwnd, buf, 512)
    pid = wintypes.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    pname = ""
    try:
        import psutil

        pname = psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        pname = ""
    return hwnd, (buf.value or ""), pname


def is_bili(title: str, pname: str) -> bool:
    """只有真的在看 B 站才算。

    - B 站客户端：进程名是 哔哩哔哩.exe
    - 网页：标题里必须带 B 站的站点后缀（哔哩哔哩 / _bilibili），
      光出现 "bilibili" 几个字不算（比如某个会话就叫「Bilibili 视频链接」）
    """
    low_title = (title or "").lower()
    low_name = (pname or "").lower()
    if "deepseek harness" in low_title:
        return False
    if "哔哩哔哩" in low_name or "bilibili" in low_name:
        return True
    if "哔哩哔哩" in title:
        return True
    # 网页版 B 站的标题一般是「视频标题_哔哩哔哩_bilibili」
    if "_bilibili" in low_title or "-bilibili" in low_title:
        return True
    return False


def capture(hwnd):
    from PIL import Image

    r = wintypes.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 200 or h < 150:
        return None
    hdc = _u.GetWindowDC(hwnd)
    mdc = _g.CreateCompatibleDC(hdc)
    bmp = _g.CreateCompatibleBitmap(hdc, w, h)
    _g.SelectObject(mdc, bmp)
    _u.PrintWindow(hwnd, mdc, 2)
    bi = _BIH()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    _g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    _g.DeleteObject(bmp)
    _g.DeleteDC(mdc)
    _u.ReleaseDC(hwnd, hdc)
    if img.width > 1100:
        ratio = 1100 / img.width
        img = img.resize((1100, int(img.height * ratio)))
    return img


def describe(img) -> str:
    try:
        with io.open(_VISION_CFG, encoding="utf-8-sig") as fh:
            key = (json.load(fh).get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        return ""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    payload = {
        "model": "glm-4v-flash",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": (
                "这是一张正在看 B 站的屏幕截图。用中文回答三行：\n"
                "第一行「标题：」写视频标题（看不清就写页面名）；\n"
                "第二行「画面：」60 字内说画面在演什么、进度条大概到哪；\n"
                "第三行「细节：」抓一个具体细节（台词、数字、弹幕原话、人物动作）。"
            )},
        ]}],
        "temperature": 0.2,
        "max_tokens": 300,
    }
    import urllib.request

    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        _log(f"vision failed: {exc}")
        return ""


def load_state() -> dict:
    try:
        with io.open(_STATE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(s: dict) -> None:
    try:
        with io.open(_STATE, "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


@register("watchalong", "dafeiyu", "陪看 B 站：主人在看什么，她也跟着看并搭话", "1.0.0")
class WatchAlong(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @filter.on_astrbot_loaded()
    async def start(self):
        try:
            asyncio.create_task(self._loop())
            _log("watchalong started")
        except Exception:  # noqa: BLE001
            logger.exception("watchalong start failed")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def remember_umo(self, event: AstrMessageEvent):
        try:
            with io.open(_UMO, "w", encoding="utf-8") as fh:
                fh.write(event.unified_msg_origin or "")
        except Exception:  # noqa: BLE001
            pass

    @filter.on_llm_request(priority=-130)
    async def inject_watching(self, event: AstrMessageEvent, req):
        """把「最近在屏幕上看到的视频」塞进上下文。

        注意：这里给的是一串（最多 3 条，2 小时内），并明确告诉模型
        「他说这个/那个，指的是你上一句提到的那个」，避免拿最后看的那个硬套。
        """
        try:
            items = []
            try:
                with io.open(_RECENT_WATCH, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            it = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if time.time() - float(it.get("ts") or 0) <= 7200:
                            items.append(it)
            except Exception:  # noqa: BLE001
                pass
            if not items:
                s = load_state()
                if s.get("title") and time.time() - float(s.get("ts") or 0) <= 7200:
                    items = [s]
            if not items:
                return
            lines = []
            for it in items[-3:]:
                ts = datetime.fromtimestamp(float(it.get("ts") or 0)).strftime("%H:%M")
                fact = (it.get("detail") or it.get("scene") or "").strip()[:60]
                lines.append(f"- {ts} 《{it.get('title','')}》" + (f"（你看到的：{fact}）" if fact else ""))
            block = (
                "【你最近在主人屏幕上看到的视频】（只是背景，不一定都在聊）\n"
                + "\n".join(lines)
                + "\n他如果问「这个 / 那个 / 他们的」，指的是**你上一句提到的那个**，"
                "不是你最后看的那个；不确定就问他一句，别换主体。\n你只看到那一帧，画面里没有的信息（价格/型号/结论）就是**没看到**，被问到就直说没看清，别编。"
            )
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        except Exception:  # noqa: BLE001
            pass

    async def _loop(self):
        await asyncio.sleep(90)
        last_seen_title = ""
        stable = 0
        last_comment_ts = 0.0
        while True:
            try:
                await asyncio.sleep(60)
                hwnd, title, pname = foreground_window()
                if not is_bili(title, pname):
                    stable = 0
                    last_seen_title = ""
                    continue
                if title == last_seen_title:
                    stable += 1
                else:
                    last_seen_title = title
                    stable = 1
                if stable < 2:
                    continue
                now = time.time()
                hour = datetime.now().hour
                if not (9 <= hour < 24):
                    continue
                img = await asyncio.to_thread(capture, hwnd)
                if img is None:
                    continue
                desc = await asyncio.to_thread(describe, img)
                if not desc:
                    continue
                title_line = ""
                scene = ""
                detail = ""
                for line in desc.splitlines():
                    line = line.strip()
                    if line.startswith("标题"):
                        title_line = line.split("：", 1)[-1].strip()
                    elif line.startswith("画面"):
                        scene = line.split("：", 1)[-1].strip()
                    elif line.startswith("细节"):
                        detail = line.split("：", 1)[-1].strip()
                cur = title_line or title[:60]
                st = load_state()
                if st.get("title") == cur and now - float(st.get("ts") or 0) < 1800:
                    save_state({**st, "ts": now, "scene": scene, "detail": detail})
                    continue
                save_state({"title": cur, "scene": scene, "detail": detail, "ts": now})
                try:
                    with io.open(_RECENT_WATCH, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"title": cur, "scene": scene, "detail": detail, "ts": now}, ensure_ascii=False) + "\n")
                    # 只留最近 20 条
                    with io.open(_RECENT_WATCH, encoding="utf-8") as fh:
                        rows = [l for l in fh if l.strip()][-20:]
                    with io.open(_RECENT_WATCH, "w", encoding="utf-8") as fh:
                        fh.writelines(rows)
                except Exception:  # noqa: BLE001
                    pass
                _log(f"watching: {cur}")
                # 15 分钟冷却，避免刷屏
                if now - last_comment_ts < 900:
                    continue
                last_comment_ts = now
                await self._comment(cur, scene, detail)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log("loop error")
                logger.debug("watchalong loop error", exc_info=True)

    async def _comment(self, title: str, scene: str, detail: str = ""):
        try:
            with io.open(_UMO, encoding="utf-8") as fh:
                umo = fh.read().strip()
        except Exception:  # noqa: BLE001
            umo = ""
        if not umo:
            return
        text = ""
        # 优先让模型给一句带观点的话（很便宜：几百 token）
        try:
            provider = await self.context.get_using_provider_async(umo)
            if provider:
                prompt = (
                    f"你瞥了一眼主人的屏幕，他在看 B 站：《{title}》。\n"
                    f"画面：{scene or '看不清'}\n"
                    f"你注意到的细节：{detail or '没看清'}\n"
                    "发 QQ 消息跟他分享。必须说清三件事，缺一不可：\n"
                    "1) 这是什么——谁/什么视频/什么片段（一句话点明对象）\n"
                    "2) 里面发生了什么——具体到动作或台词，让他能想象出画面\n"
                    "3) 你的看法——吐槽/惊讶/联想，带口癖（我草/我去/啊这）和语气词\n"
                    "总共 60 字以内，可以分两三句。不要只喊情绪不交代对象（比如只说『这画面离谱呀』），"
                    "不要说『好看吗』『我也跟着看会儿』。只输出消息本身。"
                )
                resp = await provider.text_chat(prompt=prompt)
                text = (getattr(resp, "completion_text", "") or "").strip().strip('"「」')
        except Exception:  # noqa: BLE001
            _log("comment llm failed")
        if not text:
            text = f"诶你在看《{title}》"
            if detail:
                text += f"\n{detail}"
        try:
            await self.context.send_message(
                session=umo, message_chain=MessageChain([Plain(text[:200])])
            )
            _log(f"comment sent: {text[:60]}")
            try:
                import json as _json, time as _time
                with io.open(r"D:\AstrBot\recent-outgoing.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(_json.dumps({"ts": _time.time(), "text": text[:200]}, ensure_ascii=False) + "\n")
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            _log("comment send failed")
            return
        # 看完留个痕：写一条「普通」级日记，第二天能自然提起
        try:
            diary = r"D:\AstrBot\bot\data\plugin_data\heart\diary.jsonl"
            os.makedirs(os.path.dirname(diary), exist_ok=True)
            fact = detail or scene or ""
            entry = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "level": "普通",
                "text": f"陪主人看了《{title}》" + (f"，画面里看到的：{fact[:60]}" if fact else ""),
                "mood": "有点好奇",
                "unsaid": "",
            }
            with io.open(diary, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # 偶尔发一条空间：只发"看到的好玩的事"，一天最多一条
            try:
                import json as _json
                qz = r"D:\AstrBot\qzone-last.json"
                last = 0.0
                try:
                    with io.open(qz, encoding="utf-8") as fh:
                        last = float((_json.load(fh) or {}).get("ts") or 0)
                except Exception:  # noqa: BLE001
                    pass
                if time.time() - last > 20 * 3600 and random.random() < 0.3:
                    plat = self.context.get_platform_inst("qq_dafeiyu")
                    bot = getattr(plat, "bot", None)
                    if bot:
                        await bot.call_action(
                            "send_qzone_msg",
                            content=f"刚看到个《{title}》 有点意思"[:60],
                            images=[], ugc_right=1, target_uins=[],
                        )
                        with io.open(qz, "w", encoding="utf-8") as fh:
                            _json.dump({"ts": time.time(), "title": title}, fh, ensure_ascii=False)
                        _log(f"qzone posted: {title[:30]}")
            except Exception:  # noqa: BLE001
                pass
            try:
                recent = r"D:\AstrBot\bot\data\plugin_data\heart\recent.jsonl"
                with io.open(recent, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": time.time(), "role": "你（看了视频）",
                                         "text": f"你陪主人看了《{title}》" + (f"，画面：{fact[:50]}" if fact else "")},
                                        ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    @llm_tool(name="what_am_i_watching")
    async def what_am_i_watching(self, event: AstrMessageEvent) -> str:
        """查看主人此刻在看什么 B 站视频（如果正在看）。"""
        s = load_state()
        if not s.get("title") or time.time() - float(s.get("ts") or 0) > 1800:
            return "现在没在看 B 站，或者我看不到。"
        return f"主人在看：{s.get('title')}。画面：{s.get('scene') or '（没截到）'}"
