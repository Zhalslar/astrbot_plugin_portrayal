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
                # 按第一个冒号切分，避免 group_id 只是另一个 id 的前缀时误删
                if group_user_key.split(":", 1)[0] == group_id:
                    del self._user_cache[group_user_key]
            self.save_cache()
            return None

        return cached.texts

    def _count_group_cached_messages(self, group_id: str) -> int:
        """Count total cached messages for a group across all users."""
        return sum(
            len(cached.texts)
            for key, cached in self._user_cache.items()
            if key.split(":", 1)[0] == group_id
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

    def _is_expired(self, cached: CachedMessages) -> bool:
        return time() - cached.timestamp > self.cfg.cache_ttl

    def iter_cached_texts(
        self, target_id: str, *, max_age_sec: float | None = None
    ) -> tuple[list[str], int]:
        """收集缓存里某个用户在所有群中的发言（供 WebUI 面板生成人格使用）

        与 ``get_user_texts`` 保持一致：过期条目一律视为未命中（并按群清理），
        否则面板会拿几小时前的聊天记录重新生成人格。

        Args:
            target_id: Target user ID.
            max_age_sec: 覆盖 TTL（仅测试用），None 表示用配置里的 cache_ttl。

        Returns:
            (texts, group_count)。没有可用缓存时返回 ([], 0)。
        """
        target_id = str(target_id)
        ttl = self.cfg.cache_ttl if max_age_sec is None else max_age_sec

        buckets: list[tuple[float, list[str]]] = []
        expired_groups: set[str] = set()

        for key, cached in tuple(self._user_cache.items()):
            group_id, sep, user_id = key.rpartition(":")
            if not sep or user_id != target_id or not cached.texts:
                continue
            if time() - cached.timestamp > ttl:
                expired_groups.add(group_id)
                self._user_cache.pop(key, None)
                continue
            buckets.append((cached.timestamp, list(cached.texts)))

        # 过期条目按群整组清理，和 _get_user_cache 的行为对齐
        if expired_groups:
            for group_id in expired_groups:
                self._group_cursor.pop(group_id, None)
                for key in tuple(self._user_cache):
                    if key.split(":", 1)[0] == group_id:
                        self._user_cache.pop(key, None)
            self.save_cache()

        if not buckets:
            return [], 0

        # 最近抓到的群优先，避免截断时总是丢掉后写入的群
        buckets.sort(key=lambda item: item[0], reverse=True)
        texts: list[str] = []
        for _, chunk in buckets:
            texts.extend(chunk)
            if len(texts) >= self.cfg.max_msg_count:
                break
        return texts[: self.cfg.max_msg_count], len(buckets)

    def list_cached_users(self, *, max_age_sec: float | None = None) -> list[dict]:
        """列出缓存里出现过的用户及其可用（未过期）发言数

        供面板「从缓存建档」使用：只统计未过期条目，与生成逻辑保持一致。
        """
        ttl = self.cfg.cache_ttl if max_age_sec is None else max_age_sec
        now = time()
        buckets: dict[str, dict] = {}
        for key, cached in self._user_cache.items():
            if not cached.texts:
                continue
            if now - cached.timestamp > ttl:
                continue
            group_id, sep, user_id = key.rpartition(":")
            if not sep or not user_id:
                continue
            item = buckets.setdefault(
                user_id, {"user_id": user_id, "messages": 0, "groups": 0}
            )
            item["messages"] += len(cached.texts)
            item["groups"] += 1
        return sorted(buckets.values(), key=lambda x: x["messages"], reverse=True)

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