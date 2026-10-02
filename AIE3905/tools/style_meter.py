"""Style meter: how far bot replies sit from the group's own way of talking.

Offline and deterministic; no model calls. Compares a set of bot replies with a
reference set of human group messages on length, per-reply surface markers,
template repetition (openers, endings, paragraph shape) and "echo" (reusing the
same detail across consecutive replies of one conversation). Numbers describe
surface form only; they are a screen, not a judgment of whether a reply is good.

usage:
  style_meter.py --reference db:data/qq.sqlite3 \
                 --replies db:data/qq.sqlite3?since=2026-10-01T17:53 \
                 --replies cases:.sandbox/x/heldout/results.json?arm=C \
                 [--json out.json]
Sources: db:<group secretary sqlite>[?since=ISO&until=ISO] (replies = answers,
reference = member messages), cases:<character results.json>[?arm=X],
report:<testlab report.json>, stream:<replay stream.json> (reference only). Read-only; nothing is written except --json.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs

EMOJI = r"[\U0001F300-\U0001FAFF☀-➿]"
# Each marker is a per-reply yes/no; rates are "share of replies that contain it".
MARKERS = {
    "tilde": ("～/~", r"[～~]"),
    "particle": ("啦呀哦嘛", r"[啦呀哦嘛]"),
    "exclaim": ("！", r"[！!]"),
    "emoji": ("emoji", EMOJI),
    "dash": ("破折号——", r"——"),
    "metaphor": ("比喻词", r"仿佛|宛如|如同|就像|像是|好比"),
    "contrast": ("不是…而是", r"不是[^。！？\n]{0,20}而是"),
    "cliche": ("套话", r"说白了|本质上|总的来说|值得一提|归根结底|说到底|换句话说|不得不说|从某种意义上"),
    "numbered": ("编号列表", r"(?m)^\s*(?:\d+[\.、．]|[一二三四五]、)"),
    "two_part": ("分两段以上", r"\n\s*\n"),
    "ends_question": ("结尾问句", r"[？?][\s～~。！!" + EMOJI[1:-1] + r"]*$"),
    "ends_offer": ("结尾提议服务", r"(要不要我|需要我|要我帮|我可以帮|随时找我|有需要[^。！？\n]{0,6}(随时|尽管))[^。！？\n]{0,30}[。！？?～~]*\s*\S{0,4}$"),
    "meta": ("自我说明", r"我这边|照实|核对一下|我收下|这锅我背"),
}
OPENER_CHARS = 4


def load(spec):
    """Return (conversations, humans).

    A conversation is {"turns": [(user_text, reply)], "context": [texts]}; context holds
    what humans said around it, so repeating their facts is not counted as echo.
    """
    kind, _, rest = spec.partition(":")
    path, _, query = rest.partition("?")
    q = {k: v[0] for k, v in parse_qs(query).items()}
    if kind == "db":
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        since, until = q.get("since", ""), q.get("until", "9999")
        answers = db.execute(
            "SELECT at,question,output FROM answers WHERE at>=? AND at<? ORDER BY at", (since, until)
        ).fetchall()
        humans = [r["text"] for r in db.execute(
            "SELECT text FROM messages WHERE erased=0 AND kind!='recall' AND at>=? AND at<?", (since, until))]
        db.close()
        # A live group is one long conversation; split where the bot was idle for 3 hours.
        conversations, current, last = [], [], ""
        for a in answers:
            if last and a["at"][:13] > last[:13] and _hours(last, a["at"]) > 3:
                conversations.append(current)
                current = []
            current.append((a["question"] or "", a["output"] or ""))
            last = a["at"]
        if current:
            conversations.append(current)
        return [{"turns": c, "context": humans} for c in conversations], humans
    data = json.loads(Path(path).read_text())
    if kind == "cases":
        results = data["results"] if isinstance(data, dict) else data
        conversations = []
        for r in results:
            if q.get("arm") and r.get("arm") != q["arm"] or r.get("status") != "completed":
                continue
            history = [row["text"] for row in (r.get("fixture_audit") or {}).get("rows", [])]
            conversations.append({"turns": [
                (s["input"].get("text", ""), "\n".join(x.get("text", "") for x in s["replies"]))
                for s in r["steps"] if s["replies"]], "context": history})
        return conversations, []
    if kind == "stream":
        # A replay stream (testlab rows); only history rows are human messages.
        rows = zip(data["events"], data.get("kinds", ["history"] * len(data["events"])))
        return [], [e.get("text", "") for e, k in rows if k == "history"]
    if kind == "report":
        answers = sorted(data["snapshot"]["tables"]["answers"], key=lambda a: a["at"])
        humans = [m["text"] for m in data["snapshot"]["tables"].get("messages", [])]
        return [{"turns": [(a["question"], a["output"]) for a in answers], "context": humans}], humans
    raise ValueError("unknown source " + kind)


def _hours(a, b):
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def grams(text, n=4):
    """Content n-grams: Chinese characters, letters and digits only."""
    clean = re.sub(r"[^\w一-鿿]|_", "", text)
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def echo(turns, context=(), window=3):
    """Per reply: details the bot itself brought up in its previous `window` replies and repeats.

    N-grams that humans supplied (any user turn so far, or the surrounding context)
    are excluded: restating the user's facts is answering, not a verbal tic.
    """
    given = set().union(*(grams(t) for t in context)) if context else set()
    out = []
    for i, (_, reply) in enumerate(turns):
        given |= grams(turns[i][0])
        prior = set().union(*(grams(r) for _, r in turns[max(0, i - window):i])) if i else set()
        out.append(sorted((grams(reply) & prior) - given))
    return out


def harping(turns, times=3, n=3):
    """Phrases the bot repeats in `times` or more replies of one conversation, whoever said them first.

    The current user message is excluded per reply, so echoing a question back once
    is fine; bringing the same remark up again and again is not.
    """
    seen = Counter()
    for user, reply in turns:
        seen.update(grams(reply, n) - grams(user, n))
    return sorted(g for g, n in seen.items() if n >= times)


def measure(conversations, humans=None):
    conversations = [{"turns": [(u, r) for u, r in c["turns"] if r.strip()], "context": c["context"]}
                     for c in conversations]
    replies = [r for c in conversations for _, r in c["turns"]]
    if not replies:
        return {"replies": 0}
    lengths = [len(re.sub(r"\s", "", r)) for r in replies]
    result = {"replies": len(replies), "conversations": len(conversations),
              "length_median": statistics.median(lengths),
              "length_p90": sorted(lengths)[max(0, int(0.9 * len(lengths)) - 1)]}
    for key, (_, pattern) in MARKERS.items():
        result[key] = sum(1 for r in replies if re.search(pattern, r.strip())) / len(replies)
    emojis = Counter(e for r in replies for e in re.findall(EMOJI, r))
    result["top_emoji_share"] = (emojis.most_common(1)[0][1] / sum(emojis.values())) if emojis else 0
    openers = Counter(re.sub(r"\s", "", r)[:OPENER_CHARS] for r in replies)
    result["top_opener_share"] = openers.most_common(1)[0][1] / len(replies)
    per = [echo(c["turns"], c["context"]) for c in conversations]
    echoes = [e for p in per for e in p]
    later = [e for p in per for e in p[1:]]
    result["echo"] = sum(1 for e in later if e) / len(later) if later else 0
    long = [c for c in conversations if len(c["turns"]) >= 4]
    result["harp"] = sum(1 for c in long if harping(c["turns"])) / len(long) if long else None
    repeated = Counter(g for e in echoes for g in e)
    result["echo_top"] = repeated.most_common(8)
    if humans:
        texts = [h for h in humans if h.strip() and not h.startswith("[")]
        result["human"] = {"messages": len(texts),
                           "length_median": statistics.median(len(re.sub(r"\s", "", h)) for h in texts),
                           **{k: sum(1 for h in texts if re.search(p, h.strip())) / len(texts)
                              for k, (_, p) in MARKERS.items()}}
    return result


def table(rows, human):
    keys = ["length_median", "length_p90"] + list(MARKERS) + ["top_emoji_share", "top_opener_share", "echo", "harp"]
    names = {"length_median": "中位字数", "length_p90": "p90字数", "top_emoji_share": "最常用emoji占比",
             "top_opener_share": "最常见开头占比", "echo": "复读自己提过的细节",
             "harp": "同一说法出现在3条以上的对话",
             **{k: v[0] for k, v in MARKERS.items()}}
    head = "| 指标 | " + " | ".join(label for label, _ in rows) + (" | 群友 |" if human else " |")
    lines = [head, "|" + "---|" * (len(rows) + 1 + bool(human))]
    for k in keys:
        cells = []
        for _, m in rows + ([("群友", human)] if human else []):
            v = m.get(k, "")
            if v is None or v == "":
                cells.append("—")
            else:
                cells.append(f"{v:.0f}" if k.startswith("length") else f"{v * 100:.0f}%")
        lines.append(f"| {names[k]} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--replies", action="append", required=True, help="label=source or source")
    p.add_argument("--reference", help="source whose human messages are the baseline")
    p.add_argument("--json")
    args = p.parse_args()
    human = None
    if args.reference:
        _, humans = load(args.reference)
        human = measure([{"turns": [("", "x")], "context": []}], humans)["human"]
    rows = []
    for spec in args.replies:
        label, _, source = spec.partition("=") if "=" in spec.split(":", 1)[0] else ("", "", spec)
        conversations, _ = load(source)
        rows.append((label or source, measure(conversations)))
    print(table(rows, human))
    for label, m in rows:
        print(f"\n{label} 复读最多的片段：", "、".join(f"{g}×{n}" for g, n in m.get("echo_top", [])) or "无")
    if args.json:
        Path(args.json).write_text(json.dumps({"human": human, "rows": rows}, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
