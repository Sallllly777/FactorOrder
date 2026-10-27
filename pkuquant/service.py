"""Single-tenant research job service. No broker orders, arbitrary code or uploads."""
from contextlib import asynccontextmanager,contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import ast
import secrets

from .config import Settings
from .evaluation import EvaluationSpec
from .factors import Expression

ARTIFACTS={'report.html','manifest.json','daily.csv','orders.csv','holdings.csv','targets.csv',
           'factor_diagnostics.csv','source_code.zip','checksums.json','evaluation.json',
           'evaluation_daily.csv','evaluation_quantiles.csv','evaluation_yearly.csv','evaluation_scenarios.csv',
           'evaluation_stability.csv','evaluation_period_returns.csv'}


class JobStore:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.db=self.root/'jobs.sqlite3'
        self.mutex=threading.RLock()
        self.wakeup=threading.Event()
        self.stop=threading.Event()
        self.process=None
        self.active=None
        self.file=None
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, idem TEXT UNIQUE, digest TEXT, status TEXT, created REAL, updated REAL, spec TEXT, error TEXT)')

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.db,timeout=10)
        try:
            with conn: yield conn
        finally: conn.close()

    def lock(self):
        self.file=(self.root/'service.lock').open('a+b')
        self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file=None
            raise RuntimeError('Another service owns this job directory; use exactly one worker')
        with self.connect() as c:
            c.execute("UPDATE jobs SET status='interrupted',error='Service restarted during execution',updated=? WHERE status='running'",(time.time(),))

    def release(self):
        if self.file: self.file.close(); self.file=None

    def get(self,jid):
        with self.connect() as c:
            c.row_factory=sqlite3.Row
            row=c.execute('SELECT id,status,created,updated,error FROM jobs WHERE id=?',(jid,)).fetchone()
        return dict(row) if row else None

    def submit(self,spec,idem,max_queue,max_jobs):
        digest=hashlib.sha256(json.dumps(spec,sort_keys=True,allow_nan=False).encode()).hexdigest()
        with self.mutex,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if idem:
                old=c.execute('SELECT id,digest FROM jobs WHERE idem=?',(idem,)).fetchone()
                if old:
                    if old[1]!=digest: raise ValueError('Idempotency key already used with different input')
                    return old[0]
            count=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            total=c.execute('SELECT count(*) FROM jobs').fetchone()[0]
            if count>=max_queue or total>=max_jobs: raise OverflowError('Job quota reached; administrator must review retention')
            jid=uuid.uuid4().hex
            c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',(jid,idem,digest,'queued',time.time(),time.time(),json.dumps(spec),None))
        self.wakeup.set()
        return jid

    def cancel(self,jid):
        with self.mutex,self.connect() as c:
            changed=c.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('queued','running')",(time.time(),jid)).rowcount
            if self.active==jid and self.process and self.process.poll() is None: self.process.terminate()
        return bool(changed)

    def run(self,timeout):
        while not self.stop.is_set():
            with self.mutex,self.connect() as c:
                row=c.execute("SELECT id,spec FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if row:
                    c.execute("UPDATE jobs SET status='running',updated=? WHERE id=?",(time.time(),row[0]))
                    self.active=row[0]
            if not row:
                self.wakeup.wait(.5); self.wakeup.clear(); continue
            jid,raw=row
            status,error='failed','Worker failed; administrator can inspect its local log'
            try:
                folder=self.root/jid; folder.mkdir(exist_ok=False)
                spec=json.loads(raw); spec['output']=str(folder/'result')
                (folder/'input.json').write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
                with (folder/'worker.log').open('wb') as log:
                    with self.mutex:
                        if self.get(jid)['status']=='cancelled' or self.stop.is_set():
                            status='cancelled'
                        else:
                            env=os.environ.copy(); env.pop('PKUQUANT_API_TOKEN',None)
                            self.process=subprocess.Popen([sys.executable,'-m','pkuquant.worker',str(folder/'input.json')],
                                cwd=Path(__file__).resolve().parents[1],stdout=log,stderr=log,env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    if self.process:
                        try:
                            rc=self.process.wait(timeout=timeout)
                            if rc==0 and (folder/'result'/'checksums.json').exists(): status,error='succeeded',None
                        except subprocess.TimeoutExpired:
                            self.process.kill(); self.process.wait(); status,error='timed_out','Worker exceeded wall-clock limit'
            except Exception:
                # Do not expose provider paths, credentials or tracebacks in HTTP responses.
                import logging
                logging.getLogger(__name__).exception('Job %s failed',jid)
            finally:
                with self.mutex,self.connect() as c:
                    c.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE id=? AND status='running'",(status,error,time.time(),jid))
                    self.process=None; self.active=None


def create_app(*,sources,root='service-data',token=None,timeout=1800,max_queue=8,max_jobs=1000):
    from fastapi import FastAPI,Depends,HTTPException,Request
    from fastapi.responses import FileResponse,JSONResponse
    from .web import config_view,result_view
    if not token: token=os.environ.get('PKUQUANT_API_TOKEN','')
    if len(token)<32: raise ValueError('Set PKUQUANT_API_TOKEN to a random secret of at least 32 characters')
    if timeout<=0 or max_queue<1 or max_jobs<1: raise ValueError('Invalid service limits')
    registry={}
    for key,source in sources.items():
        if not isinstance(key,str) or not key or len(key)>80: raise ValueError('Invalid source ID')
        if set(source)-{'data','states','security_history','reference_cache'} or not source.get('data'): raise ValueError('Invalid source registry')
        registry[key]={k:str(Path(v).resolve()) for k,v in source.items() if v is not None}
        if not all(Path(v).exists() for v in registry[key].values()): raise ValueError(f'Source files missing for {key}')
    if not registry: raise ValueError('At least one administrator-configured source is required')
    store=JobStore(root)
    sessions={}
    cookie_name='pkuquant_session'
    static=Path(__file__).parent/'static'

    @asynccontextmanager
    async def lifespan(app):
        store.lock()
        thread=threading.Thread(target=store.run,args=(timeout,),daemon=True)
        app.state.worker=thread
        thread.start()
        try: yield
        finally:
            store.stop.set(); store.wakeup.set()
            with store.mutex:
                if store.active: store.cancel(store.active)
            thread.join(timeout=10)
            store.release()

    app=FastAPI(title='因序 · 因子研究与回测',version='0.6.1',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.jobs=store

    async def auth(request:Request):
        supplied=request.headers.get('authorization','')
        if supplied and hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()): return
        value=request.cookies.get(cookie_name,'')
        digest=hashlib.sha256(value.encode()).hexdigest()
        if value and sessions.get(digest,0)>time.time():
            if request.method not in ('GET','HEAD') and request.headers.get('x-pkuquant-request')!='1':
                raise HTTPException(403,'Browser request verification failed')
            if request.headers.get('sec-fetch-site')=='cross-site': raise HTTPException(403,'Cross-site request blocked')
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):
                raise HTTPException(403,'Origin mismatch')
            return
        raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Bearer'})

    @app.middleware('http')
    async def browser_headers(request:Request,call_next):
        response=await call_next(request)
        response.headers.setdefault('X-Content-Type-Options','nosniff')
        response.headers.setdefault('Referrer-Policy','no-referrer')
        response.headers.setdefault('Cache-Control','no-store')
        response.headers.setdefault('X-Frame-Options','DENY')
        response.headers.setdefault('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        return response

    @app.get('/')
    def workbench(): return FileResponse(static/'index.html')

    @app.get('/assets/{name}')
    def asset(name:str):
        if name not in ('app.css','app.js','evaluation.js','mark.svg'): raise HTTPException(404,'Asset not found')
        return FileResponse(static/name)

    @app.post('/session')
    async def login(request:Request):
        supplied=request.headers.get('authorization','')
        if not hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()): raise HTTPException(401,'访问密钥不正确')
        now=time.time()
        for key in [k for k,v in sessions.items() if v<=now]: sessions.pop(key,None)
        if len(sessions)>=100: raise HTTPException(429,'Active browser session limit reached')
        value=secrets.token_urlsafe(32); sessions[hashlib.sha256(value.encode()).hexdigest()]=now+8*3600
        response=JSONResponse({'authenticated':True})
        response.set_cookie(cookie_name,value,httponly=True,secure=request.url.scheme=='https',samesite='strict',max_age=8*3600,path='/')
        return response

    @app.delete('/session',dependencies=[Depends(auth)])
    async def logout(request:Request):
        value=request.cookies.get(cookie_name,'')
        sessions.pop(hashlib.sha256(value.encode()).hexdigest(),None)
        response=JSONResponse({'authenticated':False}); response.delete_cookie(cookie_name,path='/')
        return response

    @app.get('/ui/config',dependencies=[Depends(auth)])
    def ui_config(): return config_view(registry)

    @app.get('/ui/catalog')
    def ui_catalog(): return config_view({})

    @app.get('/health')
    def health():
        if not getattr(app.state,'worker',None) or not app.state.worker.is_alive(): raise HTTPException(503,'Worker unavailable')
        try:
            with store.connect() as c: c.execute('SELECT 1').fetchone()
        except sqlite3.Error: raise HTTPException(503,'Job store unavailable')
        return {'status':'ok','mode':'research_only'}

    @app.get('/sources',dependencies=[Depends(auth)])
    def source_list(): return {'sources':list(registry)}

    @app.post('/jobs',status_code=202,dependencies=[Depends(auth)])
    async def submit(request:Request):
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>16384: raise HTTPException(413,'Request too large')
        try:
            obj=json.loads(body)
            if not isinstance(obj,dict) or set(obj)-{'source','alpha','settings','evaluation'}: raise ValueError('Use source, alpha, settings and evaluation only')
            if obj.get('source') not in registry: raise ValueError('Unknown source ID')
            alpha=obj.get('alpha'); tree=Expression(alpha).tree
            for node in ast.walk(tree):
                if isinstance(node,ast.Constant) and isinstance(node.value,(float,int)) and (not -10000<=node.value<=10000):
                    raise ValueError('Expression constants exceed service resource limit')
            settings=Settings(**obj.get('settings',{}))
            for name in ('min_history','active_window','liquidity_window','top_n','retain_n','delay','decay','stale_haircut_after','valid_price_window'):
                if getattr(settings,name)>10000: raise ValueError('Settings exceed service resource limit')
            spec=dict(source=registry[obj['source']],alpha=alpha,settings=settings.to_dict())
            if 'evaluation' in obj:
                if not isinstance(obj['evaluation'],dict): raise ValueError('evaluation must be an object')
                spec['evaluation']=EvaluationSpec(**obj['evaluation']).to_dict()
                if spec['evaluation']['enabled']:
                    from .stability import validate_periods
                    validate_periods(EvaluationSpec(**spec['evaluation']),settings)
            idem=request.headers.get('idempotency-key')
            if idem and (len(idem)>128 or not idem.isascii()): raise ValueError('Invalid idempotency key')
            jid=store.submit(spec,idem,max_queue,max_jobs)
        except OverflowError as exc: raise HTTPException(429,str(exc))
        except (ValueError,TypeError,SyntaxError,RecursionError) as exc: raise HTTPException(422,str(exc))
        return store.get(jid)

    @app.get('/jobs',dependencies=[Depends(auth)])
    def listing():
        with store.connect() as c: ids=c.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
        return {'jobs':[store.get(r[0]) for r in ids]}

    def lookup(jid):
        if len(jid)!=32 or any(c not in '0123456789abcdef' for c in jid): raise HTTPException(404,'Job not found')
        job=store.get(jid)
        if not job: raise HTTPException(404,'Job not found')
        return job

    @app.get('/jobs/{jid}',dependencies=[Depends(auth)])
    def get(jid:str): return lookup(jid)

    @app.get('/jobs/{jid}/strategy',dependencies=[Depends(auth)])
    def strategy(jid:str):
        lookup(jid)
        with store.connect() as c: raw=c.execute('SELECT spec FROM jobs WHERE id=?',(jid,)).fetchone()[0]
        spec=json.loads(raw)
        source=next((key for key,value in registry.items() if value==spec['source']),None)
        return {'source':source,'alpha':spec['alpha'],'settings':spec['settings'],**({'evaluation':spec['evaluation']} if 'evaluation' in spec else {})}

    @app.get('/jobs/{jid}/result',dependencies=[Depends(auth)])
    def result(jid:str):
        if lookup(jid)['status']!='succeeded': raise HTTPException(409,'Job has no completed output')
        try: return result_view(store.root/jid/'result')
        except (OSError,ValueError,KeyError): raise HTTPException(409,'Stored result is unavailable or invalid')

    @app.post('/jobs/{jid}/cancel',dependencies=[Depends(auth)])
    def cancel(jid:str):
        lookup(jid); store.cancel(jid); return store.get(jid)

    @app.get('/jobs/{jid}/artifacts/{name}',dependencies=[Depends(auth)])
    def artifact(jid:str,name:str):
        job=lookup(jid)
        if name not in ARTIFACTS: raise HTTPException(404,'Artifact not found')
        if job['status']!='succeeded': raise HTTPException(409,'Job has no completed output')
        path=store.root/jid/'result'/name
        if not path.is_file() or not path.resolve().is_relative_to(store.root): raise HTTPException(404,'Artifact not found')
        return FileResponse(path,filename=name,headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store',
            'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; sandbox"})
    return app


def app_from_env():
    path=Path(os.environ.get('PKUQUANT_SERVICE_CONFIG','service.json')).resolve()
    config=json.loads(path.read_text(encoding='utf-8'))
    # Resolve deployment paths relative to configuration, independent of shell cwd.
    config['root']=str((path.parent/Path(config.get('root','service-data'))).resolve())
    for source in config['sources'].values():
        for key in source:
            if source[key] is not None: source[key]=str((path.parent/Path(source[key])).resolve())
    return create_app(**config)
