"""Deterministic behaviour tests; these do not measure a real model's accuracy."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from secretary.config import Config
from secretary.engine import Engine
from secretary.providers import ModelError
from secretary.sample import demo_config
from secretary.types import Actor, Message, utcnow


def tool(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def final(text="这是建议，尚未定案。", sources=None):
    return {"text": text, "sources": sources or []}


def option(number=1, when="2026-10-02T20:00:00+08:00", **extra):
    return dict(
        number=number,
        title="集成评审",
        description=f"建议 {when} 在三楼讨论室开会",
        fields={"when": when, "location": "三楼讨论室"},
        **extra,
    )


class Scripted:
    model = "scripted"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def complete(self, system, payload, role, group):
        self.calls.append((role, payload))
        if not self.steps:
            raise AssertionError("unexpected model call")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = step(payload)
        return json.dumps(step, ensure_ascii=False)


class DialogueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = demo_config()
        self.cfg["groups"].append(
            {
                "key": "other",
                "enabled": True,
                "data_use_confirmed": True,
                "admins": ["owner"],
            }
        )
        self.e = Engine(Config(self.cfg, self.temp.name))
        await self.e.start(maintenance=False)
        self.owner = Actor("owner", ["demo"], True)
        self.lin = Actor("lin", ["demo"])
        self.n = 0

    async def asyncTearDown(self):
        await self.e.close()
        self.temp.cleanup()

    async def talk(self, text, steps, actor=None, **kw):
        self.e.answerer.provider = Scripted(*steps)
        self.n += 1
        return await self.e.dialogue(
            actor or self.owner,
            "demo",
            text,
            request_id=kw.pop("request_id", str(self.n)),
            **kw,
        )

    async def background(self, text, sender="owner", **kw):
        self.n += 1
        m = Message(
            "demo", sender, text, utcnow(), native_id="background:" + str(self.n), **kw
        )
        self.e.ingest(m)
        await self.e.flush("demo")
        return m

    async def draft(self, actor=None):
        return await self.talk(
            "帮我们设计开会安排",
            [
                tool(
                    "save_drafts",
                    options=[option(1), option(2, "2026-10-02T21:00:00+08:00")],
                ),
                final(),
            ],
            actor,
        )

    async def test_full_design_revise_confirm_query_flow(self):
        first = await self.draft()
        self.assertEqual(len(first["drafts"]), 2)
        self.assertFalse(self.e.states("demo"))
        second = first["drafts"][1]
        changed = await self.talk(
            "第二个方案提前半小时",
            [
                tool(
                    "save_drafts",
                    options=[
                        option(2, "2026-10-02T20:30:00+08:00", parent_id=second["id"])
                    ],
                ),
                final(),
            ],
        )
        draft = changed["drafts"][0]
        self.assertEqual(draft["version"], 2)
        done = await self.talk(
            "就这么定了",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final("已按你的确认记录。"),
            ],
        )
        self.assertEqual(done["operations"][0]["status"], "recorded")
        state = self.e.states("demo")[0]
        self.assertEqual(state["fields"]["when"], "2026-10-02T20:30:00+08:00")
        self.assertEqual(state["status"], "confirmed")
        self.assertEqual(state["history"][0]["provenance"]["draft_version"], 2)
        q = await self.talk(
            "最终怎么安排",
            [
                tool("read_items"),
                lambda p: final(
                    "定在20:30，三楼讨论室。",
                    [
                        p["steps"][0]["tool_results"][0]["result"][0]["status_sources"][
                            0
                        ]
                    ],
                ),
            ],
        )
        self.assertIn("20:30", q["text"])
        self.assertTrue(q["sources"])
        self.assertEqual(len(self.e.store.events("demo")), 1)

    async def test_unauthorized_confirmation_remains_pending(self):
        draft = (await self.draft(self.lin))["drafts"][1]
        response = await self.talk(
            "就按方案2定了",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final("已提交，待确认。"),
            ],
            self.lin,
        )
        self.assertEqual(response["operations"][0]["status"], "pending_confirmation")
        self.assertEqual(self.e.states("demo")[0]["status"], "unconfirmed")

    async def test_other_members_draft_requires_explicit_reference(self):
        draft = (await self.draft())["drafts"][0]
        denied = await self.talk(
            "就这样",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final("请明确引用方案。"),
            ],
            self.lin,
        )
        self.assertFalse(denied["operations"])
        self.assertFalse(self.e.states("demo"))
        pending = await self.talk(
            "采用这个方案",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final(),
            ],
            self.lin,
            reply_to=draft["answer_id"],
        )
        self.assertEqual(pending["operations"][0]["status"], "pending_confirmation")

    async def test_ambiguous_confirmation_can_clarify_without_write(self):
        await self.draft()
        result = await self.talk("就这样吧", [final("你是指方案1还是方案2？")])
        self.assertIn("方案1", result["text"])
        self.assertFalse(self.e.states("demo"))

    async def test_requests_are_idempotent_and_changed_replays_rejected(self):
        first = await self.talk(
            "帮我写一段开场白",
            [final("各位好，今天讨论集成问题。")],
            request_id="stable",
        )
        again = await self.talk("帮我写一段开场白", [], request_id="stable")
        self.assertEqual(first, again)
        self.assertFalse(self.e.answerer.provider.calls)
        with self.assertRaises(ValueError):
            await self.talk("different", [], request_id="stable")

    async def test_model_error_after_write_reports_actual_result(self):
        result = await self.talk(
            "评审定在10月2日",
            [
                tool(
                    "submit_events",
                    events=[
                        {
                            "kind": "confirm",
                            "title": "评审",
                            "fields": {"when": "2026-10-02"},
                        }
                    ],
                ),
                ModelError("offline"),
            ],
        )
        self.assertEqual(result["mode"], "dialogue_fallback")
        self.assertEqual(result["operations"][0]["status"], "recorded")
        self.assertIn("已记录", result["text"])
        self.assertEqual(len(self.e.store.events("demo")), 1)

    async def test_invalid_tool_never_writes_and_round_budget_is_bounded(self):
        result = await self.talk(
            "测试",
            [
                tool(
                    "submit_events",
                    events=[
                        {
                            "title": "评审",
                            "kind": "confirm",
                            "fields": {"when": "tomorrow"},
                        }
                    ],
                )
            ]
            * 4,
        )
        self.assertEqual(result["mode"], "dialogue_fallback")
        self.assertFalse(self.e.states("demo"))
        self.assertEqual(len(self.e.answerer.provider.calls), 4)

    async def test_timeout_has_no_write(self):
        class Slow:
            model = "slow"

            async def complete(self, *args):
                raise asyncio.TimeoutError()

        self.e.answerer.provider = Slow()
        result = await self.e.dialogue(self.owner, "demo", "你好", request_id="timeout")
        self.assertEqual(result["mode"], "dialogue_fallback")
        self.assertFalse(self.e.states("demo"))

    async def test_current_question_is_excluded_from_search_and_strict_query(self):
        result = await self.talk(
            "蓝色档案在哪里",
            [
                tool("search_messages", query="蓝色档案"),
                lambda p: final("没有找到相关记录。"),
            ],
        )
        self.assertEqual(
            self.e.answerer.provider.calls[-1][1]["steps"][0]["tool_results"][0][
                "result"
            ],
            [],
        )
        m = await self.background("/问 蓝色档案在哪里")
        self.e.answerer.provider = None
        result = await self.e.command(self.owner, "demo", m.text, message=m)
        self.assertNotIn(m.uid, [s["uid"] for s in result["sources"]])

    async def test_group_isolation_and_invalid_sources(self):
        with self.assertRaises(PermissionError):
            await self.e.dialogue(self.lin, "other", "hello", request_id="x")
        result = await self.talk(
            "跨群方案",
            [
                tool("save_drafts", options=[option()], sources=["foreign-uid"]),
                final("找不到来源。"),
            ],
        )
        self.assertFalse(result["drafts"])
        self.assertFalse(self.e.states("demo"))

    async def test_recall_removes_drafts_caches_and_adopted_payload(self):
        draft = (await self.draft())["drafts"][0]
        await self.talk(
            "方案1定了",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final(),
            ],
        )
        m = self.e.store.one(
            "SELECT native_id FROM messages WHERE uid=(SELECT message_uid FROM drafts WHERE id=?)",
            (draft["id"],),
        )
        self.e.store.recall("demo", m["native_id"])
        self.assertFalse(self.e.store.drafts("demo"))
        self.assertFalse(self.e.store.rows("SELECT * FROM dialogue_runs"))
        event = self.e.store.events("demo")[0]
        self.assertFalse(event["valid"])
        self.assertFalse(event["payload"])
        self.assertNotIn(
            "三楼讨论室", json.dumps(event["provenance"], ensure_ascii=False)
        )

    async def test_deletion_during_model_call_discards_output(self):
        m = await self.background("敏感信息")

        def revoke(payload):
            self.e.store.recall("demo", m.native_id)
            return final("敏感信息")

        result = await self.talk("刚才说了什么", [revoke])
        self.assertNotIn("敏感信息", result["text"])
        self.assertFalse(result["sources"])

    async def test_optout_is_ephemeral_and_cannot_save(self):
        await self.draft(self.lin)
        self.e.store.optout("demo", "lin")
        result = await self.talk(
            "帮我设计",
            [tool("save_drafts", options=[option()]), final("建议线上讨论。")],
            self.lin,
        )
        self.assertFalse(result["drafts"])
        self.assertFalse(self.e.store.drafts("demo"))
        self.assertFalse(self.e.store.rows("SELECT * FROM answers"))
        self.assertFalse(self.e.store.rows("SELECT * FROM dialogue_runs"))

    async def test_restart_preserves_draft_and_can_confirm(self):
        draft = (await self.draft())["drafts"][0]
        await self.e.close()
        self.e = Engine(Config(self.cfg, self.temp.name))
        await self.e.start(maintenance=False)
        result = await self.talk(
            "采用第一个方案",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final(),
            ],
        )
        self.assertEqual(result["operations"][0]["status"], "recorded")

    async def test_retention_cleans_generated_state(self):
        await self.draft()
        self.e.store.expire("demo", 1, datetime.now(timezone.utc) + timedelta(days=2))
        self.assertFalse(self.e.store.drafts("demo"))
        self.assertFalse(self.e.store.rows("SELECT * FROM dialogue_runs"))

    async def test_note_correction_cannot_modify_official_fields(self):
        await self.background("/记事 评审 | 记录 | 备注=准备材料", sender="lin")
        ev = self.e.states("demo")[0]["history"][0]
        await self.background(
            "/记事 评审 | 更正 | 目标事件=" + ev["id"] + ";时间=2026-10-03",
            sender="lin",
        )
        self.assertNotIn("when", self.e.states("demo")[0]["fields"])
        self.assertEqual(self.e.states("demo")[0]["status"], "unconfirmed")

    async def test_correcting_cancel_reason_keeps_cancelled_status(self):
        await self.background("/记事 评审 | 取消 | 原因=场地未定")
        ev = self.e.states("demo")[0]["history"][0]
        await self.background(
            "/记事 评审 | 更正 | 目标事件=" + ev["id"] + ";原因=场地关闭"
        )
        self.assertEqual(self.e.states("demo")[0]["status"], "cancelled")
        self.assertEqual(self.e.states("demo")[0]["fields"]["reason"], "场地关闭")

    async def test_dialogue_message_is_not_processed_by_background_worker(self):
        class Extractor:
            model = "forbidden"

            async def extract(self, *args):
                raise AssertionError("dialogue leaked into extractor")

        self.e.extractor = Extractor()
        await self.talk(
            "设计一个方案", [tool("save_drafts", options=[option()]), final()]
        )
        await asyncio.sleep(0.02)
        self.assertEqual(
            self.e.store.rows("SELECT status FROM messages"), [{"status": "done"}]
        )

    async def test_partial_revision_preserves_fields_and_original_dependency(self):
        first = (await self.draft())["drafts"][1]
        old = self.e.store.one(
            "SELECT message_uid FROM drafts WHERE id=?", (first["id"],)
        )
        changed = await self.talk(
            "第二个提前半小时",
            [
                tool(
                    "save_drafts",
                    options=[
                        {
                            "number": 2,
                            "parent_id": first["id"],
                            "description": "修改时间，地点不变",
                            "fields": {"when": "2026-10-02T20:30:00+08:00"},
                        }
                    ],
                ),
                final(),
            ],
        )
        draft = changed["drafts"][0]
        self.assertEqual(draft["fields"]["location"], "三楼讨论室")
        await self.talk(
            "就按修改后的定了",
            [
                tool(
                    "submit_events",
                    events=[{"kind": "confirm", "draft_id": draft["id"]}],
                ),
                final(),
            ],
        )
        event = self.e.store.events("demo")[0]
        self.assertIn(old["message_uid"], event["sources"])
        native = self.e.store.message("demo", old["message_uid"])["native_id"]
        self.e.store.recall("demo", native)
        self.assertFalse(self.e.store.events("demo")[0]["valid"])

    async def test_passive_natural_confirmation_uses_shared_draft_validator(self):
        draft = (await self.draft())["drafts"][0]
        self.e.extractor.provider = Scripted(
            {"events": [{"kind": "confirm", "draft_id": draft["id"]}]}
        )
        await self.background("就采用第一个方案")
        self.assertEqual(self.e.states("demo")[0]["status"], "confirmed")
        self.assertEqual(len(self.e.store.events("demo")), 1)
