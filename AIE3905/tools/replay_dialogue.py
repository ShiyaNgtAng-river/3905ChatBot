"""Replay synthetic multi-turn tasks with seeded background noise in an isolated DB."""

import argparse
import asyncio
import json
from pathlib import Path
import random
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import credentials  # noqa: E402
from secretary.config import Config  # noqa: E402
from secretary.engine import Engine  # noqa: E402
from secretary.types import Actor, Message, utcnow  # noqa: E402


def build_schedule(cases, noise, seed):
    if not 0 <= noise < 1:
        raise ValueError("noise 必须在 [0,1)")
    rng = random.Random(seed)
    n = round(len(cases) * noise / (1 - noise))
    buckets = [[] for _ in cases]
    chatter = [
        "今天食堂吃什么？",
        "刚才的表情包真有意思",
        "我先去拿杯水",
        "周末有人去散步吗",
        "哈哈",
        "天气挺舒服",
    ]
    for _ in range(n):
        buckets[rng.randrange(len(cases))].append(
            {"kind": "noise", "sender": "noise-user", "text": rng.choice(chatter)}
        )
    return [
        row
        for bucket, case in zip(buckets, cases)
        for row in bucket + [dict(case, kind="dialogue")]
    ]


async def run(args):
    cfg = Config.load(args.config)
    if cfg.mode == "astrbot":
        raise ValueError("独立回放请用 openai 或 demo 配置，不能共享 QQ 的运行数据库")
    cases = json.loads(Path(args.dataset).read_text())["cases"]
    schedule = build_schedule(cases, args.noise, args.seed)
    bound = sum(
        4 if row["kind"] == "dialogue" else cfg.max_attempts for row in schedule
    )
    if cfg.mode == "openai" and bound > args.max_calls:
        raise ValueError(f"请求上界 {bound} 超过 --max-calls {args.max_calls}")
    credentials(cfg)
    report = {
        "mode": cfg.mode,
        "seed": args.seed,
        "noise_ratio": sum(r["kind"] == "noise" for r in schedule) / len(schedule),
        "message_count": len(schedule),
        "call_bound": bound,
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="groupbot-dialogue-bench-") as folder:
        cfg.db = Path(folder) / "replay.sqlite3"
        cfg.groups = {"lab": next(iter(cfg.groups.values()))}
        cfg.groups["lab"].key = "lab"
        cfg.groups["lab"].admins = ["owner"]
        cfg.groups["lab"].confirmers = ["owner"]
        cfg.groups["lab"].enabled = True
        cfg.groups["lab"].data_use_confirmed = True
        cfg.groups["lab"].proactive = False
        cfg.groups["lab"].report_time = ""
        e = Engine(cfg)
        await e.start(maintenance=False)
        times = []
        cite_ok = 0
        cite_total = 0
        wrong_confirmations = 0
        try:
            for i, row in enumerate(schedule):
                if row["kind"] == "noise":
                    e.ingest(
                        Message(
                            "lab",
                            row["sender"],
                            row["text"],
                            utcnow(),
                            native_id=f"noise-{args.seed}-{i}",
                        )
                    )
                    await e.flush("lab", timeout=120)
                    continue
                started = time.monotonic()
                result = await e.dialogue(
                    Actor(row["sender"], ["lab"], row["sender"] == "owner"),
                    "lab",
                    row["text"],
                    request_id=f"{args.seed}-{i}",
                )
                times.append(time.monotonic() - started)
                checks = {}
                expected = row.get("expect", {})
                if "drafts" in expected:
                    checks["drafts"] = len(result["drafts"]) == expected["drafts"]
                if "version" in expected:
                    checks["version"] = any(
                        d["version"] == expected["version"] for d in result["drafts"]
                    )
                if "operation" in expected:
                    checks["operation"] = any(
                        o["status"] == expected["operation"]
                        for o in result["operations"]
                    )
                if expected.get("no_operations"):
                    checks["no_operations"] = not result["operations"]
                if "contains_any" in expected:
                    checks["answer"] = any(
                        t in result["text"] for t in expected["contains_any"]
                    )
                if expected.get("sources"):
                    checks["sources"] = bool(result["sources"])
                for source in result["sources"]:
                    cite_total += 1
                    cite_ok += bool(e.store.message("lab", source["uid"]))
                if row["sender"] != "owner":
                    wrong_confirmations += sum(
                        o["status"] == "recorded" for o in result["operations"]
                    )
                report["cases"].append(
                    {
                        "id": row["id"],
                        "checks": checks,
                        "passed": all(checks.values()),
                        "seconds": round(times[-1], 3),
                        "result": result,
                    }
                )
                print(row["id"], report["cases"][-1]["passed"], flush=True)
            usage = e.store.rows(
                "SELECT role,COUNT(*) calls,SUM(prompt_tokens) prompt_tokens,SUM(completion_tokens) completion_tokens FROM usage GROUP BY role"
            )
            report.update(
                task_pass_rate=sum(c["passed"] for c in report["cases"]) / len(cases),
                unauthorized_confirmations=wrong_confirmations,
                source_id_validity=cite_ok / cite_total if cite_total else None,
                citation_note="ID可访问性；不代表语义引用正确率",
                latency_p50_seconds=statistics.median(times),
                latency_max_seconds=max(times),
                usage=usage,
                processing_failures=e.store.one(
                    "SELECT COUNT(*) n FROM messages WHERE status='failed'"
                )["n"],
            )
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            return (
                0
                if report["task_pass_rate"] == 1
                and not wrong_confirmations
                and not report["processing_failures"]
                else 1
            )
        finally:
            await e.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config/deepseek.json"))
    parser.add_argument(
        "--dataset", default=str(ROOT / "datasets/dialogue-small/scenarios.json")
    )
    parser.add_argument("--noise", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-calls", type=int, default=120)
    parser.add_argument("--output", default=str(ROOT / "results/dialogue-noise.json"))
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except (ValueError, RuntimeError) as exc:
        print(str(exc))
        raise SystemExit(1)
