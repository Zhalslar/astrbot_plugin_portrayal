"""机器人自身昵称 / 头像的备份与还原。

为什么要单独做一层：
- ``set_qq_profile`` / ``set_qq_avatar`` 改的是**账号全局**的昵称和头像，
  而 AstrBot 的 ``umo`` 是**会话级**的。之前把「机器人原始昵称/头像」按 umo
  存进 shared_preferences，就会出现：A 会话切换人格 -> 昵称变成群友 A；
  B 会话再切换 -> 读到的是「已经变成群友 A」的资料，于是把 A 的名字当成
  「机器人原始昵称」记下来，之后再还原就彻底串了。
- 这里把原始资料改成**全局唯一**的一份，并额外记录「最近一次替换上去的克隆
  昵称」，以便识别出「当前昵称其实已经是某个克隆昵称」，从而不会把克隆昵称
  误当成原始昵称保存。

数据落在插件数据目录的 ``bot_identity.json``，与 ``portrayal.json`` 同级，
结构简单、便于人工查看和修复。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import logger

# 头像下载：主源 + 备用源（腾讯系两个不同域名，任一可用即可）
AVATAR_URL_TEMPLATES: tuple[str, ...] = (
    "https://q4.qlogo.cn/headimg_dl?dst_uin={uin}&spec=640",
    "https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640",
)

# 头像体积上限（约 4MB），避免把异常大的响应写进状态文件
MAX_AVATAR_BYTES = 4 * 1024 * 1024

# 记录多少条「被替换上去的克隆昵称」
MAX_CLONE_HISTORY = 50


def build_avatar_urls(user_id: str | int) -> list[str]:
    """返回某 QQ 号的头像候选地址（按优先级）"""
    uin = str(user_id).strip()
    if not uin.isdigit():
        return []
    return [tpl.format(uin=uin) for tpl in AVATAR_URL_TEMPLATES]


@dataclass
class BotIdentity:
    """机器人原始资料 + 克隆昵称占用情况"""

    nickname: str = ""
    user_id: str = ""
    avatar_b64: str = ""
    captured_at: int = 0
    # 昵称 -> {"user_id": 占用者 QQ, "at": 时间戳}
    clone_names: dict[str, dict[str, Any]] = field(default_factory=dict)
    # umo -> 当前该会话「穿着」的群友 QQ（仅用于展示与排障）
    worn: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """是否已经成功备份过原始资料"""
        return bool(self.nickname or self.avatar_b64)

    @property
    def avatar_ready(self) -> bool:
        return bool(self.avatar_b64)

    def is_clone_name(self, nickname: str | None) -> bool:
        """当前昵称是否是我们自己推上去的克隆昵称"""
        name = (nickname or "").strip()
        return bool(name) and name in self.clone_names

    def worn_owner(self, nickname: str | None) -> dict[str, Any] | None:
        name = (nickname or "").strip()
        return self.clone_names.get(name) if name else None

    def mark_clone_name(self, nickname: str, user_id: str) -> None:
        name = (nickname or "").strip()
        if not name:
            return
        self.clone_names[name] = {"user_id": str(user_id), "at": int(time.time())}
        if len(self.clone_names) > MAX_CLONE_HISTORY:
            oldest = sorted(
                self.clone_names.items(), key=lambda kv: kv[1].get("at", 0)
            )[: len(self.clone_names) - MAX_CLONE_HISTORY]
            for key, _ in oldest:
                self.clone_names.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nickname": self.nickname,
            "user_id": self.user_id,
            "avatar_b64": self.avatar_b64,
            "captured_at": self.captured_at,
            "clone_names": self.clone_names,
            "worn": self.worn,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "BotIdentity":
        if not isinstance(data, dict):
            return cls()
        clone_names = data.get("clone_names")
        worn = data.get("worn")
        return cls(
            nickname=str(data.get("nickname") or ""),
            user_id=str(data.get("user_id") or ""),
            avatar_b64=str(data.get("avatar_b64") or ""),
            captured_at=int(data.get("captured_at") or 0),
            clone_names=dict(clone_names) if isinstance(clone_names, dict) else {},
            worn=dict(worn) if isinstance(worn, dict) else {},
        )


class BotIdentityStore:
    """BotIdentity 的读写（线程安全，进程内单实例）"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: BotIdentity | None = None

    # ---------- 读写 ----------

    def load(self) -> BotIdentity:
        with self._lock:
            if self._data is not None:
                return self._data
            if not self.path.exists():
                self._data = BotIdentity()
                return self._data
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"读取机器人资料备份失败（将重新开始记录）：{e}")
                raw = None
            self._data = BotIdentity.from_dict(raw)
            return self._data

    def save(self) -> None:
        with self._lock:
            data = self.load()
            payload = json.dumps(
                data.to_dict(), ensure_ascii=False, indent=2
            )
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(payload, encoding="utf-8")
            except Exception as e:
                logger.error(f"写入机器人资料备份失败：{e}")

    def reset(self) -> None:
        """清空备份（用于人工修复）"""
        with self._lock:
            self._data = BotIdentity()
            try:
                self.path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"删除机器人资料备份失败：{e}")

    # ---------- 记录 ----------

    def remember_original(
        self,
        *,
        nickname: str = "",
        user_id: str = "",
        avatar_b64: str = "",
        overwrite: bool = False,
    ) -> BotIdentity:
        """记录机器人原始资料

        默认只补空缺（昵称/头像各自独立判断），避免后续调用把真实原始值覆盖掉。
        """
        data = self.load()
        with self._lock:
            changed = False
            nickname = (nickname or "").strip()

            if nickname and (overwrite or not data.nickname):
                if data.nickname != nickname:
                    data.nickname = nickname
                    changed = True
            if user_id and data.user_id != str(user_id):
                data.user_id = str(user_id)
                changed = True
            if avatar_b64 and (overwrite or not data.avatar_b64):
                data.avatar_b64 = avatar_b64
                changed = True
            if changed:
                data.captured_at = int(time.time())
                self.save()
        return data

    def mark_worn(
        self,
        *,
        nickname: str,
        user_id: str,
        umo: str | None = None,
        owner: str | None = None,
    ) -> None:
        """记录「我们把某个昵称推上去了」"""
        data = self.load()
        with self._lock:
            data.mark_clone_name(nickname, owner or user_id)
            if umo:
                data.worn[umo] = str(owner or user_id)
            self.save()

    def clear_worn(self, umo: str | None = None) -> None:
        data = self.load()
        with self._lock:
            if umo is None:
                data.worn.clear()
            else:
                data.worn.pop(umo, None)
            self.save()

    def describe(self) -> dict[str, Any]:
        """给命令/面板用的可读摘要"""
        data = self.load()
        return {
            "nickname": data.nickname,
            "user_id": data.user_id,
            "has_avatar": bool(data.avatar_b64),
            "avatar_kb": round(len(data.avatar_b64) * 3 / 4 / 1024) if data.avatar_b64 else 0,
            "captured_at": data.captured_at,
            "clone_names": list(data.clone_names),
            "worn": dict(data.worn),
        }


