"""No-model tests for public input validation and API isolation."""

import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import AioHTTPTestCase

from runtime import Manager, normalize_dataset, validate_rows
from server import application


class InputTests(unittest.TestCase):
    def test_existing_dataset_timestamp_is_not_a_mention(self):
        row = normalize_dataset(
            [
                {
                    "sender": "lin",
                    "kind": "create",
                    "at": "2026-09-01T10:00:00+08:00",
                    "text": "hi",
                }
            ]
        )[0]
        self.assertNotIn("at", row)
        self.assertEqual(row["kind"], "message")
        self.assertTrue(row["timestamp"].endswith("+08:00"))

    def test_invalid_input_and_assertions_rejected(self):
        for rows in [
            [],
            [{"at": "yes"}],
            [{"timestamp": "2026-09-01T10:00:00"}],
            [{"kind": "recall"}],
            [{"expect": {"reply_count": True}}],
            [{"expect": {"contains": "word"}}],
            [{"attachments": ["image"]}],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_rows(rows)


class ApiTests(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Manager(
            Path(self.tmp.name), Path(self.tmp.name) / "runs", "mock", 1
        )
        return application(self.manager, "test-token")

    async def asyncTearDown(self):
        await self.manager.close()
        await super().asyncTearDown()
        self.tmp.cleanup()

    async def test_missing_auth_and_cross_origin_rejected(self):
        r = await self.client.get("/api/runs")
        self.assertEqual(r.status, 401)
        r = await self.client.post(
            "/api/runs",
            headers={
                "Authorization": "Bearer test-token",
                "Origin": "https://evil.example",
            },
            json={},
        )
        self.assertEqual(r.status, 403)
        self.assertFalse(self.manager.runs)

    async def test_path_traversal_dataset_rejected(self):
        r = await self.client.post(
            "/api/runs",
            headers={"Authorization": "Bearer test-token"},
            json={"dataset": "../../data/qq.sqlite3"},
        )
        self.assertEqual(r.status, 400)
        self.assertFalse(self.manager.runs)

    async def test_stopping_before_task_starts_records_cancelled_state(self):
        rid = self.manager.create("queued")[0]
        await self.manager.stop(rid)
        self.assertEqual(self.manager.runs[rid].state, "stopped")
        self.assertEqual(self.manager.runs[rid].procs, [])


if __name__ == "__main__":
    unittest.main()
