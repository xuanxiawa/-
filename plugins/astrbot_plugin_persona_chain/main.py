"""大肥鱼的人格版本链：她可以提，只有主人能批。

为什么单独做一层
----------------
底层人设是 personas 表里那段固定 prompt，改一次没有记录、也没法回滚。
这里在它之上加一层「她长出来的部分」：稳定的喜好、习惯、边界，每条都带证据
（哪天的日记/哪句话），改动留版本，能回滚，红线逐字固定、她自己删不掉。

三条硬规矩
----------
1. 她只能写草稿（propose_persona），草稿不进 prompt、不算数，主人批准才落库。
2. 每条都要有依据；没依据的自我认识不批。
3. 回滚 = 用旧快照生成一个新版本，历史一条不删（和 git revert 一个道理）。

数据（plugin_data/persona_chain/）
  persona.json    当前版本：blocks / redlines / version
  versions.jsonl  版本快照，只追加
  drafts.json     待审草稿
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

_DIR = Path(get_astrbot_plugin_data_path()) / "persona_chain"
_PERSONA = _DIR / "persona.json"
_VERSIONS = _DIR / "versions.jsonl"
_DRAFTS = _DIR / "drafts.json"

MAX_BLOCKS = 12          # 条目上限，超了先合并旧的
MAX_REDLINES = 10
MAX_BLOCK_CHARS = 60     # 一条太长就不是「人格」是「作文」
MAX_PENDING = 5          # 待审草稿上限，免得她一个人刷满队列
DRAFT_COOLDOWN = 180     # 两条草稿之间至少隔这么久（秒）
BLOCK_QUOTA = 900        # 注入预算里给她的额度（12 条满配也装得下）

# 注入预算：和 soul / heart / shared 共用一本账
try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "dafeiyu_prompt_budget",
        Path(__file__).resolve().parent.parent
        / "astrbot_plugin_dafeiyu_soul"
        / "prompt_budget.py",
    )
    _budget = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_budget)
except Exception:  # noqa: BLE001
    _budget = None
    logger.warning("persona_chain: 注入预算模块没加载上", exc_info=True)

_KINDS = ("喜好", "习惯", "边界", "自我认知")


def _now() -> float:
    return time.time()


def _short_id(text: str) -> str:
    raw = f"{text}|{int(_now() * 1000)}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]


def _bigrams(text: str) -> set[str]:
    t = "".join(c for c in str(text or "") if "\u4e00" <= c <= "\u9fff" or c.isalnum())
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _similar(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _containment(a: str, b: str) -> float:
    """短的那边有多少被长的那边包含——「不是 AI」和「不许否认自己是 AI」靠这个抓。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / min(len(ga), len(gb))


