'use strict';
const $ = (id) => document.getElementById(id);
const state = {config:null, connected:false, activeJob:null, resultJob:null, result:null, chart:'nav', timer:null, jobs:[], selectedTemplate:null, selection:0, pending:null, toastTimer:null,preferredSource:null,submitting:false};
const statuses = {queued:'排队中',running:'运行中',succeeded:'已完成',failed:'失败',cancelled:'已取消',timed_out:'已超时',interrupted:'已中断'};
const fields = {
  initial_cash:['初始资金（元）','number','portfolio',100,100],
  universe:['股票池','select','portfolio',{mainboard:'沪深主板',all:'沪深全部 A 股'}],
  top_n:['目标持仓数','number','portfolio',1,1],retain_n:['保留排名上限','number','portfolio',1,1],
  rebalance:['调仓频率','select','portfolio',{weekly:'每周',daily:'每日',monthly:'每月'}],
  max_weight:['单只目标权重上限','number','portfolio',.001,.01,1],
  gross_exposure:['组合目标仓位','number','portfolio',.001,.01,1],
  sizing:['股数计划时点','select','execution',{previous_close:'前收固定计划',execution_open:'开盘重算（旧研究假设）'}],
  capacity:['容量计算','select','execution',{lagged_adv:'前日均额固定容量',daily_min:'当日总额限制（旧研究假设）'}],
  round_fees:['费用按分四舍五入','checkbox','execution'],
  delay:['信号延迟（交易日）','number','execution',1,1],
  slippage_bps:['单边滑点（bps）','number','execution',0,1],
  commission:['佣金比例','number','execution',0,.0001],min_commission:['最低佣金（元）','number','execution',0,1],
  st_policy:['已知 ST 股票','select','risk',{exclude:'排除 ST / *ST',allow:'允许参与研究'}],
  unknown_st:['ST 状态缺失时','select','risk',{allow:'允许（研究假设）',exclude:'排除未知状态',error:'停止并报错'}],
  require_verified_rules:['要求完整的逐日交易规则','checkbox','risk'],liquidate_st:['持仓变 ST 后逐日尝试退出','checkbox','risk'],
  require_trade_counts:['要求有效成交笔数','checkbox','risk'],
  amount_participation:['成交金额参与率','number','risk',.000001,.001,1],volume_participation:['成交股数参与率','number','risk',.000001,.001,1],
  min_history:['最少历史交易日','number','advanced',1,1],active_window:['连续活跃窗口','number','advanced',1,1],
  valid_price_window:['连续有效价格窗口','number','advanced',1,1],
  min_price:['最低收盘价（元）','number','advanced',0,.01],min_avg_amount:['最低日均成交额（元）','number','advanced',0,1000000],
  liquidity_window:['流动性窗口','number','advanced',1,1],liquidity_quantile:['流动性排名门槛','number','advanced',0,.05,.999],
  decay:['信号衰减窗口','number','advanced',1,1],transfer_rate:['其他过户费用率','number','advanced',0,.00001],
  stamp_tax:['计入历史卖出印花税','checkbox','advanced'],amount_multiplier:['成交金额单位乘数','number','advanced',.000001,.1],
  stale_haircut_after:['停滞估值折扣起始日','number','advanced',1,1],stale_haircut:['停滞估值折扣比例','number','advanced',0,.05,1],
  annualization:['年化交易日数','number','advanced',1,1]
};
function node(tag, text, className) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(className)e.className=className; return e; }
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(state.toastTimer); state.toastTimer=setTimeout(()=>{$('toast').hidden=true;},3200); }
function error(message) { $('error-message').textContent=message; $('error-message').hidden=false; }
function showLogin() { $('login-error').textContent=''; if(!$('login-dialog').open)$('login-dialog').showModal(); }
function setConnected(value) { state.connected=value; $('connect-button').textContent=value?'已连接服务':'连接服务 ↗'; $('connection-dot').classList.toggle('connected',value); $('logout-button').hidden=!value; $('run-button').disabled=!value||Boolean(state.activeJob); }
async function api(path, options={}) {
  const response=await fetch(path,{credentials:'same-origin',...options,headers:{'X-PKUQuant-Request':'1',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
  if(!response.ok) {
    let detail; try { const body=await response.json(); detail=typeof body.detail==='string'?body.detail:JSON.stringify(body.detail); } catch { detail=`服务返回 ${response.status}`; }
    if(response.status===401 && path!=='/session') { setConnected(false); clearTimeout(state.timer); }
    throw new Error(response.status===401?'请连接服务后继续。':detail||`请求失败 (${response.status})`);
  }
  return response.json();
}
function safeAction(fn) { return async (...args)=>{try {await fn(...args);}catch(e){error(e.message||'操作失败，请重试。');}}; }
function view(name) {
  document.querySelectorAll('[data-page]').forEach(e=>{e.hidden=e.dataset.page!==name;});
  document.querySelectorAll('[data-view]').forEach(e=>{const active=e.dataset.view===name;e.classList.toggle('active',active);if(active)e.setAttribute('aria-current','page');else e.removeAttribute('aria-current');});
  $('page-label').textContent={studio:'因子工作台',history:'回测记录',operators:'算子手册'}[name];
  if(name==='history'&&state.connected) refreshHistory().catch(e=>error(e.message));
}
function buildFields() {
  for(const [name,[label,type,group,min,step,max]] of Object.entries(fields)) {
    const wrap=node('label',undefined,'field'+(type==='checkbox'?' check-field':''));
    const input=node(type==='select'?'select':'input');input.name=name;
    if(type==='select') {for(const [value,text] of Object.entries(min)){const option=node('option',text);option.value=value;input.append(option);}}
    else {input.type=type;if(type==='number'){input.required=true;input.min=min;input.step=Number.isInteger(state.config?.defaults?.[name])?'1':'any';if(['top_n','retain_n','delay','min_history','active_window','liquidity_window','decay','stale_haircut_after','annualization'].includes(name))input.step='1';if(max!==undefined)input.max=max;}}
    if(type==='checkbox')wrap.append(input,node('span',label)); else wrap.append(document.createTextNode(label),input);
    $(group+'-fields').append(wrap);
  }
}
function applySettings(settings) {
  const merged={...state.config.defaults,...settings};
  for(const [name,value] of Object.entries(merged)) {
    const input=$('settings-form').elements.namedItem(name); if(!input)continue;
    if(input.type==='checkbox')input.checked=Boolean(value); else input.value=value===null?'':value;
  }
}
function getSettings() {
  const result={...state.config.defaults};
  for(const name of ['start','end',...Object.keys(fields)]) {
    const input=$('settings-form').elements.namedItem(name);
    result[name]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value||null;
  }
  return result;
}
function updateEditor() { const lines=$('alpha').value.split('\n').length; $('line-numbers').textContent=Array.from({length:lines},(_,i)=>i+1).join('\n'); }
function saveDraft() {
  if(!state.config)return;
  try {localStorage.setItem('pkuquant-draft-v1',JSON.stringify({alpha:$('alpha').value,settings:getSettings(),source:$('source').value}));$('draft-status').textContent='草稿已保存';}
  catch {$('draft-status').textContent='草稿仅保留在当前页';}
}
function chooseTemplate(template) {
  if(template.settings)applySettings(template.settings);
  $('alpha').value=template.alpha;$('template-description').textContent=template.description;state.selectedTemplate=template.id;
  document.querySelectorAll('.template-button').forEach(e=>e.classList.toggle('active',e.dataset.template===template.id));
  updateEditor();saveDraft();
}
function fillCatalog(config) {
  state.config=config;
  $('templates').replaceChildren();
  for(const template of config.templates){const b=node('button',template.name,'template-button');b.dataset.template=template.id;b.type='button';b.onclick=()=>chooseTemplate(template);$('templates').append(b);}
  $('operator-list').replaceChildren();
  for(const [signature,description] of config.operators){const row=node('div',undefined,'operator-row');row.append(node('code',signature),node('p',description));$('operator-list').append(row);}
}
function fillSources(sources,desired) {
  $('source').replaceChildren();
  for(const source of sources){const option=node('option',source.id+(source.synthetic?' · 合成示例':''));option.value=source.id;$('source').append(option);}
  if(desired&&sources.some(s=>s.id===desired))$('source').value=desired;
  updateSourceDescription();
}
function updateSourceDescription() {
  const source=state.config.sources.find(s=>s.id===$('source').value);
  $('source-description').textContent=source?(source.synthetic?'合成数据，仅供软件与接口验证':source.kind+(source.provider?' · '+source.provider:'')):'请先连接研究服务';
}
async function connect() {
  const draftSource=$('source').value||state.preferredSource;
  const config=await api('/ui/config');fillCatalog(config);fillSources(config.sources,draftSource);setConnected(true);
  await refreshHistory();
  if(!state.activeJob && !state.resultJob) {
    const running=state.jobs.find(j=>['running','queued'].includes(j.status));
    if(running) {state.activeJob=running.id;jobStatus(running);schedulePoll();}
    else {const latest=state.jobs.find(j=>j.status==='succeeded');if(latest)await selectResult(latest.id);}
  }
}
function readDraft() {try {const raw=localStorage.getItem('pkuquant-draft-v1');return raw?JSON.parse(raw):null;}catch{return null;} }
function validateDraft(draft) {
  if(!draft||typeof draft!=='object'||typeof draft.alpha!=='string'||draft.alpha.length>6000)throw new Error('策略文件需包含不超过 6000 字符的 alpha 表达式。');
  if(draft.settings!==undefined&&(typeof draft.settings!=='object'||draft.settings===null||Array.isArray(draft.settings)))throw new Error('settings 必须是配置对象。');
  for(const [name,value] of Object.entries(draft.settings||{})) {
    if(!Object.hasOwn(state.config.defaults,name))throw new Error('未知参数：'+name);
    if(name==='execution'&&value!=='conservative_daily')throw new Error('当前仅支持日频近似执行。');
    if(['start','end'].includes(name)){if(value!==null&&(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)))throw new Error('日期需为 YYYY-MM-DD。');continue;}
    const ref=state.config.defaults[name];if(typeof value!==typeof ref||typeof value==='number'&&!Number.isFinite(value))throw new Error('参数类型不正确：'+name);
    if(fields[name]?.[1]==='select'&&!(value in fields[name][3]))throw new Error('参数选项不正确：'+name);
  }
}
function setDraft(draft) { validateDraft(draft);$('alpha').value=draft.alpha;applySettings(draft.settings||{});if(state.config.sources.some(s=>s.id===draft.source))$('source').value=draft.source;updateSourceDescription();updateEditor();state.selectedTemplate=null;document.querySelectorAll('.template-button').forEach(e=>e.classList.remove('active'));$('template-description').textContent='自定义策略 · 参数与表达式已载入';saveDraft(); }
function jobStatus(job) {
  const active=['queued','running'].includes(job.status);
  $('run-indicator').classList.toggle('busy',active);$('cancel-job').hidden=!active;
  $('run-button').disabled=!state.connected||active;
  $('run-status-title').textContent={queued:'任务已提交，等待执行',running:'正在计算因子并逐日模拟',succeeded:'回测完成',cancelled:'任务已取消',failed:'回测未能完成',timed_out:'任务运行超时',interrupted:'服务重启，任务已中断'}[job.status]||job.status;
  $('run-status-detail').textContent=`任务 ${job.id.slice(0,10)} · `+(job.status==='running'?'耗时取决于数据规模，请稍候。':job.status==='queued'?'任务会在可用时自动开始。':job.error||'可在回测记录中复查。');
}
async function run() {
  view('studio');$('error-message').hidden=true;
  if(!state.connected){showLogin();return;}if(state.activeJob||state.submitting)return;
  if(!$('settings-form').checkValidity()){setSettingsExpanded(true);$('settings-form').querySelectorAll('details').forEach(e=>e.open=true);}
  if(!$('settings-form').reportValidity())return;
  if(!$('alpha').value.trim())throw new Error('请先编写策略表达式。');
  const settings=getSettings();if(settings.start>settings.end)throw new Error('开始日期不能晚于结束日期。');if(settings.retain_n<settings.top_n)throw new Error('保留排名上限不能小于目标持仓数。');
  const body=JSON.stringify({source:$('source').value,alpha:$('alpha').value,settings});
  // Reuse a key after a network failure with the same payload, avoiding duplicate jobs.
  if(!state.pending||state.pending.body!==body)state.pending={body,key:crypto.randomUUID()};
  $('run-button').disabled=true;state.submitting=true;
  try {
    const job=await api('/jobs',{method:'POST',body,headers:{'Idempotency-Key':state.pending.key}});state.pending=null;state.activeJob=job.id;jobStatus(job);saveDraft();await refreshHistory();schedulePoll();
  } finally {state.submitting=false;if(!state.activeJob)$('run-button').disabled=!state.connected;}
}
function schedulePoll() {clearTimeout(state.timer);state.timer=setTimeout(poll,1500);}
async function poll() {
  if(!state.activeJob||!state.connected)return;
  const jid=state.activeJob;
  try {
    const job=await api('/jobs/'+jid);if(jid!==state.activeJob)return;jobStatus(job);
    if(['queued','running'].includes(job.status)){schedulePoll();return;}
    state.activeJob=null;await refreshHistory();
    if(job.status==='succeeded')await selectResult(jid);else if(job.error)error(job.error);
  } catch(e){error('状态更新失败：'+e.message);if(state.connected)schedulePoll();}
}
async function refreshHistory() {
  const response=await api('/jobs');state.jobs=response.jobs;$('job-count').textContent=state.jobs.length;
  $('history-empty').hidden=state.jobs.length>0;$('history-table').hidden=!state.jobs.length;$('history-rows').replaceChildren();
  for(const job of state.jobs){
    const tr=node('tr');tr.append(node('td',job.id.slice(0,10)),node('td',new Date(job.created*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})));
    const status=node('td');status.append(node('span',statuses[job.status]||job.status,'status-pill '+job.status));tr.append(status);
    tr.append(node('td',Math.max(0,Math.round(job.updated-job.created))+' 秒'));
    const actions=node('td');
    if(job.status==='succeeded'){const b=node('button','查看结果 ↗','text-button');b.onclick=safeAction(async()=>{view('studio');await selectResult(job.id);$('results-title').scrollIntoView({block:'start',behavior:'smooth'});});actions.append(b);}
    const reuse=node('button','复用策略','quiet');reuse.onclick=safeAction(()=>reuseJob(job.id));actions.append(reuse);
    if(['running','queued'].includes(job.status)){const cancel=node('button','取消','quiet');cancel.onclick=safeAction(async()=>{await api(`/jobs/${job.id}/cancel`,{method:'POST'});await refreshHistory();if(state.activeJob===job.id)await poll();});actions.append(cancel);}
    tr.append(actions);$('history-rows').append(tr);
  }
}
async function reuseJob(jid){const strategy=await api(`/jobs/${jid}/strategy`);if(!strategy.source)throw new Error('该任务的数据源已被管理员移除，请选择新的数据源。');setDraft(strategy);view('studio');toast('已载入该次回测的表达式和完整参数');}
function fmt(value,digits=2){return value===null||value===undefined||!Number.isFinite(value)?'—':new Intl.NumberFormat('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(value);}
function pct(value){return value===null||value===undefined?'—':fmt(value*100)+'%';}
function addPair(dl,label,value){dl.append(node('dt',label),node('dd',value));}
async function selectResult(jid) {
  const sequence=++state.selection;
  const result=await api(`/jobs/${jid}/result`);if(sequence!==state.selection)return;
  state.result=result;state.resultJob=jid;
  $('result-empty').hidden=true;$('equity-chart').removeAttribute('hidden');$('result-toolbar').hidden=false;$('result-expression').hidden=false;$('result-detail').hidden=false;
  $('result-expression').textContent=result.expression;
  const m=result.metrics;$('result-period').textContent=result.curve.length?`${result.curve[0].date} — ${result.curve.at(-1).date} · ${result.daily_count} 日`:'无曲线数据';
  $('result-id').textContent='任务 '+jid.slice(0,10);$('result-kind').textContent=result.synthetic?'合成数据 · 软件验证':'研究回测';$('result-kind').classList.toggle('synthetic',result.synthetic);
  const cards=[['总收益',pct(m.total_return),'所选回测区间',m.total_return],['年化收益',pct(m.annual_return),`${result.settings.annualization} 个交易日`,m.annual_return],['夏普比率',fmt(m.sharpe_zero_rf),'零无风险利率',m.sharpe_zero_rf],['最大回撤',pct(m.max_drawdown),'含初始资金基准',m.max_drawdown]];
  $('metrics').replaceChildren();for(const [label,value,note,n] of cards){const card=node('div');card.append(node('span',label),node('strong',value,n===null?'':n>=0?'positive':'negative'),node('small',note));$('metrics').append(card);}
  $('execution-summary').replaceChildren();
  for(const [label,value] of [['交易意图',fmt(m.orders,0)+' 条'],['有成交订单',fmt(m.orders_with_fills,0)+' 条'],['订单成交率',pct(m.order_fill_rate)],['显式费用',fmt(m.total_explicit_cost)+' 元'],['期末现金',fmt(m.end_cash)+' 元'],['估计规则订单',fmt(m.estimated_rule_orders,0)+' 条']])addPair($('execution-summary'),label,value);
  $('ic-summary').replaceChildren();for(const n of [1,5,20]){const ic=result.factor_diagnostics['rank_ic_'+n+'d'];addPair($('ic-summary'),n+' 日平均 Rank IC',ic?fmt(ic.mean,4):'—');}
  addPair($('ic-summary'),'诊断口径','事后收盘到收盘');
  const cov=result.st_coverage;let notice=`历史 ST 状态覆盖率：${pct(cov.coverage)}。`;
  if(cov.unknown_cells>0)notice+=` 有 ${fmt(cov.unknown_cells,0)} 个证券日状态未知；当前策略设置为“${{allow:'允许未知',exclude:'排除未知',error:'未知时报错'}[result.settings.unknown_st]}”。`;
  if(m.estimated_rule_orders>0)notice+=' 本次部分订单采用估计交易规则。';
  if(result.synthetic)notice='这份结果来自合成行情和虚构状态，仅用于软件验证。 '+notice;
  $('quality-notice').textContent=notice;
  $('limitations').replaceChildren(...result.limitations.map(text=>node('li',text)));
  $('curve-note').textContent=result.curve_sampled?'图表经过抽样，指标按全部交易日计算。':'图表展示本次回测的全部交易日；净值包含费用与成交滑点。';
  drawChart();
}
function svgNode(tag,attrs,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e;}
function drawChart() {
  if(!state.result)return;const data=state.result.curve;const svg=$('equity-chart');svg.replaceChildren();if(!data.length)return;
  const isNav=state.chart==='nav';const values=data.map(p=>p[state.chart]);let lo=Math.min(...values,isNav?1:0),hi=Math.max(...values,isNav?1:0);const span=Math.max(hi-lo,.01);lo-=span*.12;hi+=span*.12;
  const x=i=>54+i/Math.max(1,data.length-1)*816;const y=v=>222-(v-lo)/(hi-lo)*196;
  const defs=svgNode('defs',{}),gradient=svgNode('linearGradient',{id:'curve-gradient',x1:'0',y1:'0',x2:'0',y2:'1'});gradient.append(svgNode('stop',{offset:'0%','stop-color':isNav?'#c6ef72':'#e7ab87','stop-opacity':'.16'}),svgNode('stop',{offset:'100%','stop-color':'#18211b','stop-opacity':'0'}));defs.append(gradient);svg.append(defs);
  for(let i=0;i<5;i++){const v=lo+(hi-lo)*i/4,yy=y(v);svg.append(svgNode('line',{x1:54,y1:yy,x2:870,y2:yy,stroke:'#2b3b2f','stroke-dasharray':'3 5'}));svg.append(svgNode('text',{x:42,y:yy+3,fill:'#6a8773','font-size':9,'text-anchor':'end','font-family':'Consolas,monospace'},isNav?v.toFixed(2):(v*100).toFixed(1)+'%'));}
  const line=values.map((v,i)=>(i?'L':'M')+x(i).toFixed(2)+' '+y(v).toFixed(2)).join(' ');
  svg.append(svgNode('path',{d:line+` L870 222 L54 222 Z`,fill:'url(#curve-gradient)'}));svg.append(svgNode('path',{d:line,fill:'none',stroke:isNav?'#c6ef72':'#e7ab87','stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round'}));
  for(const i of [...new Set([0,Math.floor((data.length-1)/2),data.length-1])])svg.append(svgNode('text',{x:x(i),y:248,fill:'#6a8773','font-size':9,'text-anchor':i===0?'start':i===data.length-1?'end':'middle','font-family':'Consolas,monospace'},data[i].date));
  const hover=svgNode('line',{x1:0,y1:22,x2:0,y2:222,stroke:'#c0d6a0','stroke-width':1,'stroke-dasharray':'3 3',visibility:'hidden'});svg.append(hover);
  svg.onpointermove=event=>{const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*900;const i=Math.max(0,Math.min(data.length-1,Math.round((px-54)/816*(data.length-1))));hover.setAttribute('x1',x(i));hover.setAttribute('x2',x(i));hover.setAttribute('visibility','visible');$('chart-tooltip').hidden=false;$('chart-tooltip').textContent=data[i].date+' · '+(isNav?'净值 '+fmt(data[i].nav,4):'回撤 '+pct(data[i].drawdown));};
  svg.onpointerleave=()=>{hover.setAttribute('visibility','hidden');$('chart-tooltip').hidden=true;};
  $('chart-legend-label').textContent=isNav?'净值基准 1.00':'相对历史最高净值';
}
function downloadBlob(blob,filename){const url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);}
async function downloadArtifact(name){if(!state.resultJob)return;const response=await fetch(`/jobs/${state.resultJob}/artifacts/${name}`,{credentials:'same-origin'});if(!response.ok)throw new Error('下载失败，请确认会话有效后重试。');downloadBlob(await response.blob(),state.resultJob.slice(0,10)+'-'+name);}

document.querySelectorAll('[data-view]').forEach(e=>e.onclick=()=>view(e.dataset.view));
$('show-operators').onclick=()=>view('operators');$('connect-button').onclick=()=>{if(state.connected)toast('已连接私有研究服务');else showLogin();};$('close-login').onclick=()=>$('login-dialog').close();
$('login-form').onsubmit=async event=>{event.preventDefault();$('login-submit').disabled=true;$('login-error').textContent='';try {const key=$('access-key').value;await api('/session',{method:'POST',headers:{Authorization:'Bearer '+key}});$('access-key').value='';await connect();$('login-dialog').close();toast('研究服务已连接');}catch(e){$('login-error').textContent=e.message==='请连接服务后继续。'?'访问密钥不正确，请重试。':e.message;}finally{$('login-submit').disabled=false;}};
$('logout-button').onclick=safeAction(async()=>{await api('/session',{method:'DELETE'});setConnected(false);clearTimeout(state.timer);state.activeJob=null;state.selection++;state.result=null;state.resultJob=null;state.jobs=[];$('job-count').textContent='0';$('history-rows').replaceChildren();$('history-table').hidden=true;$('history-empty').hidden=false;$('result-detail').hidden=true;$('result-toolbar').hidden=true;$('result-expression').hidden=true;$('equity-chart').setAttribute('hidden','');$('chart-tooltip').hidden=true;$('result-empty').hidden=false;$('result-period').textContent='等待首个结果';$('metrics').querySelectorAll('strong').forEach(e=>{e.textContent='—';e.className='';});$('cancel-job').hidden=true;$('run-indicator').classList.remove('busy');$('run-status-title').textContent='已断开服务';$('run-status-detail').textContent='重新连接后可查看任务；正在运行的任务不会被取消。';toast('已断开；本地策略草稿保留');});
$('run-button').onclick=safeAction(run);$('cancel-job').onclick=safeAction(async()=>{if(state.activeJob){await api(`/jobs/${state.activeJob}/cancel`,{method:'POST'});await poll();}});
$('refresh-history').onclick=safeAction(refreshHistory);$('reuse-result').onclick=safeAction(()=>reuseJob(state.resultJob));
$('alpha').addEventListener('input',()=>{updateEditor();saveDraft();state.selectedTemplate=null;document.querySelectorAll('.template-button').forEach(e=>e.classList.remove('active'));$('template-description').textContent='自定义策略 · 使用常用算子组合研究想法';});
$('alpha').addEventListener('scroll',()=>{$('line-numbers').scrollTop=$('alpha').scrollTop;});
$('alpha').addEventListener('keydown',event=>{if(event.key==='Tab'){event.preventDefault();const t=event.target,start=t.selectionStart;t.setRangeText('  ',start,t.selectionEnd,'end');updateEditor();saveDraft();}});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'&&!$('login-dialog').open){event.preventDefault();safeAction(run)();}});
$('settings-form').onsubmit=event=>event.preventDefault();$('settings-form').addEventListener('change',()=>{updateSourceDescription();saveDraft();});
function setSettingsExpanded(expanded){document.querySelector('.settings-panel').classList.toggle('expanded',expanded);$('toggle-settings').setAttribute('aria-expanded',String(expanded));$('toggle-settings').textContent=expanded?'收起设置 −':'展开设置 +';}
$('toggle-settings').onclick=()=>setSettingsExpanded($('toggle-settings').getAttribute('aria-expanded')!=='true');
$('reset-settings').onclick=()=>{const template=state.config.templates.find(t=>t.id===state.selectedTemplate);applySettings(template?.settings||state.config.defaults);saveDraft();toast('已恢复当前模板参数');};
document.querySelectorAll('[data-chart]').forEach(e=>e.onclick=()=>{state.chart=e.dataset.chart;document.querySelectorAll('[data-chart]').forEach(b=>{b.classList.toggle('active',b===e);b.setAttribute('aria-pressed',b===e?'true':'false');});drawChart();});
document.querySelectorAll('.download').forEach(e=>e.onclick=safeAction(()=>downloadArtifact(e.dataset.artifact)));
$('export-strategy').onclick=()=>downloadBlob(new Blob([JSON.stringify({name:'my_alpha',alpha:$('alpha').value,settings:getSettings()},null,2)],{type:'application/json'}),'strategy.json');
$('import-strategy').onclick=()=>$('strategy-file').click();$('strategy-file').onchange=safeAction(async()=>{const file=$('strategy-file').files[0];if(!file)return;try {if(file.size>32768)throw new Error('策略 JSON 不能超过 32 KiB。');setDraft(JSON.parse(await file.text()));toast('策略已导入');}finally{$('strategy-file').value='';}});

async function init(){buildFields();const catalog=await api('/ui/catalog');fillCatalog(catalog);applySettings(catalog.defaults);const draft=readDraft();state.preferredSource=draft?.source;if(draft){try{setDraft(draft);}catch{chooseTemplate(catalog.templates[0]);}}else chooseTemplate(catalog.templates[0]);try{await connect();}catch{showLogin();}}
init().catch(e=>error('无法加载工作台：'+e.message));
