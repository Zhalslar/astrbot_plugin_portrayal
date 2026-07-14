from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger


@dataclass
class CachedMessages:
    """Store cached messages for one user."""

    texts: list[str]
    timestamp: float


class MessageCacheStorage:
    """Persist message cache state as JSON."""

    def __init__(self, cache_dir: Path):
        """Initialize the cache file location.

        Args:
            cache_dir: Directory used for plugin cache files.
        """
        self.file = cache_dir / "message_cache.json"

    def load(self) -> tuple[dict[str, CachedMessages], dict[str, int]]:
        """Load cached messages and group cursors.

        Returns:
            Cached messages keyed by group and user, plus group cursors.
        """
        if not self.file.exists():
            return {}, {}

        try:
            payload: dict[str, Any] = json.loads(self.file.read_text(encoding="utf-8"))
            raw_users = payload.get("users", {})
            raw_cursors = payload.get("group_cursors", {})
            if not isinstance(raw_users, dict) or not isinstance(raw_cursors, dict):
                raise ValueError("Invalid message cache structure")

            users: dict[str, CachedMessages] = {}
            for key, value in raw_users.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                texts = value.get("texts")
                timestamp = value.get("timestamp")
                if not isinstance(texts, list) or not all(
                    isinstance(text, str) for text in texts
                ):
                    continue
                if not isinstance(timestamp, int | float):
                    continue
                users[key] = CachedMessages(texts=texts, timestamp=float(timestamp))

            cursors = {
                str(group_id): int(cursor)
                for group_id, cursor in raw_cursors.items()
                if isinstance(cursor, int)
            }
            return users, cursors
        except Exception as e:
            logger.warning(f"Failed to load message cache: {e}")
            return {}, {}

    def save(
        self,
        users: dict[str, CachedMessages],
        group_cursors: dict[str, int],
    ) -> None:
        """Persist the current message cache state.

        Args:
            users: Cached messages keyed by group and user.
            group_cursors: History scan cursors keyed by group.
        """
        payload = {
            "users": {key: asdict(value) for key, value in users.items()},
            "group_cursors": group_cursors,
        }
        temporary_file = self.file.with_suffix(".tmp")
        try:
            temporary_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_file.replace(self.file)
        except Exception as e:
            logger.error(f"Failed to save message cache: {e}")

    def clear(self) -> None:
        """Delete the persisted message cache file."""
        try:
            self.file.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to clear message cache: {e}")
