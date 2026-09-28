"""Install a tested plugin with backups. Stop AstrBot before invoking this script."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    host = args.host.expanduser().resolve()
    config = args.config.expanduser().resolve()
    destination = host / "data/plugins/astrbot_plugin_groupsecretary"
    if not (host / "main.py").is_file() or not destination.is_dir():
        raise ValueError("Expected an existing AstrBot installation with this plugin")
    settings = json.loads(config.read_text())
    database = (config.parent / settings["database"]).resolve()
    backup = (
        host
        / "data/groupsecretary_backups"
        / ("v0.2.1-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    )
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    shutil.copytree(
        destination,
        backup / "plugin",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(config, backup / "pilot-config.json")
    os.chmod(backup / "pilot-config.json", 0o600)
    options = host / "data/config/astrbot_plugin_groupsecretary_config.json"
    shutil.copy2(options, backup / "plugin-options.json")
    os.chmod(backup / "plugin-options.json", 0o600)
    if database.exists():
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup / "qq.sqlite3") as target:
                source.backup(target)
        os.chmod(backup / "qq.sqlite3", 0o600)
    manifest = {
        "host": str(host),
        "plugin": str(destination),
        "config": str(config),
        "database": str(database),
        "restore": "Stop AstrBot, restore plugin and config from this directory, then optionally restore qq.sqlite3 including removing its stale WAL/SHM files. Restoring DB loses later conversations.",
    }
    (backup / "restore.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Copy source files only; runtime configuration and credentials remain untouched.
    for source in (ROOT / "plugin").rglob("*"):
        if (
            source.is_file()
            and "__pycache__" not in source.parts
            and source.suffix != ".pyc"
        ):
            target = destination / source.relative_to(ROOT / "plugin")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    print(
        json.dumps(
            {
                "installed": "v0.2.1",
                "backup": str(backup),
                "database_backup": str(backup / "qq.sqlite3"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
