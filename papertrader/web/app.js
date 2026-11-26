'use strict';
// Approved DEMO design, connected exclusively to this account's persisted records.
const $ = s => document.querySelector(s), all = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v, n = 2) => v == null ? '—' : Number(v).toLocaleString('zh-CN', {minimumFractionDigits:n, maximumFractionDigits:n});
const pct = v => v == null ? '—' : (v > 0 ? '+' : '') + fmt(v) + '%';
const money = v => v == null ? '—' : '¥' + fmt(v, 0);
const names = {trend:'趋势', pullback:'回调'}, acts = {buy:'买入', sell:'卖出', hold:'观望', rejected:'拦截', pending:'待处理'};
const modes = {external:'外部 Agent', model:'模型决策', local:'本地策略'};
const startup = new URLSearchParams(location.hash.slice(1)).get('token');
let token = startup || sessionStorage.getItem('papertrader-token') || '';
if (startup) history.replaceState(null, '', location.pathname + location.search);
let state, selected, day, page = 0, span = 0, rule = -1, result, busy = false, epoch = 0;
let view = 'dash', pane = 'chain', filter = 'all';
const charts = new Map();
function message(text = '') { $('#message').textContent = text; $('#message').hidden = !text; }
async function api(path, data) {
  const response = await fetch(path, {method:data === undefined ? 'GET' : 'POST',
    headers:{Authorization:'Bearer ' + token, 'Content-Type':'application/json'},
    body:data === undefined ? undefined : JSON.stringify(data), signal:AbortSignal.timeout(45000)});
  const r = await response.json(); if (!response.ok) throw new Error(r.error || '请求失败'); return r;
}
async function reload() {
  const e = epoch;
  try {
    $('#refresh').disabled = true; const r = await api('/api/state'); if (e !== epoch) return;
    state = r; sessionStorage.setItem('papertrader-token', token); $('#connect').hidden = true;
    $('#connectError').textContent = ''; message(state.market_error || ''); render();
  } catch (error) {
    if (e !== epoch) return;
    if (!state) { $('#connect').hidden = false; $('#connectError').textContent = error.message; }
    else message('刷新失败，保留上次数据：' + error.message);
  } finally { $('#refresh').disabled = false; }
}
const ds = () => state?.decisions || [];
const current = () => ds().find(d => d.id === selected);
const action = d => ['rejected','pending'].includes(d.outcome?.status) ? d.outcome.status : d.proposal.action;
const filled = d => d.outcome?.status === 'filled' && ['buy','sell'].includes(d.outcome.action);
const fill = d => (state.fills || []).find(f => f.id === d.outcome?.trade_id);
const checks = d => d.submission?.checks || [];
const stock = d => d.audit?.agent_input?.quotes?.[d.proposal.code]?.name || state.quotes?.[d.proposal.code]?.name || d.proposal.code || '账户观察';
const dates = () => [...new Set([...state.nav.map(n => n.date), ...ds().map(d => d.date)])].sort();
function maxDD() {
  let peak = state.invested || state.nav[0]?.total || state.equity, worst = 0;
  state.nav.forEach(n => { peak = Math.max(peak, n.total); if (peak > 0) worst = Math.min(worst, (n.total / peak - 1) * 100); }); return worst;
}
function settled(d) {
  const o = d.outcome || {};
  if (o.feedback) return '已平仓 · 净收益 ' + pct(o.pnl_pct) + '，已更新打法经验。';
  if (action(d) === 'buy') {
    const close = ds().find(x => x.outcome?.feedback?.decision_id === d.id);
    if (close) return close.date + ' 已平仓 · 净收益 ' + pct(close.outcome.pnl_pct) + '，已计入学习。';
    const p = state.positions.find(p => p.code === d.proposal.code);
    return '待平仓结算' + (p ? ' · 当前浮动盈亏 ' + pct(p.pnl_pct) : '') + '；持仓期间不更新奖励。';
  }
  if (action(d) === 'rejected') return '原提案被拦截，没有成交，也没有交易盈亏。';
  if (action(d) === 'hold') return '主动观望，没有成交，不形成平仓样本。';
  return o.pnl_pct != null ? '卖出净收益 ' + pct(o.pnl_pct) + '；没有策略归因反馈。' : '尚无已结算结果。';
}
function gateHTML(d) {
  return checks(d).map(([s,t]) => `<span class="rv-gate ${s === 'fail' ? 'no' : ''}">${s === 'pass' ? '✓' : '✕'} ${esc(t)}</span>`).join(' ') || '<span class="dim">没有已保存的检查记录</span>';
}
function render() {
  if (!current()) selected = ds()[0]?.id; day = current()?.date || day;
  const ns = state.nav, prev = ns.length > 1 ? ns.at(-2).total : state.invested;
  const delta = prev > 0 ? state.equity - prev : null, daily = prev > 0 ? delta / prev * 100 : null;
  const cash = state.equity ? state.cash / state.equity * 100 : 0;
  $('#acct').innerHTML = [['TOTAL',money(state.equity)],['累计收益',pct(state.return_pct)],['最近一日',pct(daily)],['现金仓位',fmt(cash,0)+'%']].map(([k,v])=>`<div class="it"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
  $('#stats').innerHTML = `<div class="main"><div class="k">总资产</div><div class="v"><small>¥</small>${fmt(state.equity,0)}</div><div class="s">已记录 ${ns.length} 个交易日 · 本金 ${money(state.invested)}</div></div><div class="srow">` +
    [['累计收益率',pct(state.return_pct),state.return_pct,''],['最近一日盈亏',delta == null ? '—' : (delta > 0 ? '+' : delta < 0 ? '−' : '')+money(Math.abs(delta)),delta,pct(daily)],['现金仓位比',fmt(cash,0)+'%',null,money(state.cash)+' 可用'],['最大回撤',pct(maxDD()),maxDD(),'已记录净值的峰谷变化']].map(([k,v,n,s])=>`<div class="hstat"><div class="k">${k}</div><div class="v ${n > 0 ? 'up' : n < 0 ? 'down' : ''}">${v}</div><div class="s num">${s}</div></div>`).join('') + '</div>';
  $('#moodText').textContent = [state.sandbox?'历史测试账户':'日常经营',modes[state.mode],state.paused?'已暂停':'运行中',state.market_date||'等待行情'].join(' · ');
  const quotes = Object.entries(state.quotes || ds()[0]?.audit?.agent_input?.quotes || {});
  $('#tickerRail').innerHTML = quotes.map(([c,q])=>`<span class="tk"><span class="n">${esc(q.name)}</span><span class="p">${fmt(q.price)}</span><span class="c dim">${esc(c)}</span></span>`).join('') || '<span class="tk dim">行情尚未加载</span>';
  $('#chips').innerHTML = quotes.map(([c,q])=>`<div class="chip"><span class="n">${esc(q.name)}</span><span class="cd">${esc(c)}</span><span class="p num">${fmt(q.price)}</span><span class="c dim">MA5 ${fmt(q.ma5)}</span></div>`).join('') || '<div class="rv-note">加载行情后展示自选池快照。</div>';
  $('#posCount').textContent = state.positions.length ? '共 '+state.positions.length+' 只' : '';
  $('#posBoard').innerHTML = state.positions.map(p=>`<div class="pb-row"><span class="n">${esc(p.name)}</span><span class="cd">${esc(p.code)}</span><span class="lead"></span><span class="v num">${money(p.value)}</span><span class="pct num ${p.pnl_pct >= 0?'up':'down'}">${pct(p.pnl_pct)}</span></div>`).join('') || '<div class="pb-empty">空仓观望中</div>';
  $('#posFoot').textContent = '股票市值 '+money(state.positions.reduce((n,p)=>n+p.value,0))+' · 浮动盈亏尚未计入策略奖励';
  renderJournal(); renderGrowth(); renderCalendar(); renderRecheck(); renderEvolution(); renderAbout(); show(view);
}
function renderJournal() {
  $('#jList').innerHTML = ds().filter(d=>filter==='all'||action(d)===filter).map(d=>{
    const a=action(d), f=fill(d), o=d.outcome||{};
    return `<div class="jrow" data-id="${d.id}"><button class="head" aria-expanded="false"><span class="d">${esc(d.date.slice(5))}</span><span class="tag ${a==='rejected'?'reject':a}">${esc(acts[a])}</span><span class="rs"><b>${esc(stock(d))}</b>${esc(d.proposal.reason)}</span><span class="meta">${money(f?.value||null)}</span><span class="meta ${o.pnl_pct>=0?'up':'down'}">${pct(o.pnl_pct)}</span><span class="srctag">${esc(d.proposal.brain)}</span><span class="chev">▸</span></button>`+
      `<div class="body"><div class="bk"><div class="bk-t">决策 · #${d.id}</div><div class="kv"><span>成交价</span><b>${filled(d)?'¥'+fmt(o.price):'—'}</b></div><div class="kv"><span>数量</span><b>${f?.shares?fmt(f.shares,4):'—'}</b></div><div class="basis">${esc(d.proposal.reason)}</div><button class="rv-control" data-open="${d.id}">查看决策链 →</button></div><div class="bk"><div class="bk-t">打法与结算 · LEARNING</div><div class="rq">${esc(names[d.policy?.arm]||d.policy?.arm||'无开仓授权')}<br>${esc(settled(d))}</div></div><div class="bk"><div class="bk-t">闸门 · GATES</div>${gateHTML(d)}</div></div></div>`;
  }).join('') || '<div class="rv-note">这一类还没有记录。</div>';
}
function renderGrowth() {
  $('#rules').innerHTML = [['R1','单股仓位上限',state.rules.max_weight*100],['R2','首次建仓上限',state.rules.first_buy_cap*100],['R3','单日追高线',state.rules.chase_limit_pct]].map(([id,t,n])=>`<li><span class="rn">${id}</span><span>${t} <b class="num">${fmt(n,0)}%</b></span><span class="cnt">${state.rule_history.length?'已保存调整记录':'尚未调整'}</span></li>`).join('')+'<li><span class="rn">FIXED</span><span>遵守观望承诺 · 授权范围内自主下单 · 完整平仓后归因 · 样本不足不调规则</span></li>';
}
function chart(id, option) {
  const el=$('#'+id); if (!el?.clientWidth||!el.clientHeight) return;
  if (!window.echarts) { el.textContent='图表资源未加载，请检查安装包。'; return; }
  if (!charts.has(id)) charts.set(id,echarts.init(el)); const c=charts.get(id); c.resize(); c.setOption(option,true);
}
function drawNav(id, limit=0) {
  const ns=limit?state.nav.slice(-limit):state.nav;
  if (!ns.length) { $('#'+id).textContent='首次决策后，净值会按交易日积累。'; return; }
  const values=ns.map(n=>n.total), low=Math.min(...values), high=Math.max(...values);
  const pad=Math.max((high-low)*.15,Math.abs(high)*.0015,1);
  chart(id,{animation:false,color:['#9cbbd3','#526b84'],grid:{left:72,right:48,top:26,bottom:28},
    tooltip:{trigger:'axis',backgroundColor:'#111e2d',borderColor:'#30465c',textStyle:{color:'#d4e2ed'}},
    xAxis:{type:'category',data:ns.map(n=>n.date),boundaryGap:false,axisTick:{show:false},axisLabel:{color:'#74899f',fontSize:9,formatter:v=>v.slice(5)},axisLine:{lineStyle:{color:'#29384a'}}},
    yAxis:[{type:'value',name:'总资产 / 元',min:Math.floor((low-pad)/10)*10,max:Math.ceil((high+pad)/10)*10,splitNumber:4,nameTextStyle:{color:'#74899f',fontSize:9},axisLabel:{color:'#9db2c6',fontSize:9,formatter:v=>fmt(v,0)},splitLine:{lineStyle:{color:'rgba(170,195,218,.07)'}}},
      {type:'value',name:'现金占比',min:0,max:100,interval:25,nameTextStyle:{color:'#586d83',fontSize:9},axisLabel:{color:'#586d83',fontSize:9,formatter:'{value}%'},splitLine:{show:false}}],
    series:[{name:'账户总资产',type:'line',data:ns.map(n=>+n.total.toFixed(2)),showSymbol:ns.length===1,lineStyle:{width:1.8},areaStyle:{color:'rgba(143,184,216,.035)'},tooltip:{valueFormatter:v=>'¥'+fmt(v)}},
      {name:'现金占比',type:'line',yAxisIndex:1,data:ns.map(n=>n.total>0?+(n.cash/n.total*100).toFixed(2):null),showSymbol:false,lineStyle:{width:1,type:'dashed',opacity:.55},tooltip:{valueFormatter:v=>fmt(v,1)+'%'}}]});
  const summary=ns[0].date+' — '+ns.at(-1).date+' · '+ns.length+' 个交易日 · 总资产左轴按实际区间缩放，现金占比右轴 0–100%';
  if(id==='rvNavChart') $('#rvNavSummary').textContent=summary;
  if(id==='navChart') $('#navSummary').textContent=summary;
}
function drawStrategy() {
  const arms=Object.entries(state.strategy);
  chart('strategyChart',{animation:false,color:['#8fb8d8','#526b84'],grid:{left:65,right:25,top:30,bottom:35},tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},legend:{bottom:0,textStyle:{color:'#74899f',fontSize:10},data:['盈利平仓','未盈利平仓']},xAxis:{type:'value',minInterval:1,splitLine:{lineStyle:{color:'rgba(170,195,218,.07)'}},axisLabel:{color:'#74899f'}},yAxis:{type:'category',data:arms.map(([a])=>names[a]||a),axisLabel:{color:'#9db2c6'},axisTick:{show:false},axisLine:{show:false}},series:[{name:'盈利平仓',type:'bar',stack:'samples',barWidth:12,data:arms.map(([,s])=>s.alpha)},{name:'未盈利平仓',type:'bar',stack:'samples',data:arms.map(([,s])=>s.beta)}]});
}
function renderCalendar() {
  const days=dates(), pages=Math.ceil(days.length/14); page=Math.max(0,Math.min(page,pages-1));
  const end=days.length-page*14, shown=days.slice(Math.max(0,end-14),end);
  if(shown.length&&!shown.includes(day)) { day=shown.at(-1); selected=ds().find(d=>d.date===day)?.id; result=undefined; rule=-1; }
  const amount=date=>(state.fills||[]).filter(f=>f.date===date&&['buy','sell'].includes(f.action)).reduce((n,f)=>n+f.value,0);
  const max=Math.max(1,...shown.map(amount));
  $('#rvRailRange').textContent=shown.length?shown[0]+' — '+shown.at(-1):'暂无交易日';
  $('#rvPrev').disabled=page>=pages-1; $('#rvNext').disabled=$('#rvLatest').disabled=page===0;
  $('#rvDemo').hidden=!state.sandbox; $('#rvDemo').textContent='历史测试 · '+state.data_source;
  $('#rvRailBase').innerHTML=shown.map(date=>{const aa=ds().filter(d=>d.date===date).map(action), cls=aa.includes('buy')?'buy':aa.includes('sell')?'sell':aa.includes('rejected')?'block':'hold'; const v=amount(date), label=v?money(v):'无成交'; return `<button type="button" class="rv-bar ${cls} ${day===date?'on':''}" data-day="${date}" aria-pressed="${day===date}" aria-label="${date}，${label}"><span class="col" style="height:${Math.max(4,Math.round(v/max*100))}%" data-amt="${label}"></span></button>`;}).join('');
  $('#rvRailDays').innerHTML=shown.map(d=>`<span>${d.slice(5).replace('-','/')}</span>`).join(''); renderDay();
}
function choose(id) {
  selected=Number(id); day=current()?.date; result=undefined; rule=-1;
  const days=dates(); page=Math.max(0,Math.floor((days.length-1-days.indexOf(day))/14)); renderCalendar(); renderRecheck();
}
function renderDay() {
  const list=ds().filter(d=>d.date===day);
  if(!list.length) { $('#rvDayView').innerHTML='<div class="rv-note">选择一个有记录的日期，查看当天的决策。</div>'; return; }
  if(!list.some(d=>d.id===selected)) { selected=list[0].id; result=undefined; rule=-1; }
  $('#rvDayView').innerHTML=`<div class="eyebrow">${esc(day)} · 当日详情</div><div class="rv-trades">`+list.map(d=>`<button class="rv-trade ${d.id===selected?'on':''}" data-decision="${d.id}" aria-pressed="${d.id===selected}"><span>#${d.id} ${esc(stock(d))}</span><span class="ac">${esc(acts[action(d)])}</span></button>`).join('')+'</div><div class="rv-chain" id="rvChainSlot"></div>';
  const d=current(), r=d.audit?.agent_input?.rules||d.audit?.submission?.rules, p=d.policy||{};
  const rt=r?`单股 ≤${fmt(r.max_weight*100,0)}% · 首仓 ≤${fmt(r.first_buy_cap*100,0)}% · 追高线 ${fmt(r.chase_limit_pct,0)}%`:'未保存参数快照。';
  const step=(n,t,en,html)=>`<div class="rv-step"><div class="rh"><span class="rn">${n}</span><span class="rt">${t}</span><span class="ren">${en}</span></div><div class="rbody">${html}</div></div>`;
  $('#rvChainSlot').innerHTML='<div class="rv-timeline">'+step('01','当时的规矩','RULES',`<p>${esc(rt)}</p><p>本次打法 ${esc(names[p.arm]||p.arm||'无开仓信号')} · 买入授权 ${esc(p.allowed_buys?.join('、')||'无')}</p>`)+step('02','它怎么想的','AGENT',`<p>${esc(d.proposal.reason)}</p><p class="dim">来源 ${esc(d.proposal.brain)} · 提案 ${esc(acts[d.proposal.action])}${d.proposal.action==='buy'?' · 目标仓位 '+fmt(d.proposal.weight*100,0)+'%':''}</p>`)+step('03','逐条过闸','GATES',`<div class="rv-gates">${gateHTML(d)}</div><button class="rv-control" id="openRecheck" style="margin-top:12px">查看检查依据与复核 →</button>`)+step('04','结果与回响','OUTCOME',`<p>${esc(settled(d))}</p>${filled(d)?`<p>成交价 ¥${fmt(d.outcome.price)} · 成交额 ${money(fill(d)?.value)} · 费用 ¥${fmt(d.outcome.fee)}</p>`:''}`)+'</div>';
}
function evidence(d, entry) {
  if(!entry) return '请选择一项实际检查。'; const f=d.audit?.submission,p=d.proposal,t=entry[1]; if(!f) return t+'；未保存事实快照。';
  if(/打法/.test(t)) return '当次打法：'+(names[d.policy?.arm]||d.policy?.arm)+'；授权标的：'+(f.allowed_buys?.join('、')||'无')+'；提案标的：'+(p.code||'无');
  if(/仓位|建仓/.test(t)) return '目标 '+fmt(p.weight*100,0)+'%；当时单股上限 '+fmt(f.rules.max_weight*100,0)+'%，首仓上限 '+fmt(f.rules.first_buy_cap*100,0)+'%。';
  if(/现金/.test(t)) return '当时现金 '+money(f.cash)+'；总资产 '+money(f.equity)+'；每边费用 '+fmt(f.fee_rate*100)+'%。';
  if(/追|涨幅/.test(t)) return '当日涨幅 '+pct(f.daily_change_pct)+'；当时追高线 '+fmt(f.rules.chase_limit_pct,0)+'%。';
  if(/承诺/.test(t)) return f.commitment?'当时承诺：'+(f.commitment.quote||JSON.stringify(f.commitment)):'当时没有匹配承诺。';
  if(/持仓/.test(t)) return '当时持仓 '+fmt(f.shares,4)+' 股；记录价格 ¥'+fmt(f.price)+'。';
  if(/自选/.test(t)) return '提案标的 '+(p.code||'无')+'；快照记录：'+(f.watched?'在当时自选池中':'不在当时自选池中')+'。';
  return t+'；来自这次决策的原始规则检查。';
}
function renderRecheck() {
  const d=current();
  $('#rcDate').innerHTML=dates().reverse().map(date=>`<option value="${date}" ${date===day?'selected':''}>${date}</option>`).join('');
  $('#rcDecision').innerHTML=ds().filter(x=>x.date===day).map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>#${x.id} · ${esc(acts[action(x)])} ${esc(stock(x))}</option>`).join('');
  $('#reproduce').disabled=!d?.audit||busy;
  if(!d) {
    $('#rvRcTarget').textContent='还没有可复核的决策。'; $('#rvSwitches').innerHTML='';
    $('#rvCmp').innerHTML=''; $('#rvSwCount').textContent='';
    $('#rvVerdict').textContent='完成首次决策后可查看。'; return;
  }
  $('#rvRcTarget').innerHTML=`<span class="who">#${d.id} · ${esc(acts[action(d)])} ${esc(stock(d))}</span><span class="meta">${esc(d.date)} · ${esc(d.proposal.brain)}</span><span class="q">${esc(d.proposal.reason)}</span>`;
  $('#rvSwitches').innerHTML=checks(d).map(([s,t],i)=>`<button class="rv-swrow ${rule===i?'rm':''}" data-check="${i}" aria-pressed="${rule===i}"><span class="check-number">${String(i+1).padStart(2,'0')}</span><span class="tx">${esc(t)}</span><span class="st">${s==='pass'?'通过':'拦截'}</span></button>`).join('')||'<div class="rv-note">未保存检查项。</div>';
  if(d.audit?.submission?.commitment) $('#rvSwitches').insertAdjacentHTML('beforeend',`<button class="btn" id="ignoreCommitment" ${busy?'disabled':''}>移除承诺后复核</button>`);
  $('#rvSwCount').textContent=rule<0?'点击检查项查看依据；拦截后不再检查后续项。':evidence(d,checks(d)[rule]);
  const side=(k,t,rows,n,b='')=>`<div class="side ${b}"><div class="cap">${k}</div><div class="head">${esc(t)}</div>${rows.map(([a,v])=>`<div class="line"><span>${a}</span><span>${esc(v)}</span></div>`).join('')}<div class="concl">${esc(n)}</div></div>`;
  const r=result?.decision_id===d.id?result:undefined;
  $('#rvCmp').innerHTML=side('A · 原始记录',d.submission?.allowed?'通过':'拦截',[['决定项',d.submission?.reason||'无'],['成交',filled(d)?'已成交':'无成交'],['净收益',pct(d.outcome?.pnl_pct)]],'读取原始记录。')+'<div class="divider"></div>'+side('B · 快照复核',r?(r.rechecked.allowed?'通过':'拦截'):'等待复核',[['决定项',r?.rechecked.reason||'—'],['一致性',r?(r.matches_recorded?'一致':'不一致'):'—']],r?'仅在内存中复算。':'运行复核后显示。','b');
  $('#rvVerdict').textContent=!r?'选择日期与决策，再查看检查或运行复核。':[
    r.matches_recorded?'快照与原记录一致。':'快照与原记录不一致。',
    r.baseline.allowed!==r.rechecked.allowed?'移除承诺改变了判断。':'本次结论未改变。',
    '复核不改写历史。'
  ].join(' ');
}
async function recheck(ignore=false) {
  const id=selected,e=epoch; busy=true; renderRecheck();
  try { const r=await api('/api/recheck',{decision_id:id,ignore_commitment:ignore}); if(id===selected&&e===epoch) result=r; }
  catch(error) { message(error.message); } finally { busy=false; if(state) renderRecheck(); }
}
function renderEvolution() {
  const closed=Object.values(state.strategy).reduce((n,s)=>n+s.alpha+s.beta,0), wins=Object.values(state.strategy).reduce((n,s)=>n+s.alpha,0);
  $('#rvKpi').innerHTML=`<div class="lead"><div class="k">累计收益率 · ${state.nav.length} 个交易日</div><div class="v ${state.return_pct>=0?'up':'down'}">${pct(state.return_pct)}</div><div class="s">${money(state.equity)} · 与看板同一本账</div></div><div class="rv-ebs">`+[['CLOSED',closed,'笔已归因平仓'],['WIN',closed?fmt(wins/closed*100,0)+'%':'—','已平仓样本胜率'],['MAX DD',pct(maxDD()),'已记录最大回撤'],['FILLS',(state.fills||[]).filter(f=>['buy','sell'].includes(f.action)).length,'近窗成交记录']].map(([k,v,s])=>`<div class="rv-eb"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join('')+'</div>';
  $('#rvSpecs').innerHTML=[['max_weight','单股仓位上限',100],['first_buy_cap','首次建仓上限',100],['chase_limit_pct','单日追高线',1]].map(([k,t,f],i)=>{
    const vs=state.rule_history.map(h=>h[k]*f),min=Math.min(...vs),max=Math.max(...vs),pts=vs.map((v,j)=>`${vs.length===1?150:j/(vs.length-1)*300},${min===max?26:44-(v-min)/(max-min)*36}`).join(' ');
    const svg=vs.length?`<svg viewBox="0 0 300 52" preserveAspectRatio="none" aria-label="已保存参数版本"><polyline points="${pts}" fill="none" stroke="#8fb8d8" stroke-width="1.6" vector-effect="non-scaling-stroke"/>${vs.length===1?'<circle cx="150" cy="26" r="2.5" fill="#c6def0"/>':''}</svg>`:'<span class="dim">尚未发生参数调整</span>';
    return `<div class="rv-spec rvg"><div class="top"><span class="n">R${i+1} · CORE</span><span class="lbl">${t}</span></div><div class="val">${fmt(state.rules[k]*f,0)}<small>%</small></div><div class="meta">${vs.length} 次实际调整记录</div><div class="rv-spark rvw">${svg}</div><div class="cap">${vs.length?esc(state.rule_history[0].date+' — '+state.rule_history.at(-1).date):'样本不足或无需改变时，保留当前纪律。'}</div></div>`;
  }).join('');
  $('#rvFixed').innerHTML='<span class="fchipx">完整平仓后归因</span> <span class="fchipx">承诺需履行或显式修订</span> <span class="fchipx">每次调整有边界</span>';
}
function renderAbout() {
  $('#serviceInfo').innerHTML=[['ACCOUNT',state.character],['MODE',(state.sandbox?'历史测试 · ':'真实时间 · ')+(modes[state.mode]||state.mode)],['DATA SOURCE',state.data_source+' · '+(state.market_date||'等待行情')],['DAILY WINDOW',state.close_time+' · Asia/Shanghai'],['EXECUTION',state.execution+' · 每边费用 '+fmt(state.fee_bps_per_side/100)+'%'],['MODEL','决策来源逐笔记录；模型由接入的 Agent 或本地服务配置'],['AUDIT',state.chain_ok?'成交 hash 链校验通过':'成交 hash 链异常，请检查记录'],['SCOPE','当前为简化日频模拟，允许碎股；尚未完整模拟整手、T+1、涨跌停与公司行动']].map(([k,v])=>`<div class="setrow"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('');
  $('#pause').textContent=state.paused?'恢复任务':'暂停任务'; $('#sandboxControl').hidden=!state.sandbox; $('#tick').textContent=state.mode==='external'?'生成当前任务':'执行一次';
}
function show(name) {
  view=name; all('.view').forEach(el=>el.classList.toggle('on',el.id==='v-'+name)); all('#tabs .tab').forEach(el=>{const on=el.dataset.v===name;el.classList.toggle('on',on);el.setAttribute('aria-pressed',on);});
  requestAnimationFrame(()=>{if(!state)return;if(name==='dash')drawNav('navChart');if(name==='review'&&pane==='curve')drawNav('rvNavChart',span);});
}
function showPane(name) {
  pane=name; all('.rv-pane').forEach(el=>el.classList.toggle('on',el.id==='rvp-'+name)); all('#rvTabs .rvt').forEach(el=>{const on=el.dataset.p===name;el.classList.toggle('on',on);el.setAttribute('aria-pressed',on);});
  if(name==='curve') requestAnimationFrame(()=>{if(state)drawNav('rvNavChart',span);});
}
$('#connectForm').onsubmit=e=>{e.preventDefault();epoch++;token=$('#token').value.trim();reload();};
$('#refresh').onclick=reload;
$('#tabs').onclick=e=>{const b=e.target.closest('[data-v]');if(b&&state)show(b.dataset.v);};
$('#rvTabs').onclick=e=>{const b=e.target.closest('[data-p]');if(b&&state)showPane(b.dataset.p);};
$('#jFilters').onclick=e=>{const b=e.target.closest('[data-f]');if(!b||!state)return;filter=b.dataset.f;all('#jFilters .fchip').forEach(el=>el.classList.toggle('on',el===b));renderJournal();};
$('#jList').onclick=e=>{if(e.target.closest('[data-open]'))return;const row=e.target.closest('.jrow');if(row)row.querySelector('.head').setAttribute('aria-expanded',row.classList.toggle('open'));};
$('#rvRailBase').onclick=e=>{const b=e.target.closest('[data-day]');if(b){const d=ds().find(d=>d.date===b.dataset.day);if(d)choose(d.id);else{day=b.dataset.day;selected=undefined;result=undefined;rule=-1;renderCalendar();renderRecheck();}}};
$('#rvPrev').onclick=()=>{page++;renderCalendar();renderRecheck();}; $('#rvNext').onclick=()=>{page--;renderCalendar();renderRecheck();};
$('#rvLatest').onclick=()=>{if(ds().length)choose(ds()[0].id);};
$('#rvDayView').onclick=e=>{const b=e.target.closest('[data-decision]');if(b)choose(b.dataset.decision);if(e.target.closest('#openRecheck'))showPane('recheck');};
$('#rcDate').onchange=e=>{const d=ds().find(d=>d.date===e.target.value);if(d)choose(d.id);else{day=e.target.value;selected=undefined;result=undefined;rule=-1;renderCalendar();renderRecheck();}};
$('#rcDecision').onchange=e=>choose(e.target.value); $('#reproduce').onclick=()=>recheck();
$('#rvSwitches').onclick=e=>{const b=e.target.closest('[data-check]');if(b){rule=+b.dataset.check;renderRecheck();}if(e.target.closest('#ignoreCommitment'))recheck(true);};
$('#rvNavScale').onclick=e=>{const b=e.target.closest('[data-span]');if(b){span=+b.dataset.span;all('#rvNavScale button').forEach(el=>el.classList.toggle('on',el===b));drawNav('rvNavChart',span);}};
document.addEventListener('click',e=>{const b=e.target.closest('[data-open]');if(b&&state){e.preventDefault();choose(b.dataset.open);show('review');showPane('chain');}});
$('#pause').onclick=async()=>{try{await api('/api/pause',{paused:!state.paused});await reload();}catch(e){message(e.message);}};
$('#tick').onclick=async()=>{try{await api('/api/tick',{});await reload();if(state.mode==='external')message('当前任务已准备，等待接入的 Agent 提交决策。');}catch(e){message(e.message);}};
$('#disconnect').onclick=()=>{epoch++;token='';state=undefined;sessionStorage.removeItem('papertrader-token');$('#connect').hidden=false;$('#token').value='';message();charts.forEach(c=>c.dispose());charts.clear();all('#acct,#stats,#posBoard,#posFoot,#chips,#tickerRail,#jList,#rules,#rvKpi,#rvSpecs,#rvDayView,#rvSwitches,#rvRcTarget,#rvCmp,#rvVerdict,#rvSwCount,#serviceInfo').forEach(el=>el.textContent='');};
window.addEventListener('resize',()=>{if(state)charts.forEach(c=>c.resize());});
if(token)reload();
