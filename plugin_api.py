"""插件面板（WebUI Pages）的后端接口。

面板前端通过 AstrBot 的 plugin page bridge 调用：
    bridge.apiGet('users', {...})  ->  /api/plug/astrbot_plugin_portrayal/users
所有路由都在这里注册，业务逻辑复用 core/persona_service.py。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from .core.persona_service import PersonaError, PersonaService

PLUGIN_NAME = "astrbot_plugin_portrayal"


# =========================
# 响应与参数
# =========================


def _ok(data: Any = None, message: str = "") -> dict[str, Any]:
    return {"status": "ok", "message": message, "data": data}


def _error(message: str, data: Any = None) -> dict[str, Any]:
    return {
        "status": "error",
        "message": message,
        "message_en": "Plugin request failed.",
        "data": data,
    }


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


async def _payload() -> dict[str, Any]:
    """读取 JSON body 或 query 参数"""
    data = await request.json(default=None)
    if isinstance(data, dict):
        return data
    return dict(request.query)


# =========================
# 路由处理
# =========================


class PluginPageAPI:
    """面板后端"""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    @property
    def personas(self) -> PersonaService:
        return self.plugin.persona_service

    # ---------- 注册 ----------

    def register(self, context) -> None:
        routes: list[tuple[str, str, list[str]]] = [
            ("/overview", "_overview", ["GET", "POST"]),
            ("/users", "_users", ["GET", "POST"]),
            ("/user/<user_id>", "_user_detail", ["GET", "POST"]),
            ("/update", "_update", ["POST"]),
            ("/generate", "_generate", ["POST"]),
            ("/cache", "_cache_info", ["GET", "POST"]),
            ("/cached-users", "_cached_users", ["GET", "POST"]),
            ("/ping", "_ping", ["GET", "POST"]),
        ]
        for route, handler_name, methods in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}",
                self._logged(handler_name, getattr(self, handler_name)),
                methods,
                f"Plugin Page: {PLUGIN_NAME}{route}",
            )

    def _logged(self, name: str, handler):
        """包一层请求日志，便于排查「接口无响应 / 未找到该路由」

        只记录被调用到的路径与参数名（不含内容），写到插件数据目录，
        面板刷新一次就能看出浏览器实际打到哪个 URL。
        """

        async def wrapper(**kwargs):
            started = time.time()
            try:
                path = request.path
                query = dict(request.query)
            except Exception as e:  # pragma: no cover - 脱离请求上下文
                path, query = f"<no request context: {e}>", {}
            self._trace(f"-> {name} path={path} query_keys={sorted(query)}")
            result = handler(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            self._trace(f"<- {name} {(time.time() - started) * 1000:.0f}ms")
            return result

        wrapper.__name__ = getattr(handler, "__name__", name)
        # 保留原函数引用，便于内省/测试分别调用与打断点
        wrapper.__wrapped__ = handler
        return wrapper

    def _trace(self, line: str) -> None:
        logger.info(f"[面板] {line}")
        try:
            data_dir = getattr(self.plugin.cfg, "data_dir", None)
            if not data_dir:
                return
            log_file = Path(data_dir) / "panel_debug.log"
            stamp = time.strftime("%m-%d %H:%M:%S")
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"{stamp} {line}\n")
        except Exception:
            pass

    # ---------- 总览 ----------

    async def _overview(self, **_: Any):
        try:
            data = self.personas.overview()
        except Exception as e:  # pragma: no cover - 兜底，避免面板整页失败
            logger.error(f"[面板] 读取总览失败：{e}", exc_info=True)
            return _error(f"读取总览失败：{e}")
        return _ok(data)

    # ---------- 列表 ----------

    async def _users(self, **_: Any):
        payload = await _payload()
        try:
            data = self.personas.list_users(
                search=str(payload.get("search") or ""),
                only_clone=_bool(payload.get("only_clone")),
                only_portrait=_bool(payload.get("only_portrait")),
                sort=str(payload.get("sort") or "nickname"),
                desc=_bool(payload.get("desc")),
                limit=_int(payload.get("limit"), 50),
                offset=_int(payload.get("offset"), 0),
            )
        except Exception as e:
            logger.error(f"[面板] 读取列表失败：{e}", exc_info=True)
            return _error(f"读取列表失败：{e}")
        return _ok(data)

    # ---------- 详情 ----------

    async def _user_detail(self, user_id: str = "", **_: Any):
        if not user_id:
            payload = await _payload()
            user_id = str(payload.get("user_id") or "")
        user_id = str(user_id).strip()
        if not user_id:
            return _error("缺少 user_id 参数")

        profile = self.personas.get(user_id)
        if not profile:
            return _error(f"本地暂无 {user_id} 的档案")

        cache = self.personas.cache_info(user_id)
        clone = profile.clone_prompt.strip()
        portrait = profile.portrait.strip()
        persona_updated_at = int(getattr(profile, "persona_updated_at", 0) or 0)
        return _ok(
            {
                "user_id": profile.user_id,
                "nickname": profile.nickname,
                "remark": profile.remark,
                "long_nick": profile.long_nick,
                "sex": profile.sex,
                "persona_id": profile.persona_id,
                "clone_prompt": profile.clone_prompt,
                "clone_len": len(clone),
                "portrait": portrait,
                "portrait_len": len(portrait),
                "timestamp": profile.timestamp,
                "persona_updated_at": persona_updated_at,
                # 画像生成时间早于最近一次人格修改 => 画像可能已经过时
                "portrait_stale": bool(
                    portrait
                    and persona_updated_at
                    and profile.timestamp
                    and profile.timestamp < persona_updated_at
                ),
                "protected": self.personas.cfg.message.is_protected_user(user_id),
                "cache": cache,
                "limits": {"max_safe_len": self.personas.stats()["max_safe_len"]},
            }
        )

    # ---------- 写入 ----------

    async def _update(self, **_: Any):
        payload = await _payload()
        user_id = str(payload.get("user_id") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        content = str(payload.get("content") or "")

        if not user_id:
            return _error("缺少 user_id 参数")
        if mode not in {"append", "replace", "create", "rewrite"}:
            return _error(f"不支持的操作：{mode or '(空)'}")

        try:
            if mode == "rewrite":
                result = await self.personas.apply_rewrite(user_id, content)
            else:
                result = self.personas.apply_edit(user_id, mode, content)
        except PersonaError as e:
            return _error(str(e))
        except Exception as e:
            logger.error(f"[面板] 修改人格失败：{e}", exc_info=True)
            return _error(f"修改失败：{e}")

        return _ok(result.to_dict(), message=f"已{result.mode}")

    # ---------- 生成 ----------

    async def _generate(self, **_: Any):
        payload = await _payload()
        user_id = str(payload.get("user_id") or "").strip()
        mode = str(payload.get("mode") or "merge").strip()
        if not user_id:
            return _error("缺少 user_id 参数")
        if mode not in {"merge", "fresh"}:
            return _error(f"不支持的生成模式：{mode}")

        try:
            result = await self.personas.generate_from_cache(user_id, mode=mode)
        except PersonaError as e:
            return _error(str(e))
        except asyncio.TimeoutError:
            return _error("LLM 调用超时，请稍后重试")
        except Exception as e:
            logger.error(f"[面板] 生成人格失败：{e}", exc_info=True)
            return _error(f"生成失败：{e}")

        return _ok(result.to_dict(), message=f"已{result.mode}")

    # ---------- 缓存信息 ----------

    async def _cache_info(self, **_: Any):
        payload = await _payload()
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            return _error("缺少 user_id 参数")
        if not user_id.isdigit():
            return _error("QQ 号不合法")
        try:
            data = self.personas.cache_info(user_id)
        except Exception as e:
            return _error(f"读取缓存失败：{e}")
        return _ok(data)

    # ---------- 缓存里可建档的群友 ----------

    async def _cached_users(self, **_: Any):
        try:
            data = self.personas.cached_candidates()
        except Exception as e:
            logger.error(f"[面板] 读取候选失败：{e}", exc_info=True)
            return _error(f"读取候选失败：{e}")
        return _ok(data)

    # ---------- 连通性自检 ----------

    async def _ping(self, **_: Any):
        """自检端点：回显插件看到的请求信息

        用于排查「面板显示未找到该路由」到底卡在哪一层：与其它接口走完全相同的
        路由前缀，所以能在浏览器里直接打开验证。不返回任何用户数据。
        """
        paths = []
        try:
            registered = getattr(self.plugin.context, "registered_web_apis", []) or []
            paths = [r[0] for r in registered if isinstance(r, (tuple, list))]
        except Exception:  # pragma: no cover
            paths = []
        return _ok(
            {
                "plugin": PLUGIN_NAME,
                "request_path": request.path,
                "path_params": dict(request.path_params),
                "query": dict(request.query),
                "caller": request.username,
                "registered": paths,
            },
            message="pong",
        )


def register_plugin_page_api(context, plugin: Any) -> PluginPageAPI:
    """注册面板后端并返回实例（便于测试）"""
    api = PluginPageAPI(plugin)
    api.register(context)
    return api
