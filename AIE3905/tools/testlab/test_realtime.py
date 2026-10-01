"""No-model tests for the realtime study: export conversion, scoring and the report."""

import json
import tempfile
import unittest
from pathlib import Path

from realtime import convert_export, judge, load_questions, render_report, summarize


def message(mid, ts, uin, card, elements, **extra):
    return {
        "id": mid,
        "timestamp": ts,
        "sender": {"uin": uin, "uid": "u_" + uin, "name": card, "groupCard": card},
        "type": "text",
        "content": {"elements": elements},
        "recalled": False,
        "system": False,
        **extra,
    }


EXPORT = {
    "chatInfo": {"name": "虚构测试群"},
    "messages": [
        message("m1", 1790156507000, "101", "小林", [{"type": "text", "data": {"text": "周五下午评审"}}]),
        message("m2", 1790156510000, "102", "老张", [
            {"type": "reply", "data": {"referencedMessageId": "m1"}},
            {"type": "at", "data": {"uin": "101", "name": "小林"}},
            {"type": "text", "data": {"text": " 可以"}},
        ]),
        message("m3", 1790156520000, "103", "小余", [{"type": "image", "data": {}}]),
        message("m4", 1790156530000, "104", "机器人", [
            {"type": "markdown", "data": {"content": "**绑定成功**"}},
            {"type": "text", "data": {"text": "绑定成功"}},
        ]),
        message("m5", 1790156540000, "0", "系统消息", [{"type": "system", "data": {"text": "x 加入了群聊"}}], system=True),
        message("m6", 1790156550000, "101", "小林", [
            {"type": "reply", "data": {"referencedMessageId": "not-in-export"}},
            {"type": "file", "data": {"filename": "规则v5.docx"}},
        ]),
    ],
}


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "export.json"
        self.path.write_text(json.dumps(EXPORT, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_matches_what_the_plugin_stores(self):
        rows, info = convert_export(self.path)
        self.assertEqual(info["skipped"]["system"], 1)
        self.assertEqual([r["export_index"] for r in rows], [1, 2, 3, 4, 6])
        reply = rows[1]
        # Other members' @ become mentions, not text; the quote is kept.
        self.assertEqual(reply["text"], "可以")
        self.assertEqual(reply["mentions"], [{"sender": "101", "name": "小林"}])
        self.assertEqual(reply["reply_to"], "m1")
        self.assertEqual(rows[2]["text"], "[图片]")
        self.assertEqual(rows[3]["text"], "绑定成功")
        # A file name is invisible to the plugin; a quote outside the export is dropped.
        self.assertEqual(rows[4]["text"], "[文件]")
        self.assertNotIn("reply_to", rows[4])
        self.assertTrue(rows[0]["timestamp"].endswith("+08:00"))

    def test_questions_validated_against_the_stream(self):
        rows, _ = convert_export(self.path)
        qpath = Path(self.tmp.name) / "q.json"
        good = {"id": "a", "after": 2, "level": 1, "ask": "评审定了吗", "facts": [1, 2],
                "expect": [{"since": 1, "all": [["周五"]]}]}
        qpath.write_text(json.dumps([good]), encoding="utf-8")
        self.assertEqual(load_questions(qpath, rows)[0]["id"], "a")
        for bad in [
            {**good, "level": 9},
            {**good, "after": 99},
            {**good, "expect": [{"since": 5, "all": [["周五"]]}]},
            {**good, "expect": [{"since": 1, "all": []}]},
        ]:
            qpath.write_text(json.dumps([bad]), encoding="utf-8")
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_questions(qpath, rows)


class JudgeTests(unittest.TestCase):
    Q = {"expect": [
        {"since": 1, "all": [["今天", "25"], ["三点", "15:00"]], "none": ["26"]},
        {"since": 50, "all": [["昨天", "25"]]},
    ]}

    def test_all_groups_none_and_variants(self):
        self.assertTrue(judge(self.Q, "今天下午三点开赛", 10)["ok"])
        self.assertFalse(judge(self.Q, "今天开赛", 10)["ok"])
        self.assertFalse(judge(self.Q, "26号下午三点", 10)["ok"])
        self.assertTrue(judge(self.Q, "是昨天开始的", 60)["ok"])
        self.assertFalse(judge(self.Q, "", 10)["ok"])

    def test_normalisation_and_decline_macro(self):
        self.assertTrue(judge({"expect": [{"all": [["15:00"]]}]}, "１５：００ 开始", 1)["ok"])
        self.assertTrue(judge({"expect": [{"all": [["n15"]]}]}, "要过 N15", 1)["ok"])
        q = {"expect": [{"all": [["$DECLINE"]]}]}
        self.assertTrue(judge(q, "群里没提到这个安排", 1)["ok"])
        self.assertFalse(judge(q, "定在周六晚上七点", 1)["ok"])


class ReportTests(unittest.TestCase):
    def test_summary_and_report_render(self):
        records = [
            {"id": f"q{i}", "level": lv, "level_name": "", "kind": "", "after": after, "asked_index": after,
             "delivered": after, "distance": dist, "ask": "问题<b>", "answer": "回答&", "replies": 1,
             "ok": ok, "missing": [] if ok else [["329"]], "wrong": [], "review": "", "seconds": 3.5,
             "virtual_at": "", "message_id": i}
            for i, (lv, after, dist, ok) in enumerate([(1, 60, 5, True), (2, 60, 50, False), (5, 300, None, True), (2, 300, 280, True)])
        ]
        s = summarize(records)
        self.assertEqual(s["overall"], {"n": 4, "correct": 3, "accuracy": 0.75})
        self.assertEqual([c["delivered"] for c in s["checkpoints"]], [60, 300])
        self.assertEqual(s["distance"][0]["n"], 1)
        meta = {"group": "测试", "replayed": 300, "model": "mock-model", "model_mode": "mock", "hold": False,
                "virtual_first": "", "virtual_last": "", "real_minutes": 1, "skipped": {}, "plugin_overrides": {}}
        page = render_report({"meta": meta, "questions": records, "summary": s, "usage": {"calls": 3}, "memory_jobs": []})
        self.assertIn("<svg", page)
        self.assertIn("问题&lt;b&gt;", page)
        self.assertNotIn("问题<b>", page)


if __name__ == "__main__":
    unittest.main()
