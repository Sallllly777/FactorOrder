"""Audit a local BaoStock daily cache against the user's unadjusted bars.

No network calls occur in the simulator. Daily observations expire at the end
of that date; an omitted or mismatched record is UNKNOWN, never forward filled.
Reconstructed bounds remain distinct from exchange-provided daily limits.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd
from .engine import TradingStates, price_bounds
from .security import SecurityHistory


class ReferenceSnapshot(TradingStates):
    def __init__(self, market, directory):
        super().__init__()
        root=Path(directory)
        if not root.is_dir(): raise ValueError('Reference cache directory does not exist')
        self.dates,self.codes=market.dates,market.codes
        self.di={d:i for i,d in enumerate(self.dates)}
        self.ci={str(c):j for j,c in enumerate(self.codes)}
        shape=market.frames['close'].shape
        self.st=np.full(shape,np.nan,dtype=np.float32)
        self.tradable=np.full(shape,np.nan,dtype=np.float32)
        self.preclose=np.full(shape,np.nan,dtype=np.float64)
        compared=mismatched=0; failures=[]; hashes={}; events=[]
        for j,code in enumerate(self.codes):
            file=root/(str(code)+'.csv.gz')
            if not file.exists(): continue
            hashes[file.name]=hashlib.sha256(file.read_bytes()).hexdigest()
            try:
                frame=pd.read_csv(file,parse_dates=['date']).set_index('date')
                required={'code','close','preclose','tradestatus','isST'}
                if required-set(frame): raise ValueError('Missing reference fields')
                prefix,suffix=str(code).split('.')
                if not frame.code.astype(str).eq(suffix.lower()+'.'+prefix).all(): raise ValueError('Wrong security in cache')
                if frame.index.has_duplicates or frame.index.hasnans or frame.index.tz is not None or not (frame.index==frame.index.normalize()).all():
                    raise ValueError('Invalid reference dates')
                frame=frame.reindex(self.dates)
                numeric=frame[['close','preclose','tradestatus','isST']].apply(pd.to_numeric,errors='coerce')
                ours=market.frames['close'].iloc[:,j]
                both=ours.gt(0)&numeric.close.gt(0)
                bad=both & (ours-numeric.close).abs().gt(.005+1e-10)
                compared+=int(both.sum()); mismatched+=int(bad.sum())
                valid=both & ~bad & numeric.isST.isin([0,1]) & numeric.tradestatus.isin([0,1]) & numeric.preclose.gt(0)
                self.st[:,j]=numeric.isST.where(valid)
                self.tradable[:,j]=numeric.tradestatus.where(valid)
                self.preclose[:,j]=numeric.preclose.where(valid)
                values=self.st[:,j]
                # Compact only adjacent valid dates with the same state. Gaps
                # terminate intervals, including missing provider observations.
                changes=np.flatnonzero(np.r_[True,~((values[1:]==values[:-1]) | (np.isnan(values[1:])&np.isnan(values[:-1])))])
                ends=np.r_[changes[1:]-1,len(values)-1]
                for begin,end in zip(changes,ends):
                    if np.isfinite(values[begin]):
                        events.append(dict(code=str(code),effective_date=self.dates[begin],
                            known_at=self.dates[begin],valid_until=self.dates[end],is_st=int(values[begin])))
            except (ValueError,KeyError,TypeError) as exc:
                failures.append({'code':str(code),'error':str(exc)})
                self.st[:,j]=self.tradable[:,j]=self.preclose[:,j]=np.nan
        self.security=SecurityHistory(pd.DataFrame(events)) if events else SecurityHistory()
        self.sha256=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
        self.audit=dict(provider='BaoStock local daily cache',files=len(hashes),source_sha256=self.sha256,
            compared_cells=compared,mismatched_cells=mismatched,valid_state_cells=int(np.isfinite(self.st).sum()),
            failures=failures,st_timestamp_assumption='Historical effective-date flags assumed known before open; announcement vintages unavailable',
            limit_source='Reconstructed from provider preclose and ordinary board rules, not exchange-certified daily limits')

    def get(self,date,code,reference,is_st=None):
        i,j=self.di.get(date),self.ci.get(str(code))
        valid=i is not None and j is not None and np.isfinite(self.st[i,j])
        if not valid:
            # Never fall back to permissive estimates when this cache is selected.
            return dict(tradable=False,no_limit=True,upper_limit=np.nan,lower_limit=np.nan,
                        lot_size=100,min_buy_qty=100,price_tick=.01,source='reference_unknown')
        star=str(code).startswith(('688','689')); growth=str(code).startswith(('300','301','302'))
        rate=.20 if star or (growth and date>=pd.Timestamp('2020-08-24')) else .10
        if not star and not growth and self.st[i,j]==1 and date<pd.Timestamp('2026-07-06'): rate=.05
        if growth and date<pd.Timestamp('2020-08-24') and self.st[i,j]==1: rate=.05
        lower,upper=price_bounds(self.preclose[i,j],rate)
        return dict(tradable=bool(self.tradable[i,j]),no_limit=False,lower_limit=lower,upper_limit=upper,
                    lot_size=1 if star else 100,min_buy_qty=200 if star else 100,price_tick=.01,source='reconstructed')
