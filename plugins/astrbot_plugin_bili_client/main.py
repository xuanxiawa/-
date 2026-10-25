"""用桌面版哔哩哔哩客户端刷视频。

- 定位客户端窗口（标题含「哔哩哔哩」，排除 Edge）
- 截图用 PrintWindow，不需要抢焦点也能看到画面
- 搜索/点卡片用真实鼠标键盘（会抢焦点），只在窗口能切到前台时执行
- 客户端不认 deep link，所以只能模拟点搜索框 + 粘贴关键词 + 回车
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import random
import time
from ctypes import wintypes
from datetime import datetime

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

_EXE = r"D:\资源\billbill\bilibili\哔哩哔哩.exe"
_SHOTS = r"D:\AstrBot\bili-client-shots"
_DIARY = r"D:\AstrBot\bili-client-diary.jsonl"
_VISION_CFG = r"D:\AstrBot\bot\data\config\astrbot_plugin_screen_eye_config.json"
_SOUL_STATE = r"D:\AstrBot\bot\data\plugin_data\dafeiyu_soul\state.json"

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)

# 让进程感知 DPI，否则窗口坐标和鼠标坐标会对不上（125% 缩放时点偏）
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


class _BIH(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def find_window():
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _l):
        name = ctypes.create_unicode_buffer(256)
        _u.GetWindowTextW(hwnd, name, 256)
        if _u.IsWindowVisible(hwnd) and "哔哩哔哩" in name.value and "Edge" not in name.value:
            found.append(hwnd)
        return True

    _u.EnumWindows(_cb, 0)
    return found[0] if found else None


def window_rect(hwnd):
    r = wintypes.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def capture(hwnd) -> "object":
    from PIL import Image

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
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
    if img.width > 1280:
        ratio = 1280 / img.width
        img = img.resize((1280, int(img.height * ratio)))
    return img


def describe(img, prompt: str) -> str:
    try:
        with io.open(_VISION_CFG, encoding="utf-8-sig") as fh:
            key = (json.load(fh).get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        key = ""
    if not key:
        return "（没配视觉模型）"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    payload = {
        "model": "glm-4v-flash",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0.2,
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
            return json.loads(resp.read().decode("utf-8", "ignore"))["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        return f"（看图失败：{exc}）"


def focus(hwnd) -> bool:
    """把窗口切到前台。Windows 会阻止后台进程抢焦点，这里用 AttachThreadInput 绕过。"""
    try:
        fg = _u.GetForegroundWindow()
        if fg == hwnd:
            return True
        tid_fg = _u.GetWindowThreadProcessId(fg, None)
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        _u.AttachThreadInput(tid_me, tid_fg, True)
        _u.ShowWindow(hwnd, 9)
        _u.BringWindowToTop(hwnd)
        _u.SetForegroundWindow(hwnd)
        _u.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.3)
        return _u.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def click_rel(hwnd, rx: float, ry: float):
    left, top, right, bottom = window_rect(hwnd)
    x = int(left + (right - left) * max(0.0, min(1.0, rx)))
    y = int(top + (bottom - top) * max(0.0, min(1.0, ry)))
    _u.SetCursorPos(x, y)
    time.sleep(0.15)
    _u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    _u.mouse_event(0x0004, 0, 0, 0, 0)
    return x, y


def paste_text(text: str):
    """用剪贴板 + Ctrl+V 输入中文（比逐字 SendInput 稳）。"""
    try:
        import tkinter

        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
    except Exception:  # noqa: BLE001
        return False
    time.sleep(0.2)
    _u.keybd_event(0x11, 0, 0, 0)
    _u.keybd_event(0x56, 0, 0, 0)
    time.sleep(0.05)
    _u.keybd_event(0x56, 0, 2, 0)
    _u.keybd_event(0x11, 0, 2, 0)
    return True


def press_enter():
    _u.keybd_event(0x0D, 0, 0, 0)
    time.sleep(0.05)
    _u.keybd_event(0x0D, 0, 2, 0)


def launch_client():
    if find_window():
        return True
    try:
        import subprocess

        subprocess.Popen([r"C:\Windows\explorer.exe", _EXE])
        for _ in range(30):
            time.sleep(1)
            if find_window():
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def pick_keyword() -> str:
    try:
        s = json.load(io.open(_SOUL_STATE, encoding="utf-8-sig"))
        boredom = float(s.get("boredom") or 0)
        curiosity = float(s.get("curiosity") or 0)
    except Exception:  # noqa: BLE001
        boredom, curiosity = 0, 0
    if boredom >= 0.6:
        pool = ["搞笑", "整活", "沙雕", "抽象"]
    elif curiosity >= 0.75:
        pool = ["科普", "冷知识", "科技", "纪录片"]
    else:
        pool = ["游戏", "动漫", "音乐", "猫"]
    return random.choice(pool)


def search_and_open(keyword: str) -> dict | None:
    hwnd = find_window() or (find_window() if launch_client() else None)
    if not hwnd:
        return None
    if not focus(hwnd):
        return None
    time.sleep(1.0)
    # 搜索框在右上角（1186×734 的窗口里大约是 0.86, 0.065）
    click_rel(hwnd, 0.86, 0.065)
    time.sleep(0.8)
    if not paste_text(keyword):
        return None
    time.sleep(0.6)
    press_enter()
    time.sleep(4.0)
    # 搜索结果页：点第一张卡片（左上那格）
    click_rel(hwnd, 0.22, 0.31)
    time.sleep(9)
    img = capture(hwnd)
    desc = describe(
        img,
        "这是哔哩哔哩电脑客户端的视频播放页。用中文说明：视频标题是什么、画面在演什么、"
        "播放进度大概多少、弹幕在说什么。100 字以内，抓重点。",
    )
    # 顺手把截图存下来
    os.makedirs(_SHOTS, exist_ok=True)
    path = os.path.join(_SHOTS, f"watch_{int(time.time())}.jpg")
    img.save(path, quality=80)
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keyword": keyword,
        "desc": desc,
        "shot": path,
    }
    try:
        with io.open(_DIARY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return rec


@register("bili_client", "dafeiyu", "用桌面版 B 站客户端刷视频（模拟点击）", "1.0.0")
class BiliClient(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}

    @llm_tool(name="bili_client_look")
    async def look(self, event: AstrMessageEvent) -> str:
        """看一眼 B 站电脑客户端现在是什么页面、在播什么。不抢焦点。"""
        def _run():
            hwnd = find_window()
            if not hwnd:
                return "B 站客户端没开。"
            img = capture(hwnd)
            return describe(img, "这是哔哩哔哩电脑客户端。一句话说明当前页面、标题、有没有视频在播。")

        return await asyncio.to_thread(_run)

    @llm_tool(name="bili_client_watch")
    async def watch(self, event: AstrMessageEvent, keyword: str) -> str:
        """在 B 站客户端里搜一个词，点开第一个视频看一会儿，回来说说看到了什么。

        Args:
            keyword(string): 想看的主题，比如「泰拉瑞亚」「整活」「冷知识」
        """
        def _run():
            rec = search_and_open(keyword)
            if not rec:
                return "没操作成功：客户端没开、或者窗口切不到前台。"
            return f"在客户端搜了「{keyword}」，点开第一个视频看了会儿：{rec['desc']}"

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.exception("bili_client_watch failed")
            return f"操作失败：{exc}"

    @llm_tool(name="bili_client_click")
    async def click(self, event: AstrMessageEvent, x: str, y: str) -> str:
        """在 B 站客户端窗口里点击，坐标是 0~1 的相对位置。

        Args:
            x(string): 横向 0~1
            y(string): 纵向 0~1
        """
        def _run():
            hwnd = find_window()
            if not hwnd:
                return "B 站客户端没开。"
            if not focus(hwnd):
                return "窗口切不到前台，为了不误点别的窗口，这次不点。"
            pos = click_rel(hwnd, float(x), float(y))
            return f"已点击 {pos}"

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            return f"点击失败：{exc}"

    @filter.command("客户端刷")
    async def cmd_watch(self, event: AstrMessageEvent, keyword: str = ""):
        """发「/客户端刷 关键词」用桌面客户端看视频。"""
        kw = keyword.strip() or pick_keyword()
        rec = await asyncio.to_thread(search_and_open, kw)
        if not rec:
            yield event.plain_result("没操作成功：客户端没开或窗口切不到前台。")
            return
        yield event.chain_result([Plain(rec["desc"]), Image.fromFileSystem(rec["shot"])])
