"""Browser workbench metadata and bounded result views; no vendor connections."""
import csv
import json
import math
from pathlib import Path
from .config import Settings
from .evaluation import EvaluationSpec

TEMPLATES=[
    {'id':'5dr-current', **json.loads((Path(__file__).parent/'static'/'5dr_current.json').read_text(encoding='utf-8'))},
    *json.loads((Path(__file__).parent/'static'/'5dr_extensions.json').read_text(encoding='utf-8')),
    {'id':'low-vol','name':'低波动','description':'偏好过去 60 日收益波动较低的股票。','alpha':'-rank(ts_std(returns, 60))'},
    {'id':'momentum','name':'中期动量','description':'比较中期涨幅，跳过最近 20 日。','alpha':'rank(ts_delay(close, 20) / ts_delay(close, 120) - 1)'},
    {'id':'reversal','name':'短期反转','description':'偏好过去 5 日跌幅较大的股票。','alpha':'-rank(ts_return(close, 5))'},
    {'id':'liquidity','name':'流动性基线','description':'按 20 日平均成交金额排序，用作研究对照。','alpha':'rank(ts_mean(amt, 20))'},
]
OPERATORS_HELP=[
    ['rank(x)','横截面百分位排名，值越大排名越高。'],['zscore(x)','横截面标准化。'],
    ['ts_return(close, n)','过去 n 日前复权收益率。'],['ts_delay(x, n)','读取 n 个数据日期之前的值。'],
    ['ts_mean(x, n)','过去 n 日移动平均，需完整窗口。'],['ts_std(x, n)','过去 n 日标准差。'],
    ['ts_rank(x, n)','当前值在过去 n 日窗口中的百分位。'],['ts_corr(x, y, n)','过去 n 日滚动相关系数。'],
    ['decay_linear(x, n)','近期值获得更高权重的线性衰减。'],['where(condition, x, y)','满足条件取 x，否则取 y。'],
    ['trade_when(trigger, alpha, exit)','触发时更新因子，否则保留；退出条件优先。'],
]


def config_view(registry):
    sources=[]
    for key,source in registry.items():
        entry={'id':key,'kind':'行情数据','synthetic':False}
        if source.get('reference_cache'): entry['kind']='行情 + 历史状态缓存（运行时校验）'
        root=Path(source['data'])
        if (root/'bundle.json').is_file():
            try:
                if (root/'bundle.json').stat().st_size>1024*1024: raise ValueError('Large manifest')
                bundle=json.loads((root/'bundle.json').read_text(encoding='utf-8'))
                entry.update(kind='完整数据快照',provider=bundle.get('provider',{}).get('name'),
                             capabilities=bundle.get('provider',{}).get('capabilities',[]))
                meta=root/'market'/'metadata.json'
                if meta.is_file() and meta.stat().st_size<1024*1024:
                    entry['synthetic']=bool(json.loads(meta.read_text(encoding='utf-8')).get('synthetic'))
            except (OSError,ValueError,TypeError,AttributeError): entry['kind']='快照待检查'
        sources.append(entry)
    defaults=Settings(start='2023-01-03',end='2023-12-29').to_dict()
    return {'defaults':defaults,'templates':TEMPLATES,'operators':OPERATORS_HELP,'sources':sources,
            'evaluation_defaults':EvaluationSpec().to_dict(),'mode':'research_only','version':'0.6.1'}


def result_view(folder):
    folder=Path(folder)
    manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    initial=manifest['settings']['initial_cash']; peak=initial
    points=[]
    with (folder/'daily.csv').open(encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f):
            nav=float(row['nav']); peak=max(peak,nav)
            if not math.isfinite(nav) or nav<=0: raise ValueError('Invalid stored equity')
            points.append({'date':row['date'][:10],'nav':nav/initial,'drawdown':nav/peak-1,
                           'cash':float(row['cash']),'holdings':int(row['holdings'])})
    total=len(points)
    if total>2500:
        indices=sorted({round(i*(total-1)/2499) for i in range(2500)})
        points=[points[i] for i in indices]
    quality=manifest.get('quality',{})
    evaluation=manifest.get('evaluation'); evaluation_series=[]
    if evaluation and (folder/'evaluation_daily.csv').is_file():
        main=evaluation['protocol']['evaluation']['primary_horizon']
        with (folder/'evaluation_daily.csv').open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f):
                if int(row['horizon'])!=main: continue
                ic=float(row['rank_ic']) if row['rank_ic'] else None
                evaluation_series.append({'date':row['date'],'rank_ic':ic if ic is not None and math.isfinite(ic) else None})
    evaluation_dates=len(evaluation_series)
    if evaluation_dates>2500:
        evaluation_series=[evaluation_series[round(i*(evaluation_dates-1)/2499)] for i in range(2500)]
    return {'metrics':manifest['metrics'],'settings':manifest['settings'],'expression':manifest['expression'],
            'evaluation':evaluation,'evaluation_series':evaluation_series,'evaluation_series_sampled':evaluation_dates>2500,
            'limitations':manifest.get('limitations',[]),'st_coverage':manifest.get('st_coverage',{}),
            'readiness':manifest.get('readiness',{}),'factor_diagnostics':manifest.get('factor_diagnostics',{}),
            'synthetic':bool(quality.get('provenance',{}).get('synthetic')),
            'curve':points,'curve_sampled':total>len(points),'daily_count':total,
            'trimmed_dates':manifest.get('trimmed_dates',[]),'source_sha256':manifest.get('source_sha256')}
