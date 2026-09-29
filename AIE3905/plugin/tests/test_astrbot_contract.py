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
        self.extras = {}
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

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        return self.extras.get(key, default)

    async def send(self, chain):
        self.sent.append(chain.text)


class PluginHarness(unittest.IsolatedAsyncioTestCase):
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
            on_llm_request=lambda **kw: lambda f: f,
            on_decorating_result=lambda **kw: lambda f: f,
            llm_tool=lambda name=None, **kw: lambda f: f,
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


class AstrBotContractTests(PluginHarness):
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
        self.plugin.engine.config.frontend = "plugin"

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


class ToolSet:
    def __init__(self, names):
        self.tools = list(names)

    def names(self):
        return list(self.tools)

    def remove_tool(self, name):
        self.tools.remove(name)


class Request:
    def __init__(self, names=()):
        self.func_tool = ToolSet(names)
        self.system_prompt = "persona"
        self.contexts = [
            {"role": "user", "content": "persona example", "_no_save": True},
            {"role": "user", "content": "stale host history"},
        ]


class Result:
    def __init__(self, text, llm=True):
        self.chain = [Plain(text)]
        self.llm = llm

    def is_llm_result(self):
        return self.llm


class NativeFrontendTests(PluginHarness):
    """AstrBot answers mentions; the plugin supplies context, tools and receipts."""

    async def mention(self, text, mid, sender="owner"):
        ev = Event(text, mid=mid, wake=True, sender=sender)
        await self.plugin.observe(ev)
        return ev

    async def turn(self, ev, reply):
        req = Request(["search_group_history", "read_group_items", "web_search_bocha"])
        await self.plugin.inject_group_context(ev, req)
        ev.result = Result(reply)
        ev.get_result = lambda: ev.result
        await self.plugin.finish_group_turn(ev)
        return req, ev.result.chain[0].text

    async def test_mention_is_left_to_host_agent_once(self):
        ev = await self.mention("周末去哪玩", "n1")
        self.assertFalse(ev.default_llm)
        self.assertEqual(ev.sent, [])
        self.assertFalse(self.plugin.reply_tasks)
        rows = self.plugin.engine.store.rows("SELECT route,status FROM messages")
        self.assertEqual(rows, [{"route": "dialogue", "status": "dialogue"}])
        await self.plugin.observe(ev)  # redelivery must not start a second reply
        self.assertTrue(ev.default_llm)

    async def test_commands_still_answered_by_plugin_only(self):
        ev = Event("/事项", mid="c1", wake=True)
        await self.plugin.observe(ev)
        await asyncio.gather(*list(self.plugin.reply_tasks))
        self.assertTrue(ev.default_llm)
        self.assertTrue(ev.sent)
        self.assertIsNone(ev.get_extra("groupsecretary_turn"))

    async def test_context_replaces_host_history_and_respects_recall(self):
        await self.plugin.observe(Event("下周三团建去爬山", mid="h1", sender="lin"))
        await self.plugin.observe(Event("这条之后会撤回", mid="h2", sender="yu"))
        await self.plugin.ingest_recall("lark1", "123", "h2")
        await self.plugin.engine.flush("demo")
        ev = await self.mention("团建定在哪天", "n2")
        req, _ = await self.turn(ev, "下周三。")
        self.assertEqual([c["content"] for c in req.contexts], ["persona example"])
        self.assertIn("下周三团建去爬山", req.system_prompt)
        self.assertNotIn("之后会撤回", req.system_prompt)
        self.assertIn("可以正式确认事项", req.system_prompt)
        ev2 = await self.mention("那几点集合", "n3", sender="lin")
        req2, _ = await self.turn(ev2, "还没定时间。")
        self.assertIn(" 你] 下周三。", req2.system_prompt)

    async def test_subagent_owns_its_tools_and_other_chats_get_none(self):
        ev = await self.mention("查一下新闻", "n4")
        req = Request(
            ["transfer_to_search", "web_search_bocha", "search_group_history"]
        )
        await self.plugin.inject_group_context(ev, req)
        self.assertEqual(
            req.func_tool.names(), ["transfer_to_search", "search_group_history"]
        )
        outside = Event("hi", group="other", wake=True)
        req = Request(
            ["search_group_history", "submit_group_events", "web_search_bocha"]
        )
        await self.plugin.inject_group_context(outside, req)
        self.assertEqual(req.func_tool.names(), ["web_search_bocha"])
        self.assertIn("stale host history", str(req.contexts))
        refused = await self.plugin.search_group_history(outside, "新闻")
        self.assertIn("不能使用", refused)

    async def test_memory_tools_belong_to_the_memory_subagent(self):
        ev = await self.mention("上周团建怎么定的", "m1")
        req = Request(
            [
                "transfer_to_memory",
                "get_topic_timeline",
                "read_group_day",
                "get_group_episodes",
                "read_group_items",
            ]
        )
        await self.plugin.inject_group_context(ev, req)
        self.assertEqual(req.func_tool.names(), ["transfer_to_memory", "read_group_items"])
        timeline = json.loads(await self.plugin.get_topic_timeline(ev, "团建"))
        self.assertIn("notes", timeline)
        # Demo mode has no model to reread a day with; the agent gets an error it can use.
        reread = json.loads(await self.plugin.read_group_day(ev, "昨天", "几点集合"))
        self.assertIn("error", reread)
        outside = Event("hi", group="other", wake=True)
        self.assertIn("不能使用", await self.plugin.read_group_day(outside, "昨天", "几点"))

    async def test_draft_then_authorized_confirm_records_with_receipt(self):
        ev = await self.mention("帮我设计团建方案", "d1")
        saved = json.loads(
            await self.plugin.save_group_drafts(
                ev,
                [
                    {
                        "number": 1,
                        "title": "团建",
                        "description": "周三爬山",
                        "fields": {"when": "2026-10-07"},
                    }
                ],
            )
        )
        draft_id = saved["drafts"][0]["id"]
        _, text = await self.turn(ev, "**方案1**：周三爬山。\n希望对你有帮助！")
        self.assertEqual(text, "方案1：周三爬山。")
        confirm = await self.mention("就按这个定了", "d2")
        forged = json.loads(
            self.plugin._group_tool(
                confirm,
                "submit_events",
                {"events": [{"kind": "confirm", "draft_id": draft_id}], "group": "x"},
            )
        )
        self.assertIn("error", forged)
        done = json.loads(
            await self.plugin.submit_group_events(
                confirm, [{"kind": "confirm", "draft_id": draft_id}]
            )
        )
        self.assertEqual(done["operations"][0]["status"], "recorded")
        _, text = await self.turn(confirm, "好，定了。")
        self.assertEqual(text, "好，定了。\n（「团建」已记录）")
        self.assertEqual(self.plugin.engine.states("demo")[0]["status"], "confirmed")
        answers = self.plugin.engine.store.rows("SELECT mode FROM answers")
        self.assertEqual({a["mode"] for a in answers}, {"native"})
        status = self.plugin.engine.store.message("demo", "d2")
        self.assertEqual(status["status"], "done")

    async def test_unauthorized_confirm_stays_pending(self):
        ev = await self.mention("设计读书会方案", "u1", sender="lin")
        saved = json.loads(
            await self.plugin.save_group_drafts(
                ev,
                [
                    {
                        "number": 1,
                        "title": "读书会",
                        "description": "周五晚",
                        "fields": {"when": "2026-10-09"},
                    }
                ],
            )
        )
        await self.turn(ev, "方案1：周五晚。")
        confirm = await self.mention("就这么定", "u2", sender="lin")
        done = json.loads(
            await self.plugin.submit_group_events(
                confirm,
                [{"kind": "confirm", "draft_id": saved["drafts"][0]["id"]}],
            )
        )
        self.assertEqual(done["operations"][0]["status"], "pending_confirmation")
        _, text = await self.turn(confirm, "我先记下你的意见。")
        self.assertIn("已提交，等确认人确认", text)

    async def test_opted_out_member_can_chat_but_nothing_is_written(self):
        await self.plugin.observe(Event("/别记我", sender="lin"))
        ev = await self.mention("帮我设计方案", "o1", sender="lin")
        self.assertFalse(ev.default_llm)
        result = json.loads(
            await self.plugin.save_group_drafts(
                ev, [{"number": 1, "title": "x", "description": "y"}]
            )
        )
        self.assertIn("退出记录", result["error"])
        _, text = await self.turn(ev, "好的。")
        self.assertTrue(text.startswith("好的。\n（没有写入事项：你已退出记录"))
        self.assertEqual(self.plugin.engine.store.rows("SELECT id FROM answers"), [])
        self.assertEqual(self.plugin.engine.store.recent("demo"), [])

    async def test_text_before_tool_call_does_not_close_the_turn(self):
        # The host sends model text that accompanies a tool call as its own message.
        ev = await self.mention("帮我设计团建方案", "t1")
        await self.plugin.inject_group_context(ev, Request())
        ev.result = Result("我先拟一下～")
        ev.get_result = lambda: ev.result
        await self.plugin.finish_group_turn(ev)
        self.assertEqual(ev.result.chain[0].text, "我先拟一下～")
        saved = json.loads(
            await self.plugin.save_group_drafts(
                ev,
                [
                    {
                        "number": 1,
                        "title": "团建",
                        "description": "周三爬山",
                        "fields": {"when": "2026-10-07"},
                    }
                ],
            )
        )
        self.assertIn("drafts", saved)
        confirm = await self.mention("就按这个定了", "t2")
        await self.plugin.inject_group_context(confirm, Request())
        confirm.result = Result("好，我记一下。")
        confirm.get_result = lambda: confirm.result
        await self.plugin.finish_group_turn(confirm)
        await self.plugin.submit_group_events(
            confirm, [{"kind": "confirm", "draft_id": saved["drafts"][0]["id"]}]
        )
        confirm.result = Result("定了。")
        await self.plugin.finish_group_turn(confirm)
        self.assertEqual(confirm.result.chain[0].text, "定了。\n（「团建」已记录）")
        confirm.result = Result("还有别的吗")
        await self.plugin.finish_group_turn(confirm)
        self.assertEqual(confirm.result.chain[0].text, "还有别的吗")
        outputs = [
            r["output"]
            for r in self.plugin.engine.store.rows(
                "SELECT output FROM answers ORDER BY at"
            )
        ]
        self.assertEqual(
            outputs[-1], "好，我记一下。\n定了。\n（「团建」已记录）\n还有别的吗"
        )

    async def test_depth_keywords_switch_to_research_and_filler_is_removed(self):
        ev = await self.mention("详细讲讲腾讯会议的弱网对抗", "k1")
        req, text = await self.turn(ev, "我查一下，稍等。结论：靠 FEC 和 SVC。")
        self.assertIn("当前消息里有“详细”：这次按深度调研的方式回答", req.system_prompt)
        self.assertIn("transfer_to_search", req.system_prompt)
        self.assertEqual(text, "结论：靠 FEC 和 SVC。")
        daily = await self.mention("今天吃什么", "k2")
        req, _ = await self.turn(daily, "随便。")
        self.assertNotIn("当前消息里有", req.system_prompt)

    async def test_only_research_requests_use_the_slow_model(self):
        cfg = self.plugin.engine.config
        daily = await self.mention("今天吃什么", "p1")
        self.assertIsNone(daily.get_extra("selected_provider"))  # host default when unset
        cfg.fast_provider, cfg.deep_provider = "fast-model", "deep-model"
        daily = await self.mention("今天吃什么", "p2")
        self.assertEqual(daily.get_extra("selected_provider"), "fast-model")
        for i, text in enumerate(["帮我调研一下这家公司", "分析一下这个月的情况", "帮我查一下这个技术"]):
            ev = await self.mention(text, f"p3{i}")
            self.assertEqual(ev.get_extra("selected_provider"), "deep-model")

    async def test_service_phrases_are_trimmed_and_counted_not_hidden(self):
        from contract_plugin.secretary.dialogue import tidy_reply

        flags = []
        self.assertEqual(
            tidy_reply("抱歉，这个画不了啦。你发张图给我看看？", flags),
            "这个画不了啦。你发张图给我看看？",
        )
        self.assertEqual(flags, ["apology"])
        flags = []
        kept = "我这边只能打字和查资料，没有画图的能力。想换头像就发张图来。"
        self.assertEqual(tidy_reply(kept, flags), kept)
        self.assertEqual(flags, ["capability_note"])
        honest = "我是 AI 扮演的爱音哦，不是真人～"
        self.assertEqual(tidy_reply(honest), honest)
        self.assertEqual(tidy_reply("抱歉。"), "抱歉。")
        flags = []
        tidy_reply("好的！如有需要随时找我～", flags)
        self.assertEqual(flags, ["canned_tail"])
        ev = await self.mention("给我画张图", "s1")
        _, text = await self.turn(ev, "抱歉，我这边只能打字，画不了。你发图来吧！")
        self.assertEqual(text, "我这边只能打字，画不了。你发图来吧！")
        trace = json.loads(
            self.plugin.engine.store.one("SELECT trace FROM answers")["trace"]
        )
        self.assertEqual(trace["style_flags"], {"apology": 1, "capability_note": 1})
