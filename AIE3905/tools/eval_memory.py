"""Replay a multi-day dataset through v2 memory (and the v3 baseline) and score it.

usage:
    python3 lab.py days --days 7 --per-day 300 --output datasets/memory-7d
    python3 tools/eval_memory.py --dataset datasets/memory-7d --config config/deepseek.json \
        [--consolidate-model deepseek-v4-pro] [--baseline] [--days 3]
    python3 tools/eval_memory.py --dataset datasets/memory-7d --fake   # plumbing only, no model

The API key comes from the configured environment variable or a hidden prompt;
never paste it into chat. Scores are read from stored memory (day views, digests,
anchors, lexicon, member notes) and from the text a reply would be given, with the
dataset's oracle; no model grades a model. Each system runs in its own temporary
database and nothing touches real group data.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "plugin"))
from secretary.cli import load_records  # noqa: E402
from secretary.config import Config  # noqa: E402
from secretary.engine import Engine  # noqa: E402
from secretary.memory import rank  # noqa: E402
from secretary.providers import OpenAICompatible  # noqa: E402

KEY = "eval"
TZ = ZoneInfo("Asia/Shanghai")
DIGITS = dict(zip("零一二两三四五六七八九", "01223456789"))


def norm(text):
    """Loose form for matching: weekday names, Chinese numerals, clock styles."""
    text = str(text).replace("星期", "周").replace("礼拜", "周").replace("周天", "周日")
    text = re.sub(
        r"([一二三四五六七八九])?十([一二三四五六七八九])?",
        lambda m: (DIGITS[m[1]] if m[1] else "1") + (DIGITS[m[2]] if m[2] else "0"),
        text,
    )
    text = "".join(DIGITS.get(c, c) for c in text)
    return re.sub(r"(\d{1,2})[:：]00", r"\1点", text).replace(" ", "")


def keys(value):
    """Alternatives per part: '周六晚上8点' -> [[周6], [8点, 20点]]; places stay whole."""
    v = norm(value)
    out = []
    for part in re.findall(r"周[1-6日]|\d{1,2}号|\d{1,2}点", v) if "点" in v else []:
        hour = re.fullmatch(r"(\d{1,2})点", part)
        if hour and int(hour[1]) < 12 and re.search("下午|晚上", v):
            out.append([part, f"{int(hour[1]) + 12}点"])
        else:
            out.append([part])
    return out or [[v]]


def has(text, value):
    text = norm(text)
    return all(any(k in text for k in alternatives) for alternatives in keys(value))


def mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


class Fake:
    """Offline stand-in: valid, content-free outputs to exercise the pipeline."""

    model = "fake"

    def __init__(self, usage):
        self.usage = usage

    async def complete(self, system, payload, role, group, **kw):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        first = re.search(r"\[m(\d+) [^\]]+\] ([^\n]+)", text)
        if "做日终整合" in text:
            out = {"qa": [], "ops": []}
        elif "你是群聊的记录员" in system:
            out = {"topics": [{"id": "t1", "title": "闲聊", "points": [{"text": first[2][:30], "m": [int(first[1])]}]}] if first else []}
        elif "日终问答摘要" in system:
            out = {"qa": []}
        elif "话题摘要" in system:
            out = {"summary": json.loads(text)["messages"][0]["text"][:40], "topics": []}
        else:
            out = {"profiles": []}
        self.usage(group, role, self.model, {"prompt_tokens": len(text), "completion_tokens": 10, "cached_tokens": 0}, 0.0, "")
        return json.dumps(out, ensure_ascii=False)


def providers(args, store):
    """(reading, consolidating) providers for one engine."""
    if args.fake:
        fake = Fake(store.log_usage)
        return fake, fake
    settings = json.loads(Path(args.config).read_text(encoding="utf-8"))["models"]["understanding"]
    reading = OpenAICompatible(settings, store.log_usage)
    strong = (
        OpenAICompatible(dict(settings, model=args.consolidate_model), store.log_usage)
        if args.consolidate_model
        else reading
    )
    return reading, strong


def engine(tmp, name, baseline):
    raw = {
        "mode": "demo",  # formal-event extraction uses rules: memory is what is measured
        "database": str(Path(tmp) / f"{name}.sqlite3"),
        "groups": [{"key": KEY, "enabled": True, "data_use_confirmed": True, "timezone": "Asia/Shanghai", "retention_days": 3650}],
        "memory": {"reading": not baseline, "episodes": baseline},
    }
    return Engine(Config(raw, tmp))


def local_day(at):
    return datetime.fromisoformat(at).astimezone(TZ).date().isoformat()


async def attempt(failures, role, coro):
    """Run one model job; a failure is counted and the replay goes on, as in production."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        failures[role] = failures.get(role, 0) + 1
        failures.setdefault("types", {})[type(exc).__name__] = failures["types"].get(type(exc).__name__, 0) + 1
        return None


