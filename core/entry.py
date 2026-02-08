# config.py
from __future__ import annotations
from typing import Any

import yaml
from astrbot.api import logger
from .config import PluginConfig, PromptEntry


class EntryService:
    def __init__(self, config: PluginConfig):
        self.cfg = config

        # 加载用户配置
        self.entries: list[PromptEntry] = [
            PromptEntry(item) for item in self.cfg.entry_storage
        ]
        if self.cfg.load_builtin_prompt:
            self.load_builtin_prompts()
        logger.debug(f"已注册命令：{[e.command for e in self.entries]}")

    def load_builtin_prompts(self) -> None:
        with self.cfg.builtin_prompt_file.open("r", encoding="utf-8") as f:
            data: list[dict[str, Any]] = yaml.safe_load(f) or []
            self.add_entry(data)

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

    def match_prompt_by_cmd(self, command: str) -> str | None:
        """根据命令匹配提示词"""
        for entry in self.entries:
            if entry.command == command:
                return entry.content

    def view_entry(self, command: str | None = None) -> str:
        """
        以 Markdown 格式展示 PromptEntry
        - command 为空：展示所有
        - command 指定：仅展示对应条目
        """
        if command:
            entries = [e for e in self.entries if e.command == command]
        else:
            entries = self.entries

        blocks: list[str] = []

        for entry in entries:
            block = "\n".join(
                [
                    f"### {entry.command}",
                    "",
                    "```",
                    entry.content.strip(),
                    "```",
                ]
            )
            blocks.append(block)

        return "\n\n\n\n".join(blocks)
