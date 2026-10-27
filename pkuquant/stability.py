"""Explicit train/validation comparisons with purged labels and fresh accounts.

No model is fitted here. 'Training' identifies the research reference interval;
the evidence declaration never certifies that validation was unseen.
"""
from dataclasses import replace
import math
import re
import numpy as np
import pandas as pd

PERIOD_FIELDS=('train_start','train_end','validation_start','validation_end')
RULES={
    'retention_thresholds':[.25,.5,.75,.9],
    'uncertain_validation_grade_cap':1,
    'uncertain_training_grade_cap':2,
    'drawdown_deterioration_cap_threshold':.05,
    'tail_deterioration_cap_threshold':.005,
    'risk_deterioration_grade_cap':1,
    'aggregation':'minimum across validation aggregate and every validation calendar year',
}
METRICS={
    'rank_ic':('Rank IC','correlation'), 'top_excess':('Top-N同池超额','horizon_return'),
    'annual_excess':('扣费后基线年化收益率之差','annual_return'),
    'annual_return':('候选扣费后年化收益','annual_return'),
    'mean_daily_active':('日均扣费后基线超额','daily_return'),
    'sharpe_zero_rf':('零无风险夏普','ratio'),
    'max_drawdown':('最大回撤（负数）','return'), 'tail_mean':('最差5%日均收益','daily_return'),
}
TABLE_COLUMNS=['comparison','metric','label','unit','train_value','validation_value','delta','absolute_gap',
               'deterioration','retention','retention_reason','train_n','validation_n','train_ci_low',
               'train_ci_high','validation_ci_low','validation_ci_high','delta_ci_low','delta_ci_high',
               'grade','grade_reason']
RETURN_COLUMNS=['period','date','candidate_return','benchmark_return','daily_active_return','candidate_nav','benchmark_nav']


def validate_periods(spec,settings=None):
    values=[getattr(spec,k) for k in PERIOD_FIELDS]
    if all(v is None for v in values): return
    if any(v is None for v in values): raise ValueError('Training and validation require all four dates')
    for name,value in zip(PERIOD_FIELDS,values):
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise ValueError(f'{name} must be YYYY-MM-DD')
        try: pd.Timestamp(value)
        except (ValueError,OverflowError) as exc: raise ValueError(f'Invalid {name}') from exc
    a,b,c,d=values
    if not a<=b<c<=d: raise ValueError('Require train_start <= train_end < validation_start <= validation_end')
    if int(d[:4])-int(c[:4])>20: raise ValueError('At most 21 validation calendar years per evaluation')
    if settings is not None:
        if settings.start and pd.Timestamp(a)<pd.Timestamp(settings.start):
            raise ValueError('Training starts before the backtest; expand Settings.start to include training')
        if settings.end and pd.Timestamp(d)>pd.Timestamp(settings.end):
            raise ValueError('Validation ends after the backtest; expand Settings.end to include validation')


def comparison_values(train,validation,floor):
    """Higher is better; drawdown/tail must be signed negative returns."""
    out=dict(train_value=train,validation_value=validation,delta=None,absolute_gap=None,
             deterioration=None,retention=None,retention_reason=None)
    if train is None or validation is None or not np.isfinite(train) or not np.isfinite(validation):
        out['retention_reason']='数据不足'; return out
    delta=validation-train
    out.update(delta=delta,absolute_gap=abs(delta),deterioration=max(0,-delta))
    if floor is None: out['retention_reason']='该指标不使用比值'
    elif train<floor: out['retention_reason']='训练值非正或小于预设参考下限'
    else: out['retention']=validation/train
    return out


