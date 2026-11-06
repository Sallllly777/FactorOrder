"""V2 development-only evaluations and auditable paired comparisons."""
from evolution_preflight import ROOT, CHECKOUT, Research, MarketData, Settings, EvaluationSpec, write, digest
from evolution_rules import period_records
from evolution_screen import summarize
from pkuquant.evaluation import signal_diagnostics
from pkuquant.engine import simulate
from pkuquant.report import metrics
from pkuquant.factors import Fields, Expression, decay_linear
from pkuquant.data import FIELDS
from dataclasses import replace
from pathlib import Path
from collections import OrderedDict
import ast
import gc
import io
import json
import math
import zipfile
import numpy as np
import pandas as pd

BASELINE = ROOT / 'outputs/evolution_v1_preflight_20261104_01'
ANCHOR = ROOT / 'outputs/evolution_v1_A17_full1_20261104_01/anchor.json'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def canonical(expression):
    return ast.dump(Expression(expression).tree, include_attributes=False)


def verify_baseline():
    cfg = read(BASELINE/'protocol.json')
    if read(BASELINE/'status.json')['status'] != 'completed':
        raise RuntimeError('Baseline incomplete')
    if digest(ROOT/'feathers.zip') != cfg['source_sha256']:
        raise RuntimeError('Source market data changed')
    for name, sha in cfg['source_code'].items():
        if digest(CHECKOUT/'pkuquant'/name) != sha:
            raise RuntimeError('Backtest engine changed: '+name)
    for name in ('20', '10', '5', 'baseline'):
        folder = BASELINE / ('seed_'+name)
        sums = read(folder/'checksums.json')
        for f in ('evaluation.json','evaluation_daily.csv','evaluation_period_returns.csv','daily.csv'):
            if digest(folder/f) != sums[f]:
                raise RuntimeError('Baseline artifact changed: '+name+'/'+f)
    anchor = read(ANCHOR)
    if anchor['periods'] != period_records(read(BASELINE/'seed_20/evaluation.json')):
        raise RuntimeError('Anchor period mismatch')
    if read(ANCHOR.parent/'status.json')['status'] != 'completed':
        raise RuntimeError('Anchor stress pack incomplete')
    for variant in ('cost_x2','delay_plus_1','capital_x2','smoothing_2d'):
        folder = ANCHOR.parent/'anchor_stress'/variant
        for name, sha in read(folder/'checksums.json').items():
            if digest(folder/name) != sha:
                raise RuntimeError('Anchor stress checksum mismatch')
    return cfg, anchor


def load_development(source_hash):
    # Decode source tables locally; filter dates BEFORE MarketData validation,
    # quality checks, universe construction, feature or label calculations.
    # No 2026 performance or feature data are computed, saved or exposed.
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.feather as feather
    frames = {}
    with zipfile.ZipFile(ROOT/'feathers.zip') as archive:
        for name in FIELDS:
            table = feather.read_table(io.BytesIO(archive.read('feathers/'+name+'.feather')))
            dates = table.column('date')
            cutoff = pa.scalar(pd.Timestamp('2025-12-31').to_pydatetime(), type=dates.type)
            table = table.filter(pc.less_equal(dates, cutoff))
            frames[name] = table.to_pandas().set_index('date')
            del table
        metadata = json.loads(archive.read('feathers/metadata.json')) if 'feathers/metadata.json' in archive.namelist() else {}
    data = MarketData(frames, source=str(ROOT/'feathers.zip'), source_hash=source_hash, metadata=metadata)
    assert data.dates.max() <= pd.Timestamp('2025-12-31')
    return data


def seed_rows(cfg, anchor):
    rows = []
    for key in ('baseline','5','10','20'):
        path = BASELINE/('seed_'+key)
        expr = cfg['expressions'][key]
        summary=read(path/'evaluation.json')
        evaluation = {'periods': period_records(summary)}
        if key == '20': evaluation = dict(anchor)
        evaluation['account_diagnostics']=account_diagnostics(summary)
        rows.append(dict(id='seed_'+key, expression=expr, kind='seed', generation=0,
                         nodes=len(list(ast.walk(Expression(expr).tree))), evaluation=evaluation,
                         account_path=str(path), main_parent_id=None, parent_ids=[]))
    return rows


def account_diagnostics(summary):
    keys=('annual_return','total_explicit_cost','annual_turnover_two_sided','order_fill_rate')
    return {('development' if p['name']=='validation' else p['name']):
            dict({k:p['portfolio']['candidate'].get(k) for k in keys},tail_5pct=p['portfolio'].get('candidate_tail'))
            for p in summary['stability']['periods']}


