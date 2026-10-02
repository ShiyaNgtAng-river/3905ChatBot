from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .answer import Answerer, render
from .config import Config
from .dialogue import Dialogue
from .extract import Extractor
from .memory import project, rank, validate_candidates
from .providers import AstrBotProvider, OpenAICompatible
from .reading import Reader
from .recall import Recall
from .store import Store, encode
from .types import Actor, Message, utcnow

HELP = """我是群聊 AI 助手。支持：
/问 演示现在怎么定的
/回溯 演示为什么改期
/群报 1  —— 最近1天，可选1–30天
/事项  —— 查看事项ID、状态和待确认事件
/原文 消息ID
/记事 演示 | 确认 | 时间=2026-10-02T15:00:00+08:00;负责人=老张;优先级=5
/记事 演示 | 变更 | 时间=2026-10-06;原因=数据未齐
/记事 演示 | 缺席 | 单次=2026-10-06
/确认 事件ID  —— 按群内确认权限处理
/反馈 回答ID 过时了  —— 也支持找错了、漏重点、太啰嗦、不该插话、有用
/别记我  /恢复记录
/忘掉 事项ID 确认  —— 管理员删除插件内该事项及证据
/秘书状态  /秘书帮助
直接 @ 我或 /聊 内容：讨论、写方案、修改草案、自然确认安排。新方案默认为建议。
撤回、删除只覆盖本插件的数据，不代表平台或其他服务的副本也已删除。"""
FEEDBACK = {"找错了", "过时了", "漏重点", "太啰嗦", "不该插话", "有用"}


