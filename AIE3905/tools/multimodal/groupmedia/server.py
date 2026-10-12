from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading

from .service import CAPABILITIES, MAX_FILE, MediaError

MAX_REQUEST = MAX_FILE * 4 // 3 + 20000


def make_server(service, token, port=8792):
    if len(token) < 24:
        raise ValueError("Service token must have at least 24 characters")
    gate = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log prompts, URLs, input media or bearer tokens.

        def setup(self):
            super().setup()
            self.connection.settimeout(240)

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            expected = "Bearer " + token
            if not hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), expected.encode()
            ):
                self.send_json(
                    401,
                    {
                        "ok": False,
                        "error": {
                            "code": "unauthorized",
                            "message": "需要服务访问令牌。",
                        },
                    },
                )
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path != "/api/capabilities":
                self.send_json(404, {"ok": False, "error": {"code": "not_found"}})
                return
            self.send_json(200, service.capabilities())

        def do_POST(self):
            if not self.authorized():
                return
            operation = self.path.removeprefix("/api/")
            if not self.path.startswith("/api/") or operation not in CAPABILITIES:
                self.send_json(404, {"ok": False, "error": {"code": "not_found"}})
                return
            if not gate.acquire(blocking=False):
                self.send_json(
                    429,
                    {
                        "ok": False,
                        "error": {
                            "code": "busy",
                            "message": "媒体服务繁忙，请稍后再试。",
                        },
                    },
                )
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise MediaError("invalid_request", "不支持分块请求。")
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise MediaError("invalid_request", "请求长度无效。") from None
                if not 0 < size <= MAX_REQUEST:
                    raise MediaError("too_large", "请求为空或过大。", 413)
                try:
                    request = json.loads(self.rfile.read(size))
                except (ValueError, UnicodeError):
                    raise MediaError("invalid_request", "请求不是有效 JSON。") from None
                result = service.process(operation, request)
                self.send_json(200, result)
            except MediaError as exc:
                self.send_json(
                    exc.status,
                    {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send_json(
                    500,
                    {
                        "ok": False,
                        "error": {
                            "code": "internal_error",
                            "message": "媒体处理未完成。",
                        },
                    },
                )
            finally:
                gate.release()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def access_file(home, port):
    path = home / "access.json"
    if path.exists():
        token = json.loads(path.read_text())["token"]
    else:
        token = secrets.token_urlsafe(32)
    path.write_text(
        json.dumps({"url": f"http://127.0.0.1:{port}", "token": token}, indent=2) + "\n"
    )
    path.chmod(0o600)
    return token
