"""Run with the installed AstrBot Python and an isolated ASTRBOT_ROOT."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.core.message.components import Image, Reply
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot_plugin_groupmedia.main import AdapterError, GroupMedia


class Event:
    def __init__(self, components=None, session="test:GroupMessage:A"):
        self.unified_msg_origin = session
        self.message_obj = SimpleNamespace(
            message_id="current", message=components or []
        )
        self.extra = {}
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key):
        return self.extra.get(key)

    def set_extra(self, key, value):
        self.extra[key] = value

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value):
        self.call_llm = value

    def plain_result(self, text):
        return MessageEventResult().message(text)

    def chain_result(self, chain):
        result = MessageEventResult()
        result.chain = chain
        return result

    async def send(self, result):
        self.sent.append(result)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = GroupMedia(None, {"allowed_sessions": ["test:GroupMessage:A"]})

    async def test_other_group_rejected_before_file_or_network(self):
        event = Event(session="test:GroupMessage:B")
        with patch.object(self.plugin, "call", new_callable=AsyncMock) as call:
            result = json.loads(await self.plugin.ocr_tool(event))
            self.assertFalse(result["ok"])
            call.assert_not_called()

    async def test_quote_source_and_current_order(self):
        quoted, current = Image.fromBytes(b"quoted"), Image.fromBytes(b"current")
        event = Event([current, Reply(id="earlier", chain=[quoted])])
        self.assertEqual(
            self.plugin.attachments(event, Image),
            [(quoted, "earlier"), (current, "current")],
        )

    async def test_repeat_generation_only_calls_and_sends_once(self):
        event = Event()
        result = {"ok": True, "text": "AI 生成图片", "asset": {"base64": "bW9jaw=="}}
        with patch.object(
            self.plugin, "call", new_callable=AsyncMock, return_value=result
        ) as call:
            await self.plugin.generate_tool(event, "虚构测试")
            await self.plugin.generate_tool(event, "虚构测试")
            self.assertEqual(call.await_count, 1)
            self.assertEqual(len(event.sent), 1)

    async def test_failed_attempt_not_automatically_retried(self):
        event = Event()
        with patch.object(
            self.plugin,
            "call",
            new_callable=AsyncMock,
            side_effect=AdapterError("未配置"),
        ) as call:
            a = json.loads(await self.plugin.generate_tool(event, "test"))
            b = json.loads(await self.plugin.generate_tool(event, "test"))
            self.assertFalse(a["ok"])
            self.assertFalse(b["ok"])
            self.assertEqual(call.await_count, 1)
            self.assertEqual(event.sent, [])

    async def test_command_returns_once_and_stops_default_reply(self):
        event = Event()
        with patch.object(
            self.plugin,
            "execute",
            new_callable=AsyncMock,
            return_value={"ok": True, "text": "仅建议"},
        ):
            results = [r async for r in self.plugin.ocr_command(event)]
        self.assertEqual(len(results), 1)
        self.assertTrue(event.stopped)
        self.assertTrue(event.call_llm)
        self.assertEqual(event.sent, [])

    async def test_failed_command_has_no_success_text(self):
        event = Event()
        with patch.object(
            self.plugin,
            "execute",
            new_callable=AsyncMock,
            side_effect=AdapterError("尚未配置"),
        ):
            results = [r async for r in self.plugin.ocr_command(event)]
        self.assertEqual(results[0].chain[0].text, "尚未配置")

    async def test_missing_media_does_not_call_service(self):
        with patch.object(self.plugin, "call", new_callable=AsyncMock) as call:
            result = json.loads(await self.plugin.ocr_tool(Event()))
        self.assertFalse(result["ok"])
        call.assert_not_called()

    async def test_bounded_task_count(self):
        event = Event()
        with patch.object(
            self.plugin, "call", new_callable=AsyncMock, return_value={"ok": True}
        ) as call:
            for i in range(4):
                await self.plugin.execute(event, "generate", prompt=str(i))
            with self.assertRaises(AdapterError):
                await self.plugin.execute(event, "generate", prompt="fifth")
        self.assertEqual(call.await_count, 4)

    async def test_untrusted_access_url_is_rejected(self):
        self.plugin.options["access_token_file"] = "unused"
        with patch.object(
            Path,
            "read_text",
            return_value=json.dumps(
                {"url": "http://remote.example:8792", "token": "x" * 32}
            ),
        ):
            with self.assertRaises(AdapterError):
                self.plugin.connection()


if __name__ == "__main__":
    unittest.main()
