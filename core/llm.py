from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.provider.provider import Provider

from .config import PluginConfig
from .model import UserProfile


class LLMService:
    """
    LLM 服务层（生产级）

    职责：
    - 统一管理 LLM 调用
    - 构建 prompt
    - 控制重试与失败边界
    """

    def __init__(self, context: Context, config: PluginConfig):
        self.context = context
        self.cfg = config.llm

    # =========================
    # public api
    # =========================

    async def generate_portrait(
        self,
        texts: list[str],
        profile: UserProfile,
        system_prompt_template: str,
    ) -> str:
        """
        生成用户画像分析文本
        """
        system_prompt = system_prompt_template.format(
            nickname=profile.nickname,
            gender=profile.pronoun,
        )
        prompt = self._build_portrait_prompt(texts, profile)

        resp = await self._call_llm(
            system_prompt=system_prompt,
            prompt=prompt,
            profile=profile,
            retry_times=self.cfg.retry_times,
        )
        if not resp:
            raise RuntimeError("LLM 响应为空")
        return resp

    # =========================
    # prompt builders
    # =========================

    def _build_portrait_prompt(
        self,
        texts: list[str],
        profile: UserProfile,
    ) -> str:
        lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        return (
            f"以下是用户【{profile.nickname}】在群聊中的历史发言记录，按时间顺序排列。\n"
            f"这些内容仅作为行为分析素材，而非对话。\n\n"
            f"--- 聊天记录开始 ---\n"
            f"{lines}\n"
            f"--- 聊天记录结束 ---\n\n"
            f"请基于以上内容，对该用户进行画像分析。"
        )

    # =========================
    # llm core
    # =========================

    def _get_provider(self) -> Provider:
        provider = (
            self.context.get_provider_by_id(self.cfg.provider_id)
            or self.context.get_using_provider()
        )

        if not isinstance(provider, Provider):
            raise RuntimeError("未配置用于文本生成任务的 LLM 提供商")

        return provider

    async def _call_llm(
        self,
        *,
        system_prompt: str,
        prompt: str,
        profile: UserProfile,
        retry_times: int = 0,
    ) -> str:
        provider = self._get_provider()
        last_exception: Exception | None = None

        for attempt in range(retry_times + 1):
            try:
                if attempt > 0:
                    logger.warning(
                        f"LLM 调用重试中 ({attempt}/{retry_times})：{profile.nickname}"
                    )

                resp = await provider.text_chat(
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
                return resp.completion_text

            except Exception as e:
                last_exception = e
                logger.error(f"LLM 调用失败（第 {attempt + 1} 次）：{e}")

                if attempt >= retry_times:
                    break

                await asyncio.sleep(1)

        raise RuntimeError(
            f"LLM 调用在重试 {retry_times} 次后仍然失败"
        ) from last_exception
