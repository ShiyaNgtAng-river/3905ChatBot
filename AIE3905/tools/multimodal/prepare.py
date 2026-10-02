"""Download a public offline ASR model; no API account is required."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path(".sandbox/groupmedia"))
    parser.add_argument("--model", choices=["base", "small"], default="base")
    parser.add_argument(
        "--revision",
        default="main",
        help="Use a commit from model-manifest.json to reproduce weights",
    )
    args = parser.parse_args()
    os.umask(0o077)
    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(home / "hf-home"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import HfApi, snapshot_download

    repo = "Systran/faster-whisper-" + args.model
    revision = HfApi().model_info(repo, revision=args.revision).sha
    model = Path(
        snapshot_download(
            repo,
            revision=revision,
            local_dir=home / "models" / repo.split("/")[1],
            allow_patterns=[
                "config.json",
                "model.bin",
                "tokenizer.json",
                "vocabulary.*",
            ],
        )
    )
    manifest = {
        "repo": repo,
        "revision": revision,
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in model.iterdir()
            if p.is_file()
        },
    }
    (home / "model-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    cfg = home / "config.json"
    if not cfg.exists():
        cfg.write_text(
            json.dumps(
                {
                    "ocr": {"backend": "apple_vision"},
                    "describe": {"backend": "disabled"},
                    "transcribe": {
                        "backend": "faster_whisper",
                        "model_path": str(model),
                        "language": "zh",
                    },
                    "generate": {"backend": "disabled"},
                },
                indent=2,
            )
            + "\n"
        )
    print(
        json.dumps(
            {
                "model_path": str(model),
                "config": str(cfg),
                "revision": revision,
                "note": "Existing config is preserved; update model_path there if changing model.",
            }
        )
    )


if __name__ == "__main__":
    main()
