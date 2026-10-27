"""Versioned, ex-post factor evaluation. Labels never enter Fields or simulate.

Scores are transparent research heuristics, not probabilities or validation.
Missing evidence is never reweighted into a higher total score.
"""
from dataclasses import dataclass, asdict, replace, field
import hashlib
import json
import math
import numpy as np
import pandas as pd

VERSION = '1.1'


@dataclass(frozen=True)
class EvaluationSpec:
    enabled: bool = True
    direction: int = 1
    horizons: tuple = (1, 5, 10, 20)
    primary_horizon: int = 1
    quantiles: int = 5
    min_assets: int = 20
    min_dates: int = 60
    min_label_coverage: float = 0.8
    stress_tests: bool = False
    bootstrap_samples: int = 200
    seed: int = 17
    family: str = ''
    declared_trials: int | None = None
    evidence: str = 'exploratory'
    benchmark_alpha: str = 'rank(ts_mean(amt, 20))'
    peers: tuple = ()
    train_start: str | None = None
    train_end: str | None = None
    validation_start: str | None = None
    validation_end: str | None = None
    min_reference_ic: float = 0.005
    min_reference_top_excess: float = 0.0001
    min_reference_annual_excess: float = 0.01

    def __post_init__(self):
        for name in ('enabled', 'stress_tests'):
            if type(getattr(self, name)) is not bool: raise ValueError(f'{name} must be boolean')
        if type(self.direction) is not int or self.direction not in (-1, 1): raise ValueError('direction must be 1 or -1')
        for name, lo, hi in [('primary_horizon',1,252),('quantiles',2,10),('min_assets',3,10000),
                             ('min_dates',20,10000),('bootstrap_samples',0,2000),('seed',0,2**32-1)]:
            value=getattr(self,name)
            if type(value) is not int or not lo<=value<=hi: raise ValueError(f'Invalid {name}')
        if not isinstance(self.horizons,(list,tuple)) or not 1<=len(self.horizons)<=6:
            raise ValueError('Use 1 to 6 horizons')
        if any(type(h) is not int or not 1<=h<=252 for h in self.horizons): raise ValueError('Invalid horizon')
        object.__setattr__(self,'horizons',tuple(sorted(set(self.horizons))))
        if self.primary_horizon not in self.horizons: raise ValueError('primary_horizon must be in horizons')
        if not isinstance(self.family,str) or len(self.family)>120: raise ValueError('Invalid family')
        if self.declared_trials is not None and (type(self.declared_trials) is not int or not 1<=self.declared_trials<=10000000):
            raise ValueError('Invalid declared_trials')
        if self.evidence not in ('exploratory','declared_frozen_oos','declared_holdout'):
            raise ValueError('Invalid evidence declaration')
        if isinstance(self.min_label_coverage,bool) or not isinstance(self.min_label_coverage,(int,float)) or not 0<=self.min_label_coverage<=1:
            raise ValueError('min_label_coverage must be in [0,1]')
        from .stability import validate_periods
        validate_periods(self)
        for name in ('min_reference_ic','min_reference_top_excess','min_reference_annual_excess'):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise ValueError(f'{name} must be finite and positive')
        if not isinstance(self.peers,(list,tuple)) or len(self.peers)>3: raise ValueError('At most three fixed peers')
        object.__setattr__(self,'peers',tuple(self.peers))
        from .factors import Expression, OPERATORS
        import ast
        for expression in (self.benchmark_alpha,*self.peers):
            if not isinstance(expression,str) or not expression.strip() or len(expression)>6000: raise ValueError('Invalid comparison expression')
            tree=Expression(expression).tree
            if any(isinstance(n,ast.Call) and (not isinstance(n.func,ast.Name) or n.func.id not in OPERATORS or n.keywords) for n in ast.walk(tree)):
                raise ValueError('Forbidden comparison call')
            if any(isinstance(n,ast.Constant) and isinstance(n.value,(int,float)) and not -10000<=n.value<=10000 for n in ast.walk(tree)):
                raise ValueError('Comparison expression constants exceed resource limit')

    def to_dict(self): return asdict(self)


