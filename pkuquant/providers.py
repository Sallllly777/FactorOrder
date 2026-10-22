"""Versioned, read-only provider contract for future vendor integrations.

Adapters fetch data once; strategies and the daily simulator never call a vendor.
Credentials and SDK clients belong to the adapter, not to exported bundles.
"""
from dataclasses import dataclass,asdict
from pathlib import Path
from typing import Protocol
import hashlib
import json
import os
import uuid
import pandas as pd

from .data import MarketData,FIELDS
from .security import SecurityHistory
from .engine import TradingStates
from .sources import normalize_code,load_source

DATASETS=frozenset({'bars','calendar','security_history','trading_states'})
SCHEMA_VERSION=1


class MissingCapability(ValueError):
    """A requested capability is not advertised; no data was fabricated."""


@dataclass(frozen=True)
class DataRequest:
    symbols: tuple[str,...]
    start: str
    end: str

    def __post_init__(self):
        if isinstance(self.symbols,str): raise ValueError('symbols must be a sequence, not a string')
        symbols=tuple(normalize_code(c) for c in self.symbols)
        if not symbols or len(set(symbols))!=len(symbols): raise ValueError('symbols must be nonempty and unique')
        object.__setattr__(self,'symbols',symbols)
        for name in ('start','end'):
            t=pd.Timestamp(getattr(self,name))
            if pd.isna(t) or t.tzinfo or t!=t.normalize(): raise ValueError('Request dates must be naive market dates')
            object.__setattr__(self,name,t.strftime('%Y-%m-%d'))
        if self.start>self.end: raise ValueError('start is later than end')


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    version: str
    capabilities: tuple[str,...]

    def __post_init__(self):
        for name in ('name','version'):
            value=getattr(self,name)
            if not isinstance(value,str) or not value.strip() or len(value)>100: raise ValueError('Invalid provider identity')
        caps=tuple(self.capabilities)
        if len(set(caps))!=len(caps) or set(caps)-DATASETS or 'bars' not in caps:
            raise ValueError('Declare bars and only supported historical datasets')
        object.__setattr__(self,'capabilities',tuple(sorted(caps)))


class HistoricalProvider(Protocol):
    info: ProviderInfo

    def fetch(self,dataset:str,request:DataRequest):
        """bars -> MarketData; calendar -> DatetimeIndex; other datasets -> DataFrame."""
        ...


class FunctionProvider:
    """Small adapter: one trusted local callback per supported dataset."""
    def __init__(self,*,name,version='1',bars,calendar=None,security_history=None,trading_states=None):
        callbacks=dict(bars=bars,calendar=calendar,security_history=security_history,trading_states=trading_states)
        self.callbacks={k:v for k,v in callbacks.items() if v is not None}
        if any(not callable(f) for f in self.callbacks.values()): raise TypeError('Provider callbacks must be callable')
        self.info=ProviderInfo(name,version,tuple(self.callbacks))

    def fetch(self,dataset,request):
        if dataset not in self.callbacks: raise MissingCapability(f'{self.info.name} does not support {dataset}')
        return self.callbacks[dataset](request)


class SourceProvider(FunctionProvider):
    """Wrap existing QMTSource/TushareSource/TabularSource factories unchanged."""
    def __init__(self,source_factory,*,name,version='1',**optional_datasets):
        super().__init__(name=name,version=version,bars=lambda r:load_source(source_factory(r)),**optional_datasets)


def _calendar(values,request):
    idx=pd.DatetimeIndex(values)
    if idx.empty or idx.hasnans or idx.tz is not None or not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError('Calendar must contain unique sorted naive market dates')
    if not (idx==idx.normalize()).all() or (idx<pd.Timestamp(request.start)).any() or (idx>pd.Timestamp(request.end)).any():
        raise ValueError('Calendar dates must be within the request and at midnight')
    return idx


def _frame(value,name):
    if not isinstance(value,pd.DataFrame): raise TypeError(f'{name} must return a DataFrame, not None or a vendor object')
    frame=value.copy()
    if 'code' not in frame: raise ValueError(f'{name} is missing code')
    frame['code']=frame.code.map(normalize_code)
    return frame


def _hash_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): digest.update(chunk)
    return digest.hexdigest()


