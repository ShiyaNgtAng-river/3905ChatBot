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
        # Words that switch a mention to a researched, structured answer.
        self.deep_keywords = dialogue.get("deep_keywords", ["仔细", "深入", "详细"])
        if not isinstance(self.deep_keywords, list) or not all(
            isinstance(w, str) and 1 <= len(w) <= 10 for w in self.deep_keywords
        ):
            raise ValueError("dialogue.deep_keywords 必须是1–10字的词列表")
        self.context_messages = dialogue.get("context_messages", 40)
        if (
            type(self.context_messages) is not int
            or not 0 <= self.context_messages <= 200
        ):
            raise ValueError("dialogue.context_messages 必须是0–200的整数")
        memory = data.get("memory", {})
        # v2 memory: read each whole day in adaptive passes (a pass runs after
        # read_new_chars of new text, or read_idle_minutes with anything new, never
        # more often than read_min_minutes), then consolidate every finished day
        # into anchors and digests after consolidate_time the next morning.
        self.reading = memory.get("reading", True)
        self.read_new_chars = memory.get("read_new_chars", 6000)
        self.read_idle_minutes = memory.get("read_idle_minutes", 60)
        self.read_min_minutes = memory.get("read_min_minutes", 10)
        self.read_max_chars = memory.get("read_max_chars", 250000)
        self.consolidate_time = memory.get("consolidate_time", "04:00")
        self.long_timeout = memory.get("timeout_seconds", 180)
        self.gate = memory.get("gate", True)
        self.anchor_chars = memory.get("anchor_chars", 1500)
        self.day_chars = memory.get("day_chars", 900)
        self.half_life = memory.get("half_life_days", 14)
        # Host providers finish loading after plugins start.
        self.start_delay = memory.get("start_delay_seconds", 60)
        qa = memory.get("qa_list", "")
        if qa:
            qa = json.loads((self.base / qa).read_text(encoding="utf-8"))
        self.qa_list = qa or None
        numbers = (
            self.read_new_chars,
            self.read_idle_minutes,
            self.read_min_minutes,
            self.read_max_chars,
            self.long_timeout,
            self.anchor_chars,
            self.day_chars,
            self.half_life,
            self.start_delay,
        )
        if (
            not isinstance(self.reading, bool)
            or not isinstance(self.gate, bool)
            or any(
                not isinstance(n, (int, float)) or isinstance(n, bool) or n <= 0
                for n in numbers
            )
            or self.read_min_minutes > self.read_idle_minutes
            or self.qa_list is not None
            and (
                not isinstance(self.qa_list, list)
                or not 1 <= len(self.qa_list) <= 20
                or not all(isinstance(q, str) and 2 <= len(q) <= 120 for q in self.qa_list)
            )
        ):
            raise ValueError(
                "memory 通读配置无效：数值须为正数，read_min_minutes≤read_idle_minutes，qa_list 为 1–20 个问题"
            )
        from datetime import time

        time.fromisoformat(self.consolidate_time)
        # v3 topic episodes (every episode_size messages, or after episode_idle_minutes
        # of quiet with at least episode_min) stay available as the evaluation baseline.
        self.episodes = memory.get("episodes", not self.reading)
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
