from dataclasses import dataclass, asdict
import math


@dataclass(frozen=True)
class Settings:
    start: str | None = None
    end: str | None = None
    initial_cash: float = 1_000_000
    universe: str = 'mainboard'
    min_history: int = 252
    active_window: int = 20
    liquidity_window: int = 20
    liquidity_quantile: float = 0.5
    top_n: int = 50
    retain_n: int = 75
    max_weight: float = 0.03
    rebalance: str = 'weekly'
    delay: int = 1
    decay: int = 1
    commission: float = 0.0003
    min_commission: float = 5.0
    slippage_bps: float = 10.0
    transfer_rate: float = 0.0
    stamp_tax: bool = True
    amount_participation: float = 0.001
    amount_multiplier: float = 1.0
    stale_haircut_after: int = 20
    stale_haircut: float = 0.0
    require_trade_counts: bool = True
    annualization: int = 252
    execution: str = 'conservative_daily'
    st_policy: str = 'exclude'
    unknown_st: str = 'allow'
    liquidate_st: bool = True
    require_verified_rules: bool = False
    volume_participation: float = 0.01
    sizing: str = 'previous_close'
    capacity: str = 'lagged_adv'
    gross_exposure: float = 1.0
    min_price: float = 0.0
    min_avg_amount: float = 0.0
    valid_price_window: int = 1
    round_fees: bool = True

    def __post_init__(self):
        for name in ('min_history','active_window','liquidity_window','top_n','retain_n','delay','decay','stale_haircut_after','annualization','valid_price_window'):
            x = getattr(self, name)
            if not isinstance(x, int) or isinstance(x, bool) or x < 1:
                raise ValueError(f'{name} must be a positive integer')
        for name in ('initial_cash','max_weight','amount_participation','amount_multiplier'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('commission','min_commission','slippage_bps','transfer_rate','min_price','min_avg_amount'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if not 0 <= self.liquidity_quantile < 1 or not 0 <= self.stale_haircut <= 1:
            raise ValueError('Invalid quantile/haircut')
        if self.max_weight > 1 or self.amount_participation > 1 or self.retain_n < self.top_n:
            raise ValueError('Invalid weight, participation or retention count')
        if self.universe not in ('mainboard','all') or self.rebalance not in ('daily','weekly','monthly'):
            raise ValueError('Unknown universe or rebalance')
        if self.execution != 'conservative_daily':
            raise ValueError('Only explicitly approximate conservative_daily execution is supported')
        if self.sizing not in ('previous_close','execution_open') or self.capacity not in ('lagged_adv','daily_min'):
            raise ValueError('Invalid sizing/capacity model')
        if not math.isfinite(self.gross_exposure) or not 0 < self.gross_exposure <= 1:
            raise ValueError('gross_exposure must be in (0,1]')
        if self.st_policy not in ('exclude','allow') or self.unknown_st not in ('allow','exclude','error'):
            raise ValueError('Invalid ST policy')
        if not math.isfinite(self.volume_participation) or not 0 < self.volume_participation <= 1:
            raise ValueError('volume_participation must be in (0,1]')
        for name in ('stamp_tax','require_trade_counts','liquidate_st','require_verified_rules','round_fees'):
            if type(getattr(self,name)) is not bool: raise ValueError(f'{name} must be boolean')
        import pandas as pd
        for name in ('start','end'):
            if getattr(self,name) is not None:
                value=pd.Timestamp(getattr(self,name))
                if pd.isna(value) or value.tzinfo: raise ValueError(f'{name} must be a valid naive market date')
        if self.start and self.end and pd.Timestamp(self.start)>pd.Timestamp(self.end):
            raise ValueError('start is later than end')

    def to_dict(self):
        return asdict(self)
