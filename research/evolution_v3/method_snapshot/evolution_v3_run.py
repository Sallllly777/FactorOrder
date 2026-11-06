"""Independent V3 runs and serial nine-run coordinator. No holdout entry point."""
from evolution_v2_evaluate import (ROOT, CHECKOUT, BASELINE, ANCHOR, read, write, digest,
                                  verify_baseline, seed_rows, Evaluator, canonical, paired_series)
from evolution_v3_rules import dev, promotion, admission, champion, update_pools, select_screen, check_parents
from evolution_v3_generate import generate
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from itertools import combinations
import argparse
import ast
import gc
import json
import os
import random
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import replace
from evolution_v3_runtime import (atomic_write as write, ActiveBudget, SingleInstance,
    awake_seconds, carried_seconds, check_model_ledgers, verified_scripts)

PLAN=ROOT/'docs/factor_evolution_experiment_plan_v3.md'
SEEDS=(17,29,43)


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def script_hashes():
    names=['evolution_preflight.py','evolution_rules.py','evolution_screen.py','evolution_api_probe.py',
           'evolution_v2_rules.py','evolution_v2_model.py','evolution_v2_evaluate.py','evolution_v2_run.py',
           'evolution_v3_report.py','evolution_v3_rules.py','evolution_v3_generate.py',
           'evolution_v3_model.py','evolution_v3_run.py','evolution_v3_review.py',
           'evolution_v3_runtime.py','evolution_v3_resume.py',
           'evolution_v3_independent_a.py','evolution_v3_manual_retry.py']
    return {n:digest(ROOT/'analysis'/n) for n in names}


def verify_protocol(study):
    p=read(study/'protocol.json')
    if digest(PLAN)!=p['plan_sha256']:
        raise RuntimeError('Frozen V3 code/design changed')
    verified_scripts(study,p,script_hashes(),digest)
    for name,sha in p['baseline']['source_code'].items():
        if digest(CHECKOUT/'pkuquant'/name)!=sha: raise RuntimeError('Engine changed')
    if digest(ROOT/'feathers.zip')!=p['baseline']['source_sha256']: raise RuntimeError('Market data changed')
    return p


def initialize(study):
    if study.exists(): raise RuntimeError('Study already exists; never overwrite frozen study')
    cfg,anchor=verify_baseline()
    study.mkdir(parents=True)
    protocol=dict(version='v3',created_at=now(),plan_sha256=digest(PLAN),scripts=script_hashes(),
                  baseline=cfg,anchor=anchor,seeds=seed_rows(cfg,anchor),
                  model=dict(name='gpt-6.1-sol',temperature=.7,max_tokens=4096,api_endpoint='https://YOUR_API_ENDPOINT/v1'),
                  schedule=[dict(arm=a,seed=s) for s in SEEDS for a in ('A','B','C')],
                  budget=dict(generations=6,measurements=144,full_evaluations=18,proposals=216,
                              model_calls=12,model_total_tokens=120000,wall_seconds=21600),
                  holdout_evaluated=False,holdout_stage='separate_post_freeze_stage',
                  authorization='User requested V3 experiment; prior explicit approval for formula/hypothesis/trading summaries and 2021-2025 aggregate metrics to configured API remains in effect.')
    write(study/'protocol.json',protocol)
    shutil.copyfile(PLAN,study/'experiment_plan_v3.md')
    snapshot=study/'source_snapshot';snapshot.mkdir()
    for name in protocol['scripts']: shutil.copyfile(ROOT/'analysis'/name,snapshot/name)
    write(study/'status.json',dict(status='ready',stage='protocol_frozen',created_at=now(),runs=[],
                                  search_complete=False,holdout_evaluated=False,full_plan_complete=False))
    print(json.dumps(dict(study=str(study),status='ready'),ensure_ascii=False))


