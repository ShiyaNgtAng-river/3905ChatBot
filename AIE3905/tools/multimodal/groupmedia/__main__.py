from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path

from .service import MAX_FILE, MediaError, MediaService


def main():
    parser = argparse.ArgumentParser(
        description="Account-free local OCR/ASR and optional cloud adapters"
    )
    parser.add_argument(
        "operation",
        choices=["serve", "capabilities", "ocr", "describe", "transcribe", "generate"],
    )
    parser.add_argument("input", nargs="?", help="Media file; for generate, the prompt")
    parser.add_argument("--home", type=Path, default=Path(".sandbox/groupmedia"))
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--output", type=Path, help="Write JSON result to this private file"
    )
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    os.umask(0o077)
    cfg_path = args.config or args.home / "config.json"
    config = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    service = MediaService(config, args.home)
    try:
        if args.operation == "serve":
            from .server import access_file, make_server

            token = access_file(service.home, args.port)
            server = make_server(service, token, args.port)
            print(
                json.dumps(
                    {
                        "url": f"http://127.0.0.1:{args.port}",
                        "access_file": str(service.home / "access.json"),
                    }
                ),
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return
        if args.operation == "capabilities":
            result = service.capabilities()
        else:
            if not args.input:
                parser.error("input is required")
            request = {
                "source": {
                    "group": "local-cli",
                    "message_id": "cli-"
                    + hashlib.sha256(args.input.encode()).hexdigest()[:16],
                }
            }
            if args.operation == "generate":
                request["prompt"] = args.input
                if not args.output:
                    parser.error(
                        "generate requires --output, to avoid printing image bytes"
                    )
            else:
                p = Path(args.input).expanduser()
                if not p.is_file() or p.stat().st_size > MAX_FILE:
                    raise MediaError("invalid_media", "文件不存在或超过20MiB。")
                request["media"] = {
                    "mime": mimetypes.guess_type(p.name)[0]
                    or "application/octet-stream",
                    "base64": base64.b64encode(p.read_bytes()).decode(),
                }
            result = service.process(args.operation, request)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
            args.output.chmod(0o600)
            print(
                json.dumps(
                    {"ok": result.get("ok", True), "output": str(args.output)},
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except MediaError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    finally:
        service.close()


if __name__ == "__main__":
    main()
