"""人格克隆的共享业务逻辑。

QQ 命令（main.py）与 WebUI 面板（plugin_api.py）都走这里，
保证两条入口对 portrayal.json 的读写语义完全一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .config import PluginConfig
from .db import UserProfileDB
from .entry import EntryService
from .llm import LLMService
from .message import MessageManager
from .model import UserProfile

# 与 main.py 保持一致的模式前缀
APPEND_PREFIX = "追加："
RESET_PREFIX = "重置："

# 超过该长度时提醒不建议直接群发
MAX_SAFE_PROMPT_LEN = 2000


class PersonaError(Exception):
    """人格操作失败（可安全展示给用户的原因）"""


@dataclass(slots=True)
class PersonaEditResult:
    """一次人格写入的结果"""

    user_id: str
    nickname: str
    mode: str
    content: str

    @property
    def length(self) -> int:
        return len(self.content)

    @property
    def too_long(self) -> bool:
        return self.length > MAX_SAFE_PROMPT_LEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "mode": self.mode,
            "length": self.length,
            "too_long": self.too_long,
            "clone_prompt": self.content,
        }


@dataclass(slots=True)
class GenerateResult:
    """一次 LLM 生成的结果"""

    user_id: str
    nickname: str
    mode: str
    content: str
    used_messages: int
    groups: int
    from_cache: bool

    @property
    def length(self) -> int:
        return len(self.content)

    @property
    def too_long(self) -> bool:
        return self.length > MAX_SAFE_PROMPT_LEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "nickname": self.nickname,
            "mode": self.mode,
            "length": self.length,
            "too_long": self.too_long,
            "used_messages": self.used_messages,
            "groups": self.groups,
            "from_cache": self.from_cache,
            "clone_prompt": self.content,
        }


class PersonaService:
    """人格数据的读取与写入"""

    def __init__(
        self,
        config: PluginConfig,
        db: UserProfileDB,
        llm: LLMService,
        msg: MessageManager,
        entry_service: EntryService,
    ):
        self.cfg = config
        self.db = db
        self.llm = llm
        self.msg = msg
        self.entries = entry_service

    # =========================
    # 统计 / 查询
    # =========================

    def stats(self) -> dict[str, Any]:
        """总览统计"""
        profiles = list(self.db.all().values())
        clone_lens = [len(p.clone_prompt.strip()) for p in profiles if p.clone_prompt.strip()]
        return {
            "profiles": len(profiles),
            "with_clone": len(clone_lens),
            "with_portrait": sum(1 for p in profiles if p.portrait.strip()),
            "protected": len(self.cfg.message.protected_user_ids),
            "total_clone_chars": sum(clone_lens),
            "avg_clone_len": round(sum(clone_lens) / len(clone_lens)) if clone_lens else 0,
            "max_clone_len": max(clone_lens) if clone_lens else 0,
            "max_safe_len": MAX_SAFE_PROMPT_LEN,
            "persona_command": "切换人格",
        }

    def overview(self) -> dict[str, Any]:
        """面板首屏数据"""
        return {
            "stats": self.stats(),
            "entry_commands": [
                str(item.get("command", ""))
                for item in self.cfg.entry_storage
                if isinstance(item, dict) and item.get("command")
            ],
            "config": {
                "provider_id": self.cfg.llm.provider_id or "（跟随默认提供商）",
                "inject_prompt": bool(self.cfg.inject_prompt),
                "default_query_rounds": self.cfg.message.default_query_rounds,
                "max_msg_count": self.cfg.message.max_msg_count,
                "cache_ttl_min": self.cfg.message.cache_ttl_min,
                "protected_user_ids": list(self.cfg.message.protected_user_ids),
                "merge_prompt": self.cfg.get_merge_prompt(),
                "edit_prompt": self.cfg.get_edit_prompt(),
            },
            "limits": {"max_safe_len": MAX_SAFE_PROMPT_LEN},
        }

    def get(self, user_id: str) -> UserProfile | None:
        return self.db.get(str(user_id))

    def list_users(
        self,
        *,
        search: str = "",
        only_clone: bool = False,
        only_portrait: bool = False,
        sort: str = "nickname",
        desc: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页列出档案"""
        keyword = search.strip().lower()
        protected = set(self.cfg.message.protected_user_ids)

        rows: list[dict[str, Any]] = []
        for profile in self.db.all().values():
            clone = profile.clone_prompt.strip()
            portrait = profile.portrait.strip()
            if only_clone and not clone:
                continue
            if only_portrait and not portrait:
                continue
            if keyword:
                haystack = " ".join(
                    (profile.user_id, profile.nickname, profile.remark, profile.long_nick)
                ).lower()
                if keyword not in haystack:
                    continue
            rows.append(
                {
                    "user_id": profile.user_id,
                    "nickname": profile.nickname,
                    "remark": profile.remark,
                    "long_nick": profile.long_nick,
                    "sex": profile.sex,
                    "has_clone": bool(clone),
                    "clone_len": len(clone),
                    "has_portrait": bool(portrait),
                    "portrait_len": len(portrait),
                    "timestamp": profile.timestamp,
                    "too_long": len(clone) > MAX_SAFE_PROMPT_LEN,
                    "protected": profile.user_id in protected,
                    "persona_id": profile.persona_id,
                }
            )

        keys = {
            "nickname": lambda r: (r["nickname"], r["user_id"]),
            "clone_len": lambda r: r["clone_len"],
            "timestamp": lambda r: r["timestamp"],
        }
        rows.sort(key=keys.get(sort, keys["nickname"]), reverse=desc)

        total = len(rows)
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "users": rows[offset : offset + limit],
        }

    # =========================
    # 写入
    # =========================

    def apply_edit(
        self,
        user_id: str,
        mode: str,
        content: str,
        *,
        profile: UserProfile | None = None,
    ) -> PersonaEditResult:
        """直接写入人格（不走 LLM）

        Args:
            user_id: 目标 QQ 号
            mode: append / replace / create
            content: 正文
            profile: 可选的预置档案（例如命令层刚拉取到的陌生人资料）
        """
        user_id = str(user_id).strip()
        if not user_id or not user_id.isdigit():
            raise PersonaError("QQ 号不合法")
        if self.cfg.message.is_protected_user(user_id):
            raise PersonaError("该用户在保护名单中，不允许修改")

        text = (content or "").strip()
        if not text:
            raise PersonaError("人格正文不能为空")

        current = profile or self.db.get(user_id)

        if mode in ("append", "replace"):
            if not current:
                raise PersonaError("本地暂无该用户档案，请先执行「克隆人格 @群友」")
            old = current.clone_prompt.strip()
            if not old:
                raise PersonaError("该用户暂无可用的克隆人格，请先执行「克隆人格 @群友」")
            if mode == "append":
                new_content = f"{old}\n{text}"
                label = "追加"
            else:
                new_content = text
                label = "重置"
        elif mode == "create":
            if not current:
                current = UserProfile(user_id=user_id, nickname=f"用户{user_id}")
            new_content = text
            label = "写入"
        else:
            raise PersonaError(f"不支持的操作：{mode}")

        current.clone_prompt = new_content
        current.persona_updated_at = int(time.time())
        self.db.set(current)
        logger.info(f"[面板] 已{label} {current.nickname}({user_id}) 的克隆人格")
        return PersonaEditResult(
            user_id=user_id,
            nickname=current.nickname,
            mode=label,
            content=new_content,
        )

    async def apply_rewrite(
        self,
        user_id: str,
        instruction: str,
        *,
        umo: str | None = None,
        profile: UserProfile | None = None,
    ) -> PersonaEditResult:
        """按修改要求让 LLM 重写人格"""
        user_id = str(user_id).strip()
        if not user_id or not user_id.isdigit():
            raise PersonaError("QQ 号不合法")
        if self.cfg.message.is_protected_user(user_id):
            raise PersonaError("该用户在保护名单中，不允许修改")

        instruction = (instruction or "").strip()
        if not instruction:
            raise PersonaError("修改要求不能为空")

        current = profile or self.db.get(user_id)
        if not current:
            raise PersonaError("本地暂无该用户档案，请先执行「克隆人格 @群友」")
        old = current.clone_prompt.strip()
        if not old:
            raise PersonaError("该用户暂无可用的克隆人格，请先执行「克隆人格 @群友」")

        try:
            content = await self.llm.generate_persona_edit(
                old,
                instruction,
                current,
                self.cfg.get_edit_prompt(),
                umo=umo,
            )
        except Exception as e:
            logger.error(f"[面板] LLM 改写失败：{e}")
            raise PersonaError(f"修改失败：{e}，已保留原有人格") from e

        content = (content or "").strip()
        if not content:
            raise PersonaError("修改结果为空，已保留原有人格")

        current.clone_prompt = content
        current.persona_updated_at = int(time.time())
        self.db.set(current)
        logger.info(f"[面板] 已重写 {current.nickname}({user_id}) 的克隆人格")
        return PersonaEditResult(
            user_id=user_id,
            nickname=current.nickname,
            mode="重写",
            content=content,
        )

    async def generate_from_cache(
        self,
        user_id: str,
        *,
        mode: str = "merge",
        umo: str | None = None,
    ) -> GenerateResult:
        """用缓存到的聊天记录生成/融合人格（不依赖群会话）

        Args:
            mode: merge 表示有旧人格时融合，fresh 表示无视旧人格重生成。
        """
        user_id = str(user_id).strip()
        if not user_id or not user_id.isdigit():
            raise PersonaError("QQ 号不合法")
        if self.cfg.message.is_protected_user(user_id):
            raise PersonaError("该用户在保护名单中，不允许生成")

        profile = self.db.get(user_id)
        texts, groups = self.msg.iter_cached_texts(user_id)
        if not texts:
            raise PersonaError(
                "本地没有该群友的聊天记录缓存，请先在群里执行一次「克隆人格 @群友」"
                "（或「画像 @群友」）以抓取记录"
            )

        entry = self.entries.get_entry("克隆人格")
        if not entry or not (entry.content or "").strip():
            raise PersonaError("未找到「克隆人格」提示词条目，请在插件配置中检查")
        prompt_content = entry.content

        nickname = profile.nickname if profile else f"用户{user_id}"
        target = profile or UserProfile(user_id=user_id, nickname=nickname)

        old_clone_prompt = ""
        merge_prompt = ""
        if mode == "merge" and profile and profile.clone_prompt.strip():
            old_clone_prompt = profile.clone_prompt.strip()
            merge_prompt = self.cfg.get_merge_prompt()

        try:
            content = await self.llm.generate_portrait(
                texts,
                target,
                prompt_content,
                old_clone_prompt=old_clone_prompt,
                merge_prompt_template=merge_prompt,
                umo=umo,
            )
        except Exception as e:
            logger.error(f"[面板] LLM 生成失败：{e}")
            raise PersonaError(f"生成失败：{e}，已保留原有人格") from e

        content = (content or "").strip()
        if not content:
            raise PersonaError("生成结果为空，已保留原有人格")

        target.clone_prompt = content
        target.persona_updated_at = int(time.time())
        # 面板生成的人格同样刷新画像时间，避免面板把刚生成的档案标成「画像过期」
        target.timestamp = target.persona_updated_at
        self.db.set(target)
        logger.info(f"[面板] 已为 {target.nickname}({user_id}) 生成克隆人格（{mode}）")
        return GenerateResult(
            user_id=user_id,
            nickname=target.nickname,
            mode="融合" if old_clone_prompt else "全新生成",
            content=content,
            used_messages=len(texts),
            groups=groups,
            from_cache=True,
        )

    def cache_info(self, user_id: str) -> dict[str, Any]:
        """查看某用户本地缓存了多少条聊天记录"""
        texts, groups = self.msg.iter_cached_texts(str(user_id))
        return {"user_id": str(user_id), "messages": len(texts), "groups": groups}

    def cached_candidates(self) -> dict[str, Any]:
        """缓存里有聊天记录、但本地还没有档案的群友（面板用于一键建档）"""
        known = set(self.db.all())
        protected = set(self.cfg.message.protected_user_ids)
        items = [
            {**item, "known": False, "protected": item["user_id"] in protected}
            for item in self.msg.list_cached_users()
            if item["user_id"] not in known
        ]
        return {"total": len(items), "users": items}
