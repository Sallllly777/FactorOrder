"""Ex-post labels live ONLY here, never in the strategy Fields namespace."""
import numpy as np
import pandas as pd


def factor_diagnostics(score,data,start,end,horizons=(1,5,20)):
    """Daily cross-sectional Spearman IC; close-to-close, not executable PnL.

    Missing future endpoints are excluded and counted; horizons overlap, so no
    independent-observation t-statistic is claimed.
    """
    price = data.frames['close']*data.frames['cumadj']
    result = pd.DataFrame(index=score.index)
    result['coverage'] = score.notna().sum(axis=1)
    for horizon in horizons:
        label = price.shift(-horizon)/price-1
        valid = score.notna() & label.notna() & np.isfinite(label)
        x = score.where(valid).rank(axis=1)
        y = label.where(valid).rank(axis=1)
        counts = valid.sum(axis=1)
        # Pearson on ranks, guarding constant cross-sections explicitly.
        xc = x.sub(x.mean(axis=1),axis=0)
        yc = y.sub(y.mean(axis=1),axis=0)
        denom = np.sqrt(xc.pow(2).sum(axis=1)*yc.pow(2).sum(axis=1)).replace(0,np.nan)
        result[f'rank_ic_{horizon}d'] = (xc*yc).sum(axis=1).div(denom).where(counts>=3)
        result[f'label_count_{horizon}d'] = counts
    return result.loc[start:end]
