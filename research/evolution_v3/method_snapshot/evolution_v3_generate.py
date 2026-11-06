"""Pre-register a bounded recipe expansion before any generation measurements."""
from evolution_v2_evaluate import read,write,canonical
from evolution_v2_model import validate,feedback_row
from evolution_v2_rules import dev
from pathlib import Path
from itertools import combinations
import ast
import json
import random

GRID=[2,3,5,7,10,15,20,30,40,60,90,120]
FAMILIES=['overnight_reversal','momentum','range','amount','trade_count']
SYSTEM='''Propose causal equity factor recipes. Return JSON {"recipes":[{"slot":1,"template":"-rank(ts_sum(log(close/open), {w}))","values":[5,10,20],"hypothesis":"...","change":"..."}]}.
At most one recipe per assigned slot. Use exactly one {w} placeholder for one literal window parameter, or no placeholder and values:[] for a fixed recipe. Values must come from supplied grid; respect each slot limit. No code or other placeholders. Local recipes change ONE principal structure of assigned parent. Exploration must match assigned mechanism.
Fields: open,high,low,close,amt,num_trades,returns. Functions: rank,zscore,ts_delay,ts_delta,ts_return,ts_mean,ts_sum,ts_std,ts_min,ts_max,decay_linear,clip,log,abs,sign. No volume,vwap,attributes,indexing or imports. Windows2..120,delays1..120; at most80AST nodes/1500chars. Higher scores buy; close signal executes next open. Fixed20positions,60%exposure,daily trading,costs unchanged. Objectives:signed1day next-open RankIC and net Sharpe. No fabricated performance. Empty recipes allowed. Hypothesis/change <=180 characters each. No cross-sectional factor combinations in recipes; host generates equal-rank combinations separately.'''


def compact(row,full=False):
    out={'id':row['id']}
    if 'evaluation' in row and not full:
        p=dev(row)
        out['signal']=[round(p[k],6) for k in ('rank_ic','top_excess')]
        out['net']=[round(p[k],5) for k in ('sharpe','drawdown')]
        out['failed']=row.get('decision',{}).get('failed',[])
    elif 'evaluation' in row:
        out['p']={n:[round(p[k],6) for k in ('rank_ic','sharpe','drawdown','top_excess')]
                  for n,p in row['evaluation']['periods'].items() if n in ('development','2023','2024','2025')}
        out['failed']=row.get('decision',{}).get('failed',[])
        out['stress']={n:round(p['sharpe'],5) if p.get('sharpe') is not None else None
                       for n,p in row['evaluation'].get('stress',{}).items() if n in ('cost_x2','delay_plus_1')}
        out['cost']=row['evaluation'].get('account_diagnostics',{}).get('development',{})
    else:
        p=row.get('screen',{}).get('periods',{}).get('development',{})
        out['signal']=[p.get(k,{}).get('mean') for k in ('rank_ic','top_excess')]
        out['status']=row.get('screen',{}).get('status')
    return out


def payload(arm,seed,generation,phase,slots,history,feedback=None):
    if arm=='B' and feedback is not None:raise ValueError('B feedback forbidden')
    data=dict(arm=arm,seed=seed,generation=generation,phase=phase,grid=GRID,slots=slots)
    if feedback is not None:data['feedback']=feedback
    content=json.dumps(data,ensure_ascii=False,separators=(',',':'))
    # UTF-8 bytes + framing is a conservative byte-token bound, not a claim
    # that a provider-specific tokenizer is installed.
    estimate=len((SYSTEM+content).encode('utf-8'))+128
    # 6000 is the design target, not permission to drop mandatory parent or
    # previous-generation failures. The ledger records overshoots; the hard
    # 120000 run budget still applies before every request.
    return dict(model='gpt-6.1-sol',messages=[dict(role='system',content=SYSTEM),dict(role='user',content=content)],
                max_tokens=4096,temperature=.7)


