from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import time
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .message_cache import CachedMessages, MessageCacheStorage


@dataclass
class MessageQueryResult:
    """Store collected messages and query metadata."""

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
    """Manage group-level scans and per-user message caches.

    Queries in the same group share scan progress. Each scanned page caches
    messages for every user, and later queries continue from the group cursor.
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config.message
        self._storage = MessageCacheStorage(config.cache_dir)

        # user cache: group:user -> messages
        self._user_cache, self._group_cursor = self._storage.load()

        # group cursor: group -> message_seq
        # group lock: serialize history scans within the same group
        self._group_locks: dict[str, asyncio.Lock] = {}

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
            self._group_cursor.pop(group_id, None)
            for group_user_key in tuple(self._user_cache):
                if group_user_key.startswith(f"{group_id}:"):
                    del self._user_cache[group_user_key]
            self.save_cache()
            return None

        return cached.texts

    def _count_group_cached_messages(self, group_id: str) -> int:
        """Count total cached messages for a group across all users."""
        return sum(
            len(cached.texts)
            for key, cached in self._user_cache.items()
            if key.startswith(f"{group_id}:")
        )

    def clear_cache(self):
        self._user_cache.clear()
        self._group_cursor.clear()
        self._storage.clear()

    def save_cache(self) -> None:
        """Persist the current in-memory message cache."""
        self._storage.save(self._user_cache, self._group_cursor)

    # =========================
    # message parsing
    # =========================

    def _collect_messages(
        self,
        group_id: str,
        messages: list[dict[str, Any]],
    ):
        """Cache one page of group messages by user."""
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
                self._user_cache[key] = CachedMessages(
                    texts=[text],
                    timestamp=now,
                )
            else:
                cached.texts.append(text)
                cached.timestamp = now

    # =========================
    # public api
    # =========================

    def iter_cached_texts(self, target_id: str) -> tuple[list[str], int]:
        """收集缓存里某个用户在所有群中的发言（供 WebUI 面板生成人格使用）

        Args:
            target_id: Target user ID.

        Returns:
            (texts, group_count)。缓存为空时返回 ([], 0)。
        """
        target_id = str(target_id)
        texts: list[str] = []
        groups = 0
        for key, cached in self._user_cache.items():
            group_id, _, user_id = key.partition(":")
            if user_id != target_id or not cached.texts:
                continue
            groups += 1
            texts.extend(cached.texts)
        texts = texts[: self.cfg.max_msg_count]
        return texts, groups

    async def get_user_texts(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
        *,
        max_rounds: int,
    ) -> MessageQueryResult:
        """Get the target user history from the current group.

        Args:
            event: Current group message event.
            target_id: Target user ID.
            max_rounds: Maximum number of history pages to query.

        Returns:
            The collected texts and query metadata.
        """
        group_id = str(event.get_group_id())
        target_id = str(target_id)

        # ---------- check user cache first ----------
        cached = self._get_user_cache(group_id, target_id)
        if cached and len(cached) >= self.cfg.max_msg_count:
            return MessageQueryResult(
                texts=cached[: self.cfg.max_msg_count],
                scanned_messages=0,
                from_cache=True,
            )

        texts = cached[:] if cached else []

        # ---------- determine scan strategy ----------
        max_fetchable = max_rounds * self.cfg.per_query_count
        group_cached_count = self._count_group_cached_messages(group_id)

        # If group cache already covers what this query could fetch, skip scanning
        if group_cached_count >= max_fetchable:
            return MessageQueryResult(
                texts=texts[: self.cfg.max_msg_count],
                scanned_messages=0,
                from_cache=True,
            )

        # Only scan the missing rounds: deficit ÷ per_query_count, rounded up
        deficit = max_fetchable - group_cached_count
        needed_rounds = min(
            max_rounds,
            (deficit + self.cfg.per_query_count - 1) // self.cfg.per_query_count
        )

        rounds = 0
        cache_changed = False

        # Resume from the shared group scan cursor.
        message_seq = self._group_cursor.get(group_id, 0)
        group_lock = self._group_locks.setdefault(group_id, asyncio.Lock())

        # ---------- scan group messages ----------
        while rounds < needed_rounds and len(texts) < self.cfg.max_msg_count:
            try:
                # message_seq is a message ID, not an offset.
                async with group_lock:
                    cached = self._get_user_cache(group_id, target_id)
                    if cached and len(cached) >= self.cfg.max_msg_count:
                        texts = cached[:]
                        break

                    message_seq = self._group_cursor.get(group_id, 0)
                    result: dict[str, Any] = await event.bot.api.call_action(
                        "get_group_msg_history",
                        group_id=group_id,
                        message_seq=message_seq,
                        count=self.cfg.per_query_count,
                        reverseOrder=True,
                    )
                    messages = result.get("messages", [])
                    if messages:
                        message_seq = messages[0]["message_id"]
                        self._group_cursor[group_id] = message_seq
                        self._collect_messages(group_id, messages)
                        cache_changed = True

                messages = result.get("messages", [])
                if not messages:
                    break

                # Refresh the target cache after collecting the page.
                cached = self._get_user_cache(group_id, target_id)
                if cached:
                    texts = cached[:]

            except Exception as e:
                logger.error(e)
                break

            rounds += 1

        if cache_changed:
            self.save_cache()

        return MessageQueryResult(
            texts=texts[: self.cfg.max_msg_count],
            scanned_messages=rounds * self.cfg.per_query_count,
            from_cache=cached is not None,
        )