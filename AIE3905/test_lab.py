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


def load_eval():
    import importlib.util
    spec = importlib.util.spec_from_file_location('eval_memory', Path(lab.__file__).parent / 'tools' / 'eval_memory.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryDatasetTest(unittest.TestCase):
    def test_days_are_reproducible_and_keep_the_oracle_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            folders = []
            for name in ('a', 'b'):
                folder = Path(tmp) / name
                with contextlib.redirect_stdout(io.StringIO()):
                    lab.days(SimpleNamespace(seed=3, days=5, per_day=80, output=str(folder)))
                folders.append(folder)
            self.assertEqual((folders[0] / 'messages.jsonl').read_bytes(), (folders[1] / 'messages.jsonl').read_bytes())
            rows = [json.loads(line) for line in (folders[0] / 'messages.jsonl').read_text().splitlines()]
            oracle = json.loads((folders[0] / 'oracle.json').read_text())
        self.assertEqual((len(rows), len(oracle)), (400, 5))
        seen = set()
        for row in rows:  # replies point backwards; answers never leak into messages
            self.assertNotIn('states', row)
            if row.get('reply_to'):
                self.assertIn(row['reply_to'], seen)
            seen.add(row['native_id'])
        for day in oracle:  # every decided value is really said that day
            said = ' '.join(r['text'] for r in rows if r['at'].startswith(day['day']))
            for decision in day['decisions']:
                for value in decision['keys']:
                    self.assertIn(value, said)
        final = oracle[-1]['states']
        self.assertTrue(final and all(s['current'] for s in final))

    def test_matching_tolerates_numerals_weekday_names_and_24h_clock(self):
        ev = load_eval()
        self.assertTrue(ev.has('改到星期六上午八点', '周六早上8点'))
        self.assertTrue(ev.has('31号 18:00 集合', '31号晚上6点'))
        self.assertFalse(ev.has('周日早上8点', '周六早上8点'))
        self.assertTrue(ev.has('在2号房', '2号房'))

    def test_fake_replay_runs_both_systems_and_counts_usage(self):
        ev = load_eval()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                lab.days(SimpleNamespace(seed=4, days=2, per_day=60, output=tmp))
            oracle = json.loads((Path(tmp) / 'oracle.json').read_text())
            records = sorted(lab.load_records(Path(tmp) / 'messages.jsonl'), key=lambda m: m.at)
            v2 = asyncio.run(ev.replay(SimpleNamespace(fake=True), records, oracle, baseline=False))
            base = asyncio.run(ev.replay(SimpleNamespace(fake=True), records, oracle, baseline=True))
        self.assertEqual([d['day'] for d in v2['days']], [o['day'] for o in oracle])
        self.assertEqual((v2['failures'], base['failures']), ({}, {}))
        self.assertIn('reading', {r['role'] for r in v2['usage']})
        self.assertIn('episode', {r['role'] for r in base['usage']})
        self.assertEqual(v2['summary']['declines_unknown'], 1.0)
        self.assertEqual(ev.per_thousand(v2['usage'], len(records))['cache_hit_rate'], 0.0)



def load_tool(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(lab.__file__).parent / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HardDatasetTest(unittest.TestCase):
    def test_traps_are_said_but_never_current_and_recalls_point_back(self):
        hard = load_tool('memory_hard')
        rows, oracle = hard.build(SimpleNamespace(seed=11, days=7, per_day=150))
        again, _ = hard.build(SimpleNamespace(seed=11, days=7, per_day=150))
        self.assertEqual(rows, again)
        said = ' '.join(r['text'] for r in rows)
        final = oracle[-1]
        self.assertTrue(final['states'])
        for state in final['states']:
            self.assertTrue(state['traps'])
            for trap in state['traps']:
                self.assertIn(trap, said)
                self.assertNotIn(trap, state['current'])
        self.assertTrue(any(a['trap'] for a in final['answered'] + final['assignments']))
        ids = set()
        for r in rows:
            if r.get('kind') == 'recall':
                self.assertIn(r['target_id'], ids)
            ids.add(r['native_id'])

    def test_rewrite_keeps_values_or_falls_back_to_the_original(self):
        hard = load_tool('memory_hard')
        rows = [{'name': '老张', 'text': '那就先定周六早上8点，东门集合'}, {'name': '小林', 'text': '中午吃啥'}]

        async def complete(self, system, payload, role, group, **kw):
            return json.dumps({'lines': ['那就周六8点吧', '中午干饭吃啥']}, ensure_ascii=False)

        with patch.dict('os.environ', {'GROUPBOT_MODEL_API_KEY': 'test'}), patch.object(OpenAICompatible, 'complete', complete):
            changed, kept = asyncio.run(hard.rewrite(rows, Path(lab.__file__).parent / 'config' / 'deepseek.json'))
        self.assertEqual((changed, kept), (1, 1))
        self.assertEqual([r['text'] for r in rows], ['那就先定周六早上8点，东门集合', '中午干饭吃啥'])

    def test_fake_replay_scores_answers_on_traps_and_names(self):
        hard, ev = load_tool('memory_hard'), load_tool('eval_memory')
        rows, oracle = hard.build(SimpleNamespace(seed=5, days=4, per_day=120))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'messages.jsonl'
            path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
            records = sorted(lab.load_records(path), key=lambda m: m.at)
            result = asyncio.run(ev.replay(SimpleNamespace(fake=True), records, oracle, baseline=False))
        self.assertEqual(result['failures'], {})
        self.assertIn('stale_resisted', result['summary'])
        self.assertIn('identity', result['summary'])


if __name__=='__main__': unittest.main()
