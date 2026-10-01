"""Integration check: real isolated AstrBot processes, deterministic local model."""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from runtime import ROOT, Manager, save


async def main(host):
    os.umask(0o077)
    home = (
        ROOT
        / ".sandbox"
        / ("testlab-check-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    m = Manager(host, home, "mock", 2)
    checks = []

    def check(name, ok):
        checks.append({"name": name, "pass": bool(ok)})
        print(("PASS " if ok else "FAIL ") + name, flush=True)

    async def ready(r):
        async with asyncio.timeout(110):
            while r.state not in {"ready", "failed"}:
                await asyncio.sleep(0.2)
        if r.state == "failed":
            raise RuntimeError(r.error)

    try:
        jobs = []
        for marker in ["石榴A", "苹果B"]:
            rows = [
                {
                    "sender": "lin",
                    "text": marker,
                    "native_id": "same-id",
                    "expect": {"reply_count": 0},
                },
                {
                    "sender": "owner",
                    "text": "你好",
                    "at": True,
                    "expect": {"reply_count": 1},
                },
            ]
            jobs.append(
                m.runs[m.create(marker, rows, model_limit=20, integrate=True)[0]]
            )
        await asyncio.gather(*(r.task for r in jobs))
        check(
            "two concurrent workers complete", all(r.state == "completed" for r in jobs)
        )
        for r in jobs:
            if r.error:
                print(r.error)
        check(
            "workers overlap in time",
            max(r.started for r in jobs) < min(r.finished for r in jobs),
        )
        check(
            "ordinary messages silent; mention replies once",
            all(len(r.checks) == 2 and all(c["pass"] for c in r.checks) for r in jobs),
        )
        for i, r in enumerate(jobs):
            messages = r.snapshot.get("tables", {}).get("messages", [])
            text = " ".join(x["text"] for x in messages)
            check(
                f"run {i + 1} has isolated history",
                r.spec["title"] in text and jobs[1 - i].spec["title"] not in text,
            )
            check(
                f"run {i + 1} has day view and anchor",
                r.snapshot.get("counts", {}).get("day_views") == 1
                and r.snapshot.get("counts", {}).get("anchor_facts") == 1,
            )
            check(
                f"run {i + 1} processes stopped",
                all(p.returncode is not None for p in r.procs),
            )

        r = m.runs[m.create("interactive tool check", model_limit=30)[0]]
        await ready(r)
        await r.step(
            {
                "sender": "owner",
                "text": "帮我们设计评审后的聚餐方案",
                "at": True,
                "native_id": "design",
            }
        )
        await r.step({"sender": "owner", "text": "就按这个方案定了", "at": True})
        check(
            "real host tool calls create and confirm a draft",
            any(x["status"] == "confirmed" for x in r.snapshot["items"]),
        )
        check(
            "tool traces captured",
            any(x["tool"] == "submit_group_events" for x in r.snapshot["tools"]),
        )
        before = len(r.timeline)
        await r.step(
            {
                "sender": "owner",
                "text": "帮我们设计评审后的聚餐方案",
                "at": True,
                "native_id": "design",
            }
        )
        check(
            "stable duplicate ID produces no second answer",
            not any(x["role"] == "assistant" for x in r.timeline[before:]),
        )
        await r.step({"sender": "lin", "text": "撤回测试标记", "native_id": "remove"})
        await r.step({"sender": "lin", "kind": "recall", "target_id": "remove"})
        check(
            "recall removes source",
            not any(
                x["text"] == "撤回测试标记" and not x["erased"]
                for x in r.snapshot["tables"]["messages"]
            ),
        )
        await m.stop(r.id)
        check(
            "interactive stop cleans worker",
            r.state == "stopped" and all(p.returncode is not None for p in r.procs),
        )

        # One permitted provider call: a tool-using reply would need a second.
        r = m.runs[
            m.create(
                "budget check",
                [{"sender": "owner", "text": "帮我们设计聚餐方案", "at": True}],
                model_limit=1,
            )[0]
        ]
        await r.task
        check(
            "model budget enforced",
            r.state == "failed" and len(r.snapshot.get("calls", [])) == 1,
        )
        r = m.runs[
            m.create(
                "exact budget",
                [{"sender": "owner", "text": "你好", "at": True}],
                model_limit=1,
            )[0]
        ]
        await r.task
        check(
            "one allowed call can finish successfully",
            r.state == "completed" and len(r.snapshot.get("calls", [])) == 1,
        )
    finally:
        await m.close()
        report = {
            "checks": checks,
            "passed": all(c["pass"] for c in checks),
            "home": str(home),
            "model": "mock",
        }
        save(home / "verification.json", report)
        print(
            json.dumps(
                {"passed": report["passed"], "report": str(home / "verification.json")}
            ),
            flush=True,
        )
    if not all(c["pass"] for c in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--astrbot", required=True)
    asyncio.run(main(Path(p.parse_args().astrbot)))
