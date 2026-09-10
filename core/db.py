import json
import os

from astrbot.api import logger

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
            # utf-8-sig：兼容被编辑器加上 BOM 的文件
            raw = json.loads(self.file.read_text("utf-8-sig"))
        except Exception as e:
            # 不要把「文件损坏」当成「没有数据」，否则下一次写入会把所有档案抹掉
            logger.error(f"读取 portrayal.json 失败，已跳过本次加载：{e}")
            try:
                bad = self.file.with_suffix(".json.bad")
                self.file.replace(bad)
                logger.warning(f"损坏的文件已重命名为：{bad}")
            except Exception as rename_error:
                logger.warning(f"重命名损坏文件失败：{rename_error}")
            return {}

        if not isinstance(raw, dict):
            logger.error("portrayal.json 顶层不是对象，已跳过本次加载")
            return {}

        result: dict[str, UserProfile] = {}

        for uid, data in raw.items():
            if not isinstance(data, dict):
                continue
            try:
                result[str(uid)] = UserProfile.from_dict(
                    {"user_id": str(uid), **data}
                )
            except TypeError as e:
                logger.warning(f"跳过字段不兼容的档案 {uid}：{e}")

        return result

    def save(self) -> None:
        payload = {uid: p.to_dict() for uid, p in self._data.items()}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        # 先写临时文件再原子替换，避免写到一半崩溃导致档案全丢
        tmp = self.file.with_name(self.file.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.file)
        except Exception as e:
            logger.error(f"写入 portrayal.json 失败：{e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def get(self, user_id: str) -> UserProfile | None:
        return self._data.get(user_id)

    def set(self, profile: UserProfile) -> None:
        self._data[profile.user_id] = profile
        self.save()

    def all(self) -> dict[str, UserProfile]:
        return self._data
