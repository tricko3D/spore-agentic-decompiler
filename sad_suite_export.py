#!/usr/bin/env python3
"""Read-only, no-network exporter for SAD known-solvable ablation results."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

WORK = Path('work/scratch/codex-pass1')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def safe(value, n=1800):
    value = str(value or '')
    value = re.sub(r'(?i)\b(?:sk-or-[\w-]+|sk-[\w-]{16,})\b', '[REDACTED]', value)
    value = re.sub(r'(?i)(\b(?:api[_-]?key|authorization|bearer)\s*[=:]?\s+)\S+', r'\1[REDACTED]', value)
    value = re.sub(r'/home/[^/\s]+', '/home/[USER]', value)
    return value[:n]


def num(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError, OverflowError):
        return 0.0


def duration(a, b):
    try:
        return round((dt.datetime.fromisoformat(b.replace('Z', '+00:00')) -
                      dt.datetime.fromisoformat(a.replace('Z', '+00:00'))).total_seconds(), 2)
    except (TypeError, ValueError, AttributeError):
        return None


def opencode_logs(folder):
    totals = dict(input=0, output=0, cache_read=0, cache_write=0, opencode_cost_usd=0.0)
    types, tool_types = collections.Counter(), collections.Counter()
    step_series, checker_series, file_hashes, errors = [], [], [], []
    bad = 0
    for path in sorted(folder.rglob('*.jsonl')) if folder.is_dir() else []:
        sha, lines = hashlib.sha256(), 0
        with path.open('rb') as fh:
            for line in fh:
                sha.update(line)
                lines += 1
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    bad += 1
                    continue
                if not isinstance(obj, dict):
                    continue
                typ = str(obj.get('type') or 'unknown')
                types[typ] += 1
                part = obj.get('part') or {}
                if not isinstance(part, dict):
                    part = {}
                when = obj.get('timestamp') or obj.get('time') or part.get('timestamp')
                if typ == 'step_finish':
                    tok = part.get('tokens') or {}
                    cache = tok.get('cache') or {}
                    values = {k:int(num(v)) for k,v in (
                        ('input',tok.get('input')),('output',tok.get('output')),
                        ('cache_read',cache.get('read')),('cache_write',cache.get('write')))}
                    for k, v in values.items():
                        totals[k] += v
                    cost = round(num(part.get('cost')), 8)
                    totals['opencode_cost_usd'] += cost
                    step_series.append({'step':len(step_series)+1, **values, 'cost_usd':cost,
                                        **({'timestamp':when} if when else {})})
                elif typ == 'tool_use':
                    state = part.get('state') or {}
                    if not isinstance(state, dict):
                        state = {}
                    inp = state.get('input') or {}
                    if not isinstance(inp, dict):
                        inp = {}
                    tool_types[str(part.get('tool') or part.get('name') or 'unknown')] += 1
                    cmd = str(inp.get('command') or inp.get('cmd') or '')
                    if 'tools/contrib/check.py' in cmd:
                        checker_series.append({'ordinal':len(checker_series)+1,
                                               'state':str(state.get('status') or ''),
                                               **({'timestamp':when} if when else {})})
                elif typ == 'error':
                    error = obj.get('error') or {}
                    errors.append({'type':safe(error.get('type'),90), 'status':error.get('status')}
                                  if isinstance(error,dict) else {'type':type(error).__name__})
        file_hashes.append({'file':path.name,'lines':lines,'sha256':sha.hexdigest()})
    totals['opencode_cost_usd'] = round(totals['opencode_cost_usd'], 8)
    totals['input_plus_output'] = totals['input'] + totals['output']
    totals['all_input_output_cache'] = totals['input_plus_output']+totals['cache_read']+totals['cache_write']
    return {'usage':totals,'model_steps':len(step_series),'steps':step_series,
            'checker_calls_from_logs':len(checker_series),'checker_events':checker_series,
            'event_counts':dict(types),'tool_counts':dict(tool_types),
            'error_events':errors[:30],'log_files':file_hashes,'unparseable_lines':bad}


def brain_queries(path):
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            q = json.loads(line)
        except ValueError:
            continue
        if isinstance(q, dict):
            out.append({'utc':q.get('utc'),'characters':q.get('characters'),
                        'findings':q.get('findings'),'techniques':q.get('techniques'),
                        'question':safe(q.get('question'),1000)})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--suite', type=Path, default=Path.home()/'sad-known-solved-suite-fast')
    p.add_argument('--output', type=Path, default=Path.home()/'sad-brain-full-analysis.json')
    opts = p.parse_args()
    root = opts.suite.expanduser().resolve()
    plan, report = read(root/'suite-plan.json'), read(root/'suite-report.json')
    expected = len(plan['targets'])*4*plan['repeats']
    if len(report['trials']) != expected:
        raise SystemExit(f'Only {len(report["trials"])} of {expected} trial rows; refresh suite compare first')
    export = {
        'export_version':1,
        'interpretation':{'endpoint':'independent MATCH from suite-report.json',
            'token_definition':'OpenCode input+output; cache read/write shown separately',
            'cost_definition':'OpenCode step costs vs shared-key run_budgets cost, which may be contaminated by other work',
            'scope':'all 36 trials incl. failures, no model transcript, prompts, full tool output, source, or credentials'},
        'suite':{k:plan.get(k) for k in ('suite_version','benchmark_protocol','created','targets',
            'repeats','random_seed','model','provider_routing','frozen_knowledge_sha256',
            'historical_failures_before_match','schedule')},
        'trials':[], 'historical_attempts':{}, 'warnings':[]}
    schedules = {}
    per_target = {}
    for va in plan['targets']:
        sub = root/'functions'/va.removeprefix('0x')
        per_target[va] = read(sub/'plan.json')
        h = sub/'archive'/'target-attempts.json'
        if h.is_file():
            export['historical_attempts'][va] = [
                {'pass':x.get('pass_number'),'status':x.get('status'),
                 'utc':x.get('utc'),'note':safe(x.get('note'),700)} for x in read(h)]
    queues = {va:iter(per_target[va]['arms']) for va in plan['targets']}
    for index, va in enumerate(plan['schedule'], 1):
        item = next(queues[va])
        schedules[(va,item['arm'],item['repeat'])] = index
    for row in report['trials']:
        va = row['target']
        item = next((x for x in per_target[va]['arms'] if x['name']==row['name']),None)
        if item is None:
            raise SystemExit('Missing trial plan for '+row['name'])
        trial = root/'functions'/va.removeprefix('0x')/'trials'/row['name']
        logs = opencode_logs(trial/WORK/'logs')
        queries = brain_queries(trial/WORK/'brain-queries.jsonl')
        fields = ('target','arm','repeat','name','state','status','valid','match',
                  'checker_calls','brain_queries','findings_returned','techniques_returned',
                  'treatment_adherent','exposure','input_tokens','output_tokens','cost_usd','diff_bytes')
        val = {k:row.get(k) for k in fields}
        val.update(result_note=safe(row.get('result_note'),2600),
                   seed=item.get('seed'),schedule_position=schedules.get((va,row['arm'],row['repeat'])),
                   started_utc=item.get('started'),finished_utc=item.get('finished'),
                   elapsed_seconds=duration(item.get('started'),item.get('finished')),
                   exit_code=item.get('exit_code'),logs=logs,retrieval_queries=queries)
        export['trials'].append(val)
        if logs['model_steps'] == 0:
            export['warnings'].append('No step_finish events for '+row['name'])
        for k1,k2 in (('input','input_tokens'),('output','output_tokens')):
            if row.get(k2) is not None and logs['usage'][k1] != row[k2]:
                export['warnings'].append(k1+' discrepancy for '+row['name'])
        if row.get('checker_calls') is not None and logs['checker_calls_from_logs'] != row['checker_calls']:
            export['warnings'].append('Checker count discrepancy for '+row['name'])
    if len(export['trials']) != expected:
        raise SystemExit('Export incomplete')
    opts.output.expanduser().parent.mkdir(parents=True, exist_ok=True)
    opts.output.expanduser().write_text(json.dumps(export,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print('Exported',len(export['trials']),'trials to',opts.output.expanduser())
    print('Size:',round(opts.output.expanduser().stat().st_size/1024,1),'KiB')
    print('Warnings:',len(export['warnings']))
    for issue in export['warnings'][:8]:
        print('  ',issue)
    print('Read-only, no network requests. No full transcripts or source files exported.')
    print('Review questions and result notes for sensitive text before sharing.')


if __name__ == '__main__':
    main()