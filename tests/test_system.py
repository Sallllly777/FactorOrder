import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'.analysis_deps'))
import numpy as np
import pandas as pd
from pkuquant import Research, Settings, MarketData, rank, ts_delay
from pkuquant.factors import Expression, Fields, decay_linear, trade_when
from pkuquant.engine import Broker, TradingStates
from pkuquant.diagnostics import factor_diagnostics


def fixture(n=30,codes=('000001.SZ','600001.SH','300001.SZ')):
    dates = pd.bdate_range('2023-08-01',periods=n)
    price = pd.DataFrame(np.full((n,len(codes)),10.0),index=dates,columns=codes)
    frames = {k:price.copy() for k in ('open','high','low','close','amt','num_trades','cumadj')}
    frames['high'] += 1
    frames['low'] -= 1
    frames['amt'][:] = 1e8
    frames['num_trades'][:] = 10000
    frames['cumadj'][:] = 1
    return MarketData(frames)


def settings(**kw):
    d = dict(initial_cash=100000,min_history=1,active_window=1,liquidity_window=1,liquidity_quantile=0,
             top_n=2,retain_n=2,max_weight=.5,rebalance='daily',commission=0,min_commission=0,
             slippage_bps=0,stamp_tax=False,amount_participation=1,require_trade_counts=True)
    d.update(kw)
    return Settings(**d)


def fields(data):
    return Fields(data,pd.DataFrame(True,index=data.dates,columns=data.codes))


class FactorTests(unittest.TestCase):
    def test_rolling_is_forward_adjusted_at_each_date(self):
        d=fixture(5)
        for k in ('open','high','low','close'): d.frames[k].iloc[2:]/=2
        d.frames['cumadj'].iloc[2:]*=2
        f=fields(d)
        self.assertAlmostEqual(ts_delay(f.close,1).values.iloc[2,0],5)
        a,_=Expression('close / ts_delay(close, 1) - 1').evaluate(f)
        np.testing.assert_allclose(a.values.iloc[1:].to_numpy(),0)
        a,_=Expression('ts_mean(close, 3)').evaluate(f)
        self.assertAlmostEqual(a.values.iloc[2,0],5)

    def test_future_mutation_does_not_change_past(self):
        d=fixture()
        d.frames['close'].iloc[:,0]=np.arange(len(d.dates))*.01+10
        expression='x = ts_delay(close, 2) / ts_delay(close, 5); rank(x)'
        before=Expression(expression).evaluate(fields(d))[0].values
        for k in ('close','cumadj','amt'): d.frames[k].iloc[20:]*=3
        after=Expression(expression).evaluate(fields(d))[0].values
        pd.testing.assert_frame_equal(before.iloc[:20],after.iloc[:20])

    def test_prefix_and_full_evaluation_equal(self):
        d=fixture()
        expr='rank(ts_return(close, 3)) - rank(ts_std(returns, 4))'
        full=Expression(expr).evaluate(fields(d))[0].values
        sub=d.through(d.dates[15])
        prefix=Expression(expr).evaluate(fields(sub))[0].values
        pd.testing.assert_frame_equal(full.loc[:d.dates[15]],prefix)

    def test_safe_expression_rejects_escape_routes(self):
        for expression in ['__import__("os")','close.values','close[-1]','ts_delay(close,-1)',
                           'ts_mean(close,0)','2 ** 10000000','open("x")','[x for x in close]',
                           'close = 1; close','rank(close, **{})','_data','__class__']:
            with self.subTest(expression=expression),self.assertRaises((ValueError,SyntaxError,TypeError)):
                Expression(expression).evaluate(fields(fixture()))

    def test_missing_volume_is_explicit(self):
        with self.assertRaisesRegex(ValueError,'not share volume'):
            Expression('rank(volume)').evaluate(fields(fixture()))

    def test_rank_excludes_ineligible_names(self):
        d=fixture(5)
        d.frames['close'].iloc[:,0]=8
        mask=pd.DataFrame(True,index=d.dates,columns=d.codes)
        mask.iloc[:,2]=False
        value=rank(Fields(d,mask).close).values
        self.assertAlmostEqual(value.iloc[-1,0],.5)
        self.assertTrue(np.isnan(value.iloc[-1,2]))

    def test_decay_matches_hand_calculation(self):
        f=fields(fixture(5)).amt
        f.values.iloc[:,0]=[1,2,3,4,5]
        result=decay_linear(f,3).values
        self.assertTrue(np.isnan(result.iloc[1,0]))
        self.assertAlmostEqual(result.iloc[2,0],14/6)
        self.assertAlmostEqual(result.iloc[4,0],26/6)

    def test_trade_when_exit_has_priority_and_can_restart(self):
        f=fields(fixture(5))
        alpha=f.amt
        alpha.values.iloc[:,0]=[1,2,3,4,5]
        trigger=alpha>0
        trigger.values.iloc[:,0]=[True,False,True,False,True]
        exit=alpha>100
        exit.values.iloc[:,0]=[False,False,True,False,False]
        actual=trade_when(trigger,alpha,exit).values.iloc[:,0]
        np.testing.assert_allclose(actual,[1,1,np.nan,np.nan,5],equal_nan=True)

    def test_price_dimension_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            Expression('close + rank(amt)').evaluate(fields(fixture()))

    def test_diagnostics_do_not_expose_future_labels_to_expression(self):
        d=fixture(10)
        d.frames['close'].iloc[:,0]=np.arange(10)+10
        d.frames['close'].iloc[:,1]=10
        d.frames['close'].iloc[:,2]=10-np.arange(10)*.1
        score=pd.DataFrame(np.tile([3.,2.,1.],(10,1)),index=d.dates,columns=d.codes)
        diag=factor_diagnostics(score,d,d.dates[0],d.dates[-1],(1,5))
        np.testing.assert_allclose(diag['rank_ic_1d'].dropna(),1)
        self.assertTrue(diag['rank_ic_5d'].iloc[-5:].isna().all())
        with self.assertRaises(ValueError): Expression('rank_ic_1d').evaluate(fields(d))


