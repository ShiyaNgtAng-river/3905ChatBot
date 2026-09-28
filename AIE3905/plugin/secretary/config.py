from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .types import Group


class Config:
    def __init__(self, data: dict, base: str | Path = "."):
        self.raw = data
        self.base = Path(base).resolve()
        self.db = (self.base / data.get("database", "data/secretary.sqlite3")).resolve()
        self.mode = data.get("mode", "demo")
        if self.mode not in {"demo", "openai", "astrbot"}:
            raise ValueError("mode 必须是 demo、openai 或 astrbot")
        self.groups = {}
        for g in data.get("groups", []):
            obj = Group(**g)
            ZoneInfo(obj.timezone)
            if obj.confirmation not in {"designated", "initiator"}:
                raise ValueError("首版仅支持 designated/initiator 确认规则")
            if not 1 <= obj.retention_days <= 3650:
                raise ValueError("retention_days 必须为 1–3650")
            if obj.report_time:
                from datetime import time

                time.fromisoformat(obj.report_time)
            if obj.key in self.groups:
                raise ValueError("群 key 重复")
            self.groups[obj.key] = obj
        dialogue = data.get("dialogue", {})
        self.dialogue_enabled = dialogue.get("enabled", True)
        if not isinstance(self.dialogue_enabled, bool):
            raise ValueError("dialogue.enabled 必须是布尔值")
        # astrbot: the host agent (persona, subagents, web search) answers mentions
        # and uses this plugin as context and tools; plugin: the v0.2 JSON loop answers.
        self.frontend = dialogue.get("frontend", "astrbot")
        if self.frontend not in {"astrbot", "plugin"}:
            raise ValueError("dialogue.frontend 必须是 astrbot 或 plugin")
        self.context_messages = dialogue.get("context_messages", 40)
        if (
            type(self.context_messages) is not int
            or not 0 <= self.context_messages <= 200
        ):
            raise ValueError("dialogue.context_messages 必须是0–200的整数")
        # Long-range memory: topic episodes are summarised every episode_size
        # messages, or after episode_idle_minutes of quiet with at least episode_min.
        memory = data.get("memory", {})
        self.episodes = memory.get("episodes", True)
        self.profiles = memory.get("profiles", True)
        self.episode_size = memory.get("episode_size", 30)
        self.episode_min = memory.get("episode_min", 5)
        self.episode_idle = memory.get("episode_idle_minutes", 20)
        if (
            not isinstance(self.episodes, bool)
            or not isinstance(self.profiles, bool)
            or type(self.episode_size) is not int
            or type(self.episode_min) is not int
            or not isinstance(self.episode_idle, (int, float))
            or not 1 <= self.episode_min <= self.episode_size <= 200
            or not 0 <= self.episode_idle <= 1440
        ):
            raise ValueError(
                "memory 配置无效：episode_min≤episode_size≤200，空闲分钟为0–1440"
            )
        self.models = data.get("models", {})
        self.web = data.get("web", {})
        self.max_attempts = int(data.get("max_attempts", 3))
        self.query_wait = float(data.get("query_wait_seconds", 8))
        if not 1 <= self.max_attempts <= 10 or not 0 <= self.query_wait <= 120:
            raise ValueError("重试次数应为1–10，查询等待应为0–120秒")

    @classmethod
    def load(cls, path: str | Path):
        path = Path(path).resolve()
        return cls(json.loads(path.read_text(encoding="utf-8")), path.parent)

    def group(self, key: str) -> Group:
        g = self.groups.get(key)
        if not g or not g.enabled or not g.data_use_confirmed:
            raise PermissionError("该群未启用或尚未确认数据处理范围")
        return g