def recipes(arm,seed,generation,state,seeds):
    rng=random.Random(seed*100+generation)
    lookup={r['id']:r for r in [*seeds,*state['rows']]}
    if arm=='C':
        parents=[lookup[x] for x in state['diagnostic_pool'][:3]]
        bank=[*seeds,*[lookup[x] for x in state['formal_pool']]]
    else:parents=[];bank=[*seeds,*state['rows']]
    rng.shuffle(bank)
    for p in bank:
        if len(parents)==4:break
        if sum(x['id']==p['id'] for x in parents)<2:parents.append(p)
    slots=[dict(slot=i+1,kind='local',parent=p['id'],expression=p['expression'],
                limit=1 if arm=='C' and p['id'] in state['diagnostic_pool'] else 3)
           for i,p in enumerate(parents)]
    families=FAMILIES[generation%5:]+FAMILIES[:generation%5]
    for i,family in enumerate(families[:4]):
        p=seeds[(i+generation)%4]
        slots.append(dict(slot=i+5,kind='explore',parent=p['id'],mechanism=family,limit=2))
    return slots


def ordinary(slots,seed,generation):
    rng=random.Random(seed*100+generation);out=[]
    for s in slots:
        values=rng.sample(GRID,s['limit'])
        if s['kind']=='local':
            parent=s['expression'];action=(generation+s['slot'])%3
            if action==0:
                tree=ast.parse(parent,mode='eval')
                calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                       and n.func.id.startswith('ts_') and n.func.id!='ts_delay']
                if calls:
                    rng.choice(calls).args[-1]=ast.Name(id='WINDOW',ctx=ast.Load())
                    template=ast.unparse(tree).replace('WINDOW','{w}')
                else:template=f'ts_mean({parent}, {{w}})'
            elif action==1:template=f'ts_mean({parent}, {{w}})'
            else:template=f'({parent}) / (ts_std(returns, {{w}}) + 0.001)'
        else:
            template={'overnight_reversal':'-rank(ts_sum(log(open / ts_delay(close, 1)), {w}))',
                'momentum':'rank(ts_delay(close, 2) / ts_delay(close, {w}) - 1)',
                'range':'-rank(ts_mean((high-low)/close, {w}))',
                'amount':'rank(amt / (ts_mean(amt, {w}) + 0.001))',
                'trade_count':'-rank(log(close/open) * clip(num_trades/(ts_mean(num_trades, {w})+1),0.5,2))'}[s['mechanism']]
        out.append(dict(slot=s['slot'],template=template,values=values,hypothesis='Test the assigned structural edit using measured feedback only after evaluation.',change='One fixed structure, one window axis.'))
    return json.dumps(dict(recipes=out))


def expand(content,slots,lookup,seen):
    if '</think>' in content:content=content.rsplit('</think>',1)[1]
    content=content.strip()
    if content.startswith('```'):content='\n'.join(content.splitlines()[1:-1])
    try:
        items=json.loads(content)['recipes']
        if not isinstance(items,list) or len(items)>len(slots):raise ValueError('recipe_count')
    except (ValueError,KeyError,TypeError):return [],[{'slot':s['slot'],'reason':'invalid_json'} for s in slots],sum(s['limit'] for s in slots)
    available={s['slot']:s for s in slots};accepted=[];errors=[];charged=0;used=set();seen=set(seen)
    for item in items:
        slot=item.get('slot') if isinstance(item,dict) else None
        if type(slot)!=int or slot not in available or slot in used:
            errors.append(dict(slot=slot,reason='invalid_slot'));charged+=1;continue
        used.add(slot);s=available[slot]
        try:
            t=item['template'];values=item['values']
            if not isinstance(t,str) or len(t)>1500 or not isinstance(values,list):raise ValueError('recipe_schema')
            if not all(isinstance(item.get(k),str) and 0<len(item[k])<=180 for k in ('hypothesis','change')):raise ValueError('explanation_length')
            if '{w}' in t:
                if t.count('{w}')!=1 or not 1<=len(values)<=s['limit'] or len(set(values))!=len(values):raise ValueError('parameter_axis')
                if not all(type(v)==int and v in GRID for v in values):raise ValueError('window_grid')
                expressions=[t.replace('{w}',str(v)) for v in values]
            else:
                if values:raise ValueError('fixed_recipe_has_values')
                expressions=[t]
            charged+=len(expressions)
            built=[]
            for i,expr in enumerate(expressions):
                n=validate(expr);key=canonical(expr)
                if key in seen:raise ValueError('duplicate_expression')
                p=lookup[s['parent']]
                built.append(dict(slot=(s['slot']-1)*3+i+1,recipe_slot=slot,expression=expr,nodes=n,
                    kind=s['kind'],mechanism=s.get('mechanism','local'),main_parent_id=p['id'],parent_ids=[p['id']],
                    fusion_depth=p.get('fusion_depth',0),hypothesis=item['hypothesis'],change=item['change']))
            accepted.extend(built);seen.update(canonical(r['expression']) for r in built)
        except (ValueError,KeyError,TypeError,SyntaxError) as error:
            if 'expressions' not in locals():charged+=s['limit']
            errors.append(dict(slot=slot,reason=str(error)))
        finally:
            if 'expressions' in locals():del expressions
    return accepted,errors,charged


