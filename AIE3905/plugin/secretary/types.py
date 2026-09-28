from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(*values: Any) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def timestamp(value: str | int | float) -> str:
    if isinstance(value, (int, float)):
        if value > 10**11:
            value /= 1000
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 2026-09-27T09:00:00+08:00")
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class Message:
    group: str
    sender: str
    text: str
    at: str
    native_id: str = ""
    name: str = ""
    reply_to: str = ""
    kind: str = "create"
    revision: str = "0"
    target_id: str = ""
    source_url: str = ""
    attachments: list[dict] = field(default_factory=list)
    dataset: str = ""
    row: int = 0

    def __post_init__(self):
        self.at = timestamp(self.at)
        if not self.group or not self.sender:
            raise ValueError("group 和稳定 sender 必填；不能把昵称当作已确认身份")
        if self.kind not in {"create", "recall", "edit"}:
            raise ValueError("不支持的消息类型")
        if len(self.text) > 24000 or len(self.attachments) > 20:
            raise ValueError("消息过长或附件过多")
        if not self.native_id and not self.dataset:
            raise ValueError("缺少原生消息 ID 时必须给出固定数据集版本和行号")

    @property
    def uid(self) -> str:
        key = self.native_id or f"import:{self.dataset}:{self.row}"
        return digest(self.group, key, self.kind, self.revision)

    @property
    def fingerprint(self) -> str:
        return digest(self.group, self.sender, self.at, self.text, self.attachments)


@dataclass
class Group:
    key: str
    platform_id: str = ""
    native_group_id: str = ""
    timezone: str = "Asia/Shanghai"
    enabled: bool = False
    data_use_confirmed: bool = False
    admins: list[str] = field(default_factory=list)
    confirmers: list[str] = field(default_factory=list)
    confirmation: str = "designated"
    persona: str = "你是群内的 AI 助手。简洁、友好、具体；不假装真人，不编造经历，不机械复述用户问题。"
    glossary: dict[str, str] = field(default_factory=dict)
    proactive: bool = False
    cooldown_seconds: int = 600
    report_time: str = ""
    retention_days: int = 30
    recent_limit: int = 16
    processing_location: str = "本地"

    def can_confirm(self, user: str, creator: str) -> bool:
        return user in self.admins or user in self.confirmers or (
            self.confirmation == "initiator" and user == creator
        )


@dataclass
class Actor:
    user: str
    groups: list[str]
    admin: bool = False

    def require(self, group: str, admin: bool = False):
        if group not in self.groups or (admin and not self.admin):
            raise PermissionError("没有该群或该操作的权限")


EVENT_KINDS = {"propose", "confirm", "change", "cancel", "complete", "correct", "participant", "note", "outdated"}
FIELDS = {"when", "time_raw", "owner", "location", "reason", "note", "priority", "blocked", "target_count"}
