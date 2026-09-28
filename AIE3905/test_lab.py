import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import lab
from secretary.providers import OpenAICompatible


class ProviderWireTest(unittest.TestCase):
    def test_thinking_switches_are_top_level_and_keep_json(self):
        for extra in ({'enable_thinking':False},{'thinking':{'type':'disabled'}}):
            seen = []
            class Response:
                def __enter__(self): return self
                def __exit__(self,*args): pass
                def read(self,*args):
                    return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
            class Opener:
                def open(self,req,timeout):
                    seen.append(json.loads(req.data)); return Response()
            with patch('urllib.request.build_opener',return_value=Opener()):
                provider=OpenAICompatible({'model':'fixture','extra_body':extra})
                result=asyncio.run(provider.complete('JSON',{},'test','lab'))
            self.assertTrue(json.loads(result)['ok'])
            self.assertEqual(seen[0]['response_format'],{'type':'json_object'})
            self.assertNotIn('extra_body',seen[0])
            for key,value in extra.items(): self.assertEqual(seen[0][key],value)

    def test_extension_cannot_override_messages_or_model(self):
        for extra in ({'messages':[]},{'model':'other'},{'enable_thinking':'false'},{'thinking':{'type':'unknown'}}):
            with self.assertRaises(ValueError):
                OpenAICompatible({'model':'fixture','extra_body':extra})


class DatasetTest(unittest.TestCase):
    def test_reproducible_and_paired_with_separate_oracle(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs = []
            for name,style in [('a','commands'),('b','commands'),('c','natural')]:
                folder=Path(tmp)/name
                args=SimpleNamespace(seed=5,scenarios=3,noise=.8,hard=True,style=style,output=str(folder))
                with contextlib.redirect_stdout(io.StringIO()): lab.generate(args)
                outputs.append(folder)
            self.assertEqual((outputs[0]/'messages.jsonl').read_bytes(),(outputs[1]/'messages.jsonl').read_bytes())
            self.assertEqual((outputs[0]/'cases.json').read_bytes(),(outputs[2]/'cases.json').read_bytes())
            records=[json.loads(line) for line in (outputs[0]/'messages.jsonl').read_text().splitlines()]
            self.assertEqual(len(records),90)
            self.assertEqual(len({r['native_id'] for r in records}),len(records))
            seen=set()
            for row in records:
                self.assertNotIn('expected_state',row)
                if row.get('reply_to'): self.assertIn(row['reply_to'],seen)
                if row.get('target_id'): self.assertIn(row['target_id'],seen)
                seen.add(row['native_id'])


if __name__=='__main__': unittest.main()
