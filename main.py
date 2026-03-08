import time

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
        prompt = self.entry_service.match_prompt_by_cmd(cmd)
        if not prompt:
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
                f"{result.count}条{profile.nickname}的聊天记录，正在分析{cmd}..."
            )

        # LLM 分析画像
        try:
            content = await self.llm.generate_portrait(
                result.texts,
                profile,
                prompt,
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

        persona_id = f"portrayal_clone_{profile.user_id}"
        try:
            await self.context.persona_manager.update_persona(
                persona_id=persona_id,
                system_prompt=profile.clone_prompt,
            )
        except ValueError:
            await self.context.persona_manager.create_persona(
                persona_id=persona_id,
                system_prompt=profile.clone_prompt,
            )

        await self.context.conversation_manager.update_conversation_persona_id(
            umo, persona_id
        )
        force_warn_msg = ""
        if force_applied_persona_id:
            force_warn_msg = "提醒：由于自定义规则，您现在切换的人格将不会生效。"

        yield event.plain_result(
            f"已将当前对话切换为【{profile.nickname}】的克隆人格。"
            f"如需避免旧上下文影响，请使用 /reset。{force_warn_msg}"
        )