class BrokerTests(unittest.TestCase):
    def make(self,**kwargs):
        d=fixture()
        s=settings(**kwargs)
        b=Broker(d,s,d.frames['amt'])
        b.start_day(1)
        return d,b

    def test_t_plus_one_and_no_negative_cash(self):
        d,b=self.make()
        r=b.trade(0,100,d.dates[0])
        self.assertEqual(r['status'],'filled')
        self.assertEqual(b.cash,99000)
        r=b.trade(0,-100,d.dates[0])
        self.assertEqual(r['reason'],'t_plus_one')
        self.assertEqual(b.cash,99000)
        b.start_day(2)
        b.trade(0,-100,d.dates[1])
        self.assertEqual(b.cash,100000)
        self.assertEqual(b.units[0],0)

    def test_no_amount_never_fills(self):
        d,b=self.make()
        b.a['amt'][1,0]=0
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'no_trading_amount')
        self.assertEqual(b.cash,100000)

    def test_flat_bar_never_fills(self):
        d,b=self.make()
        b.a['high'][1,0]=b.a['low'][1,0]=10
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'flat_bar_uncertain')

    def test_failed_sell_does_not_release_cash(self):
        d,b=self.make(initial_cash=1000)
        b.trade(0,100,d.dates[0])
        b.start_day(2)
        b.a['amt'][2,0]=0
        b.trade(0,-100,d.dates[1])
        self.assertEqual(b.cash,0)
        r=b.trade(1,100,d.dates[1])
        self.assertEqual(r['filled_shares_equiv'],0)
        self.assertEqual(b.units[0],100)

    def test_amount_budget_shared_between_orders(self):
        d,b=self.make(amount_participation=.00002)
        a=b.trade(0,1000,d.dates[0])
        b2=b.trade(0,1000,d.dates[0])
        self.assertEqual(a['status'],'partial')
        self.assertEqual(a['filled_shares_equiv'],200)
        self.assertEqual(b2['filled_shares_equiv'],0)

    def test_fee_minimum_and_stamp_date_switch(self):
        d,b=self.make(commission=.0003,min_commission=5,stamp_tax=True)
        b.trade(0,100,d.dates[0])
        self.assertEqual(b.cash,98995)
        for i,expected in [(2,1.0),(20,.5)]:
            b.units[0]=b.sellable[0]=100
            b.start_day(i)
            r=b.trade(0,-100,d.dates[i-1])
            self.assertAlmostEqual(r['stamp_tax'],expected)

    def test_no_false_loss_on_adjustment(self):
        d,b=self.make()
        b.trade(0,100,d.dates[0])
        before=(b.units*b.mark(1)).sum()+b.cash
        b.a['close'][2,0]=5
        b.a['cumadj'][2,0]=2
        after=(b.units*b.mark(2)).sum()+b.cash
        self.assertEqual(before,after)
        self.assertEqual(b.units[0],100)  # units, not real shares

    def test_missing_mark_keeps_position(self):
        d,b=self.make()
        b.trade(0,100,d.dates[0])
        old=b.mark(1)
        b.a['close'][2,0]=np.nan
        b.a['amt'][2,0]=np.nan
        new=b.mark(2)
        self.assertEqual(old[0],new[0])
        self.assertEqual(b.units[0],100)
        self.assertEqual(b.stale[0],1)

    def test_price_is_not_clamped_to_bar(self):
        d,b=self.make(slippage_bps=2000)
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'slipped_price_outside_bar')

    def test_external_rules_can_deny_trading(self):
        d,b=self.make()
        f=pd.DataFrame([dict(date=d.dates[1],code=d.codes[0],tradable=0,upper_limit=11,lower_limit=9,no_limit=0,lot_size=100,min_buy_qty=100)])
        b.states=TradingStates(f)
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'not_tradable')


