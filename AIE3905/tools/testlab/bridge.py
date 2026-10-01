"""Test-only observability plugin. Installed only inside isolated test runs."""

import asyncio
import datetime as stdlib_datetime
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from astrbot.api.event import filter
from astrbot.api.star import Star
from astrbot.core.pipeline.scheduler import PipelineScheduler


class VirtualClock:
    """Replay time for realtime runs: flows at real speed from the last anchor.

    The driver moves the anchor forward over idle gaps; while the system works,
    virtual time passes exactly as fast as real time, so messages, background
    memory jobs and answers interleave the way they would in a live group.
    """

    anchor = None  # (aware UTC virtual time, monotonic seconds)

    @classmethod
    def now(cls):
        if cls.anchor is None:
            return datetime.now(timezone.utc)
        virtual, real = cls.anchor
        return virtual + timedelta(seconds=time.monotonic() - real)


class VirtualDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        t = VirtualClock.now()
        return t.astimezone(tz) if tz else t.astimezone().replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return VirtualClock.now().replace(tzinfo=None)


# AstrBot stamps each incoming message with time.time() at receipt, not the
# OneBot event's own time, so the adapter's clock must follow the replay too.
RECEIPT_CLOCK_MODULES = {
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter",
    "astrbot.core.platform.astrbot_message",
}


def install_virtual_clock():
    """Point the plugin's, the host agent's and the adapter's clocks at VirtualClock.

    Only module globals are rebound, inside this isolated test process. Modules
    that imported the datetime class get VirtualDatetime; modules that imported
    the datetime module get a copy of it whose datetime is VirtualDatetime.
    """
    shim = types.ModuleType("datetime")
    shim.__dict__.update(stdlib_datetime.__dict__)
    shim.datetime = VirtualDatetime
    clock = types.ModuleType("time")
    clock.__dict__.update(time.__dict__)
    clock.time = lambda: VirtualClock.now().timestamp()
    patched = []
    for name in RECEIPT_CLOCK_MODULES:
        module = sys.modules.get(name)
        if module is not None and getattr(module, "time", None) is time:
            module.time = clock
            patched.append(name)
    for name, module in list(sys.modules.items()):
        if not module or not (
            "astrbot_plugin_groupsecretary" in name
            or name == "astrbot.core.astr_main_agent"
        ):
            continue
        current = getattr(module, "datetime", None)
        if current is stdlib_datetime.datetime:
            module.datetime = VirtualDatetime
            patched.append(name)
        elif current is stdlib_datetime:
            module.datetime = shim
            patched.append(name)
    return patched