@dataclass
class DatasetBundle:
    market: MarketData
    provider: ProviderInfo
    request: DataRequest
    security_history: pd.DataFrame | None = None
    trading_states: pd.DataFrame | None = None
    calendar: pd.DatetimeIndex | None = None
    snapshot_sha256: str | None = None

    def validate(self):
        if not isinstance(self.market,MarketData): raise TypeError('bars must return MarketData')
        if set(self.market.codes)!=set(self.request.symbols): raise ValueError('Provider must return exactly the requested securities')
        if self.market.dates[0]<pd.Timestamp(self.request.start) or self.market.dates[-1]>pd.Timestamp(self.request.end):
            raise ValueError('Provider bars extend beyond request')
        actual={'bars'}
        if self.security_history is not None:
            self.security_history=_frame(self.security_history,'security_history')
            if set(self.security_history.code)-set(self.request.symbols): raise ValueError('Unrequested security in ST history')
            SecurityHistory(self.security_history)
            actual.add('security_history')
        if self.trading_states is not None:
            self.trading_states=_frame(self.trading_states,'trading_states')
            if set(self.trading_states.code)-set(self.request.symbols): raise ValueError('Unrequested security in trading rules')
            TradingStates(self.trading_states)
            dates=pd.to_datetime(self.trading_states.date)
            if ((dates<pd.Timestamp(self.request.start))|(dates>pd.Timestamp(self.request.end))).any():
                raise ValueError('Trading rules extend beyond request')
            actual.add('trading_states')
        if self.calendar is not None:
            self.calendar=_calendar(self.calendar,self.request)
            if not self.market.dates.equals(self.calendar): raise ValueError('Bundle bars must align with the explicit calendar')
            actual.add('calendar')
        if actual!=set(self.provider.capabilities): raise ValueError('Advertised datasets and delivered datasets differ')
        return self

    def provenance(self):
        return {'schema_version':SCHEMA_VERSION,'provider':asdict(self.provider),
                'request':asdict(self.request),'snapshot_sha256':self.snapshot_sha256,
                'calendar_sha256':hashlib.sha256(self.calendar.asi8.tobytes()).hexdigest() if self.calendar is not None else None,
                'note':'Capabilities describe delivered interfaces, not historical coverage or vendor certification.'}

    def save(self,path):
        self.validate()
        path=Path(path).resolve(); path.parent.mkdir(parents=True,exist_ok=True)
        lock=path.parent/('.'+path.name+'.lock')
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            if path.exists(): raise FileExistsError(path)
            stage=path.parent/('.'+path.name+'.pending-'+uuid.uuid4().hex); stage.mkdir()
            self.market.export(stage/'market')
            for name in ('security_history','trading_states'):
                frame=getattr(self,name)
                if frame is not None: frame.to_csv(stage/(name+'.csv'),index=False,encoding='utf-8')
            if self.calendar is not None:
                pd.DataFrame({'date':self.calendar}).to_csv(stage/'calendar.csv',index=False)
            hashes={p.relative_to(stage).as_posix():_hash_file(p) for p in sorted(stage.rglob('*')) if p.is_file()}
            manifest={**self.provenance(),'snapshot_sha256':None,'files':hashes}
            (stage/'bundle.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            stage.rename(path)
        finally:
            os.close(fd); lock.unlink()
        return path

    @classmethod
    def load(cls,path):
        root=Path(path).resolve(); manifest=root/'bundle.json'
        raw=manifest.read_bytes(); info=json.loads(raw)
        if info.get('schema_version')!=SCHEMA_VERSION: raise ValueError('Unsupported bundle schema version')
        allowed={f'market/{name}.feather' for name in (*FIELDS,'volume')}|{'market/metadata.json','security_history.csv','trading_states.csv','calendar.csv'}
        hashes=info.get('files')
        if not isinstance(hashes,dict) or set(hashes)-allowed: raise ValueError('Unsafe or unsupported bundle paths')
        required={f'market/{name}.feather' for name in FIELDS}|{'market/metadata.json'}
        if required-set(hashes): raise ValueError('Incomplete bundle manifest')
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}-{'bundle.json'}
        if actual!=set(hashes): raise ValueError('Bundle file inventory differs from manifest')
        for name,digest in hashes.items():
            file=root/name
            if not file.resolve().is_relative_to(root) or not file.is_file() or _hash_file(file)!=digest:
                raise ValueError(f'Bundle integrity check failed: {name}')
        tables={name:pd.read_csv(root/(name+'.csv')) if name+'.csv' in hashes else None for name in ('security_history','trading_states')}
        calendar=pd.DatetimeIndex(pd.read_csv(root/'calendar.csv')['date']) if 'calendar.csv' in hashes else None
        bundle=cls(MarketData.load(root/'market'),ProviderInfo(**info['provider']),DataRequest(**info['request']),
                   **tables,calendar=calendar,snapshot_sha256=hashlib.sha256(raw).hexdigest())
        bundle.validate()
        # A stale metadata flag cannot override the actual calendar contract.
        bundle.market.metadata['calendar_verified']=calendar is not None
        return bundle


def collect(provider:HistoricalProvider,request:DataRequest,*,require=()):
    if not isinstance(provider.info,ProviderInfo): raise TypeError('provider.info must be ProviderInfo')
    required=set(require)|{'bars'}
    if required-DATASETS: raise ValueError('Unknown required dataset')
    missing=required-set(provider.info.capabilities)
    if missing: raise MissingCapability(f'Missing provider capabilities: {sorted(missing)}')
    data=provider.fetch('bars',request)
    if not isinstance(data,MarketData): raise TypeError('bars must return normalized MarketData')
    extras={}
    for name in ('calendar','security_history','trading_states'):
        if name in provider.info.capabilities:
            extras[name]=provider.fetch(name,request)
            if extras[name] is None: raise ValueError(f'Advertised dataset {name} returned None')
    calendar=_calendar(extras['calendar'],request) if 'calendar' in extras else None
    frames=data.frames
    if calendar is not None:
        if not data.dates.isin(calendar).all(): raise ValueError('Provider bars contain dates outside calendar')
        frames={name:frame.reindex(calendar) for name,frame in data.frames.items()}
        extras['calendar']=calendar
    meta={**data.metadata,'calendar_verified':calendar is not None,'provider_contract':asdict(provider.info)}
    data=MarketData(frames,data.source,data.source_hash,meta)
    return DatasetBundle(data,provider.info,request,**extras).validate()
