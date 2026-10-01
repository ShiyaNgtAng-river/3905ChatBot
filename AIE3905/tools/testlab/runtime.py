"""Isolated AstrBot workers and a virtual OneBot transport for the test workbench."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import statistics
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BOT, GROUP, OWNER = 10000, 123456, 90001
TZ = ZoneInfo("Asia/Shanghai")


def save(path, obj):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def validate_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20000:
        raise ValueError("消息必须是包含1–20000条记录的数组")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("每条消息必须是JSON对象")
        if row.get("kind", "message") not in {"message", "recall"}:
            raise ValueError("当前支持 message 和 recall 事件")
        if not isinstance(row.get("text", ""), str) or len(row.get("text", "")) > 20000:
            raise ValueError("消息文本最多20000字符")
        if not isinstance(row.get("sender", "owner"), (str, int)):
            raise ValueError("sender 必须是成员名或编号")
        if "at" in row and not isinstance(row["at"], bool):
            raise ValueError("at 表示是否@机器人，必须为布尔值；日期请用 timestamp")
        if row.get("timestamp"):
            dt = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timestamp 必须带时区")
        if row.get("kind") == "recall" and not row.get("target_id"):
            raise ValueError("撤回需要 target_id")
        mentions = row.get("mentions", [])
        if not isinstance(mentions, list) or not all(
            isinstance(m, dict) and isinstance(m.get("sender"), (str, int)) for m in mentions
        ):
            raise ValueError("mentions 必须是 {sender, name} 对象数组")
        if row.get("attachments"):
            raise ValueError("此版本回放文字、引用和撤回；附件请转为明确的文字占位")
        if "expect" in row:
            exp = row["expect"]
            if not isinstance(exp, dict) or set(exp) - {
                "reply_count",
                "contains",
                "not_contains",
            }:
                raise ValueError("expect 仅支持 reply_count、contains、not_contains")
            if "reply_count" in exp and (
                type(exp["reply_count"]) is not int or exp["reply_count"] < 0
            ):
                raise ValueError("reply_count 必须是非负整数")
            for k in ["contains", "not_contains"]:
                if k in exp and (
                    not isinstance(exp[k], list)
                    or not all(isinstance(x, str) for x in exp[k])
                ):
                    raise ValueError(k + " 必须是字符串数组")
    return rows


def normalize_dataset(rows):
    """Accept existing messages.jsonl, whose `at` is a timestamp, as well as test steps."""
    normalized = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("测试集必须是消息对象数组")
    for r in rows:
        x = dict(r)
        if x.get("kind") == "create":
            x["kind"] = "message"
        if isinstance(x.get("at"), str):
            x["timestamp"] = x.pop("at")
        if "mention" in x:
            x["at"] = x.pop("mention")
        normalized.append(x)
    return validate_rows(normalized)


def host_models(host):
    """Read only selected model settings. Secrets are passed in child environments."""
    cfg = json.loads((host / "data/cmd_config.json").read_text(encoding="utf-8-sig"))
    main = cfg["agent_runner"]["config"]["model"]["provider_id"]
    business = json.loads((ROOT / "config/qq.json").read_text())
    roles = {
        "main": main,
        "fast": business["models"]["understanding"]["provider_id"],
        "strong": business["models"].get("consolidating", {}).get("provider_id", main),
        "reply_fast": business.get("dialogue", {}).get("fast_provider") or main,
        "reply_deep": business.get("dialogue", {}).get("deep_provider") or main,
    }
    models, env = [], {}
    for role, provider_id in roles.items():
        p = next(
            p
            for p in cfg["provider"]
            if p["id"] == provider_id and p.get("enable", True)
        )
        source = next(
            (
                s
                for s in cfg.get("provider_sources", [])
                if s["id"] == p.get("provider_source_id")
            ),
            {},
        )
        merged = {**source, **p}
        keys = merged.get("key", [])
        key = next((k for k in keys if k), "") if isinstance(keys, list) else keys
        if key.startswith("$"):
            key = os.environ.get(key[1:], "")
        if not key:
            raise ValueError(f"{provider_id} 尚未配置 API Key")
        variable = "GROUPBOT_TEST_KEY_" + role.upper()
        env[variable] = key
        models.append(
            {
                "id": "test-" + role,
                "provider": "openai",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "enable": True,
                "proxy": "",
                "api_base": merged["api_base"],
                "model": merged["model"],
                "key": ["$" + variable],
                "custom_headers": {},
                "custom_extra_body": merged.get("custom_extra_body", {}),
                "timeout": 180,
            }
        )
    return models, env


ROLES = ["main", "fast", "strong", "reply_fast", "reply_deep"]


def env_models(settings):
    """Models for one OpenAI-compatible endpoint, with the key from an environment variable.

    settings: {"base_url", "key_env", "models": {role: model_id}}; roles left out
    use models["main"]. Like host mode, the key travels only in child environments.
    """
    key = os.environ.get(settings["key_env"], "")
    if not key:
        raise ValueError(f"未找到环境变量 {settings['key_env']}；请在运行环境中设置模型 Key")
    models, env = [], {}
    for role in ROLES:
        variable = "GROUPBOT_TEST_KEY_" + role.upper()
        env[variable] = key
        models.append(
            {
                "id": "test-" + role,
                "provider": "openai",
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "enable": True,
                "proxy": "",
                "api_base": settings["base_url"],
                "model": settings["models"].get(role) or settings["models"]["main"],
                "key": ["$" + variable],
                "custom_headers": {},
                "custom_extra_body": settings.get("extra_body", {}),
                "timeout": 180,
            }
        )
    return models, env


class Worker:
    def __init__(self, manager, spec):
        self.manager, self.spec = manager, spec
        self.id = spec["id"]
        self.path = manager.home / self.id
        self.path.mkdir(mode=0o700)
        self.root = self.path / "root"
        self.state = "queued"
        self.timeline, self.snapshot, self.checks = [], {}, []
        self.error, self.task, self.active_task = None, None, None
        self.procs, self.files = [], []
        self.ws, self.listener = None, None
        self.ids, self.messages, self.names = {}, {}, {OWNER: "老张"}
        self.next_id = 1000
        self.current, self.bridge, self.progress = None, "", 0
        self.started, self.finished = None, None
        self.token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.session = None
        self.persist()

    def info(self):
        calls = self.snapshot.get("calls", [])
        return {
            "id": self.id,
            "title": self.spec["title"],
            "mode": self.spec["mode"],
            "state": self.state,
            "progress": self.progress,
            "total": len(self.spec.get("rows", [])),
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
            "calls": len(calls),
            "model_limit": self.spec["model_limit"],
            "checks_passed": sum(c["pass"] for c in self.checks),
            "checks_total": len(self.checks),
        }

    def report(self):
        calls = self.snapshot.get("calls", [])
        latencies = sorted(x["seconds"] for x in self.timeline if "seconds" in x)
        used = [c["usage"] for c in calls if c.get("usage")]
        uncached = sum(u.get("input_other") or 0 for u in used)
        cached = sum(u.get("input_cached") or 0 for u in used)
        manifest = self.path / "source-manifest.json"
        return {
            **self.info(),
            "timeline": self.timeline,
            "snapshot": self.snapshot,
            "checks": self.checks,
            "metrics": {
                "turn_seconds_p50": round(statistics.median(latencies), 3)
                if latencies
                else None,
                "turn_seconds_p95": latencies[
                    min(len(latencies) - 1, int((len(latencies) - 1) * 0.95 + 0.999))
                ]
                if latencies
                else None,
                "input_uncached": uncached,
                "input_cached": cached,
                "output_tokens": sum(u.get("output") or 0 for u in used),
                "cache_hit_ratio": round(cached / (uncached + cached), 4)
                if uncached + cached
                else None,
                "failed_calls": sum(c["status"] == "failed" for c in calls),
            },
            "source_manifest": json.loads(manifest.read_text())
            if manifest.exists()
            else {},
            "environment": {
                "model_mode": self.manager.model_mode,
                "source": str(self.manager.host),
                "transport": "local virtual OneBot; no QQ connection",
                "memory_scheduler": "manual",
                "snapshot_row_limit": 500,
                "model_calls": "provider calls, excluding internal transport retries",
            },
        }

    def persist(self):
        save(self.path / "report.json", self.report())

    async def api(self, method, path, data=None):
        async with self.session.request(
            method,
            self.bridge + path,
            json=data,
            headers={"Authorization": "Bearer " + self.token},
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TestBridgeHTTP{resp.status}")
            return await resp.json()

    async def prepare(self):
        data = self.root / "data"
        (data / "plugins").mkdir(parents=True)
        shutil.copytree(
            ROOT / "plugin",
            data / "plugins/astrbot_plugin_groupsecretary",
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "tests", "docs", "examples"
            ),
        )
        copied = data / "plugins/astrbot_plugin_groupsecretary"
        save(
            self.path / "source-manifest.json",
            {
                str(p.relative_to(copied)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in copied.rglob("*")
                if p.is_file()
            },
        )
        bridge = data / "plugins/astrbot_plugin_testlab_bridge"
        bridge.mkdir()
        shutil.copy2(HERE / "bridge.py", bridge / "main.py")
        (bridge / "metadata.yaml").write_text(
            "name: astrbot_plugin_testlab_bridge\nauthor: AIE3905\nversion: v0.1.0\ndesc: Isolated test instrumentation\n"
        )
        persona = json.loads((ROOT / "config/persona_anon.json").read_text())
        persona["tools"] = [
            "search_group_history",
            "get_group_episodes",
            "get_member_profile",
            "get_topic_timeline",
            "read_group_day",
            "read_group_items",
            "save_group_drafts",
            "submit_group_events",
            "transfer_to_memory",
        ]
        persona["skills"] = []
        save(self.path / "persona.json", persona)
        # Keep reservations until all three ports have been allocated together.
        sockets = [socket.socket() for _ in range(3)]
        try:
            for s in sockets:
                s.bind(("127.0.0.1", 0))
            self.ws_port, self.dashboard_port, self.mock_port = [
                s.getsockname()[1] for s in sockets
            ]
        finally:
            for s in sockets:
                s.close()
        env = dict(
            os.environ,
            ASTRBOT_ROOT=str(self.root),
            GROUPBOT_TEST_RUN=str(self.path),
            GROUPBOT_TEST_BRIDGE_TOKEN=self.token,
            GROUPBOT_TEST_CALL_LIMIT=str(self.spec["model_limit"]),
            GROUPBOT_TEST_VIRTUAL_CLOCK="1" if self.spec.get("realtime") else "0",
            PYTHONDONTWRITEBYTECODE="1",
        )
        if self.manager.model_mode == "mock":
            models = [
                {
                    "id": "test-" + role,
                    "provider": "openai",
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "enable": True,
                    "proxy": "",
                    "custom_headers": {},
                    "api_base": f"http://127.0.0.1:{self.mock_port}/v1",
                    "model": "mock-model",
                    "key": ["sk-mock"],
                }
                for role in ["main", "fast", "strong", "reply_fast", "reply_deep"]
            ]
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "tools/sandbox/mock_llm.py"),
                str(self.mock_port),
                str(self.path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            self.procs.append(proc)
        else:
            models, keys = (
                env_models(self.manager.env_settings)
                if self.manager.model_mode == "env"
                else host_models(self.manager.host)
            )
            env.update(keys)
        agent_cfg = json.loads((ROOT / "config/host_subagents.json").read_text())
        # Search is an independent external service; this lab exercises group memory.
        agent_cfg["agents"] = [a for a in agent_cfg["agents"] if a["name"] == "memory"]
        agent_cfg["router_system_prompt"] = (
            "需要回忆本群记录时交给 transfer_to_memory；闲聊和写作直接回答。当前测试环境未启用联网搜索。"
        )
        for a in agent_cfg["agents"]:
            a["provider_id"] = "test-fast"
        cfg = {
            "platform": [
                {
                    "id": "sandbox-qq",
                    "type": "aiocqhttp",
                    "enable": True,
                    "ws_reverse_host": "127.0.0.1",
                    "ws_reverse_port": self.ws_port,
                    "ws_reverse_token": self.token,
                }
            ],
            "provider": models,
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "model": {"provider_id": "test-main", "request_max_retries": 1},
                    "persona": {"persona_id": "anon"},
                    "misc": {"max_steps": 12, "tool_call_timeout": 180},
                },
            },
            "subagent_orchestrator": agent_cfg,
            "provider_settings": {
                "enable": True,
                "streaming_response": False,
                "web_search": False,
            },
            "platform_settings": {
                "segmented_reply": {"enable": False},
                "rate_limit": {"time": 60, "count": 10000, "strategy": "stall"},
            },
            "dashboard": {
                "host": "127.0.0.1",
                "port": self.dashboard_port,
                "username": "testlab",
                "password": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            },
            "disable_metrics": True,
        }
        save(data / "cmd_config.json", cfg)
        pdata = data / "plugin_data/astrbot_plugin_groupsecretary"
        pdata.mkdir(parents=True)
        # A study can mirror the pilot group's memory and dialogue tuning; providers,
        # the front end and the memory scheduler stay under the test lab's control.
        overrides = self.spec.get("plugin_overrides", {})
        memory = {
            **overrides.get("memory", {}),
            "reading": True,
            "start_delay_seconds": 864000,
        }
        if memory.get("qa_list"):
            shutil.copy2(ROOT / "config" / memory["qa_list"], pdata / memory["qa_list"])
        dialogue = {
            **{
                k: v
                for k, v in overrides.get("dialogue", {}).items()
                if k not in {"fast_provider", "deep_provider", "frontend"}
            },
            "frontend": "astrbot",
            "fast_provider": "test-reply_fast",
            "deep_provider": "test-reply_deep",
        }
        save(
            pdata / "config.json",
            {
                "mode": "astrbot",
                "database": "secretary.sqlite3",
                "query_wait_seconds": 120,
                "max_attempts": 1,
                "groups": [
                    {
                        "key": "sandbox",
                        "enabled": True,
                        "data_use_confirmed": True,
                        "admins": [str(OWNER)],
                        "confirmers": [str(OWNER)],
                        "confirmation": "designated",
                        "timezone": "Asia/Shanghai",
                        "retention_days": 3650,
                        "processing_location": "isolated synthetic test",
                        "platform_id": "sandbox-qq",
                        "native_group_id": str(GROUP),
                        "proactive": False,
                        "report_time": "",
                    }
                ],
                "models": {
                    "understanding": {"provider_id": "test-fast"},
                    "answering": {"provider_id": "test-main"},
                    "consolidating": {"provider_id": "test-strong"},
                },
                "web": {"enabled": False},
                "dialogue": dialogue,
                "memory": memory,
            },
        )
        logfile = open(self.path / "astrbot.log", "w")
        self.files.append(logfile)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "main.py",
            "--webui-dir",
            str(self.manager.host / "data/dist"),
            cwd=self.manager.host,
            env=env,
            stdout=logfile,
            stderr=logfile,
        )
        self.procs.append(proc)
        deadline = time.monotonic() + 100
        while not (self.path / "ready.json").exists():
            if proc.returncode is not None:
                raise RuntimeError("AstrBotStartupFailed; see isolated astrbot.log")
            if time.monotonic() > deadline:
                raise TimeoutError("AstrBotStartupTimeout; see isolated astrbot.log")
            await asyncio.sleep(0.25)
        self.bridge = "http://127.0.0.1:" + str(
            json.loads((self.path / "ready.json").read_text())["port"]
        )
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240))
        for _ in range(40):
            try:
                self.ws = await connect(
                    f"ws://127.0.0.1:{self.ws_port}/ws",
                    additional_headers={
                        "X-Self-ID": str(BOT),
                        "X-Client-Role": "Universal",
                        "Authorization": "Bearer " + self.token,
                    },
                )
                break
            except OSError:
                await asyncio.sleep(0.25)
        if not self.ws:
            raise RuntimeError("VirtualAdapterStartupFailed")
        self.listener = asyncio.create_task(self.listen())
        await asyncio.sleep(0.3)
        self.snapshot = await self.api("GET", "/snapshot")

    def allocate(self, value=None):
        if value is not None and str(value) in self.ids:
            return self.ids[str(value)]
        self.next_id += 1
        if value is not None:
            self.ids[str(value)] = self.next_id
        return self.next_id

    def user(self, sender, name=None):
        value = str(sender)
        uid = (
            OWNER
            if value in {"owner", str(OWNER)}
            else 200000 + int(hashlib.sha256(value.encode()).hexdigest()[:9], 16)
        )
        self.names[uid] = name or {"owner": "老张", "lin": "小林", "yu": "小余"}.get(
            value, value
        )
        return uid

    async def listen(self):
        async for raw in self.ws:
            req = json.loads(raw)
            action, p, data = req.get("action"), req.get("params", {}), {}
            if action in {"send_group_msg", "send_msg", "send_private_msg"}:
                message = p.get("message", [])
                text = (
                    message
                    if isinstance(message, str)
                    else "".join(
                        s.get("data", {}).get("text", "")
                        for s in message
                        if s.get("type") == "text"
                    )
                )
                mid = self.allocate()
                self.messages[mid] = {
                    "message_id": mid,
                    "message": message,
                    "raw_message": text,
                    "sender": {"user_id": BOT, "nickname": "爱音"},
                    "time": int(time.time()),
                }
                self.timeline.append(
                    {
                        "role": "assistant",
                        "text": text,
                        "at": time.time(),
                        "message_id": mid,
                        "request_id": self.current,
                    }
                )
                data = {"message_id": mid}
            elif action == "get_msg":
                data = self.messages.get(int(p["message_id"]), {})
            elif action == "get_group_member_info":
                uid = int(p["user_id"])
                data = {
                    "user_id": uid,
                    "nickname": self.names.get(uid, str(uid)),
                    "card": "",
                    "role": "member",
                }
            elif action == "get_login_info":
                data = {"user_id": BOT, "nickname": "爱音"}
            await self.ws.send(
                json.dumps(
                    {
                        "status": "ok",
                        "retcode": 0,
                        "data": data,
                        "echo": req.get("echo"),
                    }
                )
            )

    def message_event(self, row, mid, user, epoch):
        """The OneBot group message for one row; also remembered for get_msg."""
        segments = []
        if row.get("reply_to"):
            ref = self.ids.get(str(row["reply_to"]))
            if (
                ref is None
                and str(row["reply_to"]).isdigit()
                and int(row["reply_to"]) in self.messages
            ):
                ref = int(row["reply_to"])
            if ref is None:
                raise ValueError("引用目标不存在于该批次")
            segments.append({"type": "reply", "data": {"id": str(ref)}})
        if row.get("at"):
            segments.append({"type": "at", "data": {"qq": str(BOT)}})
        # Mentions of other members are real at segments, which the plugin drops
        # from the stored text exactly as it does for a QQ message.
        for m in row.get("mentions", []):
            known = dict(self.names)
            uid = self.user(m["sender"], m.get("name"))
            if uid in known:
                self.names[uid] = known[uid]  # a mention never renames a known member
            segments.append({"type": "at", "data": {"qq": str(uid)}})
        segments.append({"type": "text", "data": {"text": row.get("text", "")}})
        event = {
            "time": epoch,
            "self_id": BOT,
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": mid,
            "group_id": GROUP,
            "user_id": user,
            "anonymous": None,
            "message": segments,
            "raw_message": row.get("text", ""),
            "font": 0,
            "sender": {
                "user_id": user,
                "nickname": self.names[user],
                "card": "",
                "role": "member",
            },
        }
        self.messages[mid] = {k: v for k, v in event.items() if k != "post_type"}
        return event

    async def deliver(self, row, question=False):
        """Send one group message without waiting for it to be processed.

        Realtime replays use this: the stream keeps flowing while the plugin and
        the host work. A question also opens a completion marker and tags the
        replies that follow, so they can be attributed to it.
        """
        validate_rows([row])
        if row.get("kind") == "recall":
            raise ValueError("实时回放暂不支持撤回事件")
        mid = self.allocate(row.get("native_id"))
        user = self.user(row.get("sender", "owner"), row.get("name"))
        epoch = int(
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        )
        if question:
            await self.api("POST", "/begin/" + str(mid), {})
            self.current = str(mid)
        self.timeline.append(
            {
                "role": "user",
                "sender": self.names[user],
                "text": row.get("text", ""),
                "mention": row.get("at", False),
                "message_id": mid,
                "at": time.time(),
                "timestamp": epoch,
                "kind": "message",
            }
        )
        await self.ws.send(
            json.dumps(self.message_event(row, mid, user, epoch), ensure_ascii=False)
        )
        self.progress += 1
        return mid

    async def step(self, row):
        validate_rows([row])
        async with self.lock:
            started = time.monotonic()
            mid = self.allocate(row.get("native_id"))
            self.current = str(mid)
            user = self.user(row.get("sender", "owner"), row.get("name"))
            epoch = (
                int(
                    datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                )
                if row.get("timestamp")
                else int(time.time())
            )
            if not row.get("timestamp") and mid in self.messages:
                epoch = self.messages[mid]["time"]
            text = row.get("text", "")
            before = len(self.timeline)
            self.timeline.append(
                {
                    "role": "user",
                    "sender": self.names[user],
                    "text": text,
                    "mention": row.get("at", False),
                    "message_id": mid,
                    "at": time.time(),
                    "timestamp": epoch,
                    "kind": row.get("kind", "message"),
                }
            )
            if row.get("kind") == "recall":
                target = self.ids.get(str(row["target_id"]))
                if target is None:
                    raise ValueError("撤回目标不存在于该批次")
                await self.ws.send(
                    json.dumps(
                        {
                            "time": epoch,
                            "self_id": BOT,
                            "post_type": "notice",
                            "notice_type": "group_recall",
                            "group_id": GROUP,
                            "user_id": user,
                            "operator_id": user,
                            "message_id": target,
                        }
                    )
                )
                # OneBot recall is a notice rather than a scheduled message event.
                end = time.monotonic() + 10
                while True:
                    found = (await self.api("GET", "/message/" + str(target)))[
                        "message"
                    ]
                    if found is None or found["erased"]:
                        break
                    if time.monotonic() > end:
                        raise TimeoutError("RecallTimeout")
                    await asyncio.sleep(0.2)
                self.snapshot = await self.api("GET", "/snapshot")
            else:
                await self.api("POST", "/begin/" + str(mid), {})
                event = self.message_event(row, mid, user, epoch)
                await self.ws.send(json.dumps(event, ensure_ascii=False))
                end = time.monotonic() + 240
                while True:
                    done = (await self.api("GET", "/done/" + str(mid)))["done"]
                    if done is not None:
                        if done["error"]:
                            raise RuntimeError(done["error"])
                        break
                    if self.listener.done():
                        raise RuntimeError("VirtualAdapterDisconnected")
                    if time.monotonic() > end:
                        raise TimeoutError("TurnTimeout")
                    await asyncio.sleep(0.2)
                self.snapshot = await self.api("GET", "/snapshot")
            replies = [x for x in self.timeline[before:] if x["role"] == "assistant"]
            self.timeline[before]["seconds"] = round(time.monotonic() - started, 3)
            if "expect" in row:
                exp = row["expect"]
                joined = "\n".join(x["text"] for x in replies)
                checks = [word in joined for word in exp.get("contains", [])]
                if "reply_count" in exp:
                    checks.append(len(replies) == exp["reply_count"])
                checks += [word not in joined for word in exp.get("not_contains", [])]
                self.checks.append(
                    {
                        "message_id": mid,
                        "pass": all(checks),
                        "expected": exp,
                        "actual_replies": len(replies),
                    }
                )
            self.progress += 1
            self.persist()
            if self.snapshot.get("budget", {}).get("denied", 0):
                raise RuntimeError("ModelCallLimitReached")
            return replies

    async def memory(self, action, day):
        async with self.lock:
            result = await self.api("POST", "/memory", {"action": action, "day": day})
            self.snapshot = await self.api("GET", "/snapshot")
            self.persist()
            return result

    async def close(self):
        if self.session:
            try:
                self.snapshot = await asyncio.wait_for(self.api("GET", "/snapshot"), 2)
            except Exception:
                pass
        if self.listener:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        for p in reversed(self.procs):
            if p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 12)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()
        for f in self.files:
            f.close()
        for call in self.snapshot.get("calls", []):
            if call["status"] == "running":
                call["status"] = "interrupted"
        self.finished = time.time()
        self.persist()


class Manager:
    def __init__(self, host, home, model_mode, parallel, env_settings=None):
        self.host, self.home = Path(host).resolve(), Path(home).resolve()
        self.model_mode, self.parallel = model_mode, parallel
        self.env_settings = env_settings
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.slots = asyncio.Semaphore(parallel)
        self.runs, self.archived = {}, {}
        for p in self.home.glob("*/report.json"):
            report = json.loads(p.read_text())
            if report["state"] in {"queued", "starting", "ready", "running"}:
                report["state"] = "interrupted"
            self.archived[report["id"]] = report

    def create(self, title, rows=None, copies=1, model_limit=80, integrate=False):
        if not isinstance(title, str) or not 1 <= len(title) <= 100:
            raise ValueError("标题需要1–100字符")
        if (
            type(copies) is not int
            or not 1 <= copies <= 8
            or type(model_limit) is not int
            or not 1 <= model_limit <= 1000
        ):
            raise ValueError("批次数1–8；每批模型调用上限1–1000")
        if (
            sum(
                r.state in {"queued", "starting", "running", "ready"}
                for r in self.runs.values()
            )
            + copies
            > 32
        ):
            raise ValueError("排队和运行中的批次总数不能超过32")
        if rows is not None:
            validate_rows(rows)
        out = []
        for i in range(copies):
            spec = {
                "id": datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(4),
                "title": title + (f" · {i + 1}" if copies > 1 else ""),
                "mode": "batch" if rows is not None else "interactive",
                "rows": copy.deepcopy(rows or []),
                "model_limit": model_limit,
                "integrate": integrate,
            }
            r = Worker(self, spec)
            self.runs[r.id] = r
            r.task = asyncio.create_task(self.run(r))
            out.append(r.id)
        return out

    async def run(self, r):
        acquired = False
        try:
            await self.slots.acquire()
            acquired = True
            if acquired:
                r.state, r.started = "starting", time.time()
                r.persist()
                await r.prepare()
                r.state = "ready" if r.spec["mode"] == "interactive" else "running"
                r.persist()
                if r.spec["mode"] == "interactive":
                    await asyncio.Future()
                async with asyncio.timeout(1800):
                    day = None
                    for row in r.spec["rows"]:
                        newday = (
                            datetime.fromisoformat(
                                row["timestamp"].replace("Z", "+00:00")
                            )
                            .astimezone(TZ)
                            .date()
                            .isoformat()
                            if row.get("timestamp")
                            else datetime.now(TZ).date().isoformat()
                        )
                        if day and day != newday and r.spec["integrate"]:
                            await r.memory("read", day)
                            await r.memory("consolidate", day)
                        await r.step(row)
                        day = newday
                    if day and r.spec["integrate"]:
                        await r.memory("read", day)
                        await r.memory("consolidate", day)
                r.state = "completed"
        except asyncio.CancelledError:
            r.state = "stopped"
        except Exception as exc:
            r.state, r.error = "failed", f"{type(exc).__name__}: {str(exc)[:180]}"
        finally:
            if r.active_task:
                r.active_task.cancel()
                await asyncio.gather(r.active_task, return_exceptions=True)
            try:
                await r.close()
            finally:
                if acquired:
                    self.slots.release()

    async def stop(self, rid):
        r = self.runs[rid]
        if r.task and not r.task.done():
            r.task.cancel()
            await asyncio.gather(r.task, return_exceptions=True)
            if r.state == "queued":
                r.state, r.finished = "stopped", time.time()
                r.persist()

    async def close(self):
        await asyncio.gather(*(self.stop(rid) for rid in self.runs))