class Engine:
    """One writer per database. All asynchronous access runs on its owning event loop."""

    def __init__(
        self, config: Config, context=None, understanding=None, answering=None
    ):
        self.config = config
        self.store = Store(config.db)

        def provider(role):
            if config.mode == "demo":
                return None
            settings = config.models.get(role) or config.models.get("understanding", {})
            if config.mode == "astrbot":
                if context is None:
                    raise ValueError(
                        "astrbot 模式必须在 AstrBot 插件中运行；独立运行请用 openai 或 demo"
                    )
                return AstrBotProvider(
                    context, settings.get("provider_id", ""), self.store.log_usage
                )
            return OpenAICompatible(settings, self.store.log_usage)

        self.extractor = Extractor(
            understanding if understanding is not None else provider("understanding")
        )
        self.extractor.gate = config.gate
        self.answerer = Answerer(
            answering if answering is not None else provider("answering")
        )
        self.locks = {key: asyncio.Lock() for key in config.groups}
        self.wakes = {key: asyncio.Event() for key in config.groups}
        self.tasks: list[asyncio.Task] = []
        self.model_gate = asyncio.Semaphore(2)
        self.sender = None
        self.started = False
        self.writer_file = None
        self.conversation = Dialogue(self)
        self.recall = Recall(self)
        self.reader = Reader(self, provider("reading"), provider("consolidating"))

    def _lock_writer(self):
        self.writer_file = open(str(self.config.db) + ".writer.lock", "a+b")
        try:
            try:
                import fcntl

                fcntl.flock(self.writer_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                import msvcrt

                self.writer_file.seek(0)
                if not self.writer_file.read(1):
                    self.writer_file.write(b"0")
                    self.writer_file.flush()
                self.writer_file.seek(0)
                msvcrt.locking(self.writer_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.writer_file.close()
            self.writer_file = None
            raise RuntimeError(
                "该数据库已有运行中的助手；Web 和 AstrBot 不可分别启动两个写入进程"
            ) from None

    async def start(self, sender=None, maintenance=True):
        if self.started:
            return
        self._lock_writer()
        self.started, self.sender = True, sender
        for key, g in self.config.groups.items():
            if g.enabled and g.data_use_confirmed:
                self.tasks.append(
                    asyncio.create_task(self._worker(key), name="secretary:" + key)
                )
                self.wakes[key].set()
        if maintenance:
            self.tasks.append(
                asyncio.create_task(self._maintenance(), name="secretary:maintenance")
            )
            if self.config.reading:
                self.tasks.append(
                    asyncio.create_task(self._memory_loop(), name="secretary:memory")
                )

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.started = False
        self.store.close()
        if self.writer_file:
            self.writer_file.close()
            self.writer_file = None

    def ingest(self, message: Message, route="background"):
        self.config.group(message.group)
        record = self.store.put(message, route=route)
        if record:
            self.wakes[message.group].set()
        return record

    async def _worker(self, key):
        wake = self.wakes[key]
        while True:
            await wake.wait()
            wake.clear()
            while self.store.pending(key, self.config.max_attempts):
                async with self.locks[key]:
                    m = self.store.pending(key, self.config.max_attempts)
                    if m:
                        await self._process(key, m)
                await asyncio.sleep(0)

    async def _process(self, key, m):
        g = self.config.group(key)
        try:
            if m["kind"] == "recall":
                self.store.recall(key, m["target_id"], m["sender"])
                self.store.commit(m, [])
                return
            if m["kind"] == "edit":
                self.store.supersede_revision(key, m["native_id"], m["uid"])
            states = self.states(key, m["at"])
            async with self.model_gate:
                candidates, status = await self.extractor.extract(
                    self.store, g, m, states
                )
            if m["attachments"] != "[]":
                status = "unparsed_attachment"
            events = validate_candidates(
                self.store, g, m, candidates, self.extractor.model
            )
            self.store.commit(m, events, status)
            if self.sender and g.proactive:
                await self._conflict(g, m, states, events)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Payloads are never copied into error logs.
            self.store.failure(m["uid"], type(exc).__name__, self.config.max_attempts)
            await asyncio.sleep(min(0.1 * (m["attempts"] + 1), 0.5))

    async def flush(self, key, upto=None, timeout=None):
        self.config.group(key)
        if not self.started:
            raise RuntimeError("引擎尚未启动")
        if upto is None:
            r = self.store.one(
                "SELECT MAX(seq) AS n FROM messages WHERE group_key=?", (key,)
            )
            upto = r["n"] or 0
        self.wakes[key].set()
        end = time.monotonic() + (
            self.config.query_wait if timeout is None else timeout
        )
        while self.store.one(
            "SELECT 1 FROM messages WHERE group_key=? AND seq<=? AND erased=0 AND status IN ('pending','retry') LIMIT 1",
            (key, upto),
        ):
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.02)
        return True

    def states(self, key, before=None):
        return project(self.store.events(key, before))

    def status(self, actor, key):
        actor.require(key)
        g = self.config.group(key)
        counts = self.store.rows(
            "SELECT status,COUNT(*) AS count FROM messages WHERE group_key=? AND erased=0 GROUP BY status",
            (key,),
        )
        usage = self.store.one(
            "SELECT COUNT(*) AS calls,SUM(prompt_tokens) AS prompt_tokens,SUM(completion_tokens) AS completion_tokens,SUM(seconds) AS seconds FROM usage WHERE group_key=?",
            (key,),
        )
        return {
            "group": key,
            "mode": self.config.mode,
            "processing_location": g.processing_location,
            "counts": counts,
            "usage": usage,
            "items": len(self.states(key)),
            "memory": self.reader.stats(key),
            "proactive": g.proactive,
            "retention_days": g.retention_days,
        }

    def _sources(self, key, claims):
        sources = {}
        for claim in claims:
            for sid in claim["sources"]:
                source = self.store.message(key, sid)
                if not source:
                    return None
                sources[sid] = source
        return sources

    def record_answer(
        self, actor, key, question, output, sources, item_ids, mode, states=()
    ):
        aid = uuid.uuid4().hex[:12]
        if not self.store.opted_out(key, actor.user):
            trace = {
                "understanding_model": self.extractor.model,
                "answer_model": getattr(self.answerer.provider, "model", "template"),
                "prompt_version": "answer-1.0",
                "item_event_ids": {
                    s["id"]: [ev["id"] for ev in s["history"]]
                    for s in states
                    if s["id"] in item_ids
                },
            }
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO answers(id,group_key,actor,at,question,output,sources,item_ids,mode,trace) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        key,
                        actor.user,
                        utcnow(),
                        question,
                        output,
                        encode(list(sources)),
                        encode(list(item_ids)),
                        mode,
                        encode(trace),
                    ),
                )
        return aid

    async def dialogue(
        self, actor, key, text, message=None, request_id=None, reply_to=""
    ):
        if not self.config.dialogue_enabled:
            return await self.query(
                actor, key, text, exclude_uid=message.uid if message else ""
            )
        return await self.conversation.run(
            actor, key, text, message, request_id, reply_to
        )

    def uses_dialogue(self, text):
        return self.config.dialogue_enabled and (
            text.startswith("/聊 ")
            or (
                not text.startswith("/")
                and text not in {"这条记住", "这个已经过时了", "已经过时了", "别记我"}
            )
        )

    async def query(
        self, actor: Actor, key, question, report_days=None, as_of=None, exclude_uid=""
    ):
        actor.require(key)
        g = self.config.group(key)
        if not question.strip() or len(question) > 4000:
            raise ValueError("问题应为1–4000字")
        before = as_of or utcnow()
        upto = (
            self.store.one(
                "SELECT MAX(seq) AS n FROM messages WHERE group_key=? AND at<=?",
                (key, before),
            )["n"]
            or 0
        )
        ready = await self.flush(key, upto)
        states = self.states(key, before)
        search_question = question
        if (
            report_days is None
            and not rank(question, states, lambda s: s["title"])
            and any(
                k in question
                for k in ("为什么", "那现在", "后来呢", "谁负责", "几点", "在哪里")
            )
        ):
            previous = self.store.one(
                "SELECT item_ids FROM answers WHERE group_key=? AND actor=? ORDER BY at DESC LIMIT 1",
                (key, actor.user),
            )
            ids = json.loads(previous["item_ids"]) if previous else []
            titles = [s["title"] for s in states if s["id"] in ids]
            if titles:
                search_question = "、".join(titles) + " " + question
        if report_days is not None:
            if not 1 <= report_days <= 30:
                raise ValueError("群报范围为1–30天")
            since = (
                datetime.fromisoformat(before) - timedelta(days=report_days)
            ).isoformat()
            states = [s for s in states if s["last_update"] >= since]
        revision = self.store.get_meta("revocation:" + key)
        async with self.model_gate:
            opening, claims, mode = await self.answerer.work(
                search_question, states, g, report_days is not None
            )
        # Discard generated selection after any concurrent removal. Do not leak a stale snapshot.
        if revision != self.store.get_meta("revocation:" + key):
            opening, claims, mode = await Answerer().work(
                search_question, self.states(key, before), g, report_days is not None
            )
        sources = self._sources(key, claims)
        if sources is None:
            claims, sources, mode = [], {}, "evidence_changed"
        if not claims and report_days is None:
            found = self.store.search(key, question, before, 4, exclude_uid=exclude_uid)
            claims = [
                {
                    "id": m["uid"],
                    "item_id": "",
                    "text": "找到原话，尚不能据此确认最新状态：" + m["text"][:180],
                    "sources": [m["uid"]],
                }
                for m in found
            ]
            sources = {m["uid"]: m for m in found}
            mode = "raw_keyword_fallback"
        output = render(opening, claims, sources, g.timezone)
        gaps = self.store.one(
            "SELECT COUNT(*) AS n FROM messages WHERE group_key=? AND at<=? AND erased=0 AND status IN ('failed','pending','retry','dialogue','unparsed_attachment','unstructured','gated')",
            (key, before),
        )["n"]
        if not ready or gaps:
            output += f"\n\n覆盖提示：截至提问时有 {gaps} 条消息未形成可靠的结构化记录（含普通闲聊、附件或失败记录），本答案不代表已理解全部讨论。"
        aid = self.record_answer(
            actor,
            key,
            question,
            output,
            list(sources),
            {c["item_id"] for c in claims if c["item_id"]},
            mode,
            states,
        )
        result = {
            "id": aid,
            "text": output,
            "sources": list(sources.values()),
            "claims": claims,
            "mode": mode,
            "ready": ready,
        }
        return result

    def feedback(self, actor, key, answer_id, kind, note=""):
        actor.require(key)
        self.config.group(key)
        if kind not in FEEDBACK or len(note) > 500:
            raise ValueError("反馈类型或长度无效")
        if not self.store.one(
            "SELECT 1 FROM answers WHERE group_key=? AND id=?", (key, answer_id)
        ):
            raise ValueError("回答不存在或已按数据规则清理")
        if self.store.opted_out(key, actor.user):
            return "你已退出记录，本次反馈不保存。"
        with self.store.tx() as db:
            db.execute(
                "INSERT INTO feedback(group_key,actor,answer_id,kind,note,at) VALUES(?,?,?,?,?,?)",
                (key, actor.user, answer_id, kind, note, utcnow()),
            )
        return "反馈已记录，会用于人工复盘；不会直接改变模型权重。"

    def _match_item(self, key, prefix):
        found = [s for s in self.states(key) if s["id"].startswith(prefix)]
        if len(found) != 1:
            raise ValueError("事项 ID 不存在或不唯一；先用 /事项 查看")
        return found[0]

    async def command(self, actor, key, text, message=None):
        actor.require(key)
        g = self.config.group(key)
        text = text.strip()
        if text in {"/秘书帮助", "/help"}:
            return {"text": HELP}
        if text in {"/别记我", "别记我", "/恢复记录"}:
            enabled = text != "/恢复记录"
            self.store.optout(key, actor.user, enabled)
            return {
                "text": (
                    "已停止记录你在本群后续发送的消息，并清理本插件内你发送过的内容及其派生记录；不涵盖其他人提到你的内容或外部服务副本。"
                    if enabled
                    else "已恢复记录后续消息；此前删除的内容不会自动恢复。"
                )
            }
        if text.startswith("/忘掉 "):
            actor.require(key, admin=True)
            parts = text.split()
            if len(parts) != 3 or parts[2] != "确认":
                return {
                    "text": "请用 /忘掉 事项ID 确认。该事项的证据原文和派生记录会被删除；共用同一证据的其他事项也可能受影响。"
                }
            item = self._match_item(key, parts[1])
            self.store.forget(key, item["id"], actor.user)
            return {
                "text": "已删除本插件内该事项、证据及相关派生内容，并阻止相同证据重放恢复。"
            }
        if text == "/秘书状态":
            return {
                "text": json.dumps(
                    self.status(actor, key), ensure_ascii=False, indent=2
                )
            }
        if text.startswith("/反馈 "):
            parts = text.split(maxsplit=3)
            if len(parts) < 3:
                raise ValueError("格式：/反馈 回答ID 类型 [说明]")
            return {
                "text": self.feedback(
                    actor, key, parts[1], parts[2], parts[3] if len(parts) > 3 else ""
                )
            }
        if text.startswith("/原文 "):
            sid = text.split(maxsplit=1)[1]
            m = self.store.message(key, sid)
            if not m:
                matches = self.store.rows(
                    "SELECT * FROM messages WHERE group_key=? AND uid LIKE ? AND erased=0 AND kind!='recall' LIMIT 2",
                    (key, sid + "%"),
                )
                m = matches[0] if len(matches) == 1 else None
            return {
                "text": f"{m['at']} {m['name'] or m['sender']}：\n{m['text']}"
                if m
                else "没有可访问的原文；可能已撤回或清理。"
            }
        if text == "/事项":
            await self.flush(key)
            states = self.states(key)
            lines = [
                f"{s['id'][:12]}｜{s['title']}｜{s['status']}｜待确认 {len(s['pending'])}"
                for s in states
            ]
            for s in states:
                lines.extend(
                    f"  事件 {e['id'][:12]}：{e['kind']} {encode(e['payload'])}"
                    for e in s["pending"][-3:]
                )
            return {"text": "\n".join(lines) or "还没有事项。"}
        if text.startswith(("/记事 ", "/确认 ")) or text in {
            "这条记住",
            "/记住",
            "这个已经过时了",
            "已经过时了",
        }:
            if self.store.opted_out(key, actor.user):
                return {"text": "你已退出记录；如需新增事项，请先 /恢复记录。"}
            if message is None:
                message = Message(
                    key,
                    actor.user,
                    text,
                    utcnow(),
                    native_id="command:" + uuid.uuid4().hex,
                    name=actor.user,
                )
                self.ingest(message)
            await self.flush(key)
            row = self.store.one("SELECT * FROM messages WHERE uid=?", (message.uid,))
            events = [
                e
                for e in self.store.events(key)
                if e["message_uid"] == message.uid and e["valid"]
            ]
            if not events:
                return {
                    "text": "未能写入。请检查命令格式、目标事件和权限。"
                    + ("错误类型：" + row["error"] if row and row["error"] else "")
                }
            return {
                "text": "\n".join(
                    f"{e['title']}：{'已记录' if e['accepted'] else '待有权限的成员确认'}（事件 {e['id'][:12]}）。"
                    for e in events
                )
            }
        if text.startswith("/群报"):
            parts = text.split()
            days = int(parts[1]) if len(parts) > 1 else 1
            return await self.query(actor, key, "群报", report_days=days)
        if self.uses_dialogue(text):
            return await self.dialogue(
                actor,
                key,
                text[3:] if text.startswith("/聊 ") else text,
                message=message,
                request_id=uuid.uuid4().hex,
            )
        if text.startswith("/聊 ") or text in {
            "你好",
            "您好",
            "早上好",
            "晚上好",
            "谢谢",
            "谢谢你",
            "辛苦了",
            "你是谁",
            "你能做什么",
        }:
            revision = self.store.get_meta("revocation:" + key)
            recent = self.store.recent(key, limit=6)
            async with self.model_gate:
                out, mode = await self.answerer.social(
                    text[3:] if text.startswith("/聊 ") else text, recent, g
                )
            if revision != self.store.get_meta("revocation:" + key):
                out = "相关记录刚发生变化，请再说一次。"
                recent = []
            aid = self.record_answer(
                actor, key, text, out, [m["uid"] for m in recent], [], mode
            )
            return {"id": aid, "text": out, "mode": mode}
        if text.startswith("/回溯 "):
            return await self.query(
                actor,
                key,
                "历史回溯 " + text[4:],
                exclude_uid=message.uid if message else "",
            )
        if text.startswith("/问 "):
            text = text[3:]
        elif text.startswith("/"):
            return {"text": "未识别的命令。输入 /秘书帮助 查看用法。"}
        return await self.query(
            actor, key, text, exclude_uid=message.uid if message else ""
        )

    async def _conflict(self, g, m, before, events):
        last = float(self.store.get_meta("last_reminder:" + g.key, "0"))
        if time.time() - last < g.cooldown_seconds:
            return
        lookup = {s["id"]: s for s in before}
        for e in events:
            old = lookup.get(e["item_id"])
            if e["accepted"] or not old or old["status"] != "confirmed":
                continue
            newtime = e["payload"].get("when")
            oldtime = old["fields"].get("when")
            if newtime and oldtime and newtime != oldtime:
                refs = list(
                    dict.fromkeys(e["sources"] + old["field_sources"].get("when", []))
                )
                if any(not self.store.message(g.key, sid) for sid in refs):
                    continue
                text = f"{old['title']} 的已确认时间是 {oldtime}，刚才提到了 {newtime}。是有新变化吗？我先记为待确认。"
                text += "\n依据消息：" + ", ".join(
                    self.store.message(g.key, sid)["native_id"] or sid[:12]
                    for sid in refs
                )
                try:
                    await self.sender(g.key, text)
                    self.record_answer(
                        Actor("system", [g.key]),
                        g.key,
                        "[主动冲突提醒]",
                        text,
                        refs,
                        [e["item_id"]],
                        "conflict_template",
                        before,
                    )
                    self.store.set_meta("last_reminder:" + g.key, time.time())
                except Exception:
                    self.store.set_meta("send_error:" + g.key, "reminder_failed")
                break

    async def _build_memory(self, key):
        """Summarise at most one due episode; back off for 10 minutes after a failure."""
        retry = self.store.get_meta("episode_retry:" + key)
        if retry and retry > utcnow():
            return
        try:
            await self.recall.build(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Payloads are never copied into error logs.
            self.store.log_usage(
                key, "episode_failure", "runtime", {}, 0, type(exc).__name__
            )
            self.store.set_meta(
                "episode_retry:" + key,
                (datetime.now(ZoneInfo("UTC")) + timedelta(minutes=10)).isoformat(),
            )

    async def _background(self, key, name, job):
        """Run one memory job; after a failure the job backs off for 10 minutes.

        Returns:
            True when the job did some work.
        """
        retry = self.store.get_meta(name + "_retry:" + key)
        if retry and retry > utcnow():
            return False
        try:
            return bool(await job(key))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Payloads are never copied into error logs.
            self.store.log_usage(
                key, name + "_failure", "runtime", {}, 0, type(exc).__name__
            )
            self.store.set_meta(
                name + "_retry:" + key,
                (datetime.now(ZoneInfo("UTC")) + timedelta(minutes=10)).isoformat(),
            )
            return False

    async def _memory_loop(self):
        """Read, consolidate, distill and roll up each group's chat; one model job per tick."""
        await asyncio.sleep(self.config.start_delay)
        while True:
            for key, g in self.config.groups.items():
                if not (g.enabled and g.data_use_confirmed):
                    continue
                for name, job in (
                    ("read", self.reader.read),
                    ("consolidate", self.reader.consolidate),
                    ("distill", self.reader.distill),
                    ("rollup", self.reader.rollup),
                ):
                    if await self._background(key, name, job):
                        break
            await asyncio.sleep(30)

    async def _maintenance(self):
        # Host model providers finish loading after plugins start; wait before summarising.
        started = time.monotonic()
        while True:
            for key, g in self.config.groups.items():
                if not (g.enabled and g.data_use_confirmed):
                    continue
                self.store.expire(key, g.retention_days)
                if time.monotonic() - started > 60:
                    await self._build_memory(key)
                now = datetime.now(ZoneInfo(g.timezone))
                marker = now.date().isoformat()
                if (
                    self.sender
                    and g.report_time
                    and now.strftime("%H:%M") == g.report_time[:5]
                    and self.store.get_meta("report:" + key) != marker
                ):
                    try:
                        answer = await self.query(
                            Actor("system", [key]), key, "群报", report_days=1
                        )
                        await self.sender(key, answer["text"])
                        self.store.set_meta("report:" + key, marker)
                    except Exception:
                        self.store.set_meta(
                            "send_error:" + key, "scheduled_report_failed"
                        )
            await asyncio.sleep(30)
