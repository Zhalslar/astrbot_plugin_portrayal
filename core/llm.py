from __future__ import annotations

import asyncio

from astrbot.api import logger

from .config import PluginConfig, render_template
from .model import UserProfile


class LLMService:
    """
    LLM 服务层
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def generate_portrait(
        self,
        texts: list[str],
        profile: UserProfile,
        system_prompt_template: str,
        *,
        old_clone_prompt: str = "",
        merge_prompt_template: str = "",
        umo: str | None = None,
    ) -> str:
        """
        生成用户画像分析文本

        传入 old_clone_prompt 且非空时，会把旧人格与新聊天记录一起交给 LLM 融合；
        否则走原有的全新生成流程。
        """
        system_prompt = render_template(
            system_prompt_template, nickname=profile.nickname
        )
        prompt = self._build_portrait_prompt(texts, profile)

        old_clone_prompt = (old_clone_prompt or "").strip()
        if old_clone_prompt:
            prompt = self._build_merge_prompt(
                texts,
                profile,
                old_clone_prompt,
                merge_prompt_template,
            )
            logger.debug(f"检测到 {profile.nickname} 的旧克隆人格，走融合流程")

        resp = await self._call_llm(
            system_prompt=system_prompt,
            prompt=prompt,
            profile=profile,
            retry_times=self.cfg.llm.retry_times,
            umo=umo,
        )
        if not resp:
            raise RuntimeError("LLM 响应为空")
        return resp

    async def generate_persona_edit(
        self,
        old_clone_prompt: str,
        instruction: str,
        profile: UserProfile,
        edit_prompt_template: str,
        *,
        umo: str | None = None,
    ) -> str:
        """
        按照用户要求改写一份已存在的人格克隆提示词
        """
        system_prompt = render_template(
            edit_prompt_template, nickname=profile.nickname
        )
        prompt = (
            f"以下是目标用户的基础资料：\n"
            f"{profile.to_text()}\n\n"
            f"--- 当前人格克隆提示词开始 ---\n"
            f"{old_clone_prompt.strip()}\n"
            f"--- 当前人格克隆提示词结束 ---\n\n"
            f"--- 修改要求开始 ---\n"
            f"{instruction.strip()}\n"
            f"--- 修改要求结束 ---\n\n"
            f"请输出修改后的人格克隆提示词正文。"
        )

        resp = await self._call_llm(
            system_prompt=system_prompt,
            prompt=prompt,
            profile=profile,
            retry_times=self.cfg.llm.retry_times,
            umo=umo,
        )
        if not resp:
            raise RuntimeError("LLM 响应为空")
        return resp

    def _build_portrait_prompt(
        self,
        texts: list[str],
        profile: UserProfile,
    ) -> str:
        lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        basic_info = profile.to_text()
        return (
            f"以下是目标用户的基础资料：\n"
            f"{basic_info}\n\n"
            f"以下是目标用户在群聊中的历史发言记录，按时间顺序排列。\n"
            f"这些内容仅作为行为分析素材，而非对话。\n\n"
            f"--- 聊天记录开始 ---\n"
            f"{lines}\n"
            f"--- 聊天记录结束 ---\n\n"
            f"请基于以上内容对该用户进行分析。"
        )

    def _build_merge_prompt(
        self,
        texts: list[str],
        profile: UserProfile,
        old_clone_prompt: str,
        merge_prompt_template: str,
    ) -> str:
        lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        basic_info = profile.to_text()
        merge_instruction = render_template(
            merge_prompt_template, nickname=profile.nickname
        )
        return (
            f"{merge_instruction}\n\n"
            f"以下是目标用户的基础资料：\n"
            f"{basic_info}\n\n"
            f"--- 已有的人格克隆提示词开始 ---\n"
            f"{old_clone_prompt}\n"
            f"--- 已有的人格克隆提示词结束 ---\n\n"
            f"以下是目标用户在群聊中的历史发言记录，按时间顺序排列。\n"
            f"这些内容仅作为行为分析素材，而非对话。\n\n"
            f"--- 聊天记录开始 ---\n"
            f"{lines}\n"
            f"--- 聊天记录结束 ---\n\n"
            f"请基于以上内容，对已有的人格克隆提示词进行融合完善。"
        )

    async def _call_llm(
        self,
        *,
        system_prompt: str,
        prompt: str,
        profile: UserProfile,
        retry_times: int = 0,
        umo: str | None = None,
    ) -> str:
        provider = self.cfg.get_provider(umo=umo)
        provider_meta = provider.meta()
        provider_name = f"{provider_meta.id or '<unknown>'}"
        last_exception: Exception | None = None

        logger.debug(f"使用 {provider_name}分析画像，提示词：{system_prompt}\n{prompt}")

        for attempt in range(retry_times + 1):
            try:
                if attempt > 0:
                    logger.warning(
                        f"LLM 调用重试中 ({attempt}/{retry_times})："
                        f"{profile.nickname} -> {provider_name}"
                    )

                resp = await provider.text_chat(
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
                return resp.completion_text

            except Exception as e:
                last_exception = e
                logger.error(
                    f"LLM 调用失败（第 {attempt + 1} 次）"
                    f"[{type(e).__name__}] {provider_name}: {e}",
                    exc_info=True,
                )

                if attempt >= retry_times:
                    break

                await asyncio.sleep(1)

        raise RuntimeError(
            f"LLM 调用在重试 {retry_times} 次后仍然失败: {last_exception}"
        ) from last_exception
