"""Daily, long-only, cash-constrained simulation with auditable order outcomes.

Accounting is explicitly TOTAL-RETURN UNITS, not a corporate-action cash ledger.
Units = raw-equivalent shares / cumulative adjustment factor; their mark is
raw_price * factor. Dividend reinvestment is implicit; actual dividend cash dates,
tax and stock entitlement timing require a future corporate-action adapter.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import numpy as np
import pandas as pd


ORDER_COLUMNS = ['date','signal_date','code','side','requested_shares_equiv','filled_shares_equiv','price','notional','commission','stamp_tax','transfer_fee','status','reason','rule_source','is_st','decision_date','sizing_reference_close','lagged_adv','capacity_shares','upper_limit','lower_limit','cash_after']
FACTOR_EVENT_TOLERANCE = 1e-5  # supplied factors contain ~1e-6 historical quantization noise


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP))


def price_bounds(reference, rate):
    ref, r = Decimal(str(reference)), Decimal(str(rate))
    return tuple(float((ref*v).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) for v in (1-r,1+r))


class TradingStates:
    """Optional externally validated daily rules; unprovided rows use labeled estimates.

    columns: date, code, tradable (0/1), upper_limit, lower_limit,
    no_limit (0/1), lot_size, min_buy_qty. Presence doesn't prove queue fills.
    """
    def __init__(self, frame=None):
        self.rows = {}
        if frame is None: return
        f = frame.copy()
        required = {'date','code','tradable','upper_limit','lower_limit','no_limit','lot_size','min_buy_qty'}
        if required-set(f.columns): raise ValueError(f'Missing trading-state columns: {sorted(required-set(f.columns))}')
        f['date'] = pd.to_datetime(f['date'])
        if f.date.isna().any() or f.date.dt.tz is not None or (f.date!=f.date.dt.normalize()).any():
            raise ValueError('Trading state dates must be naive market dates')
        if f.duplicated(['date','code']).any(): raise ValueError('Duplicate trading state')
        for r in f.to_dict('records'):
            r['source']=r.get('source','provided')
            if r['source'] not in ('provided','reconstructed'):
                raise ValueError('Rule source must be provided or reconstructed')
            r['price_tick']=r.get('price_tick',.01)
            if not np.isfinite(r['price_tick']) or r['price_tick']<=0: raise ValueError('Invalid price_tick')
            if r['tradable'] not in (0,1,False,True) or r['no_limit'] not in (0,1,False,True):
                raise ValueError('tradable/no_limit must be 0 or 1')
            for k in ('lot_size','min_buy_qty'):
                if not np.isfinite(r[k]) or r[k] < 1 or int(r[k]) != r[k]: raise ValueError(f'Invalid {k}')
            if not r['no_limit'] and not (np.isfinite(r['lower_limit']) and np.isfinite(r['upper_limit']) and 0 < r['lower_limit'] <= r['upper_limit']):
                raise ValueError('Invalid daily price limits')
            self.rows[(pd.Timestamp(r['date']),str(r['code']))] = r

    def get(self, date, code, reference, is_st=None):
        if (date,code) in self.rows:
            return self.rows[(date,code)].copy()
        star = code.startswith(('688','689'))
        growth = code.startswith(('300','301','302'))
        if star: rate = .20
        elif growth: rate = .20 if date >= pd.Timestamp('2020-08-24') else .10
        else:
            # Historical ST unknown: deliberately conservative 5% bound, NOT official limits.
            rate = .05 if is_st!=0 and date < pd.Timestamp('2026-07-06') else .10
        lower,upper = price_bounds(reference,rate) if np.isfinite(reference) else (np.nan,np.nan)
        return {
            'tradable': True, 'no_limit': False,
            'upper_limit': upper, 'lower_limit': lower,
            'lot_size': 1 if star else 100, 'min_buy_qty': 200 if star else 100,
            'source':'estimated_conservative',
        }


@dataclass
class Simulation:
    daily: pd.DataFrame
    orders: pd.DataFrame
    holdings: pd.DataFrame
    targets: pd.DataFrame


class Broker:
    def __init__(self, data, settings, adv, states=None, status=None):
        self.data, self.s, self.adv = data, settings, adv.to_numpy()
        self.a = {k: v.to_numpy() for k,v in data.frames.items()}
        self.states = states or TradingStates()
        self.status = status.to_numpy() if status is not None else np.full(data.frames['close'].shape,np.nan)
        self.adv_volume = data.frames['volume'].rolling(settings.liquidity_window,min_periods=settings.liquidity_window).mean().to_numpy() if 'volume' in self.a else None
        n = len(data.codes)
        self.units = np.zeros(n)
        self.sellable = np.zeros(n)
        self.cash = float(settings.initial_cash)
        self.used_notional = np.zeros(n)
        self.used_shares = np.zeros(n)
        self.orders = []
        self.last_mark = np.zeros(n)
        self.stale = np.zeros(n,dtype=int)
        self.i = 0

    def start_day(self,i):
        self.i = i
        self.sellable = self.units.copy()
        self.used_notional[:] = 0
        self.used_shares[:] = 0

    def mark(self,i):
        raw = self.a['close'][i]*self.a['cumadj'][i]
        valid = np.isfinite(raw) & (raw > 0)
        self.last_mark[valid] = raw[valid]
        traded = valid & (self.a['amt'][i] > 0)
        self.stale = np.where(traded,0,self.stale+1)
        mark = self.last_mark.copy()
        mark[self.stale >= self.s.stale_haircut_after] *= 1-self.s.stale_haircut
        return mark

    def trade(self,j,delta_units,signal_date):
        i,s,a = self.i,self.s,self.a
        code,date = str(self.data.codes[j]),self.data.dates[i]
        side = 'buy' if delta_units > 0 else 'sell'
        factor = a['cumadj'][i,j]
        sizing_factor = a['cumadj'][i-1,j] if s.sizing=='previous_close' and i else factor
        qty = abs(delta_units)*sizing_factor if np.isfinite(sizing_factor) else np.nan
        order = dict(zip(ORDER_COLUMNS,[None]*len(ORDER_COLUMNS)))
        order.update(date=date,signal_date=signal_date,code=code,side=side,requested_shares_equiv=qty,
                     filled_shares_equiv=0.0,price=np.nan,notional=0.0,commission=0.0,stamp_tax=0.0,transfer_fee=0.0,
                     status='rejected',reason='',rule_source='unavailable',is_st=self.status[i,j],
                     decision_date=self.data.dates[i-1] if i else date,
                     sizing_reference_close=a['close'][i-1,j] if i else np.nan,
                     lagged_adv=self.adv[i-1,j] if i else np.nan,capacity_shares=0.,cash_after=self.cash)
        def reject(reason):
            order['reason'] = reason
            self.orders.append(order)
            return order
        if side=='buy':
            if s.st_policy=='exclude' and self.status[i,j]==1: return reject('st_buy_blocked')
            if s.unknown_st!='allow' and not np.isfinite(self.status[i,j]): return reject('unknown_st_status')
        o,h,l = (a[k][i,j] for k in ('open','high','low'))
        if not all(np.isfinite(v) and v > 0 for v in (o,h,l,factor)):
            return reject('missing_price_or_factor')
        if not np.isfinite(a['amt'][i,j]) or a['amt'][i,j] <= 0:
            return reject('no_trading_amount')
        if s.require_trade_counts and (not np.isfinite(a['num_trades'][i,j]) or a['num_trades'][i,j] <= 0):
            return reject('missing_or_zero_trade_count')
        if s.sizing=='previous_close' and (not np.isfinite(sizing_factor) or abs(factor/sizing_factor-1)>FACTOR_EVENT_TOLERANCE):
            return reject('corporate_action_cancel_replan')
        reference = a['close'][i-1,j]*a['cumadj'][i-1,j]/factor if i else np.nan
        rule = self.states.get(date,code,reference,self.status[i,j])
        order['rule_source'] = rule['source']
        order.update(upper_limit=rule['upper_limit'],lower_limit=rule['lower_limit'])
        if s.require_verified_rules and rule['source']!='provided': return reject('missing_verified_rules')
        if not rule['tradable']: return reject('not_tradable')
        if h == l: return reject('flat_bar_uncertain')
        if not rule['no_limit']:
            up,down = rule['upper_limit'],rule['lower_limit']
            if not np.isfinite(up+down): return reject('unknown_reference_price')
            if o<down-1e-8 or o>up+1e-8: return reject('open_outside_rule_bounds')
            if side == 'buy' and o >= up-1e-8: return reject('limit_up_or_conservative_bound')
            if side == 'sell' and o <= down+1e-8: return reject('limit_down_or_conservative_bound')
        price = o*(1+s.slippage_bps/10000*(1 if side=='buy' else -1))
        # Tick round against trader; never clamp an impossible fill to legal bounds.
        tick=rule.get('price_tick',.01)
        price = (np.ceil(price/tick-1e-9) if side=='buy' else np.floor(price/tick+1e-9))*tick
        if price < l-1e-8 or price > h+1e-8: return reject('slipped_price_outside_bar')
        if not rule['no_limit'] and not rule['lower_limit']-1e-8 <= price <= rule['upper_limit']+1e-8:
            return reject('slipped_price_outside_limits')
        if not rule['no_limit'] and ((side=='buy' and price>=rule['upper_limit']-1e-8) or (side=='sell' and price<=rule['lower_limit']+1e-8)):
            return reject('slipped_price_at_adverse_limit')
        order['price'] = price
        lot,minbuy = int(rule['lot_size']),int(rule['min_buy_qty'])
        reason = ''
        if side == 'buy':
            requested = np.floor((qty+1e-8)/lot)*lot
            if requested < minbuy: return reject('below_minimum_order')
        else:
            available = self.sellable[j]*factor
            if available <= 1e-8: return reject('t_plus_one')
            requested = min(qty,available)
            if requested < qty-1e-7: reason='t_plus_one_partial'
            # Full liquidation permits residual shares; these are share EQUIVALENTS in research accounting.
            if requested < self.units[j]*factor-1e-7:
                requested = np.floor((requested+1e-8)/lot)*lot
        if requested <= 1e-8: return reject('below_minimum_order')
        previous_adv = self.adv[i-1,j] if i else np.nan
        if not np.isfinite(previous_adv) or previous_adv <= 0: return reject('missing_lagged_liquidity')
        if s.capacity=='lagged_adv':
            # Fix share capacity using only information available at prior close.
            reference_close=a['close'][i-1,j] if i else np.nan
            if not np.isfinite(reference_close) or reference_close<=0: return reject('missing_capacity_reference')
            cap = max(0,np.floor((previous_adv*s.amount_multiplier*s.amount_participation/reference_close-self.used_shares[j])/lot)*lot)
        else:
            budget = min(previous_adv,a['amt'][i,j])*s.amount_multiplier*s.amount_participation-self.used_notional[j]
            cap = max(0,np.floor(budget/price/lot)*lot)
        order['capacity_shares']=float(cap)
        filled = min(requested,cap)
        if filled < requested-1e-8: reason='amount_cap'
        if filled <= 1e-8: return reject('amount_cap')
        if self.adv_volume is not None:
            lagged=self.adv_volume[i-1,j] if i else np.nan
            current=a['volume'][i,j] if s.capacity=='daily_min' else lagged
            if not np.isfinite(lagged+current) or min(lagged,current)<=0: return reject('missing_share_volume')
            vcap=max(0,np.floor((min(lagged,current)*s.volume_participation-self.used_shares[j])/lot)*lot)
            if filled>vcap: filled,reason=vcap,'volume_cap'
            if filled<=1e-8: return reject('volume_cap')
        stamp_rate = (0.0005 if date >= pd.Timestamp('2023-08-28') else 0.001) if s.stamp_tax and side=='sell' else 0.0
        def fees(q):
            n = q*price
            rounded=money if s.round_fees else float
            return rounded(max(s.min_commission,rounded(n*s.commission))), rounded(n*stamp_rate), rounded(n*s.transfer_rate)
        if side == 'buy':
            # Both branches of max(min_commission, notional*rate) must be affordable.
            affordable=min((self.cash-s.min_commission)/(1+s.transfer_rate),self.cash/(1+s.commission+s.transfer_rate))
            cash_cap = max(0,np.floor(affordable/price/lot)*lot)
            if cash_cap < filled:
                filled = cash_cap
                reason = 'insufficient_cash_partial'
            if filled < minbuy: return reject('insufficient_cash_or_capacity')
        elif lot==1 and minbuy>1 and filled<minbuy and filled<self.units[j]*factor-1e-7:
            return reject('below_minimum_sell')
        commission,tax,transfer = fees(filled)
        notional = filled*price
        if side == 'buy':
            if notional+commission+tax+transfer > self.cash+1e-7: return reject('insufficient_cash')
            self.cash -= notional+commission+tax+transfer
            self.units[j] += filled/factor
        else:
            # Tiny positions can have fees larger than proceeds: never create unauthorized debt.
            if self.cash+notional < commission+tax+transfer: return reject('insufficient_cash_for_sell_fees')
            units = min(self.units[j],filled/factor)
            self.units[j] -= units
            self.sellable[j] -= units
            self.cash += notional-commission-tax-transfer
            if self.units[j] < 1e-10: self.units[j]=0
        self.used_notional[j] += notional
        self.used_shares[j] += filled
        full = filled >= requested-1e-7 and reason not in ('t_plus_one_partial',)
        order.update(filled_shares_equiv=float(filled),notional=float(notional),commission=float(commission),
                     stamp_tax=float(tax),transfer_fee=float(transfer),status='filled' if full else 'partial',
                     reason='filled' if full else reason,cash_after=float(self.cash))
        self.orders.append(order)
        if self.cash < -1e-7 or (self.units < -1e-9).any(): raise AssertionError('Account conservation failed')
        return order


def _scheduled(dates,i,frequency):
    if frequency == 'daily' or i == 0: return True
    a,b = dates[i-1],dates[i]
    return a.isocalendar()[:2] != b.isocalendar()[:2] if frequency == 'weekly' else (a.year,a.month)!=(b.year,b.month)


def simulate(data,scores,eligible,adv,settings,states=None,status=None):
    b = Broker(data,settings,adv,states,status)
    scores,eligible = scores.to_numpy(),eligible.to_numpy()
    daily,holdings,targets = [],[],[]
    dates = data.dates
    start = pd.Timestamp(settings.start) if settings.start else dates[0]
    previous_nav = settings.initial_cash
    begun = False
    for i,date in enumerate(dates):
        b.start_day(i)
        count_before = len(b.orders)
        if date < start:
            b.mark(i)
            continue
        signal_i = i-settings.delay
        ok = eligible[signal_i] & np.isfinite(scores[signal_i]) if signal_i>=0 else np.zeros(len(data.codes),dtype=bool)
        # Execution-day state may reject a planned buy, never replace its name
        # using information unavailable when the preceding close plan was made.
        if signal_i>=0 and ((_scheduled(dates,i,settings.rebalance) and begun) or (not begun and ok.any())):
            order = np.lexsort((np.asarray(data.codes,dtype=str),-np.where(ok,scores[signal_i],-np.inf)))
            ranked = [int(j) for j in order if ok[j]]
            retain = set(ranked[:settings.retain_n])
            chosen = [j for j in ranked if j in retain and b.units[j]>1e-10][:settings.top_n]
            for j in ranked:
                if len(chosen) >= settings.top_n: break
                if j not in chosen: chosen.append(j)
            # Fixed slot weights leave cash when fewer candidates exist.
            weight = min(settings.gross_exposure/settings.top_n,settings.max_weight)
            desired = np.zeros(len(data.codes))
            for j in chosen:
                sizing_i = i-1 if settings.sizing=='previous_close' else i
                field = 'close' if settings.sizing=='previous_close' else 'open'
                current_price = b.a[field][sizing_i,j]*b.a['cumadj'][sizing_i,j]
                # Missing execution price retains existing units; issue explicit rejected entry below.
                desired[j] = previous_nav*weight/current_price if np.isfinite(current_price) and current_price>0 else b.units[j]
                targets.append(dict(date=date,signal_date=dates[signal_i],decision_date=dates[i-1],code=str(data.codes[j]),score=float(scores[signal_i,j]),target_weight=weight,target_economic_units=float(desired[j]),sizing=settings.sizing))
                if not np.isfinite(current_price) and b.units[j] == 0:
                    b.trade(j,1.0,dates[signal_i])
            if settings.st_policy=='exclude' and settings.liquidate_st:
                desired[b.status[i]==1]=0  # execution risk cancellation/exit, no replacement buy
            delta = desired-b.units
            # Sells must actually release cash before any buys are sized/funded.
            for j in np.flatnonzero(delta < -1e-10): b.trade(j,delta[j],dates[signal_i])
            for j in sorted(np.flatnonzero(delta > 1e-10),key=lambda j: (-scores[signal_i,j],str(data.codes[j]))):
                b.trade(j,delta[j],dates[signal_i])
            begun = True
        elif signal_i>=0 and settings.st_policy=='exclude' and settings.liquidate_st:
            # A newly effective ST flag is a daily risk exit, even between rebalances.
            # A failed sell remains held and is retried on the next trading day.
            for j in np.flatnonzero((b.status[i]==1)&(b.units>1e-10)):
                b.trade(j,-b.units[j],dates[signal_i])
        mark = b.mark(i)
        values = b.units*mark
        nav = b.cash+values.sum()
        today = b.orders[count_before:]
        costs = sum(r['commission']+r['stamp_tax']+r['transfer_fee'] for r in today)
        traded = sum(r['notional'] for r in today)
        daily.append(dict(date=date,nav=float(nav),cash=float(b.cash),invested=float(values.sum()),
                          return_=float(nav/previous_nav-1),costs=float(costs),turnover=float(traded/previous_nav),
                          holdings=int((b.units>1e-10).sum()),stale_exposure=float(values[b.stale>0].sum()),
                          rejected=sum(r['status']=='rejected' for r in today),partial=sum(r['status']=='partial' for r in today)))
        for j in np.flatnonzero(b.units>1e-10):
            holdings.append(dict(date=date,code=str(data.codes[j]),economic_units=float(b.units[j]),
                                 shares_equiv=float(b.units[j]*b.a['cumadj'][i,j]) if np.isfinite(b.a['cumadj'][i,j]) else np.nan,
                                 market_value=float(values[j]),weight=float(values[j]/nav) if nav>0 else np.nan,
                                 stale_days=int(b.stale[j])))
        previous_nav = nav
        if nav <= 0: raise ValueError('Portfolio equity exhausted')
    if not daily: raise ValueError('No eligible backtest days; shorten warmup or check coverage/universe settings')
    return Simulation(pd.DataFrame(daily).set_index('date').rename(columns={'return_':'return'}),
                      pd.DataFrame(b.orders,columns=ORDER_COLUMNS),pd.DataFrame(holdings,columns=['date','code','economic_units','shares_equiv','market_value','weight','stale_days']),pd.DataFrame(targets))
