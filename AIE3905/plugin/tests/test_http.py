import asyncio
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from secretary.config import Config
from secretary.engine import Engine
from secretary.providers import OpenAICompatible
from secretary.sample import demo_config
from secretary.web import AuditServer


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = demo_config()
        cfg["web"]["port"] = 0
        self.e = Engine(Config(cfg, self.temp.name))
        await self.e.start(maintenance=False)
        self.env = patch.dict(
            os.environ, {"GROUPBOT_ADMIN_TOKEN": "test-only-token-12345678901234567890"}
        )
        self.env.start()
        self.web = AuditServer(self.e)
        self.web.start()

    async def asyncTearDown(self):
        await self.web.close()
        await self.e.close()
        self.env.stop()
        self.temp.cleanup()

    async def request(self, path, body=None, token=True):
        def call():
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = (
                    "Bearer " + os.environ["GROUPBOT_ADMIN_TOKEN"]
                )
            req = urllib.request.Request(
                self.web.url + path,
                json.dumps(body).encode() if body is not None else None,
                headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, r.read().decode(), dict(r.headers)
            except urllib.error.HTTPError as r:
                return r.code, r.read().decode(), dict(r.headers)

        return await asyncio.to_thread(call)

    async def test_authorization_and_group_isolation(self):
        self.assertEqual(
            (await self.request("/api/items?group=demo", token=False))[0], 401
        )
        self.assertEqual((await self.request("/api/items?group=other"))[0], 403)
        self.assertEqual((await self.request("/api/groups"))[0], 200)

    async def test_ui_and_end_to_end_demo_query_and_feedback(self):
        code, html, headers = await self.request("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn("事项账本", html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual((await self.request("/api/demo", {"group": "demo"}))[0], 200)
        code, body, _ = await self.request(
            "/api/query", {"group": "demo", "question": "产品演示为什么改期"}
        )
        result = json.loads(body)
        self.assertEqual(code, 200)
        self.assertIn("数据还没准备齐", result["text"])
        self.assertTrue(result["sources"])
        code, body, _ = await self.request(
            "/api/feedback",
            {"group": "demo", "answer_id": result["id"], "kind": "有用"},
        )
        self.assertEqual(code, 200)

    async def test_remote_binding_and_missing_token_fail_closed(self):
        with patch.dict(os.environ, {"GROUPBOT_ADMIN_TOKEN": ""}):
            with self.assertRaises(ValueError):
                AuditServer(self.e)
        self.e.config.web["host"] = "0.0.0.0"
        with self.assertRaises(ValueError):
            AuditServer(self.e)

    async def test_dialogue_endpoint_requires_stable_id_and_uses_token_identity(self):
        self.assertEqual(
            (await self.request("/api/dialogue", {"group": "demo", "text": "hi"}))[0],
            400,
        )
        payload = {
            "group": "demo",
            "text": "hello",
            "request_id": "stable",
            "sender": "forged",
        }
        code, body, _ = await self.request("/api/dialogue", payload)
        self.assertEqual(code, 200)
        first = json.loads(body)
        code, body, _ = await self.request("/api/dialogue", payload)
        self.assertEqual(json.loads(body), first)
        self.assertNotEqual(
            self.e.store.rows("SELECT actor FROM dialogue_runs")[0]["actor"], "forged"
        )
        self.assertEqual(
            (await self.request("/api/dialogue", dict(payload, group="other")))[0], 403
        )
        self.assertEqual(
            (await self.request("/api/dialogue", dict(payload, text="changed")))[0], 400
        )


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_transport_sends_structured_prompt_and_records_usage(self):
        received = []
        usage = []

        class Mock(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(
                    (
                        self.path,
                        json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        ),
                    )
                )
                body = json.dumps(
                    {
                        "choices": [{"message": {"content": '{"events": []}'}}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            p = OpenAICompatible(
                {
                    "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
                    "model": "mock",
                },
                lambda *x: usage.append(x),
            )
            result = await p.complete("system", {"text": "hello"}, "extract", "demo")
            self.assertEqual(json.loads(result), {"events": []})
            self.assertEqual(received[0][0], "/v1/chat/completions")
            self.assertEqual(received[0][1]["response_format"]["type"], "json_object")
            self.assertEqual(usage[0][3]["prompt_tokens"], 12)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()

    async def test_remote_endpoint_requires_explicit_opt_in_and_https(self):
        for base, allow in [
            ("https://example.com/v1", False),
            ("http://example.com/v1", True),
        ]:
            with self.assertRaises(ValueError):
                OpenAICompatible(
                    {"base_url": base, "model": "test", "allow_remote": allow}
                )
