"""机器人自身昵称 / 头像的备份与还原。

为什么单独做一层：
- ``set_qq_profile`` / ``set_qq_avatar`` 改的是**账号全局**的昵称和头像，
  而 AstrBot 的 ``umo`` 是**会话级**的。如果按 umo 存「机器人原始资料」，
  就会出现：A 会话切换人格 -> 昵称变成群友 A；B 会话再切换 -> 读到的是
  「已经变成群友 A」的资料，于是把 A 的名字当成机器人真名记下来，之后再
  还原就彻底串了。
- 所以原始资料**全局唯一一份**，并且只在**还没被替换过**的时候采集。

另一条硬规则：**绝不从"当前账号状态"反推原始资料**。
账号上的头像一旦被换成群友头像，再去下载机器人 QQ 的头像拿到的就是那个群友的
头像。因此：
- 原始头像只在「当前不是克隆状态」时采集一次；
- 采集失败就记为「未知」，并且**不再自动重试**（自动重试只会在换过头像之后
  把群友头像写进来）；
- 只有管理员明确执行「还原机器人资料」（并在 QQ 里已经改回原样）时，
  才允许把当前头像确认为原始头像。

数据落在插件数据目录的 ``bot_identity.json``，与 ``portrayal.json`` 同级，
便于人工查看和修复；写入使用「临时文件 + 原子替换」。
"""

from __future__ import annotations

import json
import os
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

# 记录多少条「被替换上去的克隆昵称」（正在使用的不会因超限被淘汰）
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
    # umo -> 当前该会话「穿着」的群友 QQ
    worn: dict[str, str] = field(default_factory=dict)
    # 上次恢复时因为缺少头像原图而没能还原，拿到确认过的原图后要补一次
    pending_avatar_restore: bool = False

    @property
    def nickname_known(self) -> bool:
        return bool(self.nickname)

    @property
    def avatar_known(self) -> bool:
        """是否已经拥有可用的原始头像字节

        注意：False 表示「不知道原始头像是什么」，**不代表**可以用当前账号
        头像去补。
        """
        return bool(self.avatar_b64)

    @property
    def ready(self) -> bool:
        return self.nickname_known or self.avatar_known

    def is_clone_name(self, nickname: str | None) -> bool:
        """该昵称是否是我们自己推上去过的克隆昵称

        这只是一个**启发式**判断，用于避免把明显的克隆昵称当成真名；
        它不构成「当前账号正在穿克隆」的证明（见 wearing_clone）。
        """
        name = (nickname or "").strip()
        return bool(name) and name in self.clone_names

    def worn_owner(self, nickname: str | None) -> dict[str, Any] | None:
        name = (nickname or "").strip()
        return self.clone_names.get(name) if name else None

    def wearing_clone(self, current_nickname: str | None = None) -> bool:
        """当前是否处于「被克隆替换」状态

        有会话占用记录，或当前昵称就是记录过的克隆昵称，都算。
        """
        if self.worn:
            return True
        return self.is_clone_name(current_nickname)

    def mark_clone_name(self, nickname: str, owner: str) -> None:
        """记录一个被推上去的克隆昵称"""
        name = (nickname or "").strip()
        if not name:
            return
        self.clone_names[name] = {"user_id": str(owner), "at": int(time.time())}
        self._evict_clone_names()

    def _evict_clone_names(self) -> None:
        """超出上限时淘汰最老的记录，但**正在使用的昵称绝不淘汰**"""
        if len(self.clone_names) <= MAX_CLONE_HISTORY:
            return
        in_use = {name for name in self.worn.values() if name}
        in_use |= set(self.worn.keys())
        removable = sorted(
            (
                (name, info.get("at", 0))
                for name, info in self.clone_names.items()
                if name not in in_use
            ),
            key=lambda kv: kv[1],
        )
        for name, _ in removable[: len(self.clone_names) - MAX_CLONE_HISTORY]:
            self.clone_names.pop(name, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nickname": self.nickname,
            "user_id": self.user_id,
            "avatar_b64": self.avatar_b64,
            "captured_at": self.captured_at,
            "clone_names": self.clone_names,
            "worn": self.worn,
            "pending_avatar_restore": self.pending_avatar_restore,
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
            pending_avatar_restore=bool(data.get("pending_avatar_restore")),
        )


