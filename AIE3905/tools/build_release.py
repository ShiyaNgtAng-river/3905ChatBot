"""Build two source-only archives with a manifest; never package runtime state."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.2.1'
OUTPUT = ROOT/'dist'
DIRECTORIES = ('plugin','config','datasets','results','docs','tools')
FILES = ('README.md','CHANGELOG.md','SOURCE.json','.gitignore','run','lab.py','test_lab.py')
CONFIGS = {'demo.json','deepseek.json','qwen.json','online.template.json','qq.template.json','qq.astrbot.template.json'}


def keep(path):
    rel = path.relative_to(ROOT)
    if str(rel) == 'tools/apply_qq_pilot_config.py': return False
    if any(part in {'.venv','__pycache__','.git','data','dist'} for part in rel.parts): return False
    if path.name.startswith(('.env','.DS_Store','._')): return False
    if path.suffix in {'.pyc','.db','.sqlite','.sqlite3','.log'}: return False
    if '.sqlite3' in path.name or path.name.endswith('.lock'): return False
    if rel.parts[0] == 'config' and path.name not in CONFIGS: return False
    return path.is_file()


def archive(name, prefix, pairs):
    entries = {str(rel):path.read_bytes() for path,rel in pairs}
    manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {rel}\n' for rel,data in sorted(entries.items()))
    entries['MANIFEST.sha256'] = manifest.encode()
    target = OUTPUT/name
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for rel,data in sorted(entries.items()):
            info = zipfile.ZipInfo(prefix+'/'+rel, (2026,9,28,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = ((0o100755 if rel=='run' else 0o100644) << 16)
            z.writestr(info,data)
    return target


def main():
    OUTPUT.mkdir(exist_ok=True)
    paths = [ROOT/name for name in FILES]
    for name in DIRECTORIES: paths.extend(p for p in (ROOT/name).rglob('*') if keep(p))
    full = archive(f'AIE3905_GroupBot_v{VERSION}.zip',f'AIE3905_GroupBot_v{VERSION}',
                   [(p,p.relative_to(ROOT)) for p in sorted(paths)])
    plugin = archive(f'astrbot_plugin_groupsecretary_v{VERSION}.zip','astrbot_plugin_groupsecretary',
                     [(p,p.relative_to(ROOT/'plugin')) for p in sorted((ROOT/'plugin').rglob('*')) if keep(p)])
    sums = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in (full,plugin))
    (OUTPUT/'SHA256SUMS.txt').write_text(sums,encoding='utf-8')
    print(json.dumps([{'file':p.name,'bytes':p.stat().st_size} for p in (full,plugin)],indent=2))


if __name__=='__main__': main()