def difference_interval(train,validation,calendar,lag):
    """Calendar HAC of the two-mean contrast, including boundary covariance.

    This is not an equivalence test. A CI containing zero does not prove stability.
    """
    a=train.reindex(calendar); b=validation.reindex(calendar)
    n=int(a.notna().sum()); m=int(b.notna().sum())
    if n<20 or m<20: return None,None
    if (a.notna()&b.notna()).any(): raise ValueError('Contrast intervals must not overlap')
    influence=-(a-a.mean()).fillna(0).to_numpy()/n*np.sqrt(n/(n-1))
    influence+=(b-b.mean()).fillna(0).to_numpy()/m*np.sqrt(m/(m-1))
    t=len(calendar); bandwidth=min(max(lag,int(4*(t/100)**(2/9))),t-1)
    var=float(influence@influence)
    for k in range(1,bandwidth+1):
        var+=2*(1-k/(bandwidth+1))*float(influence[k:]@influence[:-k])
    se=math.sqrt(max(0,var)); gap=float(b.mean()-a.mean())
    return gap-1.96*se,gap+1.96*se


def preservation_grade(row,train_evidence,validation_evidence,adequate):
    if not adequate: return None,'区间样本数、信号可用性或标签覆盖率未达到门槛'
    if row['retention'] is None: return None,row['retention_reason']
    if row['validation_value']<=0: return 0,'验证值非正，未保留正向优势'
    grade=sum(row['retention']>=x for x in RULES['retention_thresholds'])
    reason='保留率≥25/50/75/90%分别累积1级，改善不扣分'
    if validation_evidence.get('ci_low') is None or validation_evidence['ci_low']<=0:
        grade=min(grade,RULES['uncertain_validation_grade_cap']); reason+='；验证均值HAC下界不为正，最多1级'
    if train_evidence.get('ci_low') is None or train_evidence['ci_low']<=0:
        grade=min(grade,RULES['uncertain_training_grade_cap']); reason+='；训练均值HAC下界不为正，最多2级'
    return int(grade),reason


