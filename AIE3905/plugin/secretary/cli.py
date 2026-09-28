from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from .config import Config
from .engine import Engine
from .sample import demo_config, demo_messages
from .types import Actor, Message, digest
from .web import AuditServer


def load_records(path,dataset=None):
    path=Path(path)
    raw=path.read_text(encoding='utf-8-sig')
    name=dataset or digest(raw)[:20]
    records=[]
    for index,line in enumerate(raw.splitlines(),1):
        if not line.strip():
            continue
        row=json.loads(line)
        row.setdefault('dataset',name);row.setdefault('row',index)
        records.append(Message(**row))
    return records


async def run(args):
    if args.action=='demo':
        with tempfile.TemporaryDirectory(prefix='groupsecretary-') as folder:
            e=Engine(Config(demo_config(),folder))
            try:
                await e.start(maintenance=False)
                for m in demo_messages(): e.ingest(m)
                await e.flush('demo')
                print('完全虚构的功能演示｜规则模式，未调用模型\n')
                for q in ['产品演示现在怎么定的','产品演示为什么改期']:
                    result=await e.query(Actor('owner',['demo'],True),'demo',q)
                    print(q+'\n'+result['text']+'\n')
            finally:
                await e.close()
        return
    cfg=Config.load(args.config)
    e=Engine(cfg)
    web=None
    try:
        await e.start(maintenance=args.action=='serve')
        if args.action=='replay':
            records=load_records(args.input,args.dataset)
            if any(m.group not in cfg.groups for m in records):
                raise ValueError('输入包含未配置的群')
            accepted=0
            for m in records:
                r=e.ingest(m);accepted+=int(bool(r and r['_new']))
            ready=all([await e.flush(k,timeout=args.timeout) for k in {m.group for m in records}])
            print(json.dumps({'new_messages':accepted,'processing_finished':ready,'groups':{
                k:e.status(Actor('replay',[k],True),k) for k in {m.group for m in records}
            }},ensure_ascii=False,indent=2))
        elif args.action=='serve':
            web=AuditServer(e);web.start()
            print(f'审核页：{web.url}（令牌从配置指定的环境变量读取）',flush=True)
            await asyncio.Event().wait()
    finally:
        if web: await web.close()
        await e.close()


def main():
    parser=argparse.ArgumentParser(description='企业群聊助手：AstrBot 核心与离线工具')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('demo',help='无需账号的虚构规则演示')
    serve=sub.add_parser('serve',help='启动本地审核页')
    serve.add_argument('--config',required=True)
    replay=sub.add_parser('replay',help='回放获准的 JSONL 消息')
    replay.add_argument('--config',required=True);replay.add_argument('--input',required=True)
    replay.add_argument('--dataset');replay.add_argument('--timeout',type=float,default=120)
    args=parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__=='__main__':
    main()
