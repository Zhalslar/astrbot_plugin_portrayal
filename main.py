from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.config import PluginConfig
from .core.message import MessageManager
from .core.profile_service import UserProfileService
from .core.llm import LLMService


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.msg = MessageManager(self.cfg)
        self.profile_service = UserProfileService()
        self.llm = LLMService(context, self.cfg)

    async def terminate(self):
        self.msg.clear_cache()

    @staticmethod
    def get_at_id(event: AiocqhttpMessageEvent) -> str | None:
        return next(
            (
                str(seg.qq)
                for seg in event.get_messages()
                if (isinstance(seg, At)) and str(seg.qq) != event.get_self_id()
            ),
            None,
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def get_portrayal(self, event: AiocqhttpMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        cmd = event.message_str.partition(" ")[0]
        prompt = self.cfg.match_prompt_by_cmd(cmd)
        if not prompt:
            return
        target_id = self.get_at_id(event) or event.get_sender_id()

        # ---------- 用户画像 ----------
        profile = await self.profile_service.get_profile(event, target_id)

        # ---------- 查询轮数 ----------
        end_param = event.message_str.split(" ")[-1]
        query_rounds = self.cfg.message.get_query_rounds(end_param)

        yield event.plain_result(
            f"正在发起{query_rounds}轮查询来获取{profile.nickname}的聊天记录..."
        )

        # ---------- 消息 ----------
        result = await self.msg.get_user_texts(
            event,
            profile.user_id,
            max_rounds=query_rounds,
        )

        if result.is_empty:
            yield event.plain_result("没有查询到该群友的任何消息")
            return

        yield event.plain_result(
            f"已从{result.scanned_messages}条群消息中提取到"
            f"{result.count}条{profile.nickname}的聊天记录，正在分析{cmd}..."
        )

        # ---------- LLM ----------
        try:
            respond = await self.llm.generate_portrait(result.texts, profile, prompt)
            yield event.plain_result(respond)
        except Exception as e:
            logger.error(f"LLM 调用失败：{e}")
            yield event.plain_result(f"分析失败：{e}")
