import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pandas as pd
from test_system import fixture, settings, ROOT
from pkuquant import Research, EvaluationSpec
from pkuquant.evaluation import signal_diagnostics, mean_evidence, holdings_overlap
from pkuquant.factors import Panel
from pkuquant.web import result_view


def panel_fixture(n=90):
    d=fixture(n,tuple(f'{i:06d}.SZ' for i in range(1,9)))
    rates=np.arange(1,9)*.0003
    price=pd.DataFrame(10*(1+rates[None,:])**np.arange(n)[:,None],index=d.dates,columns=d.codes)
    for k in ('open','close'): d.frames[k]=price.copy()
    d.frames['high']=price*1.05; d.frames['low']=price*.95
    d.frames['amt']=price*1e7
    score=pd.DataFrame(np.tile(np.arange(1,9),(n,1)),index=d.dates,columns=d.codes,dtype=float)
    eligible=score.notna()
    return d,score,eligible


class EvaluationTests(unittest.TestCase):
    def diagnostic(self,d,s,e,spec=None):
        return signal_diagnostics(s,d,e,settings(),spec or EvaluationSpec(min_assets=3,min_dates=20,bootstrap_samples=0),d.dates[0],d.dates[-1])

    def test_open_label_delay_hand_calculation_and_censor(self):
        d,s,e=panel_fixture(30)
        daily,_,_,_=self.diagnostic(d,s,e)
        row=daily[(daily.date==str(d.dates[0].date()))&(daily.horizon==1)].iloc[0]
        expected=d.frames['open'].iloc[2]/d.frames['open'].iloc[1]-1
        self.assertAlmostEqual(row.top_return,expected.iloc[-2:].mean())
        self.assertAlmostEqual(row.top_excess,expected.iloc[-2:].mean()-expected.mean())
        self.assertAlmostEqual(row.rank_ic,1)
        tail=daily[daily.horizon==1].tail(2)
        self.assertTrue((tail.right_censored==8).all())
        self.assertTrue(tail.rank_ic.isna().all())
        self.assertEqual(tail.missing_endpoints.sum(),0)

    def test_horizons_are_independent_and_groups_precede_missing_labels(self):
        d,s,e=panel_fixture(30)
        d.frames['open'].iloc[2,-1]=np.nan
        a,b,_,_=self.diagnostic(d,s,e,EvaluationSpec(horizons=(1,),min_assets=3,min_dates=20,bootstrap_samples=0))
        aa,bb,_,_=self.diagnostic(d,s,e)
        pd.testing.assert_frame_equal(a.reset_index(drop=True),aa[aa.horizon==1].reset_index(drop=True))
        first=b[b.date==str(d.dates[0].date())]
        self.assertEqual(first.assigned.sum(),8)
        self.assertEqual(first.valid.sum(),7)
        self.assertEqual(a.iloc[0].missing_endpoints,1)

    def test_boundary_ties_are_fractional_and_code_order_invariant(self):
        d,s,e=panel_fixture(30); s.iloc[:,-3:]=8
        a,_,_,_=self.diagnostic(d,s,e)
        expected=(d.frames['open'].iloc[2]/d.frames['open'].iloc[1]-1).iloc[-3:].mean()
        self.assertAlmostEqual(a.iloc[0].top_return,expected)
        reverse=s.columns[::-1]; s=s[reverse]; e=e[reverse]
        for key in d.frames:d.frames[key]=d.frames[key][reverse]
        b,_,_,_=self.diagnostic(d,s,e)
        np.testing.assert_allclose(a.top_return,b.top_return,equal_nan=True)

    def test_constant_and_infinite_factors_are_not_evidence(self):
        d,s,e=panel_fixture(30); s[:]=1; s.iloc[:,0]=np.inf
        daily,_,_,_=self.diagnostic(d,s,e)
        self.assertTrue(daily.rank_ic.isna().all())
        self.assertTrue(daily.top_excess.isna().all())
        self.assertTrue((daily.nonfinite_scores==1).all())
        self.assertTrue((daily.finite_scores==7).all())

    def test_hac_retains_calendar_gaps_and_bootstrap_is_reproducible(self):
        x=np.tile([1.,-1.,np.nan,2.,-2.],12)
        a=mean_evidence(x,lag=4,bootstrap=100,seed=8)
        self.assertEqual(a,mean_evidence(x,lag=4,bootstrap=100,seed=8))
        self.assertEqual(a['n'],48); self.assertEqual(a['calendar_dates'],60)
        z=np.where(np.isfinite(x),x-np.nanmean(x),0)
        var=z@z+sum(2*(1-k/5)*(z[k:]@z[:-k]) for k in range(1,5))
        self.assertAlmostEqual(a['hac_se'],np.sqrt(var*48/47)/48)
        self.assertIsNone(mean_evidence([1,2,3])['ci_low'])

    def test_holdings_overlap_excludes_empty_days_and_preserves_weights(self):
        dates=pd.date_range('2023-01-01',periods=2)
        a=pd.DataFrame({'date':[dates[0],dates[0]],'code':['a','b'],'weight':[.3,.2]})
        b=pd.DataFrame({'date':[dates[0],dates[0]],'code':['a','c'],'weight':[.1,.4]})
        self.assertAlmostEqual(holdings_overlap(a,b,dates),.1/(.3+.2+.4))
        self.assertAlmostEqual(holdings_overlap(a,a,dates),1)
        self.assertIsNone(holdings_overlap(a.iloc[:0],b.iloc[:0],dates))

    def test_research_gate_and_no_reweighting(self):
        d,_,_=panel_fixture()
        result=Research(d).run('rank(close)',settings(),evaluation=dict(min_assets=3,min_dates=20,bootstrap_samples=0))
        e=result.evaluation.summary
        self.assertEqual(e['status'],'research_only')
        self.assertFalse(e['evidence']['independently_verified'])
        self.assertIsNone(e['score']['total'])
        self.assertLess(e['score']['evaluated_weight'],94)
        self.assertEqual(sum(x['weight'] for x in e['score']['items']),100)
        self.assertTrue(all(x['points'] is None for x in e['score']['items'] if x['key'] in ('capacity','duplication','marginal')))

    def test_full_scenarios_preserve_settings_and_exact_benchmark(self):
        d,_,_=panel_fixture()
        opts=settings(); spec=EvaluationSpec(min_assets=3,min_dates=20,bootstrap_samples=0,stress_tests=True,benchmark_alpha='rank(close)',peers=('rank(close)',))
        r=Research(d).run('rank(close)',opts,evaluation=spec)
        scenarios=r.evaluation.scenarios.set_index('name')
        self.assertAlmostEqual(scenarios.loc['liquidity_benchmark','total_return'],r.metrics['total_return'])
        self.assertAlmostEqual(scenarios.loc['peer_basket','total_return'],scenarios.loc['peer_basket_plus_candidate','total_return'])
        self.assertEqual(r.evaluation.summary['score']['evaluated_weight'],94)
        doubled=json.loads(scenarios.loc['capital_x2','settings'])
        self.assertEqual(doubled,{**opts.to_dict(),'initial_cash':opts.initial_cash*2})
        delayed=json.loads(scenarios.loc['delay_plus_1','settings'])
        self.assertEqual(delayed,{**opts.to_dict(),'delay':opts.delay+1})
        self.assertAlmostEqual(r.evaluation.summary['peers'][0]['signal_correlation'],1)

    def test_direction_changes_execution_and_labels_together(self):
        d,_,_=panel_fixture(30); lab=Research(d)
        a=lab.run('rank(close)',settings(),evaluation={'direction':-1,'enabled':False})
        b=lab.run('-rank(close)',settings(),evaluation={'enabled':False})
        pd.testing.assert_frame_equal(a.daily,b.daily)
        pd.testing.assert_frame_equal(a.orders,b.orders)

    def test_evaluation_does_not_change_candidate_simulation(self):
        d,_,_=panel_fixture(30); lab=Research(d)
        a=lab.run('rank(close)',settings(),evaluation={'enabled':False})
        b=lab.run('rank(close)',settings(),evaluation={'bootstrap_samples':0,'min_assets':3,'min_dates':20,'stress_tests':True})
        pd.testing.assert_frame_equal(a.daily,b.daily)
        pd.testing.assert_frame_equal(a.orders,b.orders)

    def test_report_artifacts_checksums_and_legacy_view(self):
        d,_,_=panel_fixture(30)
        temp_root=ROOT/'tests'/'.tmp'; temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            r=Research(d).run('rank(close)',settings(),evaluation={'min_assets':3,'min_dates':20,'bootstrap_samples':0,'family':'<script>bad</script>'})
            folder=r.save(Path(tmp)/'report')
            checks=json.loads((folder/'checksums.json').read_text())
            for name in ('evaluation.json','evaluation_daily.csv','evaluation_quantiles.csv','evaluation_yearly.csv','evaluation_scenarios.csv'):
                self.assertEqual(checks[name],hashlib.sha256((folder/name).read_bytes()).hexdigest())
            self.assertEqual(result_view(folder)['evaluation']['version'],'1.1')
            manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8')); manifest.pop('evaluation')
            (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            self.assertIsNone(result_view(folder)['evaluation'])

    def test_invalid_protocols_fail_closed(self):
        for obj in ({'direction':True},{'horizons':[1,0]},{'horizons':[5]},{'min_dates':2},{'bootstrap_samples':5000},
                    {'peers':['rank(close)']*4},{'peers':['__import__("os")']},{'evidence':'validated'}, {'enabled':1}):
            with self.subTest(obj=obj),self.assertRaises((ValueError,TypeError)):
                EvaluationSpec(**obj)

    def test_future_data_beyond_end_does_not_change_evaluation(self):
        d,_,_=panel_fixture(40); opts=settings(end=str(d.dates[29].date()))
        spec=dict(min_assets=3,min_dates=20,bootstrap_samples=0)
        a=Research(d).run('rank(close)',opts,evaluation=spec)
        for key in ('open','high','low','close','amt'):d.frames[key].iloc[30:]*=3
        b=Research(d).run('rank(close)',opts,evaluation=spec)
        pd.testing.assert_frame_equal(a.evaluation.daily,b.evaluation.daily)
        self.assertEqual(a.evaluation.summary['score'],b.evaluation.summary['score'])

    def test_missing_label_gate_and_synthetic_total(self):
        d,_,_=panel_fixture(50)
        d.frames['open'].iloc[:,4:]=np.nan
        r=Research(d).run('rank(close)',settings(),evaluation=dict(min_assets=3,min_dates=20,bootstrap_samples=0))
        self.assertEqual(r.evaluation.summary['status'],'insufficient_evidence')
        self.assertEqual(r.evaluation.summary['score']['evaluated_weight'],0)
        self.assertIn('成熟标签覆盖率不足',r.evaluation.summary['gate_reasons'])
        d,_,_=panel_fixture();d.metadata['synthetic']=True
        r=Research(d).run('rank(close)',settings(),evaluation=dict(min_assets=3,min_dates=20,bootstrap_samples=0,stress_tests=True,peers=['rank(amt)']))
        self.assertEqual(r.evaluation.summary['score']['evaluated_weight'],94)
        self.assertIsNone(r.evaluation.summary['score']['total'])
        self.assertEqual(r.evaluation.summary['evidence']['data_state'],'synthetic')

    def test_rejected_unpriced_opens_still_reduce_execution_score(self):
        d,_,_=panel_fixture(50)
        d.frames['open'].iloc[:,-2:]=np.nan
        r=Research(d).run('rank(close)',settings(),evaluation=dict(min_assets=3,min_dates=20,bootstrap_samples=0,min_label_coverage=.7))
        self.assertTrue(r.orders.price.isna().any())
        item=next(x for x in r.evaluation.summary['score']['items'] if x['key']=='tracking')
        # First-day tied ranks can fill; subsequent high-ranked names have no opens.
        self.assertGreater(item['observed'],0)
        self.assertLess(item['observed'],.05)
        self.assertEqual(item['grade'],0)

    def test_comparison_hash_excludes_declarations_but_detects_data_changes(self):
        d,_,_=panel_fixture(30); spec=dict(min_assets=3,min_dates=20,bootstrap_samples=0)
        a=Research(d).run('rank(close)',settings(),evaluation=spec).evaluation.summary
        b=Research(d).run('rank(close)',settings(),evaluation={**spec,'declared_trials':2,'family':'new'}).evaluation.summary
        self.assertNotEqual(a['protocol_sha256'],b['protocol_sha256'])
        self.assertEqual(a['comparison_sha256'],b['comparison_sha256'])
        d.frames['amt']*=2
        c=Research(d).run('rank(close)',settings(),evaluation=spec).evaluation.summary
        self.assertNotEqual(a['comparison_sha256'],c['comparison_sha256'])


if __name__=='__main__': unittest.main()
