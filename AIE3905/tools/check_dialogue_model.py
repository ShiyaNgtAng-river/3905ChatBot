"""Small synthetic dialogue acceptance run; reads credentials without logging them."""

import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from secretary.config import Config  # noqa: E402
from secretary.engine import Engine  # noqa: E402
from secretary.types import Actor  # noqa: E402


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
        "max_tokens": 2400,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    with tempfile.TemporaryDirectory(prefix="groupbot-dialogue-") as folder:
        cfg = {
            "mode": "openai",
            "database": str(Path(folder) / "test.sqlite3"),
            "models": {"understanding": settings, "answering": settings},
            "max_attempts": 1,
            "groups": [
                {
                    "key": "lab",
                    "enabled": True,
                    "data_use_confirmed": True,
                    "admins": ["owner"],
                }
            ],
        }
        e = Engine(Config(cfg))
        await e.start(maintenance=False)
        answers = []
        latencies = []
        traces = []
        original_complete = e.answerer.provider.complete

        async def traced(*args):
            result = await original_complete(*args)
            traces.append(
                {"turn": len(answers) + 1, "role": args[2], "response": result}
            )
            return result

        e.answerer.provider.complete = traced

        async def ask(actor, text):
            started = time.monotonic()
            response = await e.dialogue(
                Actor(actor, ["lab"], actor == "owner"),
                "lab",
                text,
                request_id=str(len(answers) + 1),
            )
            seconds = round(time.monotonic() - started, 3)
            latencies.append(seconds)
            answers.append(
                {
                    "actor": actor,
                    "request": text,
                    "response": response,
                    "seconds": seconds,
                    "states": [
                        {
                            "title": s["title"],
                            "status": s["status"],
                            "fields": s["fields"],
                        }
                        for s in e.states("lab")
                    ],
                }
            )
            print(
                json.dumps(
                    {
                        "turn": len(answers),
                        "mode": response["mode"],
                        "drafts": len(response["drafts"]),
                        "operations": response["operations"],
                        "seconds": seconds,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return response

        try:
            await ask(
                "owner",
                "帮我们设计集成评审的两套开会安排。日期是2026年10月2日，两套方案都在三楼讨论室。方案1从晚上8点开始，方案2从晚上9点开始。先给建议，不要定案。",
            )
            await ask("owner", "第二个方案提前半小时，其他保持不变。")
            await ask("owner", "就按刚修改的方案定了。")
            await ask("owner", "最终的集成评审怎么安排？")
            await ask(
                "lin",
                "帮我设计2026年10月3日晚上8点在四楼的新人交流会方案，只给一套，先不要定案。",
            )
            await ask("lin", "就按这个方案定了。")
            await ask("owner", "帮我写一段轻松的会议开场白。")
            states = e.states("lab")
            assessment = next((s for s in states if "集成" in s["title"]), {})
            newcomer = next((s for s in states if "新人" in s["title"]), {})
            checks = {
                "design_saves_two_drafts": len(answers[0]["response"]["drafts"]) == 2,
                "design_does_not_confirm": answers[0]["states"] == [],
                "revision_saves_new_version": any(
                    d["version"] == 2 for d in answers[1]["response"]["drafts"]
                ),
                "authorized_confirmation": assessment.get("status") == "confirmed"
                and "20:30" in assessment.get("fields", {}).get("when", ""),
                "query_reports_time": any(
                    x in answers[3]["response"]["text"]
                    for x in ("20:30", "8:30", "八点半", "8点30", "八点三十分")
                ),
                "query_cites_sources": bool(answers[3]["response"]["sources"]),
                "unauthorized_stays_pending": newcomer.get("status") == "unconfirmed",
                "writing_without_fact_gate": answers[6]["response"]["mode"]
                == "dialogue"
                and not answers[6]["response"]["operations"],
            }
            usage = e.store.rows(
                "SELECT role,COUNT(*) calls,SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens FROM usage GROUP BY role"
            )
            report = {
                "version": "0.2.0",
                "test_data": "entirely synthetic",
                "transport": "direct compatible API; not AstrBot runtime",
                "checks": checks,
                "passed": all(checks.values()),
                "task_pass_rate": sum(checks.values()) / len(checks),
                "unauthorized_confirmations": int(
                    newcomer.get("status") == "confirmed"
                ),
                "citation_scope": "structural source validity plus synthetic expected answer, not a semantic accuracy benchmark",
                "latency_p50_seconds": statistics.median(latencies),
                "latency_max_seconds": max(latencies),
                "usage": usage,
                "turns": answers,
                "synthetic_traces": traces,
            }
            (ROOT / "results/dialogue-deepseek-v0.2.0.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in report.items()
                        if k not in {"turns", "synthetic_traces"}
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if report["passed"] else 1
        finally:
            await e.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print("Synthetic dialogue check failed:", type(exc).__name__)
        raise SystemExit(1)
