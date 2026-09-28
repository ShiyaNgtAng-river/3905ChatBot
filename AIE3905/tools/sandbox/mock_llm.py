"""OpenAI-compatible stand-in for DeepSeek/Qwen. Deterministic, local, logs every call.

Classifies each request as plugin extraction / plugin answer selection / plugin social /
host agent answering a configured group (system prompt carries <group_chat>) / AstrBot
default chat, so the harness can count duplicate default-LLM replies. The host agent
path returns real tool calls so AstrBot executes the plugin's group tools.
The extraction rules only understand the fixed sandbox script in run.py.

usage: mock_llm.py <port> <work_dir>
"""
import json
import os
import re
import sys
import time

from aiohttp import web

PORT, WORK = int(sys.argv[1]), sys.argv[2]
LOG = os.path.join(WORK, 'mock_llm.jsonl')
FAIL_FLAG = os.path.join(WORK, 'FAIL_EXTRACT')  # present = simulate a model outage for extraction


def extract(payload):
    cur = payload['current']
    text = cur['text']
    reply = payload.get('reply')
    cands = payload.get('candidates', [])
    if '建议' in text and '评审' in text:
        return [{'title': '产品评审', 'kind': 'propose', 'fields': {'when': '2026-10-09T15:00:00+08:00', 'owner': '老张'}}]
    if text.strip() == '可以' and reply:
        for c in cands:
            if c['pending']:
                return [{'title': c['title'], 'kind': 'confirm', 'fields': {}, 'target': c['pending'][-1]['id'],
                         'sources': [reply['uid']]}]
        return []
    if '来不了' in text:
        return [{'title': '产品评审', 'kind': 'participant', 'fields': {}, 'participant': cur['sender'],
                 'participant_status': 'absent', 'scope': 'occurrence', 'occurrence': '2026-10-09'}]
    if '取消' in text and '评审' in text:
        return [{'title': '产品评审', 'kind': 'cancel', 'fields': {'reason': '个人建议取消'}}]
    if '改到' in text and '评审' in text:
        return [{'title': '产品评审', 'kind': 'change', 'fields': {'when': '2026-10-12T10:00:00+08:00', 'reason': '数据还没齐'}}]
    return []


def host_agent(system, msgs, user):
    """Scripted host agent: returns (content, tool_call) for the sandbox dialogue."""
    last = msgs[-1]
    if last['role'] == 'tool':
        result = str(last.get('content', ''))
        if '"error"' in result:
            return '没办成：' + json.loads(result).get('error', ''), None
        if '"operations"' in result:
            return '好，定了。', None
        if '"messages"' not in result and '"summary"' in result:
            summaries = re.findall(r'"summary": ?"([^"]+)"', result)
            return '今天主要聊了：' + summaries[0], None
        if '"drafts"' in result:
            return '**方案1**：评审后周五晚上七点，先作为建议。\n希望对你有帮助！', None
        found = re.findall(r'"text": ?"([^"]+)"', result)
        return ('我翻了下记录：' + found[0]) if found else '没翻到相关记录。', None
    if '设计' in user:
        title = '聚餐' if '聚餐' in user else '周会'
        return None, ('save_group_drafts', {'options': [{'number': 1, 'title': title, 'description': '周五晚上七点',
                                                          'fields': {'when': '2026-10-16T19:00:00+08:00'}}]})
    if '就按' in user:
        ids = re.findall(r'id=([0-9a-f]{24})', system)
        return None, ('submit_group_events', {'events': [{'kind': 'confirm', 'draft_id': ids[0] if ids else 'none'}]})
    if '聊了什么' in user:
        return None, ('get_group_episodes', {'when': '今天'})
    if '之前' in user or '谁说' in user:
        return None, ('search_group_history', {'query': '中午 吃什么'})
    return '**收到**，评审是老张负责。', None


async def chat(request):
    body = await request.json()
    msgs = body.get('messages', [])
    system = next((m['content'] for m in msgs if m['role'] == 'system' and isinstance(m['content'], str)), '')
    user = next((m['content'] for m in reversed(msgs) if m['role'] == 'user'), '')
    if isinstance(user, list):
        user = ''.join(p.get('text', '') for p in user if isinstance(p, dict))
    if '你负责从多人群聊中提取事项事件' in system:
        kind = 'plugin_extract'
        if os.path.exists(FAIL_FLAG):
            with open(LOG, 'a') as f:
                f.write(json.dumps({'t': time.time(), 'kind': kind, 'failed': True}) + '\n')
            return web.json_response({'error': {'message': 'simulated outage'}}, status=503)
        content = json.dumps({'events': extract(json.loads(user))}, ensure_ascii=False)
    elif '你负责把一段群聊整理成话题摘要' in system:
        kind = 'plugin_episode'
        first = json.loads(user)['messages'][0]['text']
        content = json.dumps({'summary': '沙盒话题：' + first[:12], 'topics': ['沙盒']}, ensure_ascii=False)
    elif '你负责维护群成员的简短印象卡' in system:
        kind = 'plugin_profile'
        members = json.loads(user)['members']
        content = json.dumps({'profiles': [{'sender': m['sender'], 'summary': m['name'] + '在沙盒群发言'}
                                           for m in members]}, ensure_ascii=False)
    elif '你负责选择与问题相关的事实条目' in system:
        kind = 'plugin_answer'
        claims = json.loads(user)['claims']
        content = json.dumps({'opening_index': 0, 'claim_ids': [claims[0]['id']] if claims else []})
    elif '输出 JSON {"text"' in system:
        kind = 'plugin_social'
        content = json.dumps({'text': '你好，我是测试助手。'}, ensure_ascii=False)
    elif '<group_chat>' in system:
        kind = 'host_agent'
        content, call = host_agent(system, msgs, str(user))
    else:
        kind = 'astrbot_default'
        content = '【AstrBot默认LLM】这是一条默认聊天回复'
    tools = [t.get('function', {}).get('name') for t in body.get('tools') or []]
    history = [m for m in msgs if m['role'] in ('user', 'assistant')]
    with open(LOG, 'a') as f:
        f.write(json.dumps({'t': time.time(), 'kind': kind, 'user': str(user)[:200], 'tools': tools,
                            'history': len(history), 'system': system[-6000:] if kind == 'host_agent' else ''},
                           ensure_ascii=False) + '\n')
    message = {'role': 'assistant', 'content': content}
    if kind == 'host_agent' and call:
        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'call_%d' % int(time.time() * 1000), 'type': 'function',
            'function': {'name': call[0], 'arguments': json.dumps(call[1], ensure_ascii=False)}}]}
    return web.json_response({'id': 'mock', 'object': 'chat.completion', 'created': int(time.time()), 'model': body.get('model'),
                              'choices': [{'index': 0, 'message': message,
                                           'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop'}],
                              'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


async def models(request):
    return web.json_response({'object': 'list', 'data': [{'id': 'mock-model', 'object': 'model'}]})

app = web.Application()
app.router.add_post('/v1/chat/completions', chat)
app.router.add_get('/v1/models', models)
web.run_app(app, host='127.0.0.1', port=PORT, print=None)
