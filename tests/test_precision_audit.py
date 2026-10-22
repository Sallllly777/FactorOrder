"""Regression cases discovered while comparing the independent 5DR report."""
import tempfile
from pathlib import Path
import unittest
from test_system import fixture,settings,fields,ROOT
import numpy as np
import pandas as pd
from pkuquant import Research,SecurityHistory
from pkuquant.engine import Broker,TradingStates,price_bounds,money
from pkuquant.factors import ts_return
from pkuquant.reference import ReferenceSnapshot


class PrecisionAuditTests(unittest.TestCase):
    def test_gap_does_not_resize_overnight_plan(self):
        d=fixture(5)
        s=settings(top_n=1,retain_n=1,max_weight=.4)
        a=Research(d).run('rank(amt)',s)
        d.frames['open'].iloc[1,0]=10.2
        b=Research(d).run('rank(amt)',s)
        first=lambda r:r.orders[(r.orders.date==d.dates[1])&(r.orders.code==d.codes[0])].iloc[0]
        self.assertEqual(first(a).requested_shares_equiv,4000)
        self.assertEqual(first(a).requested_shares_equiv,first(b).requested_shares_equiv)
        self.assertNotEqual(first(a).price,first(b).price)

    def test_execution_st_rejects_without_backfilling_another_name(self):
        d=fixture(5)
        h=pd.DataFrame([dict(code=c,effective_date=d.dates[0],known_at=d.dates[0],is_st=0) for c in d.codes])
        h.loc[len(h)]=[d.codes[0],d.dates[1],d.dates[1],1]
        r=Research(d,security_history=h).run('rank(amt)',settings(top_n=1,retain_n=1,max_weight=.5))
        targets=r.targets[r.targets.date==d.dates[1]]
        self.assertEqual(targets.code.tolist(),[d.codes[0]])
        self.assertEqual(r.daily.loc[d.dates[1],'invested'],0)

    def test_capacity_uses_prior_information_not_positive_full_day_amount(self):
        d=fixture(5); s=settings(amount_participation=.001)
        b=Broker(d,s,d.frames['amt']); b.start_day(1)
        b.a['amt'][1,0]=100
        order=b.trade(0,1000,d.dates[0])
        self.assertEqual(order['filled_shares_equiv'],1000)
        b.a['amt'][1,1]=0
        self.assertEqual(b.trade(1,1000,d.dates[0])['reason'],'no_trading_amount')

    def test_capacity_is_shared_in_shares_when_fill_prices_change(self):
        d=fixture(5); b=Broker(d,settings(amount_participation=.00002),d.frames['amt']); b.start_day(1)
        self.assertEqual(b.trade(0,1000,d.dates[0])['filled_shares_equiv'],200)
        b.a['open'][1,0]=9.9
        self.assertEqual(b.trade(0,1000,d.dates[0])['reason'],'amount_cap')

    def test_slippage_exactly_at_adverse_limit_is_rejected_both_sides(self):
        for side,op,up,lo in [('buy',10.,10.01,9.),('sell',10.,11.,9.99)]:
            d=fixture(5); b=Broker(d,settings(slippage_bps=1),d.frames['amt']); b.start_day(1)
            b.states=TradingStates(pd.DataFrame([dict(date=d.dates[1],code=d.codes[0],tradable=1,no_limit=0,upper_limit=up,lower_limit=lo,lot_size=100,min_buy_qty=100)]))
            if side=='sell': b.units[0]=b.sellable[0]=200
            r=b.trade(0,100 if side=='buy' else -100,d.dates[0])
            self.assertEqual(r['reason'],'slipped_price_at_adverse_limit')

    def test_star_partial_sell_minimum_after_capacity(self):
        d=fixture(5,codes=('688001.SH',)); b=Broker(d,settings(amount_participation=.000015),d.frames['amt']); b.start_day(1)
        b.units[0]=b.sellable[0]=300
        self.assertEqual(b.trade(0,-300,d.dates[0])['reason'],'below_minimum_sell')
        b.units[0]=b.sellable[0]=150
        self.assertEqual(b.trade(0,-150,d.dates[0])['filled_shares_equiv'],150)

    def test_adjustment_day_cancels_frozen_quantity(self):
        d=fixture(5); b=Broker(d,settings(),d.frames['amt']); b.start_day(1)
        b.a['cumadj'][1,0]=2
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'corporate_action_cancel_replan')
        self.assertEqual(b.cash,100000)

    def test_small_factor_noise_does_not_cancel_a_valid_plan(self):
        d=fixture(5); b=Broker(d,settings(),d.frames['amt']); b.start_day(1)
        b.a['cumadj'][1,0]=1.000002
        self.assertEqual(b.trade(0,100,d.dates[0])['status'],'filled')

    def test_cash_only_warmup_is_counted_in_performance(self):
        d=fixture(12); r=Research(d).run('rank(ts_return(close,5))',settings())
        self.assertEqual(r.daily.index[0],d.dates[0])
        np.testing.assert_allclose(r.daily.nav.iloc[:6],100000)
        self.assertEqual(r.metrics['trading_days'],12)

    def test_return_matches_direct_continuous_ratio_including_ties(self):
        d=fixture(6); d.frames['cumadj'].iloc[:,:]=1.1247
        base=d.frames['close']*d.frames['cumadj']
        pd.testing.assert_frame_equal(ts_return(fields(d).close,2).values,base/base.shift(2)-1)

    def test_absolute_liquidity_price_and_cash_reserve(self):
        d=fixture(5); d.frames['amt'].iloc[:,0]=100
        s=settings(min_avg_amount=1000,min_price=9,gross_exposure=.6)
        eligible,_=d.universe(s)
        self.assertFalse(eligible.iloc[-1,0])
        r=Research(d).run('rank(amt)',s)
        self.assertTrue(r.targets.target_weight.eq(.3).all())

    def test_decimal_half_up(self):
        self.assertEqual(money(1.005),1.01)
        self.assertEqual(price_bounds(10.05,.1),(9.05,11.06))

    def test_invalid_open_cannot_be_repaired_by_slippage(self):
        d=fixture(5); b=Broker(d,settings(slippage_bps=20),d.frames['amt']); b.start_day(1)
        b.units[0]=b.sellable[0]=200
        b.a['open'][1,0]=10.51  # above estimated 10.50 bound; sell slip falls inside
        self.assertEqual(b.trade(0,-100,d.dates[0])['reason'],'open_outside_rule_bounds')

    def test_known_non_st_not_forced_into_unknown_st_five_percent_proxy(self):
        rule=TradingStates()
        self.assertEqual(rule.get(pd.Timestamp('2025-01-02'),'600000.SH',10,0)['upper_limit'],11)
        self.assertEqual(rule.get(pd.Timestamp('2025-01-02'),'600000.SH',10,1)['upper_limit'],10.5)
        self.assertEqual(rule.get(pd.Timestamp('2025-01-02'),'600000.SH',10,np.nan)['upper_limit'],10.5)

    def test_expired_daily_status_does_not_fill_missing_date(self):
        d=fixture(5)
        h=pd.DataFrame([dict(code=d.codes[0],effective_date=d.dates[0],known_at=d.dates[0],valid_until=d.dates[1],is_st=0)])
        out=SecurityHistory(h).matrix(d.dates,d.codes)
        self.assertEqual(out.iloc[1,0],0)
        self.assertTrue(np.isnan(out.iloc[2,0]))

    def test_reference_cache_mismatch_missing_and_reconstructed_provenance(self):
        d=fixture(5); tmp=ROOT/'tests'/'.tmp'; tmp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp) as directory:
            f=pd.DataFrame(dict(date=d.dates,code='sz.000001',close=[10,10,12,10,10],preclose=10,isST=0,tradestatus=1)).drop(index=3)
            f.to_csv(Path(directory)/'000001.SZ.csv.gz',index=False,compression='gzip')
            ref=ReferenceSnapshot(d,directory)
            state=ref.security.matrix(d.dates,d.codes)
            self.assertTrue(np.isnan(state.iloc[2,0]))
            self.assertTrue(np.isnan(state.iloc[3,0]))
            self.assertEqual(state.iloc[4,0],0)
            self.assertEqual(ref.audit['mismatched_cells'],1)
            self.assertEqual(ref.get(d.dates[1],d.codes[0],10)['source'],'reconstructed')
            self.assertFalse(ref.get(d.dates[1],d.codes[1],10)['tradable'])
            r=Research(d,reference_cache=directory).run('rank(amt)',settings(unknown_st='exclude'))
            self.assertTrue((r.orders[r.orders.notional>0].code==d.codes[0]).all())
            with self.assertRaisesRegex(ValueError,'Missing verified'):
                Research(d,reference_cache=directory).run('rank(amt)',settings(require_verified_rules=True))


if __name__=='__main__': unittest.main()