class Evaluator:
    def __init__(self, cfg, ping):
        self.cfg, self.ping = cfg, ping
        self.settings = Settings(**cfg['settings'])
        self.spec = replace(EvaluationSpec(**cfg['evaluation']), family='evolution-v2', declared_trials=432)
        self.lab = Research(load_development(cfg['source_sha256']))
        self.data, self.eligible, _, _ = self.lab._prepare(self.settings)
        self.panels, self.correlations = OrderedDict(), {}

    def panel(self, row):
        key = canonical(row['expression'])
        if key not in self.panels:
            self.panels[key] = self.lab.factor(row['expression'], self.settings).loc['2023-01-01':'2025-12-31']
            while len(self.panels) > 3: self.panels.popitem(last=False)
        self.panels.move_to_end(key)
        return self.panels[key]

    def corr(self, left, right):
        key = tuple(sorted((canonical(left['expression']),canonical(right['expression']))))
        if key[0] == key[1]: return 1.
        if key not in self.correlations:
            self.ping('signal_correlation', pair=[left['id'],right['id']])
            a, b = self.panel(left), self.panel(right)
            common = a.notna() & b.notna()
            series = a.where(common).rank(axis=1).corrwith(b.where(common).rank(axis=1),axis=1).where(common.sum(axis=1)>=20)
            self.correlations[key] = float(series.abs().mean()) if series.notna().sum()>=60 else None
        return self.correlations[key]

    def screen(self, row, folder):
        folder = Path(folder); folder.mkdir(parents=True,exist_ok=True)
        resultfile=folder/'screen.json'
        if resultfile.exists(): return read(resultfile)
        self.ping('screening', candidate=row['id'])
        try:
            score=self.lab.factor(row['expression'],self.settings)
            prefix=self.lab.factor(row['expression'],replace(self.settings,end='2023-06-30'))
            pd.testing.assert_frame_equal(score.loc[:'2023-06-30'],prefix)
            if not score.std(axis=1).gt(0).any(): raise ValueError('Constant signal')
            daily,quantiles,_,_=signal_diagnostics(score,self.data,self.eligible,self.settings,
                EvaluationSpec(horizons=(1,),bootstrap_samples=0),'2021-01-01','2025-12-31')
            daily.to_csv(folder/'signal_daily.csv',index=False)
            quantiles.to_csv(folder/'signal_quantiles.csv',index=False)
            periods={name:summarize(daily,start,end) for name,start,end in [
                ('reference','2021-01-01','2022-12-31'),('development','2023-01-01','2025-12-31'),
                *[(y,y+'-01-01',y+'-12-31') for y in ('2023','2024','2025')]]}
            qualified=all(periods[p]['adequate'] and periods[p]['rank_ic']['mean']>0
                          for p in ('development','2023','2024','2025'))
            result=dict(status='measured',prefix_causality='passed',periods=periods,screen_qualified=qualified)
        except (ValueError,TypeError,AssertionError,KeyError) as error:
            result=dict(status='invalid',error_type=type(error).__name__,error=str(error),screen_qualified=False)
        write(resultfile,result); gc.collect()
        return result

    def full(self, row, folder):
        folder=Path(folder); resultfile=folder/'full.json'
        if resultfile.exists(): return read(resultfile)
        pointer=folder/'account_pointer.json'
        if pointer.exists():
            account=Path(read(pointer)['path'])
        else:
            self.ping('full_account',candidate=row['id'])
            n=1
            while (folder/f'account_attempt_{n}').exists() or (folder/f'.account_attempt_{n}.lock').exists(): n+=1
            account=folder/f'account_attempt_{n}'
            result=self.lab.run(row['expression'],self.settings,evaluation=self.spec)
            result.save(account); del result
            write(pointer,dict(path=str(account),attempt=n))
        summary=read(account/'evaluation.json')
        evaluation={'periods':period_records(summary),'account_diagnostics':account_diagnostics(summary)}
        evaluation['stress']=self.stress(row,folder/'stress')
        # Development-account costs and tails, never mixed with 2021-22 returns.
        returns=pd.read_csv(account/'evaluation_period_returns.csv')
        x=returns.loc[returns.period.eq('validation'),'candidate_return']
        evaluation['tail_5pct']=float(x.nsmallest(max(1,math.ceil(len(x)*.05))).mean())
        result=dict(evaluation=evaluation,account_path=str(account))
        write(resultfile,result);gc.collect()
        return result

    def stress(self, row, folder):
        folder.mkdir(exist_ok=True)
        result=read(folder/'summary.json') if (folder/'summary.json').exists() else {}
        if len(result)==4: return result
        base=replace(self.settings,start='2023-01-01',end='2025-12-31')
        data,eligible,adv,_=self.lab._prepare(base)
        status=self.lab.security.matrix(data.dates,data.codes,at='09:25:00')
        panel=Expression(row['expression']).evaluate(Fields(data,eligible))[0]
        signal=decay_linear(panel,base.decay).values.where(eligible)
        variants=[('cost_x2',replace(base,commission=base.commission*2,min_commission=base.min_commission*2,
                    transfer_rate=base.transfer_rate*2,slippage_bps=base.slippage_bps*2),signal),
                  ('delay_plus_1',replace(base,delay=base.delay+1),signal),
                  ('capital_x2',replace(base,initial_cash=base.initial_cash*2),signal),
                  ('smoothing_2d',base,signal.rolling(2,min_periods=2).mean())]
        for name,opts,values in variants:
            if name in result: continue
            self.ping('stress_'+name,candidate=row['id'])
            values=values.copy();values.loc[values.index<pd.Timestamp(opts.start)]=np.nan
            sim=simulate(data,values,eligible,adv,opts,self.lab.states,status)
            measured=metrics(sim,opts)
            n=1
            while (folder/f'{name}_{n}').exists(): n+=1
            dest=folder/f'{name}_{n}';dest.mkdir()
            for frame in ('daily','orders','holdings','targets'):
                getattr(sim,frame).to_csv(dest/(frame+'.csv'),index=frame=='daily')
            write(dest/'manifest.json',dict(expression=row['expression'],settings=opts.to_dict(),metrics=measured,
                                           holdout_evaluated=False))
            write(dest/'checksums.json',{p.name:digest(p) for p in dest.iterdir() if p.is_file()})
            result[name]=dict(period='2023-01-01/2025-12-31',sharpe=measured['sharpe_zero_rf'],
                              drawdown=-measured['max_drawdown'],metrics=measured,path=str(dest))
            write(folder/'summary.json',result)
            del sim;gc.collect()
        return result