class BotIdentityStore:
    """BotIdentity 的读写（线程安全 + 原子写入）"""

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
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except Exception as e:
                logger.warning(f"读取机器人资料备份失败（将重新开始记录）：{e}")
                raw = None
            self._data = BotIdentity.from_dict(raw)
            return self._data

    def save(self) -> None:
        with self._lock:
            data = self.load()
            payload = json.dumps(data.to_dict(), ensure_ascii=False, indent=2)
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, self.path)
            except Exception as e:
                logger.error(f"写入机器人资料备份失败：{e}")
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    def reset(self) -> None:
        """清空备份（用于人工修复）"""
        with self._lock:
            self._data = BotIdentity()
            try:
                self.path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"删除机器人资料备份失败：{e}")

    # ---------- 昵称 ----------

    def remember_nickname(
        self, nickname: str, *, user_id: str = "", overwrite: bool = False
    ) -> BotIdentity:
        """记录机器人原始昵称（默认只补空缺）"""
        data = self.load()
        name = (nickname or "").strip()
        with self._lock:
            changed = False
            if name and (overwrite or not data.nickname) and data.nickname != name:
                data.nickname = name
                data.captured_at = int(time.time())
                changed = True
            if user_id and data.user_id != str(user_id):
                data.user_id = str(user_id)
                changed = True
            if changed:
                self.save()
        return data

    def remember_user_id(self, user_id: str) -> None:
        """记录机器人自己的 QQ 号（与昵称、头像相互独立）"""
        uid = str(user_id or "").strip()
        if not uid:
            return
        data = self.load()
        with self._lock:
            if data.user_id != uid:
                data.user_id = uid
                self.save()

    # ---------- 头像 ----------

    def remember_avatar(self, avatar_b64: str, *, overwrite: bool = False) -> bool:
        """记录机器人原始头像字节

        只应在「确认当前账号头像就是机器人原图」时调用。
        """
        avatar = (avatar_b64 or "").strip()
        if not avatar:
            return False
        data = self.load()
        with self._lock:
            if data.avatar_b64 and not overwrite:
                return False
            data.avatar_b64 = avatar
            data.captured_at = int(time.time())
            self.save()
            return True

    def forget_avatar(self) -> None:
        """把原始头像标记为「未知」（清掉可能是错的备份）"""
        data = self.load()
        with self._lock:
            if data.avatar_b64:
                data.avatar_b64 = ""
                self.save()

    def set_pending_avatar_restore(self, pending: bool) -> None:
        data = self.load()
        with self._lock:
            if data.pending_avatar_restore != pending:
                data.pending_avatar_restore = pending
                self.save()

    # ---------- 占用 ----------

    def mark_worn(self, *, nickname: str, owner: str, umo: str | None = None) -> None:
        """记录「我们把某个克隆昵称推上去了」"""
        data = self.load()
        with self._lock:
            data.mark_clone_name(nickname, owner)
            if umo:
                data.worn[umo] = str(nickname)
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
            "nickname_known": data.nickname_known,
            "user_id": data.user_id,
            "avatar_known": data.avatar_known,
            "avatar_kb": (
                max(1, round(len(data.avatar_b64) * 3 / 4 / 1024))
                if data.avatar_b64
                else 0
            ),
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
                    logger.warning(f"头像过大（{len(body)} 字节）已忽略：{url}")
                    continue
                if sniff_image_type(body) is None:
                    logger.debug(f"头像响应不是图片，已忽略：{url}")
                    continue
                return _b64.b64encode(body).decode()
    except Exception as e:  # pragma: no cover - 网络环境兜底
        logger.warning(f"创建头像下载会话失败：{e}")
    return ""
