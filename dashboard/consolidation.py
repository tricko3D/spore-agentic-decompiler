"""Strictly scoped service-control boundary for the knowledge consolidator.

The dashboard never executes arbitrary commands or runs paid requests itself.
The existing systemd user unit owns the process; the configuration is read by
the existing consolidator's serve loop between batches.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

UNIT = "sad-technique-consolidator.service"
MODULE = "spore_technique_consolidator.py"
DEFAULTS = {"daily_usd":0.25,"batch":3,"interval":180}


def settings_path(repo:Path):
    return repo / "work/scratch/codex-pass1/memory/consolidator-settings.json"


def _valid(data):
    if not isinstance(data,dict):raise ValueError("Invalid consolidation settings")
    usd=data.get("daily_usd")
    batch=data.get("batch")
    interval=data.get("interval")
    if type(usd) not in (int,float) or not 0.01<=float(usd)<=10:
        raise ValueError("Daily USD budget must be from 0.01 to 10")
    if type(batch) is not int or not 1<=batch<=20:
        raise ValueError("Batch must contain 1 to 20 findings")
    if type(interval) is not int or not 30<=interval<=7200:
        raise ValueError("Interval must be 30 to 7200 seconds")
    return {"daily_usd":float(usd),"batch":batch,"interval":interval}


def settings(repo:Path):
    p=settings_path(repo)
    if not p.is_file():return dict(DEFAULTS)
    try:
        value=json.loads(p.read_text(encoding="utf-8"))
        return _valid(value)
    except (OSError,ValueError,TypeError):
        # Surface invalid files clearly; never silently increase the budget.
        raise ValueError("Consolidation settings file invalid: "+str(p))


def save_settings(repo:Path,data):
    value=_valid(data)
    p=settings_path(repo)
    if not p.parent.is_dir():
        raise ValueError("Collective Memory must be installed first")
    fd,name=tempfile.mkstemp(prefix=".consolidation.",dir=p.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as file:
            os.fchmod(file.fileno(),0o600)
            json.dump(value,file,sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name,p)
        return value
    finally:
        if os.path.exists(name):os.unlink(name)


def _systemctl(*arguments):
    try:
        result=subprocess.run(["systemctl","--user",*arguments],
                              capture_output=True,text=True,timeout=5,check=False)
        return result.returncode,result.stdout.strip()
    except (FileNotFoundError,subprocess.TimeoutExpired,OSError):
        return 127,""


def state(repo:Path):
    unit=Path.home()/".config/systemd/user"/UNIT
    rc_active, active=_systemctl("is-active",UNIT)
    rc_enabled, enabled=_systemctl("is-enabled",UNIT)
    installed=unit.is_file()
    result={"installed":installed,"mode":"systemd-user" if installed else "unconfigured",
            "active":rc_active==0 and active=="active",
            "enabled":rc_enabled==0 and enabled=="enabled",
            "unit":UNIT,"settings":settings(repo),
            "controls_available":installed}
    if not installed:
        # A manually started or nohup process is not adopted or killed from
        # the browser. That must be managed through its owner shell.
        result["hint"]="Enable the existing systemd service once from the terminal"
    return result


def action(repo:Path,kind):
    if kind not in ("start","stop"):
        raise ValueError("Invalid consolidation service action")
    unit=Path.home()/".config/systemd/user"/UNIT
    if not unit.is_file():
        raise ValueError("The consolidation systemd user unit is not installed")
    # Explicit allowlist, no shell, no path or arguments controlled by browser.
    command="start" if kind=="start" else "stop"
    rc,_=_systemctl(command,UNIT)
    if rc:
        raise RuntimeError("Could not "+kind+" the consolidation systemd service")
    return state(repo)
