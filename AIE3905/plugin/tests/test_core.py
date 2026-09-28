import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from secretary.answer import Answerer
from secretary.config import Config
from secretary.engine import Engine
from secretary.memory import validate_candidates
from secretary.sample import demo_config
from secretary.timeparse import normalize_time
from secretary.types import Actor, Message


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        data=demo_config()
        data['groups'].append({'key':'other','enabled':True,'data_use_confirmed':True,'admins':['owner']})
        self.config=Config(data,self.temp.name)
        self.e=Engine(self.config)
        await self.e.start(maintenance=False)
        self.seq=0
        self.base=datetime.now(timezone.utc)-timedelta(days=1)
        self.admin=Actor('owner',['demo'],True)
        self.user=Actor('lin',['demo'])

    async def asyncTearDown(self):
        await self.e.close()
        self.temp.cleanup()

    async def message(self,text,sender='owner',group='demo',**kwargs):
        self.seq+=1
        data=dict(group=group,sender=sender,text=text,at=(self.base+timedelta(minutes=self.seq)).isoformat(),native_id=str(self.seq),name=sender)
        data.update(kwargs)
        m=Message(**data)
        self.e.ingest(m)
        await self.e.flush(group)
        return m

    def state(self,title='演示'):
        return next(s for s in self.e.states('demo') if s['title']==title)

    async def test_proposal_reply_confirmation_and_change(self):
        proposal=await self.message('/记事 演示 | 提议 | 时间=2026-10-02T15:00:00+08:00','lin')
        self.assertEqual(self.state()['status'],'unconfirmed')
        await self.message('可以',reply_to=proposal.native_id)
        self.assertEqual(self.state()['status'],'confirmed')
        await self.message('/记事 演示 | 变更 | 时间=2026-10-06;原因=数据未齐')
        answer=await self.e.query(self.user,'demo','演示现在怎么定')
        current=next(c for c in answer['claims'] if c['id'].endswith(':current'))
        self.assertIn('2026-10-06',current['text'])
        self.assertNotIn('2026-10-02',current['text'])
        self.assertEqual(self.state()['pending'],[])

    async def test_unauthorized_cancel_stays_pending_and_admin_accepts_cancel(self):
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        await self.message('/记事 演示 | 取消 | 原因=听说取消了','lin')
        self.assertEqual(self.state()['status'],'confirmed')
        pending=self.state()['pending'][0]
        await self.message('/确认 '+pending['id'])
        self.assertEqual(self.state()['status'],'cancelled')
        self.assertFalse(self.state()['pending'])

    async def test_occurrence_and_participation_do_not_cancel_series(self):
        await self.message('/记事 周会 | 确认 | 时间=每周三晚上八点')
        await self.message('/记事 周会 | 变更 | 时间=2026-09-30T21:00:00+08:00;单次=2026-09-30')
        await self.message('/记事 周会 | 缺席 | 单次=2026-09-30','lin')
        s=self.state('周会')
        self.assertEqual(s['fields']['time_raw'],'每周三晚上八点')
        self.assertEqual(s['status'],'confirmed')
        self.assertEqual(s['occurrences']['2026-09-30']['participants']['lin']['status'],'absent')

    async def test_recall_does_not_restore_obsolete_arrangement(self):
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        changed=await self.message('/记事 演示 | 变更 | 时间=2026-10-06')
        self.e.store.recall('demo',changed.native_id)
        self.assertEqual(self.state()['status'],'uncertain')
        self.assertNotIn('when',self.state()['fields'])
        answer=await self.e.query(self.user,'demo','演示现在怎么定')
        self.assertIn('需重新确认',answer['text'])
        self.assertFalse(any(m['uid']==changed.uid for m in answer['sources']))

    async def test_independent_confirmation_survives_recalled_proposal(self):
        proposal=await self.message('/记事 演示 | 提议 | 时间=2026-10-02','lin')
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        self.e.store.recall('demo',proposal.native_id)
        self.assertEqual(self.state()['status'],'confirmed')

    async def test_short_confirmation_is_invalidated_with_its_evidence(self):
        proposal=await self.message('/记事 演示 | 提议 | 时间=2026-10-02','lin')
        await self.message('可以',reply_to=proposal.native_id)
        self.e.store.recall('demo',proposal.native_id)
        self.assertEqual(self.e.states('demo')[0]['status'],'uncertain')

    async def test_same_message_id_is_idempotent(self):
        m=await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        again=self.e.ingest(m)
        self.assertFalse(again['_new'])
        await self.e.flush('demo')
        self.assertEqual(len(self.e.store.events('demo')),1)
        with self.assertRaises(ValueError):
            self.e.ingest(Message('demo','owner','different',m.at,native_id=m.native_id))

    async def test_edit_invalidates_old_revision_without_blocking_new_one(self):
        m=await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        edited=await self.message('/记事 演示 | 确认 | 时间=2026-10-06',native_id=m.native_id,kind='edit',revision='1')
        self.assertEqual(self.state()['fields']['when'],'2026-10-06')
        self.assertIsNone(self.e.store.message('demo',m.uid))
        self.assertIsNotNone(self.e.store.message('demo',edited.uid))

    async def test_opt_out_deletes_derived_data_and_prevents_reimport(self):
        m=await self.message('/记事 秘密计划 | 记录 | 备注=私密说明','lin')
        a=await self.e.query(self.admin,'demo','秘密计划')
        self.assertTrue(a['sources'])
        self.e.store.optout('demo','lin')
        self.assertIsNone(self.e.store.message('demo',m.uid))
        self.assertFalse(self.e.store.rows('SELECT * FROM answers WHERE group_key=?',('demo',)))
        self.assertIsNone(self.e.ingest(m))
        self.e.store.optout('demo','lin',False)
        self.assertIsNone(self.e.ingest(m))
        self.assertNotIn('私密说明',json.dumps(self.e.states('demo'),ensure_ascii=False))

    async def test_forget_requires_admin_and_cleans_dependencies(self):
        m=await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        iid=self.state()['id']
        with self.assertRaises(PermissionError):
            await self.e.command(self.user,'demo','/忘掉 '+iid+' 确认')
        await self.e.command(self.admin,'demo','/忘掉 '+iid+' 确认')
        self.assertFalse(self.e.states('demo'))
        self.assertIsNone(self.e.ingest(m))

    async def test_cross_group_access_and_sources_rejected(self):
        other=await self.message('/记事 其他群 | 记录 | 备注=隔离',group='other')
        with self.assertRaises(PermissionError):
            await self.e.query(self.user,'other','其他群')
        m=await self.message('普通消息')
        with self.assertRaises(ValueError):
            validate_candidates(self.e.store,self.config.group('demo'),self.e.store.message('demo',m.uid),[
                {'title':'演示','kind':'confirm','fields':{},'sources':[other.uid]}],'test')

    async def test_as_of_query_and_validation_never_use_future_evidence(self):
        first=await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        second=await self.message('/记事 演示 | 变更 | 时间=2026-10-06')
        a=await self.e.query(self.user,'demo','演示现在怎么定',as_of=first.at)
        self.assertNotIn('2026-10-06',a['text'])
        with self.assertRaises(ValueError):
            validate_candidates(self.e.store,self.config.group('demo'),self.e.store.message('demo',first.uid),[
                {'title':'演示','kind':'confirm','fields':{},'sources':[second.uid]}],'test')

    async def test_priority_can_be_corrected_downwards(self):
        await self.message('/记事 演示 | 确认 | 优先级=5')
        await self.message('/记事 演示 | 变更 | 优先级=2')
        self.assertEqual(self.state()['priority'],2)

    async def test_history_search_is_not_limited_to_recent_context_and_erases_index(self):
        m=await self.message('蓝色档案保存在三楼前台','lin',at=(self.base-timedelta(days=5)).isoformat())
        await self.message('今天吃什么')
        await self.message('明天吃什么')
        self.assertNotIn(m.uid,[r['uid'] for r in self.e.store.recent('demo',limit=2)])
        self.assertEqual(self.e.store.search('demo','蓝色档案')[0]['uid'],m.uid)
        a=await self.e.query(self.user,'demo','蓝色档案在哪里')
        self.assertIn('三楼前台',a['text'])
        self.e.store.recall('demo',m.native_id)
        self.assertEqual(self.e.store.search('demo','蓝色档案'),[])
        if self.e.store.fts:
            self.assertFalse(self.e.store.rows('SELECT * FROM message_fts WHERE uid=?',(m.uid,)))

    async def test_partial_correction_preserves_other_fields_with_dependencies(self):
        first=await self.message('/记事 演示 | 确认 | 时间=2026-10-02;负责人=老张')
        target=self.state()['history'][-1]['id']
        await self.message('/记事 演示 | 更正 | 目标事件='+target+';时间=2026-10-06')
        s=self.state()
        self.assertEqual(s['fields']['owner'],'老张')
        self.assertEqual(s['fields']['when'],'2026-10-06')
        self.assertIn(first.uid,s['field_sources']['owner'])

    async def test_followup_uses_same_members_previous_item(self):
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02;原因=数据未齐')
        await self.e.query(self.user,'demo','演示现在怎么定')
        followup=await self.e.query(self.user,'demo','为什么？')
        self.assertIn('数据未齐',followup['text'])
        trace=self.e.store.one('SELECT trace FROM answers WHERE id=?',(followup['id'],))
        self.assertIn('prompt_version',json.loads(trace['trace']))

    async def test_proactive_conflict_obeys_cooldown(self):
        sent=[]
        async def send(key,text):sent.append((key,text))
        self.e.sender=send;self.config.groups['demo'].proactive=True
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        await self.message('/记事 演示 | 提议 | 时间=2026-10-06','lin')
        await self.message('/记事 演示 | 提议 | 时间=2026-10-07','lin')
        self.assertEqual(len(sent),1)
        self.assertIn('待确认',sent[0][1])
        self.assertIn('依据消息',sent[0][1])

    async def test_ambiguous_time_does_not_keep_previous_exact_time(self):
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02T15:00:00+08:00')
        await self.message('/记事 演示 | 变更 | 时间=之后再定')
        self.assertNotIn('when',self.state()['fields'])
        self.assertEqual(self.state()['fields']['time_raw'],'之后再定')

    async def test_remember_and_mark_outdated_by_reply(self):
        original=await self.message('演示用的电脑放在前台了','lin')
        await self.message('这条记住',reply_to=original.native_id)
        a=await self.e.query(self.user,'demo','电脑放哪里')
        self.assertIn('前台',a['text'])
        event=await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        await self.message('这个已经过时了',reply_to=event.native_id)
        self.assertEqual(self.state()['status'],'uncertain')

    async def test_restart_recovers_durable_pending_messages(self):
        await self.e.close()
        self.e=Engine(self.config)
        m=Message('demo','owner','/记事 演示 | 确认 | 时间=2026-10-02',self.base.isoformat(),native_id='recover')
        self.e.ingest(m)
        await self.e.close()
        self.e=Engine(self.config)
        await self.e.start(maintenance=False)
        self.assertTrue(await self.e.flush('demo'))
        self.assertEqual(self.state()['status'],'confirmed')

    async def test_failures_are_bounded_and_visible(self):
        class Broken:
            model='broken'
            async def extract(self,*args): raise RuntimeError('secret should not be logged')
        self.e.extractor=Broken()
        m=await self.message('模型无法处理')
        row=self.e.store.message('demo',m.uid)
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['attempts'],3)
        self.assertEqual(row['error'],'RuntimeError')

    async def test_invalid_model_selector_cannot_invent_a_fact(self):
        class Fake:
            async def complete(self,*args):
                return '{"claim_ids":["invented"],"opening_index":999}'
        self.e.answerer=Answerer(Fake())
        await self.message('/记事 演示 | 确认 | 时间=2026-10-02')
        a=await self.e.query(self.user,'demo','演示现在怎么定')
        self.assertIn('2026-10-02',a['text'])
        self.assertNotIn('invented',a['text'])

    async def test_removal_during_answer_generation_discards_snapshot(self):
        m=await self.message('/记事 密钥会 | 确认 | 备注=不可再显示的内容')
        store=self.e.store
        class Removes:
            async def complete(self,*args):
                store.recall('demo',m.native_id)
                return '{"claim_ids":[]}'
        self.e.answerer=Answerer(Removes())
        answer=await self.e.query(self.user,'demo','密钥会')
        self.assertNotIn('不可再显示的内容',answer['text'])
        self.assertFalse(answer['sources'])

    async def test_retention_removes_expired_source_and_blocks_replay(self):
        m=await self.message('/记事 演示 | 确认 | 时间=2026-10-02',at=(self.base-timedelta(days=40)).isoformat())
        self.assertEqual(self.e.store.expire('demo',30),1)
        self.assertIsNone(self.e.store.message('demo',m.uid))
        self.assertIsNone(self.e.ingest(m))

    async def test_second_engine_cannot_write_same_database(self):
        other=Engine(self.config)
        try:
            with self.assertRaises(RuntimeError):
                await other.start(maintenance=False)
        finally:
            await other.close()


class TimeTests(unittest.TestCase):
    def test_after_midnight_tonight_is_ambiguous(self):
        self.assertIsNone(normalize_time('今晚九点','2026-09-27T00:30:00+08:00','Asia/Shanghai'))

    def test_date_without_hour_stays_date_only(self):
        self.assertEqual(normalize_time('明天','2026-09-27T13:00:00+08:00','Asia/Shanghai'),'2026-09-28')

    def test_naive_source_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            Message('demo','u','hello','2026-09-27T10:00:00',native_id='x')
