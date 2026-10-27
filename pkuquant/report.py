from pathlib import Path
from datetime import datetime, timezone
import hashlib
import html
import json
import zipfile
import platform
import os
import uuid
import importlib.metadata
import numpy as np
import pandas as pd


def metrics(sim,settings):
    d,o = sim.daily,sim.orders
    ret = d['return']
    curve = np.r_[settings.initial_cash,d.nav.to_numpy()]
    drawdown = curve/np.maximum.accumulate(curve)-1
    std = float(ret.std(ddof=1)) if len(ret)>1 else 0
    filled = o[o.filled_shares_equiv>0]
    return {
        'total_return':float(curve[-1]/curve[0]-1),
        'annual_return':float((curve[-1]/curve[0])**(settings.annualization/len(ret))-1),
        'annual_volatility':std*np.sqrt(settings.annualization),
        'sharpe_zero_rf':float(ret.mean()/std*np.sqrt(settings.annualization)) if std>0 else None,
        'max_drawdown':float(drawdown.min()), 'trading_days':len(d),
        'total_explicit_cost':float(d.costs.sum()), 'annual_turnover_two_sided':float(d.turnover.mean()*settings.annualization),
        'orders':len(o),'orders_with_fills':len(filled),
        'order_fill_rate':float(len(filled)/len(o)) if len(o) else None,
        'end_cash':float(d.cash.iloc[-1]), 'end_nav':float(d.nav.iloc[-1]),
        'end_stale_exposure':float(d.stale_exposure.iloc[-1]),
        'max_stale_exposure':float(d.stale_exposure.max()),
        'estimated_rule_orders':int((o.rule_source=='estimated_conservative').sum()),
        'reconstructed_rule_orders':int((o.rule_source=='reconstructed').sum()),
        'rejection_reasons':{str(k):int(v) for k,v in o[o.status!='filled'].reason.value_counts().items()},
    }


def _svg(values,color,baseline=None):
    y = np.asarray(values,dtype=float)
    lo,hi = float(min(y.min(),baseline if baseline is not None else y.min())),float(max(y.max(),baseline if baseline is not None else y.max()))
    span = max(hi-lo,1e-9)
    points = ' '.join(f'{35+i/max(1,len(y)-1)*930:.2f},{175-(v-lo)/span*140:.2f}' for i,v in enumerate(y))
    return f'<svg viewBox="0 0 1000 210" role="img" aria-label="时间序列曲线"><text x="4" y="20" fill="#94a3b8">{hi:.3f}</text><text x="4" y="200" fill="#94a3b8">{lo:.3f}</text><path d="M35 180 H975" stroke="#334155"/><polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/></svg>'


