import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_system import ROOT,fixture,settings
import pandas as pd
from pkuquant import (Research,DataRequest,ProviderInfo,FunctionProvider,SourceProvider,DatasetBundle,
                      MissingCapability,collect)
from examples.provider_demo import make_demo_provider,demo_settings
from pkuquant.service import create_app
from fastapi.testclient import TestClient
import time


class ProviderTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/'tests'/'.tmp'; root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(ROOT.resolve()))
        self.request=DataRequest(('000001.SZ','600001.SH'),'2023-08-01','2023-08-31')

    def tearDown(self): self.temp.cleanup()

    def bundle(self): return collect(make_demo_provider(),self.request)

    def test_request_and_capabilities_fail_early(self):
        for args in [([], '2023-01-01','2023-01-02'),(['000001.SZ']*2,'2023-01-01','2023-01-02'),
                     (['000001.SZ'],'2023-01-02','2023-01-01'),(['000001.SZ'],'2023-01-01 09:25','2023-01-02')]:
            with self.assertRaises(ValueError): DataRequest(*args)
        with self.assertRaises(ValueError): ProviderInfo('vendor','1',('bars','live_orders'))
        f=Mock()
        p=FunctionProvider(name='bars-only',bars=f)
        with self.assertRaises(MissingCapability): collect(p,self.request,require=('security_history',))
        f.assert_not_called()

    def test_bars_only_stays_unknown_and_old_adapter_still_works(self):
        d=fixture(8,codes=self.request.symbols)
        r=DataRequest(tuple(d.codes),str(d.dates[0].date()),str(d.dates[-1].date()))
        p=SourceProvider(lambda request:d,name='existing-source')
        bundle=collect(p,r)
        self.assertIsNone(bundle.security_history)
        self.assertFalse(Research(bundle).doctor(settings())['checks']['historical_st_complete'])
        self.assertIsNone(bundle.calendar)

    def test_declared_but_unavailable_dataset_does_not_silently_fallback(self):
        p=make_demo_provider(); p.callbacks['security_history']=lambda r:None
        with self.assertRaisesRegex(ValueError,'returned None'): collect(p,self.request)
        p.callbacks['security_history']=Mock(side_effect=ConnectionError('provider offline'))
        with self.assertRaises(ConnectionError): collect(p,self.request)

    def test_empty_st_table_is_unknown_not_normal(self):
        p=make_demo_provider()
        p.callbacks['security_history']=lambda r:pd.DataFrame(columns=['code','effective_date','known_at','is_st'])
        lab=Research.from_provider(p,self.request)
        self.assertEqual(lab.doctor(demo_settings())['st_coverage']['coverage'],0)
        with self.assertRaisesRegex(ValueError,'Unknown historical ST'): lab.run('rank(amt)',demo_settings())

    def test_symbol_and_calendar_mismatch_rejected(self):
        p=make_demo_provider(); original=p.callbacks['bars']
        p.callbacks['bars']=lambda r:fixture(8)
        with self.assertRaisesRegex(ValueError,'requested securities'): collect(p,self.request)
        p.callbacks['bars']=original; p.callbacks['calendar']=lambda r:pd.DatetimeIndex(['2023-08-01'])
        with self.assertRaisesRegex(ValueError,'outside calendar'): collect(p,self.request)

    def test_missing_whole_session_is_preserved_and_hashed(self):
        p=make_demo_provider(); original=p.callbacks['bars']
        def missing(request):
            d=original(request)
            from pkuquant import MarketData
            return MarketData({k:v.drop(v.index[5]) for k,v in d.frames.items()},metadata=d.metadata)
        p.callbacks['bars']=missing
        bundle=collect(p,self.request)
        self.assertEqual(len(bundle.market.dates),23)
        self.assertTrue(bundle.market.frames['close'].iloc[5].isna().all())
        self.assertIsNotNone(bundle.provenance()['calendar_sha256'])

    def test_bundle_roundtrip_identical_strategy_and_version_provenance(self):
        bundle=self.bundle(); before=Research(bundle).run('rank(ts_return(close,2))',demo_settings())
        folder=bundle.save(self.root/'snapshot')
        loaded=DatasetBundle.load(folder)
        after=Research(folder).run('rank(ts_return(close,2))',demo_settings())
        pd.testing.assert_frame_equal(before.daily,after.daily)
        pd.testing.assert_frame_equal(before.orders,after.orders)
        self.assertEqual(after.metadata['provider_bundle']['provider']['name'],'SYNTHETIC_DEMO')
        self.assertEqual(loaded.snapshot_sha256,hashlib.sha256((folder/'bundle.json').read_bytes()).hexdigest())
        self.assertTrue(any('合成' in text for text in after.metadata['limitations']))
        self.assertFalse(after.metadata['readiness']['verified_data_ready'])
        with self.assertRaises(FileExistsError): bundle.save(folder)
        with self.assertRaisesRegex(ValueError,'override'): Research(loaded,trading_states=pd.DataFrame())

    def test_tampered_extra_and_unsafe_bundle_files_rejected(self):
        for kind in ('changed','extra','escape','schema'):
            folder=self.bundle().save(self.root/kind)
            manifest=folder/'bundle.json'
            info=json.loads(manifest.read_text(encoding='utf-8'))
            if kind=='changed':
                with (folder/'trading_states.csv').open('a') as f: f.write('\n')
            elif kind=='extra': (folder/'unlisted.txt').write_text('test')
            elif kind=='escape': info['files']['../outside']='not-a-digest'
            else: info['schema_version']=999
            manifest.write_text(json.dumps(info),encoding='utf-8')
            with self.subTest(kind=kind),self.assertRaises(ValueError): DatasetBundle.load(folder)

    def test_complete_provider_callback_is_used_once_per_dataset(self):
        p=make_demo_provider()
        p.callbacks={name:Mock(wraps=callback) for name,callback in p.callbacks.items()}
        lab=Research.from_provider(p,self.request,require=('calendar','security_history','trading_states'))
        lab.run('rank(amt)',demo_settings())
        for callback in p.callbacks.values(): callback.assert_called_once_with(self.request)

    def test_api_worker_reads_entire_bundle_without_vendor_connection(self):
        folder=self.bundle().save(self.root/'source')
        token='offline-test-'+'a'*40
        app=create_app(sources={'bundle':{'data':str(folder)}},root=self.root/'jobs',token=token)
        headers={'Authorization':'Bearer '+token}
        with TestClient(app) as client:
            response=client.post('/jobs',headers=headers,json=dict(source='bundle',alpha='rank(amt)',settings=demo_settings().to_dict()))
            self.assertEqual(response.status_code,202,response.text)
            jid=response.json()['id']; deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                job=client.get('/jobs/'+jid,headers=headers).json()
                if job['status'] not in ('queued','running'): break
                time.sleep(.02)
            self.assertEqual(job['status'],'succeeded',job)
            manifest=client.get(f'/jobs/{jid}/artifacts/manifest.json',headers=headers).json()
            self.assertEqual(manifest['provider_bundle']['provider']['name'],'SYNTHETIC_DEMO')
            self.assertEqual(manifest['st_coverage']['coverage'],1.)


if __name__=='__main__': unittest.main()
