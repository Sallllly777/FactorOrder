"""V3 diverse selection and two-parent combination checks."""
from evolution_v2_rules import *


def groups(rows,corr):
    result=[];membership={}
    for r in sorted(rows,key=lambda x:x['expression']):
        if not r.get('screen',{}).get('screen_qualified'):continue
        for i,cluster in enumerate(result):
            values=[corr(r,p) for p in cluster]
            if all(v is not None and v>.98 for v in values):
                cluster.append(r);membership[r['id']]=i;break
        else:
            membership[r['id']]=len(result);result.append([r])
    return membership


def select_screen(rows,seeds,anchor,corr):
    membership=groups(rows,corr);chosen=[];decisions=[]
    for slot in ('ic','top20','diversity'):
        used={membership[r['id']] for r in chosen}
        candidates=[r for r in rows if r['id'] in membership and membership[r['id']] not in used]
        if slot!='ic':candidates=[r for r in candidates if r['screen']['periods']['development']['top_excess']['mean']>0]
        ranked=[]
        for r in candidates:
            base=(-lower(r),*complexity(r))
            if slot=='ic':key=base
            elif slot=='top20':key=(-lower(r,'top_excess'),*base)
            else:
                if r['screen']['periods']['development']['rank_ic']['mean']<dev(anchor)['rank_ic']-.01-EPS:continue
                cs=[corr(r,x) for x in [*seeds,*chosen]]
                if any(x is None for x in cs):continue
                key=(max(cs),*base)
            ranked.append((key,r))
        selected=min(ranked,key=lambda x:x[0])[1] if ranked else None
        if selected:chosen.append(selected)
        decisions.append(dict(slot=slot,id=selected['id'] if selected else None,groups=membership))
    return chosen,decisions


def check_parents(row,lookup):
    if row['kind']!='combo':return
    parents=[lookup[x] for x in row['parent_ids']]
    c=dev(row)
    incomplete=any('evaluation' not in p for p in parents)
    dominated=[]
    comparisons=[]
    for p in parents:
        if 'evaluation' not in p:continue
        d=dev(p)
        comparisons.append(dict(parent=p['id'],delta={k:c[k]-d[k] for k in ('rank_ic','sharpe','drawdown','top_excess')}))
        if all(d[k]+EPS>=c[k] for k in ('rank_ic','sharpe')) and any(d[k]>c[k]+EPS for k in ('rank_ic','sharpe')):
            dominated.append(p['id'])
    passed=not incomplete and not dominated
    row['parent_comparison']=dict(comparisons=comparisons,dominated_by=dominated,complete=not incomplete,
        fusion_improved=passed and all(all(c[k]+EPS>=dev(p)[k] for k in ('rank_ic','sharpe')) and
        any(c[k]>dev(p)[k]+EPS for k in ('rank_ic','sharpe')) for p in parents))
    row['decision']['checks']['combo:both_parents']=passed
    if not passed:
        row['decision']['promoted']=False;row['decision']['three_objective_improvement']=False
        row['decision']['failed'].append('combo:both_parents')
        row['admission'].update(formal=False,diagnostic=False)
        row['admission']['failed'].append('combo:both_parents')
