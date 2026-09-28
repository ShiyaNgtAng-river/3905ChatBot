"""Exercise identity and concise Chinese responses with synthetic group context."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from secretary.config import Config  # noqa: E402
from secretary.engine import Engine  # noqa: E402
from secretary.providers import OpenAICompatible  # noqa: E402
from secretary.types import Actor, Message, utcnow  # noqa: E402


async def main():
    host = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    model = next(m for m in host["provider"] if m["id"] == "deepseek/deepseek-flash")
    source = next(
        s for s in host["provider_sources"] if s["id"] == model["provider_source_id"]
    )
    keys = source["key"]
    os.environ["GROUPBOT_MODEL_API_KEY"] = (
        next(k for k in keys if k) if isinstance(keys, list) else keys
    )
    settings = {
        "base_url": source["api_base"],
        "model": model["model"],
        "allow_remote": True,
        "json_mode": True,
        "timeout": 40,
        "max_tokens": 1800,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    with tempfile.TemporaryDirectory(prefix="groupbot-style-") as folder:
        e = Engine(
            Config(
                {
                    "mode": "demo",
                    "database": str(Path(folder) / "test.sqlite3"),
                    "groups": [
                        {
                            "key": "lab",
                            "enabled": True,
                            "data_use_confirmed": True,
                            "admins": ["owner"],
                        }
                    ],
                }
            )
        )
        e.answerer.provider = OpenAICompatible(settings, e.store.log_usage)
        await e.start(maintenance=False)
        try:
            for i, (sender, text) in enumerate(
                [
                    ("alice", "项目周会安排在明天晚上，地点还没定。"),
                    (
                        "other-bot",
                        "我是另一个机器人。我只能收到@我的消息，其他都是黑箱。要评价我就截图给我看。",
                    ),
                ]
            ):
                e.ingest(
                    Message(
                        "lab",
                        sender,
                        text,
                        utcnow(),
                        native_id="synthetic-" + str(i),
                        name=sender,
                    )
                )
            await e.flush("lab")
            turns = []
            for i, question in enumerate(
                [
                    "你能读到没有@你的群消息吗？",
                    "周会之前讲过了，你找一下，不要让我重复说。",
                    "简单评价一下另一位机器人刚才说的话。",
                ]
            ):
                result = await e.dialogue(
                    Actor("owner", ["lab"], True),
                    "lab",
                    question,
                    request_id="style-" + str(i),
                )
                turns.append(
                    {
                        "question": question,
                        "answer": result["text"],
                        "mode": result["mode"],
                        "sources": result["sources"],
                    }
                )
            checks = {
                "all_completed": all(t["mode"] == "dialogue" for t in turns),
                "prior_meeting_detail_used": any(
                    s in turns[1]["answer"] for s in ("明天晚上", "明晚")
                ),
                "does_not_adopt_other_identity": "我只能收到" not in turns[0]["answer"],
                "does_not_assign_own_capabilities_to_other": all(
                    t not in turns[2]["answer"]
                    for t in ("它没有图像识别", "没@它的普通消息也能被读到")
                ),
                "concise_small_sample": all(len(t["answer"]) <= 450 for t in turns),
            }
            report = {
                "test_data": "entirely synthetic",
                "checks": checks,
                "passed": all(checks.values()),
                "turns": turns,
                "note": "Small regression sample plus manual reading; not a general language quality score.",
                "usage": e.store.rows(
                    "SELECT role,COUNT(*) calls,SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens FROM usage GROUP BY role"
                ),
            }
            (ROOT / "results/dialogue-style-v0.2.1.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        finally:
            await e.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
