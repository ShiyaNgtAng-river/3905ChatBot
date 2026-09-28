"""Completely fictional, deterministic-command demo. No enterprise or community records."""
from datetime import datetime, timedelta, timezone
from .types import Message


def demo_messages(group='demo'):
    base=datetime.now(timezone.utc)-timedelta(hours=3)
    day=(base+timedelta(days=2)).date().isoformat()
    later=(base+timedelta(days=5)).date().isoformat()
    rows=[
        ('lin','小林','中午大家吃什么？',''),
        ('lin','小林',f'/记事 产品演示 | 提议 | 时间={day}T15:00:00+08:00;负责人=老张;优先级=5',''),
        ('owner','老张','可以','demo-2'),
        ('yu','小余','我去买咖啡啦',''),
        ('owner','老张',f'/记事 产品演示 | 变更 | 时间={later};原因=数据还没准备齐;优先级=5',''),
        ('lin','小林',f'/记事 产品演示 | 缺席 | 单次={later}',''),
        ('yu','小余','/记事 资料审核 | 提议 | 负责人=小余;备注=需要再核对一版数据;阻塞=待数据;优先级=4',''),
        ('owner','老张','/记事 资料审核 | 确认 | 负责人=小余;备注=需要再核对一版数据;阻塞=待数据;优先级=4',''),
        ('lin','小林','/记事 产品演示 | 取消 | 原因=听说要取消，尚未获负责人确认',''),
    ]
    return [Message(group,sender,text,(base+timedelta(minutes=i)).isoformat(),native_id=f'demo-{i+1}',name=name,reply_to=reply) for i,(sender,name,text,reply) in enumerate(rows)]


def demo_config(database='data/demo.sqlite3'):
    return {'mode':'demo','database':database,'groups':[{
        'key':'demo','enabled':True,'data_use_confirmed':True,'admins':['owner'],
        'confirmers':['owner'],'processing_location':'本机规则解析；未调用模型',
    }], 'web':{'host':'127.0.0.1','port':8765,'tokens':[{
        'env':'GROUPBOT_ADMIN_TOKEN','user':'owner','groups':['demo'],'admin':True
    }]}}
