"""Realtime replay study: how well does group memory answer as the chat grows?

A real QQ group export streams into an isolated AstrBot with the plugin, one
message at a time, in order. Designed questions are asked with an @ at
checkpoints while the chat keeps flowing, and a program scores the answers.

Time: the plugin and the host agent run on a virtual clock that starts at the
first message. While anything is working (a message being processed, a
background reading or consolidation pass, an answer), virtual time flows at
real speed and the messages that fall in that window arrive meanwhile. Only
when everything is idle does the clock jump to the next message. The memory
loop runs on the same clock with its production triggers. The result equals a
real-time replay with the idle gaps cut out.

usage, from AIE3905/:
    python3 tools/testlab/realtime.py --astrbot /path/to/AstrBot-master --model host \
        --export ~/Downloads/group_export.json --questions questions.json
    python3 tools/testlab/realtime.py ... --model env --base-url https://api.deepseek.com \
        --main-model deepseek-flash            # key from GROUPBOT_MODEL_API_KEY
    python3 tools/testlab/realtime.py ... --model mock   # plumbing only, no model
    python3 tools/testlab/realtime.py --report-only .sandbox/realtime/<run>/results.json

Outputs go to .sandbox/realtime/<run>/ (ignored by Git): results.json and
report.html. Both contain chat text; do not commit them.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TZ = ZoneInfo("Asia/Shanghai")
LEVELS = {1: "近期直述", 2: "远期直述", 3: "关联推理", 4: "更新与时间", 5: "陷阱与拒答"}
DECLINE = [
    "没有", "没提", "没找到", "没看到", "没查到", "没记录", "没说", "没定", "没确定",
    "不知道", "不清楚", "不确定", "未提", "未确定", "未定", "无法确定", "没有相关",
]
DISTANCE_BUCKETS = [(0, 40, "≤40 条"), (41, 150, "41–150"), (151, 400, "151–400"), (401, 10**9, "400 条以上")]


# --------------------------------------------------------------------- export
def render_elements(elements):
    """Text as the plugin would store it, plus mentions and the quoted message.

    The plugin keeps only plain-text segments: other members' @ are dropped and
    files, images, cards and forwarded chats carry no readable text. They become
    short placeholders here, because the replay transport sends text only.
    """
    text, mentions, reply = [], [], None
    placeholder = {
        "image": "[图片]", "face": "[表情]", "market_face": "[表情]", "file": "[文件]",
        "video": "[视频]", "record": "[语音]", "json": "[分享卡片]", "forward": "[转发的聊天记录]",
    }
    for e in elements:
        kind, data = e.get("type"), e.get("data", {})
        if kind == "text":
            text.append(data.get("text", ""))
        elif kind == "at":
            if data.get("uin") or data.get("uid"):
                mentions.append({"sender": str(data.get("uin") or data.get("uid")), "name": data.get("name", "")})
        elif kind == "reply":
            reply = str(data.get("referencedMessageId") or data.get("messageId") or "") or None
        elif kind in placeholder:
            text.append(placeholder[kind])
        # markdown duplicates a text element; inline keyboards carry no text.
    return "".join(text).strip(), mentions, reply


def convert_export(path):
    """QQChatExporter JSON -> replay rows in time order, with what was skipped."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    messages = sorted(data["messages"], key=lambda m: m["timestamp"])
    original = {m["id"]: i + 1 for i, m in enumerate(data["messages"])}
    rows, skipped, seen = [], {"system": 0, "recalled": 0, "empty": 0}, set()
    for m in messages:
        if m.get("system") or m.get("type") == "system":
            skipped["system"] += 1
            continue
        if m.get("recalled"):
            skipped["recalled"] += 1  # the original text is not in the export
            continue
        text, mentions, reply = render_elements(m.get("content", {}).get("elements", []))
        if not text:
            text = "[机器人消息]" if any(
                e.get("type") in {"markdown", "inline_keyboard"} for e in m["content"].get("elements", [])
            ) else ""
        if not text:
            skipped["empty"] += 1
            continue
        sender = m["sender"]
        row = {
            "sender": str(sender.get("uin") or sender.get("uid")),
            "name": sender.get("groupCard") or sender.get("name") or str(sender.get("uin")),
            "text": text[:20000],
            "timestamp": datetime.fromtimestamp(m["timestamp"] / 1000, TZ).isoformat(),
            "native_id": str(m["id"]),
            "export_index": original[m["id"]],
        }
        if mentions:
            row["mentions"] = mentions
        if reply and reply in seen:
            row["reply_to"] = reply
        rows.append(row)
        seen.add(row["native_id"])
    info = {
        "group": data.get("chatInfo", {}).get("name", ""),
        "exported": len(data["messages"]),
        "replayed": len(rows),
        "skipped": skipped,
        "first": rows[0]["timestamp"] if rows else None,
        "last": rows[-1]["timestamp"] if rows else None,
    }
    return rows, info


