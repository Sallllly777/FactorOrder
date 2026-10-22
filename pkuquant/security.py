"""Bitemporal ST history: effective time AND time the information was known.

An absent history is UNKNOWN, never automatically treated as a normal stock.
"""
import hashlib
import numpy as np
import pandas as pd


def local_time(value):
    t = pd.Timestamp(value)
    if pd.isna(t): raise ValueError('Missing timestamp')
    return t.tz_convert('Asia/Shanghai').tz_localize(None) if t.tzinfo else t


class SecurityHistory:
    def __init__(self, frame=None):
        self.frame = pd.DataFrame(columns=['code','effective_date','known_at','is_st'])
        if frame is None:
            self.sha256 = None
            return
        f = frame.copy()
        required = {'code','effective_date','known_at','is_st'}
        if required-set(f.columns): raise ValueError(f'Missing security-history columns: {sorted(required-set(f.columns))}')
        f['code'] = f.code.astype(str)
        if not f.code.str.fullmatch(r'\d{6}\.(SH|SZ)').all(): raise ValueError('Security history needs normalized SH/SZ codes')
        f['effective_date'] = pd.to_datetime(f.effective_date.map(local_time))
        f['known_at'] = pd.to_datetime(f.known_at.map(local_time))
        if 'valid_until' in f:
            f['valid_until']=pd.to_datetime(f.valid_until.map(local_time))
            if (f.valid_until<f.effective_date).any() or (f.valid_until!=f.valid_until.dt.normalize()).any():
                raise ValueError('valid_until must be a market date >= effective_date')
        if (f.effective_date != f.effective_date.dt.normalize()).any():
            raise ValueError('effective_date must be a trading date at midnight')
        if not f.is_st.isin([0,1,False,True]).all(): raise ValueError('is_st must be 0/1, covering ST and *ST')
        if f.duplicated(['code','effective_date','known_at']).any(): raise ValueError('Duplicate security-history revision')
        self.frame = f.sort_values(['code','effective_date','known_at']).reset_index(drop=True)
        self.sha256 = hashlib.sha256(self.frame.to_csv(index=False).encode()).hexdigest()

    def matrix(self, dates, codes, at='15:00:00'):
        out = np.full((len(dates),len(codes)),np.nan,dtype=np.float32)
        positions = {str(c):i for i,c in enumerate(codes)}
        asof = pd.DatetimeIndex(dates) + pd.Timedelta(at)
        # Sorted effective date ensures a late revision of an old interval never
        # overwrites a newer effective status. Same-date revisions use known_at.
        for r in self.frame.itertuples():
            if r.code not in positions: continue
            applicable = (dates >= r.effective_date) & (asof >= r.known_at)
            if hasattr(r,'valid_until'): applicable &= dates<=r.valid_until
            out[applicable,positions[r.code]] = float(r.is_st)
        return pd.DataFrame(out,index=dates,columns=codes)


def status_coverage(status, mask):
    n = int(mask.to_numpy().sum())
    unknown = int((mask & status.isna()).to_numpy().sum())
    return {'required_cells':n,'unknown_cells':unknown,
            'known_st_cells':int((mask & status.eq(1)).to_numpy().sum()),
            'coverage':float((n-unknown)/n) if n else None}