def run_one(study,arm,seed,resume=False):
    folder=study/f'{arm}{seed}'
    with SingleInstance(folder):
        # The frozen plan isolates deterministic A runs from API/account failures.
        if arm!='A':
            check_model_ledgers(study)
        cp=read(folder/'checkpoint.json') if (folder/'checkpoint.json').exists() else {}
        with ActiveBudget(folder,cp) as budget:
            return _run_one(study,arm,seed,resume,budget)


def _run_one(study,arm,seed,resume,budget):
    protocol=verify_protocol(study)
    folder=study/f'{arm}{seed}';folder.mkdir(exist_ok=True)
    statefile=folder/'checkpoint.json'
    if statefile.exists() and not resume: raise RuntimeError('Run already exists; explicit resume required')
    seeds=protocol['seeds'];anchor=next(r for r in seeds if r['id']=='seed_20')
    state=read(statefile) if statefile.exists() else dict(arm=arm,seed=seed,rows=[],formal_pool=[],diagnostic_pool=[],
        pool_categories={},memory=[],measurement_ids=[],full_ids=[],generation_records=[],proposals=0,
        created_at=now(),started_unix=time.time(),status='running',champion='seed_20')
    if state['status']=='completed' and (folder/'frozen.json').exists(): return
    def save(): write(statefile,state)
    def ping(stage,**details):
        elapsed=budget.elapsed()
        if elapsed>21600: raise TimeoutError('Run wall-clock budget exhausted')
        if shutil.disk_usage(folder).free<5*1024**3: raise RuntimeError('Free disk space below 5 GiB')
        state.update(elapsed_seconds=elapsed,updated_at=now(),status='running')
        state.pop('error',None);state.pop('error_type',None)
        save()
        status=dict(status='running',arm=arm,seed=seed,generation=state.get('generation',0),stage=stage,
                    measurements=len(state['measurement_ids']),full_evaluations=len(state['full_ids']),
                    completed_full=sum('evaluation' in r for r in state['rows']),
                    formal_pool=len(state['formal_pool']),diagnostic_pool=len(state['diagnostic_pool']),
                    champion=state['champion'],elapsed_seconds=elapsed,updated_at=now(),pid=os.getpid(),
                    holdout_evaluated=False,search_run_complete=False,details=details)
        write(folder/'status.json',status)
        print(json.dumps(status,ensure_ascii=False),flush=True)
    try:
        ping('load_development_data');save()
        evaluator=Evaluator(protocol['baseline'],ping)
        evaluator.spec=replace(evaluator.spec,family='evolution-v3',declared_trials=1296)
        for generation in range(1,7):
            if any(r['generation']==generation for r in state['generation_records']): continue
            state['generation']=generation;gen=folder/f'generation_{generation}';gen.mkdir(exist_ok=True)
            if not (gen/'pool_at_start.json').exists():
                write(gen/'pool_at_start.json',dict(formal=state['formal_pool'],diagnostic=state['diagnostic_pool']))
            ping('generate_and_register')
            batch=generate(gen,arm,seed,generation,state,seeds,evaluator)
            phasekey=f'{generation}_registered'
            if phasekey not in state.setdefault('charged_phases',[]):
                state['proposals']+=batch['proposal_count'];state['charged_phases'].append(phasekey);save()
            for proposed in batch['accepted']:
                identity=f'{arm}{seed}_g{generation}_s{proposed["slot"]:03d}'
                row=next((r for r in state['rows'] if r['id']==identity),None)
                if row is None:
                    if len(state['measurement_ids'])>=144: raise RuntimeError('Measurement budget exhausted')
                    row=dict(proposed,id=identity,generation=generation,origin='ordinary_recipe' if arm=='A' else 'model_recipe_local_expansion')
                    state['rows'].append(row);state['measurement_ids'].append(identity);save()
                if 'screen' not in row:
                    row['screen']=evaluator.screen(row,gen/identity);save()
            current=[r for r in state['rows'] if r['generation']==generation]
            if len(current)>24 or state['proposals']>216: raise RuntimeError('Candidate budget exceeded')
            ping('select_full_evaluation')
            selected,selection=select_screen(current,seeds,anchor,evaluator.corr)
            write(gen/'selection.json',selection)
            lookup={r['id']:r for r in [*seeds,*state['rows']]}
            evaluated=[]
            for row in selected:
                if row['id'] not in state['full_ids']:
                    if len(state['full_ids'])>=18: raise RuntimeError('Full evaluation budget exhausted')
                    state['full_ids'].append(row['id']);save()
                if 'evaluation' not in row:
                    row.update(evaluator.full(row,gen/row['id']));save()
            # All same-generation accounts are now available; admission cannot
            # depend on which candidate happened to be evaluated first.
            for row in selected:
                parent=lookup[row['main_parent_id']]
                if 'evaluation' not in parent: parent=anchor
                row['admission']=admission(row,parent,anchor)
                row['decision']=promotion(row['evaluation'],anchor['evaluation'])
                check_parents(row,lookup)
                row['paired']=[]
                for reference in dict((r['id'],r) for r in [parent,anchor,*[lookup[i] for i in row['parent_ids'] if 'evaluation' in lookup[i]]]).values():
                    row['paired'].append(paired_series(row,reference))
                write(gen/row['id']/'decision.json',{k:row[k] for k in ('id','admission','decision','paired')})
                evaluated.append(row);save()
            old=[lookup[i] for i in state['formal_pool']]
            formal,diagnostic,categories=update_pools(old,evaluated,seeds,evaluator.corr)
            state['formal_pool']=[r['id'] for r in formal];state['diagnostic_pool']=[r['id'] for r in diagnostic]
            state['pool_categories']=categories
            oldchamp=state['champion'];best=champion(state['rows'],anchor);state['champion']=best['id']
            for r in evaluated:
                state['memory'].append(dict(id=r['id'],expression=r['expression'],change=r['change'],
                    delta_ic=r['admission'].get('delta_ic'),delta_sharpe=r['admission'].get('delta_sharpe'),
                    failed=r['decision']['failed'],interpretation='Measured association; causal mechanism remains a hypothesis.'))
            record=dict(generation=generation,measurements=len(current),full_evaluations=len(selected),
                        formal_pool=state['formal_pool'],diagnostic_pool=state['diagnostic_pool'],
                        expired_diagnostics=read(gen/'pool_at_start.json')['diagnostic'],
                        champion=best['id'],previous_champion=oldchamp,champion_changed=best['id']!=oldchamp,
                        promoted=[r['id'] for r in evaluated if r['decision']['promoted']],completed_at=now())
            state['generation_records'].append(record);write(gen/'completed.json',record);save()
            ping('generation_complete');refresh(study)
            evaluator.panels.clear();gc.collect()
        state.update(status='completed',completed_at=now());save()
        final=champion(state['rows'],anchor)
        write(folder/'frozen.json',dict(frozen_at=now(),candidate=final,protocol_sha256=digest(study/'protocol.json'),
                                      search_complete=True,holdout_evaluated=False))
        status=read(folder/'status.json');status.update(status='completed',stage='six_generations_complete',
            search_run_complete=True,champion=final['id'],completed_at=now(),generation=6)
        write(folder/'status.json',status);refresh(study)
    except BaseException as error:
        state.update(status='failed',error_type=type(error).__name__,error=str(error));save()
        write(folder/'status.json',dict(status='failed',arm=arm,seed=seed,stage=state.get('generation'),
            error_type=type(error).__name__,error=str(error),pid=os.getpid(),updated_at=now(),
            measurements=len(state['measurement_ids']),full_evaluations=len(state['full_ids']),
            completed_full=sum('evaluation' in r for r in state['rows']),holdout_evaluated=False,search_run_complete=False))
        (folder/'error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        refresh(study);raise


def refresh(study):
    from evolution_v3_report import report
    report(study)


def coordinate(study,resume=False):
    with SingleInstance(study):
        return _coordinate(study,resume)


def _coordinate(study,resume=False):
    protocol=verify_protocol(study)
    check_model_ledgers(study)
    state=read(study/'status.json')
    state.pop('error',None);state.pop('error_type',None)
    state.update(status='running',stage='search',pid=os.getpid(),started_at=now())
    write(study/'status.json',state)
    try:
        for task in protocol['schedule']:
            if task['seed']!=17 and not (study/'stage1_gate.json').exists():
                checks=[]
                for arm in ('A','B','C'):
                    cp=read(study/f'{arm}17/checkpoint.json')
                    checks.append(cp['status']=='completed' and len(cp['generation_records'])==6)
                    ledger=study/f'{arm}17/model/ledger.json'
                    if ledger.exists(): checks.append(all(c['status']=='completed' for c in read(ledger)['calls']))
                if not all(checks):raise RuntimeError('Stage1 engineering gate failed')
                write(study/'stage1_gate.json',dict(passed=True,performance_condition=False,checked_at=now()))
            name=f"{task['arm']}{task['seed']}";folder=study/name;folder.mkdir(exist_ok=True)
            status=read(folder/'status.json') if (folder/'status.json').exists() else {}
            cp=read(folder/'checkpoint.json') if (folder/'checkpoint.json').exists() else {}
            if status.get('status')=='completed':
                if len(cp.get('generation_records',[]))!=6 or not (folder/'frozen.json').exists():
                    raise RuntimeError('Completed status lacks six-generation freeze: '+name)
                continue
            if cp and not resume: raise RuntimeError('Existing incomplete run requires explicit review: '+name)
            remaining=21600-carried_seconds(folder,cp)
            if remaining<=0: raise RuntimeError(name+' active-time budget exhausted; no automatic extension')
            state.update(active_run=name,updated_at=now());write(study/'status.json',state);refresh(study)
            with (folder/'worker.log').open('a',encoding='utf-8') as log:
                args=[sys.executable,str(Path(__file__).resolve()),'--study',str(study),'--arm',task['arm'],'--seed',str(task['seed'])]
                if cp: args.append('--resume')
                active_start=awake_seconds()
                proc=subprocess.Popen(args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                state['child_pid']=proc.pid;write(study/'status.json',state)
                while proc.poll() is None:
                    if awake_seconds()-active_start>remaining:
                        proc.kill();proc.wait();raise TimeoutError(name+' exceeded cumulative six-hour awake-time limit')
                    time.sleep(2)
                code=proc.returncode
            if code!=0: raise RuntimeError(name+' failed; review its status/error; automatic model restart disabled')
            state['runs']=[r for r in state['runs'] if r['run']!=name]+[dict(run=name,status='completed')]
            write(study/'status.json',state)
        state.update(status='completed',stage='nine_searches_frozen',search_complete=True,
                     holdout_evaluated=False,full_plan_complete=False,completed_at=now(),active_run=None)
    except BaseException as error:
        state.update(status='failed',stage='search_stopped',error_type=type(error).__name__,error=str(error),updated_at=now())
        (study/'error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        write(study/'status.json',state);refresh(study)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser()
    parser.add_argument('--study',required=True,type=Path)
    parser.add_argument('--init',action='store_true');parser.add_argument('--worker',action='store_true')
    parser.add_argument('--arm',choices=['A','B','C']);parser.add_argument('--seed',type=int,choices=SEEDS)
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args();study=args.study.resolve()
    if not study.is_relative_to((ROOT/'outputs').resolve()): raise ValueError('Study must stay within project outputs')
    if args.init: initialize(study)
    elif args.worker: coordinate(study,args.resume)
    elif args.arm and args.seed: run_one(study,args.arm,args.seed,args.resume)
    else: parser.error('Choose --init, --worker, or --arm and --seed')
