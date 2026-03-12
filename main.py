import time

from astrbot.api import logger, sp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Node, Nodes, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.provider.entities import ProviderRequest

from .core.config import PluginConfig
from .core.db import UserProfileDB
from .core.entry import EntryService
from .core.image_search import ImageSearchService, PersonImageMatch
from .core.llm import LLMService
from .core.message import MessageManager
from .core.model import UserProfile
from .core.utils import get_at_id


class PortrayalPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.db = UserProfileDB(self.cfg)
        self.msg = MessageManager(self.cfg)
        self.entry_service = EntryService(self.cfg)
        self.image_search = ImageSearchService(self.cfg)
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
        target_id = get_at_id(event)
        if not target_id:
            yield event.plain_result("命令格式：查看画像 @群友")
            return
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return
        profile = self.db.get(target_id)
        if not profile:
            yield event.plain_result("本地暂无该用户画像记录")
            return
        async for result in self._yield_portrait_result(event, profile):
            yield result

    @filter.command("找人物")
    @filter.command("找画像")
    async def find_portrayal_person(self, event: AiocqhttpMessageEvent):
        """
        找画像/找人物 @群友 [偏好]
        """
        if not self.cfg.image_search.enabled:
            yield event.plain_result("搜图功能未开启，请先在插件配置中启用")
            return
        if not self.cfg.image_search.api_key.strip():
            yield event.plain_result("搜图功能未配置 API Key，请先在插件配置中填写")
            return

        target_id = get_at_id(event) or event.get_sender_id()
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        profile = self.db.get(target_id)
        if not profile or not profile.portrait.strip():
            yield event.plain_result("本地暂无该用户画像，请先执行“画像 @群友”")
            return

        preference = self._resolve_preference_from_command(event.message_str)
        if preference is False:
            yield event.plain_result(
                f"偏好参数无效，可用值：{self.cfg.image_search.preference_help_text()}\n"
            )
            return
        preference_label = self._get_preference_label(preference)

        async for result in self._yield_portrait_result(event, profile):
            yield result
        yield event.plain_result(
            f"正在根据【{profile.nickname or target_id}】的画像匹配人物并搜图"
            f"（当前偏好：{preference_label}）..."
        )
        match = await self._match_person_image(
            profile,
            profile.portrait,
            preference=preference,
            umo=event.unified_msg_origin,
        )
        if not match:
            yield event.plain_result("没有找到可用的人物图片")
            return

        yield event.plain_result(self._format_match_message(match))
        yield event.image_result(str(match.local_path))

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
        画像 @群友 <查询轮数> [偏好]
        """
        cmd = event.message_str.partition(" ")[0]
        is_clone = True if "克隆" in cmd else False
        prompt = self.entry_service.match_prompt_by_cmd(cmd)
        if not prompt:
            return

        target_id = get_at_id(event)
        if not target_id:
            yield event.plain_result("命令格式：画像 @群友 <查询轮数>")
            return

        # 检查权限
        if self.cfg.message.is_protected_user(target_id):
            yield event.plain_result("该用户在保护名单中，不允许查询")
            return

        # 解析查询轮数
        query_rounds = self.cfg.message.default_query_rounds
        for token in self._iter_command_tokens(event.message_str):
            if token.isdigit():
                query_rounds = self.cfg.message.get_query_rounds(token)

        preference = self._resolve_preference_from_command(event.message_str)
        if preference is False:
            yield event.plain_result(
                f"偏好参数无效，可用值：{self.cfg.image_search.preference_help_text()}\n"
            )
            return

        # 获取基本信息
        info = await event.bot.get_stranger_info(user_id=int(target_id), no_cache=True)
        profile = UserProfile.from_qq_data(target_id, data=dict(info))
        if old_profile := self.db.get(target_id):
            profile.portrait = old_profile.portrait
            profile.timestamp = old_profile.timestamp
            profile.clone_prompt = old_profile.clone_prompt
            profile.matched_person = old_profile.matched_person
            profile.matched_person_reason = old_profile.matched_person_reason
            profile.matched_search_query = old_profile.matched_search_query
            profile.matched_image_url = old_profile.matched_image_url

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
        async for result in self._yield_portrait_result(event, profile):
            yield result

        match = await self._match_person_image(
            profile,
            content,
            preference=preference,
            umo=event.unified_msg_origin,
        )
        if match:
            yield event.plain_result(self._format_match_message(match))
            yield event.image_result(str(match.local_path))

    @filter.command("切换人格")
    async def switch_persona(self, event: AiocqhttpMessageEvent):
        """
        切换人格 @群友
        """
        target_id = get_at_id(event)
        if not target_id:
            yield event.plain_result("命令格式：切换人格 @群友")
            return

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

    async def _match_person_image(
        self,
        profile: UserProfile,
        portrait: str,
        *,
        preference: str | None = None,
        umo: str | None = None,
    ) -> PersonImageMatch | None:
        if not self.cfg.image_search.is_ready():
            return None

        try:
            match = await self.image_search.match_person_by_portrait(
                portrait,
                profile,
                preference=preference,
                umo=umo,
            )
        except Exception as e:
            logger.warning(f"搜图流程失败：{profile.user_id} -> {e}")
            return None

        profile.matched_person = match.person_name
        profile.matched_person_reason = match.reason
        profile.matched_search_query = match.search_query
        profile.matched_image_url = match.image_url
        self.db.set(profile)
        return match

    @staticmethod
    def _format_match_message(match: PersonImageMatch) -> str:
        extra = f"\n来源：{match.source_link}" if match.source_link else ""
        return (
            f"根据画像匹配到的人物：{match.person_name}\n"
            f"匹配原因：{match.reason}\n"
            f"搜图关键词：{match.search_query}\n"
            "如需指定风格，可在命令末尾追加：anime / film_tv / historical / real_person"
            f"{extra}"
        )

    async def _yield_portrait_result(
        self,
        event: AiocqhttpMessageEvent,
        profile: UserProfile,
    ):
        if self.style:
            img = await self.style.AioRender(text=profile.portrait, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            yield event.image_result(str(img_path))
            return

        nodes = Nodes(
            [
                Node(
                    uin=profile.user_id,
                    name=profile.nickname,
                    content=[Plain(profile.portrait)],
                )
            ]
        )
        yield event.chain_result([nodes])

    @staticmethod
    def _iter_command_tokens(message_str: str) -> list[str]:
        parts = message_str.strip().split()
        return parts[1:] if len(parts) > 1 else []

    def _resolve_preference_from_command(self, message_str: str) -> str | bool | None:
        tokens = self._iter_command_tokens(message_str)
        if not tokens:
            return None

        saw_preference_marker = False
        for token in tokens:
            normalized = self.cfg.image_search.normalize_preference_value(token)
            if normalized:
                return normalized
            if any(marker in token.lower() for marker in ("偏好", "preference")):
                saw_preference_marker = True

        if saw_preference_marker:
            return False
        return None

    def _get_preference_label(self, preference: str | None) -> str:
        if preference:
            labels = {
                "auto": "自动",
                "anime": "二次元",
                "film_tv": "影视作品",
                "historical": "历史人物",
                "real_person": "现实人物",
            }
            return labels[preference]
        return self.cfg.image_search.preference_label()
