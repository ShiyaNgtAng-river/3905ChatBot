import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from groupmedia.service import MediaError, MediaService
from groupmedia.server import make_server


def picture():
    stream = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(stream, "PNG")
    return stream.getvalue()


def request(group="test-group", media=None):
    return {
        "source": {"group": group, "message_id": "m-1"},
        "media": media
        or {"mime": "image/png", "base64": base64.b64encode(picture()).decode()},
    }


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = MediaService({}, Path(self.tmp.name))
        self.addCleanup(self.service.close)

    def cloud(self, operation, backend, handler):
        self.service.config[operation] = {
            "backend": backend,
            "model": "test-model",
            "base_url": "https://media.example/v1",
            "api_key_env": "MEDIA_TEST_KEY",
        }
        self.service.client.close()
        self.service.client = httpx.Client(transport=httpx.MockTransport(handler))
        env = patch.dict(os.environ, {"MEDIA_TEST_KEY": "synthetic-not-real"})
        env.start()
        self.addCleanup(env.stop)

    def test_bad_media_never_runs_local_code(self):
        with patch.object(self.service, "_ocr") as run:
            for media in [
                {"path": "/etc/passwd"},
                {"url": "http://localhost"},
                {"mime": "image/png", "base64": "bad!"},
                {
                    "mime": "image/png",
                    "base64": base64.b64encode(b"not image").decode(),
                },
            ]:
                with self.assertRaises(MediaError):
                    self.service.process("ocr", request(media=media))
            run.assert_not_called()

    def test_source_validation(self):
        for source in [
            {},
            {"group": "", "message_id": "m"},
            {"group": "a", "message_id": 42},
        ]:
            with self.assertRaises(MediaError):
                self.service.process("ocr", {**request(), "source": source})

    def test_request_scoped_files_and_provenance(self):
        paths = []

        def local(path):
            paths.append(path)
            self.assertTrue(path.exists())
            return {"text": "尚未确认"}

        with patch.object(self.service, "_ocr", side_effect=local):
            a = self.service.process("ocr", request("A"))
            b = self.service.process("ocr", request("B"))
        self.assertEqual(a["source"]["group"], "A")
        self.assertEqual(b["source"]["group"], "B")
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(not p.exists() for p in paths))
        self.assertFalse(a["memory_written"])
        self.assertEqual(len(a["source"]["sha256"]), 64)

    def test_temp_files_removed_on_failure(self):
        with patch.object(
            self.service, "_ocr", side_effect=MediaError("test", "failed")
        ):
            with self.assertRaises(MediaError):
                self.service.process("ocr", request())
        self.assertEqual(list(Path(self.tmp.name).glob("media-*")), [])

    def test_missing_key_does_not_contact_cloud(self):
        calls = []
        self.cloud("describe", "vision_chat", lambda req: calls.append(req))
        with patch.dict(os.environ, {"MEDIA_TEST_KEY": ""}):
            with self.assertRaises(MediaError) as ctx:
                self.service.process("describe", request())
        self.assertEqual(ctx.exception.code, "not_configured")
        self.assertEqual(calls, [])

    def test_vision_contract_and_single_user_message(self):
        def upstream(req):
            body = json.loads(req.content)
            self.assertEqual(req.url.path, "/v1/chat/completions")
            self.assertEqual([m["role"] for m in body["messages"]], ["user"])
            self.assertTrue(
                body["messages"][0]["content"][1]["image_url"]["url"].startswith(
                    "data:image/png;base64,"
                )
            )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "仅建议"}}]}
            )

        self.cloud("describe", "vision_chat", upstream)
        self.assertEqual(self.service.process("describe", request())["text"], "仅建议")

    def test_cloud_asr_multipart_contract(self):
        def upstream(req):
            self.assertEqual(req.url.path, "/v1/audio/transcriptions")
            self.assertIn("multipart/form-data", req.headers["Content-Type"])
            self.assertIn(b'name="model"', req.content)
            self.assertIn(b'filename="audio.wav"', req.content)
            return httpx.Response(200, json={"text": "还没确认"})

        self.cloud("transcribe", "audio_transcriptions", upstream)
        r = request(
            media={
                "mime": "audio/wav",
                "base64": base64.b64encode(b"mock-wave").decode(),
            }
        )
        self.assertEqual(self.service.process("transcribe", r)["text"], "还没确认")

    def test_siliconflow_image_contract(self):
        def upstream(req):
            body = json.loads(req.content)
            self.assertEqual(req.url.path, "/v1/images/generations")
            self.assertNotIn("batch_size", body)
            self.assertEqual(body["image_size"], "1024x1024")
            return httpx.Response(
                200,
                json={"images": [{"b64_json": base64.b64encode(picture()).decode()}]},
            )

        self.cloud("generate", "siliconflow_images", upstream)
        result = self.service.process("generate", {**request(), "prompt": "test image"})
        self.assertTrue(result["generated"])
        self.assertEqual(result["asset"]["mime"], "image/png")

    def test_dashscope_image_contract(self):
        def upstream(req):
            body = json.loads(req.content)
            self.assertTrue(
                req.url.path.endswith(
                    "/api/v1/services/aigc/multimodal-generation/generation"
                )
            )
            self.assertEqual(
                body["input"]["messages"][0]["content"], [{"text": "test image"}]
            )
            return httpx.Response(
                200,
                json={
                    "output": {
                        "choices": [
                            {
                                "message": {
                                    "content": [{"image": "https://cdn.example/img"}]
                                }
                            }
                        ]
                    }
                },
            )

        self.cloud("generate", "dashscope_image", upstream)
        with patch.object(self.service, "_download_image", return_value=picture()):
            self.assertTrue(
                self.service.process("generate", {**request(), "prompt": "test image"})[
                    "generated"
                ]
            )

    def test_failed_provider_does_not_leak_response_or_key(self):
        self.cloud(
            "describe",
            "vision_chat",
            lambda req: httpx.Response(401, text="secret-provider-body"),
        )
        with self.assertRaises(MediaError) as ctx:
            self.service.process("describe", request())
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 502)

    def test_malformed_provider_result(self):
        self.cloud(
            "describe",
            "vision_chat",
            lambda req: httpx.Response(200, json={"choices": []}),
        )
        with self.assertRaises(MediaError) as ctx:
            self.service.process("describe", request())
        self.assertEqual(ctx.exception.code, "invalid_result")

    def test_private_image_download_is_rejected(self):
        with self.assertRaises(MediaError):
            self.service._download_image("https://127.0.0.1/image.png")


class HTTPTests(unittest.TestCase):
    def test_auth_missing_capability_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = MediaService({}, Path(tmp))
            server = make_server(service, "test-token-at-least-24-characters", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
                ) as client:
                    self.assertEqual(client.get("/api/capabilities").status_code, 401)
                    client.headers["Authorization"] = (
                        "Bearer test-token-at-least-24-characters"
                    )
                    self.assertEqual(client.get("/api/capabilities").status_code, 200)
                    r = client.post(
                        "/api/generate", json={**request(), "prompt": "cat"}
                    )
                    self.assertEqual(r.status_code, 503)
                    self.assertEqual(r.json()["error"]["code"], "not_configured")
                    self.assertEqual(
                        client.post("/api/ocr", content=b"not-json").status_code, 400
                    )
            finally:
                server.shutdown()
                server.server_close()
                service.close()


if __name__ == "__main__":
    unittest.main()
