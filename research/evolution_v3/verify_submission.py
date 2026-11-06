"""Verify submitted development evidence without data files, network, or API calls."""
from pathlib import Path
import ast
import copy
import hashlib
import json
import math
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'rules'))
from evolution_v2_rules import promotion,admission,champion
from evolution_v3_rules import check_parents


def require(value,message):
    if not value:raise ValueError(message)


def verify(directory=HERE):
    directory=Path(directory)
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    for name,expected in manifest['files'].items():
        file=(directory/name).resolve()
        require(file.is_relative_to(directory.resolve()),'Unsafe manifest path')
        require(hashlib.sha256(file.read_bytes()).hexdigest()==expected,'Checksum mismatch: '+name)
    evidence=json.loads((directory/'data/evidence.json').read_text(encoding='utf-8'))
    scope=evidence['scope'];runs=evidence['runs'];rows=evidence['candidates'];seeds=evidence['seeds']
    completed=scope['completed_runs'];lookup={r['id']:r for r in [*seeds,*rows]};anchor=lookup['seed_20']
    require(completed==['A17','B17','C17','A29','B29'],'Completed scope changed')
    require([r['run'] for r in runs if r['status']=='completed']==completed,'Completed status and report scope differ')
    require(len(lookup)==len(rows)+len(seeds),'Duplicate candidate identifier')
    require(scope['original_planned_runs']==9 and len(runs)==9,'Nine planned runs must remain disclosed')
    require(not scope['search_complete'] and not scope['historical_review_started'],'Do not claim full completion')
    require(sum(r['run'] in completed for r in rows)==450,'Expected 450 main-report candidates')
    require(sum(r['run']=='C29' for r in rows)==23,'Expected 23 disclosed partial candidates')
    full=[r for r in rows if 'evaluation' in r]
    require(len(full)==70,'Expected 70 complete evaluation packs')
    require(sum(r['model_calls'] for r in runs if r['run'] in completed)==34,'Completed API call count')
    require(sum(r['reported_tokens'] for r in runs if r['run'] in completed)==74223,'Completed confirmed token count')
    require(sum(r['unknown_reserved_tokens'] for r in runs)==16619,'Unknown usage reservation was dropped')
    require(sum(r['reported_tokens'] for r in runs)==76764,'All confirmed token count')
    for run in runs:
        own=[r for r in rows if r['run']==run['run']]
        require(len(own)==run['measured'],'Candidate count mismatch: '+run['run'])
        require(sum('evaluation' in r for r in own)==run['full_evaluations'],'Full count mismatch')
        require(run['measured']<=144 and run['full_evaluations']<=18,'Run measurement budget')
        require(run['model_calls']<=12 and run['reported_tokens']+run['unknown_reserved_tokens']<=120000,'Model budget')
        require(run['screened']==sum('screen' in r for r in own),'Screen count mismatch')
        if run['run'] in completed:
            require(run['status']=='completed' and run['completed_generations']==6,'Incomplete run in main comparison')
            require(len([g for g in evidence['generations'] if g['run']==run['run']])==6,'Generation count')
            require(champion([r for r in own if r.get('decision')],anchor)['id']==run['champion'],'Frozen program champion mismatch')
        for generation in range(1,7):
            batch=[r for r in own if r['generation']==generation]
            require(len(batch)<=24 and sum('evaluation' in r for r in batch)<=3,'Generation budget')
        for row in own:
            ast.parse(row['expression'],mode='eval')
            require(all(p in lookup for p in row['parent_ids']),'Missing parent')
            require(all(p.startswith('seed_') or lookup[p]['run']==run['run'] for p in row['parent_ids']),'Cross-run parent')
    for row in full:
        fresh=copy.deepcopy(row)
        parent=lookup[row['main_parent_id']]
        if 'evaluation' not in parent:parent=anchor
        fresh['admission']=admission(fresh,parent,anchor)
        fresh['decision']=promotion(fresh['evaluation'],anchor['evaluation'])
        check_parents(fresh,lookup)
        require(fresh['decision']==row['decision'],'Promotion replay mismatch: '+row['id'])
        for period,metric in row['evaluation']['periods'].items():
            require(period in ('train','development','2023','2024','2025'),'Unexpected evaluation period')
            require(all(math.isfinite(metric[k]) for k in ('rank_ic','sharpe','drawdown','top_excess')),'Non-finite primary metric')
    promoted=[r['id'] for r in full if r['decision']['promoted']]
    require(promoted==['A29_g2_s010'],'Program promotion count')
    flagged=[r['id'] for r in full if r.get('fusion_depth')==1 and r['kind']!='combo']
    require(flagged==evidence['audit']['affected_full_evaluations'],'Combination audit changed')
    winner=lookup['A29_g2_s010']
    require(winner['kind']=='local' and winner['parent_comparison'] is None,'Provisional label no longer matches evidence')
    require(not evidence['audit']['independently_validated_promotions'],'Do not relabel provisional findings')
    return dict(status='passed',completed_searches=5,main_candidates=450,partial_candidates=23,
                replayed_full_decisions=70,program_promotions=1,combination_descendants_flagged=len(flagged),
                independent_validation='not_completed',historical_review='not_started')


if __name__=='__main__':
    print(json.dumps(verify(),ensure_ascii=False,indent=2))
