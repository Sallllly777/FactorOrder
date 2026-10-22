"""Immutable source matrices; no fill-forward tradability or future universe filters."""
from pathlib import Path
import hashlib
import io
import json
import zipfile

import numpy as np
import pandas as pd

FIELDS = ('open', 'high', 'low', 'close', 'amt', 'num_trades', 'cumadj')


class MarketData:
    def __init__(self, frames, source='memory', source_hash=None, metadata=None):
        missing = set(FIELDS) - set(frames)
        if missing:
            raise ValueError(f'Missing fields: {sorted(missing)}')
        self.frames = {k: frames[k].copy() for k in (*FIELDS,'volume') if k in frames}
        for df in self.frames.values():
            df.index = pd.DatetimeIndex(df.index)
        first = self.frames['close']
        self.dates = pd.DatetimeIndex(first.index)
        self.codes = first.columns
        if len(self.dates) == 0 or len(self.codes) == 0:
            raise ValueError('Empty data')
        if not self.dates.is_monotonic_increasing or not self.dates.is_unique or not self.codes.is_unique:
            raise ValueError('Dates must be sorted and unique; codes must be unique')
        if self.dates.hasnans:
            raise ValueError('NaT date')
        if self.dates.tz is not None or not (self.dates == self.dates.normalize()).all():
            raise ValueError('Daily dates must be timezone-naive market dates at midnight')
        import re
        if not all(re.fullmatch(r'\d{6}\.(SH|SZ)',str(c)) for c in self.codes):
            raise ValueError('Only normalized six-digit SH/SZ stock codes are supported')
        for k, df in self.frames.items():
            if not df.index.equals(first.index) or not df.columns.equals(first.columns):
                raise ValueError(f'Misaligned matrix: {k}')
            if not all(pd.api.types.is_numeric_dtype(t) for t in df.dtypes):
                raise ValueError(f'Non-numeric matrix: {k}')
            a = df.to_numpy()
            if np.isinf(a).any() or (a < 0).any():
                raise ValueError(f'Negative/infinite values: {k}')
            if k in ('open', 'high', 'low', 'close', 'cumadj') and (a == 0).any():
                raise ValueError(f'Zero price/factor: {k}')
        o,h,l,c = (self.frames[k] for k in ('open','high','low','close'))
        if ((h < o) | (h < c) | (l > o) | (l > c) | (h < l)).to_numpy().any():
            raise ValueError('Invalid OHLC bounds')
        self.source = str(source)
        self.source_hash = source_hash
        self.metadata = dict(metadata or {})

    @classmethod
    def load(cls, path):
        try:
            import pyarrow.feather as feather
        except ImportError as exc:
            # Workspace-only fallback; does not install or change global packages.
            import sys
            local = Path(__file__).resolve().parents[1] / '.analysis_deps'
            if not local.is_dir():
                raise ImportError('Install pyarrow with pip install -e .') from exc
            sys.path.insert(0,str(local))
            import pyarrow.feather as feather
        path = Path(path)
        frames = {}
        metadata = {}
        if path.suffix == '.zip':
            with zipfile.ZipFile(path) as z:
                for name in FIELDS:
                    frames[name] = feather.read_feather(io.BytesIO(z.read(f'feathers/{name}.feather'))).set_index('date')
                if 'feathers/volume.feather' in z.namelist():
                    frames['volume']=feather.read_feather(io.BytesIO(z.read('feathers/volume.feather'))).set_index('date')
                if 'feathers/metadata.json' in z.namelist(): metadata=json.loads(z.read('feathers/metadata.json'))
            with path.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(f.read()).hexdigest()
        else:
            hashes = {}
            for name in FIELDS:
                file = path / f'{name}.feather'
                frames[name] = feather.read_feather(file).set_index('date')
                hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
            for name in ('volume.feather','metadata.json'):
                file=path/name
                if file.exists():
                    hashes[name]=hashlib.sha256(file.read_bytes()).hexdigest()
                    if name.endswith('.feather'): frames['volume']=feather.read_feather(file).set_index('date')
                    else: metadata=json.loads(file.read_text(encoding='utf-8'))
            digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        return cls(frames, path, digest, metadata)

    def through(self, end):
        # Slice BEFORE expression evaluation, including before quality cut-off selection.
        end = pd.Timestamp(end)
        return MarketData({k: df.loc[:end] for k, df in self.frames.items()}, self.source, self.source_hash,self.metadata)

    def universe(self, settings, status=None, tradable=None):
        f = self.frames
        price_ok = f['close'].notna() & f['cumadj'].notna()
        # Observed history is not asserted to be legal listing age.
        age = f['close'].notna().cumsum()
        active = (f['amt'] > 0).rolling(settings.active_window, min_periods=settings.active_window).sum()
        adv = f['amt'].rolling(settings.liquidity_window, min_periods=settings.liquidity_window).mean()
        board = pd.Series(True, index=self.codes)
        if settings.universe == 'mainboard':
            board = pd.Series([str(x).startswith(('000','001','002','003','600','601','603','605')) for x in self.codes], index=self.codes)
        eligible = price_ok & (age >= settings.min_history) & (active >= settings.active_window)
        eligible &= price_ok.rolling(settings.valid_price_window,min_periods=settings.valid_price_window).sum().eq(settings.valid_price_window)
        eligible &= f['close'].ge(settings.min_price) & (adv*settings.amount_multiplier).ge(settings.min_avg_amount)
        eligible &= board
        if settings.require_trade_counts: eligible &= f['num_trades'].gt(0)
        if tradable is not None: eligible &= tradable.eq(1)
        if status is not None:
            if settings.unknown_st == 'error' and (eligible & status.isna()).to_numpy().any():
                raise ValueError('Unknown historical ST status in candidate universe; provide point-in-time history')
            if settings.unknown_st in ('exclude','error'): eligible &= status.notna()
            if settings.st_policy == 'exclude': eligible &= status.ne(1)
        # Higher ranks are more liquid; rank within today's already-eligible universe.
        liq_rank = adv.where(eligible).rank(axis=1, pct=True)
        eligible &= liq_rank >= settings.liquidity_quantile
        return eligible, adv

    def quality(self):
        c = self.frames['close']
        return {
            'start': str(self.dates[0].date()), 'end': str(self.dates[-1].date()),
            'dates': len(self.dates), 'securities': len(self.codes),
            'zero_amount_with_price': int(((self.frames['amt'] == 0) & c.notna()).to_numpy().sum()),
            'missing_factor_with_price': int((self.frames['cumadj'].isna() & c.notna()).to_numpy().sum()),
            'missing_trades_with_price': int((self.frames['num_trades'].isna() & c.notna()).to_numpy().sum()),
            'all_missing_trade_dates': [str(d.date()) for d in self.dates[self.frames['num_trades'].isna().all(axis=1)]],
            'has_share_volume': 'volume' in self.frames,
            'provenance': self.metadata,
        }

    def export(self,path):
        """Write a NEW normalized bundle, never overwrite a source directory."""
        path=Path(path)
        path.mkdir(parents=True,exist_ok=False)
        try:
            import pyarrow.feather as feather
        except ImportError:
            import sys
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'.analysis_deps'))
            import pyarrow.feather as feather
        for name,frame in self.frames.items():
            feather.write_feather(frame.rename_axis('date').reset_index(),path/f'{name}.feather')
        meta={**self.metadata,'upstream_source_sha256':self.source_hash}
        (path/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        return path
