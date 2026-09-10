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

# 人格融合 / 人格改写的兜底文案。
# 正常情况下这两段文案来自插件配置（WebUI 可自行修改）；
# 只有在配置项缺失（例如升级后尚未回填）时才会退回这里。
DEFAULT_MERGE_PROMPT = """你此前已为该群友生成过一份人格克隆提示词。
现在基于新的聊天记录，对这份提示词进行完善融合：
1. 保留原有内容中依然成立的性格特质、说话风格、行为习惯，不要无故改动；
2. 仅在新的聊天记录提供明确新证据时，才修订、补充或删除相应特质；
3. 新旧特质冲突时，以新证据为准；
4. 保持结构清晰（说话风格 / 情绪模式 / 高频表达 / 触发反应 / 禁止项）；
5. 融合后全文不超过原提示词的 1.2 倍且不超过 2000 字，宁可精炼不要堆砌；
6. 只输出最终提示词正文，不要解释，不要代码块。
用户昵称：{nickname}"""

DEFAULT_EDIT_PROMPT = """你正在维护一份用于大模型“人格克隆”的系统提示词。
请按照用户给出的修改要求，重写这份提示词，并遵守以下规则：
1. 严格落实修改要求，用户没有提到的部分尽量保持原样，不要无故改动；
2. 保留原有的结构（说话风格 / 情绪模式 / 高频表达 / 触发反应 / 禁止项）；
3. 修改后全文不超过原提示词的 1.2 倍且不超过 2000 字，宁可精炼不要堆砌；
4. 只输出修改后的提示词正文，不要解释，不要代码块，不要 Markdown 格式。
用户昵称：{nickname}"""


def render_template(template: str, **kwargs: Any) -> str:
    """渲染提示词模板。

    只做字面量替换，兼容 ``{key}`` 与 ``{{key}}`` 两种写法。
    不使用 ``str.format``，避免用户自定义提示词中出现其它花括号时抛异常。
    """
    text = str(template) if template else ""
    for key, value in kwargs.items():
        value = "" if value is None else str(value)
        text = text.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
    return text


class ConfigNode:

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
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class PromptEntry(ConfigNode):
    command: str
    need_admin: bool
    content: str

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "need_admin": self.need_admin,
            "content": self.content,
        }


class LLMConfig(ConfigNode):
    provider_id: str
    retry_times: int


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
    message: MessageConfig
    inject_prompt: bool
    entry_storage: list[dict[str, Any]]

    # 新增配置项声明为可选，保证旧配置（尚未回填这两个字段）也能正常加载。
    # 注意：这里不能写成带类级默认值的注解（merge_prompt: str | None = None），
    # 否则属性查找会命中类属性，永远读不到配置里的真实值。
    merge_prompt: str | None
    edit_prompt: str | None

    _plugin_name: str = "astrbot_plugin_portrayal"

    # 配置项缺失或为空时的回退文案
    _PROMPT_DEFAULTS: dict[str, str] = {
        "merge_prompt": DEFAULT_MERGE_PROMPT,
        "edit_prompt": DEFAULT_EDIT_PROMPT,
    }

    def __init__(self, cfg: AstrBotConfig, context: Context):
        # 配置项缺失时回填进原始 config，取值与 WebUI 后续保存的行为保持一致
        for key, default in self._PROMPT_DEFAULTS.items():
            if not isinstance(cfg.get(key), str) or not cfg.get(key, "").strip():
                cfg[key] = default

        super().__init__(cfg)
        self.context = context

        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
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

    def get_merge_prompt(self) -> str:
        """获取克隆人格融合指令（配置为空时退回内置文案）"""
        return (self.merge_prompt or "").strip() or DEFAULT_MERGE_PROMPT

    def get_edit_prompt(self) -> str:
        """获取人格改写指令（配置为空时退回内置文案）"""
        return (self.edit_prompt or "").strip() or DEFAULT_EDIT_PROMPT
