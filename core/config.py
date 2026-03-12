# config.py
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.provider import Provider
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ============ 插件自定义配置 ==================


class PromptEntry(ConfigNode):
    command: str
    content: str

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "content": self.content,
        }


class LLMConfig(ConfigNode):
    provider_id: str
    retry_times: int


class ImageSearchConfig(ConfigNode):
    enabled: bool
    api_key: str
    endpoint: str
    engine: str
    preference: str
    result_limit: int
    request_timeout_sec: int
    language: str
    country: str

    def is_ready(self) -> bool:
        return self.enabled and bool((self.api_key or "").strip())

    @classmethod
    def normalize_preference_value(cls, raw: str | None) -> str | None:
        if raw is None:
            return None
        raw = str(raw).strip()
        if not raw:
            return "auto"
        for sep in ("=", ":", "："):
            if sep in raw:
                raw = raw.split(sep, 1)[1].strip()
                break
        raw = raw.lower()
        alias = {
            "": "auto",
            "auto": "auto",
            "自动": "auto",
            "anime": "anime",
            "二次元": "anime",
            "动漫": "anime",
            "影视": "film_tv",
            "影视作品": "film_tv",
            "film": "film_tv",
            "tv": "film_tv",
            "film_tv": "film_tv",
            "历史": "historical",
            "历史人物": "historical",
            "historical": "historical",
            "现实": "real_person",
            "现实人物": "real_person",
            "真人": "real_person",
            "real": "real_person",
            "real_person": "real_person",
        }
        return alias.get(raw)

    def normalized_preference(self) -> str:
        return self.normalize_preference_value(self.preference) or "auto"

    def preference_label(self) -> str:
        labels = {
            "auto": "自动",
            "anime": "二次元",
            "film_tv": "影视作品",
            "historical": "历史人物",
            "real_person": "现实人物",
        }
        return labels[self.normalized_preference()]

    @staticmethod
    def preference_help_text() -> str:
        return "自动/二次元/影视作品/历史人物/现实人物"


class MessageConfig(ConfigNode):
    default_query_rounds: int
    max_msg_count: int
    cache_ttl_min: int
    protected_user_ids: list[str]

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.cache_ttl = self.cache_ttl_min * 60
        self.max_query_rounds = 200
        self.per_query_count = 200

    def get_query_rounds(self, rounds=None) -> int:
        """获取查询轮数"""
        if rounds and str(rounds).isdigit():
            rounds = int(rounds)
        if not isinstance(rounds, int) or rounds <= 0 or rounds > self.max_query_rounds:
            return self.default_query_rounds
        return rounds

    def is_protected_user(self, user_id: str | int) -> bool:
        """检查用户是否在保护名单中"""
        return str(user_id) in self.protected_user_ids


class PluginConfig(ConfigNode):
    llm: LLMConfig
    image_search: ImageSearchConfig
    message: MessageConfig
    inject_prompt: bool
    entry_storage: list[dict[str, Any]]

    _plugin_name: str = "astrbot_plugin_portrayal"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        cfg.setdefault(
            "image_search",
            {
                "enabled": False,
                "api_key": "",
                "endpoint": "https://serpapi.com/search.json",
                "engine": "google_images",
                "preference": "auto",
                "result_limit": 5,
                "request_timeout_sec": 20,
                "language": "zh-cn",
                "country": "cn",
            },
        )
        super().__init__(cfg)
        self.context = context

        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.style_dir = self.plugin_dir / "pillowmd_style"
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.builtin_prompt_file = self.plugin_dir / "builtin_prompts.yaml"
        self.portrayal_file = self.data_dir / "portrayal.json"

    def get_provider(self, *, umo: str | None = None) -> Provider:
        provider = self.context.get_provider_by_id(
            self.llm.provider_id
        ) or self.context.get_using_provider(umo=umo)

        if not isinstance(provider, Provider):
            raise RuntimeError("未配置用于文本生成任务的 LLM 提供商")

        return provider
