"""Submit a JSON/JSONL batch to the running local test workbench."""

import argparse
import json
from pathlib import Path
import time
import urllib.request

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("file", type=Path)
p.add_argument("--copies", type=int, default=2)
p.add_argument("--title", default="命令行回放")
p.add_argument("--model-limit", type=int, default=80)
p.add_argument("--integrate", action="store_true")
p.add_argument(
    "--access",
    type=Path,
    default=Path(__file__).resolve().parents[2] / ".sandbox/testlab/access.json",
)
a = p.parse_args()
access = json.loads(a.access.read_text())
raw = a.file.read_text().strip()
rows = (
    json.loads(raw)
    if raw.startswith("[")
    else [json.loads(line) for line in raw.splitlines() if line.strip()]
)


def api(path, body=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{access['port']}/api/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + access["token"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


ids = api(
    "runs",
    {
        "title": a.title,
        "rows": rows,
        "copies": a.copies,
        "model_limit": a.model_limit,
        "integrate": a.integrate,
    },
)["ids"]
print("Submitted: " + ", ".join(ids), flush=True)
previous = None
while True:
    runs = [r for r in api("runs")["runs"] if r["id"] in ids]
    status = [(r["id"], r["state"], r["progress"], r["calls"]) for r in runs]
    if status != previous:
        print(json.dumps(status, ensure_ascii=False), flush=True)
        previous = status
    if all(
        r["state"] in {"completed", "failed", "stopped", "interrupted"} for r in runs
    ):
        raise SystemExit(
            0
            if all(
                r["state"] == "completed" and r["checks_passed"] == r["checks_total"]
                for r in runs
            )
            else 1
        )
    time.sleep(2)