# ------------------------------------------------------------------ questions
def load_questions(path, rows):
    """Questions keyed by the export index they follow; facts must exist."""
    qs = json.loads(Path(path).read_text(encoding="utf-8"))
    indices = {r["export_index"] for r in rows}
    last = max(indices) if indices else 0
    ids = set()
    for q in qs:
        missing = {"id", "after", "level", "ask", "facts", "expect"} - set(q)
        if missing:
            raise ValueError(f"问题 {q.get('id')} 缺少字段：{sorted(missing)}")
        if q["id"] in ids:
            raise ValueError(f"问题编号重复：{q['id']}")
        ids.add(q["id"])
        if q["level"] not in LEVELS or not 1 <= q["after"] <= last:
            raise ValueError(f"问题 {q['id']} 的 level 或 after 无效")
        if not q["expect"] or q["expect"][0].get("since", 1) > q["after"]:
            raise ValueError(f"问题 {q['id']} 在提问时没有生效的标准答案")
        for v in q["expect"]:
            if not v.get("all") or not all(isinstance(g, list) and g for g in v["all"]):
                raise ValueError(f"问题 {q['id']} 的 all 必须是非空的同义词组列表")
    return sorted(qs, key=lambda q: (q["after"], q["id"]))


def norm(text):
    text = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"\s+", "", text)


def expand(alternatives):
    out = []
    for a in alternatives:
        out.extend(DECLINE if a == "$DECLINE" else [a])
    return out


def judge(question, answer, index):
    """Program scoring against the variant in force when the question was asked.

    Every synonym group in `all` must be matched by at least one alternative, and
    nothing in `none` may appear. Matching is on NFKC-normalised, space-free text.
    """
    variant = [v for v in question["expect"] if v.get("since", 1) <= index][-1]
    text = norm(answer)
    missing = [g for g in variant["all"] if not any(norm(a) in text for a in expand(g))]
    wrong = [w for w in expand(variant.get("none", [])) if norm(w) in text]
    return {"ok": bool(text) and not missing and not wrong, "missing": missing, "wrong": wrong}