def evaluate_stability(daily,score,benchmark_score,data,eligible,adv,settings,spec,states,status):
    from .evaluation import mean_evidence,clean
    from .engine import simulate
    from .report import metrics
    empty_grades={k:dict(grade=None,reason='未配置训练与验证区间',comparisons=[])
                  for k in ('rank_ic','top_excess','annual_excess')}
    summary=dict(status='not_configured',grades=empty_grades,periods=[],comparisons=[],rules=RULES,
                 independently_verified=False)
    if spec.train_start is None:
        return summary,pd.DataFrame(columns=TABLE_COLUMNS),pd.DataFrame(columns=RETURN_COLUMNS)
    validate_periods(spec,settings)
    h=spec.primary_horizon
    main=daily[daily.horizon==h].copy(); main.index=pd.to_datetime(main.date)
    calendar=data.dates[(data.dates>=spec.train_start)&(data.dates<=spec.validation_end)]
    floors={'rank_ic':spec.min_reference_ic,'top_excess':spec.min_reference_top_excess,
            'annual_excess':spec.min_reference_annual_excess}
    definitions=[('train',spec.train_start,spec.train_end),('validation',spec.validation_start,spec.validation_end)]
    for year in range(int(spec.validation_start[:4]),int(spec.validation_end[:4])+1):
        definitions.append((str(year),max(spec.validation_start,f'{year}-01-01'),min(spec.validation_end,f'{year}-12-31')))
    records=[]; period_map={}; all_returns=[]; cache={}
    for name,start,end in definitions:
        # Identical annual/aggregate windows reuse calculations, without re-simulation.
        if (start,end) in cache:
            old,series,returns=cache[(start,end)]
            period_map[name]=(dict(old,name=name),series)
            all_returns.extend([{**row,'period':name} for row in returns]); continue
        part=main.loc[start:end].copy()
        matured=part.exit_date.notna() & (part.exit_date.fillna('9999-12-31')<=end)
        series={k:part[k].where(matured).astype(float) for k in ('rank_ic','top_excess')}
        evidence={k:mean_evidence(v,h-1) for k,v in series.items()}
        denominator=int(part.loc[matured,'finite_scores'].sum())
        coverage=float(part.loc[matured,'label_count'].sum()/denominator) if denominator else None
        period=dict(name=name,start=start,end=end,
            effective_start=str(part.index[0].date()) if len(part) else None,
            effective_end=str(part.index[-1].date()) if len(part) else None,
            calendar_dates=len(part),purged_signal_dates=int((~matured).sum()),
            purged_score_cells=int(part.loc[~matured,'finite_scores'].sum()),matured_label_coverage=coverage,
            signal_evidence=evidence,signal_adequate={k:v['n']>=spec.min_dates and coverage is not None and coverage>=spec.min_label_coverage for k,v in evidence.items()},
            portfolio={},portfolio_adequate=False,benchmark_signal_dates=0,candidate_signal_dates=0)
        values={k:evidence[k]['mean'] for k in series}
        count={k:evidence[k]['n'] for k in series}; returns=[]
        trading_dates=data.dates[(data.dates>=start)&(data.dates<=end)]
        if len(trading_dates):
            cut=data.through(end); subset=cut.dates
            opts=replace(settings,start=start,end=end)
            selected=[]; signal_counts=[]
            for source in (score,benchmark_score):
                s=source.reindex(subset).copy()
                s.loc[s.index<pd.Timestamp(start)]=np.nan
                s=s.where(np.isfinite(s))
                signal_counts.append(int(s.loc[start:end].notna().any(axis=1).sum()))
                selected.append(simulate(cut,s,eligible.loc[subset],adv.loc[subset],opts,states,status.loc[subset]))
            candidate,benchmark=selected
            cm,bm=metrics(candidate,opts),metrics(benchmark,opts)
            portfolio_adequate=len(trading_dates)>=spec.min_dates and min(signal_counts)>=spec.min_dates
            active=candidate.daily['return']-benchmark.daily['return']
            series['mean_daily_active']=active if portfolio_adequate else active*np.nan
            evidence['mean_daily_active']=mean_evidence(series['mean_daily_active'])
            tail=lambda sim: float(sim.daily['return'].nsmallest(max(1,math.ceil(len(sim.daily)*.05))).mean())
            values.update(annual_return=cm['annual_return'],annual_excess=cm['annual_return']-bm['annual_return'] if portfolio_adequate else None,
                          mean_daily_active=evidence['mean_daily_active']['mean'],sharpe_zero_rf=cm['sharpe_zero_rf'],
                          max_drawdown=cm['max_drawdown'],tail_mean=tail(candidate))
            count.update({k:len(trading_dates) for k in ('annual_return','annual_excess','mean_daily_active','sharpe_zero_rf','max_drawdown','tail_mean')})
            period.update(portfolio=dict(candidate=cm,benchmark=bm,candidate_tail=tail(candidate),benchmark_tail=tail(benchmark),settings=opts.to_dict()),
                          portfolio_adequate=portfolio_adequate,candidate_signal_dates=signal_counts[0],benchmark_signal_dates=signal_counts[1])
            returns=[dict(period=name,date=str(date.date()),candidate_return=float(candidate.daily.loc[date,'return']),
                          benchmark_return=float(benchmark.daily.loc[date,'return']),daily_active_return=float(active.loc[date]),
                          candidate_nav=float(candidate.daily.loc[date,'nav']),benchmark_nav=float(benchmark.daily.loc[date,'nav'])) for date in trading_dates]
        else:
            values.update({k:None for k in METRICS if k not in values})
            series['mean_daily_active']=pd.Series(dtype=float)
            evidence['mean_daily_active']=mean_evidence([])
        period.update(values=values,counts=count)
        period_map[name]=(period,series); cache[(start,end)]=(period,series,returns)
        all_returns.extend(returns)
    train,train_series=period_map['train']
    for name,_,_ in definitions[1:]:
        val,val_series=period_map[name]
        for metric,(label,unit) in METRICS.items():
            row=dict(comparison=name,metric=metric,label=label,unit=unit,
                **comparison_values(train['values'].get(metric),val['values'].get(metric),floors.get(metric)),
                train_n=train['counts'].get(metric,0),validation_n=val['counts'].get(metric,0),
                train_ci_low=None,train_ci_high=None,validation_ci_low=None,validation_ci_high=None,
                delta_ci_low=None,delta_ci_high=None,grade=None,grade_reason='旁证，不独立加分')
            te=train['signal_evidence'].get(metric,{})
            ve=val['signal_evidence'].get(metric,{})
            if metric in train_series:
                row.update(train_ci_low=te.get('ci_low'),train_ci_high=te.get('ci_high'),
                           validation_ci_low=ve.get('ci_low'),validation_ci_high=ve.get('ci_high'))
                row['delta_ci_low'],row['delta_ci_high']=difference_interval(train_series[metric],val_series[metric],calendar,h-1 if metric!='mean_daily_active' else 0)
            if metric in ('rank_ic','top_excess'):
                adequate=train['signal_adequate'][metric] and val['signal_adequate'][metric]
                row['grade'],row['grade_reason']=preservation_grade(row,te,ve,adequate)
            if metric=='annual_excess':
                # Annual excess is a CAGR difference; its uncertainty proxy is clearly
                # labelled DAILY active mean, never a fake CI on the CAGR difference.
                te=train['signal_evidence'].get('mean_daily_active',{})
                ve=val['signal_evidence'].get('mean_daily_active',{})
                row['grade'],row['grade_reason']=preservation_grade(row,te,ve,train['portfolio_adequate'] and val['portfolio_adequate'])
                row['grade_reason']+='；显著性上限依据日均扣费后基线超额HAC区间'
                if row['grade'] is not None:
                    dd=train['values']['max_drawdown']-val['values']['max_drawdown']
                    tail=train['values']['tail_mean']-val['values']['tail_mean']
                    if dd>RULES['drawdown_deterioration_cap_threshold'] or tail>RULES['tail_deterioration_cap_threshold']:
                        row['grade']=min(row['grade'],1); row['grade_reason']+='；回撤恶化>5个百分点或尾部日均收益恶化>0.5个百分点，最多1级'
            records.append(row)
    grades={}
    for metric in empty_grades:
        rows=[r for r in records if r['metric']==metric]
        missing=[r['comparison'] for r in rows if r['grade'] is None]
        grade=None if missing else min(r['grade'] for r in rows)
        grades[metric]=dict(grade=grade,reference_floor=floors[metric],
            reason=('未评价区间：'+', '.join(missing)) if missing else '取验证整体及逐验证年份的最低等级；保留率25/50/75/90%累积1级，并受验证有效性与不确定性上限约束',
            comparisons=[{k:r[k] for k in ('comparison','retention','deterioration','grade','grade_reason')} for r in rows])
    summary.update(status='evaluated' if all(v['grade'] is not None for v in grades.values()) else 'incomplete_evidence',
        grades=grades,periods=[period_map[name][0] for name,_,_ in definitions],comparisons=records,
        account_policy='Each interval starts with identical fresh cash; signals before its start are excluded; historical feature warmup is allowed; end holdings are marked, not force-liquidated.',
        label_policy='Signal dates and exit endpoints must both lie inside each interval; annual endpoints are purged independently.',
        notes=['训练指研究参考区间，系统未自动拟合模型；日期声明不证明因子、参数或方向在验证前冻结。',
               '整体与逐年验证分别从现金开始；年初重新建仓含成本，不能把各年度结果拼接为整体结果。',
               '差值HAC区间保留交易日间距及边界协方差；区间包含零不证明等效或稳定，未校正多重检验或市场状态变化。',
               '年度窗口按指定日期截取，首尾年度可能不完整；样本不足的年份不跳过，相关评分保留未评价。',
               '训练值小于参考下限时保留率未定义；训练与验证同时无效不会获得稳定性高分。'])
    return clean(summary),pd.DataFrame(records,columns=TABLE_COLUMNS),pd.DataFrame(all_returns,columns=RETURN_COLUMNS)
