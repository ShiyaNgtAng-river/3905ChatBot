"""Validate archive contents and manifests without exposing credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-config", type=Path)
    args = parser.parse_args()
    secrets = []
    if args.host_config:
        config = json.loads(args.host_config.read_text(encoding="utf-8-sig"))
        for source in config.get("provider_sources", []):
            value = source.get("key", [])
            secrets.extend(value if isinstance(value, list) else [value])
        for platform in config.get("platform", []):
            secrets.append(platform.get("secret", ""))
        secrets = [s.encode() for s in secrets if isinstance(s, str) and len(s) > 8]
    reports = []
    for archive in sorted((ROOT / "dist").glob("*v0.2.1.zip")):
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            names = z.namelist()
            prefix = names[0].split("/")[0] + "/"
            manifest = z.read(prefix + "MANIFEST.sha256").decode().splitlines()
            assert len(manifest) == len(names) - 1
            for line in manifest:
                expected, rel = line.split("  ", 1)
                raw = z.read(prefix + rel)
                assert hashlib.sha256(raw).hexdigest() == expected, rel
                parts = Path(rel).parts
                assert not any(
                    p
                    in {
                        ".venv",
                        ".git",
                        "__pycache__",
                        "data",
                        "groupsecretary_backups",
                    }
                    for p in parts
                ), rel
                assert rel != "config/qq.json" and not Path(rel).name.startswith(
                    ".env"
                ), rel
                assert not any(s in raw for s in secrets), "Credential found in " + rel
            with tempfile.TemporaryDirectory(
                prefix="groupbot-archive-check-"
            ) as folder:
                z.extractall(folder)
                root = Path(folder) / prefix
                plugin = (
                    root if archive.name.startswith("astrbot_") else root / "plugin"
                )
                assert "v0.2.1" in (plugin / "metadata.yaml").read_text()
                assert (plugin / "secretary/dialogue.py").is_file()
                assert 'version = "0.2.1"' in (plugin / "pyproject.toml").read_text()
        reports.append(
            {
                "file": archive.name,
                "entries": len(names),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "manifest": True,
                "no_runtime_state": True,
                "configured_credentials_absent": True,
            }
        )
    assert len(reports) == 2
    (ROOT / "dist/archive-validation-v0.2.1.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