# ---------------------------------------------------------------------- study
class Study:
    def __init__(self, worker, rows, questions, args):
        self.w, self.rows, self.args = worker, rows, args
        self.queue_after = {}
        for q in questions:
            self.queue_after.setdefault(q["after"], []).append(q)
        self.ordinal = {r["export_index"]: i + 1 for i, r in enumerate(rows)}
        self.queue, self.asking, self.records = [], None, []
        self.delivered, self.last_index = 0, 0
        self.anchor = None
        self.next_tick = None
        self.ticks = 0
        self.real_started = time.time()

    def vnow(self):
        virtual, real = self.anchor
        return virtual + timedelta(seconds=time.monotonic() - real)

    async def set_clock(self, at):
        self.anchor = (at.astimezone(timezone.utc), time.monotonic())
        await self.w.api("POST", "/clock", {"now": at.isoformat()})

    async def tick(self, busy):
        await self.w.api("POST", "/tick", {})
        self.ticks += 1
        # Production ticks every 30 s; while idle nothing changes but time, so the
        # replay checks less often.
        self.next_tick = self.vnow() + timedelta(seconds=30 if busy else self.args.idle_step)

    async def poll_question(self):
        if self.asking:
            q, mid, asked = self.asking
            done = (await self.w.api("GET", f"/done/{mid}"))["done"]
            if done is None:
                if time.monotonic() - asked["mono"] > self.args.answer_timeout:
                    asked["timeout"] = True
                else:
                    return
            self.finish(q, mid, asked, done)
            self.asking = None
        if not self.asking and self.queue:
            q = self.queue.pop(0)
            at = self.vnow()
            mid = await self.w.deliver(
                {
                    "sender": "realtime-asker",
                    "name": self.args.asker,
                    "text": q["ask"],
                    "at": True,
                    "timestamp": at.isoformat(),
                },
                question=True,
            )
            self.asking = (q, mid, {
                "mono": time.monotonic(), "real": time.time(), "virtual": at.isoformat(),
                "delivered": self.delivered, "index": self.last_index,
            })

    def finish(self, q, mid, asked, done):
        replies = [x for x in self.w.timeline if x["role"] == "assistant" and x.get("request_id") == str(mid)]
        answer = "\n".join(x["text"] for x in replies)
        facts = [self.ordinal[i] for i in q["facts"] if i in self.ordinal]
        verdict = judge(q, answer, asked["index"])
        self.records.append({
            "id": q["id"], "level": q["level"], "level_name": LEVELS[q["level"]], "kind": q.get("kind", ""),
            "after": q["after"], "asked_index": asked["index"], "delivered": asked["delivered"],
            "distance": asked["delivered"] - max(facts) if facts else None,
            "ask": q["ask"], "answer": answer, "replies": len(replies),
            "ok": verdict["ok"], "missing": verdict["missing"], "wrong": verdict["wrong"],
            "review": q.get("review", ""), "virtual_at": asked["virtual"],
            "real_asked": asked["real"], "real_done": time.time(),
            "seconds": round(time.monotonic() - asked["mono"], 2),
            "error": (done or {}).get("error") or ("AnswerTimeout" if asked.get("timeout") else None),
            "message_id": mid,
        })
        mark = "✓" if verdict["ok"] else "✗"
        print(f"  {mark} {q['id']} L{q['level']} @{asked['delivered']}条 {self.records[-1]['seconds']}s  {q['ask']}", flush=True)

    async def advance(self, target):
        """Let virtual time reach `target`, flowing while busy and jumping while idle."""
        while True:
            await self.poll_question()
            now = self.vnow()
            hold = self.args.hold and (self.asking or self.queue)
            if now >= target and not hold:
                return
            state = await self.w.api("GET", "/busy")
            busy = state["busy"] or bool(self.asking)
            if now >= self.next_tick:
                await self.tick(busy)
            if busy:
                await asyncio.sleep(0.25)
                continue
            if now < target:
                await self.set_clock(min(target, self.next_tick))
            else:
                await asyncio.sleep(0.25)  # holding for an answer that has not started yet

    async def run(self):
        start = datetime.fromisoformat(self.rows[0]["timestamp"]) - timedelta(minutes=1)
        await self.set_clock(start)
        self.next_tick = start
        total = len(self.rows)
        for row in self.rows:
            await self.advance(datetime.fromisoformat(row["timestamp"]))
            await self.w.deliver({k: v for k, v in row.items() if k != "export_index"})
            self.delivered += 1
            self.last_index = row["export_index"]
            if self.delivered % 50 == 0 or self.delivered == total:
                print(f"已投递 {self.delivered}/{total}（虚拟时间 {self.vnow().astimezone(TZ):%m-%d %H:%M}）", flush=True)
            for q in self.queue_after.get(row["export_index"], []):
                self.queue.append(q)
            await self.poll_question()
        while self.queue or self.asking:
            await self.advance(self.vnow() + timedelta(seconds=1))


