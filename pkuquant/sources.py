"""Read-only provider adapters. No trading client or account credentials are used."""
from pathlib import Path
from typing import Protocol, runtime_checkable
import hashlib
import json
import re
import numpy as np
import pandas as pd
from .data import MarketData, FIELDS


@runtime_checkable
class DataSource(Protocol):
    def load(self) -> MarketData: ...


def normalize_code(code):
    s=str(code).strip().upper().replace('XSHG','SH').replace('XSHE','SZ')
    if re.fullmatch(r'\d{6}\.(SH|SZ)',s): return s
    m=re.fullmatch(r'(SH|SZ)[.\-]?(\d{6})',s)
    if m: return m[2]+'.'+m[1]
    raise ValueError(f'Code needs explicit SH/SZ exchange: {code!r}; numeric codes are not guessed')


def from_long(frame, *, mapping=None, amount_unit, price_mode='raw', volume_unit=None, provider='tabular', provenance=None,expected_sessions=None):
    """mapping maps incoming column names TO canonical names. Units are explicit."""
    if price_mode!='raw': raise ValueError('Execution needs raw OHLC plus cumadj; pre-adjusted-only prices are not accepted')
    factors={'CNY':1.0,'CNY_1K':1000.0,'CNY_10K':10000.0}
    if amount_unit not in factors: raise ValueError('amount_unit must be CNY, CNY_1K or CNY_10K')
    f=frame.rename(columns=mapping or {}).copy()
    if f.columns.duplicated().any(): raise ValueError('Column mapping creates duplicate columns')
    required={'date','code','open','high','low','close','amt','cumadj'}
    if required-set(f): raise ValueError(f'Missing normalized columns: {sorted(required-set(f))}')
    f['code']=f.code.map(normalize_code)
    raw_dates=f.date.astype(str)
    f['date']=pd.to_datetime(raw_dates,format='%Y%m%d') if raw_dates.str.fullmatch(r'\d{8}').all() else pd.to_datetime(f.date)
    if f.duplicated(['date','code']).any(): raise ValueError('Duplicate provider date/code; choose a revision explicitly')
    for name in required-{'date','code'}: f[name]=pd.to_numeric(f[name],errors='raise')
    f['amt']*=factors[amount_unit]
    if 'num_trades' not in f: f['num_trades']=np.nan
    else: f['num_trades']=pd.to_numeric(f.num_trades,errors='raise')
    names=list(FIELDS)
    if 'volume' in f:
        if volume_unit not in ('shares','lots100'): raise ValueError('Explicit volume_unit is required: shares or lots100')
        f['volume']=pd.to_numeric(f.volume,errors='raise')*(100 if volume_unit=='lots100' else 1)
        names.append('volume')
    f=f.sort_values(['date','code'])
    frames={k:f.pivot(index='date',columns='code',values=k) for k in names}
    if expected_sessions is not None:
        sessions=pd.DatetimeIndex(expected_sessions)
        if not sessions.is_unique or not sessions.is_monotonic_increasing or sessions.hasnans: raise ValueError('Invalid trading calendar')
        if not frames['close'].index.isin(sessions).all(): raise ValueError('Provider contains dates outside trading calendar')
        frames={k:v.reindex(sessions) for k,v in frames.items()}
    hasher=hashlib.sha256(pd.util.hash_pandas_object(f,index=False).to_numpy().tobytes())
    hasher.update(frames['close'].index.asi8.tobytes())
    hasher.update(json.dumps(names).encode())
    digest=hasher.hexdigest()
    meta={**(provenance or {}),'provider':provider,'price_mode':'raw','amount_unit':'CNY',
          'source_amount_unit':amount_unit,'volume_unit':'shares' if 'volume' in names else None,
          'num_trades_available':bool(f.num_trades.notna().any()),'units_explicit':True,
          'calendar_verified':expected_sessions is not None}
    return MarketData(frames,provider,digest,meta)


