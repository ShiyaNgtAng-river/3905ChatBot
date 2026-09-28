"""API-shape simulator. Does not claim to test a real AstrBot installation or platform."""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from secretary.sample import demo_config


class Plain:
    def __init__(self, text):
        self.text = text


class MessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text += text
        return self


class Context:
    async def send_message(self, origin, chain):
        return True


class Event:
    def __init__(self, text, mid="one", wake=False, group="123", sender="owner"):
        self.text = text
        self.group = group
        self.sender = sender
        self.sent = []
        self.default_llm = None
        self.is_at_or_wake_command = wake
        self.unified_msg_origin = "lark1:GroupMessage:" + group
        self.message_obj = types.SimpleNamespace(
            message_id=mid,
            timestamp=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
            raw_message={},
        )

    def get_platform_id(self):
        return "lark1"

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "bot"

    def get_sender_name(self):
        return self.sender

    def get_message_str(self):
        return self.text

    def get_messages(self):
        return [Plain(self.text)]

    def should_call_llm(self, value):
        self.default_llm = value

    async def send(self, chain):
        self.sent.append(chain.text)


class AstrBotContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)

        class Star:
            def __init__(self, context):
                self.context = context
                self.logger = logging.getLogger("plugin-test")

        event_module = types.ModuleType("astrbot.api.event")
        event_module.AstrMessageEvent = Event
        event_module.MessageChain = MessageChain
        event_module.filter = types.SimpleNamespace(
            EventMessageType=types.SimpleNamespace(ALL="all"),
            event_message_type=lambda kind: lambda f: f,
        )
        star_module = types.ModuleType("astrbot.api.star")
        star_module.Star = Star
        star_module.Context = Context
        star_module.StarTools = types.SimpleNamespace(
            get_data_dir=lambda name: directory
        )
        package = types.ModuleType("contract_plugin")
        package.__path__ = [str(Path(__file__).parents[1])]
        self.modules = patch.dict(
            sys.modules,
            {
                "astrbot": types.ModuleType("astrbot"),
                "astrbot.api": types.ModuleType("astrbot.api"),
                "astrbot.api.event": event_module,
                "astrbot.api.star": star_module,
                "contract_plugin": package,
            },
        )
        self.modules.start()
        spec = importlib.util.spec_from_file_location(
            "contract_plugin.main", Path(__file__).parents[1] / "main.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cfg = demo_config()
        cfg["groups"][0].update(platform_id="lark1", native_group_id="123")
        cfg["web"]["enabled"] = False
        (directory / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.plugin = module.GroupSecretary(Context())
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.modules.stop()
        self.temp.cleanup()

    async def test_non_mentioned_message_is_collected_without_reply(self):
        event = Event("【演示】确认：2026-10-02")
        await self.plugin.observe(event)
        await self.plugin.engine.flush("demo")
        self.assertTrue(event.default_llm)
        self.assertEqual(event.sent, [])
        self.assertEqual(
            self.plugin.engine.states("demo")[0]["fields"]["when"], "2026-10-02"
        )

    async def test_explicit_question_replies_once_and_duplicate_is_suppressed(self):
        await self.plugin.observe(Event("【演示】确认：2026-10-02"))
        await self.plugin.engine.flush("demo")
        question = Event("/问 演示现在怎么定", mid="two", wake=True)
        await self.plugin.observe(question)
        await asyncio.gather(*list(self.plugin.reply_tasks))
        sent = len(question.sent)
        self.assertGreater(sent, 0)
        self.assertIn("2026-10-02", "".join(question.sent))
        await self.plugin.observe(question)
        self.assertEqual(sent, len(question.sent))

    async def test_other_groups_and_bot_messages_are_excluded(self):
        outside = Event("机密", group="other")
        await self.plugin.observe(outside)
        self.assertIsNone(outside.default_llm)
        await self.plugin.observe(Event("机器人自己说的话", sender="bot"))
        self.assertEqual(self.plugin.engine.store.recent("demo"), [])

    async def test_explicit_recall_bridge_invalidates_memory(self):
        await self.plugin.observe(Event("【演示】确认：2026-10-02"))
        await self.plugin.engine.flush("demo")
        await self.plugin.ingest_recall("lark1", "123", "one")
        await self.plugin.engine.flush("demo")
        self.assertEqual(self.plugin.engine.states("demo")[0]["status"], "uncertain")

    async def test_opt_out_control_is_not_stored_as_content(self):
        ev = Event("/别记我", sender="lin")
        await self.plugin.observe(ev)
        self.assertTrue(self.plugin.engine.store.opted_out("demo", "lin"))
        self.assertEqual(self.plugin.engine.store.recent("demo"), [])
        self.assertTrue(ev.sent)

    async def test_wake_uses_dialogue_and_does_not_extract_or_append_feedback(self):
        class Model:
            model = "fake"

            async def complete(self, *args):
                return '{"text":"建议晚上八点讨论。","sources":[]}'

        self.plugin.engine.answerer.provider = Model()
        ev = Event("帮我们设计开会安排", mid="design", wake=True)
        await self.plugin.observe(ev)
        await asyncio.gather(*list(self.plugin.reply_tasks))
        self.assertIn("建议", "".join(ev.sent))
        self.assertNotIn("/反馈", "".join(ev.sent))
        rows = self.plugin.engine.store.rows("SELECT route,status FROM messages")
        self.assertEqual(rows, [{"route": "dialogue", "status": "done"}])
        self.assertFalse(self.plugin.engine.states("demo"))

    async def test_disable_dialogue_keeps_legacy_route(self):
        self.plugin.engine.config.dialogue_enabled = False
        ev = Event("怎么安排", mid="legacy", wake=True)
        await self.plugin.observe(ev)
        await asyncio.gather(*list(self.plugin.reply_tasks))
        self.assertEqual(
            self.plugin.engine.store.rows("SELECT route FROM messages"),
            [{"route": "background"}],
        )
