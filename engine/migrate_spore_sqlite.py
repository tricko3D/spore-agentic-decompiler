#!/usr/bin/env python3
"""One-time non-destructive import from the current Spore CSV+JSON runner."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'work'/'scratch'))
import spore_sqlite_store as store


def old_runners():
    found=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit() or int(p.name)==__import__('os').getpid():
            continue
        try:
            argv=[x.decode('utf-8','replace') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
        except (OSError,PermissionError):
            continue
        names=('spore_first_pass.py','spore_first_pass_parallel.py','spore_first_pass_sqlite.py')
        if 'run' in argv and any(any(a == n or a.endswith('/'+n) for n in names) for a in argv):
            found.append((p.name,' '.join(argv)[:160]))
    return found


def main():
    if Path.cwd().resolve()!=ROOT:
        sys.exit('Run from your spore-decomp repository root.')
    if not (ROOT/'tools/contrib/task.py').is_file():
        sys.exit('This does not look like the spore-decomp repository.')
    live=old_runners()
    if live:
        sys.exit('Stop the existing batch before migrating. Active processes:\n'+
                 '\n'.join(f'  PID {pid} {cmd}' for pid,cmd in live))
    info=store.migrate_legacy()
    store.ensure_imported()
    q,rs,n=store.summary()
    print('Already migrated, verified current legacy hashes.' if info['already'] else 'Legacy migration complete.')
    print(f'Queue: {q} functions | Historical attempts: {n} | Function outcomes: {len(rs)}')
    print(f'SQLite: {store.DB}')
    print(f'Original CSV/JSON files are preserved, unmodified.')
    print('C++ sources and JSONL logs remain on disk and can be reused.')
    print('Do not start the legacy CSV runner after this migration.')

if __name__=='__main__':
    main()