def summarize(records):
    def acc(rows):
        return {"n": len(rows), "correct": sum(r["ok"] for r in rows),
                "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 4) if rows else None}

    by_cp = {}
    for r in records:
        by_cp.setdefault(r["after"], []).append(r)
    checkpoints = []
    for after, rows in sorted(by_cp.items()):
        item = {"after": after, "delivered": min(r["delivered"] for r in rows), **acc(rows)}
        item["levels"] = {lv: acc([r for r in rows if r["level"] == lv]) for lv in LEVELS if any(r["level"] == lv for r in rows)}
        checkpoints.append(item)
    distance = []
    for lo, hi, label in DISTANCE_BUCKETS:
        rows = [r for r in records if r["distance"] is not None and r["level"] != 5 and lo <= r["distance"] <= hi]
        distance.append({"bucket": label, **acc(rows)})
    seconds = [r["seconds"] for r in records]
    return {
        "overall": acc(records),
        "levels": {lv: {"name": LEVELS[lv], **acc([r for r in records if r["level"] == lv])} for lv in LEVELS},
        "checkpoints": checkpoints,
        "distance": distance,
        "answer_seconds_p50": statistics.median(seconds) if seconds else None,
        "answer_seconds_max": max(seconds) if seconds else None,
    }


# --------------------------------------------------------------------- report
def svg_line(points, series, width=680, height=280, ymax=1.0, ylabel="正确率", xlabel="机器人已看到的群消息条数"):
    """Small hand-drawn line chart; points: [(x, {series: y or None})]."""
    left, right, top, bottom = 52, 20, 18, 46
    xs = [p[0] for p in points] or [0, 1]
    x0, x1 = 0, max(xs) * 1.04 or 1
    def sx(x): return left + (x - x0) / (x1 - x0) * (width - left - right)
    def sy(y): return top + (1 - y / ymax) * (height - top - bottom)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(ylabel)}随{html.escape(xlabel)}的变化">']
    for k in range(6):
        y = ymax * k / 5
        out.append(f'<line class="grid" x1="{left}" y1="{sy(y):.1f}" x2="{width-right}" y2="{sy(y):.1f}"/>')
        out.append(f'<text class="tick" x="{left-8}" y="{sy(y)+4:.1f}" text-anchor="end">{y*100:.0f}%</text>')
    step = next((c for c in (10, 20, 50, 100, 200, 500, 1000, 2000, 5000) if max(xs) / c <= 8), 10000)
    for x in range(0, int(x1) + 1, step):
        out.append(f'<text class="tick" x="{sx(x):.1f}" y="{height-bottom+18}" text-anchor="middle">{x}</text>')
    out.append(f'<text class="tick" x="{(left+width-right)/2:.0f}" y="{height-8}" text-anchor="middle">{html.escape(xlabel)}</text>')
    for name, cls in series:
        pts = [(sx(x), sy(v[name])) for x, v in points if v.get(name) is not None]
        if len(pts) > 1:
            out.append(f'<polyline class="{cls}" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}"/>')
        for a, b in pts:
            out.append(f'<circle class="{cls}-dot" cx="{a:.1f}" cy="{b:.1f}" r="3.5"/>')
    out.append("</svg>")
    return "".join(out)


def svg_bars(items, width=420, height=240, label="正确率"):
    """Horizontal bars: items [(label, accuracy or None, n)]."""
    left, right, row = 104, 92, 30
    height = 16 + row * len(items)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">']
    span = width - left - right
    for i, (name, value, n) in enumerate(items):
        y = 8 + i * row
        out.append(f'<text class="tick" x="{left-10}" y="{y+16}" text-anchor="end">{html.escape(name)}</text>')
        out.append(f'<rect class="track" x="{left}" y="{y+4}" width="{span}" height="16" rx="3"/>')
        if value is not None:
            out.append(f'<rect class="bar" x="{left}" y="{y+4}" width="{span*value:.1f}" height="16" rx="3"/>')
        text = f"{value*100:.0f}%  (n={n})" if value is not None else "无题"
        out.append(f'<text class="value" x="{left+span+8}" y="{y+16}">{text}</text>')
    out.append("</svg>")
    return "".join(out)


