"""The dashboard page: one self-contained HTML string, no build step, no CDN.

Deliberately inline: the sandbox preview renders the page inside a sandboxed
iframe with no network access, so a CDN stylesheet or a bundled JS file would
simply be missing and the page would look broken. Everything needed is here.

All requests are *relative* (`/api/…`) because the preview host forwards a port;
an absolute `http://127.0.0.1:8765` call from the browser would never arrive.
"""

from __future__ import annotations

LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scholar Radar · unlock</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0e1116;color:#dfe5eb;
     font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.card{background:#161b22;border:1px solid #232a33;border-radius:14px;padding:26px;width:min(430px,92vw)}
h1{margin:0 0 4px;font-size:19px}p{margin:0 0 16px;color:#8b98a8;font-size:13px}
code{background:#0e1116;padding:2px 5px;border-radius:5px;font-size:12px;color:#7fd1ff}
input{width:100%;box-sizing:border-box;background:#0e1116;border:1px solid #30363d;border-radius:9px;
      color:#dfe5eb;padding:10px 12px;font:14px ui-monospace,SFMono-Regular,Menlo,monospace}
button{margin-top:12px;width:100%;background:#238636;border:0;border-radius:9px;color:#fff;padding:10px;
       font-weight:600;font-size:14px;cursor:pointer}
</style></head><body>
<div class="card">
  <h1>Scholar Radar</h1>
  <p>This dashboard can edit <code>config.yaml</code> and start scrapes, so it asks for a token.
     It lives in <code>data/dashboard.token</code> — open the printed <code>?token=</code> link once
     and a cookie remembers you.</p>
  <input id="t" placeholder="paste the dashboard token" autofocus>
  <button onclick="go()">Unlock</button>
</div>
<script>
function go(){var v=document.getElementById('t').value.trim();if(!v)return;
 localStorage.setItem('radar_token',v);location.href='/?token='+encodeURIComponent(v);}
document.getElementById('t').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></body></html>
"""

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0e1116;color:#dfe5eb;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:#7fd1ff;text-decoration:none}a:hover{text-decoration:underline}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;z-index:30;background:#0e1116ee;backdrop-filter:blur(8px);
  border-bottom:1px solid #232a33;padding:11px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.brand{font-size:16px;font-weight:700;letter-spacing:.2px}
.brand span{color:#4ea1ff}
.pill{display:inline-flex;gap:6px;align-items:center;padding:3px 9px;border-radius:999px;
  background:#161b22;border:1px solid #232a33;font-size:12px;color:#8b98a8}
.pill b{color:#dfe5eb;font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:#8b98a8;display:inline-block}
.dot.on{background:#3fb950}.dot.warn{background:#d29922}.dot.off{background:#f85149}
.spacer{flex:1}
nav{display:flex;gap:2px;padding:0 12px;border-bottom:1px solid #232a33;background:#0e1116;
  position:sticky;top:47px;z-index:25;overflow-x:auto}
nav button{background:none;border:0;border-bottom:2px solid transparent;color:#8b98a8;padding:9px 13px;
  font:inherit;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
nav button:hover{color:#dfe5eb}nav button.sel{color:#dfe5eb;border-bottom-color:#4ea1ff}
main{padding:16px 18px 60px;max-width:1400px}
section{display:none}section.sel{display:block}
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:#161b22;border:1px solid #232a33;border-radius:12px;padding:14px}
.card h2{margin:0 0 10px;font-size:13px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;color:#8b98a8}
.card h2 small{text-transform:none;letter-spacing:0;font-weight:400;color:#6e7f91;font-size:12px}
.stat{font-size:26px;font-weight:700;line-height:1.15}
.stat small{font-size:12px;color:#8b98a8;font-weight:500}
.kv{display:grid;grid-template-columns:132px 1fr;gap:3px 10px;font-size:12.5px}
.kv div:nth-child(odd){color:#8b98a8}
button.btn,input[type=text],input[type=number],select,textarea{font:inherit;color:#dfe5eb;
  background:#0e1116;border:1px solid #30363d;border-radius:8px;padding:7px 10px}
textarea{width:100%;resize:vertical;min-height:56px}
input,select:focus{outline:none;border-color:#4ea1ff}
button.btn{cursor:pointer;background:#21262d;border-color:#30363d;font-size:13px;font-weight:600;padding:7px 12px}
button.btn:hover{background:#30363d}
button.btn.prim{background:#238636;border-color:#2ea043;color:#fff}button.btn.prim:hover{background:#2ea043}
button.btn.warn{background:#4d2d17;border-color:#8a4b1f;color:#ffd8a8}
button.btn.danger{background:#460b12;border-color:#a91f2c;color:#ffb3b8}
button.btn:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
.field{display:flex;flex-direction:column;gap:4px;min-width:120px}
.field label{font-size:11.5px;color:#8b98a8;text-transform:uppercase;letter-spacing:.3px}
.field .help{font-size:11.5px;color:#6e7f91;max-width:340px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#8b98a8;
  border-bottom:1px solid #30363d;padding:7px 8px;position:sticky;top:0;background:#161b22;z-index:2}
td{padding:7px 8px;border-bottom:1px solid #1f252c;vertical-align:top}
tbody tr:hover td{background:#1b222b}
tr.dim td{color:#6e7f91}tr.gone td{opacity:.5}
.score{font-variant-numeric:tabular-nums;font-weight:700}
.s-a{color:#3fb950}.s-b{color:#d29922}.s-c{color:#8b98a8}
.tier{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid #30363d;white-space:nowrap}
.t-must_apply{background:#12261a;border-color:#238636;color:#7ee2a8}
.t-strong{background:#12202e;border-color:#1f6feb;color:#a9d1ff}
.t-worth_a_look{background:#2a2210;border-color:#9e6a03;color:#ffd58a}
.t-low,.t-ineligible{background:#20161a;border-color:#4d3039;color:#c99}
.badge{font-size:11px;padding:1px 6px;border-radius:5px;background:#21262d;color:#8b98a8;border:1px solid #30363d}
.badge.new{background:#12261a;border-color:#238636;color:#7ee2a8}
.badge.chg{background:#12202e;border-color:#1f6feb;color:#a9d1ff}
.badge.star{background:#2a2410;border-color:#9e6a03;color:#ffd58a;cursor:pointer}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}.mut{color:#8b98a8}
pre.log{margin:0;max-height:340px;overflow:auto;background:#0b0f14;border:1px solid #232a33;border-radius:9px;
  padding:10px;font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.scroll{max-height:62vh;overflow:auto;border:1px solid #232a33;border-radius:10px}
#toast{position:fixed;right:16px;bottom:16px;z-index:60;display:flex;flex-direction:column;gap:8px}
.tmsg{background:#161b22;border:1px solid #30363d;border-left:3px solid #4ea1ff;border-radius:9px;padding:10px 13px;
  font-size:13px;max-width:430px;box-shadow:0 8px 24px #0008;animation:in .18s ease-out}
.tmsg.err{border-left-color:#f85149}.tmsg.ok{border-left-color:#3fb950}
@keyframes in{from{transform:translateY(8px);opacity:0}}
#drawer{position:fixed;inset:0 0 0 auto;width:min(680px,100%);background:#0e1116;border-left:1px solid #232a33;
  z-index:50;transform:translateX(100%);transition:transform .2s ease;display:flex;flex-direction:column}
#drawer.open{transform:none}#drawer .body{padding:16px 18px 40px;overflow:auto}
#drawer h3{margin:0 0 3px;font-size:17px;line-height:1.3}
.bar{height:6px;border-radius:99px;background:#21262d;overflow:hidden}
.bar i{display:block;height:100%;background:#4ea1ff}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12px;padding:2px 8px;border-radius:7px;background:#0e1116;border:1px solid #30363d}
.sw{position:relative;width:36px;height:20px;border-radius:99px;background:#30363d;border:0;cursor:pointer;padding:0}
.sw i{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#8b98a8;transition:.15s}
.sw.on{background:#238636}.sw.on i{left:18px;background:#fff}
hr{border:0;border-top:1px solid #232a33;margin:14px 0}
.tabs2{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.tabs2 button{background:#0e1116;border:1px solid #30363d;border-radius:8px;padding:5px 10px;font-size:12.5px;cursor:pointer;color:#8b98a8}
.tabs2 button.sel{border-color:#4ea1ff;color:#dfe5eb}
.mini{font-size:12px;color:#8b98a8}
label.ck{display:inline-flex;gap:6px;align-items:center;font-size:13px;color:#dfe5eb;cursor:pointer}
label.ck input{accent-color:#238636}
@media (max-width:760px){.kv{grid-template-columns:1fr}nav{top:74px}}
"""

JS = r"""
const S = {status:null, settings:null, sch:null, job:null, tab:localStorage.getItem('radar_tab')||'overview',
           filters:{status:'active',sort:'score',limit:120,offset:0,q:'',country:'',degree:'',tier:'',source:'',
                    funding:'',gate:'',starred:0,hidden:0}, detail:null, poll:null, chatPick:''};
const $ = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money = v => v==null?'—':'US$'+Number(v).toLocaleString(undefined,{maximumFractionDigits:0});
const num = v => v==null?'—':Number(v).toLocaleString();
function tok(){ return localStorage.getItem('radar_token')||''; }
async function api(path, opts){
  opts = opts||{};
  const h = Object.assign({'X-Radar-Token':tok()}, opts.headers||{});
  if(opts.body && typeof opts.body!=='string'){ h['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
  const r = await fetch(path, Object.assign({},opts,{headers:h}));
  const ct = r.headers.get('content-type')||'';
  const data = ct.indexOf('json')>=0 ? await r.json() : await r.text();
  if(r.status===401){ askToken(); throw new Error('unauthorized'); }
  if(!r.ok){ const msg = (data && data.error) ? data.error : ('HTTP '+r.status); throw new Error(msg); }
  return data;
}
function askToken(){ const v = prompt('Dashboard token (see data/dashboard.token):'); if(v){ localStorage.setItem('radar_token',v.trim()); loadStatus(); } }
function toast(msg, kind){
  const box = document.createElement('div'); box.className = 'tmsg '+(kind||'');
  box.innerHTML = '<b>'+(kind==='err'?'Failed':kind==='ok'?'Done':'Notice')+'</b><br>'+esc(msg);
  $('#toast').appendChild(box); setTimeout(()=>box.remove(), kind==='err'?9000:4200);
}
const sc = v => v==null?'':(v>=76?'s-a':v>=60?'s-b':'s-c');

/* ---------------------------------------------------------------- tabs */
function showTab(name){
  S.tab = name; localStorage.setItem('radar_tab', name);
  $$('nav button').forEach(b=>b.classList.toggle('sel', b.dataset.tab===name));
  $$('main section').forEach(s=>s.classList.toggle('sel', s.id==='tab-'+name));
  ({overview:loadStatus, scholarships:loadSch, sources:()=>{loadSources();loadStatus();},
    notify:loadNotify, settings:loadSettings, log:loadJobs, exports:loadExports})[name]?.();
}

/* ------------------------------------------------------------- overview */
async function loadStatus(){
  try{
    const st = await api('/api/status'); S.status = st; renderStatus();
    if(st.job && st.job.status==='running'){ setJob(st.job); }
  }catch(e){ if(String(e.message)!=='unauthorized') toast(e.message,'err'); }
}
function renderStatus(){
  const st = S.status; if(!st) return;
  const c = st.counts||{}, tg = st.telegram||{}, llm = st.llm||{}, n = st.notify||{};
  $('#hdr').innerHTML = [
    pill(c.active+' active', 'on'), pill('gate pass '+c.gate_pass, 'on'),
    pill('LLM', llm.enabled ? (llm.has_key?'on':'warn') : 'off', (llm.enabled?(llm.has_key?'model on':'no key'):'regex only')),
    pill('Telegram', tg.configured && tg.verified ? 'on' : (tg.configured?'warn':'off'), tg.bot?('@'+tg.bot.username):'not connected'),
    pill('channels', 'on', (n.channels||[]).join(', ')||'none'),
    n.dry_run ? pill('dry run','warn','nothing is sent') : '',
    st.job ? pill(st.job.status==='running'?'working':'last job', st.job.status==='running'?'warn':(st.job.status==='ok'?'on':'off'), st.job.label) : ''
  ].join('');
  const tiers = c.by_tier||{}; const tot = Math.max(1,Object.values(tiers).reduce((a,b)=>a+b,0));
  $('#stats').innerHTML = [
    stat('Rows in archive', num(c.rows), st.db),
    stat('Pass your gates', num(c.gate_pass), c.active ? Math.round(100*c.gate_pass/c.active)+'% of active' : ''),
    stat('Starred', num(c.starred), 'saved from the Scholarships tab'),
    stat('Hidden', num(c.hidden), 'kept out of digests'),
    stat('Soonest deadline', c.soonest_deadline||'—', c.last_source_run?('last scrape '+c.last_source_run.started_at):'never scraped'),
    stat('Largest award seen', money(c.max_award_usd), 'what is on the table')
  ].join('') + `<div class="card"><h2>Tiers <small>active rows</small></h2>`+
    ['must_apply','strong','worth_a_look','low','ineligible'].filter(t=>tiers[t]).map(t=>
      `<div class="row" style="justify-content:space-between;margin-bottom:5px">
        <span class="tier t-${t}">${t.replace(/_/g,' ')}</span>
        <div class="bar" style="flex:1"><i style="width:${Math.round(100*tiers[t]/tot)}%"></i></div>
        <b style="font-variant-numeric:tabular-nums">${tiers[t]}</b></div>`).join('')+`</div>`;
  const ln = c.last_notification;
  $('#engine').innerHTML = `<div class="kv">
    <div>config</div><div class="mono">${esc(st.config)}</div>
    <div>exports</div><div class="mono">${esc(st.out_dir)}</div>
    <div>playwright</div><div>${st.playwright?'<span class=ok>installed</span>':'<span class=mut>not installed (optional)</span>'}</div>
    <div>LLM</div><div>${esc(llm.model)} · <span class="${llm.enabled?'ok':'mut'}">${llm.enabled?'on':'off'}</span>
       · key ${llm.has_key?'<span class=ok>set</span>':'<span class=bad>missing</span>'}</div>
    <div>gates</div><div>full funding ${st.gates.needs_full_funding?'required':'not required'} ·
       min award ${money(st.gates.min_award_usd)} · fee ≤ ${money(st.gates.max_application_fee_usd)}</div>
    <div>last notify</div><div>${ln?`${ln.channel} · ${ln.status} · ${esc(ln.created_at||'')}`:'<span class=mut>nothing sent yet</span>'}</div>
    <div>quiet hours</div><div>${(n.quiet_hours||[0,7]).map(x=>String(x).padStart(2,'0')+':00').join('–')} UTC ·
       min score ${n.min_score} · max ${n.max_rows} rows</div></div>`;
  $('#sources-mini').innerHTML = sourceTable(st.sources, true);
  const j = st.job;
  $('#ovlog').innerHTML = j ? `<div class="row" style="justify-content:space-between;margin-bottom:6px">
      <span class="mini">${esc(j.label)} · ${j.status}</span>
      <button class="btn" onclick="showTab('log')">open log →</button></div>
      <pre class="log">${esc((j.lines||[]).slice(-9).join('\n'))}</pre>`
    : `<div class="mini mut">No job has run from this dashboard yet. Pick an action on the right —
        “Scrape now” is the same code path as <code>python cli.py run</code>.</div>`;
  const busy = !!(j && j.status==='running');
  $$('[data-run]').forEach(b=>{ b.disabled = busy || !st.writable; });
  $('#ro').classList.toggle('mut', !st.writable);
  $('#ro').textContent = st.writable ? '' : '· server started with --read-only';
}
const pill = (label, dot, val) => `<span class="pill"><i class="dot ${dot||''}"></i>${label}${val?' <b>'+esc(val)+'</b>':''}</span>`;
const stat = (label, value, sub) => `<div class="card"><h2>${label}</h2><div class="stat">${value}</div>
  <div class="mini mut">${esc(sub||'')}</div></div>`;

async function runJob(kind, body){
  const payload = Object.assign({kind}, body||{});
  try{
    const job = await api('/api/jobs',{method:'POST',body:payload});
    setJob(job); toast('Started: '+job.label,'ok'); showTab('log');
  }catch(e){ toast(e.message,'err'); }
}
function setJob(job){
  S.job = job; renderJob();
  clearInterval(S.poll);
  if(job.status==='running'){ S.poll = setInterval(pollJob, 1200); }
}
async function pollJob(){
  if(!S.job) return clearInterval(S.poll);
  try{
    // `after` is a cursor: send back only what is new since the last poll.
    const job = await api('/api/jobs/'+encodeURIComponent(S.job.id)+'?after='+(S.job.next||0));
    S.job = {id:job.id, kind:job.kind, label:job.label, status:job.status, started_at:job.started_at,
             finished_at:job.finished_at, error:job.error, result:job.result, total:job.total, next:job.next,
             lines:(S.job.lines||[]).concat(job.lines||[])};
    renderJob();
    if(job.status!=='running'){ clearInterval(S.poll); S.poll=null; loadStatus();
      if(S.tab==='scholarships') loadSch(); if(S.tab==='sources') loadSources();
      toast(job.label+' → '+job.status, job.status==='ok'?'ok':'err'); }
  }catch(e){ clearInterval(S.poll); S.poll=null; toast(e.message,'err'); }
}
function renderJob(){
  const box = $('#joblog'); if(!box || !S.job) return;
  const j = S.job;
  box.innerHTML = `<div class="row" style="margin-bottom:8px">
      <b>${esc(j.label)}</b><span class="badge">${j.status}</span>
      <span class="mini mut">${esc(j.started_at)}${j.finished_at?(' → '+esc(j.finished_at)):' · running…'}</span>
      <span class="spacer"></span>
      ${j.status==='running'?'<button class="btn" onclick="followTail()"><span id=follow>follow: on</span></button>':''}
    </div><pre class="log" id=jobtext>${esc((j.lines||[]).join('\n'))}</pre>
    ${j.error?`<div class="bad mini" style="margin-top:8px">${esc(j.error)}</div>`:''}
    ${j.result?renderResult(j):''}`;
  const t = $('#jobtext'); if(t && ($('#follow')||{}).textContent!=='follow: off') t.scrollTop = t.scrollHeight;
}
function renderResult(j){
  const r = j.result||{};
  if(j.kind==='run'){
    return `<div class="kv" style="margin-top:10px"><div>found</div><div>${r.candidates} candidates ·
      ${r.new} new · ${r.changed} changed · ${r.duplicates} dup · ${r.rejected} rejected</div>
      <div>sources</div><div>${(r.sources_ok||[]).length} ok ${(r.sources_failed||[]).length?'· <span class=bad>'+esc((r.sources_failed||[]).join(', '))+'</span>':'· 0 failed'}</div>
      <div>LLM</div><div>${r.llm_calls||0} calls · ${num(r.llm_tokens||0)} tokens · $${(r.llm_cost_usd||0).toFixed(4)}</div>
      <div>notified</div><div>${r.notified&&Object.keys(r.notified.sent||{}).length?esc(JSON.stringify(r.notified.sent)):'<span class=mut>nothing sent (threshold or no channel)</span>'}</div>
      <div>exports</div><div>${Object.entries(r.reports||{}).map(([k,v])=>'<a href="/api/export/'+k+'">'+k+'</a>').join(' · ')||'—'}</div></div>`;
  }
  if(j.kind==='health'){
    return `<div class="mini" style="margin-top:8px">${(r.sources||[]).length} sources in the latest run ·
      ${r.unhealthy&&r.unhealthy.length?'<span class=bad>needs attention: '+esc(r.unhealthy.join(', '))+'</span>':'<span class=ok>all healthy</span>'}</div>`;
  }
  if(j.kind==='test-source'){
    return `<div class="mini" style="margin-top:8px">${r.found} candidates · status ${esc(r.status||'')}
      ${(r.preview||[]).map(p=>`<div class="chip">${esc(p.title)} · ${p.score} · ${esc(p.tier||'')}</div>`).join('')}</div>`;
  }
  if(j.kind==='notify'){
    const res=r.result||{};
    return `<div class="mini" style="margin-top:8px">pool ${r.pool} ·
      sent ${esc(JSON.stringify(res.sent||{}))} · skipped ${esc(JSON.stringify(res.skipped||{}))}</div>
      ${r.preview?'<pre class="log" style="margin-top:8px;max-height:180px">'+esc(r.preview)+'</pre>':''}`;
  }
  return '';
}
function followTail(){ const f=$('#follow'); f.textContent = f.textContent==='follow: on'?'follow: off':'follow: on'; }

async function loadJobs(){
  const box = $('#joblist');
  try{
    const data = await api('/api/jobs');
    box.innerHTML = data.recent.length ? data.recent.map(j=>`<tr data-job="${esc(j.id)}" style="cursor:pointer">
      <td>${esc(j.id)}</td><td>${esc(j.label)}</td>
      <td><span class="badge">${j.status}</span></td><td class="mono mini">${esc(j.started_at)} → ${esc(j.finished_at||'running')}</td>
      <td class="mini">${j.total||0} lines${j.error?' · <span class=bad>'+esc(j.error.slice(0,60))+'</span>':''}</td></tr>`).join('')
      : '<tr><td class="mut">No jobs yet — “Run now” on the Overview tab starts one.</td></tr>';
    $$('#joblist tr[data-job]').forEach(tr=>tr.onclick=()=>{ const id=tr.dataset.job;
      api('/api/jobs/'+id+'?after=0').then(j=>{ S.job=j; renderJob(); $('#joblog').scrollIntoView({behavior:'smooth'});
        if(j.status==='running') setJob(j); }); });
    if(data.running) setJob(data.running);
  }catch(e){ box.innerHTML = '<tr><td class="bad">'+esc(e.message)+'</td></tr>'; }
}

/* --------------------------------------------------------- scholarships */
async function loadSch(){
  const f = S.filters;
  const qs = Object.entries(f).filter(([k,v])=>v!==''&&v!==0&&v!==null).map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');
  try{
    const data = await api('/api/scholarships?'+qs); S.sch = data; renderSch();
  }catch(e){ toast(e.message,'err'); }
}
function renderSch(){
  const d = S.sch; if(!d) return;
  const fx = d.facets||{};
  $('#schcount').innerHTML = `<b>${d.rows.length}</b> of ${d.total} rows · page ${Math.floor(d.offset/Math.max(1,d.limit))+1}`;
  const sel = (name, list, all) => `<select data-f="${name}"><option value="">${all||'any'}</option>`+
     list.map(o=>`<option value="${esc(o.value)}" ${String(S.filters[name])===String(o.value)?'selected':''}>${esc(o.value)} (${o.count})</option>`).join('')+`</select>`;
  $('#schfilters').innerHTML = `
    <div class="field"><label>Search</label><input type=text data-f="q" value="${esc(S.filters.q)}" placeholder="title, provider, country, text"></div>
    <div class="field"><label>Country</label>${sel('country', fx.country||[])}</div>
    <div class="field"><label>Degree</label>${sel('degree', fx.degree_level||[])}</div>
    <div class="field"><label>Tier</label>${sel('tier', fx.tier||[])}</div>
    <div class="field"><label>Source</label>${sel('source', fx.source||[])}</div>
    <div class="field"><label>Funding</label>${sel('funding', fx.funding_type||[])}</div>
    <div class="field"><label>Status</label><select data-f="status">${['active','expiring','expired','closed','any']
       .map(v=>`<option ${S.filters.status===v?'selected':''}>${v}</option>`).join('')}</select></div>
    <div class="field"><label>Gates</label><select data-f="gate"><option value="">any</option>
       <option value="pass" ${S.filters.gate==='pass'?'selected':''}>pass</option>
       <option value="fail" ${S.filters.gate==='fail'?'selected':''}>fail</option></select></div>
    <div class="field"><label>Sort</label><select data-f="sort">${[['score','score ↓'],['deadline','deadline ↑'],
       ['amount','award ↓'],['title','title'],['seen','last seen'],['days','days left']]
       .map(([v,l])=>`<option value="${v}" ${S.filters.sort===v?'selected':''}>${l}</option>`).join('')}</select></div>
    <div class="field"><label>Page size</label><select data-f="limit">${[50,120,250,500]
       .map(v=>`<option ${+S.filters.limit===v?'selected':''}>${v}</option>`).join('')}</select></div>
    <label class="ck"><input type=checkbox data-f="starred" ${S.filters.starred?'checked':''}> starred only</label>
    <label class="ck"><input type=checkbox data-f="hidden" ${S.filters.hidden?'checked':''}> show hidden</label>
    <button class="btn prim" onclick="S.filters.offset=0;loadSch()">Apply</button>
    <button class="btn" onclick="resetFilters()">Reset</button>`;
  $$('#schfilters [data-f]').forEach(el=>{
    el.onchange = el.oninput = () => {
      const k = el.dataset.f;
      S.filters[k] = el.type==='checkbox' ? (el.checked?1:0) : (k==='limit'?+el.value:el.value);
      if(S.filters[k]==='' ) delete S.filters[k];
      S.filters.offset = 0;
      if(el.tagName==='SELECT' || el.type==='checkbox') loadSch();
    };
    if(el.type==='text') el.onkeydown = e => { if(e.key==='Enter'){ S.filters.offset=0; loadSch(); } };
  });
  $('#schbody').innerHTML = d.rows.length ? d.rows.map(row=>`
    <tr class="${row.gate_pass?'':'dim'}" data-id="${row.id}">
      <td style="width:34px"><span class="badge ${row.starred?'star':''}" data-star="${row.id}"
          title="${row.starred?'unstar':'star this'}">${row.starred?'★':'☆'}</span></td>
      <td><a href="${esc(row.url)}" target="_blank" rel="noopener">${esc(row.title)}</a>
        <div class="mini mut">${esc(row.provider||'')}${row.university?' · '+esc(row.university):''}
          ${row.amount_text?' · '+esc(row.amount_text):''}</div></td>
      <td>${esc(row.country||'—')}</td><td>${esc(row.degree_level||'—')}</td>
      <td class="mono">${row.deadline||'<span class=mut>rolling</span>'}
        ${row.deadline_estimated?'<span title="estimated">≈</span>':''}</td>
      <td>${row.days_left==null?'—':(row.days_left<0?'<span class=bad>closed</span>':row.days_left+'d')}</td>
      <td>${row.amount_usd!=null?money(row.amount_usd):'<span class=mut>unpriced</span>'}</td>
      <td class="score ${sc(row.match_score)}">${row.match_score==null?'—':row.match_score.toFixed(0)}</td>
      <td>${row.tier?'<span class="tier t-'+row.tier+'">'+row.tier.replace(/_/g,' ')+'</span>':''}</td>
      <td>${row.is_new?'<span class="badge new">new</span> ':''}${row.changed?'<span class="badge chg">changed</span>':''}</td>
    </tr>`).join('') : `<tr><td colspan="10" class="mut" style="padding:22px">
      Nothing matches those filters. Try Status = any, or loosen the gates on the Settings tab.</td></tr>`;
  $$('#schbody tr[data-id]').forEach(tr=>tr.onclick=e=>{
    if(e.target.dataset.star){ e.stopPropagation(); return star(e.target.dataset.star); }
    if(e.target.tagName==='A') return;
    openDetail(tr.dataset.id);
  });
  $('#schpage').innerHTML = `
    <button class="btn" ${d.offset?'':'disabled'} onclick="S.filters.offset=Math.max(0,S.filters.offset-${d.limit});loadSch()">← prev</button>
    <button class="btn" ${d.offset+d.rows.length<d.total?'':'disabled'} onclick="S.filters.offset+=${d.limit};loadSch()">next →</button>`;
}
function resetFilters(){ S.filters = {status:'active',sort:'score',limit:120,offset:0}; loadSch(); }
async function star(id){
  const row = (S.sch.rows||[]).find(r=>String(r.id)===String(id));
  try{ await api('/api/scholarships/'+id,{method:'POST',body:{star:!(row&&row.starred)}}); loadSch(); }
  catch(e){ toast(e.message,'err'); }
}
async function openDetail(id){
  $('#drawer').classList.add('open'); $('#dbody').innerHTML = '<div class="mut">loading…</div>';
  try{
    const r = await api('/api/scholarships/'+encodeURIComponent(id)); S.detail = r; renderDetail();
  }catch(e){ $('#dbody').innerHTML = '<div class="bad">'+esc(e.message)+'</div>'; }
}
function renderDetail(){
  const r = S.detail; if(!r) return;
  const bd = r.score_breakdown||{}, cov = Array.isArray(r.coverage)?r.coverage:[];
  const flags = Array.isArray(r.quality_flags)?r.quality_flags:[];
  $('#dbody').innerHTML = `
  <div class="row" style="align-items:flex-start">
    <div style="flex:1"><h3>${esc(r.title)}</h3>
      <div class="mini mut">${esc(r.provider||'')}${r.university?' · '+esc(r.university):''} ·
        <a href="${esc(r.url)}" target="_blank" rel="noopener">official page ↗</a></div></div>
    <button class="btn ${r.starred?'warn':''}" onclick="dflag('star',${!r.starred})">${r.starred?'★ starred':'☆ star'}</button>
    <button class="btn ${r.hidden?'danger':''}" onclick="dflag('hide',${!r.hidden})">${r.hidden?'shown: no — unhide':'hide'}</button>
    <button class="btn" onclick="$('#drawer').classList.remove('open')">close</button>
  </div>
  <div class="grid g2" style="margin-top:12px">
    <div class="card"><h2>Match</h2>
      <div class="row" style="gap:14px;align-items:flex-end">
        <div class="stat" style="font-size:34px">${(r.match_score??0).toFixed(0)}</div>
        <div><span class="tier t-${r.tier||'low'}">${(r.tier||'?').replace(/_/g,' ')}</span>
          <div class="mini mut">confidence ${r.confidence==null?'—':Math.round(r.confidence*100)+'%'}</div></div>
      </div>
      <div class="bar" style="margin:8px 0 12px"><i style="width:${Math.min(100,r.match_score||0)}%"></i></div>
      ${r.gate_pass?'<div class="ok mini">✓ all hard gates pass</div>'
        :'<div class="bad mini">✗ gated out:</div><ul class="mini">'+
          (r.gate_reasons||[]).map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>'}
      ${Object.keys(bd).length?'<details style="margin-top:10px"><summary class="mini mut">score breakdown &amp; parse notes</summary>'+
        '<pre class="log" style="max-height:280px">'+esc(JSON.stringify(bd,null,1).slice(0,4000))+'</pre></details>':''}
    </div>
    <div class="card"><h2>Money</h2><div class="kv">
      <div>as stated</div><div>${esc(r.amount_text||'—')}</div>
      <div>normalised</div><div>${r.amount_usd!=null?money(r.amount_usd)+' / yr':'<span class=mut>not turned into a number</span>'}</div>
      <div>currency</div><div>${esc(r.currency||'—')}</div>
      <div>fee</div><div>${r.application_fee_usd!=null?money(r.application_fee_usd):'unknown'}</div>
      <div>funding</div><div>${esc(r.funding_type||'—')}</div>
      <div>coverage</div><div class="chips">${cov.length?cov.map(c=>'<span class=chip>'+esc(c)+'</span>').join(''):'<span class=mut>—</span>'}</div>
    </div>
    ${flags.length?'<div class="mini warn" style="margin-top:8px">⚠ '+flags.map(esc).join(' · ')+'</div>':''}
    <div class="mini mut" style="margin-top:6px">amount_usd is the only money figure your
      “min award” gate reads; anything ambiguous stays text so it can't wrongly pass.</div></div>
  </div>
  <div class="grid g2" style="margin-top:12px">
    <div class="card"><h2>Dates &amp; place</h2><div class="kv">
      <div>deadline</div><div>${r.deadline?esc(r.deadline)+(r.days_left!=null?' <span class="mini mut">('+r.days_left+' days)</span>':'')
          :(r.deadline_rolling?'rolling / until filled':'unknown')}</div>
      <div>as stated</div><div>${esc(r.deadline_text||'—')}</div>
      <div>opens</div><div>${esc(r.open_date||'—')}</div>
      <div>intake</div><div>${esc(r.intake||'—')} ${r.round_label?'· '+esc(r.round_label):''}</div>
      <div>country</div><div>${esc(r.country||'—')} <span class="mini mut">${esc(r.country_scope||'')}</span></div>
      <div>eligible</div><div class="mini">${(r.eligible_countries||[]).slice(0,18).map(esc).join(', ')||'—'}</div>
      <div>excluded</div><div class="mini">${(r.excluded_countries||[]).slice(0,12).map(esc).join(', ')||'—'}</div>
      <div>degree / field</div><div>${esc(r.degree_level||'—')} · ${esc(r.field||'—')}</div>
    </div></div>
    <div class="card"><h2>Requirements</h2><div class="kv">
      <div>GPA</div><div>${r.min_gpa??'—'} <span class="mini mut">${r.gpa_scale?('of '+r.gpa_scale):''}</span></div>
      <div>IELTS / TOEFL</div><div>${r.min_ielts??'—'} / ${r.min_toefl??'—'}</div>
      <div>GRE</div><div>${r.requires_gre?'required':'not stated'}</div>
      <div>work exp.</div><div>${r.min_work_experience_years??'—'} years</div>
      <div>age limit</div><div>${r.max_age??'—'}</div>
    </div>
    ${r.eligibility?'<div class="mini" style="margin-top:8px">'+esc(String(r.eligibility).slice(0,900))+'</div>':''}</div>
  </div>
  ${r.description?'<div class="card" style="margin-top:12px"><h2>Notes from the source</h2><div class="mini" style="white-space:pre-wrap">'+esc(String(r.description).slice(0,2600))+'</div></div>':''}
  <div class="card" style="margin-top:12px"><h2>Tracking</h2><div class="kv">
    <div>source</div><div>${esc(r.source||'')} <span class="mini mut">seen in: ${esc(r.seen_in_sources||'')}</span></div>
    <div>seen</div><div>${r.times_seen||1}× · first ${esc(r.first_seen_at||'')} · last ${esc(r.last_seen_at||'')}</div>
    <div>notified</div><div>${r.notified?('yes, '+esc(r.notify_count||0)+'×'):'no'}</div>
    <div>parse</div><div>${esc(r.parse_method||'')} ${r.llm_used?'· LLM used':'· regex only'}</div>
    <div>status</div><div>${esc(r.status||'')}</div></div>
    <div class="field" style="margin-top:10px"><label>Your notes (saved in the DB, never sent anywhere)</label>
      <textarea id=dnotes>${esc(r.notes||'')}</textarea>
      <button class="btn" onclick="dflag('notes',document.getElementById('dnotes').value)">Save notes</button></div>
  </div>`;
}
async function dflag(key, value){
  try{ const body = {}; body[key] = value;
    await api('/api/scholarships/'+encodeURIComponent(S.detail.id),{method:'POST',body});
    toast('saved','ok'); openDetail(S.detail.id); loadSch(); loadStatus();
  }catch(e){ toast(e.message,'err'); }
}

/* -------------------------------------------------------------- sources */
async function loadSources(){
  try{
    const [st, help] = await Promise.all([S.status?Promise.resolve(S.status):api('/api/status'), api('/api/sources/help')]);
    S.status = st; S.kindHelp = help; renderSources(); sourceForm();
  }catch(e){ toast(e.message,'err'); }
}
function sourceTable(sources, mini){
  const sw = s => `<button class="sw ${s.enabled?'on':''}" title="${s.enabled?'disable':'enable'} this source"
      onclick="event.stopPropagation();toggleSource('${esc(s.id)}',${!s.enabled})"><i></i></button>`;
  return `<div class="scroll"><table><thead><tr><th>on</th><th>id</th><th>kind</th><th>priority</th>
    <th>url</th>${mini?'<th>last run</th><th>found</th><th>new</th><th>detail</th><th>cost</th>':''}
    ${mini?'':'<th>pages</th><th>detail</th><th>limit</th><th>interval</th><th></th>'}</tr></thead><tbody>
    ${sources.map(s=>{ const l = s.last||{}; const dot = !l.status?'off':(l.status==='ok'?'on':(l.status==='blocked'?'warn':'off'));
      return `<tr class="${s.enabled?'':'gone'}">
      <td>${mini?`<span class="dot ${dot}"></span>`:sw(s)}</td>
      <td class="mono">${esc(s.id)}${s.note?'<div class="mini mut">'+esc(s.note)+'</div>':''}</td>
      <td>${esc(s.kind)}</td><td>${s.priority}</td>
      <td class="mini" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(String(s.url).replace(/^https?:\/\//,''))}</a></td>
      ${mini?`<td><span class="mini">${esc(l.started_at||'never')}</span></td><td>${l.found==null?'—':l.found}</td>
        <td>${l.new==null?'—':l.new}</td><td>${l.detail_fetches==null?'—':l.detail_fetches}</td>
        <td>${l.cost_usd!=null&&l.cost_usd>0?'$'+l.cost_usd.toFixed(4):'—'}
          ${l.error?'<div class="mini bad">'+esc(l.error.slice(0,80))+'</div>':''}</td>`
        : `<td>${s.pages??''}</td><td>${s.fetch_detail?'on':'off'}${s.max_detail_fetches?(' / '+s.max_detail_fetches):''}</td>
          <td>${s.limit??''}</td><td>${s.min_interval_seconds??'default'}</td>
          <td class="row" style="flex-wrap:nowrap">
            <button class="btn" onclick="testSource('${esc(s.id)}')">test</button>
            <button class="btn danger" onclick="delSource('${esc(s.id)}')">remove</button></td>`}
      </tr>`;}).join('')}</tbody></table></div>`;
}
function renderSources(){
  const st = S.status, list = st.sources||[];
  $('#src-summary').innerHTML = `<div class="grid g4">${
    stat('Enabled', list.filter(s=>s.enabled).length+' / '+list.length, 'runs on every scrape'),
    stat('Broken last run', list.filter(s=>s.last&&s.last.status!=='ok').length, 'non-ok sources'),
    stat('LLM cost last run', '$'+list.reduce((a,s)=>a+((s.last&&s.last.cost_usd)||0),0).toFixed(4), 'per-source budgets'),
    stat('Kinds available', Object.keys(S.kindHelp.kinds||{}).join(', '), 'see “Add source” below')
  }</div>`;
  $('#srctable').innerHTML = sourceTable(list, false);
  $('#srchelp').innerHTML = Object.entries(S.kindHelp.kinds||{}).map(([k,v])=>
    `<span class="chip" title="${esc(v.label)}"><b>${k}</b> ${esc(v.label)}</span>`).join(' ');
}
async function toggleSource(id, enabled){
  try{ await api('/api/settings',{method:'POST',body:{source_edits:[{id, key:'enabled', value:enabled?'true':'false'}]}});
    S.status=null; await loadStatus(); if(S.tab==='sources'){ loadSources(); } toast((enabled?'enabled ':'disabled ')+id,'ok');
  }catch(e){ toast(e.message,'err'); }
}
async function testSource(id){ await runJob('test-source',{source:id, limit:3}); }
async function delSource(id){
  if(!confirm('Remove "'+id+'" from config.yaml? Rows already in the DB stay.')) return;
  try{ await api('/api/settings',{method:'POST',body:{source_delete:id}}); S.status=null; loadStatus(); loadSources();
    toast('removed '+id,'ok'); }catch(e){ toast(e.message,'err'); }
}
function sourceForm(){
  const t = S.kindHelp.template||{};
  $('#addsrc').innerHTML = `<div class="row" style="align-items:flex-end">
    <div class="field"><label>id</label><input type=text id=n_id value="${esc(t.id||'')}" placeholder="my_university_awards"></div>
    <div class="field"><label>kind</label><select id=n_kind>${Object.keys(S.kindHelp.kinds||{}).map(k=>
      `<option ${k==='listing'?'selected':''}>${k}</option>`).join('')}</select></div>
    <div class="field" style="flex:1;min-width:280px"><label>url</label><input type=text id=n_url placeholder="https://…/scholarships/"></div>
    <div class="field"><label>item selector</label><input type=text id=n_item value="${esc(t.item||'article')}" style="width:150px"></div>
    <div class="field"><label>title</label><input type=text id=n_title value="h2 a" style="width:110px"></div>
    <div class="field"><label>link</label><input type=text id=n_link value="a" style="width:80px"></div>
    <div class="field"><label>pages</label><input type=number id=n_pages value="1" style="width:70px"></div>
    <div class="field"><label>limit</label><input type=number id=n_limit value="25" style="width:80px"></div>
    <div class="field"><label>detail url template</label><input type=text id=n_detail placeholder="optional, {url} = row link" style="width:230px"></div>
    <label class="ck"><input type=checkbox id=n_fetch checked> fetch detail pages</label>
    <button class="btn prim" onclick="addSource()">add to config.yaml</button></div>
  <div class="mini mut" style="margin-top:6px">The selector keys are exactly what
    <code>config.yaml</code> uses — a wrong selector yields <code>empty</code>, and
    <i>Test</i> shows why. Nothing is fetched until you save and run.</div>`;
}
async function addSource(){
  const src = {id:$('#n_id').value.trim(), kind:$('#n_kind').value, url:$('#n_url').value.trim(),
               item:$('#n_item').value.trim(), title_selector:$('#n_title').value.trim(),
               link_selector:$('#n_link').value.trim(), pages:+$('#n_pages').value||1,
               limit:+$('#n_limit').value||25, fetch_detail:$('#n_fetch').checked,
               max_detail_fetches:8, priority:40, enabled:true, note:'added from the dashboard'};
  if($('#n_detail').value.trim()) src.detail_url = $('#n_detail').value.trim();
  if(!src.id || !src.url){ toast('id and url are required','err'); return; }
  try{
    await api('/api/settings',{method:'POST',body:{source_add:src}});
    toast('added '+src.id+' — click test before trusting it','ok');
    S.status=null; loadStatus(); loadSources();
    runJob('test-source',{source:src.id, limit:2});
  }catch(e){ toast(e.message,'err'); }
}

/* --------------------------------------------------------------- notify */
async function loadNotify(){
  try{
    const [st, view, tg] = await Promise.all([api('/api/status'), api('/api/settings'), api('/api/telegram')]);
    S.status = st; S.settings = view; S.tg = tg; renderNotify();
  }catch(e){ toast(e.message,'err'); }
}
function renderNotify(){
  const n = S.settings.groups.notify, specs = {}; (S.settings.specs.notify||[]).forEach(s=>specs[s.key]=s);
  $('#nform').innerHTML = Object.entries(n).map(([k,v])=>fieldHtml('notify', k, v, specs[k])).join('');
  bindFields('#nform');
  const tg = S.tg||{}, bot = tg.bot||{};
  $('#tgstate').innerHTML = `
    <div class="kv">
      <div>token</div><div>${tg.configured?'<span class="ok">saved in .env</span>':'<span class="bad">not set</span>'}</div>
      <div>bot</div><div>${bot.username?('<b>@'+esc(bot.username)+'</b> · '+esc(bot.name||'')):'<span class="mut">unverified</span>'}</div>
      <div>chat id</div><div class="mono">${esc(tg.chat_id||'—')}</div>
      <div>channel</div><div>${tg.channel_on?'<span class="ok">telegram is in notify.channels</span>'
         :'<span class="warn">off — tick “telegram” below and save, or the digest never routes here</span>'}</div>
      <div>checked</div><div class="mini mut">${esc(tg.checked_at||tg.bound_at||'never from this server')}</div>
      <div>dry run</div><div>${tg.dry_run?'<span class="warn">on — nothing leaves the box</span>':'<span class="ok">off</span>'}</div>
    </div>`;
  const chats = (tg.chats||[]);
  $('#tgchats').innerHTML = chats.length ? chats.map(c=>`<tr>
      <td class="mono">${esc(c.chat_id)}</td><td>${esc(c.title)}</td><td>${esc(c.type)}</td>
      <td class="mini mut">${esc(c.username?(' @'+c.username):'')}</td><td class="mini">${esc(c.text||'')}</td>
      <td><button class="btn" onclick="$('#tgchat').value='${esc(c.chat_id)}'">use</button></td></tr>`).join('')
    : '<tr><td class="mut">Nothing fetched yet. Press <i>Pair</i> after sending your bot any message in Telegram.</td></tr>';
  $('#tgchat').value = tg.chat_id||S.chatPick||'';
}
function fieldHtml(group, key, value, spec){
  spec = spec||{label:key, type:'text', section:group};
  const id = 'f_'+group+'_'+key;
  const lab = `<label for="${id}">${esc((spec.label||key).replace(/_/g,' '))}</label>`;
  const help = spec.help?`<div class="help">${esc(spec.help)}</div>`:'';
  let input;
  if(spec.type==='bool') input = `<select id="${id}" data-g="${group}" data-k="${key}">
      <option value="true" ${value?'selected':''}>yes</option><option value="false" ${!value?'selected':''}>no</option></select>`;
  else if(spec.type==='select'||spec.options&&spec.type!=='multiselect'&&spec.type!=='list')
    input = `<select id="${id}" data-g="${group}" data-k="${key}">${(spec.options||[]).map(o=>
      `<option ${String(value)===o?'selected':''}>${esc(o)}</option>`).join('')}</select>`;
  else if(spec.type==='multiselect')
    input = `<div class="chips">${(spec.options||[]).map(o=>`<label class="ck">
      <input type=checkbox data-g="${group}" data-k="${key}" value="${esc(o)}"
        ${((value||[]).indexOf(o)>=0)?'checked':''}> ${esc(o)}</label>`).join('')}</div>`;
  else if(spec.type==='list') input = `<input type=text id="${id}" data-g="${group}" data-k="${key}"
      value="${esc((value||[]).join(', '))}" style="min-width:260px">`;
  else input = `<input type="${spec.type==='int'||spec.type==='float'?'number':'text'}" id="${id}"
      data-g="${group}" data-k="${key}" value="${esc(value==null?'':value)}"
      ${spec.type==='float'?'step="0.1"':''}>`;
  return `<div class="field" style="min-width:170px">${lab}${input}${help}</div>`;
}
function bindFields(root){
  $$(root+' [data-k]').forEach(el=>{ el.onchange = null; });
}
function collect(root){
  // Multi-selects render as several checkboxes sharing one key: collect them into
  // one array (empty is a real config value, so missing-vs-empty must stay distinct).
  const out = {}, multi = {};
  $$(root+' [data-k]').forEach(el=>{
    const g = el.dataset.g, k = el.dataset.k;
    out[g] = out[g]||{};
    if(el.type==='checkbox'){ const key = g+'|'+k; multi[key] = multi[key]||[]; if(el.checked) multi[key].push(el.value); return; }
    out[g][k] = el.value;
  });
  Object.keys(multi).forEach(key=>{ const g = key.split('|')[0], k = key.split('|')[1];
    out[g] = out[g]||{}; out[g][k] = multi[key]; });
  return out;
}
async function saveGroups(root, note){
  const groups = collect(root);
  try{
    const res = await api('/api/settings',{method:'POST',body:{groups}});
    toast((res.changed?note||'saved to config.yaml':'nothing changed'),'ok');
    S.settings=null; S.status=null; await loadStatus();
    if(S.tab==='settings') loadSettings(); if(S.tab==='notify') loadNotify();
  }catch(e){ toast(e.message,'err'); }
}
async function tgAct(action, extra){
  const btns = $$('#tgbtns button'); btns.forEach(b=>b.disabled=true);
  const body = Object.assign({action, token:$('#tgtk').value.trim()}, extra||{});
  if(action==='bind'||action==='test') body.chat_id = $('#tgchat').value.trim();
  try{
    const res = await api('/api/telegram',{method:'POST',body});
    S.chatPick = res.chat_id||S.chatPick;
    const tg = await api('/api/telegram'); S.tg = Object.assign(S.tg||{}, tg);
    toast(res.note||res.error||'ok', res.error?'err':'ok');
    if(action==='pair' && res.chat_id) {
      if(confirm('Save this chat id and turn on the telegram channel?\n\n'+res.chat_id))
        await tgAct('bind', {chat_id:res.chat_id, also_channel:true});
    }
    const [v,s] = [api('/api/status'), api('/api/settings')];
    await Promise.all([v,s]).then(([a,b])=>{S.status=a;S.settings=b;renderStatus();renderNotify();});
  }catch(e){ toast(e.message,'err'); const s = await api('/api/settings'); S.settings=s; renderNotify(); }
  finally{ btns.forEach(b=>b.disabled=false); }
}

/* ------------------------------------------------------------- settings */
async function loadSettings(){
  try{
    const [view, st] = await Promise.all([api('/api/settings'), api('/api/status')]);
    S.settings = view; S.status = st; renderSettings();
  }catch(e){ toast(e.message,'err'); }
}
function renderSettings(){
  const view = S.settings, groups = view.groups, specs = view.specs;
  const card = (group, title, sub) => {
    const keys = Object.keys(groups[group]||{});
    return `<div class="card"><h2>${title} <small>${esc(sub||'')}</small></h2>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))" data-grp="${group}">
      ${keys.map(k=>fieldHtml(group, k, groups[group][k], (specs[group]||[]).find(s=>s.key===k))).join('')}</div></div>`;
  };
  $('#setgroups').innerHTML = [
    card('profile','Applicant profile','the matcher reads every one of these'),
    card('gates','Hard gates','a failing gate means the row is out, not just penalised'),
    card('scoring','Scoring','weights sum to 100, tiers set the labels'),
    card('llm','LLM / budget','off = regex only, $0'),
    card('runtime','Runtime','rate limits, cache, staleness'),
  ].join('') + `<div class="row" style="margin-top:12px">
      <button class="btn prim" onclick="saveAllSettings()">Save everything</button>
      <button class="btn" onclick="loadSettings()">Discard &amp; reload</button>
      <span class="mini mut">writes config.yaml (comments kept) and reloads it to prove it parses; a bad value is rolled back.</span></div>`;
  const sec = view.secrets||{};
  $('#secrets').innerHTML = (view.secret_specs||[]).map(sp=>{
    const s = sec[sp.key]||{};
    return `<div class="card"><h2>${esc(sp.label)} <small class="mono">${sp.key}</small></h2>
      <div class="row" style="margin-bottom:6px">
        <span class="pill"><i class="dot ${s.in_env?'on':'off'}"></i>.env</span>
        <span class="pill"><i class="dot ${s.effective?'on':'off'}"></i>in use</span>
        <span class="mini mut">${esc(s.masked)}</span></div>
      <input type=text data-secret="${sp.key}" placeholder="${s.in_env?'leave blank to keep, empty string to clear':'paste value'}" style="width:100%">
      <div class="mini mut" style="margin-top:5px">${esc(sp.help||'')}</div></div>`;
  }).join('');
  $('#setraw').innerHTML = `<div class="row" style="justify-content:space-between;margin-bottom:8px">
      <span class="mini mut">${esc(view.config_path)}</span>
      <button class="btn" onclick="$('#rawbox').hidden=!$('#rawbox').hidden">view / hide</button></div>
    <pre class="log" id="rawbox" hidden style="max-height:420px">${esc((S.rawText||'loading…'))}</pre>`;
  if(!S.rawText) api('/api/raw-config').then(d=>{ S.rawText=d.text; $('#rawbox').textContent=d.text; });
}
async function saveAllSettings(){
  const groups = {};
  $$('[data-grp]').forEach(box=>{
    const g = box.dataset.grp;
    $$('[data-g="'+g+'"]', box).forEach(el=>{
      groups[g]=groups[g]||{};
      if(el.type==='checkbox') return;
      groups[g][el.dataset.k]=el.value;
    });
    const boxes = $$('input[type=checkbox][data-g="'+g+'"]', box);
    if(boxes.length){ const k = boxes[0].dataset.k; groups[g][k] = boxes.filter(b=>b.checked).map(b=>b.value); }
  });
  const secrets = {};
  $$('[data-secret]').forEach(el=>{ if(el.value.trim()) secrets[el.dataset.secret] = el.value.trim(); });
  try{
    const res = await api('/api/settings',{method:'POST',body:{groups, secrets}});
    S.rawText=null; toast(res.changed?('saved'+(Object.keys(secrets).length?' (+secrets in .env)':'')):'nothing changed','ok');
    await loadSettings(); loadStatus();
    if(res.backups&&res.backups.length) toast('backup: '+res.backups.map(b=>b.split('/').pop()).join(', '));
  }catch(e){ toast(e.message,'err'); }
}
async function saveSecretsOnly(){
  const secrets = {}; $$('[data-secret]').forEach(el=>{ if(el.value.trim()) secrets[el.dataset.secret]=el.value.trim(); });
  if(!Object.keys(secrets).length){ toast('nothing typed in the secret boxes','err'); return; }
  try{ await api('/api/settings',{method:'POST',body:{secrets}}); toast('written to .env','ok');
    $$('[data-secret]').forEach(el=>el.value=''); loadSettings(); loadStatus();
  }catch(e){ toast(e.message,'err'); }
}

/* -------------------------------------------------------------- exports */
async function loadExports(){
  try{
    const d = await api('/api/exports');
    $('#exp').innerHTML = `<div class="mini mut" style="margin-bottom:8px">${esc(d.dir)}</div>`+
      d.files.map(f=>`<div class="row" style="justify-content:space-between;padding:7px 0;border-bottom:1px solid #1f252c">
        <span class="mono">${esc(f.name)}</span>
        <span class="mini mut">${f.exists?(num(f.bytes)+' bytes · '+esc(f.modified)):'not generated yet'}</span>
        <span>${f.exists?`<a class="btn" href="/api/export/${f.name}" target="_blank" rel="noopener">open</a>
          <a class="btn" href="/api/export/${f.name}" download>download</a>`:''}</span></div>`).join('')+
      `<div class="mini mut" style="margin-top:10px">These are the exact files
        <code>reporting.export()</code> writes for GitHub Pages, and the same CSV
        <code>cli.py report</code> produces. A scrape refreshes them.</div>`;
  }catch(e){ toast(e.message,'err'); }
}

/* ---------------------------------------------------------------- boot */
const on = (sel, fn) => { const el = $(sel); if(el) el.onclick = fn; };   // a missing control is not fatal
async function boot(){
  $$('nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));
  on('#runnow', ()=>runJob('run',{force:$('#c_force')?.checked, offline:$('#c_off')?.checked, no_notify:$('#c_nn')?.checked}));
  on('#runhealth', ()=>runJob('health'));
  await loadStatus();
  showTab(S.tab);
  S.timer = setInterval(()=>{ if(!document.hidden) loadStatus(); }, 25000);
  document.addEventListener('keydown', e=>{ if(e.key==='Escape') $('#drawer').classList.remove('open'); });
}
document.addEventListener('DOMContentLoaded', boot);
"""

PAGE = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Scholar Radar · control</title>
<style>"""
    + CSS
    + """</style></head><body>
<header>
  <div class="brand">Scholar <span>Radar</span></div>
  <div id="hdr" class="row" style="gap:6px"></div>
  <span class="spacer"></span>
  <label class="ck"><input type=checkbox id=c_off> offline</label>
  <label class="ck"><input type=checkbox id=c_force> force</label>
  <label class="ck"><input type=checkbox id=c_nn> no notify</label>
  <button class="btn prim" id="runnow" data-run>Run now</button>
  <button class="btn" id="runhealth" data-run>Health</button>
  <span id="ro"></span>
</header>
<nav>
  <button data-tab="overview">Overview</button>
  <button data-tab="scholarships">Scholarships</button>
  <button data-tab="sources">Sources</button>
  <button data-tab="notify">Notify</button>
  <button data-tab="settings">Settings</button>
  <button data-tab="log">Log</button>
  <button data-tab="exports">Exports</button>
</nav>
<main>
<section id="tab-overview">
  <div class="grid g4" id="stats" style="margin-bottom:12px"></div>
  <div class="grid g2">
    <div class="card"><h2>Sources <small>latest run per source</small></h2><div id="sources-mini"></div></div>
    <div class="card"><h2>Engine</h2><div id="engine"></div></div>
  </div>
  <div class="grid g2" style="margin-top:12px">
    <div class="card"><h2>Last job</h2><div id="ovlog"></div></div>
    <div class="card"><h2>Actions</h2>
      <div class="row">
        <button class="btn prim" data-run onclick="runJob('run',{force:true})">Scrape + notify</button>
        <button class="btn" data-run onclick="runJob('run',{offline:true})">Rebuild from cache</button>
        <button class="btn" data-run onclick="runJob('notify',{force:true})">Send digest now</button>
        <button class="btn" data-run onclick="runJob('health')">Source health</button>
      </div>
      <div class="mini mut" style="margin-top:8px">One job at a time — they all write the same SQLite file.
        “Rebuild from cache” refilters and rescans what is already in
        <code>data/cache</code>, so you can change a gate and see the effect in seconds without touching the sites.</div>
    </div>
  </div>
</section>

<section id="tab-scholarships">
  <div id="schfilters" class="filters"></div>
  <div class="row" style="margin-bottom:8px"><span id="schcount" class="mini mut"></span>
    <span class="spacer"></span><span id="schpage" class="row"></span></div>
  <div class="scroll"><table>
    <thead><tr><th></th><th>title</th><th>country</th><th>level</th><th>deadline</th><th>in</th>
      <th>award</th><th>score</th><th>tier</th><th>flags</th></tr></thead>
    <tbody id="schbody"></tbody></table></div>
</section>

<section id="tab-sources">
  <div id="src-summary" style="margin-bottom:12px"></div>
  <div class="card"><h2>Configured sources <small>toggle = instant save to config.yaml</small></h2>
    <div id="srctable"></div>
    <div class="mini mut" style="margin-top:8px">Enabled flags, priorities and per-source tuning are edited in
      <span class="mono">Settings → sources</span> above; <i>test</i> runs the real scraper without writing to the DB.</div>
  </div>
  <div class="card" style="margin-top:12px"><h2>Add a source <small>appends a commented block under <span class="mono">sources:</span></small></h2>
    <div class="chips" id="srchelp" style="margin-bottom:10px"></div>
    <div id="addsrc"></div>
  </div>
</section>

<section id="tab-notify">
  <div class="grid g2">
    <div class="card"><h2>Telegram</h2>
      <div id="tgstate"></div>
      <hr>
      <div class="field"><label>bot token <span class="mini mut">@BotFather → paste or leave blank to use the saved one</span></label>
        <input type=text id="tgtk" placeholder="123456789:AA…" style="width:100%"></div>
      <div class="field" style="margin-top:8px"><label>chat id <span class="mini mut">negative for groups — pair, don't guess</span></label>
        <input type=text id="tgchat" placeholder="fills in after Pair" style="width:100%"></div>
      <div class="row" style="margin-top:10px" id="tgbtns">
        <button class="btn prim" onclick="tgAct('verify')">Verify token</button>
        <button class="btn" onclick="tgAct('pair')">Pair</button>
        <button class="btn" onclick="tgAct('bind')">Use this chat</button>
        <button class="btn" onclick="tgAct('test')">Send test message</button>
        <button class="btn warn" onclick="tgAct('digest')">Send real digest</button>
        <button class="btn danger" onclick="tgAct('unbind')">Clear chat</button>
      </div>
      <div class="mini mut" style="margin-top:8px">Verify checks the token with
        <code>getMe</code>. Pair reads <code>getUpdates</code> to find the chat that
        messaged your bot — the id is never guessed. Both write to
        <span class="mono">.env</span>, never to config.yaml.</div>
    </div>
    <div class="card"><h2>Routing &amp; thresholds</h2>
      <div id="nform" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(170px,1fr))"></div>
      <div class="row" style="margin-top:10px">
        <button class="btn prim" onclick="saveGroups('#nform','notify settings saved')">Save notify settings</button>
        <button class="btn" onclick="loadNotify()">reload</button></div>
    </div>
  </div>
  <div class="card" style="margin-top:12px"><h2>Chats that have messaged this bot</h2>
    <div class="scroll"><table><thead><tr><th>chat id</th><th>name</th><th>type</th><th>username</th><th>last text</th><th></th></tr></thead>
      <tbody id="tgchats"></tbody></table></div></div>
</section>

<section id="tab-settings">
  <div id="setgroups"></div>
  <div class="card" style="margin-top:12px"><h2>Secrets <small>.env only · never echoed back by this server</small></h2>
    <div class="grid g3" id="secrets"></div>
    <div class="row" style="margin-top:10px"><button class="btn prim" onclick="saveSecretsOnly()">Write to .env</button>
      <span class="mini mut">config.yaml is committed to git, so no secret is ever stored there.
      Deleting a line here removes it from .env too.</span></div></div>
  <div class="card" style="margin-top:12px"><h2>Raw config <small>read-only mirror of what the engine will load</small></h2>
    <div id="setraw"></div></div>
</section>

<section id="tab-log">
  <div class="grid g2">
    <div class="card"><h2>Jobs <small>this server, newest first — click a row to open it</small></h2>
      <div class="scroll"><table><tbody id="joblist"></tbody></table></div></div>
    <div class="card"><h2>Output</h2><div id="joblog"><div class="mini mut">No job selected.</div></div></div>
  </div>
</section>

<section id="tab-exports">
  <div class="card"><h2>Generated files <small>static export for GitHub Pages + downloads</small></h2>
    <div id="exp"></div></div>
</section>
</main>
<div id="toast"></div>
<aside id="drawer"><div class="body" id="dbody"></div></aside>
<script>"""
    + JS
    + """</script></body></html>
"""
)
