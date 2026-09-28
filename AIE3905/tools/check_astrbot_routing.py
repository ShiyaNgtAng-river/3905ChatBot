"""Check the installed host's real ProcessStage with synthetic events; no QQ sends."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HOST = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(ROOT / "plugin"))
from astrbot.core.pipeline.process_stage.stage import ProcessStage  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "routing_contract", ROOT / "plugin/tests/test_astrbot_contract.py"
)
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


async def main():
    suite = contract.AstrBotContractTests(
        "test_non_mentioned_message_is_collected_without_reply"
    )
    await suite.asyncSetUp()
    try:

        class Model:
            model = "fake"

            async def complete(self, *args):
                return '{"text":"建议明晚八点讨论，先作为草案。","sources":[]}'

        suite.plugin.engine.answerer.provider = Model()

        class Event(contract.Event):
            call_llm = False
            _has_send_oper = False

            def should_call_llm(self, flag):
                AstrMessageEvent.should_call_llm(self, flag)

            def get_extra(self, key):
                return ["plugin"] if key == "activated_handlers" else None

            def get_result(self):
                return None

            def is_stopped(self):
                return False

        calls = []

        class DefaultAgent:
            async def process(self, event):
                calls.append(event)
                if False:
                    yield

        class PluginStage:
            async def process(self, event):
                await suite.plugin.observe(event)
                if False:
                    yield

        stage = ProcessStage()
        stage.ctx = SimpleNamespace(
            astrbot_config={"provider_settings": {"enable": True}}
        )
        stage.agent_sub_stage = DefaultAgent()
        stage.star_request_sub_stage = PluginStage()
        event = Event("帮我们安排会议", mid="synthetic-routing", wake=True)
        async for _ in stage.process(event):
            pass
        await asyncio.gather(*list(suite.plugin.reply_tasks))
        assert not calls, "Host default agent generated a duplicate reply"
        assert len(event.sent) == 1, "Expected one plugin message"
        async for _ in stage.process(event):
            pass
        await asyncio.gather(*list(suite.plugin.reply_tasks))
        assert not calls and len(event.sent) == 1, "Replayed event sent a duplicate"
        other = Event("hello", mid="other", group="outside", wake=True)
        async for _ in stage.process(other):
            pass
        assert len(calls) == 1, "Unconfigured groups should keep default routing"
        report = {
            "host_process_stage": "real installed source",
            "platform_send": "synthetic capture; no QQ transmission",
            "managed_group_default_agent_calls": 0,
            "managed_group_plugin_messages": 1,
            "replay_messages": 0,
            "unmanaged_group_default_agent_calls": 1,
            "passed": True,
        }
        (ROOT / "results/astrbot-routing-v0.2.1.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await suite.asyncTearDown()


asyncio.run(main())
