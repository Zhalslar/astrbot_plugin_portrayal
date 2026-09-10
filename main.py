import base64
import time

import aiohttp

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

from .core.config import PluginConfig
from .core.db import UserProfileDB
from .core.entry import EntryService
from .core.llm import LLMService
from .core.message import MessageManager
from .core.model import UserProfile

# 「改人格」的三种模式前缀
APPEND_PREFIX = "追加："
RESET_PREFIX = "重置："

# 超出该长度时额外提示「不建议直接群发」
MAX_SAFE_PROMPT_LEN = 2000


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(self.cfg)

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

        优先取消息链里的 At 段；若客户端没有生成 At 段（例如用户直接手打 QQ 号），
        则退化为把命令词之后第一个纯数字 token 当作目标 QQ 号。
        """
        segments = event.get_messages()
        ats = [str(seg.qq) for seg in segments if isinstance(seg, At)]
        target_id = ats[0] if ats else ""
        if target_id:
            texts: list[str] = []
            hit_at = False
            for seg in segments:
                if isinstance(seg, At):
                    hit_at = True
                    continue
                if not hit_at:
                    continue
                text = getattr(seg, "text", None)
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())

            payload = " ".join(texts).strip()
            if payload.startswith(target_id):
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
        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
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
        if not target_id:
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

        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
        if not ats:
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return

        # 检查权限
        target_id = ats[0]
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        # 解析查询轮数
        end_param = event.message_str.split()[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        # 获取基本信息（沿用旧档案里已有的画像与克隆人格，避免被空值覆盖）
        info = await event.bot.get_stranger_info(user_id=int(target_id), no_cache=True)
        profile = UserProfile.from_qq_data(target_id, data=dict(info))
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
        if not target_id:
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

        # 重置模式允许目标用户还没有档案，自动拉取陌生人资料建档
        if not profile and instruction.startswith(RESET_PREFIX):
            try:
                info = await event.bot.get_stranger_info(
                    user_id=int(target_id), no_cache=True
                )
            except Exception as e:
                logger.error(f"获取用户资料失败：{e}")
                yield event.plain_result(f"获取该用户资料失败：{e}")
                return
            profile = UserProfile.from_qq_data(target_id, data=dict(info))

        if not profile:
            yield event.plain_result(
                "本地暂无该用户档案，请先执行“克隆人格 @群友”；"
                "若只想手工写入完整人格，可用“改人格 @群友 重置：<完整人格>”"
            )
            return

        old_clone_prompt = profile.clone_prompt.strip()

        # 模式一：追加（不走 LLM）
        if instruction.startswith(APPEND_PREFIX):
            add_text = instruction[len(APPEND_PREFIX) :].strip()
            if not add_text:
                yield event.plain_result(
                    f"追加内容不能为空，正确用法：改人格 @{profile.nickname} 追加：<补充文字>"
                )
                return
            if not old_clone_prompt:
                yield event.plain_result(
                    f"【{profile.nickname}】暂无可用的克隆人格，"
                    f"请先执行“克隆人格 @{profile.nickname}”"
                )
                return
            content = f"{old_clone_prompt}\n{add_text}"
            mode_desc = "追加"

        # 模式二：重置（不走 LLM）
        elif instruction.startswith(RESET_PREFIX):
            content = instruction[len(RESET_PREFIX) :].strip()
            if not content:
                yield event.plain_result(
                    f"重置内容不能为空，正确用法：改人格 @{profile.nickname} 重置：<完整人格>"
                )
                return
            mode_desc = "重置"

        # 模式三：LLM 重写
        else:
            if not instruction:
                yield event.plain_result(
                    f"请补充修改要求，例如：改人格 @{profile.nickname} 说话更简短一点"
                )
                return
            if not old_clone_prompt:
                yield event.plain_result(
                    f"【{profile.nickname}】暂无可用的克隆人格，"
                    f"请先执行“克隆人格 @{profile.nickname}”"
                )
                return
            try:
                content = await self.llm.generate_persona_edit(
                    old_clone_prompt,
                    instruction,
                    profile,
                    self.cfg.get_edit_prompt(),
                    umo=event.unified_msg_origin,
                )
            except Exception as e:
                logger.error(f"LLM 调用失败：{e}")
                yield event.plain_result(f"修改失败：{e}，已保留原有人格")
                return
            if not content or not content.strip():
                yield event.plain_result("修改结果为空，已保留原有人格")
                return
            content = content.strip()
            mode_desc = "重写"

        profile.clone_prompt = content
        profile.timestamp = int(time.time())
        self.db.set(profile)

        yield event.plain_result(
            f"【{profile.nickname}】的克隆人格已{mode_desc}（{len(content)} 字）。"
            f"\n执行“切换人格 @{profile.nickname}”后生效。"
            f"\n发送“查看克隆 @{profile.nickname}”可查看当前人格全文。"
        )
        if len(content) > MAX_SAFE_PROMPT_LEN:
            yield event.plain_result(
                f"提示：当前人格 {len(content)} 字，超过 {MAX_SAFE_PROMPT_LEN} 字，"
                f"不建议直接群发全文。"
            )

    # =========================
    # 切换 / 恢复人格
    # =========================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换人格")
    async def switch_persona(self, event: AiocqhttpMessageEvent):
        """
        切换人格 @群友
        """
        ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
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

        # 切换前保存 bot 原始昵称 / 头像字节
        saved_info = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_original_bot_info",
            default=None,
        )
        if not saved_info:
            try:
                login_info = await event.bot.get_login_info()
                bot_user_id = str(login_info.get("user_id", ""))
                avatar_b64 = ""
                if bot_user_id:
                    avatar_url = (
                        f"https://q4.qlogo.cn/headimg_dl?dst_uin={bot_user_id}&spec=640"
                    )
                    try:
                        timeout = aiohttp.ClientTimeout(total=15)
                        async with aiohttp.ClientSession(timeout=timeout) as session:
                            async with session.get(avatar_url) as resp:
                                resp.raise_for_status()
                                avatar_bytes = await resp.read()
                                avatar_b64 = base64.b64encode(avatar_bytes).decode()
                    except Exception as e:
                        logger.warning(f"下载 bot 原始头像失败：{e}")

                await sp.put_async(
                    scope="umo",
                    scope_id=umo,
                    key="portrayal_original_bot_info",
                    value={
                        "nickname": login_info.get("nickname", ""),
                        "user_id": bot_user_id,
                        "avatar_b64": avatar_b64,
                    },
                )
            except Exception as e:
                logger.warning(f"获取 bot 原始资料失败：{e}")

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

        yield event.plain_result(
            f"已将当前对话切换为【{profile.nickname}】的克隆人格，对话历史已清空。"
            f"如需还原，请使用：恢复人格。{force_warn_msg}"
        )

        # 同步 bot 昵称
        await event.bot.set_qq_profile(nickname=profile.nickname)
        logger.debug(f"已同步bot的昵称为: {profile.nickname}")

        # 同步 bot 头像
        avatar_url = (
            f"https://q4.qlogo.cn/headimg_dl?dst_uin={profile.user_id}&spec=640"
        )
        await event.bot.set_qq_avatar(file=avatar_url)
        logger.debug(f"已同步bot的头像为: {avatar_url}")

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

        # 还原 bot 昵称 / 头像
        original_info = await sp.get_async(
            scope="umo",
            scope_id=umo,
            key="portrayal_original_bot_info",
            default=None,
        )

        restored_nickname = ""
        avatar_restored = False
        if original_info:
            nickname = original_info.get("nickname", "")
            avatar_b64 = original_info.get("avatar_b64", "")
            try:
                if nickname:
                    await event.bot.set_qq_profile(nickname=nickname)
                    restored_nickname = nickname
                    logger.debug(f"已还原bot的昵称为: {nickname}")
                if avatar_b64:
                    # 用 base64 字节原样塞回去；不能用 dst_uin URL，
                    # 因为那个 URL 在切换后已经指向克隆群友的头像了
                    await event.bot.set_qq_avatar(file=f"base64://{avatar_b64}")
                    avatar_restored = True
                    logger.debug("已用缓存的原图还原bot头像")
            except Exception as e:
                logger.error(f"还原 bot 资料失败：{e}")
            # 还原成功后清掉缓存的原始信息
            await sp.remove_async(
                scope="umo",
                scope_id=umo,
                key="portrayal_original_bot_info",
            )

        msg = f"已恢复默认人格【{default_persona_id}】，对话历史已清空。"
        if restored_nickname:
            msg += f" bot 昵称已还原为【{restored_nickname}】。"
        if original_info and not avatar_restored:
            msg += "（头像原图未缓存或还原失败，需手动恢复）"
        elif not original_info:
            msg += "（未找到原始 bot 资料缓存，昵称/头像需手动恢复）"

        yield event.plain_result(msg)
