"""Apply tested pilot settings, preserving existing credentials and backups."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
host = Path('/Users/tangshiyang/CUHKSZ/AstrBot/AstrBot-master')
backup = host/'data'/'groupsecretary_backups'/datetime.now().strftime('%Y%m%d-%H%M%S')
backup.mkdir(parents=True,exist_ok=False)
os.chmod(backup,0o700)

for name in ('memory.py','extract.py'):
    destination = host/'data/plugins/astrbot_plugin_groupsecretary/secretary'/name
    shutil.copy2(destination,backup/name)
    shutil.copy2(root/'plugin/secretary'/name,destination)

config = host/'data/config/astrbot_plugin_groupsecretary_config.json'
shutil.copy2(config,backup/config.name)
options = json.loads(config.read_text(encoding='utf-8-sig'))
options['config_path'] = str(root/'config/qq.json')
config.write_text(json.dumps(options,ensure_ascii=False,indent=2)+'\n')

main_config = host/'data/cmd_config.json'
shutil.copy2(main_config,backup/main_config.name)
os.chmod(backup/main_config.name,0o600)
settings = json.loads(main_config.read_text(encoding='utf-8-sig'))
model = next(x for x in settings['provider'] if x['id']=='deepseek/deepseek-flash')
model.setdefault('custom_extra_body',{})['thinking'] = {'type':'disabled'}
main_config.write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n')
print('Applied plugin fixes and QQ pilot configuration; restart required.')
print('Backup directory:',backup)
