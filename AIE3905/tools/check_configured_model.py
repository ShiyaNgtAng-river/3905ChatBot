"""Test configured provider with synthetic data; never print or persist credentials."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'plugin'))
from secretary.config import Config
from secretary.engine import Engine
from secretary.providers import OpenAICompatible, json_object
from secretary.types import Message, Actor
import secretary.engine as engine_module

async def main():
    config_path = Path(sys.argv[1])
    host = json.loads(config_path.read_text(encoding='utf-8-sig'))
    model = next(m for m in host['provider'] if m['id']=='deepseek/deepseek-flash')
    source = next(s for s in host['provider_sources'] if s['id']==model['provider_source_id'])
    keys = source['key']
    key = next(k for k in keys if k) if isinstance(keys,list) else keys
    os.environ['GROUPBOT_MODEL_API_KEY'] = key
    settings = {'base_url':source['api_base'],'model':model['model'],'allow_remote':True,
                'json_mode':True,'timeout':45,'max_tokens':1800,
                'extra_body':{'thinking':{'type':'disabled'}}}
    traces = []
    original_validate = engine_module.validate_candidates
    def traced_validate(*args, **kwargs):
        try: return original_validate(*args, **kwargs)
        except Exception as exc:
            traces.append({'validation_error':str(exc),'candidates':args[3]})
            raise
    engine_module.validate_candidates = traced_validate
    base = datetime.now(timezone.utc)
    texts = [('lin','建议集成测试评审在2026-10-02举行，负责人是老张，还没定。',''),
             ('owner','可以','check-1'),
             ('owner','集成测试评审正式改到2026-10-06，原因是样本尚未备齐。','')]
    with tempfile.TemporaryDirectory(prefix='groupbot-model-check-') as folder:
        cfg = {'mode':'openai','database':str(Path(folder)/'test.sqlite3'),
               'models':{'understanding':settings,'answering':settings},'max_attempts':1,
               'groups':[{'key':'lab','enabled':True,'data_use_confirmed':True,
                          'admins':['owner'],'confirmers':['owner'],'processing_location':'DeepSeek API；虚构测试数据'}],'query_wait_seconds':60}
        e = Engine(Config(cfg))
        provider = e.extractor.provider
        original_complete = provider.complete
        async def traced_complete(*args, **kwargs):
            result = await original_complete(*args, **kwargs)
            traces.append({'role':args[2],'response':result})
            return result
        provider.complete = traced_complete
        try:
            await e.start(maintenance=False)
            for i,(sender,text,reply) in enumerate(texts,1):
                e.ingest(Message('lab',sender,text,(base+timedelta(seconds=i)).isoformat(),native_id=f'check-{i}',reply_to=reply))
            ready = await e.flush('lab',timeout=170)
            answer = await e.query(Actor('owner',['lab'],True),'lab','集成测试评审现在怎么定的？',as_of=(base+timedelta(seconds=4)).isoformat())
            report = {'ready':ready,'provider':model['id'],'test_data':'entirely synthetic',
                      'transport':'direct compatible API; not AstrBot runtime',
                      'status':e.status(Actor('owner',['lab'],True),'lab'),
                      'states':[{'title':s['title'],'status':s['status'],'fields':s['fields']} for s in e.states('lab')],
                      'answer':answer['text'],'synthetic_traces':traces}
            report['passed'] = bool(ready and len(report['states']) == 1
                and report['states'][0]['status'] == 'confirmed'
                and report['states'][0]['fields'].get('when') == '2026-10-06'
                and all(row['status']=='done' for row in report['status']['counts'])
                and '2026-10-06' in report['answer'])
            path = ROOT/'results/configured-deepseek-check.json'
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            if not report['passed']:
                raise RuntimeError('Synthetic pipeline acceptance failed')
        finally:
            await e.close()

if __name__=='__main__':
    try: asyncio.run(main())
    except Exception as exc:
        print('Check failed:',type(exc).__name__,flush=True)
        raise SystemExit(1)
