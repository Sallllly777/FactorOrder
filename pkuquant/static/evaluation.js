'use strict';
// All user-controlled expressions and evidence are rendered as text nodes.
let evaluationDraft = {};
function applyEvaluation(spec) {
  evaluationDraft={...state.config.evaluation_defaults,...(spec||{})};
  for(const input of document.querySelectorAll('[data-eval]')) {
    const value=evaluationDraft[input.dataset.eval];
    if(input.type==='checkbox')input.checked=Boolean(value);
    else input.value=Array.isArray(value)?(input.dataset.eval==='peers'?JSON.stringify(value,null,2):value.join(', ')):value??'';
  }
}
function getEvaluation() {
  const spec={...evaluationDraft};
  for(const input of document.querySelectorAll('[data-eval]')) {
    const key=input.dataset.eval;
    spec[key]=input.type==='checkbox'?input.checked:input.type==='number'||['direction','primary_horizon'].includes(key)?(input.value===''?null:Number(input.value)):input.value;
    if(input.type==='date')spec[key]=input.value||null;
    if(key==='horizons')spec[key]=input.value.split(',').map(x=>Number(x.trim()));
    if(key==='peers'){
      try{spec[key]=JSON.parse(input.value||'[]');}catch{throw new Error('同伴因子需填写 JSON 字符串数组。');}
      if(!Array.isArray(spec[key])||spec[key].length>3||spec[key].some(x=>typeof x!=='string'))throw new Error('同伴因子需为最多三个表达式组成的 JSON 字符串数组。');
    }
  }
  return spec;
}
function renderStability(root,stability) {
  root.append(node('h2','训练—验证稳定性'));
  if(!stability){root.append(node('p','此历史报告尚未包含训练—验证评价，权重遵循原评分版本；重新运行后可使用新规则。','quality-notice'));return;}
  if(stability.status==='not_configured'){
    root.append(node('p','尚未配置训练与验证区间；样本外稳定性6分未评价。请在评价协议中填写四个日期，且整体回测区间需覆盖两段。','quality-notice'));return;
  }
  root.append(node('p','固定训练参考，与验证整体及每个验证年份比较；评分取最弱一项。各区间独立从相同现金开始，边界外标签剔除。声明区间不等于独立样本外认证。','quality-notice'));
  evaluationTable(root,['区间','请求日期','实际数据日期','交易日数','边界剔除信号日','成熟标签覆盖率'],stability.periods.map(p=>[p.name==='train'?'训练':p.name==='validation'?'验证整体':p.name,`${p.start} ～ ${p.end}`,`${p.effective_start??'—'} ～ ${p.effective_end??'—'}`,p.calendar_dates,p.purged_signal_dates,pct(p.matured_label_coverage)]));
  const value=(v,row)=>['correlation','ratio'].includes(row.unit)?fmt(v,4):pct(v);
  evaluationTable(root,['对照','指标','训练值','验证值','变化（验证−训练）','绝对差值','退化幅度','保留率','等级 / 4','评分依据'],stability.comparisons.map(r=>[r.comparison==='validation'?'验证整体':r.comparison,r.label,value(r.train_value,r),value(r.validation_value,r),value(r.delta,r),value(r.absolute_gap,r),value(r.deterioration,r),r.retention===null?r.retention_reason:pct(r.retention),r.grade??'—',r.grade_reason]));
  const details=node('details',undefined,'assumptions');details.append(node('summary','均值差的95% HAC区间与区间样本数'));
  evaluationTable(details,['对照','指标','训练 n','验证 n','训练均值区间','验证均值区间','差值区间'],stability.comparisons.filter(r=>['rank_ic','top_excess','mean_daily_active'].includes(r.metric)).map(r=>[r.comparison,r.label,r.train_n,r.validation_n,`${value(r.train_ci_low,r)} ～ ${value(r.train_ci_high,r)}`,`${value(r.validation_ci_low,r)} ～ ${value(r.validation_ci_high,r)}`,`${value(r.delta_ci_low,r)} ～ ${value(r.delta_ci_high,r)}`]));
  const notes=node('ul');for(const text of stability.notes)notes.append(node('li',text));details.append(notes);root.append(details);
}
function evaluationTable(parent,headers,rows) {
  const wrap=node('div',undefined,'table-wrap'),table=node('table'),thead=node('thead'),tr=node('tr');
  for(const h of headers)tr.append(node('th',h));thead.append(tr);table.append(thead);
  const tbody=node('tbody');for(const row of rows){const r=node('tr');for(const cell of row)r.append(node('td',cell??'未评价'));tbody.append(r);}
  table.append(tbody);wrap.append(table);parent.append(wrap);
}
function observedEvidence(value) {
  if(value===null||value===undefined)return '—';
  if(typeof value==='number')return fmt(value,4);
  if(Array.isArray(value))return value.map(observedEvidence).join(' / ');
  if(Array.isArray(value.comparisons))return value.comparisons.map(x=>`${x.comparison}: ${x.grade??'未评价'}`).join('；')||'未配置';
  if(Object.hasOwn(value,'mean'))return `均值 ${fmt(value.mean,4)}；HAC ${fmt(value.ci_low,4)} ～ ${fmt(value.ci_high,4)}；n=${value.n}`;
  return JSON.stringify(value);
}
function renderEvaluation(result) {
  const root=$('evaluation-content');root.replaceChildren();
  const e=result?.evaluation;
  if(!e){root.append(node('p',result?'此记录未启用因子评价，或来自旧版报告。载入策略并重新运行可生成评价。':'运行回测后，在这里审阅评价证据、分项得分和压力测试。','quality-notice'));return;}
  const s=e.score,headline=node('div',undefined,'evaluation-headline');
  headline.append(node('strong',`${fmt(s.earned_points,1)} / ${s.evaluated_weight}`),node('span','已评项目得分 / 已评权重（满权重 100）'));
  root.append(headline,node('p',`${e.status==='insufficient_evidence'?'样本不足':'研究评价'} · ${s.priority} · 总分：${s.total===null?'未发布':fmt(s.total,1)}`,'quality-notice'));
  root.append(node('p','评分版本：'+s.version+'；不同版本的分数不可直接比较。','subtle'));
  root.append(node('p',`主期限 ${e.protocol.evaluation.primary_horizon} 日；信号后第 ${e.label.entry_offset} 日开盘进入，持有至第 delay + horizon 日开盘。Top-${e.label.top_n}；${e.label.direction===1?'高值优先':'低值优先'}。分层收益为信号诊断，实际交易见回测。`,'subtle'));
  const protocol=node('details',undefined,'assumptions');protocol.append(node('summary','评价协议与证据声明'));
  protocol.append(node('p',`研究族：${e.evidence.family||'未声明'} · 累计试验数：${e.evidence.declared_trials??'未知'} · 证据：${e.evidence.declared}（未经独立验证）`));
  protocol.append(node('p','协议 SHA-256：'+e.protocol_sha256),node('pre',JSON.stringify(e.protocol,null,2)));root.append(protocol);
  protocol.append(node('p','可比协议 SHA-256（排除研究族、试验数、证据声明）：'+e.comparison_sha256));
  if(e.gate_reasons?.length)root.append(node('p','评价门槛未通过：'+e.gate_reasons.join('；'),'quality-notice'));
  root.append(node('h2','分项评分与依据'));
  evaluationTable(root,['分项','权重','等级 / 4','得分','观测值','规则'],s.items.map(x=>[x.label,x.weight,x.grade??'未评价',x.points===null?'—':fmt(x.points,2),observedEvidence(x.observed),x.reason]));
  renderStability(root,e.stability);
  const series=result.evaluation_series||[];
  if(series.length){
    root.append(node('h2','主期限逐日 Rank IC'));
    const svg=svgNode('svg',{viewBox:'0 0 900 200',role:'img','aria-label':'主期限逐日Rank IC；纵轴负1至1'});
    svg.append(svgNode('line',{x1:35,x2:880,y1:90,y2:90,stroke:'#64756a','stroke-dasharray':'4 4'}));
    let path='',connected=false;
    series.forEach((v,i)=>{if(v.rank_ic===null){connected=false;return;}const x=35+i/Math.max(1,series.length-1)*845,y=90-v.rank_ic*75;path+=(connected?' L':' M')+x+' '+y;connected=true;});
    svg.append(svgNode('path',{d:path,fill:'none',stroke:'#c6ef72','stroke-width':1.5}));
    for(const [v,y] of [['+1',15],['0',90],['−1',165]])svg.append(svgNode('text',{x:2,y,fill:'#94ad9b','font-size':12},v));
    svg.append(svgNode('text',{x:35,y:195,fill:'#94ad9b','font-size':12},series[0].date),svgNode('text',{x:880,y:195,fill:'#94ad9b','font-size':12,'text-anchor':'end'},series.at(-1).date));
    root.append(svg,node('p',result.evaluation_series_sampled?'曲线抽样至2500点；缺失日期在抽样后可能未显示，完整逐日样本请下载CSV。':'缺失值处断线；曲线使用全部日期，置信区间请结合下表。','subtle'));
  }
  root.append(node('h2','期限诊断 · 95% HAC 区间'));
  evaluationTable(root,['持有期','有效 IC 日','平均 Rank IC','HAC 区间','Top-N 同池超额','IC 正值日期'],Object.entries(e.horizons).map(([h,v])=>[h+' 日',v.rank_ic.n,fmt(v.rank_ic.mean,4),`${fmt(v.rank_ic.ci_low,4)} ～ ${fmt(v.rank_ic.ci_high,4)}`,pct(v.top_excess.mean),pct(v.rank_ic.positive_fraction)]));
  root.append(node('h2','样本审计 · 证券日数'));
  evaluationTable(root,['期限','可选样本','有限信号','有效标签','右端截断','内部缺失端点','非有限信号'],Object.entries(e.horizons).map(([h,v])=>[h+' 日',v.audit.eligible,v.audit.finite_scores,v.audit.label_count,v.audit.right_censored,v.audit.missing_endpoints,v.audit.nonfinite_scores]));
  root.append(node('h2','分层收益 · 每日均值的时间平均'));
  evaluationTable(root,['期限','组号（高分靠后）','均值收益','有效日期','分配样本','有效标签'],(e.quantile_profile||[]).map(x=>[x.horizon+' 日',x.quantile,pct(x.mean_return),x.valid_dates,x.assigned,x.valid]));
  root.append(node('h2','对照与压力测试'));
  const scenarioLabels={liquidity_benchmark:'流动性基线',cost_x2:'费用与滑点 ×2',delay_plus_1:'延迟 +1 日',capital_x2:'资金 ×2',smoothing_2d:'信号平滑 2 日',peer_basket:'同伴固定组合',peer_basket_plus_candidate:'同伴组合 + 候选'};
  evaluationTable(root,['场景','状态','年化收益','最大回撤','夏普'],(e.scenarios||[]).map(x=>[scenarioLabels[x.name]||x.name,x.status==='evaluated'?'已评':'样本不足',pct(x.annual_return),pct(x.max_drawdown),fmt(x.sharpe_zero_rf)]));
  if(!e.protocol.evaluation.stress_tests)root.append(node('p','本次未启用压力测试，相关分项保留为未评价。可在评价协议中启用后重新运行。','subtle'));
  root.append(node('h2','年度诊断'));
  evaluationTable(root,['年度','期限','有效 IC 日','平均 Rank IC','Top-N 超额'],(e.yearly||[]).map(x=>[x.year,x.horizon+' 日',x.valid_ic_dates,fmt(x.rank_ic,4),pct(x.top_excess)]));
  if(e.peers.length){root.append(node('h2','同伴信号、收益与持仓重复度'));evaluationTable(root,['同伴表达式','平均信号相关','绝对信号相关均值','日收益相关','股票持仓交并比'],e.peers.map(p=>[p.expression,fmt(p.signal_correlation,4),fmt(p.signal_absolute_correlation,4),fmt(p.return_correlation,4),pct(p.holdings_overlap)]));}
  const limitations=node('ul');for(const text of e.limitations)limitations.append(node('li',text));root.append(node('h2','证据缺口与适用边界'),limitations);
  const downloads=node('div',undefined,'downloads');
  for(const [file,label] of [['evaluation.json','完整评价 JSON'],['evaluation_daily.csv','逐日诊断 CSV'],['evaluation_quantiles.csv','分层收益 CSV'],['evaluation_yearly.csv','年度诊断 CSV'],['evaluation_scenarios.csv','压力场景 CSV']]){
    const b=node('button',label+' ↓','secondary');b.onclick=safeAction(()=>downloadArtifact(file));downloads.append(b);
  }
  if(e.stability){for(const [file,label] of [['evaluation_stability.csv','训练验证对照 CSV'],['evaluation_period_returns.csv','独立区间净值 CSV']]){
    const b=node('button',label+' ↓','secondary');b.onclick=safeAction(()=>downloadArtifact(file));downloads.append(b);
  }}
  root.append(downloads);
}