def paired_series(candidate, parent):
    def source(row):
        p=Path(row['account_path'])
        sig=pd.read_csv(p/'evaluation_daily.csv')
        sig=sig[sig.horizon.eq(1)&sig.date.between('2023-01-01','2025-12-31')].set_index('date')
        valid=sig.exit_date.notna() & sig.exit_date.le('2025-12-31')
        sig.loc[~valid,['rank_ic','top_excess']]=np.nan
        ret=pd.read_csv(p/'evaluation_period_returns.csv')
        ret=ret[ret.period.eq('validation')].set_index('date').candidate_return
        return sig,ret
    a,ar=source(candidate);b,br=source(parent)
    dates=ar.index.union(br.index).sort_values()
    if not ar.index.equals(br.index) or ar.isna().any() or br.isna().any():
        raise ValueError('Development return calendars differ or contain missing values')
    rng=np.random.default_rng(20261104);n=len(dates);block=20
    # Non-circular moving blocks preserve the original chronological axis.
    starts=rng.integers(0,max(1,n-block+1),size=(2000,math.ceil(n/block)))
    indices=(starts[:,:,None]+np.arange(min(block,n))).reshape(2000,-1)[:,:n]
    out={}
    for metric in ('rank_ic','top_excess','sharpe'):
        x=(ar if metric=='sharpe' else a[metric]).reindex(dates).to_numpy(float)
        y=(br if metric=='sharpe' else b[metric]).reindex(dates).to_numpy(float)
        joint=np.isfinite(x)&np.isfinite(y);x=np.where(joint,x,np.nan);y=np.where(joint,y,np.nan)
        if joint.sum()<60:
            out[metric]={'evidence':'insufficient','valid_dates':int(joint.sum())};continue
        if metric=='sharpe':
            xs,ys=x[indices],y[indices]
            sx,sy=np.nanstd(xs,axis=1,ddof=1),np.nanstd(ys,axis=1,ddof=1)
            estimates=np.divide(np.nanmean(xs,axis=1),sx,out=np.full(2000,np.nan),where=sx>0)*np.sqrt(252)
            estimates-=np.divide(np.nanmean(ys,axis=1),sy,out=np.full(2000,np.nan),where=sy>0)*np.sqrt(252)
            observed=float((np.nanmean(x)/np.nanstd(x,ddof=1)-np.nanmean(y)/np.nanstd(y,ddof=1))*np.sqrt(252))
        else:
            estimates=np.nanmean((x-y)[indices],axis=1);observed=float(np.nanmean(x-y))
        samples=estimates[np.isfinite(estimates)]
        lo,hi=map(float,np.quantile(samples,[.025,.975])) if len(samples)>=1900 else (None,None)
        out[metric]=dict(delta=observed,low=lo,high=hi,valid_dates=int(joint.sum()),valid_samples=len(samples),
                         evidence='insufficient' if lo is None else 'strong_positive' if lo>0 else 'positive_uncertain' if observed>0 else 'not_improved')
    return dict(compared_to=parent['id'],block=20,samples=2000,seed=20261104,metrics=out,
                multiple_testing_corrected=False)
