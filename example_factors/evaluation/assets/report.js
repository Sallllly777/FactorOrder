'use strict';
const el=id=>document.getElementById(id), pct=x=>x==null?'—':(x*100).toFixed(2)+'%', num=x=>x==null?'—':x.toFixed(4), pts=x=>x==null?'—':Number(x).toFixed(2).replace(/\.00$/,'');
const names={baseline:'基础5DR','5':'日内5日反转','10':'日内10日反转','20':'日内20日反转'};
function node(tag,text,cls){const x=document.createElement(tag);if(text!=null)x.textContent=text;if(cls)x.className=cls;return x;}
function row(parent,values){const tr=node('tr');values.forEach(v=>tr.append(node('td',v)));parent.append(tr);}
function period(e,key){return e.stability.periods.find(x=>x.name===key);}
function sum(items){return [items.reduce((s,x)=>s+(x.points??0),0),items.reduce((s,x)=>s+(x.grade===null?0:x.weight),0)];}
function show(r){const e=r.evaluation;el('identity').textContent=names[r.key]+' · '+r.alpha;el('items-body').replaceChildren();el('stability-body').replaceChildren();el('dimensions').replaceChildren();el('downloads').replaceChildren();
 for(const label of ['预测','经济','稳健','实施','增量']){const items=e.score.items.filter(x=>x.label.startsWith(label));const [p,w]=sum(items);const d=node('div',null,'dim');d.append(node('span',label+' · 得分 / 已评权重'),node('strong',pts(p)+' / '+w));el('dimensions').append(d);}
 for(const x of e.score.items)row(el('items-body'),[x.label,x.weight,x.grade??'未评价',pts(x.points),x.reason]);
 for(const x of e.stability.comparisons.filter(x=>['rank_ic','top_excess','annual_excess'].includes(x.metric))){const f=x.metric==='rank_ic'?num:pct;row(el('stability-body'),[x.comparison==='validation'?'验证整体':x.comparison,x.label,f(x.train_value),f(x.validation_value),f(x.delta),x.retention===null?'未定义':pct(x.retention),x.grade??'未评价',x.grade_reason]);}
 for(const [name,file] of [['完整评价JSON','evaluation.json'],['训练验证对照CSV','evaluation_stability.csv'],['独立区间净值CSV','evaluation_period_returns.csv'],['可复跑策略配置','strategy.json']]){const a=node('a',name+' ↓');a.href='results/'+r.key+'/'+file;el('downloads').append(a);}
}
async function init(){const data=/* PUBLIC_EVALUATION_DATA */;el('cards').replaceChildren();
 for(const r of data){const e=r.evaluation,s=e.score,v=period(e,'validation'),t=period(e,'train'),ic=e.stability.comparisons.find(x=>x.comparison==='validation'&&x.metric==='rank_ic');const c=node('article',null,'card');c.append(node('h3',names[r.key]));const score=node('div',pts(s.earned_points),'score');score.append(node('small','/ '+s.evaluated_weight));c.append(score,node('div','已评项目得分 / 已评权重（满权重100）','caption'),node('div',s.total===null?'总分未发布 · 尚缺 '+(100-s.evaluated_weight)+' 分证据':'研究评级：'+s.priority,'status'));
 const mini=node('div',null,'mini');for(const [label,value] of [['验证累计收益',pct(v.portfolio.candidate.total_return)],['验证最大回撤',pct(v.portfolio.candidate.max_drawdown)]]){const d=node('div');d.append(node('span',label),node('strong',value));mini.append(d);}c.append(mini);const a=node('a','查看评分依据 ↘');a.href='#detail';a.onclick=()=>{el('factor-select').value=r.key;show(r);};c.append(a);el('cards').append(c);
 const [sp,sw]=sum(s.items.filter(x=>x.key.startsWith('oos_')));row(el('compare-body'),[names[r.key],num(ic.train_value),num(ic.validation_value),pct(ic.retention),pct(t.portfolio.candidate.annual_return),pct(v.portfolio.candidate.annual_return),pct(v.portfolio.candidate.max_drawdown),pts(sp)+' / '+sw+'（满6）']);
 const opt=node('option',names[r.key]);opt.value=r.key;el('factor-select').append(opt);}
 el('findings').textContent='怎样读这页：先看已评权重，再看得分来源和验证期退化。累计收益为正，不代表预测稳定、风险可控或对已有因子有增量；缺失项目要先补齐证据，不能把当前比例放大成满分100。';
 el('factor-select').onchange=()=>show(data.find(r=>r.key===el('factor-select').value));el('factor-select').value='5';show(data.find(r=>r.key==='5')||data[0]);}
init().catch(e=>{el('cards').replaceChildren(node('p',e.message));});
