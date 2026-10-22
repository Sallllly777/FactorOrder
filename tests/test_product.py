import json
from pathlib import Path
import tempfile
import time
import subprocess
import threading
import unittest
from unittest.mock import patch
import hashlib

from test_system import fixture,settings,fields,ROOT
import numpy as np
import pandas as pd
from pkuquant import Research,SecurityHistory,from_long,QMTSource,TushareSource,TabularSource
from pkuquant.engine import Broker,TradingStates
from pkuquant.factors import Expression
from pkuquant.service import create_app,JobStore
from fastapi.testclient import TestClient


def history(d):
    return pd.DataFrame([dict(code=c,effective_date=d.dates[0],known_at=d.dates[0],is_st=0) for c in d.codes])


def long_fixture():
    d=fixture(8)
    return pd.concat([v.rename_axis(index='date',columns='code').stack().rename(k) for k,v in d.frames.items()],axis=1).reset_index()


class SecurityTests(unittest.TestCase):
    def test_known_time_prevents_lookahead_and_distinguishes_open_close(self):
        d=fixture(8); h=history(d)
        h.loc[len(h)]=[d.codes[0],d.dates[2],d.dates[3]+pd.Timedelta(hours=12),1]
        s=SecurityHistory(h)
        opening=s.matrix(d.dates,d.codes,at='09:25:00')
        closing=s.matrix(d.dates,d.codes)
        self.assertEqual(opening.iloc[3,0],0)
        self.assertEqual(closing.iloc[3,0],1)
        self.assertEqual(opening.iloc[4,0],1)
        self.assertEqual(closing.iloc[2,0],0)

    def test_unknown_is_not_normal_and_invalid_history_rejected(self):
        d=fixture()
        self.assertTrue(SecurityHistory().matrix(d.dates,d.codes).isna().all().all())
        with self.assertRaisesRegex(ValueError,'Unknown historical ST'):
            Research(d).run('rank(amt)',settings(unknown_st='error'))
        h=history(d); h.loc[0,'is_st']=2
        with self.assertRaises(ValueError): SecurityHistory(h)
        with self.assertRaises(ValueError): SecurityHistory(pd.concat([history(d),history(d)]))

    def test_late_revision_does_not_override_newer_effective_state(self):
        d=fixture(8); h=history(d)
        h.loc[len(h)]=[d.codes[0],d.dates[1],d.dates[1],1]
        h.loc[len(h)]=[d.codes[0],d.dates[3],d.dates[3],0]
        h.loc[len(h)]=[d.codes[0],d.dates[1],d.dates[5],1]
        s=SecurityHistory(h).matrix(d.dates,d.codes)
        self.assertEqual(s.iloc[2,0],1)
        self.assertEqual(s.iloc[5,0],0)

    def test_st_exclusion_and_unknown_exclusion_precede_rank(self):
        d=fixture(8); h=history(d).iloc[:2].copy(); h.loc[0,'is_st']=1
        s=SecurityHistory(h).matrix(d.dates,d.codes)
        eligible,_=d.universe(settings(universe='all',unknown_st='exclude'),s)
        self.assertFalse(eligible.iloc[-1,0]); self.assertTrue(eligible.iloc[-1,1]); self.assertFalse(eligible.iloc[-1,2])

    def test_daily_st_exit_retries_failed_sell_between_rebalances(self):
        d=fixture(8); h=history(d)
        h.loc[len(h)]=[d.codes[0],d.dates[2],d.dates[1]+pd.Timedelta(hours=18),1]
        # Aug 3 is Thursday, so monthly rebalance would not otherwise sell.
        d.frames['amt'].iloc[2,0]=0
        r=Research(d,security_history=h).run('rank(amt)',settings(rebalance='monthly'))
        sells=r.orders[(r.orders.code==d.codes[0])&(r.orders.side=='sell')]
        self.assertEqual(sells.iloc[0].date,d.dates[2]); self.assertEqual(sells.iloc[0].reason,'no_trading_amount')
        self.assertEqual(sells.iloc[1].date,d.dates[3]); self.assertEqual(sells.iloc[1].reason,'missing_lagged_liquidity')
        self.assertEqual(sells.iloc[2].date,d.dates[4]); self.assertEqual(sells.iloc[2].status,'filled')
        self.assertFalse(((r.orders.code==d.codes[0])&(r.orders.side=='buy')&(r.orders.date>=d.dates[2])).any())

    def test_execution_day_st_gate_and_tplusone(self):
        d=fixture(5); status=pd.DataFrame(0.,index=d.dates,columns=d.codes)
        b=Broker(d,settings(),d.frames['amt'],status=status)
        b.start_day(1); b.status[1,0]=1
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'st_buy_blocked')
        b.status[1,0]=0; b.trade(0,100,d.dates[0]); b.status[1,0]=1
        self.assertEqual(b.trade(0,-100,d.dates[0])['reason'],'t_plus_one')

    def test_strict_rules_fail_instead_of_guessing(self):
        with self.assertRaisesRegex(ValueError,'Missing verified trading rules'):
            Research(fixture()).run('rank(amt)',settings(require_verified_rules=True))
        d=fixture(5); b=Broker(d,settings(require_verified_rules=True),d.frames['amt']); b.start_day(1)
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'missing_verified_rules')

    def test_future_history_does_not_change_past_nav(self):
        d=fixture(15); h=history(d)
        a=Research(d,security_history=h).run('rank(amt)',settings())
        h.loc[len(h)]=[d.codes[0],d.dates[10],d.dates[10],1]
        b=Research(d,security_history=h).run('rank(amt)',settings())
        pd.testing.assert_frame_equal(a.daily.iloc[:8],b.daily.iloc[:8])


