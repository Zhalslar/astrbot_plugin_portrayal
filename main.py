import base64
import time

import aiohttp

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Node, Nodes, Plain
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


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.llm = LLMService(self.cfg)
        self.style = None

    async def initialize(self):
        """加载插件时调用"""
        try:
            import pillowmd

            self.style = pillowmd.LoadMarkdownStyles(self.cfg.style_dir)
        except Exception as e:
            logger.error(f"无法加载pillowmd样式：{e}")

    async def terminate(self):
        self.msg.clear_cache()

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

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = event.message_str.partition(" ")[0]
        is_clone = True if "克隆" in cmd else False
        prompt = self.entry_service.get_entry(cmd)
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
        end_param = event.message_str.split(" ")[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        # 获取基本信息
        info = await event.bot.get_stranger_info(user_id=int(target_id), no_cache=True)
        profile = UserProfile.from_qq_data(target_id, data=dict(info))
        if old_profile := self.db.get(target_id):
            profile.portrait = old_profile.portrait
            profile.timestamp = old_profile.timestamp
            profile.clone_prompt = old_profile.clone_prompt

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
                f"正在分析{cmd}..."
            )
        else:
            yield event.plain_result(
                f"已从{result.scanned_messages}条群消息中提取到"
                f"{result.count}条{profile.nickname}的聊天记录，正在{cmd}..."
            )

        # LLM 分析画像
        try:
            content = await self.llm.generate_portrait(
                result.texts,
                profile,
                prompt.content,
                umo=event.unified_msg_origin,
            )
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败：{e}")
            return

        # 保存克隆人格并发送
        if is_clone:
            profile.clone_prompt = content
            self.db.set(profile)
            nodes = Nodes(
                [
                    Node(
                        uin=profile.user_id,
                        name=f"克隆的{profile.nickname}",
                        content=[Plain(content)],
                    )
                ]
            )
            yield event.chain_result([nodes])
            return

        # 保存画像并发送
        profile.portrait = content
        profile.timestamp = int(time.time())
        self.db.set(profile)
        if self.style:
            img = await self.style.AioRender(text=content, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            yield event.image_result(str(img_path))
        else:
            nodes = Nodes(
                [
                    Node(
                        uin=profile.user_id,
                        name=profile.nickname,
                        content=[Plain(content)],
                    )
                ]
            )
            yield event.chain_result([nodes])

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

        # 切换前保存 bot 原始 昵称 / 头像字节，用于后续 "恢复人格"
        # 仅在首次切换时缓存，避免被克隆名字 / 头像覆盖
        # 注意：头像不能仅存 URL（dst_uin=bot_id 总是指向"当前头像"，
        # 一旦改完就回来的不是原图了），必须把字节快照保存下来
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

        # 切换时一并清空当前对话历史，避免旧上下文污染；
        # 同时绕开 “bot 昵称变更后 /reset 唤醒失败” 的问题
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

        # 取默认人格 id；找不到就回落到 "default"
        cfg = self.context.get_config(umo=umo)
        default_persona_id = (
            cfg.get("provider_settings", {}).get("default_personality") or "default"
        )

        if cid:
            await self.context.conversation_manager.update_conversation_persona_id(
                umo, default_persona_id
            )
            # 顺手清空历史，相当于内置 /reset。
            # 切换人格后 bot 昵称被改，按名称唤醒的场景下 /reset 可能无法触发，
            # 在恢复指令里直接清掉历史，让用户不再依赖 /reset。
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
