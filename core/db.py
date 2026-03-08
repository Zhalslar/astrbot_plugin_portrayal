import json

from .config import PluginConfig
from .model import UserProfile


class UserProfileDB:
    def __init__(self, config: PluginConfig):
        self.file = config.portrayal_file
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, UserProfile] = self._load()

    def _load(self) -> dict[str, UserProfile]:
        if not self.file.exists():
            return {}

        try:
            raw = json.loads(self.file.read_text("utf-8"))
        except Exception:
            return {}

        result: dict[str, UserProfile] = {}

        for uid, data in raw.items():
            if isinstance(data, dict):
                result[uid] = UserProfile.from_dict(data)

        return result

    def save(self) -> None:
        payload = {uid: p.to_dict() for uid, p in self._data.items()}
        self.file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, user_id: str) -> UserProfile | None:
        return self._data.get(user_id)

    def set(self, profile: UserProfile) -> None:
        self._data[profile.user_id] = profile
        self.save()

    def all(self) -> dict[str, UserProfile]:
        return self._data
