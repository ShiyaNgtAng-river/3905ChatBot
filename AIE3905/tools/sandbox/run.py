"""GroupBot sandbox: real AstrBot + real OneBot v11 adapter + this plugin, with a simulated QQ client.

Only QQ's own servers are simulated. The model is either a local deterministic mock (no key,
no cost) or a real DeepSeek/Qwen endpoint called through an AstrBot-managed provider.

    python3 tools/sandbox/run.py                              # mock model
    python3 tools/sandbox/run.py --model deepseek             # key from GROUPBOT_MODEL_API_KEY
    python3 tools/sandbox/run.py --model qwen --model-id qwen-plus
    python3 tools/sandbox/run.py --astrbot /path/to/AstrBot   # reuse a local install, no download

Without --astrbot the first run clones AstrBot (pinned tag) into .sandbox/astrbot and installs
it with uv. With --astrbot only its source and .venv are used; all runtime data goes to
.sandbox/work-<model>/ through ASTRBOT_ROOT, so the local instance's data is never touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

LAB = Path(__file__).resolve().parents[2]
TOOL = Path(__file__).resolve().parent
ASTRBOT_REPO = 'https://github.com/AstrBotDevs/AstrBot'
ASTRBOT_TAG = 'v4.28.1'
PLUGIN = 'astrbot_plugin_groupsecretary'
KEY_ENV = 'GROUPBOT_MODEL_API_KEY'
PRESETS = {'deepseek': LAB / 'config/deepseek.json', 'qwen': LAB / 'config/qwen.json'}
SELF, GROUP, OTHER = 10000, 123456, 777777
OWNER, LIN, YU = 90001, 20001, 20002
NAMES = {OWNER: '老张', LIN: '小林', YU: '小余'}


# ---------------------------------------------------------------- setup (any Python 3.11+)
def parse():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--model', choices=['mock', 'deepseek', 'qwen'], default='mock')
    p.add_argument('--model-id', default=os.environ.get('GROUPBOT_MODEL_ID', ''),
                   help='覆盖预设模型 ID；也可用环境变量 GROUPBOT_MODEL_ID')
    p.add_argument('--base-url', default='', help='覆盖预设服务地址（例如 Qwen 的其他地域）')
    p.add_argument('--home', default=str(LAB / '.sandbox'), help='AstrBot 安装与运行数据目录')
    p.add_argument('--output', default='', help='报告路径，默认 results/sandbox-<model>.json')
    p.add_argument('--ws-port', type=int, default=16199)
    p.add_argument('--dashboard-port', type=int, default=16185)
    p.add_argument('--mock-port', type=int, default=16800)
    p.add_argument('--plugin-main', default='', help='用指定 main.py 替换插件入口，用于回归对照')
    p.add_argument('--astrbot', default=os.environ.get('GROUPBOT_ASTRBOT', ''),
                   help='复用本机已安装的 AstrBot 源码目录（需含 .venv）；不克隆、不下载，也不读写其 data 目录')
    p.add_argument('--frontend', choices=['astrbot', 'plugin'], default='astrbot',
                   help='插件的 dialogue.frontend：astrbot 由宿主 agent 回答 @，plugin 为 v0.2 自有对话')
    return p.parse_args()


def ensure_astrbot(home: Path) -> Path:
    src = home / 'astrbot'
    py = src / '.venv/bin/python'
    if py.exists():
        return py
    if not src.exists():
        print(f'首次运行：克隆 AstrBot {ASTRBOT_TAG} …', flush=True)
        subprocess.run(['git', 'clone', '--depth', '1', '--branch', ASTRBOT_TAG, ASTRBOT_REPO, str(src)],
                       check=True, env=dict(os.environ, GIT_LFS_SKIP_SMUDGE='1'))
    if not shutil.which('uv'):
        sys.exit('需要 uv（https://docs.astral.sh/uv/）来安装 AstrBot 依赖。')
    print('安装 AstrBot 依赖（uv sync，首次约 1–3 分钟）…', flush=True)
    subprocess.run(['uv', 'sync', '--python', '3.12'], cwd=src, check=True)
    return py


def model_settings(args) -> dict | None:
    if args.model == 'mock':
        return None
    preset = json.loads(PRESETS[args.model].read_text(encoding='utf-8'))['models']['understanding']
    return {'base_url': (args.base_url or preset['base_url']).rstrip('/'),
            'model': args.model_id or preset['model']}


def preflight(settings: dict):
    """One tiny request, so key/model/network problems surface before AstrBot starts."""
    key = os.environ.get(KEY_ENV, '')
    if not key:
        sys.exit(f'未找到环境变量 {KEY_ENV}。请在运行环境中设置 API Key（不要写进文件或聊天）。')
    body = {'model': settings['model'], 'max_tokens': 20, 'temperature': 0,
            'messages': [{'role': 'system', 'content': '只输出 JSON。'}, {'role': 'user', 'content': '返回 {"ok":true}'}],
            'response_format': {'type': 'json_object'}}
    req = urllib.request.Request(settings['base_url'] + '/chat/completions', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read())
        print(f"模型连通：{settings['model']} @ {settings['base_url']}（{time.monotonic() - t:.1f}s）", flush=True)
        return {'ok': True, 'seconds': round(time.monotonic() - t, 2), 'usage': data.get('usage')}
    except urllib.error.HTTPError as e:
        hint = {401: 'Key 无效或与服务地址／地域不匹配', 402: '余额不足', 403: 'Key 无权使用该模型或地域',
                404: '模型 ID 或服务地址不对', 429: '限流或额度用尽'}.get(e.code, '见服务商文档')
        sys.exit(f'模型服务返回 HTTP {e.code}：{hint}。')
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if '403' in reason and 'Tunnel' in reason:
            sys.exit(f"网络策略未放行 {settings['base_url']} 的域名：请在运行环境的网络设置中允许该域名。")
        sys.exit(f'无法连接模型服务：{reason}')


# ---------------------------------------------------------------- sandbox run (AstrBot venv)
class Sandbox:
    def __init__(self, args, settings):
        self.args, self.settings = args, settings
        self.real = settings is not None
        self.home = Path(args.home).resolve()
        self.astrbot = Path(args.astrbot).resolve() if args.astrbot else self.home / 'astrbot'
        self.work = self.home / f'work-{args.model}'
        self.root = self.work / 'root'
        self.data = self.root / 'data'
        self.pdata = self.data / 'plugin_data' / PLUGIN
        self.procs = {}
        self.log = None
        tag = ASTRBOT_TAG
        if args.astrbot:
            found = re.search(r'__version__ = "([^"]+)"', (self.astrbot / 'astrbot/__init__.py').read_text(encoding='utf-8'))
            tag = 'local v' + (found.group(1) if found else '?')
        self.report = {'model': args.model, 'model_id': settings['model'] if settings else 'mock-model',
                       'astrbot_tag': tag, 'frontend': args.frontend, 'started': datetime.now().isoformat(timespec='seconds'),
                       'checks': [], 'observations': {}}

    # -- bookkeeping
    def check(self, kind, name, ok, detail=''):
        self.report['checks'].append({'kind': kind, 'name': name, 'pass': bool(ok), 'detail': str(detail)[:500]})
        print(f"{'PASS' if ok else 'FAIL'} [{'系统' if kind == 'system' else '理解'}] {name}"
              + (f' | {str(detail)[:200]}' if detail else ''), flush=True)

    def mock_calls(self, kind, since):
        path = self.work / 'mock_llm.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        return [r for r in rows if r['t'] >= since and r['kind'] == kind]

    # -- environment
    def prepare(self):
        shutil.rmtree(self.work, ignore_errors=True)
        (self.data / 'plugins').mkdir(parents=True)
        shutil.copytree(LAB / 'plugin', self.data / 'plugins' / PLUGIN,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'tests', 'docs', 'examples'))
        if self.args.plugin_main:
            shutil.copy(self.args.plugin_main, self.data / 'plugins' / PLUGIN / 'main.py')
        if self.real:
            provider = {'api_base': self.settings['base_url'], 'model': self.settings['model'], 'key': ['$' + KEY_ENV], 'timeout': 60}
        else:
            provider = {'api_base': f'http://127.0.0.1:{self.args.mock_port}/v1', 'model': 'mock-model', 'key': ['sk-mock'], 'timeout': 30}
        # AstrBot fills every missing key from its defaults on first start.
        cfg = {'platform': [{'id': 'sandbox-qq', 'type': 'aiocqhttp', 'enable': True, 'ws_reverse_host': '127.0.0.1',
                             'ws_reverse_port': self.args.ws_port, 'ws_reverse_token': 'sandbox-token'}],
               'provider': [{'id': 'sandbox-model', 'provider': 'openai', 'type': 'openai_chat_completion',
                             'provider_type': 'chat_completion', 'enable': True, 'proxy': '', 'custom_headers': {}, **provider}],
               'provider_settings': {'default_provider_id': 'sandbox-model'},
               'platform_settings': {'rate_limit': {'time': 60, 'count': 1000, 'strategy': 'stall'}},
               'dashboard': {'port': self.args.dashboard_port, 'host': '127.0.0.1'},
               'disable_metrics': True}
        (self.data / 'cmd_config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

    def write_plugin_config(self):
        cfg = {'mode': 'astrbot', 'database': 'secretary.sqlite3', 'max_attempts': 3, 'query_wait_seconds': 30 if self.real else 8,
               'groups': [{'key': 'sandbox', 'enabled': True, 'data_use_confirmed': True,
                           'admins': [str(OWNER)], 'confirmers': [str(OWNER)], 'confirmation': 'designated',
                           'timezone': 'Asia/Shanghai', 'retention_days': 30,
                           'processing_location': f"沙盒：{self.report['model_id']}；全部为虚构数据",
                           'proactive': False, 'report_time': '', 'platform_id': 'sandbox-qq', 'native_group_id': str(GROUP)}],
               'web': {'enabled': False},
               'models': {'understanding': {'provider_id': 'sandbox-model'}, 'answering': {'provider_id': 'sandbox-model'}},
               'dialogue': {'frontend': self.args.frontend}}
        (self.pdata / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

    def start_mock(self):
        self.procs['mock'] = subprocess.Popen([sys.executable, str(TOOL / 'mock_llm.py'), str(self.args.mock_port), str(self.work)],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        wait_port(self.args.mock_port)

    def start_astrbot(self, tag):
        self.log = self.work / f'astrbot-{tag}.log'
        env = dict(os.environ, ASTRBOT_ROOT=str(self.root))
        cmd = [sys.executable, 'main.py']
        # Serve an existing dashboard build instead of downloading one.
        if (self.astrbot / 'data' / 'dist' / 'index.html').exists():
            cmd += ['--webui-dir', str(self.astrbot / 'data' / 'dist')]
        self.procs['astrbot'] = subprocess.Popen(cmd, cwd=self.astrbot, env=env,
                                                 stdout=open(self.log, 'w'), stderr=subprocess.STDOUT)
        wait_port(self.args.ws_port, 120, self.procs['astrbot'])

    def stop_astrbot(self):
        proc = self.procs.pop('astrbot', None)
        if proc:
            proc.terminate()
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self):
        self.stop_astrbot()
        for proc in self.procs.values():
            proc.terminate()

    def logtext(self):
        return self.log.read_text(encoding='utf-8', errors='replace') if self.log else ''

    # -- plugin database probes (read-only)
    def db(self):
        conn = sqlite3.connect(f"file:{self.pdata / 'secretary.sqlite3'}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def msg(self, mid):
        with self.db() as c:
            r = c.execute('SELECT * FROM messages WHERE native_id=?', (str(mid),)).fetchone()
            return dict(r) if r else None

    def count(self, sql, args=()):
        with self.db() as c:
            return c.execute(sql, args).fetchone()[0]

    async def processed(self, mid):
        end = time.time() + (120 if self.real else 20)
        while time.time() < end:
            try:
                r = self.msg(mid)
                if r and r['status'] not in ('pending', 'retry'):
                    return r
            except sqlite3.OperationalError:
                pass
            await asyncio.sleep(.3)
        return self.msg(mid)

    def states(self):
        sys.path.insert(0, str(self.data / 'plugins' / PLUGIN))
        from secretary.memory import project
        with self.db() as c:
            rows = [dict(r) for r in c.execute('SELECT e.*,i.title,i.creator FROM events e JOIN items i ON i.id=e.item_id ORDER BY e.at,e.seq')]
        for r in rows:
            r['payload'], r['sources'] = json.loads(r['payload']), json.loads(r['sources'])
        return project(rows)

    def item(self, *words):
        found = [s for s in self.states() if all(w in s['title'] for w in words)]
        return found[0] if found else None


def wait_port(port, timeout=30, proc=None):
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError('AstrBot 进程提前退出，见 work 目录中的日志')
        try:
            socket.create_connection(('127.0.0.1', port), 1).close()
            return
        except OSError:
            time.sleep(.5)
    raise TimeoutError(f'端口 {port} 未就绪')


def same_time(value, expected):
    try:
        return datetime.fromisoformat(str(value)) == datetime.fromisoformat(expected)
    except ValueError:
        return False


class NapCat:
    """Simulated OneBot v11 implementation (the NapCat side). Message ids never repeat."""
    ids = [1000]
    store = {}

    def __init__(self, port):
        self.port, self.ws, self.task, self.sent = port, None, None, []

    def new_id(self):
        NapCat.ids[0] += 1
        return NapCat.ids[0]

    async def connect(self):
        from websockets.asyncio.client import connect
        self.ws = await connect(f'ws://127.0.0.1:{self.port}/ws', additional_headers={
            'X-Self-ID': str(SELF), 'X-Client-Role': 'Universal', 'Authorization': 'Bearer sandbox-token'})
        self.task = asyncio.create_task(self.serve())
        await asyncio.sleep(2)

    async def close(self):
        if self.task:
            self.task.cancel()
        if self.ws:
            await self.ws.close()

    async def serve(self):
        async for raw in self.ws:
            req = json.loads(raw)
            action, params, data = req.get('action'), req.get('params', {}), {}
            if action in ('send_group_msg', 'send_msg'):
                m = params.get('message')
                text = m if isinstance(m, str) else ''.join(s.get('data', {}).get('text', '') for s in m if s.get('type') == 'text')
                self.sent.append({'t': time.time(), 'group': params.get('group_id'), 'text': text})
                data = {'message_id': self.new_id()}
            elif action == 'get_msg':
                data = NapCat.store.get(int(params['message_id']), {})
            elif action == 'get_group_member_info':
                uid = int(params['user_id'])
                data = {'user_id': uid, 'nickname': NAMES.get(uid, '群记沙盒'), 'card': '', 'role': 'member'}
            elif action == 'get_login_info':
                data = {'user_id': SELF, 'nickname': '群记沙盒'}
            await self.ws.send(json.dumps({'status': 'ok', 'retcode': 0, 'data': data, 'echo': req.get('echo')}))

    async def say(self, user, text, reply=None, at=False, group=GROUP, mid=None):
        mid = mid or self.new_id()
        seg = ([{'type': 'reply', 'data': {'id': str(reply)}}] if reply else []) + \
              ([{'type': 'at', 'data': {'qq': str(SELF)}}] if at else []) + [{'type': 'text', 'data': {'text': text}}]
        ev = {'time': int(time.time()), 'self_id': SELF, 'post_type': 'message', 'message_type': 'group', 'sub_type': 'normal',
              'message_id': mid, 'group_id': group, 'user_id': user, 'anonymous': None, 'message': seg, 'raw_message': text,
              'font': 0, 'sender': {'user_id': user, 'nickname': NAMES.get(user, str(user)), 'card': '', 'role': 'member'}}
        NapCat.store[mid] = {k: v for k, v in ev.items() if k != 'post_type'}
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return mid

    async def recall(self, mid, user, group=GROUP):
        await self.ws.send(json.dumps({'time': int(time.time()), 'self_id': SELF, 'post_type': 'notice', 'notice_type': 'group_recall',
                                       'group_id': group, 'user_id': user, 'operator_id': user, 'message_id': mid}))

    async def replies(self, n0, quiet=4.0, timeout=30.0):
        """Wait for a first new send (if any), then a quiet window that would catch a duplicate reply."""
        end = time.time() + timeout
        while time.time() < end and len(self.sent) == n0:
            await asyncio.sleep(.2)
        last, t = len(self.sent), time.time()
        while time.time() - t < quiet:
            await asyncio.sleep(.2)
            if len(self.sent) != last:
                last, t = len(self.sent), time.time()
        return self.sent[n0:]


async def scenario(sb: Sandbox):
    real = sb.real
    wait = 90.0 if real else 30.0
    S, M = 'system', 'model'

    # ---- first install: no plugin config yet
    sb.start_astrbot('install')
    end = time.time() + 60
    while time.time() < end and not (sb.pdata / 'config.json').exists():
        await asyncio.sleep(.5)
    await asyncio.sleep(2)
    log = sb.logtext()
    sb.check(S, f'插件在 AstrBot {ASTRBOT_TAG} 中加载并通过版本检查', 'Traceback' not in log and (sb.pdata / 'config.json').exists(),
             '首次启动已生成禁用采集的配置模板' if '已生成配置模板' in log else log[-300:])
    sb.stop_astrbot()

    # ---- configured run
    sb.write_plugin_config()
    sb.start_astrbot('main')
    nc = NapCat(sb.args.ws_port)
    await nc.connect()
    latencies = []

    async def ask(user, text, **kw):
        n0, t = len(nc.sent), time.time()
        await nc.say(user, text, **kw)
        out = await nc.replies(n0, timeout=wait)
        if out:
            latencies.append(round(out[0]['t'] - t, 2))
        return out

    n0 = len(nc.sent)
    m1 = await nc.say(LIN, '中午大家吃什么？')
    r1 = await sb.processed(m1)
    out = await nc.replies(n0, quiet=3, timeout=3)
    sb.check(S, '普通消息入库、不回复', r1 is not None and not out, f"status={r1 and r1['status']} replies={len(out)}")

    m2 = await nc.say(LIN, '建议产品评审放在10月9日下午3点，老张负责，大家看看')
    await sb.processed(m2)
    s = sb.item('评审')
    sb.check(M, '成员提议 → 待确认，时间 10-09 15:00', s and s['status'] == 'unconfirmed' and s['pending']
             and any(same_time(e['payload'].get('when'), '2026-10-09T15:00:00+08:00') for e in s['pending']),
             s and {'status': s['status'], 'pending': [e['payload'] for e in s['pending']]})

    m3 = await nc.say(OWNER, '可以', reply=m2)
    r3 = await sb.processed(m3)
    s = sb.item('评审')
    sb.check(S, '引用回复经 get_msg 传入插件', r3 and r3['reply_to'] == str(m2), r3 and r3['reply_to'])
    sb.check(M, '确认人引用回复“可以” → 已确认，依据含提议', s and s['status'] == 'confirmed'
             and sb.msg(m2)['uid'] in s.get('status_sources', []), s and s['status'])

    m4 = await nc.say(YU, '10月9日那次我来不了')
    await sb.processed(m4)
    s = sb.item('评审')
    people = {**(s or {}).get('participants', {}), **{p: v for o in (s or {}).get('occurrences', {}).values() for p, v in o['participants'].items()}}
    sb.check(M, '“那次我来不了” → 个人缺席，事项不取消', s and s['status'] == 'confirmed' and people.get(str(YU), {}).get('status') == 'absent',
             s and {'status': s['status'], 'people': {k: v['status'] for k, v in people.items()}})

    m5 = await nc.say(YU, '我觉得产品评审取消算了')
    await sb.processed(m5)
    s = sb.item('评审')
    sb.check(M, '非确认人建议取消 → 不覆盖正式结论', s and s['status'] == 'confirmed', s and s['status'])

    m6 = await nc.say(OWNER, '产品评审改到10月12日上午10点，数据还没齐')
    await sb.processed(m6)
    s = sb.item('评审')
    sb.check(M, '确认人改期 → 新时间 10-12 10:00 生效', s and same_time(s['fields'].get('when'), '2026-10-12T10:00:00+08:00'),
             s and s['fields'])

    t0 = time.time()
    out = await ask(LIN, '/问 产品评审现在怎么定的')
    sb.check(S, '/问 只收到一条回复（默认 AI 未抢答）', len(out) == 1 and not sb.mock_calls('astrbot_default', t0),
             f'replies={len(out)}')
    text = out[0]['text'] if out else ''
    sb.check(M, '/问 回答给出新时间并附依据', ('10-12' in text or '10月12' in text) and '依据' in text, text[:200])
    sb.report['observations']['sample_answer'] = text

    t_at = time.time()
    out = await ask(LIN, '产品评审谁负责？', at=True)
    sb.check(S, '@机器人提问只收到一条回复', len(out) == 1, f'replies={len(out)}')
    sb.report['observations']['at_answer'] = out[0]['text'] if out else ''
    if sb.args.frontend == 'astrbot' and not real:
        await native_checks(sb, ask, out, t_at)

    before = sb.count('SELECT COUNT(*) FROM events')
    n0 = len(nc.sent)
    await nc.say(OWNER, '可以', reply=m2, mid=m3)
    out = await nc.replies(n0, quiet=3, timeout=3)
    sb.check(S, '重复投递同一消息 → 不重复记事、不回复', before == sb.count('SELECT COUNT(*) FROM events') and not out)

    await nc.recall(m6, OWNER)
    end = time.time() + 20
    while time.time() < end and not (sb.msg(m6) or {}).get('erased'):
        await asyncio.sleep(.3)
    uid6 = sb.msg(m6)['uid'] if sb.msg(m6) else ''
    with sb.db() as c:
        still = [r[0] for r in c.execute('SELECT sources FROM events WHERE valid=1') if uid6 and uid6 in r[0]]
    sb.check(S, 'OneBot 撤回通知 → 原文清除，依赖它的事件失效', (sb.msg(m6) or {}).get('erased') == 1 and not still)
    s = sb.item('评审')
    sb.check(M, '撤回改期后不回退到旧时间', s and not same_time(s['fields'].get('when'), '2026-10-09T15:00:00+08:00'), s and s['fields'])

    for cmd, needle in [('/秘书帮助', '/问'), ('/事项', '评审'), ('/秘书状态', 'sandbox'), ('/群报 1', '评审')]:
        out = await ask(OWNER, cmd)
        sb.check(S, f'{cmd} 单条回复且内容正确', len(out) == 1 and needle in out[0]['text'], f'replies={len(out)}')

    out = await ask(LIN, '/记事 资料审核 | 提议 | 负责人=小林;优先级=4')
    pending = (sb.item('资料审核') or {}).get('pending', [])
    sb.check(S, '/记事 提议 → 待确认', pending and out and '待有权限的成员确认' in out[0]['text'])
    if pending:
        await ask(OWNER, '/确认 ' + pending[0]['id'][:12])
        sb.check(S, '/确认 事件ID → 已确认', (sb.item('资料审核') or {}).get('status') == 'confirmed')

    out = await ask(OWNER, '/sid')
    sb.report['observations']['sid_replies'] = [o['text'][:80] for o in out]

    n0 = len(nc.sent)
    mo = await nc.say(LIN, '随便聊聊', group=OTHER)
    await nc.replies(n0, quiet=3, timeout=3)
    sb.check(S, '未配置群的消息不被插件记录', sb.msg(mo) is None)

    await ask(YU, '/别记我')
    left = sb.count('SELECT COUNT(*) FROM messages WHERE sender=? AND erased=0', (str(YU),))
    my = await nc.say(YU, '这句不应被记录')
    await asyncio.sleep(2)
    sb.check(S, '/别记我 → 清除已有消息，后续不记录', left == 0 and sb.msg(my) is None, f'left={left}')

    if not real:
        (sb.work / 'FAIL_EXTRACT').touch()
        t = time.time()
        mf = await nc.say(LIN, '建议产品评审再议')
        end = time.time() + 150
        while time.time() < end and (sb.msg(mf) or {}).get('status') != 'failed':
            await asyncio.sleep(1)
        sb.report['observations']['seconds_until_failed_during_outage'] = round(time.time() - t)
        (sb.work / 'FAIL_EXTRACT').unlink()
        await sb.processed(await nc.say(LIN, '服务恢复后的普通消息'))
        sb.report['observations']['failed_message_after_recovery'] = (sb.msg(mf) or {}).get('status')

    if sb.args.frontend == 'astrbot':
        sb.check(S, '配置群内没有不带群记上下文的默认 AI 回复', not sb.mock_calls('astrbot_default', 0) if not real else True)
    with sb.db() as c:
        sb.report['observations']['message_status'] = {r[0]: r[1] for r in c.execute('SELECT status,COUNT(*) FROM messages GROUP BY status')}
        sb.report['observations']['model_usage'] = [dict(r) for r in c.execute(
            'SELECT role,COUNT(*) AS calls,ROUND(SUM(seconds),1) AS seconds,SUM(error!=\'\') AS errors FROM usage GROUP BY role')]
    sb.report['observations']['reply_latency_seconds'] = latencies
    await nc.close()
    sb.stop_astrbot()

    # ---- restart
    before = sb.count('SELECT COUNT(*) FROM events')
    sb.start_astrbot('restart')
    nc = NapCat(sb.args.ws_port)
    await nc.connect()
    out = await ask(LIN, '/问 资料审核现在怎么定的')
    sb.check(S, '重启后状态保留、无重复事件', out and '资料审核' in out[0]['text'] and before == sb.count('SELECT COUNT(*) FROM events'))
    sb.check(S, '全程 AstrBot 日志无异常堆栈', all('Traceback' not in p.read_text(errors='replace') for p in sb.work.glob('astrbot-*.log')))
    await nc.close()
    sb.stop_astrbot()


async def native_checks(sb, ask, first_reply, t0):
    """AstrBot-native frontend: host agent answers with plugin context and real tool calls."""
    S = 'system'
    calls = sb.mock_calls('host_agent', t0)
    first = calls[0] if calls else {}
    sb.check(S, '@ 由宿主 agent 回答：带群聊上下文、群记工具，且不带宿主旧历史',
             bool(first) and '中午大家吃什么' in first['system'] and 'search_group_history' in first['tools']
             and first['history'] == 1, {k: first.get(k) for k in ('tools', 'history')})
    text = first_reply[0]['text'] if first_reply else ''
    sb.check(S, '回复去掉 Markdown 并记为 native 回答', '**' not in text and text.startswith('收到')
             and sb.count("SELECT COUNT(*) FROM answers WHERE mode='native'") == 1, text[:80])

    t = time.time()
    out = await ask(YU, '之前谁说过中午吃什么', at=True)
    turn = sb.mock_calls('host_agent', t)
    sb.check(S, '宿主 agent 调用 search_group_history 查到原话', len(out) == 1 and '中午大家吃什么' in out[0]['text']
             and bool(turn) and turn[0]['history'] == 1 and ' 你] 收到' in turn[0]['system'],
             out[0]['text'][:80] if out else 'no reply')

    out = await ask(OWNER, '帮我们设计评审后的聚餐方案', at=True)
    drafts = sb.count("SELECT COUNT(*) FROM drafts WHERE title LIKE '%聚餐%'")
    sb.check(S, 'save_group_drafts 经宿主工具链保存草案，回复单条', len(out) == 1 and drafts == 1
             and '**' not in out[0]['text'] and '希望对你有帮助' not in out[0]['text'],
             f"drafts={drafts} reply={out[0]['text'][:60] if out else ''}")
    out = await ask(OWNER, '就按这个方案定了', at=True)
    s = sb.item('聚餐')
    sb.check(S, '确认人自然确认 → 正式记录并附程序回执', len(out) == 1 and '（「聚餐」已记录）' in out[0]['text']
             and s and s['status'] == 'confirmed', out[0]['text'][:80] if out else 'no reply')

    await ask(LIN, '帮我设计周会方案', at=True)
    out = await ask(LIN, '就按这个方案定了', at=True)
    s = sb.item('周会')
    sb.check(S, '非确认人自然确认 → 只进入待确认', len(out) == 1 and '等确认人确认' in out[0]['text']
             and s and s['status'] != 'confirmed', out[0]['text'][:80] if out else 'no reply')


def inner(args):
    settings = model_settings(args)
    sb = Sandbox(args, settings)
    if sb.real:
        sb.report['preflight'] = preflight(settings)
    output = Path(args.output) if args.output else LAB / 'results' / f'sandbox-{args.model}.json'
    sb.prepare()
    try:
        if not sb.real:
            sb.start_mock()
        asyncio.run(scenario(sb))
    finally:
        sb.stop_all()
        sb.report['finished'] = datetime.now().isoformat(timespec='seconds')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(sb.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for kind, label in (('system', '系统管道'), ('model', '模型理解')):
        rows = [c for c in sb.report['checks'] if c['kind'] == kind]
        print(f'{label}：{sum(c["pass"] for c in rows)}/{len(rows)} 通过', flush=True)
    print('报告：', output)
    return int(not all(c['pass'] for c in sb.report['checks'] if c['kind'] == 'system'))


def main():
    args = parse()
    home = Path(args.home).resolve()
    source = Path(args.astrbot).resolve() if args.astrbot else home / 'astrbot'
    venv = (source / '.venv').resolve()
    if Path(sys.prefix).resolve() != venv:
        if args.model != 'mock':
            model_settings(args)  # validate choices before the long install
        if args.astrbot:
            py = source / '.venv/bin/python'
            if not (source / 'main.py').exists() or not py.exists():
                sys.exit(f'{source} 不是含 .venv 的 AstrBot 源码目录')
        else:
            py = ensure_astrbot(home)
        os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])
    return inner(args)


if __name__ == '__main__':
    raise SystemExit(main())
