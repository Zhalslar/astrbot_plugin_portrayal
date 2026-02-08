from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Any

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig


# =========================
# cache models
# =========================


@dataclass
class _CachedMessages:
    texts: list[str]
    timestamp: float


@dataclass
class MessageQueryResult:
    """
    消息查询结果对象
    """
    texts: list[str]
    scanned_messages: int
    from_cache: bool

    @property
    def count(self) -> int:
        return len(self.texts)

    @property
    def is_empty(self) -> bool:
        return not self.texts


# =========================
# message manager
# =========================


class MessageManager:
    """
    群级扫描 + 用户级缓存 的消息管理器

    特性：
    - 同一群查询任意用户都会复用扫描进度
    - 扫描过程中自动缓存其他人的消息
    - 下次查询从群断点继续
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config.message

        # user cache: group:user -> messages
        self._user_cache: dict[str, _CachedMessages] = {}

        # group cursor: group -> message_seq
        self._group_cursor: dict[str, int] = {}

    # =========================
    # cache helpers
    # =========================

    def _user_key(self, group_id: str, user_id: str) -> str:
        return f"{group_id}:{user_id}"

    def _get_user_cache(self, group_id: str, user_id: str) -> list[str] | None:
        key = self._user_key(group_id, user_id)
        cached = self._user_cache.get(key)
        if not cached:
            return None

        if time() - cached.timestamp > self.cfg.cache_ttl:
            del self._user_cache[key]
            return None

        return cached.texts

    def clear_cache(self):
        self._user_cache.clear()
        self._group_cursor.clear()

    # =========================
    # message parsing
    # =========================

    def _collect_messages(
        self,
        group_id: str,
        messages: list[dict[str, Any]],
    ):
        """
        将一页群消息拆分并缓存到各个用户
        """
        now = time()

        for msg in messages:
            user_id = str(msg["sender"]["user_id"])

            text = "".join(
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
            ).strip()

            if not text:
                continue

            key = self._user_key(group_id, user_id)
            cached = self._user_cache.get(key)

            if not cached:
                self._user_cache[key] = _CachedMessages(
                    texts=[text],
                    timestamp=now,
                )
            else:
                cached.texts.append(text)
                cached.timestamp = now

    # =========================
    # public api
    # =========================

    async def get_user_texts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        *,
        max_rounds: int,
    ) -> MessageQueryResult:
        """
        获取指定用户在群内的历史文本消息
        """
        group_id = str(event.get_group_id())
        target_id = str(target_id)

        # ---------- cache first ----------
        cached = self._get_user_cache(group_id, target_id)
        if cached and len(cached) >= self.cfg.max_msg_count:
            return MessageQueryResult(
                texts=cached[: self.cfg.max_msg_count],
                scanned_messages=0,
                from_cache=True,
            )

        texts = cached[:] if cached else []
        rounds = 0

        # 群级扫描断点
        message_seq = self._group_cursor.get(group_id, 0)

        # ---------- scan group messages ----------
        while rounds < max_rounds and len(texts) < self.cfg.max_msg_count:
            result: dict[str, Any] = await event.bot.api.call_action(
                "get_group_msg_history",
                group_id=group_id,
                message_seq=message_seq,
                count=self.cfg.per_query_count,
                reverseOrder=True,
            )

            messages = result.get("messages", [])
            if not messages:
                break

            # 更新群扫描断点
            message_seq = messages[0]["message_id"]
            self._group_cursor[group_id] = message_seq

            # 关键点：这一页给所有人缓存
            self._collect_messages(group_id, messages)

            # 再取目标用户
            cached = self._get_user_cache(group_id, target_id)
            if cached:
                texts = cached[:]

            rounds += 1

        return MessageQueryResult(
            texts=texts[: self.cfg.max_msg_count],
            scanned_messages=rounds * self.cfg.per_query_count,
            from_cache=cached is not None,
        )