async def replay(args, records, oracle, baseline):
    """Feed the dataset day by day on a simulated clock; score after each day."""
    failures, days = {}, []
    with tempfile.TemporaryDirectory(prefix="groupbot-memory-eval-") as tmp:
        e = engine(tmp, "baseline" if baseline else "v2", baseline)
        await e.start(maintenance=False)
        try:
            reading, strong = providers(args, e.store)
            e.reader.reading, e.reader.consolidating = reading, strong
            e.recall.provider = reading
            for entry in oracle:
                day = entry["day"]
                todays = [m for m in records if local_day(m.at) == day]
                for m in todays:
                    e.ingest(m)
                    now = datetime.fromisoformat(m.at)
                    if baseline:
                        while e.recall.due(KEY, now):
                            if not await attempt(failures, "episode", e.recall.build(KEY, now)):
                                break
                    elif e.reader.due(KEY, now):
                        await attempt(failures, "reading", e.reader.read_pass(KEY, day, now))
                await e.flush(KEY, timeout=120)
                morning = datetime.combine(datetime.fromisoformat(day).date() + timedelta(days=1), time(4, 30), TZ)
                if baseline:
                    await attempt(failures, "episode", e.recall.build(KEY, morning))
                else:
                    if todays:
                        view = e.store.one("SELECT last_seq FROM day_views WHERE group_key=? AND day=?", (KEY, day))
                        if e.reader.rows(KEY, day, after=view["last_seq"] if view else 0):
                            end = datetime.fromisoformat(todays[-1].at) + timedelta(minutes=1)
                            await attempt(failures, "reading", e.reader.read_pass(KEY, day, end))
                    for _ in range(2):  # one retry, like the production backoff
                        if await attempt(failures, "consolidating", e.reader.consolidate(KEY, morning)):
                            break
                    await attempt(failures, "rollup", e.reader.rollup(KEY, morning))
                at = (morning + timedelta(hours=4, minutes=30)).astimezone(timezone.utc).isoformat()
                days.append({"day": day, **(score_baseline if baseline else score_v2)(e, entry, at)})
            usage = e.store.rows(
                """SELECT role,model,COUNT(*) AS calls,SUM(prompt_tokens) AS prompt_tokens,
                SUM(cached_tokens) AS cached_tokens,SUM(completion_tokens) AS completion_tokens,
                ROUND(SUM(seconds),1) AS seconds FROM usage WHERE role NOT LIKE '%failure'
                GROUP BY role,model"""
            )
        finally:
            await e.close()
    keys_ = sorted({k for d in days for k in d if k != "day"})
    return {
        "days": days,
        "summary": {k: mean(d.get(k) for d in days) for k in keys_},
        "usage": usage,
        "failures": failures,
    }


def ratio(checks):
    return round(sum(checks) / len(checks), 3) if checks else None


