"""Conservative readiness checks. Passing data checks is not broker certification."""
import pandas as pd
from .security import status_coverage


def inspect_readiness(lab,settings):
    d=lab.data.through(settings.end) if settings.end else lab.data
    mask=d.frames['close'].notna()
    if settings.start: mask.loc[mask.index<pd.Timestamp(settings.start)]=False
    coverage=status_coverage(lab.security.matrix(d.dates,d.codes,at='09:25:00'),mask)
    supplied=sum(bool(mask.at[date,code]) for (date,code),rule in lab.states.rows.items() if rule.get('source')=='provided' and date in mask.index and code in mask.columns)
    required=int(mask.to_numpy().sum())
    checks={
        'non_synthetic_dataset':not bool(d.metadata.get('synthetic',False)),
        'historical_st_complete':coverage['unknown_cells']==0 and required>0,
        'daily_rules_complete':supplied==required and required>0,
        'amount_units_explicit':bool(d.metadata.get('units_explicit')),
        'share_volume_present':'volume' in d.frames,
        'trade_counts_present':not bool((mask & d.frames['num_trades'].isna()).to_numpy().any()),
        'adjustment_factors_present':not bool((mask & d.frames['cumadj'].isna()).to_numpy().any()),
        'calendar_verified':bool(d.metadata.get('calendar_verified',False)),
    }
    blockers=[k for k,v in checks.items() if not v and (k!='trade_counts_present' or settings.require_trade_counts)]
    return dict(checks=checks,st_coverage=coverage,rule_cells=supplied,required_rule_cells=required,
                verified_data_ready=not blockers,blockers=blockers,
                live_trading_ready=False,live_blockers=[
                    'No real corporate-action cash/share entitlement ledger',
                    'No order gateway, broker reconciliation or live risk controls',
                    'Daily bars do not establish opening liquidity or limit-order queue fills'],
                note='Source declarations and coverage checks are not independent vendor certification; absent bars require an external trading calendar/security master.')
