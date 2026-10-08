"""The digital-employee IM protocol (伺客 seat over WeCom), free of AstrBot imports.

Inbound (platform -> bot): POST with nine string fields, answered by HTTP 202
{"messageNo", "accepted": true, "duplicate"}. Replies go back on a separate callback
with seven fields and an idempotency header. See digital-employee-talk
docs/downstream-integration.md for the reference implementation of the other side.
"""

from __future__ import annotations

from collections import OrderedDict
import re
import uuid

INBOUND_FIELDS = (
    "messageId",
    "messageType",
    "messageContent",
    "messageSource",
    "receiveAccountId",
    "receiveUserId",
    "userId",
    "conversationType",
    "conversationId",
)
REQUEST_NO_HEADER = "X-Digital-Employee-Request-No"


class InboundError(ValueError):
    """The request is not a valid inbound message; answer HTTP 400."""


def _text(value):
    return isinstance(value, str) and value.strip() != ""


def parse_inbound(body, account_id, user_id):
    """A validated inbound message, or InboundError naming the first problem."""
    if not isinstance(body, dict):
        raise InboundError("body must be a JSON object")
    if set(body) != set(INBOUND_FIELDS):
        raise InboundError("expected exactly the nine inbound fields")
    for field in INBOUND_FIELDS:
        if not _text(body[field]):
            raise InboundError(f"{field} must be a non-empty string")
    if body["messageSource"] != "wecom":
        raise InboundError("messageSource must be wecom")
    if body["receiveAccountId"] != account_id or body["receiveUserId"] != user_id:
        raise InboundError("message is addressed to another seat")
    kind = body["conversationType"]
    if kind not in {"SINGLE", "GROUP"}:
        raise InboundError("conversationType must be SINGLE or GROUP")
    if kind == "SINGLE" and body["conversationId"] != user_id:
        raise InboundError("a SINGLE conversationId must equal receiveUserId")
    if kind == "GROUP" and body["conversationId"].strip() in {"null", ".", ".."}:
        raise InboundError("invalid group conversationId")
    if body["messageType"] not in {"TEXT", "IMAGE"}:
        raise InboundError("messageType must be TEXT or IMAGE")
    if body["messageType"] == "IMAGE" and not re.match(
        r"https?://", body["messageContent"]
    ):
        raise InboundError("an IMAGE messageContent must be an http(s) URL")
    return {
        "message_id": body["messageId"],
        "type": body["messageType"],
        "content": body["messageContent"],
        "sender": body["userId"].strip(),
        "group": body["conversationId"] if kind == "GROUP" else None,
    }


def split_mention(text, names):
    """(True, text without the @name) when the text @-mentions one of the bot's names.

    The protocol has no mention field, so a typed "@数字员工" is the only signal. A name
    must end at whitespace, punctuation or the end of the text: "@数字员工们" is not it.
    """
    names = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not names:
        return False, text
    pattern = re.compile(
        "@(?:" + "|".join(map(re.escape, names)) + r")(?=$|[\s,，:：、!！?？.。])[ \t　,，:：]*"
    )
    rest, count = pattern.subn("", text)
    return count > 0, rest.strip() if count else text


def reply_body(account_id, user_id, receiver, room_id, text):
    """The seven-field callback. room_id None is a direct reply (JSON null)."""
    return {
        "userId": user_id,
        "accountId": account_id,
        "receiver": receiver,
        "roomId": room_id,
        "isOutContact": "true",
        "msgType": "TEXT",
        "content": {"text": text},
    }


class Deduplicator:
    """Remembers recent messageIds so a redelivery is acknowledged, not processed twice."""

    def __init__(self, size=5000):
        self.size = size
        self.seen = OrderedDict()

    def accept(self, message_id):
        """(messageNo, duplicate)."""
        if message_id in self.seen:
            self.seen.move_to_end(message_id)
            return self.seen[message_id], True
        number = "sike-" + uuid.uuid4().hex
        self.seen[message_id] = number
        if len(self.seen) > self.size:
            self.seen.popitem(last=False)
        return number, False


def names_from(value):
    """Bot names from config: a list, or one comma-separated string from the WebUI."""
    if isinstance(value, str):
        value = re.split(r"[,，]", value)
    return [str(v).strip() for v in value or [] if str(v).strip()]
