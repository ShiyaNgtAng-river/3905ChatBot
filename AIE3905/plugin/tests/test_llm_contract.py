import json
import tempfile
import unittest

from secretary.config import Config
from secretary.engine import Engine
from secretary.sample import demo_config
from secretary.types import Message


class NullableTargetTests(unittest.IsolatedAsyncioTestCase):
    async def run_candidate(self, target, kind='propose'):
        class Provider:
            model = 'captured-response-fixture'

            async def complete(self, system, payload, role, group):
                return json.dumps({'events':[{
                    'title':'集成测试评审','kind':kind,
                    'fields':{'when':'2026-10-02'},
                    'sources':[payload['current']['uid']],
                    'target':target,'scope':'series','occurrence':''
                }]})

        with tempfile.TemporaryDirectory() as folder:
            raw = demo_config()
            raw['max_attempts'] = 1
            engine = Engine(Config(raw,folder),understanding=Provider())
            try:
                await engine.start(maintenance=False)
                message = Message('demo','lin','建议评审在十月二日举行',
                                  '2026-09-28T09:00:00+08:00',native_id='proposal')
                engine.ingest(message)
                self.assertTrue(await engine.flush('demo'))
                return engine.store.message('demo',message.uid)['status'],engine.states('demo')
            finally:
                await engine.close()

    async def test_json_null_target_is_absent(self):
        status,states = await self.run_candidate(None)
        self.assertEqual(status,'done')
        self.assertEqual(states[0]['status'],'unconfirmed')
        self.assertEqual(states[0]['pending'][0]['payload']['when'],'2026-10-02')

    async def test_unknown_nonempty_target_is_still_rejected(self):
        status,_ = await self.run_candidate('nonexistent-event')
        self.assertEqual(status,'failed')

    async def test_null_does_not_grant_confirmation_permission(self):
        status,states = await self.run_candidate(None,'confirm')
        self.assertEqual(status,'done')
        self.assertEqual(states[0]['status'],'unconfirmed')
        self.assertFalse(states[0]['pending'][0]['accepted'])


if __name__ == '__main__': unittest.main()
