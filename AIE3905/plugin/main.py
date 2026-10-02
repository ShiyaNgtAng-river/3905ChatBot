from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .secretary.config import Config
from .secretary.dialogue import banned_in, drop_banned, only_filler, tidy_reply
from .secretary.engine import Engine
from .secretary.types import Actor, Message, utcnow
from .secretary.web import AuditServer

STATE_KEY = "groupsecretary_turn"
GROUP_TOOLS = {
    "search_group_history",
    "get_group_episodes",
    "get_member_profile",
    "get_topic_timeline",
    "read_group_day",
    "read_group_items",
    "save_group_drafts",
    "submit_group_events",
}
# Tools the main agent must leave to a configured subagent, keyed by handoff name.
DELEGATED = {
    "transfer_to_search": (
        "web_search_",
        "tavily_extract_web_page",
        "firecrawl_extract_web_page",
        "exa_get_contents",
    ),
    "transfer_to_memory": (
        "search_group_history",
        "get_group_episodes",
        "get_member_profile",
        "get_topic_timeline",
        "read_group_day",
    ),
}


class GroupSecretary(Star):
    """Collect every delivered group message and answer each turn exactly once.

    Plugin commands are answered here. With dialogue.frontend=astrbot, natural
    mentions are answered by the host agent using this plugin's context and tools.
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.options = config or {}
        self.engine = None
        self.web = None
        self.reply_tasks = set()
        self.reply_gate = asyncio.Semaphore(4)

    async def initialize(self):
        directory = StarTools.get_data_dir("astrbot_plugin_groupsecretary")
        configured = str(self.options.get("config_path", "")).strip()
        path = (
            Path(configured).expanduser().resolve()
            if configured
            else directory / "config.json"
        )
        if not path.exists():
            if configured:
                raise ValueError("指定的 config_path 不存在")
            path.write_text(
                json.dumps(
                    {
                        "mode": "demo",
                        "database": "secretary.sqlite3",
                        "groups": [],
                        "web": {"enabled": False},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.logger.info(
                "群聊助手已生成配置模板，默认不采集任何群；配置路径：%s", path
            )
        cfg = Config.load(path)
        self.engine = Engine(cfg, context=self.context)
        try:
            await self.engine.start(sender=self.send_to_group)
            if cfg.web.get("enabled", False):
                self.web = AuditServer(self.engine)
                self.web.start()
                self.logger.info("群聊助手审核页：%s", self.web.url)
        except Exception:
            await self.engine.close()
            self.engine = None
            raise

    def group_for(self, platform_id, group_id):
        if not self.engine:
            return None
        return next(
            (
                g
                for g in self.engine.config.groups.values()
                if g.enabled
                and g.data_use_confirmed
                and g.platform_id == platform_id
                and g.native_group_id == str(group_id)
            ),
            None,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe(self, event: AstrMessageEvent):
        if not self.engine:
            return
        g = self.group_for(event.get_platform_id(), event.get_group_id())
        if not g:
            return
        # AstrBot 4.28.1 uses True to FORBID its default LLM request.
        # False allows a second, independent response while our task is running.
        event.should_call_llm(True)
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        obj = event.message_obj
        raw = getattr(obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("notice_type") == "group_recall":
            await self.ingest_recall(
                event.get_platform_id(), event.get_group_id(), str(raw["message_id"])
            )
            return
        message_id = str(getattr(obj, "message_id", "") or "")
        if not message_id:
            self.logger.warning(
                "群聊助手未记录一条缺少稳定消息 ID 的事件；请检查适配器。"
            )
            return
        self.engine.store.set_meta("umo:" + g.key, event.unified_msg_origin)
        components = event.get_messages()
        plains = [
            getattr(c, "text", "")
            for c in components
            if c.__class__.__name__ == "Plain"
        ]
        text = "".join(plains).strip() if plains else event.get_message_str().strip()
        reply = next(
            (
                str(getattr(c, "id", "") or getattr(c, "message_id", ""))
                for c in components
                if c.__class__.__name__ == "Reply"
            ),
            "",
        )
        # QQ's send() does not return a native message ID. Resolve an exact quoted
        # assistant reply when the adapter supplies its text; never fuzzy-match it.
        quoted = next(
            (
                getattr(c, "message_str", "") or ""
                for c in components
                if c.__class__.__name__ == "Reply"
            ),
            "",
        )
        if quoted:
            matches = self.engine.store.rows(
                "SELECT id,output FROM answers WHERE group_key=? ORDER BY at DESC LIMIT 30",
                (g.key,),
            )
            matched = [
                a
                for a in matches
                if quoted.strip() in {a["output"].strip(), a["output"][:1800].strip()}
            ]
            if len(matched) == 1:
                reply = matched[0]["id"]
        attachments = [
            {"type": c.__class__.__name__, "processed": False}
            for c in components
            if c.__class__.__name__ in {"Image", "Record", "Video", "File"}
        ]
        sender = str(event.get_sender_id())
        actor = Actor(sender, [g.key], sender in g.admins)
        # Explicit privacy controls must be processed before storing the command itself.
        if text in {"/别记我", "别记我", "/恢复记录"}:
            result = await self.engine.command(actor, g.key, text)
            await event.send(MessageChain().message(result["text"]))
            return
        message = Message(
            g.key,
            sender,
            text,
            getattr(obj, "timestamp", None) or utcnow(),
            native_id=message_id,
            name=event.get_sender_name() or sender,
            reply_to=reply,
            attachments=attachments,
        )
        explicit = text.startswith(
            (
                "/问 ",
                "/回溯 ",
                "/群报",
                "/记事 ",
                "/确认 ",
                "/反馈 ",
                "/原文 ",
                "/聊 ",
                "/忘掉 ",
                "/事项",
                "/秘书帮助",
                "/秘书状态",
            )
        ) or text in {"这条记住", "/记住", "这个已经过时了", "已经过时了"}
        wake = bool(getattr(event, "is_at_or_wake_command", False))
        # "/" is also a wake prefix; other bots' commands in the group are not for us.
        at_me = any(
            c.__class__.__name__ == "At"
            and str(getattr(c, "qq", "")) == str(event.get_self_id())
            for c in components
        )
        if text.startswith("/") and not explicit and not at_me:
            wake = False
        # Natural-language mentions go to the host agent (persona, subagents, web
        # search); this plugin supplies group context and validated tools.
        native = (
            wake
            and not explicit
            and not text.startswith("/")
            and self.engine.config.frontend == "astrbot"
            and self.engine.config.dialogue_enabled
        )
        wants_reply = explicit or wake
        try:
            saved = self.engine.ingest(
                message,
                route="dialogue"
                if native or (wants_reply and self.engine.uses_dialogue(text))
                else "background",
            )
        except ValueError:
            self.logger.warning(
                "群聊助手发现重复 ID 的内容变化；须由适配器提供 edit 与 revision。"
            )
            return
        # An opted-out member may still ask a one-off question; it will not be logged.
        if saved is not None and not saved["_new"]:
            return
        if native:
            row = saved or dict(
                uid=message.uid,
                group_key=g.key,
                sender=sender,
                name=message.name,
                text=text,
                at=message.at,
                reply_to=message.reply_to,
            )
            event.set_extra(
                STATE_KEY,
                self.engine.conversation.native_state(
                    actor, g.key, row, ephemeral=saved is None
                ),
            )
            # Everyday mentions run on the fast model; only research-style requests
            # pay for the slower reasoning model.
            cfg = self.engine.config
            deep = any(w in text for w in cfg.deep_keywords)
            provider = cfg.deep_provider if deep else cfg.fast_provider
            if provider:
                event.set_extra("selected_provider", provider)
            # Only this turn re-enables the host chain; it replaces our own reply.
            event.should_call_llm(False)
            return
        if not wants_reply:
            return
        if len(self.reply_tasks) >= 32:
            await event.send(
                MessageChain().message("当前查询较多，已保存消息；请稍后再问。")
            )
            return
        task = asyncio.create_task(self._reply(event, actor, g.key, text, message))
        self.reply_tasks.add(task)
        task.add_done_callback(self.reply_tasks.discard)

    async def _reply(self, event, actor, key, text, message):
        try:
            async with self.reply_gate:
                result = await self.engine.command(
                    actor, key, text or "/秘书帮助", message=message
                )
                output = result["text"]
                if (
                    result.get("id")
                    and not result.get("mode", "").startswith("dialogue")
                    and not self.engine.store.opted_out(key, actor.user)
                ):
                    output += "\n\n反馈：/反馈 " + result["id"] + " 类型"
                chunks = await self._send_chunks(
                    lambda chain: event.send(chain), output
                )
                self.logger.info(
                    "Group secretary sent answer=%s chunks=%d chars=%d",
                    result.get("id", "command"),
                    chunks,
                    len(output),
                )
        except (ValueError, PermissionError) as exc:
            await event.send(MessageChain().message(str(exc)[:200]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "Group secretary reply failed (%s); check provider and platform status.",
                type(exc).__name__,
            )

    @filter.on_llm_request()
    async def inject_group_context(self, event: AstrMessageEvent, req):
        """Give the host agent this group's record and tools for a mention turn."""
        state = event.get_extra(STATE_KEY)
        tools = getattr(req, "func_tool", None)
        names = set(tools.names()) if tools else set()
        if not state or not self.engine:
            # Group tools only make sense inside a configured group turn.
            for name in GROUP_TOOLS & names:
                tools.remove_tool(name)
            return
        contexts = req.contexts
        if isinstance(contexts, str):
            contexts = json.loads(contexts)
        # Host history can hold text this plugin has since erased (recall, opt-out,
        # retention). Keep only persona example dialogs, which are never saved.
        req.contexts = [
            c for c in contexts or [] if isinstance(c, dict) and c.get("_no_save")
        ]
        req.system_prompt = (
            (req.system_prompt or "")
            + "\n"
            + self.engine.conversation.native_prompt(state, event.get_sender_name())
        )
        state["system_prompt"] = req.system_prompt
        # Next to the current message, not saved to history: a note at the end of the long
        # system prompt did not stop the model from repeating itself.
        reminder = self.engine.conversation.native_reminder(state)
        if reminder and hasattr(req, "extra_user_content_parts"):
            try:
                from astrbot.core.agent.message import TextPart
            except ImportError:
                TextPart = None
            if TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=reminder).mark_as_temp())
        # A configured subagent owns its tools so the router cannot bypass it.
        for handoff, prefixes in DELEGATED.items():
            if handoff in names:
                for name in names:
                    if name != handoff and name.startswith(prefixes):
                        tools.remove_tool(name)

    def _group_tool(self, event, name, args):
        state = event.get_extra(STATE_KEY)
        if not state or not self.engine:
            return "当前会话不是启用群记的群聊，不能使用该工具。"
        return self.engine.conversation.native_tool(
            state, name, {k: v for k, v in args.items() if v is not None}
        )

    @filter.llm_tool(name="search_group_history")
    async def search_group_history(
        self, event: AstrMessageEvent, query: str = "", who: str = "", when: str = ""
    ):
        """在本群保存的聊天记录里查以前的原话，附前后文和相关话题摘要。适合“之前谁说过”“上周怎么定的”；原话不代表最终决定。三个参数至少填一个。

        Args:
            query(string): 关键词，多个词用空格分开
            who(string): 可选，只看某个成员的发言（名字）
            when(string): 可选，时间范围，如 今天、昨天、上周、这个月、最近3天、10月3日
        """
        return self._group_tool(
            event, "search_history", {"query": query, "who": who, "when": when}
        )

    @filter.llm_tool(name="get_group_episodes")
    async def get_group_episodes(
        self, event: AstrMessageEvent, query: str = "", who: str = "", when: str = ""
    ):
        """查看本群某段时间聊了什么：几天以内按话题列出，更长的时间给每日、每周或每月摘要。适合“最近群里聊了什么”“上周发生了什么”。

        Args:
            query(string): 可选，话题关键词
            who(string): 可选，只看某个成员参与的话题
            when(string): 可选，时间范围，如 今天、上周、最近7天、上个月；不填为最近三天
        """
        return self._group_tool(
            event, "episodes", {"query": query, "who": who, "when": when}
        )

    @filter.llm_tool(name="get_member_profile")
    async def get_member_profile(self, event: AstrMessageEvent, who: str):
        """查看某个群成员在本群的公开印象：称呼、分工、偏好、发言数和最近活跃时间。

        Args:
            who(string): 成员名字
        """
        return self._group_tool(event, "profile", {"who": who})

    @filter.llm_tool(name="get_topic_timeline")
    async def get_topic_timeline(self, event: AstrMessageEvent, query: str):
        """查某件事在本群的来龙去脉：什么时候定的、后来改过几次、现在以哪条为准，附原话依据。适合“之前怎么定的”“后来改了吗”。

        Args:
            query(string): 事情的名称或关键词
        """
        return self._group_tool(event, "timeline", {"query": query})

    @filter.llm_tool(name="read_group_day")
    async def read_group_day(self, event: AstrMessageEvent, when: str, question: str):
        """重读本群某一天的完整聊天记录来回答具体问题，适合摘要和搜索都找不到的细节。要读整天记录，比较慢，只在需要时用。

        Args:
            when(string): 哪一天，如 今天、昨天、前天、10月3日、2026-10-03
            question(string): 要在那天的记录里找的问题
        """
        state = event.get_extra(STATE_KEY)
        if not state or not self.engine:
            return "当前会话不是启用群记的群聊，不能使用该工具。"
        return await self.engine.conversation.native_tool_async(
            state, "read_day", {"when": when, "question": question}
        )

    @filter.llm_tool(name="read_group_items")
    async def read_group_items(self, event: AstrMessageEvent, query: str = ""):
        """读取本群正式事项的当前状态、待确认内容和变更历史。

        Args:
            query(string): 可选，事项名称关键词；留空返回最近的事项
        """
        return self._group_tool(event, "read_items", {"query": query})

    @filter.llm_tool(name="save_group_drafts")
    async def save_group_drafts(
        self, event: AstrMessageEvent, options: list, sources: list = None
    ):
        """把可以被采用的安排方案保存为草案。草案只是建议，不是正式事项。

        Args:
            options(array[object]): 1到4个方案，每个为 {number:编号1-4, title:稳定的事项名称(不含编号和时间), description:方案说明, fields:{when:带时区的ISO时间或日期, time_raw:原始时间说法, owner:负责人, location:地点, reason:原因, note:备注}, parent_id:修改已有草案时填原草案id}
            sources(array[string]): 可选，作为依据的消息uid
        """
        return self._group_tool(
            event, "save_drafts", {"options": options, "sources": sources}
        )

    @filter.llm_tool(name="submit_group_events")
    async def submit_group_events(self, event: AstrMessageEvent, events: list):
        """提交正式事项事件，每轮最多一次。有人明确拍板采用草案时用 {kind:"confirm", draft_id:草案id}；是否正式记录由权限决定，以返回结果为准。

        Args:
            events(array[object]): 事件列表，每个为 {kind:confirm/propose/change/cancel/complete/correct/participant/note/outdated, draft_id:采用草案时填, title:事项名称, fields:{when,time_raw,owner,location,reason,note}, target:已知事件id, scope:series}
        """
        return self._group_tool(event, "submit_events", {"events": events})

    @filter.on_llm_response()
    async def retry_empty_answer(self, event: AstrMessageEvent, resp):
        """Fix the host agent's final reply before it is sent.

        The host calls this for the main agent's last response. An empty or
        filler-only reply is answered once more: a model that says "let me check"
        and stops without a tool call would otherwise leave the asker with nothing,
        since the filler is dropped. A reply that uses a phrase from
        dialogue.banned_phrases is reworded once; if the rewording still uses one,
        the sentences with it are removed, so a banned phrase never reaches the group.
        """
        state = event.get_extra(STATE_KEY)
        if not state or not self.engine:
            return
        text = getattr(resp, "completion_text", "") or ""
        flags = state["style_flags"]
        if not state["operations"] and (not text.strip() or only_filler(text)):
            flags["empty_retry"] = flags.get("empty_retry", 0) + 1
            answer = await self._answer_again(
                event, state, self.engine.conversation.native_retry_prompt(state), "an empty answer"
            )
            if not answer or only_filler(answer):
                answer = "这次没整理出答案，麻烦再@我问一次～"
            resp.completion_text = text = answer
        phrases = self.engine.config.banned_phrases
        found = banned_in(text, phrases)
        if found:
            flags["banned_rewrite"] = flags.get("banned_rewrite", 0) + 1
            answer = await self._answer_again(
                event,
                state,
                self.engine.conversation.native_rewrite_prompt(state, text, found),
                "a banned phrase",
            )
            if not answer or only_filler(answer) or banned_in(answer, phrases):
                flags["banned_dropped"] = flags.get("banned_dropped", 0) + 1
                answer = drop_banned(answer if answer and not only_filler(answer) else text, phrases)
            resp.completion_text = answer

    async def _answer_again(self, event, state, prompt, reason):
        """One tool-free call on the turn's model and system prompt; '' on failure."""
        try:
            provider = event.get_extra(
                "selected_provider"
            ) or await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            result = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider,
                    system_prompt=state.get("system_prompt", ""),
                    prompt=prompt,
                ),
                timeout=60,
            )
            return (result.completion_text or "").strip()
        except Exception as exc:
            self.logger.warning(
                "Group secretary answering again after %s failed (%s).",
                reason,
                type(exc).__name__,
            )
            return ""

    @filter.on_decorating_result()
    async def finish_group_turn(self, event: AstrMessageEvent):
        """Tidy the host agent's reply, append real write receipts and record it."""
        state = event.get_extra(STATE_KEY)
        result = event.get_result()
        if (
            not state
            or not self.engine
            or result is None
            or not result.chain
            or not result.is_llm_result()
        ):
            return
        plains = [c for c in result.chain if c.__class__.__name__ == "Plain"]
        if not plains:
            return
        flags = []
        raw = "".join(c.text for c in plains)
        if only_filler(raw) and not state["operations"][state["reported_ops"] :]:
            # "我查一下～" before a tool call says nothing; send the answer only.
            flags = state["style_flags"]
            flags["filler_dropped"] = flags.get("filler_dropped", 0) + 1
            result.chain[:] = [c for c in result.chain if c.__class__.__name__ != "Plain"]
            return
        text = tidy_reply(raw, flags)
        plains[0].text = self.engine.conversation.native_finish(state, text, flags)
        result.chain[:] = [
            c for c in result.chain if c.__class__.__name__ != "Plain" or c is plains[0]
        ]

    @staticmethod
    async def _send_chunks(send, text):
        # Avoid a single oversized platform message, and cap runaway output.
        chunks = 0
        for start in range(0, min(len(text), 12000), 1800):
            await send(MessageChain().message(text[start : start + 1800]))
            chunks += 1
        return chunks

    async def send_to_group(self, key, text):
        origin = self.engine.store.get_meta("umo:" + key)
        if not origin:
            raise ValueError("尚未取得该群的发送会话；先从实际平台接收一条消息")

        async def send(chain):
            result = await self.context.send_message(origin, chain)
            if result is False:
                raise RuntimeError("AstrBot 未找到可发送的平台")

        await self._send_chunks(send, text)

    async def ingest_recall(self, platform_id, group_id, native_id):
        """Bridge for adapters that expose recall separately. Never infer a recall from ordinary text."""
        g = self.group_for(platform_id, group_id)
        if g:
            self.engine.ingest(
                Message(
                    g.key,
                    "platform",
                    "",
                    utcnow(),
                    native_id="recall:" + native_id,
                    kind="recall",
                    target_id=native_id,
                )
            )

    async def terminate(self):
        for task in list(self.reply_tasks):
            task.cancel()
        await asyncio.gather(*self.reply_tasks, return_exceptions=True)
        self.reply_tasks.clear()
        if self.web:
            await self.web.close()
            self.web = None
        if self.engine:
            await self.engine.close()
            self.engine = None
