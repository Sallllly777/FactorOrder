import json
from pathlib import Path
import tempfile
import time
import unittest
from test_system import ROOT,fixture,settings
from pkuquant.service import create_app
from pkuquant.web import result_view
from pkuquant import Research
from fastapi.testclient import TestClient


class WebTests(unittest.TestCase):
    def setUp(self):
        root=ROOT/'tests'/'.tmp';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.temp.name).resolve();self.assertTrue(self.root.is_relative_to(ROOT.resolve()))
        self.data=fixture(8).export(self.root/'market')
        self.token='web-test-only-'+'t'*40
        self.app=create_app(sources={'test':{'data':str(self.data)}},root=self.root/'jobs',token=self.token)

    def tearDown(self):self.temp.cleanup()

    def login(self,client):
        return client.post('/session',headers={'Authorization':'Bearer '+self.token})

    def test_public_shell_does_not_disclose_sources_or_secret(self):
        with TestClient(self.app) as c:
            response=c.get('/')
            self.assertEqual(response.status_code,200)
            self.assertIn('因子工作台',response.text)
            self.assertNotIn(self.token,response.text)
            self.assertEqual(c.get('/ui/catalog').json()['sources'],[])
            self.assertEqual(c.get('/ui/config').status_code,401)
            self.assertEqual(c.get('/assets/app.js').status_code,200)
            self.assertEqual(c.get('/assets/secret.py').status_code,404)
            self.assertIn("script-src 'self'",response.headers['content-security-policy'])

    def test_session_cookie_csrf_and_logout(self):
        with TestClient(self.app) as c:
            self.assertEqual(c.post('/session',headers={'Authorization':'Bearer invalid'}).status_code,401)
            login=self.login(c);cookie=login.headers['set-cookie'].lower()
            self.assertIn('httponly',cookie);self.assertIn('samesite=strict',cookie)
            self.assertEqual(c.get('/ui/config').status_code,200)
            spec=dict(source='test',alpha='rank(amt)',settings=settings().to_dict())
            self.assertEqual(c.post('/jobs',json=spec).status_code,403)
            self.assertEqual(c.post('/jobs',json=spec,headers={'X-PKUQuant-Request':'1','Origin':'https://attacker.invalid'}).status_code,403)
            self.assertEqual(c.delete('/session',headers={'X-PKUQuant-Request':'1'}).status_code,200)
            self.assertEqual(c.get('/ui/config').status_code,401)

    def test_https_session_has_secure_flag(self):
        with TestClient(self.app,base_url='https://testserver') as c:
            self.assertIn('Secure',self.login(c).headers['set-cookie'])

    def test_browser_job_result_and_strategy_roundtrip(self):
        with TestClient(self.app) as c:
            self.login(c)
            spec=dict(source='test',alpha='rank(amt)',settings=settings().to_dict())
            response=c.post('/jobs',json=spec,headers={'X-PKUQuant-Request':'1','Origin':'http://testserver'})
            self.assertEqual(response.status_code,202,response.text);jid=response.json()['id']
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                job=c.get('/jobs/'+jid).json()
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'succeeded',job)
            strategy=c.get(f'/jobs/{jid}/strategy').json()
            self.assertEqual(strategy,spec)
            self.assertNotIn(str(self.data),json.dumps(strategy))
            result=c.get(f'/jobs/{jid}/result').json()
            self.assertEqual(result['metrics']['total_return'],0)
            self.assertEqual(result['daily_count'],len(result['curve']))
            self.assertTrue(all(p['nav']==1 for p in result['curve']))
            self.assertTrue(all(p['drawdown']==0 for p in result['curve']))
            self.assertEqual(result['st_coverage']['coverage'],0)
            self.assertEqual(c.get(f'/jobs/{jid}/artifacts/report.html').status_code,200)

    def test_result_drawdown_includes_initial_cash(self):
        result=Research(fixture(8)).run('rank(amt)',settings(commission=.001,min_commission=5))
        path=result.save(self.root/'result')
        response=result_view(path)
        first_loss=next(i for i,row in enumerate(response['curve']) if row['nav']<1)
        self.assertLess(response['curve'][first_loss]['drawdown'],0)
        self.assertAlmostEqual(response['curve'][first_loss]['drawdown'],result.daily.nav.iloc[first_loss]/settings().initial_cash-1)

    def test_evaluation_api_roundtrip_and_artifact(self):
        from pkuquant import EvaluationSpec
        with TestClient(self.app) as c:
            self.login(c)
            headers={'X-PKUQuant-Request':'1'}
            spec=dict(source='test',alpha='rank(amt)',settings=settings().to_dict(),
                      evaluation=json.loads(json.dumps(EvaluationSpec(min_assets=3,min_dates=20,bootstrap_samples=0,family='api-test').to_dict())))
            self.assertEqual(c.get('/assets/evaluation.js').status_code,200)
            self.assertIn('evaluation_defaults',c.get('/ui/config').json())
            invalid={**spec,'evaluation':{'evidence':'validated'}}
            self.assertEqual(c.post('/jobs',json=invalid,headers=headers).status_code,422)
            invalid={**spec,'evaluation':{'peers':['__import__("os")']}}
            self.assertEqual(c.post('/jobs',json=invalid,headers=headers).status_code,422)
            response=c.post('/jobs',json=spec,headers=headers)
            self.assertEqual(response.status_code,202,response.text);jid=response.json()['id']
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                job=c.get('/jobs/'+jid).json()
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'succeeded',job)
            self.assertEqual(c.get(f'/jobs/{jid}/strategy').json(),spec)
            result=c.get(f'/jobs/{jid}/result').json()
            self.assertEqual(result['evaluation']['status'],'insufficient_evidence')
            self.assertIsNone(result['evaluation']['score']['total'])
            self.assertEqual(len(result['evaluation_series']),8)
            artifact=c.get(f'/jobs/{jid}/artifacts/evaluation.json')
            self.assertEqual(artifact.status_code,200)
            self.assertEqual(artifact.json()['evidence']['family'],'api-test')

    def test_training_validation_protocol_roundtrip(self):
        from pkuquant import EvaluationSpec
        from test_evaluation import panel_fixture
        from test_stability import split_spec
        data,_,_=panel_fixture(100)
        path=data.export(self.root/'stability-market')
        app=create_app(sources={'test':{'data':str(path)}},root=self.root/'stability-jobs',token=self.token)
        with TestClient(app) as c:
            self.login(c);headers={'X-PKUQuant-Request':'1'}
            evaluation=json.loads(json.dumps(EvaluationSpec(**split_spec(data)).to_dict()))
            spec=dict(source='test',alpha='rank(amt)',settings=settings().to_dict(),evaluation=evaluation)
            invalid={**spec,'evaluation':{**evaluation,'validation_start':evaluation['train_end']}}
            self.assertEqual(c.post('/jobs',json=invalid,headers=headers).status_code,422)
            invalid={**spec,'settings':settings(start=evaluation['validation_start']).to_dict()}
            self.assertEqual(c.post('/jobs',json=invalid,headers=headers).status_code,422)
            response=c.post('/jobs',json=spec,headers=headers)
            self.assertEqual(response.status_code,202,response.text);jid=response.json()['id']
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                job=c.get('/jobs/'+jid).json()
                if job['status'] not in ('queued','running'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'succeeded',job)
            self.assertEqual(c.get(f'/jobs/{jid}/strategy').json(),spec)
            e=c.get(f'/jobs/{jid}/result').json()['evaluation']
            self.assertEqual(e['score']['version'],'heuristic-1.1')
            self.assertEqual(e['stability']['status'],'evaluated')
            self.assertFalse(e['stability']['independently_verified'])
            for name in ('evaluation_stability.csv','evaluation_period_returns.csv'):
                self.assertEqual(c.get(f'/jobs/{jid}/artifacts/{name}').status_code,200)


if __name__=='__main__':unittest.main()