class SourceTests(unittest.TestCase):
    def test_units_volume_and_missing_numtrades(self):
        f=long_fixture().drop(columns='num_trades'); f['volume']=100.; f['amt']=1000.
        d=from_long(f,amount_unit='CNY_1K',volume_unit='lots100')
        self.assertEqual(d.frames['amt'].iloc[0,0],1000000)
        self.assertEqual(d.frames['volume'].iloc[0,0],10000)
        self.assertTrue(d.frames['num_trades'].isna().all().all())
        self.assertTrue(Expression('rank(volume)').evaluate(fields(d))[0].values.notna().any().any())

    def test_bad_mapping_adjustment_and_ambiguous_codes_rejected(self):
        for change in ('code','duplicate','price','volume'):
            f=long_fixture(); kw=dict(amount_unit='CNY')
            if change=='code': f['code']='000001'
            if change=='duplicate': f=pd.concat([f,f.iloc[:1]])
            if change=='price': kw['price_mode']='qfq'
            if change=='volume': f['volume']=100
            with self.subTest(change=change),self.assertRaises(ValueError): from_long(f,**kw)

    def test_calendar_preserves_missing_session(self):
        f=long_fixture(); dates=pd.DatetimeIndex(sorted(f.date.unique()))
        f=f[f.date!=dates[3]]
        d=from_long(f,amount_unit='CNY',expected_sessions=dates)
        self.assertEqual(len(d.dates),8)
        self.assertTrue(d.frames['close'].loc[dates[3]].isna().all())
        self.assertTrue(d.metadata['calendar_verified'])
        self.assertNotEqual(d.source_hash,from_long(f,amount_unit='CNY').source_hash)

    def test_volume_budget_and_exact_cash_minimum(self):
        d=fixture(); d.frames['volume']=d.frames['amt']*0+1000
        b=Broker(d,settings(volume_participation=.2),d.frames['amt']); b.start_day(1)
        self.assertEqual(b.trade(0,1000,d.dates[0])['filled_shares_equiv'],200)
        self.assertEqual(b.trade(0,100,d.dates[0])['reason'],'volume_cap')
        b=Broker(fixture(),settings(initial_cash=1005,commission=.0003,min_commission=5),d.frames['amt']); b.start_day(1)
        self.assertEqual(b.trade(0,100,d.dates[0])['filled_shares_equiv'],100)
        self.assertAlmostEqual(b.cash,0)

    def test_qmt_sdk_contract_raw_and_back_ratio(self):
        class Client:
            def __init__(self): self.calls=[]
            def get_market_data_ex(self,**kw):
                self.calls.append(kw)
                f=pd.DataFrame(dict(open=[10,5],high=[11,6],low=[9,4],close=[10,5],amount=[1e6,1e6],volume=[1000,2000]),index=['20230801','20230802'])
                if kw['dividend_type']=='back_ratio': f['close']=[10,10]
                return {'000001.SZ':f}
        client=Client(); d=QMTSource(['000001.SZ'],'2023-08-01','2023-08-02',volume_unit='shares',client=client).load()
        np.testing.assert_allclose(d.frames['cumadj'].iloc[:,0],[1,2])
        self.assertEqual(client.calls[0]['dividend_type'],'none')
        self.assertFalse(client.calls[0]['fill_data'])
        self.assertTrue(d.frames['num_trades'].isna().all().all())

    def test_tushare_contract_and_missing_factor(self):
        class Client:
            def daily(self,**kw):
                return pd.DataFrame([dict(ts_code='000001.SZ',trade_date='20230801',open=10,high=11,low=9,close=10,amount=1000,vol=100)])
            def adj_factor(self,**kw): return pd.DataFrame([dict(ts_code='000001.SZ',trade_date='20230801',adj_factor=2)])
        d=TushareSource(Client(),['000001.SZ'],'2023-08-01','2023-08-02').load()
        self.assertEqual(d.frames['volume'].iloc[0,0],10000); self.assertEqual(d.frames['amt'].iloc[0,0],1e6)
        with patch.object(Client,'adj_factor',return_value=pd.DataFrame(columns=['ts_code','trade_date','adj_factor'])):
            with self.assertRaisesRegex(ValueError,'Missing Tushare'): TushareSource(Client(),['000001.SZ'],'2023-08-01','2023-08-02').load()

    def test_readiness_does_not_claim_production(self):
        r=Research(fixture()).doctor(settings())
        self.assertFalse(r['verified_data_ready']); self.assertFalse(r['live_trading_ready'])
        self.assertIn('historical_st_complete',r['blockers'])
        self.assertEqual(r['st_coverage']['coverage'],0)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/'tests'/'.tmp'; root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(ROOT.resolve()))
        self.data=fixture(8).export(self.root/'data')
        self.token='test-only-'+'x'*40
        self.headers={'Authorization':'Bearer '+self.token}
        self.spec={'source':'test','alpha':'rank(amt)','settings':settings().to_dict()}

    def tearDown(self): self.temp.cleanup()

    def app(self,**kw):
        return create_app(sources={'test':{'data':str(self.data)}},root=self.root/'jobs',token=self.token,**kw)

    def test_auth_size_quota_validation_and_path_restrictions(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/health').status_code,200)
            self.assertEqual(client.get('/sources').status_code,401)
            self.assertEqual(client.get('/sources',headers=self.headers).json(),{'sources':['test']})
            self.assertEqual(client.post('/jobs',content='x'*17000,headers=self.headers).status_code,413)
            bad={**self.spec,'data':'C:/secret'}
            self.assertEqual(client.post('/jobs',json=bad,headers=self.headers).status_code,422)
            self.assertEqual(client.post('/jobs',json={**self.spec,'alpha':'ts_mean(close, 1000000)'},headers=self.headers).status_code,422)
            self.assertEqual(client.get('/jobs/not-an-id',headers=self.headers).status_code,404)

    def test_real_subprocess_job_and_artifact_integrity(self):
        with TestClient(self.app()) as client:
            headers={**self.headers,'Idempotency-Key':'one'}
            response=client.post('/jobs',json=self.spec,headers=headers)
            self.assertEqual(response.status_code,202,response.text); jid=response.json()['id']
            self.assertEqual(client.post('/jobs',json=self.spec,headers=headers).json()['id'],jid)
            self.assertEqual(client.post('/jobs',json={**self.spec,'alpha':'rank(close)'},headers=headers).status_code,422)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                job=client.get('/jobs/'+jid,headers=self.headers).json()
                if job['status'] not in ('queued','running'): break
                time.sleep(.05)
            self.assertEqual(job['status'],'succeeded',job)
            response=client.get(f'/jobs/{jid}/artifacts/manifest.json',headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['accounting'],'total_return_economic_units')
            checks=client.get(f'/jobs/{jid}/artifacts/checksums.json',headers=self.headers).json()
            self.assertEqual(checks['manifest.json'],hashlib.sha256(response.content).hexdigest())
            self.assertEqual(client.get(f'/jobs/{jid}/artifacts/input.json',headers=self.headers).status_code,404)

    def test_job_store_quota_cancel_restart_and_exclusive_lock(self):
        store=JobStore(self.root/'store'); store.lock()
        try:
            other=JobStore(self.root/'store')
            with self.assertRaises(RuntimeError): other.lock()
            jid=store.submit(self.spec,None,1,2)
            with self.assertRaises(OverflowError): store.submit(self.spec,None,1,2)
            self.assertTrue(store.cancel(jid)); self.assertEqual(store.get(jid)['status'],'cancelled')
            second=store.submit(self.spec,None,1,2)
            with store.connect() as c: c.execute("UPDATE jobs SET status='running' WHERE id=?",(second,))
        finally: store.release()
        other.lock()
        try: self.assertEqual(other.get(second)['status'],'interrupted')
        finally: other.release()

    def test_weak_secret_rejected(self):
        with self.assertRaises(ValueError): create_app(sources={},root=self.root/'invalid',token='short')

    def test_tabular_roundtrip_csv_and_parquet(self):
        f=long_fixture()
        for suffix in ('csv','parquet'):
            path=self.root/('vendor.'+suffix)
            if suffix=='csv': f.to_csv(path,index=False)
            else: f.to_parquet(path,index=False)
            d=TabularSource(path,amount_unit='CNY').load()
            pd.testing.assert_frame_equal(d.frames['close'],fixture(8).frames['close'].sort_index(axis=1),check_names=False,check_freq=False)

    def test_atomic_report_failure_never_publishes_target(self):
        result=Research(fixture()).run('rank(amt)',settings())
        target=self.root/'failed-result'
        with patch.object(result,'_save_files',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): result.save(target)
        self.assertFalse(target.exists())
        self.assertFalse((target.parent/('.'+target.name+'.lock')).exists())
        result.save(target)
        self.assertTrue((target/'checksums.json').exists())

    def test_timeout_kills_worker_and_cancel_interrupts_running_job(self):
        class FakeProcess:
            def __init__(self,*args,**kwargs): self.done=threading.Event(); self.killed=False
            def poll(self): return -15 if self.done.is_set() else None
            def wait(self,timeout=None):
                if not self.done.wait(timeout): raise subprocess.TimeoutExpired('test worker',timeout)
                return -15
            def kill(self): self.killed=True; self.done.set()
            def terminate(self): self.done.set()
        app=self.app(timeout=.05)
        with patch('pkuquant.service.subprocess.Popen',FakeProcess),TestClient(app) as client:
            jid=client.post('/jobs',json=self.spec,headers=self.headers).json()['id']
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                job=client.get('/jobs/'+jid,headers=self.headers).json()
                if job['status']=='timed_out': break
                time.sleep(.01)
            self.assertEqual(job['status'],'timed_out')
            self.assertEqual(client.get(f'/jobs/{jid}/artifacts/report.html',headers=self.headers).status_code,409)
            second=client.post('/jobs',json=self.spec,headers=self.headers).json()['id']
            response=client.post(f'/jobs/{second}/cancel',headers=self.headers)
            self.assertEqual(response.json()['status'],'cancelled')


if __name__=='__main__': unittest.main()