class IntegrationTests(unittest.TestCase):
    def test_constant_prices_conserve_nav(self):
        result=Research(fixture()).run('rank(amt)',settings())
        np.testing.assert_allclose(result.daily.nav,100000)
        self.assertTrue((result.orders.date>result.orders.signal_date).all())
        self.assertTrue((result.daily.cash>=0).all())

    def test_future_data_cannot_change_past_fills(self):
        d=fixture()
        before=Research(d).run('rank(ts_return(close,2))',settings())
        for k in ('open','high','low','close'): d.frames[k].iloc[20:]*=1.02
        d.frames['cumadj'].iloc[20:]*=2
        after=Research(d).run('rank(ts_return(close,2))',settings())
        pd.testing.assert_frame_equal(before.daily.loc[:d.dates[19]],after.daily.loc[:d.dates[19]])
        pd.testing.assert_frame_equal(before.orders[before.orders.date<d.dates[20]],after.orders[after.orders.date<d.dates[20]])

    def test_last_incomplete_day_trimmed(self):
        d=fixture()
        d.frames['num_trades'].iloc[-1]=np.nan
        r=Research(d).run('rank(amt)',settings())
        self.assertEqual(r.daily.index[-1],d.dates[-2])
        self.assertEqual(len(r.metadata['trimmed_dates']),1)

    def test_export_is_immutable_and_json_valid(self):
        r=Research(fixture()).run('rank(amt)',settings())
        temp_root=ROOT/'tests'/'.tmp'
        temp_root.mkdir(exist_ok=True)
        self.assertTrue(temp_root.resolve().is_relative_to(ROOT.resolve()))
        with tempfile.TemporaryDirectory(dir=temp_root) as t:
            self.assertTrue(Path(t).resolve().is_relative_to(temp_root.resolve()))
            path=r.save(Path(t)/'one')
            manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['accounting'],'total_return_economic_units')
            self.assertTrue((path/'report.html').exists())
            self.assertTrue((path/'source_code.zip').exists())
            with self.assertRaises(FileExistsError): r.save(path)

    def test_callback_agrees_with_expression(self):
        lab=Research(fixture())
        a=lab.factor('rank(ts_return(close,2))',settings())
        b=lab.factor(lambda f:rank(f.close/ts_delay(f.close,2)-1),settings())
        pd.testing.assert_frame_equal(a,b)

    def test_invalid_settings_fail_early(self):
        for kw in [dict(delay=0),dict(top_n=100,retain_n=50),dict(max_weight=2),dict(initial_cash=-1),dict(commission=-1)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError): settings(**kw)

    def test_missing_date_is_not_removed_from_historical_universe(self):
        d=fixture()
        for frame in d.frames.values(): frame.iloc[20:,0]=np.nan
        mask,_=d.universe(settings())
        self.assertTrue(mask.iloc[10,0])
        self.assertFalse(mask.iloc[25,0])


if __name__=='__main__': unittest.main()
