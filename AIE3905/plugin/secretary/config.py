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
        self.dialogue_enabled = data.get("dialogue", {}).get("enabled", True)
        if not isinstance(self.dialogue_enabled, bool):
            raise ValueError("dialogue.enabled 必须是布尔值")
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
