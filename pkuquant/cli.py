import argparse
from datetime import datetime
import json
from pathlib import Path
import html
import pandas as pd
from dataclasses import fields
from .api import Research
from .config import Settings
from .factors import OPERATORS

DEFAULT_ALPHA = 'rank(ts_delay(close, 20) / ts_delay(close, 120) - 1)'


def main(argv=None):
    parser = argparse.ArgumentParser(description='因序：因子批量算，交易逐日走')
    parser.add_argument('command',choices=['run','compare','operators','inspect','doctor','import-data','pack-data','serve'])
    parser.add_argument('--data',default='feathers.zip')
    parser.add_argument('--alpha',default=None)
    parser.add_argument('--strategy',help='JSON file containing alpha and settings')
    parser.add_argument('--strategies',nargs='+',help='Strategy JSON files for compare')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--cash',type=float)
    parser.add_argument('--output',help='New output directory (existing directories are never overwritten)')
    parser.add_argument('--states',help='Optional externally verified trading-state CSV')
    parser.add_argument('--security-history',help='Historical ST events CSV with effective_date and known_at')
    parser.add_argument('--reference-cache',help='Audited BaoStock daily CSV.gz cache; missing/mismatched records reject trades')
    parser.add_argument('--calendar',help='CSV with date column; used by pack-data')
    parser.add_argument('--provider-name',default='local-files')
    parser.add_argument('--provider-version',default='1')
    parser.add_argument('--source-spec',help='Local tabular adapter JSON: path, mapping, amount_unit, volume_unit, expected_sessions')
    parser.add_argument('--service-config',default='service.json',help='Administrator-managed service configuration')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args = parser.parse_args(argv)
    if args.command == 'operators':
        print('Fields: open, high, low, close, amt, num_trades, returns; volume when actual share volume is provided')
        print('Operators: '+', '.join(sorted(OPERATORS)))
        print('No synthetic volume/VWAP, future shifts, imports, attributes or arbitrary Python in expressions.')
        return 0
    if args.command=='serve':
        import os
        import uvicorn
        os.environ['PKUQUANT_SERVICE_CONFIG']=str(Path(args.service_config).resolve())
        uvicorn.run('pkuquant.service:app_from_env',factory=True,host=args.host,port=args.port,workers=1)
        return 0
    source=args.data
    if args.source_spec:
        from .sources import TabularSource
        specpath=Path(args.source_spec).resolve()
        spec=json.loads(specpath.read_text(encoding='utf-8-sig'))
        spec['path']=specpath.parent/Path(spec['path'])
        source=TabularSource(**spec)
    if args.command=='pack-data':
        from .providers import DataRequest,FunctionProvider,collect
        from .sources import load_source
        if not args.output: parser.error('pack-data requires --output')
        market=load_source(source)
        tables={name:pd.read_csv(file) for name,file in [('security_history',args.security_history),('trading_states',args.states)] if file}
        callbacks={name:(lambda request,table=table:table) for name,table in tables.items()}
        if args.calendar:
            calendar=pd.DatetimeIndex(pd.read_csv(args.calendar)['date'])
            callbacks['calendar']=lambda request:calendar
            start,end=calendar.min(),calendar.max()
        else: start,end=market.dates[0],market.dates[-1]
        request=DataRequest(tuple(market.codes),str(start.date()),str(end.date()))
        provider=FunctionProvider(name=args.provider_name,version=args.provider_version,bars=lambda request:market,**callbacks)
        folder=collect(provider,request).save(args.output)
        print(folder); return 0
    lab = Research(source,args.states,args.security_history,reference_cache=args.reference_cache)
    if args.command=='import-data':
        if not args.source_spec or not args.output: parser.error('import-data requires --source-spec and --output')
        print(lab.data.export(args.output).resolve()); return 0
    if args.command == 'inspect':
        print(json.dumps(lab.data.quality(),ensure_ascii=False,indent=2)); return 0
    if args.command=='doctor':
        config=json.loads(Path(args.strategy).read_text(encoding='utf-8-sig')) if args.strategy else {}
        opts=config.get('settings',{}).copy()
        if args.start: opts['start']=args.start
        if args.end: opts['end']=args.end
        check=lab.doctor(Settings(**opts))
        print(json.dumps(check,ensure_ascii=False,indent=2))
        return 0 if check['verified_data_ready'] else 2
    if args.command == 'compare':
        sources = args.strategies or ['examples/liquidity_baseline.json','examples/momentum.json','examples/low_volatility.json','examples/reversal.json']
        folder = Path(args.output or str(Path('outputs')/datetime.now().strftime('compare_%Y%m%d_%H%M%S_%f')))
        folder.mkdir(parents=True,exist_ok=False)
        records = []
        for i,source in enumerate(sources):
            cfg = json.loads(Path(source).read_text(encoding='utf-8-sig'))
            opts = cfg.get('settings',{}).copy()
            if args.start: opts['start']=args.start
            if args.end: opts['end']=args.end
            if args.cash is not None: opts['initial_cash']=args.cash
            print(f'[{i+1}/{len(sources)}] {source}',flush=True)
            r = lab.run(cfg['alpha'],Settings(**opts),evaluation=cfg.get('evaluation'))
            name = f'{i+1:02d}_{Path(source).stem}'
            r.save(folder/name)
            records.append(dict(strategy=cfg.get('name',Path(source).stem),start=str(r.daily.index[0].date()),end=str(r.daily.index[-1].date()),
                                total_return=r.metrics['total_return'],annual_return=r.metrics['annual_return'],
                                sharpe=r.metrics['sharpe_zero_rf'],max_drawdown=r.metrics['max_drawdown'],
                                explicit_cost=r.metrics['total_explicit_cost'],report=f'{name}/report.html'))
        frame = pd.DataFrame(records)
        frame.to_csv(folder/'comparison.csv',index=False,encoding='utf-8-sig')
        display = frame.copy()
        for c in ['total_return','annual_return','max_drawdown']: display[c]=display[c].map(lambda x:f'{x:.2%}')
        display['report'] = display['report'].map(lambda x:f'<a href="{html.escape(x,quote=True)}">报告</a>')
        # Escape user-configured names before permitting our generated report links.
        display['strategy'] = display['strategy'].map(lambda x:html.escape(str(x)))
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>策略对比</title><style>body{font:15px/1.8 system-ui;background:#0c1424;color:#e2e8f0;padding:32px}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #334155}a{color:#5eead4}</style><h1>策略对比</h1><p>研究级复权账户与日线近似执行。未进行参数优化；不是盈利保证。请检查各策略起止日期与配置是否一致，样本期不同不可直接排名。</p>' + display.to_html(index=False,escape=False) + '</html>'
        (folder/'comparison.html').write_text(page,encoding='utf-8')
        print(frame.to_string(index=False))
        print(folder.resolve()/'comparison.html')
        return 0
    config = json.loads(Path(args.strategy).read_text(encoding='utf-8-sig')) if args.strategy else {}
    if set(config)-{'alpha','settings','name','description','evaluation'}: parser.error('Unknown strategy keys')
    settings = config.get('settings',{}).copy()
    known = {f.name for f in fields(Settings)}
    if set(settings)-known: parser.error(f'Unknown settings: {set(settings)-known}')
    if args.start: settings['start']=args.start
    if args.end: settings['end']=args.end
    if args.cash is not None: settings['initial_cash']=args.cash
    alpha = args.alpha if args.alpha is not None else config.get('alpha',DEFAULT_ALPHA)
    print('Computing factor and daily simulation...',flush=True)
    result = lab.run(alpha,Settings(**settings),evaluation=config.get('evaluation'))
    output = args.output or str(Path('outputs')/datetime.now().strftime('run_%Y%m%d_%H%M%S_%f'))
    folder = result.save(output)
    print(json.dumps(result.metrics,ensure_ascii=False,indent=2))
    print(f'Research accounting: total-return economic units. Report: {folder / "report.html"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
