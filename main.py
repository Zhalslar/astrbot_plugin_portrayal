import time

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.entities import ProviderRequest

from .core.bot_identity import BotIdentityStore, build_avatar_urls, download_avatar_b64
from .core.config import PluginConfig
from .core.db import UserProfileDB
from .core.entry import EntryService
from .core.llm import LLMService
from .core.message import MessageManager
from .core.model import UserProfile
from .core.persona_service import PersonaError, PersonaService
from .plugin_api import register_plugin_page_api

# 「改人格」的三种模式前缀
APPEND_PREFIX = "追加："
RESET_PREFIX = "重置："

# 超出该长度时额外提示「不建议直接群发」
MAX_SAFE_PROMPT_LEN = 2000

# 这些命令由各自的 @filter.command handler 处理，提示词监听器不再重复响应
RESERVED_COMMANDS = frozenset(
    {
        "查看画像",
        "查看克隆",
        "改人格",
        "切换人格",
        "恢复人格",
        "查看机器人身份",
        "还原机器人资料",
    }
)


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(self.cfg)
        # 机器人自身昵称/头像的全局备份（账号级，修复「切换后昵称头像串了」）
        self.identity = BotIdentityStore(self.cfg.bot_identity_file)
        # 头像下载实现（实例属性，便于注入与替换）
        self._avatar_downloader = download_avatar_b64
        # QQ 命令与 WebUI 面板共用的人格服务
        self.persona_service = PersonaService(
            self.cfg, self.db, self.llm, self.msg, self.entry_service
        )
        # 注册 WebUI 面板后端（/api/plug/astrbot_plugin_portrayal/...）
        try:
            self.page_api = register_plugin_page_api(context, self)
        except Exception as e:
            self.page_api = None
            logger.warning(f"注册 WebUI 面板接口失败（不影响聊天命令）：{e}")

    async def initialize(self):
        pass

    async def terminate(self):
        self.msg.save_cache()

    # =========================
    # 解析辅助
    # =========================

    @staticmethod
    def _get_cmd(event: AstrMessageEvent) -> str:
        """取消息中的命令词（默认配置的唤醒前缀已在 message_str 中被剥离）

        使用 split() 而不是 partition(" ")，兼容全角空格与连续空格。
        """
        parts = event.message_str.split()
        if parts:
            return parts[0]
        return event.message_str.strip()

    @staticmethod
    def _split_target_and_text(event: AstrMessageEvent) -> tuple[str, str]:
        """拆分 @群友 与其后的纯文本内容

        Returns:
            (target_id, payload)，没有 @ 时 target_id 为空字符串。

        优先取消息链里的 At 段，并跳过 @机器人 自身（aiocqhttp 会把
        「@bot 改人格 @群友 …」里的第一个 At(self) 留在消息链里）；
        若客户端没有生成 At 段（例如用户直接手打 QQ 号），则退化为把命令词
        之后第一个纯数字 token 当作目标 QQ 号。
        """
        segments = event.get_messages()
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""

        ats = [
            str(seg.qq)
            for seg in segments
            if isinstance(seg, At) and str(seg.qq) not in (self_id, "all")
        ]
        target_id = ats[0] if ats else ""
        if target_id:
            texts: list[str] = []
            hit_at = False
            for seg in segments:
                if isinstance(seg, At):
                    # 只在遇到真正的目标 At 之后才开始收集文本
                    hit_at = hit_at or str(seg.qq) == target_id
                    continue
                if not hit_at:
                    continue
                text = getattr(seg, "text", None)
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())

            payload = " ".join(texts).strip()
            # 部分客户端会把 @群友 渲染成 "@123" 混进正文，这里只剔除
            # 「正好等于 QQ 号」或「QQ 号 + 空格」的前缀，避免误伤以该数字
            # 开头的修改要求
            if payload == target_id:
                payload = ""
            elif payload.startswith(f"{target_id} "):
                payload = payload[len(target_id) :].strip()
            return target_id, payload

        # 退化路径：命令词之后的第一个纯数字 token 视为目标 QQ 号
        tokens = event.message_str.split()
        for index, token in enumerate(tokens[1:], start=1):
            if token.isdigit():
                return token, " ".join(tokens[index + 1 :]).strip()
        return "", ""

    # =========================
    # 查看画像
    # =========================

    @filter.command("查看画像")
    async def view_portrayal(self, event: AiocqhttpMessageEvent):
        """
        查看画像 @群友
        """
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：查看画像 @群友")
            return
        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return
        msg = f"【{profile.nickname}】的画像\n{profile.to_text()}"
        yield event.plain_result(msg)

    # =========================
    # 查看克隆人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看克隆")
    async def view_clone(self, event: AiocqhttpMessageEvent):
        """
        查看克隆 @群友
        """
        target_id, _ = self._split_target_and_text(event)
        if not target_id or not target_id.isdigit():
            yield event.plain_result("命令格式：查看克隆 @群友")
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return
        if not profile.clone_prompt.strip():
            yield event.plain_result(
                f"【{profile.nickname}】暂未生成克隆人格，"
                f"请先执行“克隆人格 @{profile.nickname}”"
            )
            return
        content = profile.clone_prompt.strip()
        yield event.plain_result(
            f"【{profile.nickname}】当前的克隆人格（{len(content)} 字）：\n"
            f"{content}"
        )
        if len(content) > MAX_SAFE_PROMPT_LEN:
            yield event.plain_result(
                f"提示：当前人格 {len(content)} 字，超过 {MAX_SAFE_PROMPT_LEN} 字，"
                f"不建议直接群发全文。"
            )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.cfg.inject_prompt:
            return
        if not event.message_str:
            return
        sender_id = event.get_sender_id()
        profile = self.db.get(sender_id)
        if not profile:
            return
        info = profile.to_text()
        req.system_prompt += f"\n\n### 当前对话用户的背景信息\n{info}\n\n"

    # =========================
    # 画像 / 克隆人格
    # =========================

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = self._get_cmd(event)
        # 内置命令有自己的 handler，即使配置里出现同名提示词条目也不要重复处理
        if cmd in RESERVED_COMMANDS:
            return
        prompt = self.entry_service.get_entry(cmd)
        if not prompt:
            # 容错：升级后配置里可能还没回填“重克隆人格”条目，
            # 此时退回复用“克隆人格”的提示词内容
            if cmd == "重克隆人格":
                prompt = self.entry_service.get_entry("克隆人格")
            if not prompt:
                return
        if prompt.need_admin and not event.is_admin():
            return

        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return

        # 检查权限
        target_id = ats[0]
        if not target_id.isdigit():
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        # 解析查询轮数
        end_param = event.message_str.split()[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        # 获取基本信息（沿用旧档案里已有的画像与克隆人格，避免被空值覆盖）
        try:
            info = await event.bot.get_stranger_info(
                user_id=int(target_id), no_cache=True
            )
            profile = UserProfile.from_qq_data(target_id, data=dict(info))
        except Exception as e:
            logger.error(f"获取用户资料失败：{e}")
            yield event.plain_result(f"获取该用户资料失败：{e}")
            return
        old_profile = self.db.get(target_id)
        if old_profile:
            profile.portrait = old_profile.portrait
            profile.timestamp = old_profile.timestamp
            profile.clone_prompt = old_profile.clone_prompt

        # 只有“克隆”类命令才涉及人格融合，其余命令（画像 / 找对象…）保持原样
        old_clone_prompt = ""
        merge_prompt = ""
        if "克隆" in cmd:
            old_clone_prompt = profile.clone_prompt
            # “重克隆人格”明确要求丢弃旧人格；旧人格为空时自然走全新生成
            if "重克隆" in cmd or not old_clone_prompt.strip():
                old_clone_prompt = ""
            else:
                merge_prompt = self.cfg.get_merge_prompt()

        if old_clone_prompt.strip():
            yield event.plain_result(
                f"检测到【{profile.nickname}】已有克隆人格，将结合新的聊天记录进行融合..."
            )

        yield event.plain_result(
            f"正在发起{query_rounds}轮查询来获取{profile.nickname}的聊天记录..."
        )

        # 获取聊天记录
        result = await self.msg.get_user_texts(
            event,
            profile.user_id,
            max_rounds=query_rounds,
        )
        if result.is_empty:
            yield event.plain_result("没有查询到该群友的任何消息")
            return
        if result.from_cache and result.scanned_messages <= 0:
            yield event.plain_result(
                f"命中缓存，已提取到{result.count}条{profile.nickname}的聊天记录，"
                f"正在{cmd}..."
            )
        else:
            yield event.plain_result(
                f"已从{result.scanned_messages}条群消息中提取到"
                f"{result.count}条{profile.nickname}的聊天记录，正在{cmd}..."
            )

        # LLM 分析画像（存在旧克隆人格时自动走融合）
        try:
            content = await self.llm.generate_portrait(
                result.texts,
                profile,
                prompt.content,
                old_clone_prompt=old_clone_prompt,
                merge_prompt_template=merge_prompt,
                umo=event.unified_msg_origin,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败：{e}，已保留原有人格")
            return

        # 防御：LLM 返回空内容时不写库，避免把已有的人格覆盖成空
        if not content or not content.strip():
            yield event.plain_result("分析结果为空，已保留原有人格")
            return

        content = content.strip()

        # 保存克隆人格
        if "克隆" in cmd:
            profile.clone_prompt = content

        # 保存画像并发送
        profile.portrait = content
        profile.timestamp = int(time.time())
        self.db.set(profile)
        yield event.plain_result(content)

    # =========================
    # 快捷修改人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("改人格")
    async def edit_persona(self, event: AiocqhttpMessageEvent):
        """
        改人格 @群友 <修改要求>          —— 由 LLM 按修改要求重写
        改人格 @群友 追加：<补充文字>   —— 直接在末尾追加，不调用 LLM
        改人格 @群友 重置：<完整人格>   —— 整段替换（无档案时自动建档）
        """
        target_id, instruction = self._split_target_and_text(event)
        if not target_id or not target_id.isdigit():
            yield event.plain_result(
                "命令格式：\n"
                "改人格 @群友 <修改要求>（LLM 重写）\n"
                "改人格 @群友 追加：<补充文字>（零成本追加）\n"
                "改人格 @群友 重置：<完整人格>（整段替换）"
            )
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许修改")
            return

        profile = self.db.get(target_id)
        is_new_profile = profile is None

        # 重置模式允许目标用户还没有档案：先拉取陌生人资料，再交给服务层写入
        if is_new_profile and instruction.startswith(RESET_PREFIX):
            try:
                info = await event.bot.get_stranger_info(
                    user_id=int(target_id), no_cache=True
                )
                profile = UserProfile.from_qq_data(target_id, data=dict(info))
            except Exception as e:
                logger.error(f"获取用户资料失败：{e}")
                yield event.plain_result(f"获取该用户资料失败：{e}")
                return

        try:
            if instruction.startswith(APPEND_PREFIX):
                result = self.persona_service.apply_edit(
                    target_id,
                    "append",
                    instruction[len(APPEND_PREFIX) :],
                    profile=profile,
                )
            elif instruction.startswith(RESET_PREFIX):
                # 目标还没有档案时（刚拉过陌生人资料）用 create 建档写入
                result = self.persona_service.apply_edit(
                    target_id,
                    "create" if is_new_profile else "replace",
                    instruction[len(RESET_PREFIX) :],
                    profile=profile,
                )
            else:
                result = await self.persona_service.apply_rewrite(
                    target_id,
                    instruction,
                    umo=event.unified_msg_origin,
                    profile=profile,
                )
        except PersonaError as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"修改人格失败：{e}", exc_info=True)
            yield event.plain_result(f"修改失败：{e}，已保留原有人格")
            return

        yield event.plain_result(
            f"【{result.nickname}】的克隆人格已{result.mode}（{result.length} 字）。"
            f"\n执行“切换人格 @{result.nickname}”后生效。"
            f"\n发送“查看克隆 @{result.nickname}”可查看当前人格全文。"
        )
        if result.too_long:
            yield event.plain_result(
                f"提示：当前人格 {result.length} 字，超过 {MAX_SAFE_PROMPT_LEN} 字，"
                f"不建议直接群发全文。"
            )

    # =========================
    # 机器人身份（昵称 / 头像）同步
    # =========================

    async def _fetch_bot_avatar(self, bot_user_id: str, *, force: bool = False) -> str:
        """下载并缓存机器人原始头像（失败返回空串，不会抛异常）"""
        data = self.identity.load()
        if data.avatar_b64 and not force:
            return data.avatar_b64

        avatar_b64 = await self._avatar_downloader(bot_user_id)
        if avatar_b64:
            self.identity.remember_original(
                nickname="", user_id=bot_user_id, avatar_b64=avatar_b64, overwrite=force
            )
            logger.debug(f"已备份机器人原始头像（{len(avatar_b64) * 3 // 4} 字节）")
        else:
            logger.warning(
                "备份机器人原始头像失败（已尝试多个头像源）；"
                "还原时若头像不对，请手动设置一次"
            )
        return avatar_b64

    async def _sync_qq_nickname(self, event: AiocqhttpMessageEvent, nickname: str) -> str:
        """设置机器人昵称，返回错误描述（成功返回空串）"""
        nickname = (nickname or "").strip()
        if not nickname:
            return "昵称为空，已跳过"
        try:
            await event.bot.set_qq_profile(nickname=nickname)
        except Exception as e:
            logger.error(f"设置机器人昵称失败：{e}")
            return f"设置昵称失败：{e}"

        # 校验是否真的生效（协议端可能限流或拒绝）
        try:
            info = await event.bot.get_login_info()
            actual = str(info.get("nickname") or "").strip()
            if actual and actual != nickname:
                logger.warning(f"昵称可能未生效：期望 {nickname!r}，实际 {actual!r}")
                return f"昵称可能未生效（当前仍是「{actual}」）"
        except Exception as e:
            logger.debug(f"校验机器人昵称失败（忽略）：{e}")
        return ""

    async def _sync_qq_avatar(
        self,
        event: AiocqhttpMessageEvent,
        avatar: str,
        *,
        downloader=None,
    ) -> str:
        """设置机器人头像（avatar 可以是 base64、图片直链或 QQ 号）

        优先「自己下载图片再以 base64 上传」，因为协议端从 URL 拉取图片
        常常受网络/白名单限制；下载失败再退回让协议端自己拉。

        Args:
            downloader: 可注入的头像下载实现（默认用模块内的 download_avatar_b64）
        """
        fetch = downloader or download_avatar_b64
        avatar = (avatar or "").strip()
        if not avatar:
            return "头像为空，已跳过"

        if avatar.isdigit():
            avatar_b64 = await fetch(avatar)
            if not avatar_b64:
                return "头像下载失败（已尝试多个头像源）"
            return await self._upload_qq_avatar(event, avatar_b64)

        if avatar.startswith("http://") or avatar.startswith("https://"):
            avatar_b64 = await fetch(avatar, urls=[avatar])
            if avatar_b64:
                return await self._upload_qq_avatar(event, avatar_b64)
            # 退回让协议端自己尝试拉取
            try:
                await event.bot.set_qq_avatar(file=avatar)
                return ""
            except Exception as e:
                logger.error(f"设置机器人头像失败：{e}")
                return f"设置头像失败：{e}"

        if avatar.startswith("base64://"):
            avatar = avatar[len("base64://") :]
        return await self._upload_qq_avatar(event, avatar)

    @staticmethod
    async def _upload_qq_avatar(
        event: AiocqhttpMessageEvent, avatar_b64: str
    ) -> str:
        try:
            await event.bot.set_qq_avatar(file=f"base64://{avatar_b64}")
        except Exception as e:
            logger.error(f"上传机器人头像失败：{e}")
            return f"设置头像失败：{e}"
        return ""

    async def _capture_bot_identity(
        self, event: AiocqhttpMessageEvent, umo: str
    ) -> str:
        """切换前备份机器人原始昵称 / 头像，返回提醒文案

        关键点：机器人昵称/头像是**账号级**的，所以备份只有一份（全局），
        并且只补空缺。这样第二次切换时不会把「群友的昵称」误记成原始昵称。
        """
        data = self.identity.load()
        warning = ""

        try:
            info = await event.bot.get_login_info()
        except Exception as e:
            logger.warning(f"获取机器人资料失败：{e}")
            info = {}

        current_nickname = str(info.get("nickname") or "").strip()
        bot_user_id = str(info.get("user_id") or "").strip()

        if not data.ready:
            # 首次备份：如果当前昵称已经是我们自己推上去的克隆昵称，就说明
            # 之前那次备份丢了，无法恢复真名，只能提示用户手动改一次
            if current_nickname and data.is_clone_name(current_nickname):
                owner = data.worn_owner(current_nickname) or {}
                warning = (
                    f"注意：当前机器人昵称「{current_nickname}」看起来是克隆昵称，"
                    f"但本地没有原始昵称备份，无法自动还原。"
                    f"请手动把机器人昵称/头像改回原样后，执行「还原机器人资料」让它记住。"
                )
                logger.warning(warning)
            else:
                self.identity.remember_original(
                    nickname=current_nickname, user_id=bot_user_id
                )
                logger.info(f"已备份机器人原始昵称：{current_nickname or '(空)'}")

        data = self.identity.load()
        if bot_user_id and not data.user_id:
            self.identity.remember_original(user_id=bot_user_id)

        if data.ready and not data.avatar_ready:
            await self._fetch_bot_avatar(data.user_id or bot_user_id)

        return warning

    # =========================
    # 切换 / 恢复人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def switch_persona(self, event: AiocqhttpMessageEvent):
        """
        切换人格 @群友
        """
        ats = [
            str(seg.qq)
            for seg in event.get_messages()[1:]
            if isinstance(seg, At) and str(seg.qq).isdigit()
        ]
        if not ats:
            yield event.plain_result("命令格式：切换人格 @群友")
            return

        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许切换")
            return

        profile = self.db.get(target_id)
        if not profile or not profile.clone_prompt.strip():
            yield event.plain_result(
                "该群友暂无可用的克隆人格，请先执行“克隆人格 @群友”"
            )
            return

        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            yield event.plain_result(
                "当前没有对话，请先开始对话或使用 /new 创建一个对话。"
            )
            return

        force_applied_persona_id = (
            await sp.get_async(
                scope="umo",
                scope_id=umo,
                key="session_service_config",
                default={},
            )
        ).get("persona_id")

        # 切换前备份机器人原始昵称 / 头像（全局唯一一份）
        identity_warning = await self._capture_bot_identity(event, umo)

        try:
            await self.context.persona_manager.update_persona(
                persona_id=profile.persona_id,
                system_prompt=profile.clone_prompt,
            )
        except ValueError:
            await self.context.persona_manager.create_persona(
                persona_id=profile.persona_id,
                system_prompt=profile.clone_prompt,
            )

        await self.context.conversation_manager.update_conversation_persona_id(
            umo, profile.persona_id
        )

        # 清空当前对话历史
        await self.context.conversation_manager.update_conversation(
            umo, cid, history=[]
        )

        force_warn_msg = ""
        if force_applied_persona_id:
            force_warn_msg = "提醒：由于自定义规则，您现在切换的人格将不会生效。"

        # 机器人昵称/头像是账号级的，这里会同时影响所有会话
        self.identity.mark_worn(
            nickname=profile.nickname,
            user_id=profile.user_id,
            umo=umo,
            owner=profile.user_id,
        )
        nickname_error = await self._sync_qq_nickname(event, profile.nickname)
        avatar_error = await self._sync_qq_avatar(event, profile.user_id, downloader=self._avatar_downloader)

        msg = (
            f"已将当前对话切换为【{profile.nickname}】的克隆人格，对话历史已清空。"
            f"如需还原，请使用：恢复人格。{force_warn_msg}"
        )
        if nickname_error:
            msg += f"\n⚠️ {nickname_error}"
        if avatar_error:
            msg += f"\n⚠️ {avatar_error}"
        if identity_warning:
            msg += f"\n⚠️ {identity_warning}"
        yield event.plain_result(msg)
        logger.debug(
            f"已同步机器人资料：昵称={profile.nickname!r} 头像来源=QQ{profile.user_id}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("恢复人格")
    async def restore_persona(self, event: AiocqhttpMessageEvent):
        """
        恢复人格
        """
        umo = event.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

        # 取默认人格 id
        cfg = self.context.get_config(umo=umo)
        default_persona_id = (
            cfg.get("provider_settings", {}).get("default_personality") or "default"
        )

        if cid:
            await self.context.conversation_manager.update_conversation_persona_id(
                umo, default_persona_id
            )
            await self.context.conversation_manager.update_conversation(
                umo, cid, history=[]
            )

        # 还原机器人昵称 / 头像（全局备份）
        data = self.identity.load()
        restored_nickname = ""
        nickname_error = ""
        avatar_error = ""
        avatar_restored = False
        had_backup = data.ready

        if data.nickname:
            nickname_error = await self._sync_qq_nickname(event, data.nickname)
            if not nickname_error:
                restored_nickname = data.nickname

        # 头像：优先用备份的原图字节；备份缺失时按机器人自己的 QQ 号重新下载。
        # 不能用克隆群友头像地址，也不能拿「备份时的昵称」去顶——那时协议端上
        # 挂着的很可能还是克隆头像，会直接把克隆头像又设回去。
        if had_backup:
            avatar_source = data.avatar_b64
            if not avatar_source:
                avatar_source = await self._fetch_bot_avatar(
                    data.user_id, force=True
                )
            if avatar_source:
                avatar_error = await self._sync_qq_avatar(
                    event, avatar_source, downloader=self._avatar_downloader
                )
                avatar_restored = not avatar_error
            else:
                avatar_error = "头像原图未备份且重新下载失败"

        self.identity.clear_worn(umo)

        msg = f"已恢复默认人格【{default_persona_id}】，对话历史已清空。"
        if restored_nickname:
            msg += f" 机器人昵称已还原为【{restored_nickname}】。"
        if nickname_error:
            msg += f"\n⚠️ {nickname_error}"
        if avatar_error:
            msg += f"\n⚠️ {avatar_error}，头像可能仍需手动恢复。"
        if avatar_restored:
            msg += " 头像已还原为机器人原图。"
        if not had_backup:
            msg += (
                "\n⚠️ 本地没有机器人原始资料备份，昵称/头像需手动恢复；"
                "手动改好后执行「还原机器人资料」即可让它重新记住。"
            )

        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看机器人身份")
    async def show_bot_identity(self, event: AiocqhttpMessageEvent):
        """
        查看机器人身份
        """
        data = self.identity.load()
        try:
            info = await event.bot.get_login_info()
        except Exception as e:
            info = {}
            logger.warning(f"获取机器人资料失败：{e}")

        current = str(info.get("nickname") or "").strip()
        lines = [
            "【机器人身份备份】",
            f"备份昵称：{data.nickname or '（未备份）'}",
            f"备份 QQ：{data.user_id or '（未知）'}",
            f"备份头像：{'已缓存 ' + str(len(data.avatar_b64) * 3 // 4 // 1024) + ' KB' if data.avatar_ready else '未缓存'}",
            f"当前协议端昵称：{current or '（读取失败）'}",
        ]
        if data.clone_names:
            lines.append(f"记录过的克隆昵称（{len(data.clone_names)}）：" + "、".join(list(data.clone_names)[:8]))
        if data.worn:
            worn = "、".join(f"{k}→{v}" for k, v in list(data.worn.items())[:5])
            lines.append(f"会话占用：{worn}")

        if current and data.is_clone_name(current):
            owner = data.worn_owner(current) or {}
            lines.append(
                f"⚠️ 当前昵称是克隆昵称（来自 {owner.get('user_id', '未知')}），"
                f"执行「恢复人格」可还原。"
            )
        elif data.ready and current and current != data.nickname:
            lines.append("⚠️ 当前昵称与备份不一致，可执行「恢复人格」还原。")
        else:
            lines.append("✅ 当前昵称与备份一致。")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("还原机器人资料")
    async def remember_bot_identity(self, event: AiocqhttpMessageEvent):
        """
        还原机器人资料 —— 把「当前」昵称/头像记为机器人原始资料
        """
        try:
            info = await event.bot.get_login_info()
        except Exception as e:
            yield event.plain_result(f"获取机器人资料失败：{e}")
            return

        nickname = str(info.get("nickname") or "").strip()
        bot_user_id = str(info.get("user_id") or "").strip()
        if not nickname and not bot_user_id:
            yield event.plain_result("没有读到机器人昵称/QQ 号，无法记录")
            return

        # 先把旧的克隆昵称记录清掉，避免把「当前这个克隆昵称」当成真名
        data = self.identity.load()
        if nickname and data.is_clone_name(nickname):
            yield event.plain_result(
                f"当前昵称「{nickname}」正是我们用过的克隆昵称，"
                f"请先在 QQ 里把机器人昵称改成真正的名字，再执行本命令。"
            )
            return

        self.identity.remember_original(
            nickname=nickname, user_id=bot_user_id, overwrite=True
        )
        avatar_b64 = await self._avatar_downloader(bot_user_id)
        if avatar_b64:
            self.identity.remember_original(
                avatar_b64=avatar_b64, user_id=bot_user_id, overwrite=True
            )
        self.identity.clear_worn()

        msg = f"已把当前资料记为机器人原始资料：昵称【{nickname or '（空）'}】。"
        if avatar_b64:
            msg += f" 头像已备份（{len(avatar_b64) * 3 // 4 // 1024} KB）。"
        else:
            msg += "\n⚠️ 头像下载失败，未备份头像。"
        yield event.plain_result(msg)
