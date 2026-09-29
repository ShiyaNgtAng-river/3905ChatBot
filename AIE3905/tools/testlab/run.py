"""Launch the test workbench using an existing AstrBot Python environment."""

import argparse
import os
from pathlib import Path
import sys

p = argparse.ArgumentParser(add_help=False)
p.add_argument("--astrbot", default=os.environ.get("GROUPBOT_ASTRBOT", ""))
a, _ = p.parse_known_args()
if not a.astrbot:
    raise SystemExit(
        "请指定 --astrbot /path/to/AstrBot-master（或设置 GROUPBOT_ASTRBOT）"
    )
host = Path(a.astrbot).expanduser().resolve()
python = host / ".venv/bin/python"
if not python.is_file() or not (host / "main.py").is_file():
    raise SystemExit("AstrBot 路径必须包含 main.py 和 .venv/bin/python")
os.execv(
    str(python),
    [str(python), str(Path(__file__).with_name("server.py")), *sys.argv[1:]],
)
