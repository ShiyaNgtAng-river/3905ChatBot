"""v2 memory: whole-day reading, consolidation into anchors, rendering and deletion."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from secretary.config import Config
from secretary.engine import Engine
from secretary.sample import demo_config
from secretary.types import Actor, Message

TZ = timezone(timedelta(hours=8))


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class Scripted:
    model = "scripted"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def complete(self, system, payload, role, group, **kw):
        self.calls.append((system, payload, role))
        step = self.steps.pop(0)
        if callable(step):
            step = step(payload)
        return step if isinstance(step, str) else json.dumps(step, ensure_ascii=False)


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = demo_config()
        cfg["memory"] = {
            "read_new_chars": 30,
            "read_idle_minutes": 60,
            "read_min_minutes": 10,
        }
        self.e = Engine(Config(cfg, self.temp.name))
        await self.e.start(maintenance=False)
        self.r = self.e.reader
        self.n = 0

    async def asyncTearDown(self):
        await self.e.close()
        self.temp.cleanup()

    async def say(self, sender, name, text, when, **kw):
        self.n += 1
        m = Message("demo", sender, text, when.isoformat(), native_id=f"n{self.n}", name=name, **kw)
        self.e.ingest(m)
        await self.e.flush("demo")
        return self.e.store.one("SELECT seq FROM messages WHERE uid=?", (m.uid,))["seq"]

    def model(self, *steps):
        self.r.reading = self.r.consolidating = Scripted(*steps)
        return self.r.reading

    def state(self, text="之前怎么定的", sender="owner", when=None):
        self.n += 1
        m = Message("demo", sender, text, (when or at(8, 12)).isoformat(), native_id=f"ask{self.n}", name="老张")
        row = self.e.ingest(m, route="dialogue")
        return self.e.conversation.native_state(Actor(sender, ["demo"]), "demo", row)

    async def first_day(self):
        """Day one: a plan with a time, a question, a term, some noise."""
        s = [
            await self.say("lin", "小林", "周六去爬山吧", at(5, 9)),
            await self.say("owner", "老张", "好，周六早上八点东门集合", at(5, 9, 5)),
            await self.say("yu", "小余", "谁带急救包？老地方见", at(5, 9, 10)),
            await self.say("yu", "小余", "哈哈哈", at(5, 9, 11)),
            await self.say("lin", "小林", "😂😂", at(5, 9, 12)),
        ]
        side = {
            "topics": [
                {
                    "id": "t1",
                    "title": "周末爬山",
                    "time": "09:00–09:10",
                    "status": "已定",
                    "people": ["小林", "老张"],
                    "points": [
                        {"text": "老张定了周六早上八点东门集合", "m": [s[1]]},
                        {"text": "编造的要点", "m": [99999]},
                    ],
                },
                {"id": "t9", "title": "没有依据的话题", "points": [{"text": "x", "m": []}]},
            ],
            "terms": [{"term": "老地方", "meaning": "东门", "m": [s[2]]}],
        }
        self.model(side)
        self.assertEqual(await self.r.read_pass("demo", "2026-10-05", at(5, 10)), "2026-10-05")
        return s

    async def test_transcript_merges_noise_and_echoes_and_marks_replies(self):
        first = await self.say("lin", "小林", "周末团建去爬山吧", at(5, 9))
        await self.say("yu", "小余", "😂😂", at(5, 9, 1))
        await self.say("owner", "老张", "哈哈哈哈", at(5, 9, 2))
        await self.say("zhao", "小赵", "", at(5, 9, 3), attachments=[{"type": "Image"}])
        for who in ("a", "b", "c"):
            await self.say(who, who, "+1", at(5, 9, 4))
        await self.say("owner", "老张", "我同意", at(5, 9, 5), reply_to="n1")
        text, seqs = self.r.transcript("demo", "2026-10-05", self.r.rows("demo", "2026-10-05"))
        self.assertIn(f"[m{first} 09:00 小林] 周末团建去爬山吧", text)
        self.assertIn("3条表情/笑声/图片]", text)
        self.assertIn("3人复读] +1", text)
        self.assertIn(f"我同意 ↩m{first}", text)
        self.assertEqual(len(seqs), 8)

    async def test_due_waits_for_volume_or_quiet_and_keeps_a_minimum_gap(self):
        await self.say("lin", "小林", "早", at(5, 9))
        self.assertIsNone(self.r.due("demo", at(5, 9, 5)))
        self.assertEqual(self.r.due("demo", at(5, 10, 1)), "2026-10-05")
        self.model({"topics": []})
        await self.r.read_pass("demo", "2026-10-05", at(5, 10, 1))
        await self.say("owner", "老张", "下午三点在三楼会议室讨论新版本的发布安排，大家准备一下材料，别迟到", at(5, 10, 5))
        self.assertIsNone(self.r.due("demo", at(5, 10, 6)))  # within read_min_minutes
        self.assertEqual(self.r.due("demo", at(5, 10, 12)), "2026-10-05")

    async def test_passes_share_a_byte_identical_prefix(self):
        await self.say("lin", "小林", "周六去爬山吧", at(5, 9))
        await self.say("owner", "老张", "好", at(5, 9, 1))
        provider = self.model({"topics": []}, {"topics": []})
        await self.r.read_pass("demo", "2026-10-05", at(5, 10))
        await self.say("yu", "小余", "我也去", at(5, 10, 30))
        await self.r.read_pass("demo", "2026-10-05", at(5, 11))
        (sys1, first, _), (sys2, second, _) = provider.calls
        self.assertEqual(sys1, sys2)
        head = first.split("\n</聊天记录>")[0]
        self.assertTrue(second.startswith(head))
        self.assertIn("本轮新增的消息从 m3 开始", second)

    async def test_view_keeps_only_supported_points_and_survives_bad_output(self):
        s = await self.first_day()
        view = json.loads(self.e.store.one("SELECT sidebar FROM day_views")["sidebar"])
        self.assertEqual([t["title"] for t in view["topics"]], ["周末爬山"])
        self.assertEqual(view["topics"][0]["points"], [{"text": "老张定了周六早上八点东门集合", "m": [s[1]]}])
        self.assertEqual(view["terms"][0]["term"], "老地方")
        self.model("不是 JSON", "还是不行", {"topics": []})
        with self.assertRaises(ValueError):  # asked twice, both malformed
            await self.r.read_pass("demo", "2026-10-05", at(5, 11))
        with self.assertRaises(ValueError):  # an empty view never replaces a good one
            await self.r.read_pass("demo", "2026-10-05", at(5, 11))
        again = json.loads(self.e.store.one("SELECT sidebar FROM day_views")["sidebar"])
        self.assertEqual(again, view)

    async def test_malformed_json_is_asked_again_once(self):
        await self.say("lin", "小林", "周六去爬山吧", at(5, 9))
        good = {"topics": [{"id": "t1", "title": "爬山", "points": [{"text": "周六爬山", "m": [1]}]}]}
        provider = self.model('{"topics":[{"title":"爬山"说好了}]}', good)
        self.assertEqual(await self.r.read_pass("demo", "2026-10-05", at(5, 10)), "2026-10-05")
        self.assertEqual(len(provider.calls), 2)
        self.assertIn("不是合法 JSON", provider.calls[1][1])

    async def test_removal_during_the_call_discards_the_pass(self):
        await self.say("lin", "小林", "周六去爬山吧", at(5, 9))

        def recall_then_answer(payload):
            self.e.store.recall("demo", "n1")
            return {"topics": [{"id": "t1", "title": "爬山", "points": [{"text": "周六爬山", "m": [1]}]}]}

        self.model(recall_then_answer)
        self.assertIsNone(await self.r.read_pass("demo", "2026-10-05", at(5, 10)))
        self.assertIsNone(self.e.store.one("SELECT 1 FROM day_views"))

    async def consolidate_first_day(self, s):
        self.model(
            {
                "qa": [
                    {"q": 2, "answer": "老张定了周六早上八点东门集合爬山", "m": [s[1]]},
                    {"q": 5, "answer": "无", "m": []},
                    {"q": 4, "answer": "没有依据的回答", "m": [424242]},
                ],
                "ops": [
                    {"op": "topic", "ref": "new1", "title": "周末爬山", "importance": 5, "aliases": ["团建"], "m": [s[0]]},
                    {"op": "record", "topic": "new1", "text": "10-05 09:05 老张定了周六早上八点东门集合爬山", "m": [s[1]]},
                    {"op": "record", "topic": "new1", "text": "10-05 09:10 小余问谁带急救包，当时没人回", "m": [s[2]]},
                    {"op": "person", "name": "老张", "text": "常负责定时间和地点", "m": [s[1]]},
                    {"op": "term", "term": "老地方", "meaning": "小余说指学校东门", "m": [s[2]]},
                    {"op": "record", "topic": "a999", "text": "不存在的话题", "m": [s[0]]},
                    {"op": "record", "topic": "new1", "text": "没有依据"},
                    {"op": "record", "topic": "new1", "text": "太长" * 70, "m": [s[1]]},
                ],
            }
        )
        self.assertEqual(self.r.due_consolidation("demo", at(6, 3)), None)  # before 04:00
        self.assertEqual(self.r.due_consolidation("demo", at(6, 5)), "2026-10-05")
        self.assertEqual(await self.r.consolidate("demo", at(6, 5)), "2026-10-05")

    async def test_consolidation_only_organizes_and_records(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        topics = self.e.store.rows("SELECT title,aliases,importance FROM anchor_topics")
        self.assertEqual(topics, [{"title": "周末爬山", "aliases": "[]", "importance": 0}])  # no verdicts kept
        facts = self.e.store.rows("SELECT kind,statement FROM anchor_facts ORDER BY id")
        self.assertEqual([f["kind"] for f in facts], ["record", "record"])
        qa = json.loads(self.e.store.one("SELECT qa FROM digests WHERE level='day'")["qa"])
        self.assertEqual([a["q"] for a in qa], [2])
        note = self.e.store.one("SELECT summary,sources FROM profiles WHERE sender='owner'")
        self.assertEqual((note["summary"], json.loads(note["sources"])), ("常负责定时间和地点", [s[1]]))
        self.assertEqual(self.e.store.one("SELECT meaning FROM lexicon")["meaning"], "小余说指学校东门")
        self.assertIsNone(self.r.due_consolidation("demo", at(6, 5)))

    async def second_day(self, s):
        s2 = [
            await self.say("owner", "老张", "周六下雨，爬山改到周日早上八点", at(6, 20)),
            await self.say("yu", "小余", "急救包我带", at(6, 20, 5)),
        ]
        self.model({"topics": []})
        await self.r.read_pass("demo", "2026-10-06", at(6, 21))
        topic = self.e.store.one("SELECT id FROM anchor_topics")["id"]
        first = self.e.store.one("SELECT id FROM anchor_facts ORDER BY id")["id"]
        self.model(
            lambda payload: {
                "qa": [{"q": 3, "answer": "爬山从周六改到周日", "m": [s2[0]]}],
                "ops": [
                    {"op": "topic", "ref": f"a{topic}", "title": "周末爬山", "m": [s2[0]]},
                    # An older prompt's verdict fields are ignored, not obeyed.
                    {"op": "fact", "topic": f"a{topic}", "kind": "decision", "supersedes": f"f{first}",
                     "text": "10-06 20:00 老张说因下雨改到周日早上八点", "m": [s2[0]]},
                    {"op": "record", "topic": f"a{topic}", "text": "10-06 20:05 小余说急救包他带", "m": [s2[1]]},
                ],
            }
            if "10-05 老张定了周六早上八点东门集合爬山" in payload.replace("10-05 09:05 ", "")
            else {"qa": [], "ops": []}
        )
        self.assertEqual(await self.r.consolidate("demo", at(7, 5)), "2026-10-06")
        return s2, topic

    async def test_changes_are_kept_in_time_order_and_nothing_is_hidden(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        s2, topic = await self.second_day(s)
        texts = [f["statement"] for f in self.r.facts("demo", topic)]
        self.assertEqual(len(texts), 4)
        self.assertLess(texts.index("10-05 09:05 老张定了周六早上八点东门集合爬山"), texts.index("10-06 20:00 老张说因下雨改到周日早上八点"))
        self.assertEqual(self.e.store.rows("SELECT 1 FROM anchor_facts WHERE superseded_by!=0"), [])
        t = self.e.store.one("SELECT * FROM anchor_topics WHERE id=?", (topic,))
        self.assertEqual((t["days_seen"], t["last_day"]), (2, "2026-10-06"))
        brief = self.r.brief("demo", "爬山几点集合", at(7, 9).astimezone(timezone.utc).isoformat())
        self.assertIn("按时间排列，后面的更新", brief)
        self.assertLess(brief.index("周六早上八点"), brief.index("改到周日早上八点"))
        line = self.r.timeline(self.state("爬山怎么定的", when=at(7, 9)), "爬山")
        self.assertEqual([r["day"] for r in line[0]["records"]], ["2026-10-05", "2026-10-05", "2026-10-06", "2026-10-06"])
        self.assertIn("老张", line[0]["records"][0]["evidence"][0])

    async def test_other_names_are_found_through_records(self):
        s = await self.first_day()
        self.model(
            {
                "qa": [],
                "ops": [
                    {"op": "topic", "ref": "new1", "title": "LGU杯", "m": [s[0]]},
                    {"op": "record", "topic": "new1", "text": "10-05 老张的公告写作「LGU杯（神仙胡杯）正式开赛」", "m": [s[1]]},
                ],
            }
        )
        await self.r.consolidate_day("demo", "2026-10-05", at(6, 5))
        st = self.state("神仙胡杯", when=at(6, 9))
        line = self.r.timeline(st, "神仙胡杯什么时候开赛")
        self.assertEqual(line[0]["topic"], "LGU杯")

    async def test_memory_tools_carry_dates_and_related_records(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        st = self.state("爬山怎么定的", when=at(6, 9))
        line = json.loads(self.e.conversation.native_tool(st, "timeline", {"query": "爬山"}))
        self.assertEqual(line["context"]["today"], "2026-10-06")
        self.assertEqual(line["context"]["records"], "2026-10-05 至 2026-10-06")
        related = line["context"]["related_topics"][0]
        self.assertEqual(related["topic"], "周末爬山")
        self.assertIn("2026-10-05 10-05 09:05 老张定了周六早上八点东门集合爬山", related["records"])
        self.assertEqual(line["results"][0]["topic"], "周末爬山")
        self.model()
        empty = json.loads(await self.e.conversation.native_tool_async(st, "read_day", {"when": "09-24", "question": "几点"}))
        self.assertIn("本群记录覆盖 2026-10-05 至 2026-10-06", empty["answer"])

    async def test_questions_to_the_bot_are_marked_not_evidence(self):
        await self.say("lin", "小林", "周六去爬山吧", at(5, 9))
        st = self.state("听说爬山改到周日了？", sender="yu", when=at(5, 10))
        await self.say("owner", "老张", "没改", at(5, 11))
        text, _ = self.r.transcript("demo", "2026-10-05", self.r.rows("demo", "2026-10-05"))
        self.assertIn("老张 @助手] 听说爬山改到周日了？", text)
        later = self.state("爬山", when=at(5, 12))
        found = self.e.recall.search(later, query="改到周日")["messages"]
        self.assertTrue(any(x.get("asked_bot") for x in found))
        self.assertIsNotNone(st)

    async def test_removed_evidence_takes_derived_memory_with_it(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        s2, topic = await self.second_day(s)
        self.e.store.recall("demo", "n6")  # 周六下雨，改到周日
        texts = [f["statement"] for f in self.r.facts("demo", topic)]
        self.assertNotIn("10-06 20:00 老张说因下雨改到周日早上八点", texts)
        self.assertIn("10-05 09:05 老张定了周六早上八点东门集合爬山", texts)
        digest = self.e.store.one("SELECT qa FROM digests WHERE period='2026-10-06'")
        self.assertEqual(json.loads(digest["qa"]), [])  # row stays as the consolidated marker
        self.e.store.recall("demo", "n3")  # 谁带急救包？老地方见
        self.assertIsNone(self.e.store.one("SELECT 1 FROM lexicon WHERE term='老地方'"))
        view = json.loads(self.e.store.one("SELECT sidebar FROM day_views WHERE day='2026-10-05'")["sidebar"])
        self.assertEqual(view["terms"], [])
        self.e.store.optout("demo", "owner")
        self.assertIsNone(self.e.store.one("SELECT 1 FROM profiles WHERE sender='owner'"))
        left = self.e.store.rows("SELECT statement FROM anchor_facts WHERE topic_id=?", (topic,))
        self.assertEqual(left, [{"statement": "10-06 20:05 小余说急救包他带"}])

    async def test_native_prompt_puts_memory_first(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        st = self.state("周六爬山几点", when=at(5, 12))
        prompt = self.e.conversation.native_prompt(st, "老张")
        self.assertTrue(prompt.startswith("<group_memory>\n今天群里的话题"))
        self.assertIn("- 周末爬山（09:00–09:10）：老张定了周六早上八点东门集合", prompt)
        self.assertLess(prompt.index("</group_memory>"), prompt.index("<group_chat>"))

    async def test_brief_respects_budgets_and_prefers_relevant_topics(self):
        self.e.config.anchor_chars = 60
        with self.e.store.tx() as db:
            for i in range(12):
                cur = db.execute(
                    "INSERT INTO anchor_topics(group_key,title,aliases,status,importance,first_day,last_day,days_seen,sources,updated_at) VALUES('demo',?,'[]','active',?,?,?,1,'[1]','')",
                    (f"话题{i}" if i != 7 else "年会节目", 5 if i < 3 else 1, "2026-10-01", "2026-10-01"),
                )
                db.execute(
                    "INSERT INTO anchor_facts(group_key,topic_id,kind,statement,day,sources,at) VALUES('demo',?,'fact',?,'2026-10-01','[1]','')",
                    (cur.lastrowid, f"第{i}件事的一个比较长的说明，用来占预算"),
                )
        brief = self.r.brief("demo", "年会节目排好了吗", at(7, 9).astimezone(timezone.utc).isoformat())
        lines = [x for x in brief.splitlines() if x.startswith("- ")]
        self.assertTrue(lines[0].startswith("- 年会节目"))
        self.assertLessEqual(len(lines), 2)

    async def test_summaries_and_reread_use_views_digests_and_the_raw_day(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        st = self.state("最近聊了啥", when=at(6, 9))
        recent = self.r.summaries(st, when="最近3天")
        self.assertEqual(recent["periods"][0]["period"], "2026-10-05")
        self.assertIn("周末爬山", recent["periods"][0]["topics"][0])
        week = self.r.summaries(st, when="最近7天")
        self.assertEqual(week["periods"][0]["summary"], ["老张定了周六早上八点东门集合爬山"])
        self.assertEqual(self.r.summaries(st, who="不存在的人")["periods"], [])
        provider = self.model({"answer": "老张说周六早上八点在东门集合", "m": [s[1]]})
        out = await self.r.reread(st, "昨天", "几点集合")
        self.assertEqual(out["day"], "2026-10-05")
        self.assertIn("东门集合", out["evidence"][0])
        self.assertIn("<聊天记录 日期=2026-10-05>", provider.calls[0][1])
        with self.assertRaises(ValueError):
            await self.r.reread(st, "上周", "几点")

    async def test_weekly_rollup_after_the_week_and_rebuilt_after_removal(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        self.assertIsNone(self.r.due_rollup("demo", at(11, 9)))  # week of 10-05 not over
        level, period, rows = self.r.due_rollup("demo", at(12, 9))
        self.assertEqual((level, period), ("week", "2026-W41"))
        self.model({"qa": [{"q": 3, "answer": "定了周六八点爬山", "days": ["2026-10-05"]}, {"q": 1, "answer": "无出处", "days": []}]})
        self.assertEqual(await self.r.rollup("demo", at(12, 9)), "2026-W41")
        week = self.e.store.one("SELECT qa,sources FROM digests WHERE level='week'")
        self.assertEqual(json.loads(week["sources"]), [s[1]])
        self.e.store.recall("demo", "n2")
        self.assertIsNone(self.e.store.one("SELECT 1 FROM digests WHERE level='week'"))

    async def test_quiet_topics_turn_dormant_then_archive(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        for day, status in (("2026-10-13", "dormant"), ("2026-11-05", "archived")):
            with self.e.store.tx() as db:
                self.r.tidy(db, "demo", day)
            self.assertEqual(self.e.store.one("SELECT status FROM anchor_topics")["status"], status)
        self.assertEqual(self.r.brief("demo", "爬山", at(5, 12).isoformat()).count("周末爬山"), 1)  # day view only

    async def test_retention_removes_views_digests_and_anchors(self):
        s = await self.first_day()
        await self.consolidate_first_day(s)
        self.e.store.expire("demo", 1, now=at(10, 9))
        for table in ("day_views", "digests", "anchor_topics", "anchor_facts", "lexicon"):
            self.assertEqual(self.e.store.rows(f"SELECT 1 FROM {table}"), [], table)

    async def test_failed_job_backs_off(self):
        async def boom(key):
            raise RuntimeError("network")

        self.assertFalse(await self.e._background("demo", "read", boom))
        self.assertTrue(self.e.store.get_meta("read_retry:demo"))
        calls = []

        async def job(key):
            calls.append(key)
            return True

        self.assertFalse(await self.e._background("demo", "read", job))
        self.assertEqual(calls, [])
        failure = self.e.store.one("SELECT role,error FROM usage")
        self.assertEqual((failure["role"], failure["error"]), ("read_failure", "RuntimeError"))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.e = Engine(Config(demo_config(), self.temp.name))
        await self.e.start(maintenance=False)
        self.calls = []
        e = self

        class Understanding:
            model = "scripted"

            async def complete(self, system, payload, role, group, **kw):
                e.calls.append(payload["current"]["text"])
                return json.dumps({"events": []})

        self.e.extractor.provider = Understanding()
        self.n = 0

    async def asyncTearDown(self):
        await self.e.close()
        self.temp.cleanup()

    async def say(self, text, **kw):
        self.n += 1
        m = Message("demo", "lin", text, at(5, 9, self.n).isoformat(), native_id=f"g{self.n}", name="小林", **kw)
        self.e.ingest(m)
        await self.e.flush("demo")
        return self.e.store.one("SELECT status FROM messages WHERE uid=?", (m.uid,))["status"]

    async def test_chatter_skips_the_model_and_plans_reach_it(self):
        self.assertEqual(await self.say("午饭吃啥"), "gated")
        self.assertEqual(await self.say("哈哈哈"), "gated")
        self.assertEqual(await self.say("可以"), "gated")  # nothing awaits a confirmation
        self.assertEqual(await self.say("今晚睡觉先存一千块钱"), "gated")  # a relative time alone
        self.assertEqual(await self.say("周日早上九点爬山"), "done")
        self.assertEqual(await self.say("我负责订大巴"), "done")
        self.assertEqual(await self.say("那就这样", reply_to="g4"), "done")
        self.assertEqual(self.calls, ["周日早上九点爬山", "我负责订大巴", "那就这样"])

    async def test_gate_can_be_switched_off(self):
        self.e.extractor.gate = False
        await self.say("午饭吃啥")
        self.assertEqual(self.calls, ["午饭吃啥"])


if __name__ == "__main__":
    unittest.main()