def score_v2(e, entry, at):
    r, store = e.reader, e.store
    view = store.one("SELECT sidebar FROM day_views WHERE group_key=? AND day=?", (KEY, entry["day"]))
    digest = store.one("SELECT qa FROM digests WHERE group_key=? AND level='day' AND period=?", (KEY, entry["day"]))
    day_text = (view["sidebar"] if view else "") + (digest["qa"] if digest else "")
    s = {"key": KEY, "m": {"at": at, "uid": ""}, "g": e.config.group(KEY), "sources": {}, "used_sources": set()}
    topics = r.topics(KEY)

    def anchor(state):
        found = rank(state["key"] + " " + state["topic"], topics, r.label)[:2]
        return [f["statement"] for t in found for f in r.facts(KEY, t["id"])]

    current, clean, history, brief = [], [], [], []
    for st in entry["states"]:
        facts = anchor(st)
        text = " ".join(facts)
        current.append("取消" in text if st["cancelled"] else all(has(text, v) for v in st["current"]))
        if st["obsolete"]:
            clean.append(
                not any(
                    any(has(f, o) for o in st["obsolete"]) and not all(has(f, c) for c in st["current"])
                    for f in facts
                )
            )
            line = r.timeline(s, st["key"] + " " + st["topic"])
            past = json.dumps(line, ensure_ascii=False) if isinstance(line, list) else ""
            history.append(all(has(past, o) for o in st["obsolete"]))
        if not st["cancelled"]:
            shown = r.brief(KEY, st["topic"] + "现在怎么定的", at)
            brief.append(all(has(shown, v) for v in st["current"]))
    words = {w["term"]: w["meaning"] for w in store.rows("SELECT term,meaning FROM lexicon WHERE group_key=?", (KEY,))}
    facts_all = " ".join(f["statement"] for f in store.rows("SELECT statement FROM anchor_facts WHERE group_key=?", (KEY,)))
    notes = {p["name"]: p["summary"] for p in store.rows("SELECT name,summary FROM profiles WHERE group_key=?", (KEY,))}
    questions = " ".join(
        f["statement"]
        for f in store.rows(
            "SELECT statement FROM anchor_facts WHERE group_key=? AND kind='question' AND superseded_by=0", (KEY,)
        )
    )
    return {
        "day_topics": ratio([has(day_text, k) for k in entry["topics"]]),
        "day_decisions": ratio([all(has(day_text, v) for v in d["keys"]) for d in entry["decisions"]]),
        "state_current": ratio(current),
        "state_no_stale": ratio(clean),
        "history_kept": ratio(history),
        "reply_sees_current": ratio(brief),
        "terms": ratio([t["key"] in words.get(t["term"], "") for t in entry["terms"]]),
        "assignments": ratio(
            [a["task"] in notes.get(a["member"], "") or (a["member"] in facts_all and a["task"] in facts_all) for a in entry["assignments"]]
        ),
        "answers_kept": ratio([has(facts_all, a["key"]) for a in entry["answered"]]),
        "open_questions": ratio([q in questions for q in entry["open_questions"]]),
        "abstains": ratio([not isinstance(r.timeline(s, x), list) for x in entry["absent"]]),
        "anchor_topics": len([t for t in topics if t["status"] != "archived"]),
    }


