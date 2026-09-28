from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .secretary.config import Config
from .secretary.engine import Engine
from .secretary.types import Actor, Message, utcnow
from .secretary.web import AuditServer


class GroupSecretary(Star):
    """Collect every delivered group message; respond only through this plugin's route."""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.options = config or {}
        self.engine = None
        self.web = None
        self.reply_tasks = set()
        self.reply_gate = asyncio.Semaphore(4)

    async def initialize(self):
        directory = StarTools.get_data_dir("astrbot_plugin_groupsecretary")
        configured = str(self.options.get("config_path", "")).strip()
        path = (
            Path(configured).expanduser().resolve()
            if configured
            else directory / "config.json"
        )
        if not path.exists():
            if configured:
                raise ValueError("指定的 config_path 不存在")
            path.write_text(
                json.dumps(
                    {
                        "mode": "demo",
                        "database": "secretary.sqlite3",
                        "groups": [],
                        "web": {"enabled": False},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.logger.info(
                "群聊助手已生成配置模板，默认不采集任何群；配置路径：%s", path
            )
        cfg = Config.load(path)
        self.engine = Engine(cfg, context=self.context)
        try:
            await self.engine.start(sender=self.send_to_group)
            if cfg.web.get("enabled", False):
                self.web = AuditServer(self.engine)
                self.web.start()
                self.logger.info("群聊助手审核页：%s", self.web.url)
        except Exception:
            await self.engine.close()
            self.engine = None
            raise

    def group_for(self, platform_id, group_id):
        if not self.engine:
            return None
        return next(
            (
                g
                for g in self.engine.config.groups.values()
                if g.enabled
                and g.data_use_confirmed
                and g.platform_id == platform_id
                and g.native_group_id == str(group_id)
            ),
            None,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe(self, event: AstrMessageEvent):
        if not self.engine:
            return
        g = self.group_for(event.get_platform_id(), event.get_group_id())
        if not g:
            return
        # AstrBot 4.28.1 uses True to FORBID its default LLM request.
        # False allows a second, independent response while our task is running.
        event.should_call_llm(True)
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        obj = event.message_obj
        raw = getattr(obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("notice_type") == "group_recall":
            await self.ingest_recall(
                event.get_platform_id(), event.get_group_id(), str(raw["message_id"])
            )
            return
        message_id = str(getattr(obj, "message_id", "") or "")
        if not message_id:
            self.logger.warning(
                "群聊助手未记录一条缺少稳定消息 ID 的事件；请检查适配器。"
            )
            return
        self.engine.store.set_meta("umo:" + g.key, event.unified_msg_origin)
        components = event.get_messages()
        plains = [
            getattr(c, "text", "")
            for c in components
            if c.__class__.__name__ == "Plain"
        ]
        text = "".join(plains).strip() if plains else event.get_message_str().strip()
        reply = next(
            (
                str(getattr(c, "id", "") or getattr(c, "message_id", ""))
                for c in components
                if c.__class__.__name__ == "Reply"
            ),
            "",
        )
        # QQ's send() does not return a native message ID. Resolve an exact quoted
        # assistant reply when the adapter supplies its text; never fuzzy-match it.
        quoted = next(
            (
                getattr(c, "message_str", "") or ""
                for c in components
                if c.__class__.__name__ == "Reply"
            ),
            "",
        )
        if quoted:
            matches = self.engine.store.rows(
                "SELECT id,output FROM answers WHERE group_key=? ORDER BY at DESC LIMIT 30",
                (g.key,),
            )
            matched = [
                a
                for a in matches
                if quoted.strip() in {a["output"].strip(), a["output"][:1800].strip()}
            ]
            if len(matched) == 1:
                reply = matched[0]["id"]
        attachments = [
            {"type": c.__class__.__name__, "processed": False}
            for c in components
            if c.__class__.__name__ in {"Image", "Record", "Video", "File"}
        ]
        sender = str(event.get_sender_id())
        actor = Actor(sender, [g.key], sender in g.admins)
        # Explicit privacy controls must be processed before storing the command itself.
        if text in {"/别记我", "别记我", "/恢复记录"}:
            result = await self.engine.command(actor, g.key, text)
            await event.send(MessageChain().message(result["text"]))
            return
        message = Message(
            g.key,
            sender,
            text,
            getattr(obj, "timestamp", None) or utcnow(),
            native_id=message_id,
            name=event.get_sender_name() or sender,
            reply_to=reply,
            attachments=attachments,
        )
        wants_reply = (
            text.startswith(
                (
                    "/问 ",
                    "/回溯 ",
                    "/群报",
                    "/记事 ",
                    "/确认 ",
                    "/反馈 ",
                    "/原文 ",
                    "/聊 ",
                    "/忘掉 ",
                    "/事项",
                    "/秘书帮助",
                    "/秘书状态",
                )
            )
            or text in {"这条记住", "/记住", "这个已经过时了", "已经过时了"}
            or bool(getattr(event, "is_at_or_wake_command", False))
        )
        try:
            saved = self.engine.ingest(
                message,
                route="dialogue"
                if wants_reply and self.engine.uses_dialogue(text)
                else "background",
            )
        except ValueError:
            self.logger.warning(
                "群聊助手发现重复 ID 的内容变化；须由适配器提供 edit 与 revision。"
            )
            return
        # An opted-out member may still ask a one-off question; it will not be logged.
        if saved is not None and not saved["_new"]:
            return
        if not wants_reply:
            return
        if len(self.reply_tasks) >= 32:
            await event.send(
                MessageChain().message("当前查询较多，已保存消息；请稍后再问。")
            )
            return
        task = asyncio.create_task(self._reply(event, actor, g.key, text, message))
        self.reply_tasks.add(task)
        task.add_done_callback(self.reply_tasks.discard)

    async def _reply(self, event, actor, key, text, message):
        try:
            async with self.reply_gate:
                result = await self.engine.command(
                    actor, key, text or "/秘书帮助", message=message
                )
                output = result["text"]
                if (
                    result.get("id")
                    and not result.get("mode", "").startswith("dialogue")
                    and not self.engine.store.opted_out(key, actor.user)
                ):
                    output += "\n\n反馈：/反馈 " + result["id"] + " 类型"
                chunks = await self._send_chunks(
                    lambda chain: event.send(chain), output
                )
                self.logger.info(
                    "Group secretary sent answer=%s chunks=%d chars=%d",
                    result.get("id", "command"),
                    chunks,
                    len(output),
                )
        except (ValueError, PermissionError) as exc:
            await event.send(MessageChain().message(str(exc)[:200]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "Group secretary reply failed (%s); check provider and platform status.",
                type(exc).__name__,
            )

    @staticmethod
    async def _send_chunks(send, text):
        # Avoid a single oversized platform message, and cap runaway output.
        chunks = 0
        for start in range(0, min(len(text), 12000), 1800):
            await send(MessageChain().message(text[start : start + 1800]))
            chunks += 1
        return chunks

    async def send_to_group(self, key, text):
        origin = self.engine.store.get_meta("umo:" + key)
        if not origin:
            raise ValueError("尚未取得该群的发送会话；先从实际平台接收一条消息")

        async def send(chain):
            result = await self.context.send_message(origin, chain)
            if result is False:
                raise RuntimeError("AstrBot 未找到可发送的平台")

        await self._send_chunks(send, text)

    async def ingest_recall(self, platform_id, group_id, native_id):
        """Bridge for adapters that expose recall separately. Never infer a recall from ordinary text."""
        g = self.group_for(platform_id, group_id)
        if g:
            self.engine.ingest(
                Message(
                    g.key,
                    "platform",
                    "",
                    utcnow(),
                    native_id="recall:" + native_id,
                    kind="recall",
                    target_id=native_id,
                )
            )

    async def terminate(self):
        for task in list(self.reply_tasks):
            task.cancel()
        await asyncio.gather(*self.reply_tasks, return_exceptions=True)
        self.reply_tasks.clear()
        if self.web:
            await self.web.close()
            self.web = None
        if self.engine:
            await self.engine.close()
            self.engine = None
