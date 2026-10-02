"""Deterministic checks for the style meter; no data files needed."""
import unittest

from style_meter import echo, harping, measure


def convo(*pairs, context=()):
    return {"turns": list(pairs), "context": list(context)}


class StyleMeterTests(unittest.TestCase):
    def test_markers_are_per_reply_shares(self):
        m = measure([convo(("在吗", "在的～"), ("干嘛", "没事啦"), ("哦", "好"))])
        self.assertAlmostEqual(m["tilde"], 1 / 3)
        self.assertAlmostEqual(m["particle"], 1 / 3)
        self.assertEqual(m["length_median"], 3)  # 在的～ / 没事啦 / 好

    def test_question_and_offer_endings(self):
        m = measure([convo(("a", "你还不睡吗？😄"), ("b", "好的，要不要我帮你整理一下？"), ("c", "好"))])
        self.assertAlmostEqual(m["ends_question"], 2 / 3)
        self.assertAlmostEqual(m["ends_offer"], 1 / 3)

    def test_echo_counts_only_details_the_bot_brought_up(self):
        turns = [("你是谁", "我是爱音，凌晨两点还不睡呀"),
                 ("你好热情", "凌晨两点了你还在这儿"),
                 ("A场周三开会", "好，A场周三"),
                 ("再说一遍", "A场周三")]
        hits = echo(turns)
        self.assertIn("凌晨两点", hits[1])
        self.assertEqual(hits[3], [])  # the user supplied "A场周三"

    def test_harping_catches_repeats_the_user_started(self):
        turns = [("我国庆加班", "好惨"), ("你好蠢", "国庆加班还骂我"),
                 ("锐评一下", "国庆加班的人说话真冲"), ("哈哈", "国庆加班笑得出来")]
        self.assertIn("国庆加", harping(turns))
        self.assertEqual(harping(turns[:2]), [])
        rainy = [("今天下雨了", "下雨了啊"), ("我在家躺着", "下雨天躺着最舒服"),
                 ("好无聊", "下雨天窝着确实无聊"), ("你在干嘛", "下雨天没事干")]
        self.assertIn("下雨天", harping(rainy))

    def test_human_reference(self):
        m = measure([convo(("", "x"))], ["哈哈哈", "[图片]", "好～"])
        self.assertEqual(m["human"]["messages"], 2)
        self.assertAlmostEqual(m["human"]["tilde"], 0.5)


if __name__ == "__main__":
    unittest.main()