class PersonaStore:
    """当前人格 + 草稿 + 版本链，一个类管完，方便无头测试。"""

    def __init__(self) -> None:
        self.version = 0
        self.blocks: list[dict] = []
        self.redlines: list[dict] = []
        self.drafts: list[dict] = []
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        _DIR.mkdir(parents=True, exist_ok=True)
        try:
            if _PERSONA.exists():
                d = json.loads(_PERSONA.read_text("utf-8"))
                self.version = int(d.get("version") or 0)
                self.blocks = d.get("blocks") or []
                self.redlines = d.get("redlines") or []
        except Exception:  # noqa: BLE001
            # 读坏了先挪走，别让下一次 save() 用空人格盖掉（红线在这里面）
            try:
                bad = _PERSONA.with_suffix(f".corrupt-{int(_now())}")
                _PERSONA.replace(bad)
                logger.warning(f"persona_chain: 人格文件读坏了，原文挪到 {bad.name}")
            except Exception:  # noqa: BLE001
                logger.warning("persona_chain: 人格文件读坏了且挪不走", exc_info=True)
        try:
            if _DRAFTS.exists():
                self.drafts = json.loads(_DRAFTS.read_text("utf-8")) or []
        except Exception:  # noqa: BLE001
            logger.warning("persona_chain: 草稿文件读坏了，这轮从空队列开始", exc_info=True)

    def save(self) -> None:
        _DIR.mkdir(parents=True, exist_ok=True)
        try:
            tmp = _PERSONA.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "version": self.version,
                        "blocks": self.blocks,
                        "redlines": self.redlines,
                        "updated": _now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "utf-8",
            )
            os.replace(tmp, _PERSONA)
            dt = _DRAFTS.with_suffix(".tmp")
            dt.write_text(
                json.dumps(self.drafts, ensure_ascii=False, indent=2), "utf-8"
            )
            os.replace(dt, _DRAFTS)
        except Exception:  # noqa: BLE001
            logger.warning("persona_chain: 保存失败", exc_info=True)

    def _snapshot(self, reason: str, note: str = "") -> None:
        try:
            row = {
                "version": self.version,
                "ts": round(_now()),
                "reason": reason,
                "note": note,
                "blocks": self.blocks,
                "redlines": self.redlines,
            }
            with open(_VERSIONS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.warning("persona_chain: 写版本快照失败", exc_info=True)

    def _bump(self, reason: str, note: str = "") -> int:
        self.version += 1
        self.save()
        self._snapshot(reason, note)
        return self.version

    # ---------- 草稿 ----------
    def add_draft(self, text: str, kind: str = "自我认知", evidence: str = "") -> dict | None:
        text = (text or "").strip()
        if not text or len(text) > MAX_BLOCK_CHARS:
            return None
        if kind not in _KINDS:
            kind = "自我认知"
        if len(self.pending()) >= MAX_PENDING:
            return None
        for b in self.blocks:
            if _similar(b.get("text", ""), text) > 0.8:
                return None  # 已经是她的一部分了
        for d in self.drafts:
            if _similar(d.get("text", ""), text) > 0.8:
                return d  # 同一个意思不重复提
        item = {
            "id": _short_id(text),
            "text": text,
            "kind": kind,
            "evidence": str(evidence or "").strip()[:120],
            "created": round(_now()),
            "status": "pending",
            "version": self.version + 1,
        }
        self.drafts.append(item)
        self.save()
        return item

    def pending(self) -> list[dict]:
        return [d for d in self.drafts if d.get("status") == "pending"]

    def draft_blocker(self) -> str:
        """现在该不该让她提新草稿；空串表示可以。只挡工具调用，不挡主人手动加。"""
        if len(self.pending()) >= MAX_PENDING:
            return f"待审的已经 {MAX_PENDING} 条了，等主人先处理"
        last = max((float(d.get("created") or 0) for d in self.drafts), default=0.0)
        if last and _now() - last < DRAFT_COOLDOWN:
            return "刚提过一条，过几分钟再说"
        return ""

    def approve(self, draft_id: str, force: bool = False) -> tuple[bool, str]:
        draft = next(
            (d for d in self.drafts if d.get("id") == draft_id and d.get("status") == "pending"),
            None,
        )
        if draft is None:
            if any(d.get("id") == draft_id for d in self.drafts):
                return False, "这条已经处理过了。"
            return False, f"没找到草稿「{draft_id}」。"
        text = draft.get("text", "")
        # 红线优先：词法上像冲突就先拦下，要主人带「强制」才放行
        conflict = self._redline_conflict(text)
        if conflict and not force:
            return False, (
                f"疑似和红线「{conflict[0][:20]}」冲突。"
                f"真觉得没事，就 /人格 批准 {draft_id} 强制"
            )
        # 没证据的自我认识不落库（主人手动加的除外，这里只管草稿）
        if not draft.get("evidence"):
            return False, "这条没写依据，先别批——让她说清楚是从哪儿看出来的。"
        if len(text) > MAX_BLOCK_CHARS:
            return False, f"太长了（{len(text)} 字），压到 {MAX_BLOCK_CHARS} 字以内再批。"
        if len(self.blocks) >= MAX_BLOCKS:
            return False, f"条目已经 {MAX_BLOCKS} 条满了，先合并或删掉旧的那条再批。"
        for b in self.blocks:
            if _similar(b.get("text", ""), text) > 0.7:
                return False, f"和已有条目「{b.get('text', '')[:16]}」太像，直接改那条就行。"
        self.blocks.append(
            {
                "id": draft.get("id"),
                "text": text,
                "kind": draft.get("kind") or "自我认知",
                "evidence": draft.get("evidence", ""),
                "since": round(_now()),
            }
        )
        draft["status"] = "approved"
        draft["decided"] = round(_now())
        v = self._bump("approve", f"{draft.get('kind')}：{text}")
        return True, f"收下了，现在是第 {v} 版。"

    def reject(self, draft_id: str) -> tuple[bool, str]:
        draft = next(
            (d for d in self.drafts if d.get("id") == draft_id and d.get("status") == "pending"),
            None,
        )
        if draft is None:
            return False, f"没找到待审的「{draft_id}」。"
        draft["status"] = "rejected"
        draft["decided"] = round(_now())
        self.save()
        return True, "扔掉了，这条以后不会再提。"

    # ---------- 主人手动改 ----------
    def add_block(self, text: str, kind: str = "习惯") -> tuple[bool, str]:
        text = (text or "").strip()
        if not text or len(text) > MAX_BLOCK_CHARS:
            return False, f"空或者超过 {MAX_BLOCK_CHARS} 字。"
        if len(self.blocks) >= MAX_BLOCKS:
            return False, f"条目满了（{MAX_BLOCKS}），先删一条。"
        self.blocks.append(
            {
                "id": _short_id(text),
                "text": text,
                "kind": kind if kind in _KINDS else "习惯",
                "evidence": "主人加的",
                "since": round(_now()),
            }
        )
        v = self._bump("owner-add", text)
        return True, f"加上了，第 {v} 版。"

    def drop_block(self, key: str) -> tuple[bool, str]:
        target = next(
            (b for b in self.blocks if b.get("id") == key or key in b.get("text", "")),
            None,
        )
        if target is None:
            return False, f"没找到「{key}」。"
        self.blocks = [b for b in self.blocks if b is not target]
        v = self._bump("owner-drop", target.get("text", ""))
        return True, f"删了「{target.get('text', '')[:16]}」，第 {v} 版。"

    # ---------- 红线 ----------
    def _redline_conflict(self, text: str) -> tuple[str, float] | None:
        """词法上最像的那条红线。只是线索，不是判决——所以允许主人强制放行。"""
        best = None
        for r in self.redlines:
            rt = str(r.get("text") or "")
            score = max(_similar(rt, text), _containment(rt, text))
            if best is None or score > best[1]:
                best = (rt, score)
        if best and best[1] >= 0.25:
            return best
        return None

    def add_redline(self, text: str) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "红线不能是空的。"
        if len(self.redlines) >= MAX_REDLINES:
            return False, f"红线最多 {MAX_REDLINES} 条。"
        self.redlines.append({"text": text, "added": round(_now())})
        self.save()
        self._snapshot("redline-add", text)
        return True, "红线加上了，她自己删不掉。"

    def drop_redline(self, index: str) -> tuple[bool, str]:
        try:
            i = int(index) - 1
            if i < 0 or i >= len(self.redlines):
                raise ValueError
        except Exception:  # noqa: BLE001
            return False, "红线编号不对。"
        removed = self.redlines.pop(i)
        self.save()
        self._snapshot("redline-drop", removed.get("text", ""))
        return True, f"红线「{removed.get('text', '')[:16]}」撤了。"

    # ---------- 回滚 ----------
    def rollback(self, version: int) -> tuple[bool, str]:
        snap = None
        try:
            for line in _VERSIONS.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if int(row.get("version") or -1) == int(version):
                    snap = row  # 同一版本多条取最后一条
        except FileNotFoundError:
            return False, "还没有历史版本。"
        except Exception:  # noqa: BLE001
            return False, "版本文件读不了。"
        if snap is None:
            return False, f"没有第 {version} 版。"
        self.blocks = snap.get("blocks") or []
        # 红线不跟着回滚：撤红线是单独的操作，不能顺手丢掉
        new_v = self._bump("rollback", f"回到第 {version} 版")
        return True, f"回到第 {version} 版的内容，记为第 {new_v} 版。"

    # ---------- 注入 ----------
    def render(self, limit: int = 900) -> str:
        if not self.blocks and not self.redlines:
            return ""
        lines: list[str] = []
        if self.redlines:
            lines.append("【红线 · 你自己不能改，也不能忘】")
            for r in self.redlines:
                lines.append(f"- {r.get('text', '')}")
        if self.blocks:
            lines.append(
                f"【你长出来的部分 · 第 {self.version} 版】"
                "（这些不是设定，是相处里慢慢稳下来的；和基础人设打架时以这里为准，"
                "但别主动背给人家听）"
            )
            for b in self.blocks:
                ev = b.get("evidence") or ""
                tail = f"（依据：{ev[:24]}）" if ev and ev != "主人加的" else ""
                lines.append(f"- [{b.get('kind', '')}] {b.get('text', '')}{tail}")
        body = "\n".join(lines)
        return body[:limit]


_STORE: PersonaStore | None = None

_OWNER = {"ts": 0.0, "id": ""}


def _owner_id() -> str:
    """主人 QQ：先看 heart 记的私聊 umo（最准），再看 AstrBot admins_id 里的数字。"""
    now = _now()
    if now - _OWNER["ts"] < 300:
        return _OWNER["id"]
    qq = ""
    try:
        umo = (
            Path(get_astrbot_plugin_data_path()) / "heart" / "umo.txt"
        ).read_text("utf-8").strip()
        if umo:
            qq = umo.split(":")[-1].strip()
    except Exception:  # noqa: BLE001
        pass
    if not qq:
        try:
            cfg = json.loads(
                (Path(get_astrbot_data_path()) / "cmd_config.json").read_text(
                    "utf-8-sig"
                )
            )
            for a in cfg.get("admins_id") or []:
                if str(a).isdigit():
                    qq = str(a)
                    break
        except Exception:  # noqa: BLE001
            pass
    _OWNER.update(ts=now, id=qq)
    return qq


def _is_owner(event: AstrMessageEvent) -> bool:
    try:
        if event.is_admin():
            return True
    except Exception:  # noqa: BLE001
        pass
    owner = _owner_id()
    try:
        return bool(owner) and str(event.get_sender_id()) == owner
    except Exception:  # noqa: BLE001
        return False


def _store() -> PersonaStore:
    global _STORE
    if _STORE is None:
        _STORE = PersonaStore()
    return _STORE


@register(
    "persona_chain",
    "dafeiyu",
    "人格版本链：她提草稿、主人批准、可回滚，红线改不了",
    "1.0.0",
)
class PersonaChain(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.store = _store()

    # ---------- 注入 ----------
    @filter.on_llm_request(priority=-30)
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            block = self.store.render()
            if not block:
                return
            if _budget is not None:
                block = _budget.offer(
                    req, "persona", block, quota=BLOCK_QUOTA, audit_dir=_DIR
                )
            base = req.system_prompt or ""
            req.system_prompt = f"{base.rstrip()}\n\n{block}" if base else block
        except Exception:  # noqa: BLE001
            logger.warning("persona_chain: 注入失败", exc_info=True)

    # ---------- 工具：她只能写草稿 ----------
    @llm_tool(name="propose_persona")
    async def propose_persona(
        self, event: AstrMessageEvent, text: str, kind: str = "自我认知", evidence: str = ""
    ) -> str:
        """当你发现自己身上有一条稳定下来的东西（不是今天的情绪），可以提一条人格草稿。
        草稿不会生效，也不会进你的记忆，主人批准了才算数。

        Args:
            text(string): 一句话，不超过 60 字，比如「我不喜欢被叫工具人」
            kind(string): 喜好 / 习惯 / 边界 / 自我认知
            evidence(string): 依据。哪天的日记、哪句话让你这么觉得，必须有
        """
        if not (evidence or "").strip():
            return "没写依据就别提，我不知道这条是从哪儿来的。"
        blocker = self.store.draft_blocker()
        if blocker:
            return f"先别提：{blocker}。"
        item = self.store.add_draft(text, kind, evidence)
        if item is None:
            return "这条要么已经有了，要么超过 60 字了。"
        return f"记成草稿了（{item['id']}），等主人批。"

    @llm_tool(name="persona_self")
    async def persona_self(self, event: AstrMessageEvent) -> str:
        """看看自己现在是第几版、身上有什么是后来长出来的。"""
        s = self.store
        if not s.blocks and not s.redlines:
            return "还是最初的样子，没长出新东西。"
        lines = [f"第 {s.version} 版"]
        if s.redlines:
            lines.append("红线：" + "；".join(r.get("text", "") for r in s.redlines))
        if s.blocks:
            lines.append(
                "长出来的："
                + "；".join(f"{b.get('text', '')}" for b in s.blocks)
            )
        return "\n".join(lines)

    # ---------- 命令：只有主人能用 ----------
    @filter.command("人格")
    async def persona_cmd(self, event: AstrMessageEvent, text: str = ""):
        """人格版本链：/人格 看状态，/人格 草稿，/人格 批准 <id>，/人格 回滚 <版本>，/人格 红线 加 <话>"""
        try:
            is_owner = _is_owner(event)
        except Exception:  # noqa: BLE001
            is_owner = False
        if not is_owner:
            # 群里谁都能发 /人格，但人格是她的根，只有主人能动
            yield event.plain_result("这个只有主人能看。")
            return

        s = self.store
        arg = (text or "").strip()
        parts = arg.split(maxsplit=1)
        sub = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not sub:
            pending = len(s.pending())
            out = [f"现在第 {s.version} 版"]
            if s.redlines:
                out.append("红线（她自己改不了）：")
                out += [f"{i}. {r.get('text', '')}" for i, r in enumerate(s.redlines, 1)]
            if s.blocks:
                out.append(f"长出来的（{len(s.blocks)}/{MAX_BLOCKS}）：")
                out += [
                    f"- [{b.get('kind', '')}] {b.get('text', '')}"
                    + (f"｜依据：{b['evidence'][:20]}" if b.get("evidence") else "")
                    for b in s.blocks
                ]
            out.append(f"待审草稿：{pending} 条" + ("（/人格 草稿 看）" if pending else ""))
            yield event.plain_result("\n".join(out))
            return

        if sub in ("草稿", "待审"):
            pending = s.pending()
            if not pending:
                yield event.plain_result("没有待审的草稿。")
                return
            yield event.plain_result(
                "\n".join(
                    f"[{d['id']}] {d.get('kind', '')}｜{d.get('text', '')}\n"
                    f"    依据：{d.get('evidence', '')}"
                    for d in pending
                )
                + "\n\n/人格 批准 <id> 或 /人格 驳回 <id>"
            )
            return

        if sub in ("批准", "通过"):
            bits = (rest or "").split()
            if not bits:
                yield event.plain_result("要给草稿 id，比如 /人格 批准 a1b2")
                return
            force = any(x in ("强制", "--force", "force") for x in bits[1:])
            ok, msg = s.approve(bits[0], force=force)
            yield event.plain_result(msg)
            return

        if sub in ("驳回", "否决"):
            ok, msg = s.reject(rest)
            yield event.plain_result(msg)
            return

        if sub == "回滚":
            try:
                ver = int(rest)
            except Exception:  # noqa: BLE001
                yield event.plain_result("要给版本号，比如 /人格 回滚 3")
                return
            ok, msg = s.rollback(ver)
            yield event.plain_result(msg)
            return

        if sub == "加":
            ok, msg = s.add_block(rest)
            yield event.plain_result(msg)
            return

        if sub == "删":
            ok, msg = s.drop_block(rest)
            yield event.plain_result(msg)
            return

        if sub == "红线":
            if not rest:
                if not s.redlines:
                    yield event.plain_result("还没设红线。")
                    return
                yield event.plain_result(
                    "\n".join(
                        f"{i}. {r.get('text', '')}" for i, r in enumerate(s.redlines, 1)
                    )
                )
                return
            act, _, body = rest.partition(" ")
            if act == "加":
                ok, msg = s.add_redline(body)
            elif act == "删":
                ok, msg = s.drop_redline(body)
            else:
                ok, msg = False, "用法：/人格 红线 加 <话> 或 /人格 红线 删 <编号>"
            yield event.plain_result(msg)
            return

        yield event.plain_result(
            "用法：/人格｜/人格 草稿｜/人格 批准 <id>｜/人格 驳回 <id>｜"
            "/人格 回滚 <版本>｜/人格 加 <话>｜/人格 删 <id或片段>｜/人格 红线"
        )
