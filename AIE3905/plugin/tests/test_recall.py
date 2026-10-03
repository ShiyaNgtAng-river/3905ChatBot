"""Long-range memory: episodes, member notes, filtered recall and their deletion."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from secretary.config import Config
from secretary.engine import Engine
from secretary.recall import parse_range
from secretary.sample import demo_config
from secretary.types import Actor, Message, utcnow


class Scripted:
    model = "scripted"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def complete(self, system, payload, role, group):
        self.calls.append((role, payload))
        step = self.steps.pop(0)
        if callable(step):
            step = step(payload)
        return json.dumps(step, ensure_ascii=False)


class ParseRangeTests(unittest.TestCase):
    anchor = "2026-10-08T04:00:00+00:00"  # Thursday 12:00 in Shanghai

    def local_days(self, text):
        since, until = parse_range(text, self.anchor, "Asia/Shanghai")
        if since is None:
            return None
        tz = timezone(timedelta(hours=8))
        return (
            datetime.fromisoformat(since).astimezone(tz).strftime("%m-%d %H:%M"),
            datetime.fromisoformat(until).astimezone(tz).strftime("%m-%d %H:%M"),
        )

    def test_relative_and_absolute_phrases(self):
        self.assertEqual(self.local_days("今天"), ("10-08 00:00", "10-08 23:59"))
        self.assertEqual(self.local_days("昨天"), ("10-07 00:00", "10-07 23:59"))
        self.assertEqual(self.local_days("上周"), ("09-28 00:00", "10-04 23:59"))
        self.assertEqual(self.local_days("本周"), ("10-05 00:00", "10-11 23:59"))
        self.assertEqual(self.local_days("最近3天"), ("10-06 00:00", "10-08 23:59"))
        self.assertEqual(self.local_days("近三天"), ("10-06 00:00", "10-08 23:59"))
        self.assertEqual(self.local_days("10月3日"), ("10-03 00:00", "10-03 23:59"))
        self.assertEqual(self.local_days("12月3号"), ("12-03 00:00", "12-03 23:59"))
        self.assertEqual(self.local_days("2026-10-03"), ("10-03 00:00", "10-03 23:59"))
        self.assertEqual(self.local_days("上个月"), ("09-01 00:00", "09-30 23:59"))
        for phrase in ("10-03", "10.3", "10/03", "10月3", "2026.10.3", "2026年10月3日"):
            self.assertEqual(self.local_days(phrase), ("10-03 00:00", "10-03 23:59"), phrase)

    def test_unknown_or_invalid_phrases_are_not_guessed(self):
        self.assertIsNone(self.local_days("那天"))
        self.assertIsNone(self.local_days("2月30日"))
        self.assertIsNone(self.local_days(""))

    def test_month_day_in_future_means_last_year(self):
        since, _ = parse_range("12月3日", self.anchor, "Asia/Shanghai")
        self.assertTrue(since.startswith("2025-12-02T16:00"))


class RecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = demo_config()
        # The v3 episode path, kept as the v2 evaluation baseline.
        cfg["memory"] = {
            "reading": False,
            "episodes": True,
            "episode_size": 6,
            "episode_min": 3,
            "episode_idle_minutes": 20,
        }
        self.e = Engine(Config(cfg, self.temp.name))
        await self.e.start(maintenance=False)
        self.base = datetime.now(timezone.utc) - timedelta(hours=2)
        self.n = 0

    async def asyncTearDown(self):
        await self.e.close()
        self.temp.cleanup()

    async def say(self, sender, name, text, minutes):
        self.n += 1
        m = Message(
            "demo",
            sender,
            text,
            (self.base + timedelta(minutes=minutes)).isoformat(),
            native_id=f"r{self.n}",
            name=name,
        )
        self.e.ingest(m)
        await self.e.flush("demo")
        return m

    def state(self, text="之前谁说过", sender="owner"):
        self.n += 1
        m = Message("demo", sender, text, utcnow(), native_id="ask" + str(self.n))
        row = self.e.ingest(m, route="dialogue")
        return self.e.conversation.native_state(Actor(sender, ["demo"]), "demo", row)

    async def chat(self):
        await self.say("lin", "小林", "周末团建去爬山吧", 0)
        await self.say("yu", "小余", "我周六不行，周日可以", 1)
        await self.say("lin", "小林", "那就周日早上九点", 2)
        await self.say("owner", "老张", "我负责订大巴", 3)

    def model(self):
        self.e.extractor.provider = Scripted(
            {
                "summary": "小林提议周日九点团建爬山，老张订大巴。",
                "topics": ["团建", "爬山"],
            },
            lambda p: {
                "profiles": [
                    {"sender": x["sender"], "summary": x["name"] + "常组织活动"}
                    for x in p["members"]
                ]
            },
        )
        return self.e.extractor.provider

    async def test_episode_waits_for_size_or_idle_time(self):
        await self.say("lin", "小林", "刚开始聊", 118)
        self.assertEqual(self.e.recall.due("demo"), [])
        await self.chat()
        self.assertEqual(len(self.e.recall.due("demo")), 5)
        eid = await self.e.recall.build("demo")
        self.assertTrue(eid)
        self.assertEqual(self.e.recall.due("demo"), [])

    async def test_model_summary_and_member_notes(self):
        await self.chat()
        provider = self.model()
        eid = await self.e.recall.build("demo")
        self.assertEqual([c[0] for c in provider.calls], ["episode", "profile"])
        self.assertNotIn("sender", json.dumps(provider.calls[0][1]))
        ep = self.e.store.one("SELECT * FROM episodes WHERE id=?", (eid,))
        self.assertIn("团建", ep["summary"])
        self.assertEqual(len(json.loads(ep["sources"])), 4)
        profiles = self.e.recall.profile("demo", "小林")
        self.assertEqual(profiles[0]["note"], "小林常组织活动")
        self.assertEqual(profiles[0]["messages"], 2)

    async def test_recall_removes_derived_episode_and_notes(self):
        await self.chat()
        self.model()
        await self.e.recall.build("demo")
        self.e.store.recall("demo", "r2")
        self.assertEqual(self.e.store.rows("SELECT id FROM episodes"), [])
        self.assertEqual(self.e.store.rows("SELECT sender FROM profiles"), [])
        # The span is summarised again without the recalled message.
        self.e.extractor.provider = None
        await self.e.recall.build("demo")
        ep = self.e.store.one("SELECT sources FROM episodes")
        self.assertEqual(len(json.loads(ep["sources"])), 3)

    async def test_opt_out_removes_member_note(self):
        await self.chat()
        self.model()
        await self.e.recall.build("demo")
        self.e.store.optout("demo", "owner")
        senders = {
            r["sender"] for r in self.e.store.rows("SELECT sender FROM profiles")
        }
        self.assertNotIn("owner", senders)
        self.assertEqual(self.e.store.rows("SELECT id FROM episodes"), [])

    async def test_source_removed_during_model_call_discards_episode(self):
        await self.chat()

        def recall_then_answer(payload):
            self.e.store.recall("demo", "r1")
            return {"summary": "会引用已撤回内容的摘要", "topics": []}

        self.e.extractor.provider = Scripted(recall_then_answer)
        self.assertIsNone(await self.e.recall.build("demo"))
        self.assertEqual(self.e.store.rows("SELECT id FROM episodes"), [])

    async def test_empty_summary_backs_off(self):
        await self.chat()
        self.e.extractor.provider = Scripted({"summary": ""})
        await self.e._build_memory("demo")
        self.assertEqual(self.e.store.rows("SELECT id FROM episodes"), [])
        self.assertGreater(self.e.store.get_meta("episode_retry:demo"), utcnow())
        await self.e._build_memory("demo")  # skipped: no second model call
        self.assertEqual(self.e.extractor.provider.steps, [])

    async def test_retention_expires_derived_memory(self):
        await self.chat()
        self.model()
        await self.e.recall.build("demo")
        self.e.store.expire(
            "demo", 1, now=datetime.now(timezone.utc) + timedelta(days=3)
        )
        self.assertEqual(self.e.store.rows("SELECT id FROM episodes"), [])
        self.assertEqual(self.e.store.rows("SELECT sender FROM profiles"), [])

    async def test_search_filters_by_person_and_time_with_context(self):
        await self.chat()
        s = self.state()
        found = self.e.recall.search(s, query="", who="小林", when="今天")
        texts = [m["text"] for m in found["messages"]]
        self.assertEqual(set(texts), {"周末团建去爬山吧", "那就周日早上九点"})
        hit = self.e.recall.search(s, query="大巴")["messages"][0]
        self.assertEqual(hit["name"], "老张")
        self.assertIn("那就周日早上九点", " ".join(hit["context"]))
        self.assertIn(hit["uid"], s["sources"])
        none = self.e.recall.search(s, query="大巴", who="不存在的人", when="那天")
        self.assertEqual(none["messages"], [])
        self.assertEqual(len(none["notes"]), 3)

    async def test_tools_and_prompt_expose_episodes_after_they_scroll_away(self):
        await self.chat()
        self.e.extractor.provider = None
        await self.e.recall.build("demo")
        self.e.config.context_messages = 2
        s = self.state("最近聊了什么")
        prompt = self.e.conversation.native_prompt(s, "老张")
        self.assertIn("更早的话题摘要", prompt)
        self.assertIn("小林：周末团建去爬山吧", prompt)
        episodes = json.loads(
            self.e.conversation.native_tool(s, "episodes", {"when": "今天"})
        )
        self.assertEqual(len(episodes), 1)
        profile = json.loads(
            self.e.conversation.native_tool(s, "profile", {"who": "小余"})
        )
        self.assertEqual(profile[0]["messages"], 1)
        bad = json.loads(self.e.conversation.native_tool(s, "search_history", {}))
        self.assertIn("error", bad)

    async def test_recent_chat_names_the_quoted_message_and_relative_day(self):
        asked = await self.say("yu", "小余", "明天能不能提前刷个2背包开", -60 * 24)
        self.n += 1
        self.e.ingest(
            Message(
                "demo",
                "owner",
                "@小余 可以",
                (self.base + timedelta(minutes=5)).isoformat(),
                native_id=f"r{self.n}",
                name="老张",
                reply_to=asked.native_id,
            )
        )
        await self.e.flush("demo")
        s = self.state("有人答复了吗")
        prompt = self.e.conversation.native_prompt(s, "老张")
        tz = timezone(timedelta(hours=8))
        now = datetime.fromisoformat(s["m"]["at"]).astimezone(tz)
        then = datetime.fromisoformat(asked.at).astimezone(tz)
        label = {0: "（今天）", 1: "（昨天）"}.get((now.date() - then.date()).days, "")
        quoted = f"回复小余 {then:%Y-%m-%d}{label} {then:%H:%M}「明天能不能提前刷个2背包开」] @小余 可以"
        self.assertIn(quoted, prompt)
        self.assertIn(f"时间 {now:%Y-%m-%d}（今天） {now:%H:%M}", prompt)

    def test_a_chat_reply_is_one_paragraph(self):
        from secretary.dialogue import tidy_reply

        flags = []
        text = "行啦行啦，我这不是在吗～\n\n到底啥事让你气成这样？"
        self.assertEqual(tidy_reply(text, flags), "行啦行啦，我这不是在吗～到底啥事让你气成这样？")
        self.assertIn("blank_line", flags)
        self.assertEqual(tidy_reply("先说结论\n\n然后再看"), "先说结论，然后再看")
        numbered = "最要紧的一点是缓存。\n\n1. 先清缓存\n\n2. 再重启"
        self.assertEqual(tidy_reply(numbered), numbered)  # a numbered answer keeps its layout

    async def test_recent_stock_replies_are_named_before_the_next_turn(self):
        from secretary.dialogue import recent_repeats

        self.assertEqual(recent_repeats(["好，我改。", "收到"], "你又错了"), ["我改"])
        self.assertEqual(
            recent_repeats(["凌晨两点了还不睡呀", "凌晨两点还在群里", "凌晨两点你还在"], "你好蠢"), ["凌晨两点"]
        )
        # Two replies with the same remark are enough; one reply is not a pattern.
        self.assertEqual(recent_repeats(["凌晨两点了", "凌晨两点了"], "嗯"), ["凌晨两点"])
        self.assertEqual(recent_repeats(["凌晨两点了"], "嗯"), [])
        # The same time and topic brought up in reply after reply, letters included
        # (live replies, 2026-10-02): named even when the latest reply skipped them.
        live = ["晚上好呀～都十点半了，改bot的进度怎么样？别又熬太晚哦。",
                "晚上还发这个，是改bot改到看开了，还是bug终于修明白啦？十点半了，别躺平哦～",
                "诶，你刚才那一串表情包连环炮我正看着呢，重新丢一句给我～"]
        self.assertEqual(sorted(recent_repeats(live, "你为什么不回复我")), ["十点半", "改bot"])
        # A phrase the user is saying now is not a tic to avoid.
        self.assertEqual(recent_repeats(["祥子同学很强", "祥子同学很冷", "祥子同学很稳"], "祥子同学呢"), [])
        await self.chat()
        now = utcnow()
        with self.e.store.tx() as db:
            for i, output in enumerate(["哪句？我改。", "诶，我改还不行嘛。"]):
                db.execute(
                    "INSERT INTO answers(id,group_key,actor,at,question,output,sources,item_ids,mode,trace) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"a{i}", "demo", "owner", now, "q", output, "[]", "[]", "native", "{}"),
                )
        s = self.state("你又记错了")
        prompt = self.e.conversation.native_prompt(s, "老张")
        self.assertNotIn("已经用过这些说法", prompt)  # not buried in the system prompt
        self.assertEqual(s["avoid"], ["我改"])
        self.assertIn("你最近几条回复里已经用过这些说法：「我改」", self.e.conversation.native_reminder(s))
        self.assertEqual(self.e.conversation.native_reminder(self.state("你好")), "")

    async def test_retry_prompt_carries_a_search_for_the_question(self):
        await self.chat()
        s = self.state("谁负责订大巴")
        prompt = self.e.conversation.native_retry_prompt(s)
        self.assertIn("不要再说要去查", prompt)
        self.assertIn("我负责订大巴", prompt)

    async def test_speaker_note_is_injected_only_for_known_members(self):
        await self.chat()
        self.model()
        await self.e.recall.build("demo")
        prompt = self.e.conversation.native_prompt(self.state("hi", "lin"), "小林")
        self.assertIn("你对他的印象：小林常组织活动", prompt)
        stranger = self.e.conversation.native_prompt(self.state("hi", "zhao"), "小赵")
        self.assertNotIn("你对他的印象", stranger)
        ghost = self.e.conversation.native_state(
            Actor("lin", ["demo"]),
            "demo",
            dict(
                uid="x", sender="lin", name="小林", text="hi", at=utcnow(), reply_to=""
            ),
            ephemeral=True,
        )
        self.assertNotIn("你对他的印象", self.e.conversation.native_prompt(ghost))
        self.e.store.optout("demo", "lin")
        after = self.e.conversation.native_prompt(self.state("hi", "yu"), "小余")
        self.assertNotIn("小林常组织活动", after)


if __name__ == "__main__":
    unittest.main()
