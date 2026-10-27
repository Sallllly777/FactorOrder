import json
import hashlib
import tempfile
from pathlib import Path
import unittest
import numpy as np
import pandas as pd
from test_system import settings,ROOT
from test_evaluation import panel_fixture
from pkuquant import Research,EvaluationSpec
from pkuquant.stability import comparison_values,preservation_grade,difference_interval,validate_periods
from pkuquant.evaluation import signal_diagnostics


def split_spec(d,cut=44,**kw):
    spec=dict(min_assets=3,min_dates=20,bootstrap_samples=0,horizons=[1],primary_horizon=1,
              train_start=str(d.dates[0].date()),train_end=str(d.dates[cut].date()),
              validation_start=str(d.dates[cut+1].date()),validation_end=str(d.dates[-1].date()),
              benchmark_alpha='-rank(amt)')
    return {**spec,**kw}


class StabilityTests(unittest.TestCase):
    def test_diff_improvement_and_signed_risk(self):
        r=comparison_values(.08,.01,.005)
        self.assertAlmostEqual(r['delta'],-.07);self.assertAlmostEqual(r['absolute_gap'],.07)
        self.assertAlmostEqual(r['deterioration'],.07);self.assertAlmostEqual(r['retention'],.125)
        improved=comparison_values(.08,.12,.005)
        self.assertEqual(improved['deterioration'],0);self.assertAlmostEqual(improved['retention'],1.5)
        risk=comparison_values(-.1,-.2,None)
        self.assertAlmostEqual(risk['deterioration'],.1);self.assertIsNone(risk['retention'])

    def test_scores_distinguish_decay_from_merely_positive(self):
        evidence={'ci_low':.001}
        row=comparison_values(.08,.01,.005)
        self.assertEqual(preservation_grade(row,evidence,evidence,True)[0],0)
        row=comparison_values(.08,.09,.005)
        self.assertEqual(preservation_grade(row,evidence,evidence,True)[0],4)
        self.assertEqual(preservation_grade(row,evidence,{'ci_low':-.01},True)[0],1)
        self.assertIsNone(preservation_grade(row,evidence,evidence,False)[0])
        for train in (0,-.01,.001):
            row=comparison_values(train,.001,.005)
            self.assertIsNone(row['retention'])
            self.assertIsNone(preservation_grade(row,evidence,evidence,True)[0])

    def test_explicit_period_validation(self):
        good=dict(train_start='2022-01-01',train_end='2022-12-31',validation_start='2023-01-01',validation_end='2023-12-31')
        for invalid in ({'train_start':'2022-01-01'}, {**good,'validation_start':'2022-12-31'},
                        {**good,'train_end':'2022-02-30'},{**good,'train_start':'2022-01-01 01:00'},
                        {**good,'validation_end':'2050-01-01'}, {'min_reference_ic':0}):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):EvaluationSpec(**invalid)
        spec=EvaluationSpec(**good)
        with self.assertRaises(ValueError):validate_periods(spec,settings(start='2023-01-01'))
        with self.assertRaises(ValueError):validate_periods(spec,settings(end='2022-12-31'))

    def test_contrast_ci_hand_case_and_no_fake_significance(self):
        calendar=pd.bdate_range('2023-01-01',periods=50)
        a=pd.Series(.04,index=calendar[:20]); b=pd.Series(.01,index=calendar[30:])
        lo,hi=difference_interval(a,b,calendar,4)
        self.assertAlmostEqual(lo,-.03);self.assertAlmostEqual(hi,-.03)
        self.assertEqual(difference_interval(a.iloc[:3],b,calendar,4),(None,None))
        with self.assertRaises(ValueError):difference_interval(a,a,calendar,1)

    def test_unconfigured_stability_has_no_substitute_score(self):
        d,_,_=panel_fixture(35)
        r=Research(d).run('rank(close)',settings(),evaluation=dict(min_assets=3,min_dates=20,bootstrap_samples=0))
        self.assertEqual(r.evaluation.summary['stability']['status'],'not_configured')
        items=r.evaluation.summary['score']['items']
        self.assertEqual(sum(x['weight'] for x in items),100)
        self.assertEqual(next(x['weight'] for x in items if x['key']=='time'),2)
        self.assertTrue(all(x['grade'] is None for x in items if x['key'].startswith('oos_')))

    def test_training_boundary_and_future_mutation(self):
        d,s,e=panel_fixture(110);spec=split_spec(d,horizons=[1,5],primary_horizon=5)
        a=Research(d).run('rank(amt)',settings(),evaluation=spec)
        train=next(p for p in a.evaluation.summary['stability']['periods'] if p['name']=='train')
        self.assertEqual(train['purged_signal_dates'],6)
        self.assertEqual(train['signal_evidence']['rank_ic']['n'],38) # one initial constant-signal date
        before=a.evaluation.period_returns.query("period == 'train'").reset_index(drop=True)
        for key in ('open','high','low','close','amt'):d.frames[key].iloc[45:]*=1.03
        b=Research(d).run('rank(amt)',settings(),evaluation=spec)
        after=next(p for p in b.evaluation.summary['stability']['periods'] if p['name']=='train')
        self.assertEqual(train,after)
        pd.testing.assert_frame_equal(before,b.evaluation.period_returns.query("period == 'train'").reset_index(drop=True))
        # Direct subinterval diagnostics also obey the requested right boundary.
        daily,_,_,_=signal_diagnostics(s,d,e,settings(),EvaluationSpec(**spec),d.dates[0],d.dates[44])
        self.assertTrue(daily[daily.horizon==5].tail(6).rank_ic.isna().all())

    def test_fresh_accounts_and_period_exports(self):
        d,_,_=panel_fixture(100); spec=split_spec(d)
        r=Research(d).run('rank(amt)',settings(),evaluation=spec)
        evidence=r.evaluation.summary['stability'];self.assertEqual(evidence['status'],'evaluated')
        for _,part in r.evaluation.period_returns.groupby('period'):
            self.assertAlmostEqual(part.candidate_nav.iloc[0],settings().initial_cash)
            self.assertEqual(part.candidate_return.iloc[0],0)
            self.assertAlmostEqual(part.benchmark_nav.iloc[0],settings().initial_cash)
        # Main continuous account is untouched by independently reset comparison accounts.
        legacy=Research(d).run('rank(amt)',settings(),evaluation={'enabled':False})
        pd.testing.assert_frame_equal(r.daily,legacy.daily);pd.testing.assert_frame_equal(r.orders,legacy.orders)
        temp_root=ROOT/'tests'/'.tmp';temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            folder=r.save(Path(tmp)/'result');checks=json.loads((folder/'checksums.json').read_text())
            for name in ('evaluation_stability.csv','evaluation_period_returns.csv'):
                self.assertEqual(checks[name],hashlib.sha256((folder/name).read_bytes()).hexdigest())
            self.assertIn('训练—验证稳定性',(folder/'report.html').read_text(encoding='utf-8'))

    def test_bad_validation_year_cannot_hide_in_positive_aggregate(self):
        d,_,_=panel_fixture(135);start=int(np.flatnonzero(d.dates.year==2024)[0])
        rates=(4.5-np.arange(1,9))*.001
        future=d.frames['close'].iloc[start-1].to_numpy()[None,:]*(1+rates[None,:])**np.arange(1,len(d.dates)-start+1)[:,None]
        for key,scale in [('open',1),('close',1),('high',1.05),('low',.95)]:d.frames[key].iloc[start:]=future*scale
        r=Research(d).run('rank(amt)',settings(),evaluation=split_spec(d,cut=29))
        stable=r.evaluation.summary['stability']
        rows={x['comparison']:x for x in stable['comparisons'] if x['metric']=='rank_ic'}
        self.assertGreater(rows['validation']['validation_value'],0)
        self.assertLess(rows['2024']['validation_value'],0)
        self.assertEqual(stable['grades']['rank_ic']['grade'],0)
        self.assertEqual(rows['2024']['grade'],0)

    def test_incomplete_year_is_not_dropped_from_scoring(self):
        d,_,_=panel_fixture(120) # 2024 tail shorter than min_dates
        r=Research(d).run('rank(amt)',settings(),evaluation=split_spec(d))
        stable=r.evaluation.summary['stability']
        self.assertTrue(any(x['name']=='2024' for x in stable['periods']))
        self.assertIsNone(stable['grades']['rank_ic']['grade'])
        self.assertEqual(stable['status'],'incomplete_evidence')


if __name__=='__main__':unittest.main()
