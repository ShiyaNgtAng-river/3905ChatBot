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
        # Plain neighbours are not shown: in a busy group they are other conversations.
        self.assertNotIn("context", hit)
        self.assertIn(hit["uid"], s["sources"])
        none = self.e.recall.search(s, query="大巴", who="不存在的人", when="那天")
        self.assertEqual(none["messages"], [])
        self.assertEqual(len(none["notes"]), 3)

    async def test_search_shows_the_answer_that_came_later(self):
        # 2026-10-09 test: the reply "3000就行" never repeats "定金" and was missed.
        await self.say("lin", "小林", "M2X8E 两个气囊要调货，得先付定金", 0)
        await self.say("owner", "老张", "定金走月结行不行？", 5)
        for i, text in enumerate(["收到", "好的", "外面下雨了"]):
            await self.say("yu", "小余", text, 6 + i)
        await self.say("lin", "小林", "这单得先付，3000就行，其余月结", 10)
        await self.say("lin", "小林", "下午统一发货", 70)
        found = self.e.recall.search(self.state(), query="定金")["messages"]
        ask = next(x for x in found if x["text"] == "定金走月结行不行？")
        self.assertIn("3000就行", " ".join(ask.get("followups", [])))
        later = " ".join(" ".join(x.get("followups", [])) for x in found)
        self.assertNotIn("下午统一发货", later)  # more than an hour later
        self.assertNotIn("外面下雨了", later)  # not one of the people in the hits

    async def test_search_follows_a_code_to_messages_without_the_name(self):
        # "3H6T9 散热器装好了" names the plate, not the car model.
        await self.say("jie", "阿杰", "飞度 粤B·3H6T9 水箱漏了，要个散热器", 0)
        await self.say("lin", "小林", "飞度散热器 品牌380", 2)
        await self.say("he", "小何", "飞度散热器送到了", 30)
        await self.say("jie", "阿杰", "3H6T9 散热器装好了，防冻液也加了", 60)
        result = self.e.recall.search(self.state(), query="飞度 安装")
        self.assertIn("3H6T9 散热器装好了，防冻液也加了", [x["text"] for x in result["messages"]])
        self.assertTrue(any("3H6T9" in n for n in result["notes"]))
        plain = self.e.recall.search(self.state(), query="3H6T9")
        self.assertFalse(any("编号" in n for n in plain["notes"]))  # asked by code already
        # An earlier question to the bot sharing the words gives no code and comes last.
        self.state("DF5J2 大灯是谁安装的？")
        again = self.e.recall.search(self.state(), query="飞度 安装")
        self.assertFalse(any("DF5J2" in n for n in again["notes"]))
        order = [x["text"] for x in again["messages"]]
        asked = next(i for i, x in enumerate(again["messages"]) if x.get("asked_bot"))
        self.assertLess(order.index("飞度 粤B·3H6T9 水箱漏了，要个散热器"), asked)
        self.assertIn("3H6T9 散热器装好了，防冻液也加了", order)

    async def test_search_keeps_interleaved_exchanges_apart(self):
        # 2026-10-09 rerun: 小吴's "要原厂的" answered her own question about spark
        # plugs, but was shown after the wiper quote and pinned on it.
        await self.say("jie", "阿杰", "哈弗H6 雨刮有货吗", 0)
        await self.say("wu", "小吴", "思域的火花塞有没有？", 1)
        await self.say("lin", "小林", "思域火花塞 有，NGK45一支，原厂80", 3)
        await self.say("lin", "小林", "有的，哈弗H6雨刮博世的55，原厂要110", 5)
        await self.say("wu", "小吴", "要原厂的", 6)
        await self.say("he", "小何", "已出库", 7)
        await self.say("jie", "阿杰", "给我来两对", 8)
        await self.say("lin", "小林", "哈弗H6水泵 GMB480一个", 9)
        result = self.e.recall.search(self.state(), query="哈弗H6 雨刮")
        quote = next(x for x in result["messages"] if x["text"].startswith("有的，哈弗H6雨刮"))
        self.assertIn("哈弗H6 雨刮有货吗", quote["answers"])
        after = " ".join(quote.get("followups", []))
        self.assertIn("给我来两对（对方之后的话，未必在回这条）", after)
        self.assertNotIn("要原厂的", after)
        self.assertNotIn("已出库", after)
        # A brand and price is not a plate.
        self.assertFalse(any("GMB480" in n for n in result["notes"]))

    async def test_search_reads_chinese_numerals_in_dates(self):
        await self.say("lin", "小林", "8月12号那笔是 粤B·6R9A1 的两个雾灯", 0)
        found = self.e.recall.search(self.state(), query="八月十二号")["messages"]
        self.assertEqual([x["text"] for x in found], ["8月12号那笔是 粤B·6R9A1 的两个雾灯"])

    async def test_prompt_lists_the_last_seven_days(self):
        await self.chat()
        s = self.state("周一那台车")
        prompt = self.e.conversation.native_prompt(s, "老张")
        today = datetime.now(timezone(timedelta(hours=8)))
        week = "周" + "一二三四五六日"[today.weekday()]
        self.assertIn(f"{today:%m-%d} {week}。", prompt)
        monday = today - timedelta(days=today.weekday())
        self.assertIn(f"{monday:%m-%d} 周一", prompt)

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

    async def test_tool_budget_is_configurable_and_unchecked_is_not_absent(self):
        await self.chat()
        self.e.config.tool_budget = 3
        s = self.state("帮我核对三件事")
        for _ in range(3):
            self.assertNotIn("error", json.loads(
                self.e.conversation.native_tool(s, "search_history", {"query": "爬山"})))
        spent = json.loads(self.e.conversation.native_tool(s, "search_history", {"query": "周日"}))
        # Spent budget: say the item was not checked, never that the record lacks it.
        self.assertIn("没能核对", spent["error"])
        self.assertNotIn("直接回答", spent["error"])
        self.assertEqual(Config(demo_config(), ".").tool_budget, 32)

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
        # Live demo 2026-10-08: "仓库：" then a quoted draft became "仓库：，「".
        self.assertEqual(
            tidy_reply("整理好了，您直接转给仓库：\n\n「王师傅这单：刹车片明早到。」"),
            "整理好了，您直接转给仓库：「王师傅这单：刹车片明早到。」",
        )
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