# =========================
# 头像下载
# =========================


def sniff_image_type(data: bytes) -> str | None:
    """按文件头判断图片类型，非图片返回 None"""
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def download_avatar_b64(
    user_id: str | int,
    *,
    urls: Iterable[str] | None = None,
    timeout: float = 15.0,
    max_bytes: int = MAX_AVATAR_BYTES,
) -> str:
    """下载头像并返回 base64（失败返回空串）

    依次尝试候选地址，并且只接受真正的图片内容（防止 CDN 返回 HTML 错误页）。
    """
    import base64 as _b64

    import aiohttp

    candidates = list(urls) if urls is not None else build_avatar_urls(user_id)
    if not candidates:
        return ""

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            for url in candidates:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.debug(f"头像下载失败 {url}：HTTP {resp.status}")
                            continue
                        body = await resp.read()
                except Exception as e:
                    logger.debug(f"头像下载异常 {url}：{e}")
                    continue

                if not body:
                    continue
                if len(body) > max_bytes:
                    logger.warning(
                        f"头像过大（{len(body)} 字节）已忽略：{url}"
                    )
                    continue
                if sniff_image_type(body) is None:
                    logger.debug(f"头像响应不是图片，已忽略：{url}")
                    continue
                return _b64.b64encode(body).decode()
    except Exception as e:  # pragma: no cover - 网络环境兜底
        logger.warning(f"创建头像下载会话失败：{e}")
    return ""
