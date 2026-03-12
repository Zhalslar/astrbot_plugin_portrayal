from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from astrbot.api import logger

from .config import PluginConfig
from .model import UserProfile


@dataclass(slots=True)
class PersonCandidate:
    person_name: str
    reason: str
    search_query: str


@dataclass(slots=True)
class ImageSearchHit:
    title: str
    image_url: str
    thumbnail_url: str
    source: str
    source_link: str


@dataclass(slots=True)
class PersonImageMatch:
    person_name: str
    reason: str
    search_query: str
    image_url: str
    local_path: Path
    source: str
    source_link: str


class ImageSearchService:
    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def match_person_by_portrait(
        self,
        portrait: str,
        profile: UserProfile,
        *,
        preference: str | None = None,
        umo: str | None = None,
    ) -> PersonImageMatch:
        if not self.cfg.image_search.is_ready():
            raise RuntimeError("搜图功能未启用或未配置 API Key")

        candidate = await self._infer_candidate(
            portrait,
            profile,
            preference=preference,
            umo=umo,
        )
        hits = await self._search_images(candidate.search_query)
        if not hits:
            raise RuntimeError("搜图 API 未返回可用图片")

        last_error: Exception | None = None
        for hit in hits:
            for image_url in (hit.image_url, hit.thumbnail_url):
                if not image_url:
                    continue
                try:
                    local_path = await self._download_image(
                        image_url=image_url,
                        user_id=profile.user_id,
                    )
                    return PersonImageMatch(
                        person_name=candidate.person_name,
                        reason=candidate.reason,
                        search_query=candidate.search_query,
                        image_url=image_url,
                        local_path=local_path,
                        source=hit.source,
                        source_link=hit.source_link,
                    )
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"下载搜图结果失败：{image_url} -> {exc}")

        raise RuntimeError(f"没有可下载的搜图结果: {last_error}")

    async def _infer_candidate(
        self,
        portrait: str,
        profile: UserProfile,
        *,
        preference: str | None = None,
        umo: str | None = None,
    ) -> PersonCandidate:
        provider = self.cfg.get_provider(umo=umo)
        preference = preference or self.cfg.image_search.normalized_preference()
        system_prompt = (
            "你是一个根据人物画像匹配公众人物或虚构角色的助手。"
            "请根据输入画像，挑选一个最贴近的人物，并输出严格 JSON。"
            'JSON 格式为 {"person_name":"", "reason":"", "search_query":""}。'
            "person_name 必须是具体人名或角色名；reason 不超过 50 字；"
            "search_query 用于搜图，必须是适合搜索图片的短语。"
            "不要输出 JSON 以外的任何内容。"
        )
        prompt = (
            f"目标用户昵称：{profile.nickname or profile.user_id}\n"
            f"偏好类型：{self._preference_prompt(preference)}\n"
            f"目标用户画像：\n{portrait}\n\n"
            "请给出最像的一个人物，并生成适合搜图 API 使用的搜索词。"
        )

        resp = await provider.text_chat(system_prompt=system_prompt, prompt=prompt)
        data = self._extract_json(resp.completion_text)

        person_name = str(data.get("person_name", "")).strip()
        reason = str(data.get("reason", "")).strip()
        search_query = str(data.get("search_query", "")).strip()

        if not person_name:
            raise RuntimeError("LLM 未返回匹配人物")
        if not search_query:
            search_query = self._build_fallback_query(person_name, preference)
        if not reason:
            reason = "该人物与画像描述最接近。"

        logger.debug(
            f"画像匹配人物成功：{profile.user_id} -> {person_name} / {search_query}"
        )
        return PersonCandidate(
            person_name=person_name,
            reason=reason,
            search_query=search_query,
        )

    async def _search_images(self, query: str) -> list[ImageSearchHit]:
        params = {
            "engine": self.cfg.image_search.engine,
            "q": query,
            "api_key": self.cfg.image_search.api_key,
            "hl": self.cfg.image_search.language,
            "gl": self.cfg.image_search.country,
            "safe": "active",
        }

        async with httpx.AsyncClient(
            timeout=self.cfg.image_search.request_timeout_sec,
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.cfg.image_search.endpoint, params=params)
            resp.raise_for_status()
            payload = resp.json()

        if error_msg := payload.get("error"):
            raise RuntimeError(f"搜图 API 返回错误：{error_msg}")

        items = payload.get("images_results") or []
        hits: list[ImageSearchHit] = []
        for item in items[: self.cfg.image_search.result_limit]:
            hits.append(
                ImageSearchHit(
                    title=str(item.get("title", "")).strip(),
                    image_url=str(item.get("original", "")).strip(),
                    thumbnail_url=str(item.get("thumbnail", "")).strip(),
                    source=str(item.get("source", "")).strip(),
                    source_link=str(item.get("link", "")).strip(),
                )
            )

        return hits

    @staticmethod
    def _preference_prompt(preference: str) -> str:
        mapping = {
            "auto": "自动判断，不限制人物来源。",
            "anime": "优先匹配动漫、游戏、二次元角色。",
            "film_tv": "优先匹配电影、电视剧、综艺中的人物或角色。",
            "historical": "优先匹配历史人物、古代人物、近现代名人。",
            "real_person": "优先匹配现实世界中的公众人物、明星、博主、运动员等。",
        }
        return mapping[preference]

    @staticmethod
    def _build_fallback_query(person_name: str, preference: str) -> str:
        suffix = {
            "auto": "人物照片",
            "anime": "角色 立绘",
            "film_tv": "角色 剧照",
            "historical": "人物画像",
            "real_person": "人物照片",
        }
        return f"{person_name} {suffix[preference]}"

    async def _download_image(self, image_url: str, user_id: str) -> Path:
        digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:12]
        suffix = self._guess_suffix(image_url)
        filename = f"portrait_match_{user_id}_{digest}{suffix}"
        target = self.cfg.cache_dir / filename
        if target.exists():
            return target

        async with httpx.AsyncClient(
            timeout=self.cfg.image_search.request_timeout_sec,
            follow_redirects=True,
        ) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if not content_type.startswith("image/"):
                raise RuntimeError(f"返回内容不是图片: {content_type or 'unknown'}")
            if target.suffix == ".img":
                target = target.with_suffix(self._suffix_from_content_type(content_type))
            target.write_bytes(resp.content)

        return target

    @staticmethod
    def _extract_json(text: str) -> dict:
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
        if match:
            text = match.group(1)
        else:
            match = re.search(r"(\{.*\})", text, re.S)
            if match:
                text = match.group(1)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"无法解析人物匹配 JSON：{exc}") from exc

        if not isinstance(data, dict):
            raise RuntimeError("人物匹配结果不是 JSON 对象")
        return data

    @staticmethod
    def _guess_suffix(url: str) -> str:
        path = urlparse(url).path
        suffix = Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return suffix
        return ".img"

    @staticmethod
    def _suffix_from_content_type(content_type: str) -> str:
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "gif" in content_type:
            return ".gif"
        return ".img"
