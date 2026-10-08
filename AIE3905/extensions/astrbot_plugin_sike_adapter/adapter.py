"""AstrBot platform adapter for the digital-employee IM protocol (伺客 seat over WeCom)."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from typing import cast

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    Group,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .protocol import (
    REQUEST_NO_HEADER,
    Deduplicator,
    InboundError,
    names_from,
    parse_inbound,
    reply_body,
    split_mention,
)

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 18080,
    "inbound_path": "/api/v1/im/messages",
    "reply_url": "http://127.0.0.1:3000/api/v1/im/replies",
    "bot_account_id": "LIRUNLIN919",
    "bot_user_id": "sales-bot-01",
    "bot_names": "数字员工",
    "inbound_token": "",
}


class Callback:
    """Posts replies to the platform; retries reuse the request number, so they stay idempotent."""

    def __init__(self, config):
        self.url = config["reply_url"]
        self.account_id = config["bot_account_id"]
        self.user_id = config["bot_user_id"]
        self.session = None

    async def post(self, request_no, receiver, room_id, text):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        body = reply_body(self.account_id, self.user_id, receiver, room_id, text)
        for attempt in range(3):
            try:
                async with self.session.post(
                    self.url, json=body, headers={REQUEST_NO_HEADER: request_no}
                ) as resp:
                    if resp.status < 500:
                        if resp.status != 200:
                            logger.warning(
                                "[sike] reply %s rejected with HTTP %d", request_no, resp.status
                            )
                        return resp.status == 200
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("[sike] reply %s failed: %s", request_no, type(exc).__name__)
            await asyncio.sleep(0.5 * 2**attempt)
        return False

    async def close(self):
        if self.session is not None:
            await self.session.close()


def chain_text(chain: MessageChain):
    """The protocol carries text only; mentions become @name and media is dropped."""
    parts = []
    for c in chain.chain:
        if isinstance(c, Plain):
            parts.append(c.text)
        elif isinstance(c, At):
            parts.append(f"@{c.name or c.qq} ")
    return "".join(parts).strip()


class SikeMessageEvent(AstrMessageEvent):
    def __init__(self, message_str, message_obj, platform_meta, session_id, callback):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.callback = callback
        self.replies = itertools.count(1)

    async def send(self, message: MessageChain) -> None:
        text = chain_text(message)
        if text:
            # In a group the receiver is the member who triggered the reply; the
            # platform shows the reply to the whole group without marking anyone.
            await self.callback.post(
                f"{self.message_obj.message_id}:{next(self.replies)}",
                self.get_sender_id(),
                self.get_group_id() or None,
                text,
            )
        await super().send(message)


@register_platform_adapter(
    "sike_http",
    "数字员工 IM 协议（伺客坐席，企业微信）：HTTP 收单，回调回复",
    default_config_tmpl=dict(DEFAULTS),
    adapter_display_name="伺客 HTTP",
    support_streaming_message=False,
)
class SikeAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue):
        super().__init__(platform_config, event_queue)
        self.settings = {**DEFAULTS, **platform_config}
        self.names = names_from(self.settings["bot_names"]) + [self.settings["bot_user_id"]]
        self.callback = Callback(self.settings)
        self.dedup = Deduplicator()
        self.runner = None
        self.stopped = asyncio.Event()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="sike_http",
            description="数字员工 IM 协议（伺客坐席，企业微信）",
            id=cast(str, self.config.get("id", "sike_http")),
            support_streaming_message=False,
        )

    async def run(self) -> None:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_post(self.settings["inbound_path"], self.receive)
        app.router.add_get("/healthz", lambda _: web.json_response({"ok": True}))
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        host, port = self.settings["listen_host"], int(self.settings["listen_port"])
        await web.TCPSite(self.runner, host, port).start()
        logger.info("[sike] listening on http://%s:%d%s", host, port, self.settings["inbound_path"])
        await self.stopped.wait()

    async def terminate(self) -> None:
        self.stopped.set()
        if self.runner is not None:
            await self.runner.cleanup()
        await self.callback.close()

    async def receive(self, request: web.Request) -> web.Response:
        token = self.settings.get("inbound_token")
        if token and request.headers.get("Authorization") != f"Bearer {token}":
            return web.json_response({"accepted": False, "message": "unauthorized"}, status=401)
        try:
            body = await request.json()
            msg = parse_inbound(
                body, self.settings["bot_account_id"], self.settings["bot_user_id"]
            )
        except (InboundError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, InboundError) else "invalid JSON"
            return web.json_response({"accepted": False, "message": reason}, status=400)
        number, duplicate = self.dedup.accept(msg["message_id"])
        if not duplicate:
            self.commit_event(self.create_event(self.convert(msg, body)))
        return web.json_response(
            {"messageNo": number, "accepted": True, "duplicate": duplicate}, status=202
        )

    def convert(self, msg, raw) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.self_id = self.settings["bot_user_id"]
        abm.message_id = msg["message_id"]
        abm.sender = MessageMember(user_id=msg["sender"], nickname=msg["sender"])
        abm.raw_message = raw
        if msg["group"]:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group = Group(group_id=msg["group"])
            abm.session_id = msg["group"]
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = msg["sender"]
        if msg["type"] == "IMAGE":
            abm.message = [Image.fromURL(msg["content"])]
            abm.message_str = ""
        else:
            mentioned, text = split_mention(msg["content"], self.names)
            abm.message = ([At(qq=abm.self_id)] if mentioned else []) + (
                [Plain(text)] if text else []
            )
            abm.message_str = text
        return abm

    def create_event(self, message: AstrBotMessage) -> SikeMessageEvent:
        return SikeMessageEvent(
            message.message_str, message, self.meta(), message.session_id, self.callback
        )

    async def send_by_session(self, session, message_chain: MessageChain) -> None:
        text = chain_text(message_chain)
        if text:
            group = session.message_type == MessageType.GROUP_MESSAGE
            # A message nobody triggered has no member to name; the bot seat stands in.
            await self.callback.post(
                "push:" + uuid.uuid4().hex,
                self.settings["bot_user_id"] if group else session.session_id,
                session.session_id if group else None,
                text,
            )
        await super().send_by_session(session, message_chain)
