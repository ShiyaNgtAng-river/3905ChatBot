"""Local comparison probes. Uses temporary databases and mock providers only."""
import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from secretary.config import Config
from secretary.engine import Engine
from secretary.sample import demo_config
from secretary.timeparse import normalize_time
from secretary.types import Actor, Message

RESULTS = {'prevented_previous_patterns': {}, 'remaining_findings': {}}


@asynccontextmanager
async def scenario(understanding=None, answering=None):
    with tempfile.TemporaryDirectory(prefix='parallel-probe-') as folder:
        config = Config(demo_config(), folder)
        engine = Engine(config, understanding=understanding, answering=answering)
        await engine.start(maintenance=False)
        serial = 0
        anchor = datetime.now(timezone.utc) - timedelta(days=1)

        async def send(text, sender='owner', **kw):
            nonlocal serial
            serial += 1
            msg = Message('demo', sender, text, (anchor + timedelta(minutes=serial)).isoformat(),
                          native_id=str(serial), name=sender, **kw)
            engine.ingest(msg)
            assert await engine.flush('demo')
            row = engine.store.message('demo', msg.uid)
            return msg, row

        try:
            yield engine, send
        finally:
            await engine.close()


async def main():
    prevented = RESULTS['prevented_previous_patterns']
    findings = RESULTS['remaining_findings']

    class InventedAnswer:
        model = 'review-mock'
        async def complete(self, *args):
            return '{"reply":"地点在火星基地，负责人是张三。","claim_ids":["invented"],"opening_index":99}'

    async with scenario(answering=InventedAnswer()) as (e, send):
        await send('/记事 演示 | 确认 | 地点=三楼')
        answer = await e.query(Actor('lin', ['demo']), 'demo', '演示在哪里')
        assert '火星' not in answer['text'] and '三楼' in answer['text']
        prevented['fabricated_answer_text'] = answer['text']

    async with scenario() as (e, send):
        m, _ = await send('/记事 演示 | 提议 | 时间=2026-10-02', 'lin')
        await send('可以', reply_to=m.native_id)
        e.store.recall('demo', m.native_id)
        state = e.states('demo')[0]
        assert state['status'] == 'uncertain'
        prevented['dependent_short_confirmation'] = state['status']

    async with scenario() as (e, send):
        await send('/记事 甲事项 | 确认 | 地点=一楼')
        await send('/记事 乙事项 | 确认 | 地点=二楼')
        first = next(s for s in e.states('demo') if s['title'] == '甲事项')
        await e.query(Actor('lin', ['demo']), 'demo', '甲事项在哪里')
        assert e.store.rows('SELECT * FROM answers')
        e.store.forget('demo', first['id'], 'owner')
        assert not e.store.rows('SELECT * FROM answers')
        _, row = await send('/记事 乙事项 | 变更 | 地点=三楼')
        assert row['status'] == 'done'
        assert next(s for s in e.states('demo') if s['title'] == '乙事项')['fields']['location'] == '三楼'
        prevented['event_id_after_delete'] = row['status']
        prevented['derived_answer_cleanup'] = len(e.store.rows('SELECT * FROM answers'))

    async with scenario() as (e, send):
        await send('/记事 周会 | 确认 | 时间=每周三晚上八点')
        await send('/记事 周会 | 取消 | 单次=2026-09-30')
        state = e.states('demo')[0]
        assert state['status'] == 'confirmed'
        assert state['occurrences']['2026-09-30']['status'] == 'cancelled'
        prevented['instance_cancellation'] = {'series_status': state['status'],
                                             'occurrence_status': state['occurrences']['2026-09-30']['status']}

    class InvalidJSON:
        model = 'review-invalid-json'
        calls = 0
        async def complete(self, *args):
            self.calls += 1
            return '暂时无法输出 JSON'

    invalid = InvalidJSON()
    async with scenario(understanding=invalid) as (e, send):
        _, row = await send('明天开会')
        assert row['status'] == 'failed' and row['attempts'] == 3
        prevented['malformed_extraction'] = {'status': row['status'], 'attempts': row['attempts']}

    async with scenario() as (e, send):
        await send('/记事 演示 | 确认 | 时间=2026-10-02')
        await send('/记事 演示 | 记录 | 备注=我了解情况', 'lin')
        note = next(x for x in e.store.events('demo') if x['kind'] == 'note')
        assert not e.config.group('demo').can_confirm('lin', 'owner')
        await send('/记事 演示 | 更正 | 目标事件=' + note['id'] + ';时间=2026-10-06', 'lin')
        state = e.states('demo')[0]
        last = state['history'][-1]
        assert state['fields']['when'] == '2026-10-06' and last['accepted']
        findings['note_correction_bypasses_confirmation'] = {'actor': last['actor'], 'accepted': last['accepted'],
                                                             'when': state['fields']['when'], 'status': state['status']}

    async with scenario() as (e, send):
        await send('/记事 演示 | 确认 | 时间=2026-10-02')
        await send('/记事 演示 | 取消 | 原因=场地问题')
        cancelled = e.states('demo')[0]
        assert cancelled['status'] == 'cancelled'
        target = cancelled['history'][-1]['id']
        await send('/记事 演示 | 更正 | 目标事件=' + target + ';原因=客户请假')
        state = e.states('demo')[0]
        assert state['status'] == 'confirmed'
        findings['correcting_cancellation_reason_reopens_item'] = {'before': 'cancelled', 'after': state['status'],
                                                                  'reason': state['fields']['reason']}

    class Capture:
        model = 'review-capture'
        payloads = []
        async def complete(self, system, payload, role, group):
            self.payloads.append(payload)
            return '{"events":[]}'

    capture = Capture()
    async with scenario(understanding=capture) as (e, send):
        original, _ = await send('/记事 演示 | 确认 | 时间=2026-10-02')
        target = e.states('demo')[0]['history'][-1]['id']
        await send('我刚才说错了，演示应该是十月六日。', reply_to=original.native_id)
        supplied = json.dumps(capture.payloads[-1], ensure_ascii=False)
        assert target not in supplied
        findings['natural_correction_target_id_absent'] = {'required_event_id': target,
                                                         'present_in_model_input': False,
                                                         'candidate_fields': list(capture.payloads[-1]['candidates'][0])}

    async with scenario() as (e, send):
        await send('/记事 演示 | 提议 | 时间=2026-10-02', 'lin')
        await send('/记事 演示 | 确认 | 时间=2026-10-06')
        state = e.states('demo')[0]
        assert state['fields']['when'] == '2026-10-06' and len(state['pending']) == 1
        findings['old_proposal_still_pending_after_new_decision'] = {'current': state['fields']['when'],
                                                                  'pending': [x['payload'] for x in state['pending']]}

    times = {raw: normalize_time(raw, '2026-09-28T12:00:00+08:00', 'Asia/Shanghai')
             for raw in ['每周三晚上八点', '下下周三下午三点', '明天九点']}
    assert times['下下周三下午三点'].startswith('2026-10-07')
    assert times['每周三晚上八点'].startswith('2026-09-30')
    assert 'T09:00' in times['明天九点']
    findings['time_parser_precision_and_range'] = times

    async with scenario() as (e, send):
        await send('/记事 产品演示 | 确认 | 时间=2026-10-02;负责人=甲团队')
        await send('/记事 产品演示 | 确认 | 时间=2026-10-06;负责人=乙团队')
        states = e.states('demo')
        assert len(states) == 1
        findings['same_title_item_identity_disclosed_limit'] = {'items': len(states), 'current': states[0]['fields']}

    path = Path(__file__).with_name('review-parallel-results.json')
    path.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(RESULTS, ensure_ascii=False, indent=2))


asyncio.run(main())