class TabularSource:
    def __init__(self,path,*,mapping=None,amount_unit,price_mode='raw',volume_unit=None,expected_sessions=None):
        self.path=Path(path)
        self.options=dict(mapping=mapping,amount_unit=amount_unit,price_mode=price_mode,volume_unit=volume_unit,expected_sessions=expected_sessions)
    def load(self):
        if self.path.suffix.lower()=='.csv': f=pd.read_csv(self.path,dtype=str)
        elif self.path.suffix.lower()=='.parquet':
            try: import pyarrow
            except ImportError:
                import sys
                sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.analysis_deps'))
            f=pd.read_parquet(self.path)
        else: raise ValueError('Tabular source must be .csv or .parquet')
        return from_long(f,**self.options,provider='tabular',provenance={
            'input_sha256':hashlib.sha256(self.path.read_bytes()).hexdigest(),'filename':self.path.name})


class FunctionSource:
    """Adapt another vendor using a trusted local fetch function returning long data."""
    def __init__(self,fetch,*,provider,**options):
        self.fetch,self.provider,self.options=fetch,provider,options
    def load(self): return from_long(self.fetch(),provider=self.provider,**self.options)


class QMTSource:
    """QMT/miniQMT historical data, optional SDK injection for contract tests.

    Uses locally available SDK history. Download permissions/account setup remain
    with the user. Never substitutes today's instrument name for historical ST.
    """
    def __init__(self,symbols,start,end,*,volume_unit,amount_unit='CNY',client=None):
        self.symbols=[normalize_code(c) for c in symbols]
        self.start,self.end=pd.Timestamp(start).strftime('%Y%m%d'),pd.Timestamp(end).strftime('%Y%m%d')
        self.volume_unit,self.amount_unit,self.client=volume_unit,amount_unit,client
    def load(self):
        client=self.client
        if client is None:
            from xtquant import xtdata
            client=xtdata
        args=dict(stock_list=self.symbols,period='1d',start_time=self.start,end_time=self.end,fill_data=False)
        raw=client.get_market_data_ex(field_list=['open','high','low','close','amount','volume'],dividend_type='none',**args)
        adjusted=client.get_market_data_ex(field_list=['close'],dividend_type='back_ratio',**args)
        parts=[]
        for code in self.symbols:
            if code not in raw or code not in adjusted or raw[code].empty: raise ValueError(f'QMT history unavailable: {code}')
            r=raw[code].copy()
            if not r.index.is_unique or not adjusted[code].index.is_unique: raise ValueError('Duplicate QMT history timestamps')
            adj=adjusted[code]['close'].reindex(r.index)
            if adj.isna().any(): raise ValueError('QMT adjusted/raw history mismatch')
            r['cumadj']=adj/r['close']
            r['date']=r.index.astype(str)
            r['code']=code
            parts.append(r.rename(columns={'amount':'amt'}))
        return from_long(pd.concat(parts,ignore_index=True),amount_unit=self.amount_unit,volume_unit=self.volume_unit,
                         provider='qmt',provenance={'factor_method':'back_ratio_close / raw_close','live_connection_verified':False})


class TushareSource:
    """Read-only daily+adj_factor API. Client must be configured by the caller."""
    def __init__(self,client,symbols,start,end):
        self.client,self.symbols=client,[normalize_code(c) for c in symbols]
        self.start,self.end=pd.Timestamp(start).strftime('%Y%m%d'),pd.Timestamp(end).strftime('%Y%m%d')
    def load(self):
        parts=[]
        for code in self.symbols:
            args=dict(ts_code=code,start_date=self.start,end_date=self.end)
            raw=self.client.daily(**args)
            adj=self.client.adj_factor(**args)
            if raw is None or adj is None or raw.empty: raise ValueError(f'Tushare history unavailable: {code}')
            if len(raw)>=6000 or len(adj)>=6000: raise ValueError('Potential Tushare row limit reached; request smaller date ranges')
            f=raw.merge(adj[['ts_code','trade_date','adj_factor']],on=['ts_code','trade_date'],how='left',validate='one_to_one')
            if f.adj_factor.isna().any(): raise ValueError('Missing Tushare adjustment factors')
            parts.append(f)
        return from_long(pd.concat(parts,ignore_index=True),mapping={'ts_code':'code','trade_date':'date','amount':'amt','vol':'volume','adj_factor':'cumadj'},
                         amount_unit='CNY_1K',volume_unit='lots100',provider='tushare',
                         provenance={'live_connection_verified':False,'pagination_note':'Caller must verify requested trading-calendar coverage and provider limits'})


def load_source(source):
    if isinstance(source,MarketData): return source
    if isinstance(source,DataSource): return source.load()
    return MarketData.load(source)
