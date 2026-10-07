'use strict';
const $ = id => document.getElementById(id), NS = 'http://www.w3.org/2000/svg';
const T = MiniTime, PAGE = 25;
function saved(key, fallback = null) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } }
function save(key, value) { try { localStorage.setItem(key, value); } catch { /* Viewing still works without browser storage. */ } }
let selected = saved('mn-session'), zone = saved('mn-timezone', 'Asia/Hong_Kong');
if (!['Asia/Hong_Kong', 'UTC'].includes(zone)) zone = 'Asia/Hong_Kong';
let sessions = [], data = null, paused = false, kind = 'trades', basis = 'event';
let before = null, until = null, after = null, cursors = [], range = {from: null, to: null};
let timer, active, revision = 0, failures = 0, renderedChart = '', renderedSessions = '', searchTimer;
const visits = new Map();
const fmt = (v, d = 2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const pnl = v => fmt(v, data?.lot ? 6 : 2);
const quantity = v => v == null ? '—' : data?.lot ? fmt(v * data.lot, Math.min(8, Math.max(0, Math.ceil(-Math.log10(data.lot))))) : fmt(v, 0);
const price = v => v == null ? '—' : data?.tick ? fmt(v * data.tick, Math.min(8, Math.max(0, Math.ceil(-Math.log10(data.tick))))) : fmt(v, 0);
const date = (v, compact = false) => T.datetime(v, zone, compact);
function text(id, value) { $(id).textContent = value ?? '—'; }
function polarity(id, n) { $(id).classList.toggle('positive', n > 0); $(id).classList.toggle('negative', n < 0); }
function mode(m) { return ({testnet: 'Spot testnet', 'testnet-churn': 'Testnet churn', 'adversarial-paper': 'Latency experiment', paper: 'Paper', journal: 'Journal replay'})[m] || m; }
function notify(message) { $('notice').hidden = !message; text('notice', message); }
function resetPage() { before = null; until = null; cursors = []; }
function chooseKind(value) {
  kind = value; resetPage();
  document.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === kind));
}
function renderSessions() {
  const search = $('session-search').value.toLowerCase();
  const signature = JSON.stringify([sessions, selected, search]);
  if (signature === renderedSessions) return;
  renderedSessions = signature; $('sessions').replaceChildren(); text('session-count', sessions.length);
  for (const s of sessions.filter(s => s.name.toLowerCase().includes(search))) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'session' + (s.id === selected ? ' active' : '');
    button.setAttribute('aria-pressed', s.id === selected);
    const name = document.createElement('strong'), meta = document.createElement('small');
    name.textContent = s.name.replace(/\/events\.jsonl$/, '');
    for (const label of [mode(s.mode || 'journal'), s.error ? 'Error' : s.health || 'Loading']) {
      const span = document.createElement('span'); span.textContent = label; meta.append(span);
    }
    button.append(name, meta);
    button.onclick = () => {
      selected = s.id; save('mn-session', selected); data = null; after = null; resetPage();
      renderedChart = ''; renderSessions(); refresh(true);
    };
    $('sessions').append(button);
  }
}
function svg(tag, attrs, content) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (content != null) e.textContent = content;
  return e;
}
function drawChart() {
  const root = $('chart'); root.replaceChildren(); $('chart-tooltip').hidden = true;
  const all = data.history || [];
  // PnL is a state *observation*. A reconciled old trade must not move today's state into yesterday.
  const dated = all.filter(p => p.received_time_ms != null || p.event_time_ms != null);
  const points = dated.length ? dated : all;
  if (!points.length) return;
  const timestamp = p => dated.length ? p.received_time_ms ?? p.event_time_ms : p.at;
  const times = points.map(timestamp), monotonic = times.every((t, i) => !i || t >= times[i - 1]);
  const coords = monotonic ? times : points.map((_, i) => i);
  const left = 74, right = 862, start = coords[0], end = coords.at(-1);
  const x = n => left + (n - start) / Math.max(end - start, 1) * (right - left);
  const values = points.map(p => p.pnl).filter(v => v != null);
  let lo = Math.min(0, ...values), hi = Math.max(0, ...values);
  if (lo === hi) { hi = lo + 1; lo -= 1; }
  const pad = (hi - lo) * .12; lo -= pad; hi += pad;
  const y = n => 155 - (n - lo) / (hi - lo) * 125;
  for (let i = 0; i < 4; i++) {
    const value = lo + (hi - lo) * i / 3, yy = y(value);
    root.append(svg('line', {x1:left,x2:right,y1:yy,y2:yy,stroke:'#e5ebf2'}), svg('text', {x:left-10,y:yy+3,'text-anchor':'end',fill:'#687c8e','font-size':10}, fmt(value, Math.abs(value)<1 ? 4 : Math.abs(value)<10 ? 2 : 0)));
  }
  let d = '', last = false;
  points.forEach((p, i) => { if (p.pnl == null) { last = false; return; } d += `${last?' L':' M'}${x(coords[i])},${y(p.pnl)}`; last = true; });
  root.append(svg('path', {d,fill:'none',stroke:'#187e8b','stroke-width':2.2,'stroke-linejoin':'round'}));
  if (points.length === 1 && points[0].pnl != null) root.append(svg('circle', {cx:left,cy:y(points[0].pnl),r:3,fill:'#187e8b'}));
  root.append(svg('text', {x:left,y:190,fill:'#687c8e','font-size':10}, 'Position / target (lots)'));
  const min = Math.min(0, ...points.flatMap(p => [p.position, p.target ?? p.position]));
  const max = Math.max(1, ...points.flatMap(p => [p.position, p.target ?? p.position]));
  const yp = n => 249 - (n - min) / Math.max(1, max - min) * 44;
  for (const value of [min, max]) root.append(svg('text', {x:left-10,y:yp(value)+3,'text-anchor':'end',fill:'#687c8e','font-size':10},fmt(value,0)));
  for (const [key,color,dashed] of [['position','#5d7aa5',false],['target','#7565ac',true]]) {
    let path = '', previous = null;
    points.forEach((p,i) => { const value=p[key]; if (value == null) {previous=null;return;} path += previous==null ? ` M${x(coords[i])},${yp(value)}` : ` H${x(coords[i])} V${yp(value)}`; previous=value; });
    root.append(svg('path', {d:path,fill:'none',stroke:color,'stroke-width':2,'stroke-dasharray':dashed?'5 4':'none'}));
  }
  const stamp = (t, compact = false) => dated.length ? date(t, compact) : T.elapsed(t);
  for (let i=0;i<4;i++) {
    const index = Math.round((points.length-1)*i/3);
    const axisTime = monotonic ? times[0] + (times.at(-1)-times[0])*i/3 : times[index];
    root.append(svg('text', {x:left+(right-left)*i/3,y:277,'text-anchor':i===0?'start':i===3?'end':'middle',fill:'#687c8e','font-size':10}, stamp(axisTime, true)));
  }
  const cursor = svg('line', {x1:left,x2:left,y1:18,y2:253,stroke:'#93aabc','stroke-dasharray':'3 3',visibility:'hidden'}); root.append(cursor);
  root.onpointermove = e => {
    const box=root.getBoundingClientRect(), px=(e.clientX-box.left)*900/box.width;
    let index=0; coords.forEach((v,i)=>{if(Math.abs(x(v)-px)<Math.abs(x(coords[index])-px))index=i;});
    const p=points[index]; cursor.setAttribute('x1',x(coords[index]));cursor.setAttribute('x2',x(coords[index]));cursor.setAttribute('visibility','visible');
    $('chart-tooltip').hidden=false;
    text('chart-tooltip',`${stamp(times[index])} ${dated.length?zone:'elapsed'}\nGross PnL ${pnl(p.pnl)} ${data.unit}\nPosition ${p.position} lots · target ${p.target??'—'}`);
  };
  root.onpointerleave=()=>{cursor.setAttribute('visibility','hidden');$('chart-tooltip').hidden=true;};
  text('chart-range',`${stamp(times[0])} — ${stamp(times.at(-1))}`);
  text('chart-note',`${points.length} samples · ${dated.length?'state observation time':'elapsed; datetime unavailable'}${monotonic?'':' · sequence spacing (clock regressed)'}`);
}
function eventName(event) { return typeof event === 'string' ? event : Object.keys(event || {})[0] || '—'; }
function describe(event) {
  if (typeof event === 'string') return '';
  const name = eventName(event); let p = event[name];
  if (name === 'SubmitTargeted') p = p.intent;
  if (name === 'Submit' || name === 'SubmitTargeted') return `${p.side} ${quantity(p.qty)} @ ${price(p.limit)} · order ${p.id}`;
  if (name === 'Execution') return `${eventName(p.report)} · ${JSON.stringify(p.report[eventName(p.report)])}`;
  if (name === 'SetTarget') return `Target ${quantity(p.position)} · revision ${p.revision}`;
  return JSON.stringify(p);
}
function cell(value, className = '') { const td=document.createElement('td');td.textContent=value??'—';td.className=className;return td; }
function timeCell(row) {
  const picked=T.pick(row,basis), td=cell(picked.elapsed?T.elapsed(picked.ms):date(picked.ms),'datetime');
  const note=document.createElement('small');note.textContent=picked.source;td.append(note);
  td.title=`Event: ${date(row.event_time_ms)}\nReceived: ${date(row.received_time_ms)}\nEngine elapsed: ${T.elapsed(row.at)}\nSequence ${row.seq}`;
  return td;
}
function sideCell(side) { const td=cell(''),span=document.createElement('span');span.className='side '+(side==='Buy'?'buy':'sell');span.textContent=side;td.append(span);return td; }
function renderTable() {
  const ledger=data.ledger, rows=ledger.rows;
  const headers=kind==='trades'?['Datetime','Execution / order','Side','Quantity','Price','Fee']:kind==='orders'?['Created / first observed','Order','Side','Quantity / filled','Limit price','Current state']:kind==='signals'?['Datetime','Sequence','Target / revision','Result']:['Datetime','Sequence','Event','Detail / outcome'];
  const head=document.createElement('tr');for(const name of headers){const th=document.createElement('th');th.textContent=name;head.append(th);}
  $('table-head').replaceChildren(head);$('table-body').replaceChildren();
  for(const r of rows){
    const tr=document.createElement('tr');
    if(kind==='trades')tr.append(timeCell(r),cell(`${r.id} / ${r.order_id}`),sideCell(r.side),cell(quantity(r.qty),'numeric'),cell(price(r.price),'numeric'),cell(r.fee?`${r.fee.amount} ${r.fee.asset}`:'Not recorded','status'));
    else if(kind==='orders')tr.append(timeCell(r),cell(r.id),sideCell(r.side),cell(`${quantity(r.qty)} / ${quantity(r.filled)}`,'numeric'),cell(price(r.price),'numeric'),cell(`${r.status}${r.uncertain?' · uncertain':''}${r.pending?' · pending '+r.pending:''}`,'status'));
    else if(kind==='signals')tr.append(timeCell(r),cell(r.seq),cell(describe(r.event)),cell(r.accepted?'Accepted':'Refused',r.accepted?'positive':'negative'));
    else {let detail=describe(r.event);if(r.effects?.length)detail+=' '+r.effects.map(e=>`${eventName(e)}: ${typeof e==='string'?'':JSON.stringify(e[eventName(e)])}`).join(' / ');tr.append(timeCell(r),cell(r.seq),cell(eventName(r.event)),cell(detail,'reason'));}
    $('table-body').append(tr);
  }
  if(!rows.length){const tr=document.createElement('tr'),td=cell('No matching records. Clear filters or choose another time range.','no-rows');td.colSpan=headers.length;tr.append(td);$('table-body').append(tr);}
  text('row-status',`${rows.length} shown · ${fmt(ledger.total,0)} matching · newest observed first`);
  text('page-label',`Page ${cursors.length+1}`);$('prev').disabled=!cursors.length;$('next').disabled=!ledger.next_cursor;
  text('latest',ledger.newer?`${fmt(ledger.newer,0)} new · Latest records`:'Latest records');
  text('range-note',ledger.missing_time?`${ledger.missing_time} records without this datetime excluded`:after?`Journal events after sequence ${after}`:kind==='orders'?'Order states are current; dates identify first observation':'');
}
function visitKey() { return `mn-seen:${selected}`; }
function visit() {
  if(!visits.has(selected)){let value=null;try{value=JSON.parse(saved(visitKey(),'null'));}catch{}visits.set(selected,value);}
  return visits.get(selected);
}
function renderVisit() {
  const baseline=visit();
  const valid=baseline && baseline.generation===data.generation && /^\d+$/.test(baseline.seq||'') && BigInt(data.seq)>=BigInt(baseline.seq);
  const delta=valid?BigInt(data.seq)-BigInt(baseline.seq):0n;
  $('return-notice').hidden=!valid||delta===0n;
  text('return-text',`${delta.toLocaleString()} journal events since your last visit${baseline?.at?' ('+date(baseline.at)+')':''}. History is retained while this tab is closed.`);
  if(!data.catching_up&&!data.error&&!document.hidden) save(visitKey(),JSON.stringify({generation:data.generation,seq:data.seq,at:Date.now()}));
}
function render() {
  $('empty').hidden=!!data.ready;$('workspace').hidden=!data.ready;
  text('run-title',data.name?.replace(/\/events\.jsonl$/,'')||'Loading journal');text('symbol',data.symbol);text('mode',mode(data.mode||'journal'));text('sequence',`Sequence ${data.seq||'0'}`);
  notify(data.error?`Observer stopped: ${data.error}. Values below are the last validated state.`:'');
  if(!data.ready)return;
  const p=data.pnl;
  for(const [id,value] of [['gross',p.gross],['realized',p.realized],['unrealized',p.unrealized]]){text(id,pnl(value));polarity(id,value);}
  text('position',quantity(data.position));text('target',quantity(data.target?.position));text('position-bounds',`· bounds ${quantity(Number(data.lower))} / ${quantity(Number(data.upper))}`);text('qty-unit',data.lot?'base units':'lots');text('unit',data.unit);
  text('fills',fmt(data.fill_count,0));text('trade-tab-count',fmt(data.fill_count,0));text('open-orders',data.open_orders);text('total-orders',data.order_count);
  text('health',data.killed?'Kill latched':data.health);$('health-dot').classList.toggle('offline',data.health!=='Healthy'||data.killed);
  const last=data.time.received_time_ms??data.time.event_time_ms;
  text('engine-time',last!=null?date(last):`Elapsed ${T.elapsed(data.engine_time)}`);$('engine-time').title=`Engine elapsed ${T.elapsed(data.engine_time)}`;
  text('refusals',data.refusals);text('recoveries',data.recoveries);text('fees',p.fees==null?'—':`${fmt(p.fees,6)} ${data.unit}`);text('net',pnl(p.net));
  text('fee-note',p.fees==null?'Net PnL unavailable until complete fee records are available.':'Complete audited fees; base fees converted at execution price.');
  text('mark-basis',data.position===0?'Gross cash from fills':data.mark_stale?'Last known mark · stale':'Marked at executable side');
  const mark=data.mark_time||{},markAt=mark.received_time_ms??mark.event_time_ms;
  text('mark-detail',data.mark==null?'No market mark recorded':`${data.position<0?'Ask':'Bid'} ${price(data.mark)} · ${markAt!=null?date(markAt):'elapsed '+T.elapsed(data.mark_at)}${data.mark_stale?' · stale':''}`);
  text('last-update',`Observer read ${data.updated_at?date(Number(data.updated_at)):'—'} · ${zone}`);
  text('observer',paused?'Auto refresh paused':data.error?'Stopped':data.catching_up?'Replaying journal':data.pending_tail?'Waiting for frame':Date.now()-Number(data.updated_at)<3000?'Receiving updates':'No new events');
  text('time-note',last==null?'Legacy run: missing datetimes stay unknown. Trade audits may provide event time.':'Event time may arrive out of order. History pages follow journal sequence.');
  const signature=JSON.stringify([data.id,data.generation,data.seq,data.tick,data.lot,zone]);
  if(signature!==renderedChart){drawChart();renderedChart=signature;}
  renderTable();renderVisit();
}
function params() {
  const p=new URLSearchParams({kind,limit:String(PAGE),basis,side:$('side').value,q:$('activity-search').value});
  if(before)p.set('before',before);if(until!=null)p.set('until',until);if(after!=null)p.set('after',after);
  if(range.from!=null)p.set('from',range.from);if(range.to!=null)p.set('to',range.to);
  return p;
}
async function fetchJSON(url, signal) {
  const r=await fetch(url,{signal,cache:'no-store'});
  if(!r.ok)throw Error(`HTTP ${r.status}`);
  return r.json();
}
function refresh(resume=false) {
  if(resume){paused=false;text('pause','Pause view');}
  clearTimeout(timer);active?.abort();revision++;
  if(!paused)poll(revision);
}
async function poll(token) {
  const controller=new AbortController();active=controller;
  const timeout=setTimeout(()=>controller.abort(),8000);let delay=500;
  try {
    const payload=await fetchJSON('/api/sessions',controller.signal);
    if(token!==revision)return;
    sessions=payload.sessions.sort((a,b)=>Number(b.modified_at)-Number(a.modified_at));
    // A restarting observer may briefly list no sessions. Keep the selection and last view.
    if(!selected&&sessions.length){selected=sessions[0].id;save('mn-session',selected);}
    renderSessions();
    if(selected&&sessions.some(s=>s.id===selected)) {
      const next=await fetchJSON(`/api/session/${selected}?${params()}`,controller.signal);
      if(token!==revision||paused)return;
      if(data && next.generation!==data.generation){resetPage();after=null;visits.set(selected,null);data=null;refresh();return;}
      if(data?.ready && (!next.ready || BigInt(next.seq)<BigInt(data.seq))) {
        notify('Observer is rebuilding history. Keeping the last displayed state until replay catches up.');
      } else {data=next;render();}
    } else if(data) notify('Waiting for the observer to rediscover this journal. The last view is retained.');
    failures=0;text('connection','Observer connected');$('connection-dot').classList.remove('offline');
  } catch(error) {
    if(token!==revision)return;
    failures++;delay=Math.min(30000,500*2**Math.min(failures,6))*(.8+Math.random()*.4);
    text('connection',`Retry in ${Math.ceil(delay/1000)}s`);$('connection-dot').classList.add('offline');
    notify('Observer connection lost or timed out. Values are frozen; retrying automatically. History comes from the journal after reconnection.');
  } finally {
    clearTimeout(timeout);
    if(token===revision&&!paused){active=null;timer=setTimeout(()=>poll(token),document.hidden?Math.max(5000,delay):delay);}
  }
}
$('timezone').value=zone;
$('timezone').onchange=()=>{zone=$('timezone').value;save('mn-timezone',zone);$('from-time').value='';$('to-time').value='';range={from:null,to:null};resetPage();renderedChart='';refresh(true);};
$('time-basis').onchange=()=>{basis=$('time-basis').value;range={from:null,to:null};$('from-time').value='';$('to-time').value='';$('from-time').disabled=$('to-time').disabled=basis==='engine';resetPage();refresh(true);};
$('apply-range').onclick=()=>{try{range={from:T.parseInput($('from-time').value,zone),to:T.parseInput($('to-time').value,zone)};if(range.from!=null&&range.to!=null&&range.from>range.to)throw Error('From must precede To');resetPage();refresh(true);}catch(e){notify(e.message);}};
$('clear-range').onclick=()=>{$('from-time').value='';$('to-time').value='';$('activity-search').value='';$('side').value='all';range={from:null,to:null};after=null;resetPage();refresh(true);};
$('latest').onclick=()=>{after=null;resetPage();refresh(true);};
$('pause').onclick=()=>{paused=!paused;text('pause',paused?'Resume view':'Pause view');if(paused){clearTimeout(timer);revision++;active?.abort();text('observer','Auto refresh paused');}else refresh();};
$('session-search').oninput=renderSessions;
for(const b of document.querySelectorAll('[data-tab]'))b.onclick=()=>{chooseKind(b.dataset.tab);refresh(true);};
$('activity-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{resetPage();refresh(true);},250);};
$('side').onchange=()=>{resetPage();refresh(true);};
$('prev').onclick=()=>{before=cursors.pop()??null;refresh(true);};
$('next').onclick=()=>{if(!data?.ledger.next_cursor)return;until=data.ledger.until;cursors.push(before);before=data.ledger.next_cursor;refresh(true);};
$('review-unseen').onclick=()=>{const baseline=visit();if(!baseline)return;chooseKind('events');after=baseline.seq;range={from:null,to:null};$('from-time').value='';$('to-time').value='';$('activity-search').value='';$('side').value='all';refresh(true);};
$('mark-seen').onclick=()=>{if(!data)return;visits.set(selected,{generation:data.generation,seq:data.seq,at:Date.now()});renderVisit();};
$('export').onclick=async()=>{
  if(!data?.ready)return;
  const id=selected,generation=data.generation,ceiling=data.seq;let cursor=null;
  const escape=v=>`"${String(v??'').replaceAll('"','""')}"`;
  const chunks=[['execution_id','order_id','side','quantity_lots','price_ticks','event_time_utc','received_time_utc','time_source','engine_ms','sequence','fee','fee_asset'].join(',')+'\n'];
  $('export').disabled=true;text('export','Exporting…');
  try {
    do {
      const p=new URLSearchParams({kind:'trades',limit:'200',until:ceiling});if(cursor)p.set('before',cursor);
      const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);let snapshot;
      try{snapshot=await fetchJSON(`/api/session/${id}?${p}`,controller.signal);}finally{clearTimeout(timeout);}
      if(snapshot.generation!==generation||BigInt(snapshot.seq)<BigInt(ceiling))throw Error('History changed or is rebuilding. Retry export when replay completes.');
      chunks.push(snapshot.ledger.rows.map(r=>[r.id,r.order_id,r.side,r.qty,r.price,r.event_time_ms==null?'':new Date(r.event_time_ms).toISOString(),r.received_time_ms==null?'':new Date(r.received_time_ms).toISOString(),r.time_source,r.at,r.seq,r.fee?.amount,r.fee?.asset].map(escape).join(',')).join('\n')+'\n');
      cursor=snapshot.ledger.next_cursor;
    }while(cursor);
    const url=URL.createObjectURL(new Blob(chunks,{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=`mininautilus-${id}-trades.csv`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
  }catch(e){notify(`Export not completed: ${e.message}`);}finally{$('export').disabled=false;text('export','Export trades ↓');}
};
window.addEventListener('online',()=>refresh());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
refresh();
