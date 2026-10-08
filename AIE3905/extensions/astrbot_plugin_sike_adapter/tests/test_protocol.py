from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol import (  # noqa: E402
    Deduplicator,
    InboundError,
    names_from,
    parse_inbound,
    reply_body,
    split_mention,
)

ACCOUNT, SEAT = "LIRUNLIN919", "sales-bot-01"


def inbound(**changes):
    body = {
        "messageId": "m-1",
        "messageType": "TEXT",
        "messageContent": "@数字员工 刹车片哪天到？",
        "messageSource": "wecom",
        "receiveAccountId": ACCOUNT,
        "receiveUserId": SEAT,
        "userId": "王师傅",
        "conversationType": "GROUP",
        "conversationId": "room-demo",
    }
    body.update(changes)
    return body


class InboundTest(unittest.TestCase):
    def test_group_text(self):
        msg = parse_inbound(inbound(), ACCOUNT, SEAT)
        self.assertEqual(msg["group"], "room-demo")
        self.assertEqual(msg["sender"], "王师傅")
        self.assertEqual(msg["type"], "TEXT")

    def test_single_uses_the_seat_as_conversation(self):
        msg = parse_inbound(
            inbound(conversationType="SINGLE", conversationId=SEAT), ACCOUNT, SEAT
        )
        self.assertIsNone(msg["group"])
        with self.assertRaises(InboundError):
            parse_inbound(
                inbound(conversationType="SINGLE", conversationId="room-demo"), ACCOUNT, SEAT
            )

    def test_rejects_wrong_shapes(self):
        bad = [
            inbound(extra="x"),
            {k: v for k, v in inbound().items() if k != "userId"},
            inbound(userId=" "),
            inbound(messageSource="qq"),
            inbound(receiveAccountId="other"),
            inbound(receiveUserId="other"),
            inbound(conversationType="ROOM"),
            inbound(conversationId="null"),
            inbound(messageType="VOICE"),
            inbound(messageType="IMAGE", messageContent="file:///etc/passwd"),
            inbound(messageId=1),
            ["not", "an", "object"],
        ]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(InboundError):
                parse_inbound(body, ACCOUNT, SEAT)

    def test_image_url(self):
        msg = parse_inbound(
            inbound(messageType="IMAGE", messageContent="http://127.0.0.1:3000/api/chat/images/i1"),
            ACCOUNT,
            SEAT,
        )
        self.assertEqual(msg["content"], "http://127.0.0.1:3000/api/chat/images/i1")


class MentionTest(unittest.TestCase):
    names = ["数字员工", SEAT]

    def test_mentions(self):
        cases = {
            "@数字员工 刹车片哪天到？": (True, "刹车片哪天到？"),
            "刹车片哪天到？@数字员工": (True, "刹车片哪天到？"),
            "@数字员工，在吗": (True, "在吗"),
            "@数字员工": (True, ""),
            "@sales-bot-01 在吗": (True, "在吗"),
            "@数字员工们 都来看看": (False, "@数字员工们 都来看看"),
            "@小李 刹车片到了吗": (False, "@小李 刹车片到了吗"),
            "数字员工好用吗": (False, "数字员工好用吗"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(split_mention(text, self.names), expected)

    def test_no_names(self):
        self.assertEqual(split_mention("@数字员工 在吗", []), (False, "@数字员工 在吗"))

    def test_names_from_config(self):
        self.assertEqual(names_from("数字员工, 小助手，"), ["数字员工", "小助手"])
        self.assertEqual(names_from(["数字员工", " "]), ["数字员工"])


class ReplyTest(unittest.TestCase):
    def test_group_and_direct(self):
        group = reply_body(ACCOUNT, SEAT, "王师傅", "room-demo", "明早 9 点到。")
        self.assertEqual(
            set(group),
            {"userId", "accountId", "receiver", "roomId", "isOutContact", "msgType", "content"},
        )
        self.assertEqual(group["isOutContact"], "true")
        self.assertEqual(group["content"], {"text": "明早 9 点到。"})
        self.assertIsNone(reply_body(ACCOUNT, SEAT, "王师傅", None, "好的")["roomId"])

    def test_duplicates_keep_their_number(self):
        d = Deduplicator(size=2)
        first, dup = d.accept("m-1")
        self.assertFalse(dup)
        self.assertEqual(d.accept("m-1"), (first, True))
        d.accept("m-2")
        d.accept("m-3")
        self.assertFalse(d.accept("m-1")[1])


if __name__ == "__main__":
    unittest.main()
