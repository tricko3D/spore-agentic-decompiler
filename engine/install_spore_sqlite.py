#!/usr/bin/env python3
"""Install cross-process checker lock, retaining an untouched backup."""
from pathlib import Path
from datetime import datetime, timezone
import os,sys,shutil,re
ROOT=Path(__file__).resolve().parent
MARKER='SPGONZAGO_SQLITE_CHECK_LOCK_V1'


def old_runners():
    out=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid(): continue
        try: args=[a.decode('utf-8','replace') for a in (p/'cmdline').read_bytes().split(b'\0') if a]
        except (OSError,PermissionError):continue
        names=('spore_first_pass.py','spore_first_pass_sqlite.py','spore_first_pass_parallel.py')
        if 'run' in args and any(any(a == n or a.endswith('/'+n) for n in names) for a in args):
            out.append((p.name,' '.join(args)[:120]))
    return out


def main():
    if Path.cwd().resolve()!=ROOT:sys.exit('Run from your spore-decomp repository root.')
    p=ROOT/'tools/contrib/check.py'
    if not p.is_file():sys.exit('Missing tools/contrib/check.py')
    if not (ROOT/'work/scratch/spore_first_pass_sqlite.py').is_file():
        sys.exit('Missing SQLite runner. Extract the ZIP in repository root first.')
    live=old_runners()
    if live:sys.exit('Stop all existing runners before patching the shared checker:\n'+str(live[:4]))
    # The checker lock can already be installed while SQLite still needs an
    # additive schema migration (for example, inference_slots.verified).
    # Always perform this migration, including on repeated setup runs.
    sys.path.insert(0,str(ROOT/'work/scratch'))
    import spore_sqlite_store as store
    store.init_schema()
    print('SQLite schema up to date; existing attempt history retained.')
    code=p.read_text(encoding='utf-8')
    if MARKER in code:
        print('Checker lock already installed.');return
    target=re.compile(r"(?m)^if __name__ == [\'\"]__main__[\'\"]:\n[ \t]+sys\.exit\(main\(\)\)[ \t]*$" )
    if len(target.findall(code))!=1:
        sys.exit('Unexpected check.py version: refusing to patch. Need a manual compatibility review.')
    backup_dir=ROOT/'work/scratch/codex-pass1/sqlite-backups'
    backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/('check.py.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.bak')
    shutil.copy2(p,backup)
    replacement=f'''# {MARKER}\nif __name__ == '__main__':
    import fcntl as _spore_fcntl
    _spore_lock_path = os.path.join(ROOT, 'work', 'scratch', 'codex-pass1', 'sqlite-check.lock')
    os.makedirs(os.path.dirname(_spore_lock_path), exist_ok=True)
    with open(_spore_lock_path, 'a+') as _spore_lock:
        _spore_fcntl.flock(_spore_lock.fileno(), _spore_fcntl.LOCK_EX)
        sys.exit(main())'''
    new=target.sub(replacement,code)
    compile(new,str(p),'exec')
    tmp=p.with_name(p.name+f'.spore-sqlite-{os.getpid()}.tmp')
    tmp.write_text(new,encoding='utf-8')
    os.chmod(tmp,p.stat().st_mode)
    os.replace(tmp,p)
    print('Checker serialized across concurrent workers.')
    print('Backup:',backup.relative_to(ROOT))
    print('Original CSV runner preserved. Use spore_parallel_sqlite.py run.')

if __name__=='__main__':main()
