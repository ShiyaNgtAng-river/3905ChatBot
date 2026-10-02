"""Synthetic end-to-end smoke, never connects to QQ or writes group memory.

Use the host AstrBot Python, set PYTHONPATH to host + this directory and
ASTRBOT_ROOT to a private scratch directory. Start the sidecar first.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time

import httpx
from astrbot.core.message.components import Image, Record
from astrbot_plugin_groupmedia.main import GroupMedia
from tests.test_astrbot import Event


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--deepseek-config",
        type=Path,
        help="Explicitly authorize synthetic text-only cloud tests using an existing provider",
    )
    args = parser.parse_args()
    os.umask(0o077)
    home = args.home.resolve()
    plugin = GroupMedia(
        None,
        {
            "access_token_file": str(home / "access.json"),
            "allowed_sessions": ["test:GroupMessage:A"],
        },
    )
    evidence = {
        "synthetic_only": True,
        "qq_messages_sent": 0,
        "memory_written": False,
        "capabilities": await plugin.call("capabilities"),
    }
    events = {}
    results = {}
    for operation, file, cls in [
        ("ocr", "notice.png", Image),
        ("transcribe", "voice.wav", Record),
    ]:
        path = home / "fixtures" / file
        event = Event([cls.fromFileSystem(str(path))])
        event.message_obj.message_id = "synthetic-" + operation
        started = time.monotonic()
        result = await plugin.execute(event, operation)
        results[operation] = result
        events[operation] = event
        evidence[operation] = {
            "result": result,
            "total_seconds": round(time.monotonic() - started, 3),
            "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        (home / (operation + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            operation,
            json.dumps(
                {"text": result.get("text"), "elapsed_ms": result.get("elapsed_ms")},
                ensure_ascii=False,
            ),
            flush=True,
        )
    missing = await plugin.generate_tool(Event(), "生成一张虚构的测试图")
    evidence["unconfigured_generation"] = json.loads(missing)
    assert evidence["unconfigured_generation"]["ok"] is False
    ocr_text = results["ocr"]["text"]
    evidence["ocr_checks"] = {
        s: s in ocr_text for s in ["2026年10月8日", "15:30", "B302", "尚未确认"]
    }
    assert all(evidence["ocr_checks"].values())
    if args.deepseek_config:
        cfg = json.loads(args.deepseek_config.read_text(encoding="utf-8-sig"))
        provider = next(
            p for p in cfg["provider"] if p["id"] == "deepseek/deepseek-flash"
        )
        source = next(
            p
            for p in cfg["provider_sources"]
            if p["id"] == provider["provider_source_id"]
        )
        key = source["key"][0] if isinstance(source["key"], list) else source["key"]
        evidence["deepseek"] = []
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            for operation in ["ocr", "transcribe"]:
                tool_name = "groupmedia_" + operation
                tool = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": "读取本条消息中的"
                        + ("图片文字" if operation == "ocr" else "录音并转写"),
                        "parameters": {
                            "type": "object",
                            "properties": {"index": {"type": "integer"}},
                            "required": [],
                        },
                    },
                }
                messages = [
                    {
                        "role": "system",
                        "content": "你是群助手。媒体需要实际调用工具才能读取。只据工具结果回答，不把建议说成正式确认。",
                    },
                    {
                        "role": "user",
                        "content": "请读取我附上的"
                        + ("截图" if operation == "ocr" else "语音")
                        + "，告诉我开会的时间和地点，还有是否已确定。",
                    },
                ]
                usage = []
                started = time.monotonic()

                async def complete():
                    r = await client.post(
                        source["api_base"].rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + key},
                        json={
                            "model": provider["model"],
                            "messages": messages,
                            "tools": [tool],
                            "max_tokens": 1000,
                            "thinking": {"type": "disabled"},
                        },
                    )
                    if r.status_code != 200:
                        raise RuntimeError(
                            "Existing text provider returned HTTP " + str(r.status_code)
                        )
                    data = r.json()
                    usage.append(data.get("usage", {}))
                    return data["choices"][0]["message"]

                first = await complete()
                calls = first.get("tool_calls") or []
                if len(calls) != 1 or calls[0]["function"]["name"] != tool_name:
                    raise RuntimeError("Model did not request the expected media tool")
                messages.append(first)
                call = calls[0]
                tool_args = json.loads(call["function"]["arguments"])
                # Use a fresh event so this is a real tool -> HTTP -> local-model call.
                event = Event(events[operation].get_messages())
                output = await getattr(plugin, operation + "_tool")(event, **tool_args)
                assert json.loads(output).get("ok"), "Tool failed"
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": output}
                )
                final = await complete()
                evidence["deepseek"].append(
                    {
                        "operation": operation,
                        "model": provider["model"],
                        "tool": tool_name,
                        "tool_arguments": tool_args,
                        "answer": final.get("content"),
                        "llm_calls": 2,
                        "usage": usage,
                        "seconds": round(time.monotonic() - started, 3),
                    }
                )
                print("DeepSeek", operation, final.get("content"), flush=True)
    (home / "smoke-results.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    )
    print("Evidence:", home / "smoke-results.json")


if __name__ == "__main__":
    asyncio.run(main())
