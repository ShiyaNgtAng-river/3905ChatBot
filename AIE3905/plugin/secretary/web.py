from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .sample import demo_messages
from .types import Actor, Message


class AuditServer:
    def __init__(self, engine, loop=None):
        self.engine = engine
        self.loop = loop or asyncio.get_running_loop()
        cfg = engine.config.web
        host = cfg.get("host", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("审核页只监听本机；远程访问请用 SSH 隧道或受控反向代理")
        self.tokens = []
        for entry in cfg.get("tokens", []):
            token = os.environ.get(entry["env"], "")
            if len(token) < 24:
                raise ValueError(
                    f"环境变量 {entry['env']} 需要至少24字符的随机访问令牌"
                )
            unknown = set(entry["groups"]) - set(engine.config.groups)
            if unknown:
                raise ValueError("Web 令牌包含未知群")
            self.tokens.append(
                (token, Actor(entry["user"], entry["groups"], bool(entry.get("admin"))))
            )
        if not self.tokens:
            raise ValueError("审核页未配置访问令牌，拒绝启动")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GroupSecretary"

            def log_message(self, *args):
                pass  # Never log query text, tokens, source snippets, or exception payloads.

            def send_data(self, code, body, ctype="application/json; charset=utf-8"):
                data = (
                    json.dumps(body, ensure_ascii=False).encode()
                    if not isinstance(body, bytes)
                    else body
                )
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
                )
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in {"/", "/app.js", "/style.css"}:
                    name = {
                        "/": "index.html",
                        "/app.js": "app.js",
                        "/style.css": "style.css",
                    }[path]
                    ctype = {
                        "/": "text/html; charset=utf-8",
                        "/app.js": "application/javascript; charset=utf-8",
                        "/style.css": "text/css; charset=utf-8",
                    }[path]
                    return self.send_data(
                        200, (Path(__file__).parent / "ui" / name).read_bytes(), ctype
                    )
                self.handle_api("GET")

            def do_POST(self):
                self.handle_api("POST")

            def handle_api(self, method):
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.startswith("Bearer ") else ""
                actor = next(
                    (a for t, a in owner.tokens if hmac.compare_digest(token, t)), None
                )
                if actor is None:
                    return self.send_data(401, {"error": "请输入有效访问令牌"})
                try:
                    parsed = urlsplit(self.path)
                    if method == "POST":
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 1_000_000:
                            raise ValueError("请求大小必须为1–1000000字节")
                        payload = json.loads(self.rfile.read(size))
                        if not isinstance(payload, dict):
                            raise ValueError("请求必须为 JSON 对象")
                    else:
                        payload = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                    fut = asyncio.run_coroutine_threadsafe(
                        owner.dispatch(method, parsed.path, payload, actor), owner.loop
                    )
                    result = fut.result(timeout=130)
                    self.send_data(200, result)
                except PermissionError:
                    self.send_data(403, {"error": "没有该群或该操作权限"})
                except (ValueError, KeyError, TypeError) as exc:
                    self.send_data(400, {"error": str(exc)[:200]})
                except Exception:
                    self.send_data(
                        503,
                        {"error": "处理暂未完成，请稍后查看状态；避免重复提交写入操作"},
                    )

        self.http = ThreadingHTTPServer((host, int(cfg.get("port", 8765))), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(
            target=self.http.serve_forever, name="secretary-web", daemon=True
        )

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_address[1]}"

    def start(self):
        self.thread.start()

    async def close(self):
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        await asyncio.to_thread(self.thread.join, 2)

    async def dispatch(self, method, path, p, actor):
        e = self.engine
        if method == "GET" and path == "/api/groups":
            return {
                "groups": [
                    {"key": k, "admin": actor.admin, "mode": e.config.mode}
                    for k in actor.groups
                    if e.config.groups[k].enabled
                    and e.config.groups[k].data_use_confirmed
                ]
            }
        key = p.get("group", "")
        actor.require(key)
        e.config.group(key)
        if method == "GET":
            if path == "/api/status":
                return e.status(actor, key)
            if path == "/api/items":
                return {"items": e.states(key)}
            if path == "/api/messages":
                return {"messages": e.store.recent(key, limit=100)}
            if path == "/api/message":
                row = e.store.message(key, p.get("id", ""))
                return {"message": row}
            if path == "/api/feedback":
                actor.require(key, admin=True)
                return {
                    "feedback": e.store.rows(
                        "SELECT * FROM feedback WHERE group_key=? ORDER BY id DESC LIMIT 100",
                        (key,),
                    )
                }
        if method == "POST":
            if path == "/api/query":
                return await e.query(
                    actor, key, p.get("question", ""), report_days=p.get("report_days")
                )
            if path == "/api/dialogue":
                return await e.dialogue(
                    actor,
                    key,
                    p.get("text", ""),
                    request_id=p.get("request_id"),
                    reply_to=p.get("reply_to", ""),
                )
            if path == "/api/command":
                return await e.command(actor, key, p.get("text", ""))
            if path == "/api/feedback":
                return {
                    "text": e.feedback(
                        actor,
                        key,
                        p.get("answer_id", ""),
                        p.get("kind", ""),
                        p.get("note", ""),
                    )
                }
            if path == "/api/ingest":
                actor.require(key, admin=True)
                m = dict(p.get("message", {}))
                m["group"] = key
                r = e.ingest(Message(**m))
                return {
                    "accepted": bool(r),
                    "new": bool(r and r["_new"]),
                    "id": r["uid"] if r else None,
                }
            if path == "/api/demo":
                actor.require(key, admin=True)
                if e.config.mode != "demo":
                    raise ValueError("只有 demo 模式可以导入虚构样例")
                # Fixed IDs are intentional: do not duplicate or silently rewrite the demo on repeat clicks.
                if e.store.one(
                    "SELECT 1 FROM messages WHERE group_key=? AND native_id=?",
                    (key, "demo-1"),
                ):
                    return {"text": "该演示已导入，未重复写入。"}
                for m in demo_messages(key):
                    e.ingest(m)
                await e.flush(key)
                return {
                    "text": "已导入9条完全虚构的演示消息；包含明确记事命令，不是自然语言模型效果展示。"
                }
        raise ValueError("未知接口")