def clean(value):
    """Strict JSON: undefined statistics remain null, including numpy scalars."""
    if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v) for v in value]
    if isinstance(value,(np.integer,)): return int(value)
    if isinstance(value,(float,np.floating)): return float(value) if np.isfinite(value) else None
    if isinstance(value,(np.bool_,)): return bool(value)
    return value


def mean_evidence(values, lag=0, bootstrap=0, seed=17):
    """Bartlett HAC on calendar positions (missing dates are NOT compressed).

    Normal approximation and moving circular-block percentile intervals are
    descriptive; neither corrects for selection across research trials.
    """
    x=np.asarray(values,dtype=float); valid=np.isfinite(x); n=int(valid.sum()); t=len(x)
    out={'n':n,'calendar_dates':t,'mean':None,'hac_se':None,'ci_low':None,'ci_high':None,
         'positive_fraction':None,'bootstrap_low':None,'bootstrap_high':None}
    if not n: return out
    mu=float(x[valid].mean()); out.update(mean=mu,positive_fraction=float((x[valid]>0).mean()))
    bandwidth=min(max(lag,int(4*(t/100)**(2/9))),max(0,t-1)); out['hac_lags']=bandwidth
    if n<20: return out
    z=np.where(valid,x-mu,0.0)
    variance=float(z@z)
    for k in range(1,bandwidth+1): variance+=2*(1-k/(bandwidth+1))*float(z[k:]@z[:-k])
    se=math.sqrt(max(0,variance)*n/(n-1))/n
    out.update(hac_se=se,ci_low=mu-1.96*se,ci_high=mu+1.96*se)
    if bootstrap:
        rng=np.random.default_rng(seed); block=min(t,max(lag+1,int(np.ceil(t**(1/3)))))
        estimates=[]
        for _ in range(bootstrap):
            starts=rng.integers(0,t,size=int(np.ceil(t/block)))
            sample=x[((starts[:,None]+np.arange(block))%t).ravel()[:t]]
            if np.isfinite(sample).any(): estimates.append(float(np.nanmean(sample)))
        if estimates: out.update(bootstrap_low=float(np.quantile(estimates,.025)),bootstrap_high=float(np.quantile(estimates,.975)),bootstrap_block=block)
    return clean(out)


def _corr(x,y):
    x=np.asarray(x,dtype=float); y=np.asarray(y,dtype=float); good=np.isfinite(x)&np.isfinite(y)
    if good.sum()<3: return np.nan
    a=x[good]-x[good].mean(); b=y[good]-y[good].mean(); denom=np.sqrt((a@a)*(b@b))
    return float(a@b/denom) if denom>0 else np.nan


def holdings_overlap(left,right,dates):
    """Average daily stock-weight intersection / union (cash excluded).

    Both-empty days are unknown, not proof of dissimilarity or duplication.
    """
    a=left.set_index(['date','code']).weight
    b=right.set_index(['date','code']).weight
    pair=pd.concat([a.rename('left'),b.rename('right')],axis=1).fillna(0)
    if pair.empty: return None
    numerator=pair.min(axis=1).groupby(level='date').sum().reindex(dates,fill_value=0)
    denominator=pair.max(axis=1).groupby(level='date').sum().reindex(dates,fill_value=0)
    ratios=numerator/denominator.replace(0,np.nan)
    return float(ratios.mean()) if ratios.notna().any() else None


