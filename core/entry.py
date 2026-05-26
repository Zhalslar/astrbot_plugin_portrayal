# config.py
from __future__ import annotations

from typing import Any

import yaml

from astrbot.api import logger

from .config import PluginConfig, PromptEntry


class EntryService:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._migrate_entry_storage()

        # 加载用户配置
        self.entries: list[PromptEntry] = [
            PromptEntry(item) for item in self.cfg.entry_storage
        ]
        self._load_prompts()
        logger.debug(f"已注册命令：{[e.command for e in self.entries]}")

    def _load_prompts(self) -> None:
        with self.cfg.builtin_prompt_file.open("r", encoding="utf-8") as f:
            data: list[dict[str, Any]] = yaml.safe_load(f) or []
            self.add_entry(data)

    def _migrate_entry_storage(self) -> None:
        updated = False
        for item in self.cfg.entry_storage:
            if "need_admin" not in item:
                item["need_admin"] = False
                updated = True
        if updated:
            self.cfg.save_config()
            logger.info("已为旧版提示词配置补全 need_admin 字段")

    def add_entry(self, data: list[dict[str, Any]]) -> None:
        existed_commands = {e.command for e in self.entries}
        new_items: list[dict[str, Any]] = []

        for item in data:
            if item["command"] in existed_commands:
                continue
            self.cfg.entry_storage.append(item)
            new_items.append(item)
            self.entries.append(PromptEntry(item))

        if new_items:
            self.cfg.save_config()
            logger.info(f"已加载提示词：{[item['command'] for item in new_items]}")

    def get_entry(self, command: str) -> PromptEntry | None:
        """获取条目"""
        for entry in self.entries:
            if entry.command == command:
                return entry


