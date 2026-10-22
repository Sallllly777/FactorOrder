"""Runnable offline contract demo. All prices/status events are SYNTHETIC."""
from pathlib import Path
import argparse
import sys
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'.analysis_deps'))
from pkuquant import Research,Settings,DataRequest,FunctionProvider,collect,from_long


def make_demo_provider():
    def calendar(request): return pd.bdate_range(request.start,request.end)

    def bars(request):
        dates=calendar(request); rows=[]
        for j,code in enumerate(request.symbols):
            for i,date in enumerate(dates):
                price=10+j+i*.01
                rows.append(dict(date=date,code=code,open=price,high=price+.3,low=price-.3,close=price,
                                 amt=1e8,volume=1e7,num_trades=10000,cumadj=1))
        return from_long(pd.DataFrame(rows),amount_unit='CNY',volume_unit='shares',provider='SYNTHETIC_DEMO',provenance={'synthetic':True})

    def security_history(request):
        dates=calendar(request)
        rows=[dict(code=c,effective_date=dates[0],known_at=dates[0]-pd.Timedelta(days=1),is_st=0) for c in request.symbols]
        if len(dates)>10:
            rows.append(dict(code=request.symbols[0],effective_date=dates[10],known_at=dates[9]+pd.Timedelta(hours=18),is_st=1))
        return pd.DataFrame(rows)

    def trading_states(request):
        return pd.DataFrame([dict(date=date,code=code,tradable=1,no_limit=1,upper_limit=np.nan,lower_limit=np.nan,
                                 lot_size=100,min_buy_qty=100,price_tick=.01) for date in calendar(request) for code in request.symbols])

    return FunctionProvider(name='SYNTHETIC_DEMO',version='1',bars=bars,calendar=calendar,
                            security_history=security_history,trading_states=trading_states)


def demo_settings():
    return Settings(min_history=1,active_window=1,liquidity_window=1,liquidity_quantile=0,top_n=2,retain_n=2,
                    max_weight=.5,rebalance='weekly',unknown_st='error',require_verified_rules=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='outputs/provider_demo')
    args=parser.parse_args()
    root=Path(args.output); root.mkdir(parents=True,exist_ok=False)
    request=DataRequest(('000001.SZ','600001.SH'),'2023-08-01','2023-08-31')
    bundle=collect(make_demo_provider(),request,require=('calendar','security_history','trading_states'))
    bundle.save(root/'bundle')
    lab=Research(root/'bundle')
    result=lab.run('rank(ts_return(close, 2))',demo_settings())
    result.save(root/'report')
    print('SYNTHETIC DATA ONLY - no brokerage or market-data connection')
    print(root.resolve())
    print(lab.doctor(demo_settings()))


if __name__=='__main__': main()
