"""Convenient local start. Generated access token is printed only to the operator console."""
import os
import secrets
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from secretary.cli import main

if not os.environ.get('GROUPBOT_ADMIN_TOKEN'):
    os.environ['GROUPBOT_ADMIN_TOKEN']=secrets.token_urlsafe(32)
print('本机审核页令牌（粘贴到页面连接框）：',os.environ['GROUPBOT_ADMIN_TOKEN'],flush=True)
sys.argv=['groupsecretary','serve','--config',str(root/'examples/demo.config.json')]
main()
