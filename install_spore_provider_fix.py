#!/usr/bin/env python3
"""Safely install the November 17 provider recovery fix. Preview by default.

Run from ~/spore-decomp. --apply saves existing scripts to a timestamped
backup directory, then installs tested replacements. Never edits SQLite.
"""
from datetime import datetime, timezone
import argparse
import os
from pathlib import Path
import py_compile
import shutil
import sys

ROOT=Path.cwd()
STAGE=Path(__file__).resolve().parent/'spore_provider_fix'
FILES=[
    'spore_parallel_sqlite.py',
    'spore_matched_bytes.py',
    'spore_recover_provider_attempts.py',
    'work/scratch/spore_first_pass_sqlite.py',
    'work/scratch/spore_sqlite_store.py',
    'work/scratch/spore_provider_log.py',
]


def active_workers():
    proc=Path('/proc')
    if not proc.is_dir():
        raise RuntimeError('/proc unavailable, cannot ensure workers are stopped')
    ids=[]
    for p in proc.iterdir():
        if not p.name.isdigit() or int(p.name)==os.getpid():
            continue
        try:
            argv=[x.decode('utf-8','replace') for x in (p/'cmdline').read_bytes().split(b'\0') if x]
        except (OSError,PermissionError):
            continue
        if (any(x.endswith('/spore_first_pass_sqlite.py') for x in argv) or
            any(x.endswith('/spore_parallel_sqlite.py') for x in argv) and 'run' in argv):
            ids.append(p.name)
    return ids


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--apply', action='store_true', help='Back up scripts and install patch')
    args=ap.parse_args()
    if not (ROOT/'work/scratch/spore_first_pass_sqlite.py').exists():
        raise SystemExit('Run from ~/spore-decomp with existing SQLite runner installed')
    if not STAGE.exists():
        raise SystemExit(f'Missing extracted patch directory: {STAGE}')
    for rel in FILES:
        src=STAGE/rel
        if not src.is_file():
            raise SystemExit(f'Patch file missing: {src}')
        py_compile.compile(str(src), doraise=True)
    for rel in FILES:
        print(('REPLACE' if (ROOT/rel).exists() else 'CREATE ')+'  '+rel)
    if not args.apply:
        print('Preview only. Run with --apply to install, original files will be backed up.')
        return
    pids=active_workers()
    if pids:
        raise SystemExit('Stop active workers/coordinator first: '+', '.join(pids))
    ts=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/'work/scratch/codex-pass1/sqlite-backups'/('scripts-before-provider-fix-'+ts)
    backup.mkdir(parents=True,exist_ok=False)
    for rel in FILES:
        dest=ROOT/rel
        if dest.exists():
            old=backup/rel
            old.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(dest,old)
    for rel in FILES:
        src=STAGE/rel;dest=ROOT/rel
        dest.parent.mkdir(parents=True,exist_ok=True)
        tmp=dest.with_name(dest.name+'.installing')
        shutil.copy2(src,tmp)
        os.replace(tmp,dest)
    print('Backups:', backup)
    print('Installed. Next run spore_recover_provider_attempts.py preview, then --apply.')


if __name__=='__main__':
    main()