def score_baseline(e, entry, at):
    store, recall = e.store, e.recall
    start = datetime.combine(datetime.fromisoformat(entry["day"]).date(), time(), TZ)
    since, until = start.astimezone(timezone.utc).isoformat(), (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    day_text = " ".join(
        r["summary"]
        for r in store.rows("SELECT summary FROM episodes WHERE group_key=? AND end_at>=? AND start_at<?", (KEY, since, until))
    )
    everything = " ".join(r["summary"] for r in store.rows("SELECT summary FROM episodes WHERE group_key=?", (KEY,)))
    newest = " ".join(r["summary"] for r in recall.episodes(KEY, until=at, limit=3))  # what the prompt gets
    notes = {p["name"]: p["summary"] for p in store.rows("SELECT name,summary FROM profiles WHERE group_key=?", (KEY,))}
    current, clean, history, brief = [], [], [], []
    for st in entry["states"]:
        found = recall.episodes(KEY, st["key"] + " " + st["topic"], until=at, limit=5)
        first = found[0]["summary"] if found else ""
        current.append("取消" in first if st["cancelled"] else all(has(first, v) for v in st["current"]))
        if st["obsolete"]:
            clean.append(not (any(has(first, o) for o in st["obsolete"]) and not all(has(first, c) for c in st["current"])))
            history.append(all(any(has(f["summary"], o) for f in found) for o in st["obsolete"]))
        if not st["cancelled"]:
            brief.append(all(has(newest, v) for v in st["current"]))
    return {
        "day_topics": ratio([has(day_text, k) for k in entry["topics"]]),
        "day_decisions": ratio([all(has(day_text, v) for v in d["keys"]) for d in entry["decisions"]]),
        "state_current": ratio(current),
        "state_no_stale": ratio(clean),
        "history_kept": ratio(history),
        "reply_sees_current": ratio(brief),
        "terms": ratio([t["term"] in everything and t["key"] in everything for t in entry["terms"]]),
        "assignments": ratio(
            [a["task"] in notes.get(a["member"], "") or (a["member"] in everything and a["task"] in everything) for a in entry["assignments"]]
        ),
        "answers_kept": ratio([has(everything, a["key"]) for a in entry["answered"]]),
        "open_questions": ratio([q in everything for q in entry["open_questions"]]),
        "abstains": ratio([not recall.episodes(KEY, x, until=at) for x in entry["absent"]]),
    }


def per_thousand(usage, messages):
    total = {k: sum(r[k] or 0 for r in usage) for k in ("calls", "prompt_tokens", "cached_tokens", "completion_tokens")}
    scale = 1000 / max(1, messages)
    return {
        "calls": round(total["calls"] * scale, 1),
        "uncached_prompt_tokens": round((total["prompt_tokens"] - total["cached_tokens"]) * scale),
        "cached_prompt_tokens": round(total["cached_tokens"] * scale),
        "completion_tokens": round(total["completion_tokens"] * scale),
        "cache_hit_rate": round(total["cached_tokens"] / total["prompt_tokens"], 3) if total["prompt_tokens"] else None,
    }


def main():
    p = argparse.ArgumentParser(description="多日群聊记忆离线评测：v2 通读与锚点 vs v3 话题摘要")
    p.add_argument("--dataset", default="datasets/memory-7d")
    p.add_argument("--config", default="config/deepseek.json", help="取 models.understanding 作为通读模型")
    p.add_argument("--consolidate-model", default="", help="日终整合换用的模型名，例如 deepseek-v4-pro")
    p.add_argument("--baseline", action="store_true", help="同时回放现有的固定窗口话题摘要作为基线")
    p.add_argument("--days", type=int, default=0, help="只评测前几天，0 为全部")
    p.add_argument("--max-calls", type=int, default=600, help="模型调用的保守上限，超过就不开始")
    p.add_argument("--fake", action="store_true", help="不调用模型，只检查流程")
    p.add_argument("--output", default="", help="报告路径，默认 results/memory-eval-<dataset>.json")
    args = p.parse_args()
    folder = Path(args.dataset) if Path(args.dataset).is_absolute() else LAB / args.dataset
    oracle = json.loads((folder / "oracle.json").read_text(encoding="utf-8"))
    if args.days:
        oracle = oracle[: args.days]
    wanted = {entry["day"] for entry in oracle}
    records = [m for m in sorted(load_records(folder / "messages.jsonl"), key=lambda m: m.at) if local_day(m.at) in wanted]
    chars = sum(len(m.text) for m in records)
    # Reading passes: one per ~6000 new characters plus at most one per idle hour, a
    # final pass and a consolidation per day, a rollup per week; baseline doubles episodes.
    bound = math.ceil(chars / 6000) + len(oracle) * 19 + len(oracle) // 7 + 1
    if args.baseline:
        bound += math.ceil(len(records) / 30) * 2 + len(oracle) * 2
    if not args.fake:
        if bound > args.max_calls:
            raise SystemExit(f"保守调用上界 {bound} > --max-calls {args.max_calls}；先用 --days 缩小范围，或明确提高上限")
        settings = json.loads(Path(args.config).read_text(encoding="utf-8"))["models"]["understanding"]
        name = settings.get("api_key_env", "GROUPBOT_MODEL_API_KEY")
        if not os.environ.get(name):
            if not sys.stdin.isatty():
                raise SystemExit(f"请在自己的终端运行并输入 {name}，不要把 Key 发到聊天里")
            os.environ[name] = getpass.getpass(f"{name}（隐藏输入，仅本次进程有效）: ").strip()
    report = {
        "dataset": json.loads((folder / "manifest.json").read_text(encoding="utf-8")),
        "days_evaluated": len(oracle),
        "messages": len(records),
        "call_bound": bound,
        "models": {"reading": "fake" if args.fake else "understanding from " + args.config,
                   "consolidating": args.consolidate_model or "same as reading"},
    }
    report["v2"] = asyncio.run(replay(args, records, oracle, baseline=False))
    report["v2"]["per_1k_messages"] = per_thousand(report["v2"]["usage"], len(records))
    if args.baseline:
        report["baseline"] = asyncio.run(replay(args, records, oracle, baseline=True))
        report["baseline"]["per_1k_messages"] = per_thousand(report["baseline"]["usage"], len(records))
    report["limits"] = (
        "按关键词核对存储的记忆和回复会看到的注入文本，不等于人工语义评分；模拟群聊由模板生成，比真实群聊规整；"
        "正式事项提取用规则，不计入成本。"
    )
    output = Path(args.output) if args.output else LAB / "results" / f"memory-eval-{folder.name}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"v2": report["v2"]["summary"], "baseline": report.get("baseline", {}).get("summary")}, ensure_ascii=False, indent=2))
    print("报告：", output)


if __name__ == "__main__":
    main()