def generate(folder,arm,seed,generation,state,seeds,evaluator):
    from evolution_v3_runtime import atomic_write as write
    from evolution_v3_model import call_model
    file=folder/'registered_generation.json'
    if file.exists():return read(file)
    lookup={r['id']:r for r in [*seeds,*state['rows']]}
    slots=recipes(arm,seed,generation,state,seeds)
    seen={canonical(r['expression']) for r in lookup.values()}
    feedback=None
    if arm=='C':
        parentids=list(dict.fromkeys(s['parent'] for s in slots))
        feedback=dict(columns=['IC','netSharpe','drawdown','Top20'],
            parents=[compact(lookup[x],True) for x in parentids],
            previous=[compact(r) for r in state['rows'] if r['generation']==generation-1])
        # Parent records are represented once; all previous failures remain.
        feedback['previous']=[r for r in feedback['previous'] if r['id'] not in parentids]
    write(folder/'recipe_slots.json',slots)
    content=ordinary(slots,seed,generation) if arm=='A' else call_model(folder.parent/'model',arm,seed,generation,'directions',slots,[],feedback)
    rows,errors,count=expand(content,slots,lookup,seen)
    # One repair, before any measured feedback. No hidden parameter sweeps.
    if errors and arm!='A':
        valid={r['recipe_slot'] for r in rows}
        bad={e['slot'] for e in errors if type(e['slot'])==int}-valid
        repair=[];remaining=32-count
        for s in slots:
            if s['slot'] in bad and s['limit']<=remaining:repair.append(s);remaining-=s['limit']
        ledger_path=folder.parent/'model/ledger.json'
        ledger=read(ledger_path) if ledger_path.exists() else {'calls':[]}
        remaining_calls=len(ledger['calls'])<12 and sum(c['generation']==generation for c in ledger['calls'])<2
        if repair and remaining_calls:
            second=call_model(folder.parent/'model',arm,seed,generation,'repair',repair,[],feedback)
            repaired,errs,n=expand(second,repair,lookup,seen|{canonical(r['expression']) for r in rows})
            rows+=repaired;count+=n;errors+=errs
        elif repair:
            errors.append(dict(slot=None,reason='format_repair_skipped_request_budget_exhausted'))
    if arm=='C':
        pool=[r for r in [*seeds,*[lookup[i] for i in state['formal_pool']]] if not r.get('fusion_depth',0) and dev(r)['top_excess']>0]
        pairs=[]
        for a,b in combinations(pool,2):
            c=evaluator.corr(a,b)
            if c is not None:pairs.append((c,a['id'],b['id'],a,b))
        pairs=[(x[-2],x[-1]) for x in sorted(pairs,key=lambda x:x[:3])]
    else:
        pairs=list(combinations(seeds,2));random.Random(seed*100+generation).shuffle(pairs)
    combos=[]
    for a,b in pairs:
        expr=f"(rank({a['expression']}) + rank({b['expression']})) / 2"
        if canonical(expr) in seen:continue
        try:n=validate(expr)
        except ValueError:continue
        combos.append(dict(slot=101+len(combos),expression=expr,nodes=n,kind='combo',fusion_depth=1,
            mechanism='equal_rank',main_parent_id=a['id'],parent_ids=[a['id'],b['id']],
            hypothesis='Test equal-rank fusion against BOTH components.',change='Equal-rank fusion.'))
        seen.add(canonical(expr))
        if len(combos)==4:break
    rows+=combos;count+=len(combos)
    if count>36 or len(rows)>24:raise RuntimeError('V3 generation proposal limit exceeded')
    result=dict(accepted=rows,errors=errors,proposal_count=count,registered_before_measurement=True,
                performance_feedback=arm=='C',generation=generation)
    write(file,result);return result