class TestLabBridge(Star):
    async def initialize(self):
        self.root = Path(os.environ["GROUPBOT_TEST_RUN"]).resolve()
        if Path(os.environ["ASTRBOT_ROOT"]).resolve() != self.root / "root":
            raise RuntimeError("Test bridge requires an isolated ASTRBOT_ROOT")
        self.done, self.calls, self.traces = {}, [], []
        self.denied = 0
        self.limit = int(os.environ["GROUPBOT_TEST_CALL_LIMIT"])
        self.virtual = os.environ.get("GROUPBOT_TEST_VIRTUAL_CLOCK") == "1"
        self.pipelines, self.memory_task, self.memory_jobs = 0, None, []
        self.original_execute = PipelineScheduler.execute
        owner = self

        async def execute(scheduler, event):
            mid = str(event.message_obj.message_id)
            started = time.monotonic()
            error = None
            owner.pipelines += 1
            try:
                await owner.original_execute(scheduler, event)
                plugin = owner.plugin()
                # Plugin commands finish in a separate task after the host pipeline.
                if plugin.reply_tasks:
                    await asyncio.gather(*list(plugin.reply_tasks))
                if not await plugin.engine.flush("sandbox", timeout=120):
                    error = "ExtractionTimeout"
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                owner.pipelines -= 1
                owner.done[mid] = {
                    "seconds": round(time.monotonic() - started, 3),
                    "error": error,
                }

        PipelineScheduler.execute = execute

    def plugin(self):
        return next(
            s.star_cls
            for s in self.context.get_all_stars()
            if s.name == "astrbot_plugin_groupsecretary" and s.star_cls
        )

    @filter.on_astrbot_loaded()
    async def loaded(self):
        persona = json.loads((self.root / "persona.json").read_text())
        if not any(
            p.persona_id == persona["persona_id"]
            for p in await self.context.persona_manager.get_all_personas()
        ):
            await self.context.persona_manager.create_persona(**persona)
        for provider in self.context.get_all_providers():
            original = provider.text_chat

            async def measured(*args, _original=original, _provider=provider, **kwargs):
                if len(self.calls) >= self.limit:
                    self.denied += 1
                    raise RuntimeError("TestModelCallLimit")
                row = {
                    "model": _provider.model_name,
                    "at": time.time(),
                    "status": "running",
                }
                self.calls.append(row)
                started = time.monotonic()
                try:
                    result = await _original(*args, **kwargs)
                    u = getattr(result, "usage", None)
                    row.update(
                        status="done",
                        usage={
                            k: getattr(u, k, None)
                            for k in ["input_other", "input_cached", "output"]
                        },
                    )
                    return result
                except BaseException as exc:
                    row.update(status="failed", error=type(exc).__name__)
                    raise
                finally:
                    row["seconds"] = round(time.monotonic() - started, 3)

            provider.text_chat = measured

        @web.middleware
        async def auth(request, handler):
            if (
                request.headers.get("Authorization")
                != "Bearer " + os.environ["GROUPBOT_TEST_BRIDGE_TOKEN"]
            ):
                raise web.HTTPUnauthorized()
            return await handler(request)

        app = web.Application(middlewares=[auth])
        app.router.add_get("/done/{mid}", self.completed)
        app.router.add_post("/begin/{mid}", self.begin)
        app.router.add_get("/snapshot", self.snapshot)
        app.router.add_get("/message/{mid}", self.message)
        app.router.add_post("/memory", self.memory)
        app.router.add_post("/clock", self.clock)
        app.router.add_post("/tick", self.tick)
        app.router.add_get("/busy", self.busy)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        (self.root / "ready.json").write_text(json.dumps({"port": port}))

    async def completed(self, request):
        return web.json_response({"done": self.done.get(request.match_info["mid"])})

    async def begin(self, request):
        self.done.pop(request.match_info["mid"], None)
        return web.json_response({"ok": True})

    async def snapshot(self, request):
        e = self.plugin().engine
        result = {
            "tables": {},
            "calls": self.calls,
            "tools": self.traces,
            "budget": {"limit": self.limit, "denied": self.denied},
        }
        for table in [
            "messages",
            "answers",
            "drafts",
            "day_views",
            "anchor_topics",
            "anchor_facts",
            "digests",
            "lexicon",
            "profiles",
            "usage",
        ]:
            result["tables"][table] = e.store.rows(
                f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 500"
            )
        result["items"] = e.states("sandbox")
        result["counts"] = {
            t: e.store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
            for t in result["tables"]
        }
        return web.json_response(result)

    async def message(self, request):
        row = self.plugin().engine.store.one(
            "SELECT erased FROM messages WHERE group_key=? AND native_id=?",
            ("sandbox", request.match_info["mid"]),
        )
        return web.json_response({"message": row})

    async def memory(self, request):
        p = await request.json()
        day = datetime.strptime(p["day"], "%Y-%m-%d").date().isoformat()
        e = self.plugin().engine
        await e.flush("sandbox", timeout=120)
        if p["action"] == "read":
            result = await e.reader.read_pass("sandbox", day)
        elif p["action"] == "consolidate":
            result = await e.reader.consolidate_day("sandbox", day)
        else:
            raise web.HTTPBadRequest(text="Unknown memory action")
        return web.json_response({"result": result})

    async def clock(self, request):
        """Move the virtual clock; realtime runs only."""
        if not self.virtual:
            raise web.HTTPBadRequest(text="Virtual clock is off for this run")
        p = await request.json()
        at = datetime.fromisoformat(p["now"])
        if at.tzinfo is None:
            raise web.HTTPBadRequest(text="now must include a timezone")
        if not getattr(self, "patched", None):
            self.patched = install_virtual_clock()
        VirtualClock.anchor = (at.astimezone(timezone.utc), time.monotonic())
        return web.json_response({"now": VirtualClock.now().isoformat(), "patched": len(self.patched)})

    async def tick(self, request):
        """One iteration of the plugin's own memory loop, on the virtual clock.

        The production loop tries reading, consolidation and rollup in that order
        and stops after the first job that did work; this calls the same methods
        through the same backoff wrapper. The job runs in the background so the
        replay keeps flowing while the model works.
        """
        if not self.virtual:
            raise web.HTTPBadRequest(text="Virtual clock is off for this run")
        if self.memory_task and not self.memory_task.done():
            return web.json_response({"running": True})
        e = self.plugin().engine

        async def loop_once():
            started, at = time.monotonic(), VirtualClock.now().isoformat()
            for name, job in (
                ("read", e.reader.read),
                ("consolidate", e.reader.consolidate),
                ("rollup", e.reader.rollup),
            ):
                if await e._background("sandbox", name, job):
                    self.memory_jobs.append(
                        {"job": name, "virtual_at": at, "seconds": round(time.monotonic() - started, 3)}
                    )
                    return name
            return None

        self.memory_task = asyncio.create_task(loop_once())
        await asyncio.sleep(0)
        return web.json_response({"running": False})

    async def busy(self, request):
        memory = bool(self.memory_task and not self.memory_task.done())
        calls = sum(c["status"] == "running" for c in self.calls)
        return web.json_response(
            {
                "busy": bool(self.pipelines or calls or memory),
                "pipelines": self.pipelines,
                "calls": calls,
                "memory": memory,
                "memory_jobs": self.memory_jobs,
                "now": VirtualClock.now().isoformat(),
            }
        )

    @filter.on_using_llm_tool()
    async def tool_start(self, event, tool, tool_args):
        self.traces.append(
            {
                "at": time.time(),
                "message_id": str(event.message_obj.message_id),
                "tool": tool.name,
                "arguments": tool_args,
                "phase": "start",
            }
        )

    @filter.on_llm_tool_respond()
    async def tool_end(self, event, tool, tool_args, tool_result):
        self.traces.append(
            {
                "at": time.time(),
                "message_id": str(event.message_obj.message_id),
                "tool": tool.name,
                "result": str(tool_result)[:6000],
                "phase": "end",
            }
        )

    async def terminate(self):
        PipelineScheduler.execute = self.original_execute
        if hasattr(self, "runner"):
            await self.runner.cleanup()