class Result:
    def __init__(self, sim,settings,metadata,diagnostics=None,evaluation=None):
        self.daily,self.orders,self.holdings,self.targets = sim.daily,sim.orders,sim.holdings,sim.targets
        self.settings,self.metadata = settings,metadata
        self.metrics = metrics(sim,settings)
        self.evaluation = evaluation
        self.metadata['evaluation'] = evaluation.summary if evaluation is not None else None
        self.diagnostics = diagnostics if diagnostics is not None else pd.DataFrame()
        self.metadata['factor_diagnostics'] = {
            col: {'mean':float(self.diagnostics[col].mean()) if self.diagnostics[col].notna().any() else None,
                  'valid_dates':int(self.diagnostics[col].notna().sum())}
            for col in self.diagnostics if col.startswith('rank_ic_')
        }

    def summary(self):
        return pd.Series(self.metrics)

    def save(self,path):
        path = Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        # Exclusive reservation prevents races. Failed output is never presented
        # as a complete report; the hidden staging directory is kept for diagnosis.
        lock=path.parent/('.'+path.name+'.lock')
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            if path.exists(): raise FileExistsError(path)
            stage=path.parent/('.'+path.name+'.pending-'+uuid.uuid4().hex)
            stage.mkdir()
            self._save_files(stage)
            stage.rename(path)
            return path.resolve()
        finally:
            os.close(fd)
            lock.unlink()

    def _save_files(self,path):
        for name in ('daily','orders','holdings','targets'):
            getattr(self,name).to_csv(path/f'{name}.csv',index=name=='daily',encoding='utf-8-sig')
        self.diagnostics.to_csv(path/'factor_diagnostics.csv',encoding='utf-8-sig')
        if self.evaluation is not None: self.evaluation.save(path)
        code_hash = hashlib.sha256()
        with zipfile.ZipFile(path/'source_code.zip','w',compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(Path(__file__).parent.glob('*.py')):
                content=file.read_bytes()
                code_hash.update(file.name.encode()); code_hash.update(content)
                archive.writestr(f'pkuquant/{file.name}',content)
            for name in ('run.py','pyproject.toml'):
                file=Path(__file__).parent.parent/name
                if file.exists(): archive.write(file,arcname=name)
        packages = {}
        for name in ('numpy','pandas','pyarrow'):
            try: packages[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError: packages[name] = 'unavailable'
        manifest = {**self.metadata,'settings':self.settings.to_dict(),'metrics':self.metrics,
                    'created_utc':datetime.now(timezone.utc).isoformat(),'code_sha256':code_hash.hexdigest(),
                    'python':platform.python_version(),'packages':packages}
        (path/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        (path/'report.html').write_text(self._html(),encoding='utf-8')
        files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in path.iterdir() if f.is_file()}
        (path/'checksums.json').write_text(json.dumps(files,indent=2),encoding='utf-8')

    def _html(self):
        m = self.metrics
        esc = html.escape
        def pct(x): return f'{x:.2%}'
        cards = [('总收益',pct(m['total_return'])),('年化收益',pct(m['annual_return'])),('最大回撤',pct(m['max_drawdown'])),
                 ('夏普（零无风险利率）',f"{m['sharpe_zero_rf']:.2f}" if m['sharpe_zero_rf'] is not None else '—'),
                 ('有成交订单比例',pct(m['order_fill_rate']) if m['order_fill_rate'] is not None else '—'),('显式费用',f"{m['total_explicit_cost']:,.0f}")]
        cards_html = ''.join(f'<div class="card"><span>{k}</span><strong>{v}</strong></div>' for k,v in cards)
        nav = np.r_[1,self.daily.nav.to_numpy()/self.settings.initial_cash]
        dd = nav/np.maximum.accumulate(nav)-1
        year = self.daily['return'].groupby(self.daily.index.year).apply(lambda x: (1+x).prod()-1).rename('收益').to_frame()
        year['收益'] = year['收益'].map(pct)
        reasons = pd.Series(m['rejection_reasons'],name='订单数').to_frame()
        ic = pd.DataFrame(self.metadata.get('factor_diagnostics',{})).T
        warnings = ''.join(f'<li>{esc(x)}</li>' for x in self.metadata['limitations'])
        return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>因序 回测报告</title><style>
body{{margin:0;background:#0c1424;color:#e2e8f0;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:1150px;margin:auto;padding:36px 24px}}h1{{margin:0;font-size:32px}}h2{{margin-top:30px;font-size:20px}}.muted,span{{color:#94a3b8}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}}.card,.panel{{background:#152137;border:1px solid #2a3a53;border-radius:12px;padding:18px}}strong{{display:block;font-size:26px;color:#5eead4}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}th,td{{padding:9px;border-bottom:1px solid #334155;text-align:left}}.notice{{border-left:4px solid #fbbf24;padding:12px 18px;background:#29251c}}svg{{width:100%}}@media(max-width:650px){{.cards{{grid-template-columns:repeat(2,1fr)}}strong{{font-size:20px}}}}
</style><main><div class="muted">因序 · 因子研究与回测</div><h1>因子研究与执行模拟</h1>
<p class="muted">{esc(str(self.daily.index[0].date()))} — {esc(str(self.daily.index[-1].date()))} · {len(self.daily)} 个回测日期 · 做多</p>
<div class="notice">研究级复权经济单位账户；不等同于实盘可实现收益。请结合以下假设与未解决仓位阅读结果。</div>
<div class="cards">{cards_html}</div><h2>策略表达式</h2><pre class="panel">{esc(self.metadata['expression'])}</pre>
<h2>净值（起点 1）</h2><div class="panel">{_svg(nav,'#5eead4',1)}</div>
<h2>回撤</h2><div class="panel">{_svg(dd,'#f59e0b',0)}</div>
<h2>分年度收益</h2>{year.to_html(escape=True)}<p class="muted">首尾年度可能为非完整年度；年度结果不是独立样本外检验。</p>
<h2>因子诊断：未来 1 / 5 / 20 日 Rank IC</h2>{ic.to_html(escape=True)}<p class="muted">仅用于事后研究的收盘到收盘标签，不代表可执行收益。缺失未来端点的样本排除并记录数量；多日标签重叠，未声称独立样本显著性。标签不进入策略数据接口。</p>
<h2>未成交及部分成交原因</h2>{reasons.to_html(escape=True)}
{self.evaluation.html() if self.evaluation is not None else '<h2>因子评价</h2><p>未启用评价。</p>'}
<p>期末停滞行情持仓估值：{m['end_stale_exposure']:,.2f}；历史最大停滞行情持仓估值：{m['max_stale_exposure']:,.2f}。未强制假设清仓。</p>
<h2>模型假设与数据限制</h2><ul>{warnings}</ul>
<h2>配置</h2><pre class="panel">{esc(json.dumps(self.settings.to_dict(),ensure_ascii=False,indent=2))}</pre>
<p class="muted">完整记录：daily.csv · orders.csv · holdings.csv · targets.csv · manifest.json。滑点已计入成交价，显式费用不含独立滑点现金扣款。</p></main></html>'''
