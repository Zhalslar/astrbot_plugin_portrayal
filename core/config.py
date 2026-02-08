# config.py
from __future__ import annotations

import yaml
from pathlib import Path
from collections.abc import Mapping, MutableMapping
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
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


class LLMConfig(ConfigNode):
    provider_id: str
    retry_times: int


class MessageConfig(ConfigNode):
    default_query_rounds: int
    max_msg_count: int
    cache_ttl_min: int

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


class PluginConfig(ConfigNode):
    llm: LLMConfig
    message: MessageConfig
    load_builtin_prompt: bool
    entry_storage: list[dict[str, Any]]

    _plugin_name: str = "astrbot_plugin_portrayal"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context

        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.builtin_prompt_file = self.plugin_dir / "builtin_prompts.yaml"

        # 加载用户配置
        self.entries: list[PromptEntry] = [
            PromptEntry(item) for item in self.entry_storage
        ]
        if self.load_builtin_prompt:
            self.load_builtin_prompts()
        logger.debug(f"已注册命令：{[e.command for e in self.entries]}")

    def load_builtin_prompts(self) -> None:
        with self.builtin_prompt_file.open("r", encoding="utf-8") as f:
            data: list[dict[str, Any]] = yaml.safe_load(f) or []

        existed_commands = {e.command for e in self.entries}
        new_items: list[dict[str, Any]] = []

        for item in data:
            if item["command"] in existed_commands:
                continue
            self.entry_storage.append(item)
            new_items.append(item)
            self.entries.append(PromptEntry(item))

        if new_items:
            self.save_config()
            logger.info(
                f"[{self._plugin_name}] 已补充并保存内置提示词："
                f"{[item['command'] for item in new_items]}"
            )

    def match_prompt_by_cmd(self, message: str) -> str | None:
        """根据命令匹配提示词"""
        for entry in self.entries:
            if entry.command == message:
                return entry.content