def render_report(results):
    s, meta = results["summary"], results["meta"]
    levels_present = [lv for lv in LEVELS if s["levels"][lv]["n"]]
    points = [(c["delivered"], {"all": c["accuracy"], **{f"L{lv}": c["levels"].get(lv, {}).get("accuracy") for lv in LEVELS}}) for c in s["checkpoints"]]
    near = {1, 2}
    near_points = []
    for c in s["checkpoints"]:
        rows = [r for r in results["questions"] if r["after"] == c["after"]]
        recall = [r for r in rows if r["level"] in near]
        hard = [r for r in rows if r["level"] in {3, 4, 5}]
        near_points.append((c["delivered"], {
            "recall": sum(r["ok"] for r in recall) / len(recall) if recall else None,
            "hard": sum(r["ok"] for r in hard) / len(hard) if hard else None,
        }))
    line_all = svg_line(points, [("all", "s-all")])
    line_split = svg_line(near_points, [("recall", "s-recall"), ("hard", "s-hard")])
    level_bars = svg_bars([(f"L{lv} {LEVELS[lv]}", s["levels"][lv]["accuracy"], s["levels"][lv]["n"]) for lv in LEVELS], label="各难度正确率")
    distance_bars = svg_bars([(d["bucket"], d["accuracy"], d["n"]) for d in s["distance"]], label="按事实距离的正确率")
    rows_html = []
    for r in results["questions"]:
        cls = "ok" if r["ok"] else "bad"
        shown = lambda g: "/".join("否认类说法" if a == "$DECLINE" else a for a in g)  # noqa: E731
        why = "" if r["ok"] else ("缺：" + "；".join(shown(g) for g in r["missing"]) if r["missing"] else "") + (" 出现错误说法：" + "、".join(r["wrong"]) if r["wrong"] else "")
        rows_html.append(
            f'<tr class="{cls}"><td>{r["delivered"]}</td><td>L{r["level"]}</td><td>{r["distance"] if r["distance"] is not None else "–"}</td>'
            f'<td>{html.escape(r["ask"])}</td><td><details><summary>{"✓ 正确" if r["ok"] else "✗ " + html.escape(why or "错误")}</summary>'
            f'<div class="answer">{html.escape(r["answer"] or "（无回复）")}</div>{"<p class=note>需人工复核：" + html.escape(r["review"]) + "</p>" if r.get("review") else ""}</details></td>'
            f'<td>{r["seconds"]}</td></tr>'
        )
    o = s["overall"]
    usage = results.get("usage", {})
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时记忆研究</title><style>
:root{{--bg:#f5f7fa;--fg:#17202b;--muted:#5b6678;--line:#d4dbe4;--card:#fff;--a:#2361a5;--b:#0f7f67;--c:#b2640a;--good:#1d7a45;--bad:#b3362c;--track:#e6ebf1}}
@media (prefers-color-scheme:dark){{:root{{--bg:#10141a;--fg:#e4e9ef;--muted:#9aa6b6;--line:#2e3846;--card:#171f29;--a:#74abe8;--b:#4fc4a4;--c:#f0aa44;--good:#5cc98a;--bad:#f07a6e;--track:#232d3a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.7 "PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;margin:0}}
main{{max-width:980px;margin:0 auto;padding:28px 18px 60px;display:grid;gap:26px}}
h1{{font-size:1.6rem;margin:0}} h2{{font-size:1.15rem;margin:0 0 6px}} p{{margin:0}}
.muted{{color:var(--muted)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:0}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.stat b{{display:block;font-size:1.5rem}} .stat span{{color:var(--muted);font-size:.85rem}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}
svg{{width:100%;height:auto;display:block}} svg text{{font-size:11px;fill:var(--muted)}} svg .value{{fill:var(--fg)}}
.grid{{stroke:var(--line)}} .track{{fill:var(--track)}} .bar{{fill:var(--b)}}
.s-all{{fill:none;stroke:var(--a);stroke-width:2.5}} .s-all-dot{{fill:var(--a)}}
.s-recall{{fill:none;stroke:var(--b);stroke-width:2.5}} .s-recall-dot{{fill:var(--b)}}
.s-hard{{fill:none;stroke:var(--c);stroke-width:2.5;stroke-dasharray:5 4}} .s-hard-dot{{fill:var(--c)}}
.legend span{{margin-right:16px;font-size:.85rem}} .k{{display:inline-block;width:18px;height:3px;vertical-align:middle;margin-right:6px}}
table{{border-collapse:collapse;width:100%;font-size:.88rem}} th,td{{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top}}
th{{color:var(--muted);font-weight:600;white-space:nowrap}} .wrap{{overflow-x:auto}} tr.ok summary{{color:var(--good)}} tr.bad summary{{color:var(--bad)}}
.answer{{white-space:pre-wrap;margin-top:6px;color:var(--fg)}} .note{{color:var(--c);font-size:.85rem}}
</style></head><body><main>
<header><h1>实时记忆研究：{html.escape(meta.get("group",""))}</h1>
<p class="muted">{meta["replayed"]} 条群消息逐条实时回放，{o["n"]} 个问题按节点插入。模型：{html.escape(meta["model"])}。{"（假模型：只验证流程，正确率没有意义）" if meta["model_mode"] == "mock" else ""}</p></header>
<section class="stats">
<div class="card stat"><b>{(o["accuracy"] or 0)*100:.0f}%</b><span>总体正确率（{o["correct"]}/{o["n"]}）</span></div>
<div class="card stat"><b>{s["answer_seconds_p50"] or 0:.1f} 秒</b><span>回答耗时中位数</span></div>
<div class="card stat"><b>{usage.get("calls", 0)}</b><span>模型调用总数</span></div>
<div class="card stat"><b>{len(results.get("memory_jobs", []))}</b><span>后台通读／整合次数</span></div>
</section>
<section class="card"><h2>正确率随消息数量的变化</h2>
<p class="muted">横轴是提问时机器人已经看到的群消息条数，每个点是一个提问节点的全部问题。</p>{line_all}</section>
<section class="card"><h2>直接回忆 与 推理／更新／陷阱</h2>
<p class="legend"><span><i class="k" style="background:var(--b)"></i>L1–L2 直接回忆</span><span><i class="k" style="background:var(--c)"></i>L3–L5 推理、更新与陷阱</span></p>{line_split}</section>
<section class="grid2"><div class="card"><h2>各难度正确率</h2>{level_bars}</div>
<div class="card"><h2>事实距离与正确率</h2><p class="muted">事实出现到提问之间隔了多少条消息；不含 L5。</p>{distance_bars}</div></section>
<section class="card"><h2>逐题结果</h2><p class="muted">程序按标准答案的关键词判分，点开看完整回答。标注“需人工复核”的题目，自动判分可能不准。</p>
<div class="wrap"><table><thead><tr><th>已看条数</th><th>难度</th><th>距离</th><th>问题</th><th>回答</th><th>秒</th></tr></thead><tbody>{"".join(rows_html)}</tbody></table></div></section>
<section class="card"><h2>运行条件</h2><p class="muted">{html.escape(json.dumps({k: meta[k] for k in ("model_mode","hold","virtual_first","virtual_last","real_minutes","skipped","plugin_overrides")}, ensure_ascii=False))}</p></section>
</main></body></html>"""


def finalize(study, worker, meta):
    calls = worker.snapshot.get("calls", [])
    used = [c["usage"] for c in calls if c.get("usage")]
    busy = getattr(study, "final_busy", {})
    tools = worker.snapshot.get("tools", [])
    for r in study.records:
        r["tools"] = [t["tool"] for t in tools if t.get("message_id") == str(r["message_id"]) and t.get("phase") == "start"]
    results = {
        "meta": meta,
        "questions": study.records,
        "summary": summarize(study.records),
        "memory_jobs": busy.get("memory_jobs", []),
        "usage": {
            "calls": len(calls),
            "failed": sum(c["status"] == "failed" for c in calls),
            "models": sorted({c["model"] for c in calls}),
            "input_uncached": sum(u.get("input_other") or 0 for u in used),
            "input_cached": sum(u.get("input_cached") or 0 for u in used),
            "output": sum(u.get("output") or 0 for u in used),
            "denied": worker.snapshot.get("budget", {}).get("denied", 0),
        },
        "tools": worker.snapshot.get("tools", []),
    }
    return results


async def main_async(args):
    sys.path.insert(0, str(HERE))
    from runtime import Manager, Worker  # noqa: E402  (AstrBot venv: aiohttp, websockets)

    rows, info = convert_export(args.export)
    questions = load_questions(args.questions, rows)
    if args.limit:
        rows = [r for r in rows if r["export_index"] <= args.limit]
        questions = [q for q in questions if q["after"] <= rows[-1]["export_index"]]
        info["replayed"] = len(rows)
    out = Path(args.out or ROOT / ".sandbox/realtime" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    overrides = {}
    pilot = ROOT / "config/qq.json"
    if args.mirror_pilot and pilot.exists():
        business = json.loads(pilot.read_text(encoding="utf-8"))
        overrides = {"memory": business.get("memory", {}), "dialogue": business.get("dialogue", {})}
    env_settings = None
    if args.model == "env":
        env_settings = {
            "base_url": args.base_url,
            "key_env": args.key_env,
            "models": {"main": args.main_model, "fast": args.fast_model, "strong": args.strong_model,
                       "reply_fast": args.fast_model, "reply_deep": args.main_model},
        }
    manager = Manager(args.astrbot, out / "runs", "host" if args.model == "host" else args.model, 1, env_settings)
    spec = {
        "id": "realtime-" + datetime.now().strftime("%H%M%S"),
        "title": "实时记忆研究",
        "mode": "realtime",
        "rows": [],
        "model_limit": args.max_calls,
        "integrate": False,
        "realtime": True,
        "plugin_overrides": overrides,
    }
    worker = Worker(manager, spec)
    print(f"消息 {info['replayed']} 条（跳过 {info['skipped']}），问题 {len(questions)} 个；输出 {out}", flush=True)
    started = time.time()
    study = Study(worker, rows, questions, args)
    try:
        await worker.prepare()
        await study.run()
    finally:
        try:
            study.final_busy = await worker.api("GET", "/busy")
        except Exception:  # noqa: BLE001
            study.final_busy = {}
        await worker.close()  # refreshes worker.snapshot one last time
        meta = {
            **info, "model_mode": args.model, "hold": args.hold,
            "model": ", ".join(sorted({c["model"] for c in worker.snapshot.get("calls", [])})) or args.model,
            "virtual_first": rows[0]["timestamp"], "virtual_last": rows[-1]["timestamp"],
            "real_minutes": round((time.time() - started) / 60, 1), "plugin_overrides": overrides,
            "limit": args.limit, "questions_file": str(args.questions),
        }
        results = finalize(study, worker, meta)
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "report.html").write_text(render_report(results), encoding="utf-8")
        o = results["summary"]["overall"]
        print(f"完成：{o['correct']}/{o['n']} 正确；报告 {out / 'report.html'}", flush=True)


def parse(argv=None):
    p = argparse.ArgumentParser(description="逐条实时回放真实群聊，按节点提问并评估记忆")
    p.add_argument("--astrbot", default=os.environ.get("GROUPBOT_ASTRBOT", ""))
    p.add_argument("--model", choices=["host", "env", "mock"], default="host")
    p.add_argument("--export", help="QQChatExporter 导出的群聊 JSON")
    p.add_argument("--questions", help="问题与标准答案 JSON")
    p.add_argument("--out", default="", help="输出目录，默认 .sandbox/realtime/<时间>")
    p.add_argument("--max-calls", type=int, default=900, help="整次运行的模型调用上限")
    p.add_argument("--hold", action="store_true", help="提问时暂停后续消息，直到回答完成")
    p.add_argument("--limit", type=int, default=0, help="只回放导出里前 N 条（按原始序号）")
    p.add_argument("--idle-step", type=int, default=120, help="空闲时虚拟时钟每步前进秒数")
    p.add_argument("--answer-timeout", type=float, default=300)
    p.add_argument("--asker", default="提问的群友", help="提问者在群里的昵称")
    p.add_argument("--no-mirror-pilot", dest="mirror_pilot", action="store_false",
                   help="不读取 config/qq.json 里的记忆与对话参数")
    p.add_argument("--base-url", default="https://api.deepseek.com")
    p.add_argument("--key-env", default="GROUPBOT_MODEL_API_KEY")
    p.add_argument("--main-model", default="deepseek-flash", help="env 模式：宿主主模型与深度回复")
    p.add_argument("--fast-model", default="deepseek-flash", help="env 模式：抽取、通读与日常回复")
    p.add_argument("--strong-model", default="deepseek-flash", help="env 模式：日终整合")
    p.add_argument("--report-only", default="", help="只根据已有 results.json 重新生成图表")
    return p.parse_args(argv)


def main():
    args = parse()
    if args.report_only:
        path = Path(args.report_only)
        results = json.loads(path.read_text(encoding="utf-8"))
        results["summary"] = summarize(results["questions"])
        path.with_name("report.html").write_text(render_report(results), encoding="utf-8")
        print(path.with_name("report.html"))
        return
    if not (args.astrbot and args.export and args.questions):
        raise SystemExit("需要 --astrbot、--export 和 --questions")
    host = Path(args.astrbot).expanduser().resolve()
    python = host / ".venv/bin/python"
    if not python.is_file() or not (host / "main.py").is_file():
        raise SystemExit("AstrBot 路径必须包含 main.py 和 .venv/bin/python")
    if Path(sys.prefix).resolve() != (host / ".venv").resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    args.astrbot = str(host)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
