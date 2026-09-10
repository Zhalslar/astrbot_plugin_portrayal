"""离线测试桩：伪造 astrbot 模块，使插件可以在不启动 AstrBot 的情况下被导入。

仅用于本地测试（tests/ 目录不会被插件加载）。
"""

from __future__ import annotations

import sys
import types
from typing import Any


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _SP:
    """astrbot.api.sp 的最小实现"""

    def __init__(self):
        self.store: dict[tuple, Any] = {}

    async def get_async(self, *, scope=None, scope_id=None, key=None, default=None):
        return self.store.get((scope, scope_id, key), default)

    async def put_async(self, *, scope=None, scope_id=None, key=None, value=None):
        self.store[(scope, scope_id, key)] = value

    async def remove_async(self, *, scope=None, scope_id=None, key=None):
        self.store.pop((scope, scope_id, key), None)


class _Filter:
    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"

    class EventMessageType:
        GROUP_MESSAGE = "group_message"

    class PermissionType:
        ADMIN = "admin"

    @staticmethod
    def command(name, *a, **k):
        def deco(fn):
            fn.__command__ = name
            return fn

        return deco

    @staticmethod
    def permission_type(*a, **k):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def platform_adapter_type(*a, **k):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def event_message_type(*a, **k):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def on_llm_request(*a, **k):
        def deco(fn):
            return fn

        return deco


class _At:
    def __init__(self, qq):
        self.qq = qq

    def __repr__(self):
        return f"At({self.qq})"


class _Plain:
    def __init__(self, text):
        self.text = text


class _Star:
    def __init__(self, context=None):
        self.context = context


class _Context:
    """记录 register_web_api 的调用，便于断言面板路由"""

    def __init__(self, *a, **k):
        self.registered_web_apis: list[tuple] = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered_web_apis.append((route, view_handler, methods, desc))


class _WebRequest:
    """astrbot.api.web.request 的最小实现，供面板接口测试使用"""

    def __init__(self):
        self.method = "GET"
        self.query: dict[str, str] = {}
        self.path_params: dict[str, str] = {}
        self.username = "tester"
        self.plugin_name = "astrbot_plugin_portrayal"
        self._json: Any = None

    def set(self, *, method="GET", query=None, json_body=None, path_params=None):
        self.method = method
        self.query = dict(query or {})
        self._json = json_body
        self.path_params = dict(path_params or {})

    async def json(self, default=None):
        return self._json if self._json is not None else default


WEB_REQUEST = _WebRequest()


def _json_response(data=None, **kwargs):
    return {"status": "ok", "message": "", "data": data}


def _error_response(message, *, status_code=400, data=None, **kwargs):
    return {"status": "error", "message": message, "data": data}


class _StarTools:
    @staticmethod
    def get_data_dir(plugin_name):
        from pathlib import Path

        # 固定放在插件目录下的 .test_tmp，避免临时目录带来的沙箱问题
        p = Path(__file__).resolve().parents[1] / ".test_tmp" / plugin_name
        p.mkdir(parents=True, exist_ok=True)
        return p


class _AstrBotConfig(dict):
    def save_config(self, *a, **k):
        pass


class _Provider:
    pass


class _ProviderRequest:
    def __init__(self):
        self.system_prompt = ""


_PROVIDER_IMPL: dict[str, Any] = {"impl": None}


def install() -> None:
    """把桩模块注册进 sys.modules"""
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.sp = _SP()
    api.AstrBotConfig = _AstrBotConfig

    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = _Filter

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = _Context
    api_star.Star = _Star

    core = types.ModuleType("astrbot.core")
    core_config = types.ModuleType("astrbot.core.config")
    core_config_astrbot = types.ModuleType("astrbot.core.config.astrbot_config")
    core_config_astrbot.AstrBotConfig = _AstrBotConfig

    core_message = types.ModuleType("astrbot.core.message")
    core_message_components = types.ModuleType("astrbot.core.message.components")
    core_message_components.At = _At
    core_message_components.Plain = _Plain

    core_platform = types.ModuleType("astrbot.core.platform")
    core_platform_event = types.ModuleType("astrbot.core.platform.astr_message_event")
    core_platform_event.AstrMessageEvent = object
    sources = types.ModuleType("astrbot.core.platform.sources")
    sources_aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    sources_aiocqhttp_event = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    sources_aiocqhttp_event.AiocqhttpMessageEvent = object

    core_provider = types.ModuleType("astrbot.core.provider")
    core_provider_provider = types.ModuleType("astrbot.core.provider.provider")
    core_provider_provider.Provider = _Provider
    core_provider_entities = types.ModuleType("astrbot.core.provider.entities")
    core_provider_entities.ProviderRequest = _ProviderRequest

    core_star = types.ModuleType("astrbot.core.star")
    core_star_context = types.ModuleType("astrbot.core.star.context")
    core_star_context.Context = _Context
    core_star_tools = types.ModuleType("astrbot.core.star.star_tools")
    core_star_tools.StarTools = _StarTools

    core_utils = types.ModuleType("astrbot.core.utils")
    core_utils_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    core_utils_path.get_astrbot_plugin_path = lambda: str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
    )

    api_web = types.ModuleType("astrbot.api.web")
    api_web.request = WEB_REQUEST
    api_web.json_response = _json_response
    api_web.error_response = _error_response

    api.__path__ = []  # 让 astrbot.api 成为一个包

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": api_event,
        "astrbot.api.star": api_star,
        "astrbot.api.web": api_web,
        "astrbot.core": core,
        "astrbot.core.config": core_config,
        "astrbot.core.config.astrbot_config": core_config_astrbot,
        "astrbot.core.message": core_message,
        "astrbot.core.message.components": core_message_components,
        "astrbot.core.platform": core_platform,
        "astrbot.core.platform.astr_message_event": core_platform_event,
        "astrbot.core.platform.sources": sources,
        "astrbot.core.platform.sources.aiocqhttp": sources_aiocqhttp,
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": (
            sources_aiocqhttp_event
        ),
        "astrbot.core.provider": core_provider,
        "astrbot.core.provider.provider": core_provider_provider,
        "astrbot.core.provider.entities": core_provider_entities,
        "astrbot.core.star": core_star,
        "astrbot.core.star.context": core_star_context,
        "astrbot.core.star.star_tools": core_star_tools,
        "astrbot.core.utils": core_utils,
        "astrbot.core.utils.astrbot_path": core_utils_path,
    }
    sys.modules.update(modules)
