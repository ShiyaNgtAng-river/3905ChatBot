"""OpenAI-compatible stand-in for DeepSeek/Qwen. Deterministic, local, logs every call.

Classifies each request as plugin extraction / plugin answer selection / plugin social /
AstrBot default chat, so the harness can count duplicate default-LLM replies.
The extraction rules only understand the fixed sandbox script in run.py.

usage: mock_llm.py <port> <work_dir>
"""
import json, os, sys, time
from aiohttp import web

PORT, WORK = int(sys.argv[1]), sys.argv[2]
LOG = os.path.join(WORK, 'mock_llm.jsonl')
FAIL_FLAG = os.path.join(WORK, 'FAIL_EXTRACT')  # present = simulate a model outage for extraction


def extract(payload):
    cur = payload['current']; text = cur['text']; reply = payload.get('reply')
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
            with open(LOG, 'a') as f: f.write(json.dumps({'t': time.time(), 'kind': kind, 'failed': True}) + '\n')
            return web.json_response({'error': {'message': 'simulated outage'}}, status=503)
        content = json.dumps({'events': extract(json.loads(user))}, ensure_ascii=False)
    elif '你负责选择与问题相关的事实条目' in system:
        kind = 'plugin_answer'
        claims = json.loads(user)['claims']
        content = json.dumps({'opening_index': 0, 'claim_ids': [claims[0]['id']] if claims else []})
    elif '输出 JSON {"text"' in system:
        kind = 'plugin_social'
        content = json.dumps({'text': '你好，我是测试助手。'}, ensure_ascii=False)
    else:
        kind = 'astrbot_default'
        content = '【AstrBot默认LLM】这是一条默认聊天回复'
    with open(LOG, 'a') as f:
        f.write(json.dumps({'t': time.time(), 'kind': kind, 'user': str(user)[:200]}, ensure_ascii=False) + '\n')
    return web.json_response({'id': 'mock', 'object': 'chat.completion', 'created': int(time.time()), 'model': body.get('model'),
                              'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                              'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


async def models(request):
    return web.json_response({'object': 'list', 'data': [{'id': 'mock-model', 'object': 'model'}]})

app = web.Application()
app.router.add_post('/v1/chat/completions', chat)
app.router.add_get('/v1/models', models)
web.run_app(app, host='127.0.0.1', port=PORT, print=None)