def signal_diagnostics(score,data,eligible,settings,spec,start,end):
    """Entry=t+delay; exit=t+delay+h. Each horizon owns its sample.

    Bins and Top-N membership are assigned before observing labels. Boundary
    ties receive fractional membership so security-code ordering has no effect.
    """
    price=data.frames['open']*data.frames['cumadj']
    finite=score.where(np.isfinite(score)); dates=score.index
    rows=[]; bins=[]
    for i,date in enumerate(dates):
        if date<pd.Timestamp(start) or date>pd.Timestamp(end): continue
        s=finite.iloc[i]; candidate=s.dropna(); count=len(candidate)
        groups=np.ceil(candidate.rank(method='average',pct=True)*spec.quantiles).clip(1,spec.quantiles).astype(int)
        topweights=pd.Series(0.0,index=candidate.index)
        if count:
            n=min(settings.top_n,count); cutoff=candidate.nlargest(n).iloc[-1]
            above=candidate>cutoff; tied=candidate==cutoff
            topweights[above]=1; topweights[tied]=(n-int(above.sum()))/int(tied.sum())
        for h in spec.horizons:
            entry=i+settings.delay; exit_=entry+h
            boundary=exit_>=len(dates) or dates[exit_]>pd.Timestamp(end)
            entry_price=price.iloc[entry] if entry<len(dates) else price.iloc[0]*np.nan
            exit_price=price.iloc[exit_] if not boundary else price.iloc[0]*np.nan
            label=(exit_price/entry_price-1).where((entry_price>0)&(exit_price>0))
            label=label.where(np.isfinite(label)).reindex(candidate.index); good=label.notna()
            valid_count=int(good.sum()); valid_s=candidate[good]; valid_y=label[good]
            sufficient=valid_count>=spec.min_assets
            means=[]
            for group in sorted(groups.unique()):
                mask=groups==group; vals=label[mask]; avg=float(vals.mean()) if vals.notna().any() else None
                bins.append(dict(date=str(date.date()),horizon=h,quantile=int(group),assigned=int(mask.sum()),
                                 valid=int(vals.notna().sum()),mean_return=avg))
                if avg is not None: means.append((int(group),avg))
            pool=float(valid_y.mean()) if valid_count else np.nan
            weights=topweights[good]; weight=float(weights.sum())
            top=float((valid_y*weights).sum()/weight) if weight else np.nan
            # Do not treat a single surviving group or a constant signal as evidence.
            monotonic=_corr(*zip(*means)) if len(means)>=3 else np.nan
            rows.append(dict(date=str(date.date()),horizon=h,
                entry_date=str(dates[entry].date()) if entry<len(dates) else None,
                exit_date=str(dates[exit_].date()) if exit_<len(dates) else None,
                eligible=int(eligible.iloc[i].sum()),
                finite_scores=count,nonfinite_scores=int((score.iloc[i].notna()&~np.isfinite(score.iloc[i])).sum()),
                label_count=valid_count,right_censored=count if boundary else 0,
                missing_endpoints=0 if boundary else count-valid_count,
                top_assigned=float(topweights.sum()),top_valid_weight=weight,
                rank_ic=_corr(valid_s.rank(),valid_y.rank()) if sufficient else np.nan,
                pearson_ic=_corr(valid_s,valid_y) if sufficient else np.nan,
                pool_return=pool,top_return=top,
                top_excess=top-pool if sufficient and count>settings.top_n and weight>0 and candidate.nunique()>1 else np.nan,
                quantile_monotonicity=monotonic if sufficient else np.nan,
                quantile_spread=means[-1][1]-means[0][1] if sufficient and len(means)>=2 else np.nan))
    daily=pd.DataFrame(rows); quantiles=pd.DataFrame(bins,columns=['date','horizon','quantile','assigned','valid','mean_return'])
    horizons={}; yearly=[]
    for h in spec.horizons:
        part=daily[daily.horizon==h]
        horizons[str(h)]={key:mean_evidence(part[key],h-1,spec.bootstrap_samples,spec.seed+h)
                         for key in ('rank_ic','pearson_ic','top_excess','quantile_monotonicity','quantile_spread')}
        horizons[str(h)]['audit']={k:int(part[k].sum()) for k in ('eligible','finite_scores','label_count','right_censored','missing_endpoints','nonfinite_scores')}
        audit=horizons[str(h)]['audit']; matured=audit['finite_scores']-audit['right_censored']
        audit['matured_label_coverage']=audit['label_count']/matured if matured else None
        audit['top_assigned_weight']=float(part.top_assigned.sum())
        audit['top_valid_weight']=float(part.top_valid_weight.sum())
        for year,group in part.groupby(part.date.str[:4]):
            yearly.append(dict(year=year,horizon=h,dates=len(group),valid_ic_dates=int(group.rank_ic.notna().sum()),
                               rank_ic=group.rank_ic.mean(),top_excess=group.top_excess.mean()))
    return daily,quantiles,pd.DataFrame(yearly),clean(horizons)


