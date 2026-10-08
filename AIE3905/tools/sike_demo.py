"""Run the group secretary behind the 伺客 HTTP adapter, for a local web demo.

Starts an isolated AstrBot: its own ASTRBOT_ROOT, database, ports and log under
.sandbox/sike-demo/<time>/. It loads the group secretary and the sike adapter copied
from this repo, one sike_http platform, one configured group and a persona package.
There is no QQ platform and no live data. Model keys are read from the host config
like the test workbench does and reach the child only through environment variables.

The other side is digital-employee-talk (the web test client); start it with
DOWNSTREAM_URL=http://127.0.0.1:<port>/api/v1/im/messages. Create the group in the
web page with the same group ID as --room, then @数字员工 in that group.

usage (with the AstrBot venv python; Ctrl+C stops the instance):
  <AstrBot>/.venv/bin/python tools/sike_demo.py --astrbot <AstrBot> [--port 18080]
      [--room room-demo] [--persona extensions/default_persona] [--admins 陈经理]
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/testlab"))
from runtime import host_models  # noqa: E402

PLATFORM_ID = "sike-demo"
GROUP_TOOLS = [
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
SETUP_PLUGIN = '''import json
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Star


class DemoPersona(Star):
    """Creates the demo persona once AstrBot has loaded; writes a marker when done."""

    @filter.on_astrbot_loaded()
    async def loaded(self):
        here = Path(__file__).parent
        persona = json.loads((here / "persona.json").read_text(encoding="utf-8"))
        manager = self.context.persona_manager
        if not any(p.persona_id == persona["persona_id"] for p in await manager.get_all_personas()):
            await manager.create_persona(**persona)
        (here / "ready").write_text("ok")
'''


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def persona_from(package):
    meta = json.loads((package / "package.json").read_text(encoding="utf-8"))
    return {
        "persona_id": meta["persona_id"],
        "system_prompt": (package / "persona.md").read_text(encoding="utf-8").strip(),
        "begin_dialogs": [],
        "tools": GROUP_TOOLS,
        "skills": [],
        "custom_error_message": meta.get("custom_error_message"),
    }


def prepare(a, run):
    data = run / "root/data"
    plugins = data / "plugins"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", "docs", "examples")
    shutil.copytree(ROOT / "plugin", plugins / "astrbot_plugin_groupsecretary", ignore=ignore)
    shutil.copytree(
        ROOT / "extensions/astrbot_plugin_sike_adapter",
        plugins / "astrbot_plugin_sike_adapter",
        ignore=ignore,
    )
    package = (ROOT / a.persona).resolve()
    persona = persona_from(package)
    setup = plugins / "astrbot_plugin_demo_persona"
    setup.mkdir()
    (setup / "main.py").write_text(SETUP_PLUGIN, encoding="utf-8")
    (setup / "metadata.yaml").write_text(
        "name: astrbot_plugin_demo_persona\nauthor: AIE3905\nversion: v0.1.0\n"
        "desc: Creates the demo persona in an isolated instance\n",
        encoding="utf-8",
    )
    save(setup / "persona.json", persona)

    models, env = host_models(Path(a.astrbot))
    agents = json.loads((ROOT / "config/host_subagents.json").read_text(encoding="utf-8"))
    # Web search is a separate external service; the demo shows group memory.
    agents["agents"] = [x for x in agents["agents"] if x["name"] == "memory"]
    agents["router_system_prompt"] = (
        "需要回忆本群记录时交给 transfer_to_memory；闲聊和写作直接回答。当前演示环境未启用联网搜索。"
    )
    for x in agents["agents"]:
        x["provider_id"] = "test-fast"
    save(
        data / "cmd_config.json",
        {
            "platform": [
                {
                    "id": PLATFORM_ID,
                    "type": "sike_http",
                    "enable": True,
                    "listen_host": "127.0.0.1",
                    "listen_port": a.port,
                    "inbound_path": "/api/v1/im/messages",
                    "reply_url": a.reply_url,
                    "bot_account_id": a.account_id,
                    "bot_user_id": a.user_id,
                    "bot_names": a.names,
                }
            ],
            "provider": models,
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "model": {"provider_id": "test-main", "request_max_retries": 1},
                    "persona": {"persona_id": persona["persona_id"]},
                    "misc": {"max_steps": 12, "tool_call_timeout": 180},
                },
            },
            "subagent_orchestrator": agents,
            "provider_settings": {
                "enable": True,
                "streaming_response": False,
                "web_search": False,
                "datetime_system_prompt": False,
                "buffer_intermediate_messages": True,
            },
            "platform_settings": {
                "segmented_reply": {"enable": False},
                "rate_limit": {"time": 60, "count": 10000, "strategy": "stall"},
            },
            "dashboard": {
                "host": "127.0.0.1",
                "port": free_port(),
                "username": "demo",
                "password": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            },
            "disable_metrics": True,
        },
    )
    pdata = data / "plugin_data/astrbot_plugin_groupsecretary"
    admins = [x.strip() for x in a.admins.split(",") if x.strip()]
    save(
        pdata / "config.json",
        {
            "mode": "astrbot",
            "database": "secretary.sqlite3",
            "query_wait_seconds": 120,
            "max_attempts": 1,
            "groups": [
                {
                    "key": "demo",
                    "enabled": True,
                    "data_use_confirmed": True,
                    "admins": admins,
                    "confirmers": admins,
                    "confirmation": "designated",
                    "timezone": "Asia/Shanghai",
                    "retention_days": 30,
                    "processing_location": "local demo",
                    "platform_id": PLATFORM_ID,
                    "native_group_id": a.room,
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
            "dialogue": {
                "frontend": "astrbot",
                "fast_provider": "test-reply_fast",
                "deep_provider": "test-reply_deep",
            },
            # A short demo is answered from recent messages and search; the nightly
            # reading jobs stay off so they do not spend calls in the background.
            "memory": {"reading": True, "start_delay_seconds": 864000},
        },
    )
    if (package / "rules.json").is_file():
        rules = pdata / "persona_rules"
        rules.mkdir()
        shutil.copy2(package / "rules.json", rules / (persona["persona_id"] + ".json"))
    return env, setup / "ready"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--astrbot", required=True)
    p.add_argument("--port", type=int, default=18080)
    p.add_argument("--reply-url", default="http://127.0.0.1:3000/api/v1/im/replies")
    p.add_argument("--room", default="room-demo")
    p.add_argument("--persona", default="extensions/default_persona")
    p.add_argument("--names", default="数字员工")
    p.add_argument("--admins", default="")
    p.add_argument("--account-id", default="LIRUNLIN919")
    p.add_argument("--user-id", default="sales-bot-01")
    p.add_argument("--home", default=".sandbox/sike-demo")
    a = p.parse_args()
    host = Path(a.astrbot).expanduser().resolve()
    a.astrbot = str(host)
    run = (ROOT / a.home / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    run.mkdir(parents=True, mode=0o700)
    keys, ready = prepare(a, run)
    env = dict(os.environ, ASTRBOT_ROOT=str(run / "root"), PYTHONDONTWRITEBYTECODE="1", **keys)
    log = open(run / "astrbot.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--webui-dir", str(host / "data/dist")],
        cwd=host,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    stop = lambda *_: proc.terminate()  # noqa: E731
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + 120
    url = f"http://127.0.0.1:{a.port}/healthz"
    while True:
        if proc.poll() is not None:
            sys.exit(f"AstrBot exited during startup; see {run / 'astrbot.log'}")
        if time.monotonic() > deadline:
            proc.terminate()
            sys.exit(f"AstrBot did not become ready; see {run / 'astrbot.log'}")
        try:
            urllib.request.urlopen(url, timeout=1)
            if ready.exists():
                break
        except OSError:
            pass
        time.sleep(0.5)
    print(f"Demo ready: inbound http://127.0.0.1:{a.port}/api/v1/im/messages", flush=True)
    print(f"Replies go to {a.reply_url}; group ID {a.room}; log {run / 'astrbot.log'}", flush=True)
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
