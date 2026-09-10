import json
import os

from astrbot.api import logger

from .config import PluginConfig
from .model import UserProfile


class UserProfileDB:
    def __init__(self, config: PluginConfig):
        self.file = config.portrayal_file
        self.file.parent.mkdir(parents=True, exist_ok=True)
        # 读取失败（IO 类错误）时置为 True：此时内存里没有真实数据，
        # 绝不能拿它去覆盖磁盘上可能还完好的文件
        self._degraded = False
        self._data: dict[str, UserProfile] = self._load()

    def _load(self) -> dict[str, UserProfile]:
        if not self.file.exists():
            return {}

        try:
            text = self.file.read_text("utf-8-sig")
        except OSError as e:
            # 文件被占用/权限问题等：文件本身可能完好，**不要改名、不要清空**
            self._degraded = True
            logger.error(f"读取 portrayal.json 失败（IO 问题，已跳过本次加载，不会覆盖文件）：{e}")
            return {}

        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 内容确实坏了：改名留档，避免下次写入时把坏文件当空档案
            self._degraded = True
            logger.error(f"portrayal.json 内容损坏，已跳过本次加载：{e}")
            try:
                bad = self.file.with_suffix(".json.bad")
                self.file.replace(bad)
                logger.warning(f"损坏的文件已重命名为：{bad}")
            except Exception as rename_error:
                logger.warning(f"重命名损坏文件失败：{rename_error}")
            return {}

        if not isinstance(raw, dict):
            self._degraded = True
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

    @property
    def degraded(self) -> bool:
        """本次加载是否失败（失败时禁止写盘，避免覆盖尚未读到的数据）"""
        return self._degraded

    def save(self) -> None:
        if self._degraded:
            logger.error("当前档案未能成功加载，已阻止本次写入以免覆盖 portrayal.json")
            return
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