@dataclass
class EvaluationResult:
    summary: dict
    daily: pd.DataFrame
    quantiles: pd.DataFrame
    yearly: pd.DataFrame
    scenarios: pd.DataFrame
    stability: pd.DataFrame = field(default_factory=pd.DataFrame)
    period_returns: pd.DataFrame = field(default_factory=pd.DataFrame)

    def save(self,path):
        from pathlib import Path
        path=Path(path)
        (path/'evaluation.json').write_text(json.dumps(clean(self.summary),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        for name in ('daily','quantiles','yearly','scenarios','stability','period_returns'):
            getattr(self,name).to_csv(path/f'evaluation_{name}.csv',index=False,encoding='utf-8-sig')

    def html(self):
        import html
        s=self.summary; score=s['score']; esc=html.escape
        table=pd.DataFrame(score['items'])[['label','weight','grade','points','reason']].fillna('未评价')
        return ('<h2>因子评价 · '+esc(s['version'])+'</h2><p>'+esc(s['status'])+' · 已评项目得分 '+
                str(score['earned_points'])+' / '+str(score['evaluated_weight'])+'；总分 '+str(score['total'] if score['total'] is not None else '未发布')+
                '</p><p>评分为未校准研究规则；证据声明不等于独立验证。主标签：信号日之后延迟开盘到开盘收益。</p>'+table.to_html(index=False,escape=True)+
                '<h3>期限诊断与样本审计</h3><pre>'+esc(json.dumps(s['horizons'],ensure_ascii=False,indent=2))+'</pre>'+
                '<h3>对照与压力测试</h3>'+self.scenarios.to_html(index=False,escape=True)+
                '<h3>训练—验证稳定性</h3><pre>'+esc(json.dumps(s.get('stability',{}),ensure_ascii=False,indent=2))+'</pre>'+
                self.stability.to_html(index=False,escape=True)+
                '<h3>未覆盖的证据</h3><ul>'+''.join('<li>'+esc(x)+'</li>' for x in s['limitations'])+'</ul>')


def evaluate(score,data,eligible,adv,settings,spec,sim,states,status,metadata):
    from .engine import simulate
    from .factors import Fields,Expression,decay_linear
    from .report import metrics
    daily,bins,yearly,horizons=signal_diagnostics(score,data,eligible,settings,spec,sim.daily.index[0],sim.daily.index[-1])
    primary=horizons[str(spec.primary_horizon)]
    gate_reasons=[]
    if primary['rank_ic']['n']<spec.min_dates: gate_reasons.append('主期限有效IC日期不足')
    if (primary['audit']['matured_label_coverage'] or 0)<spec.min_label_coverage: gate_reasons.append('成熟标签覆盖率不足')
    adequate=not gate_reasons
    fields=Fields(data,eligible)
    def expression_score(expression):
        panel=Expression(expression).evaluate(fields)[0]
        values=decay_linear(panel,settings.decay).values.where(eligible)
        return values.where(np.isfinite(values))
    scenarios=[]; comparisons={}
    def run_comparison(name,values,opts):
        # An empty factor is NOT a valid all-cash benchmark.
        available=values.loc[sim.daily.index[0]:sim.daily.index[-1]].notna().any(axis=1).sum()
        if available<spec.min_dates:
            scenarios.append(dict(name=name,status='insufficient_evidence',signal_dates=int(available)))
            return None
        other=simulate(data,values,eligible,adv,opts,states,status); m=metrics(other,opts)
        comparisons[name]=(other,m)
        scenarios.append(dict(name=name,status='evaluated',signal_dates=int(available),**{k:m[k] for k in ('annual_return','total_return','max_drawdown','sharpe_zero_rf','annual_turnover_two_sided','order_fill_rate')},settings=json.dumps(opts.to_dict(),sort_keys=True)))
        return other,m
    base=metrics(sim,settings)
    benchmark_score=expression_score(spec.benchmark_alpha)
    benchmark=run_comparison('liquidity_benchmark',benchmark_score,settings)
    from .stability import evaluate_stability
    stability,stability_table,period_returns=evaluate_stability(daily,score,benchmark_score,data,eligible,adv,settings,spec,states,status)
    if spec.stress_tests:
        run_comparison('cost_x2',score,replace(settings,commission=settings.commission*2,min_commission=settings.min_commission*2,slippage_bps=settings.slippage_bps*2,transfer_rate=settings.transfer_rate*2))
        run_comparison('delay_plus_1',score,replace(settings,delay=settings.delay+1))
        run_comparison('capital_x2',score,replace(settings,initial_cash=settings.initial_cash*2))
        # Causal smoothing is a signal perturbation, not a raw-data robustness claim.
        run_comparison('smoothing_2d',score.rolling(2,min_periods=2).mean(),settings)
    peer_info=[]; peer_values=[]
    for i,expression in enumerate(spec.peers):
        values=expression_score(expression); other=run_comparison(f'peer_{i+1}',values,settings)
        peer_values.append(values.rank(axis=1,pct=True))
        common=np.isfinite(score)&np.isfinite(values)
        daily_corr=[_corr(a,b) for a,b in zip(score.where(common).loc[sim.daily.index].rank(axis=1).to_numpy(),values.where(common).loc[sim.daily.index].rank(axis=1).to_numpy())]
        valid=[v for v in daily_corr if np.isfinite(v)]
        peer_info.append(dict(expression=expression,signal_correlation=float(np.mean(valid)) if valid else None,
                              signal_absolute_correlation=float(np.mean(np.abs(valid))) if valid else None,
                              return_correlation=_corr(sim.daily['return'],other[0].daily['return']) if other else None,
                              holdings_overlap=holdings_overlap(sim.holdings,other[0].holdings,sim.daily.index) if other else None))
    if peer_values:
        # Complete-case common universe; fixed equal-weight rank fusion, no fitted weights.
        peer_mix=sum(peer_values)/len(peer_values)
        run_comparison('peer_basket',peer_mix,settings)
        run_comparison('peer_basket_plus_candidate',(peer_mix*len(peer_values)+score.rank(axis=1,pct=True))/(len(peer_values)+1),settings)
    items=[]
    def add(key,label,weight,grade,reason,observed=None):
        if not adequate: grade=None; reason=' / '.join(gate_reasons)+'；'+reason
        items.append(dict(key=key,label=label,weight=weight,grade=grade,points=weight*grade/4 if grade is not None else None,reason=reason,observed=observed))
    def evidence_grade(e):
        if e['n']<spec.min_dates or e['mean'] is None: return None
        if e['mean']<=0: return 0
        if e['ci_low'] is None or e['ci_low']<=0: return 1
        return 4 if e['positive_fraction']>=.6 else 3
    add('ic','预测 · 主期限 Rank IC',10,evidence_grade(primary['rank_ic']),'均值≤0:0；均值>0但95%HAC下界≤0:1；下界>0:3；再满足正值日期≥60%:4',primary['rank_ic'])
    add('top','预测 · Top-N 同池超额',10,evidence_grade(primary['top_excess']),'使用与组合相同的 Top-N；边界同分按比例分配，评分规则同 IC',primary['top_excess'])
    add('stratification','预测 · 分层一致性',5,evidence_grade(primary['quantile_monotonicity']),'至少三个有标签分组；使用分组序号与均值收益的相关性，评分规则同 IC',primary['quantile_monotonicity'])
    excess=base['annual_return']-benchmark[1]['annual_return'] if benchmark else None
    economic=None if excess is None else int(sum(excess>x for x in (0,.02,.05,.10)))
    add('economic','经济 · 扣费后基线超额',15,economic,'年化收益率之差超过 0 / 2 / 5 / 10 个百分点分别累积1级；不是年化超额收益复利',excess)
    def tail(other): return float(other.daily['return'].nsmallest(max(1,math.ceil(len(other.daily)*.05))).mean())
    risk=None if not benchmark else int(base['max_drawdown']>=benchmark[1]['max_drawdown'])*2+int(tail(sim)>=tail(benchmark[0]))*2
    if excess is not None and excess<=0: risk=0
    add('risk','经济 · 回撤与尾部',10,risk,'正基线超额前提下，最大回撤和最差5%日均收益各不劣于基线得2级；亏损低仓位不因此加分',dict(candidate_tail=tail(sim),benchmark_tail=tail(benchmark[0]) if benchmark else None))
    series=daily[daily.horizon==spec.primary_horizon].rank_ic.to_numpy()
    blocks=np.array_split(series,4); block_means=[float(np.nanmean(b)) if np.isfinite(b).sum()>=max(5,spec.min_dates//4) else None for b in blocks]
    temporal=sum(x>0 for x in block_means) if all(x is not None for x in block_means) else None
    add('time','稳健 · 区间内时间分段',2,temporal,'按日历顺序四等分；各段样本足够且均值IC>0各得1级；仅占2分，不替代训练—验证衰减评价',block_means)
    for key,label,weight in [('rank_ic','IC保留',3),('top_excess','Top-N超额保留',1),('annual_excess','扣费后基线超额保留',2)]:
        evidence=stability['grades'][key]
        add('oos_'+key,'稳健 · 训练→验证 '+label,weight,evidence['grade'],evidence['reason'],evidence)
    def retention(name):
        if name not in comparisons or base['annual_return']<=0: return None
        return comparisons[name][1]['annual_return']/base['annual_return']
    def retention_grade(value): return None if value is None else int(sum(value>=x for x in (0,.5,.75,.9)))
    add('perturbation','稳健 · 信号平滑扰动',6,retention_grade(retention('smoothing_2d')),'正原始年化收益前提下，两日因果平滑后保留比例≥0 / 50 / 75 / 90%各1级；不代表原始数据或全部参数稳健',retention('smoothing_2d'))
    stress=[retention(n) for n in ('cost_x2','delay_plus_1')]
    add('stress','稳健 · 费用与延迟压力',6,retention_grade(min(stress)) if all(v is not None for v in stress) else None,'费用(不改法定印花税)翻倍和延迟增加一天，取较差收益保留率；阈值同平滑扰动',stress)
    # Requested and filled quantities are economic equivalent shares; use price-weighted notionals.
    orders=sim.orders
    references=pd.to_numeric(orders.sizing_reference_close,errors='coerce')
    priced=np.isfinite(references)&(references>0)
    requested=float((orders.requested_shares_equiv.abs()*references).sum())
    filled=float((orders.filled_shares_equiv.abs()*references).sum())
    # Fail closed if any requested order lacks a reference; rejected opens remain in denominator.
    fill=filled/requested if requested>0 and priced.all() else None
    add('tracking','实施 · 请求金额成交率',5,None if fill is None else int(sum(fill>=x for x in (.5,.75,.9,.98))),'全部请求以各自前收估值，含缺失开盘或涨跌停拒单；≥50 / 75 / 90 / 98%各1级；存在无法估值请求时不评分',fill)
    add('capacity','实施 · 资金翻倍',5,retention_grade(retention('capital_x2')),'资金翻倍后的年化收益保留率，阈值同平滑扰动；不是市场冲击容量估计',retention('capital_x2'))
    add('cost_retention','实施 · 成本后优势保留',5,retention_grade(retention('cost_x2')),'成本翻倍收益保留率；与压力项共享证据，权重属于待校准的研究偏好',retention('cost_x2'))
    correlations=[p[k] for p in peer_info for k in ('signal_absolute_correlation','return_correlation','holdings_overlap')]
    duplicate=max(abs(x) for x in correlations) if correlations and all(x is not None and np.isfinite(x) for x in correlations) else None
    novelty=None if duplicate is None else int(sum(duplicate<x for x in (.95,.8,.6,.4)))
    if novelty is not None and (primary['rank_ic']['mean'] or 0)<=0: novelty=0
    add('duplication','增量 · 信号/收益/持仓重复度',5,novelty,'取日信号绝对相关均值、绝对收益相关、持仓交并比三者的最大值；<95 / 80 / 60 / 40%各1级；主IC非正记0',duplicate)
    marginal=None
    if 'peer_basket' in comparisons and 'peer_basket_plus_candidate' in comparisons:
        old=comparisons['peer_basket'][1]; new=comparisons['peer_basket_plus_candidate'][1]
        marginal=new['annual_return']-old['annual_return']
        grade=int(sum(marginal>x for x in (0,.01,.025,.05)))
        if new['max_drawdown']<old['max_drawdown']-.02: grade=min(grade,1)
    else: grade=None
    add('marginal','增量 · 固定组合加入效果',10,grade,'固定等权排名融合；年化改善>0 / 1 / 2.5 / 5个百分点各1级；回撤恶化>2个百分点最多1级',marginal)
    earned=sum(x['points'] or 0 for x in items); weight=sum(x['weight'] for x in items if x['grade'] is not None)
    # Compatibility hash includes the actual data/rule snapshots and evaluation contract.
    data_hash=hashlib.sha256()
    for name,frame in sorted(data.frames.items()):
        data_hash.update(name.encode()); data_hash.update(json.dumps(list(frame.columns)).encode())
        data_hash.update(pd.util.hash_pandas_object(frame,index=True).to_numpy().tobytes())
    contract=dict(version=VERSION,settings=settings.to_dict(),evaluation=spec.to_dict(),source_sha256=metadata.get('source_sha256'),effective_data_sha256=data_hash.hexdigest(),
                  security_sha256=metadata.get('security_history_sha256'),rules_sha256=metadata.get('trading_states_sha256'))
    fingerprint=hashlib.sha256(json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    comparison={**contract,'evaluation':{k:v for k,v in spec.to_dict().items() if k not in ('family','declared_trials','evidence')}}
    comparison_hash=hashlib.sha256(json.dumps(comparison,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    synthetic=bool(data.metadata.get('synthetic'))
    profile=bins.groupby(['horizon','quantile'],as_index=False).agg(mean_return=('mean_return','mean'),
        valid_dates=('mean_return','count'),assigned=('assigned','sum'),valid=('valid','sum')).to_dict('records') if len(bins) else []
    summary=dict(version=VERSION,protocol=contract,protocol_sha256=fingerprint,status='research_only' if adequate else 'insufficient_evidence',
        comparison_sha256=comparison_hash,gate_reasons=gate_reasons,
        evidence=dict(declared=spec.evidence,independently_verified=False,declared_trials=spec.declared_trials,family=spec.family,data_state='synthetic' if synthetic else 'research_approximate'),
        label=dict(price='open * cumadj',entry_offset=settings.delay,exit_offset=f'delay + horizon',direction=spec.direction,
                   top_n=settings.top_n,ties='fractional Top-N; average-rank quantiles',missing='per-horizon endpoints; no forward fill'),
        horizons=horizons,peers=peer_info,base_metrics=base,
        scenarios=pd.DataFrame(scenarios).to_dict('records'),yearly=yearly.to_dict('records'),quantile_profile=profile,stability=stability,
        score=dict(version='heuristic-1.1',earned_points=round(earned,3),evaluated_weight=weight,total=round(earned,3) if weight==100 and not synthetic else None,
                   priority='合成数据，仅用于软件验证' if synthetic else ('优先复核' if earned>=80 else '候选研究' if earned>=65 else '继续观察' if earned>=50 else '低优先级') if weight==100 else '证据未齐，不发布总评级',items=items),
        limitations=['分数阈值未经过独立数据校准，分项存在相关性；不得解释为盈利概率或统计认证。',
            'HAC和区块bootstrap仅描述单次试验的时间依赖；未校正跨因子、参数、方向和研究路径选择，未计算DSR/PBO。',
            '样本外/留出声明由研究者填写，系统未证明预注册、锁定历史或未窥视。',
            '标签是复权开盘持有收益；不包含无法成交、停牌退出、冲击或退市损失的真实执行保证。',
            '行业/规模中性、风格归因、条件尾部增量、拥挤度和原始数据修订敏感性尚未实现；不能由低相关替代。',
            '费用压力保持法定印花税不变；资金压力复用日线容量模型，未建模内生市场冲击。',
            '同伴表达式按值越大越好；主方向只影响候选。分组与Top-N先分配，再审计标签缺失，仍须关注非随机缺失偏差。',
            '本地试验记录不能覆盖外部试验；declared_trials只是声明，不据此伪造多重检验修正。'])
    return EvaluationResult(clean(summary),daily,bins,yearly,pd.DataFrame(scenarios),stability_table,period_returns)
