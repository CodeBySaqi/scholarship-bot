"""Reporting: a static dashboard (JSON + HTML) plus CSV/markdown exports.

Why static? GitHub Pages hosting costs $0 and needs no server, while a FastAPI
app on a VPS costs money and uptime.  So the workflow commits `docs/` with
`index.html` + `data.json`, and the page filters client-side.  `serve` in the
CLI still gives you a localhost preview while developing.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from db.models import utcnow

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scholarship tracker</title>
<style>
 :root{--bg:#0b1220;--card:#151d2e;--ink:#e8edf7;--mut:#93a1bd;--line:#26314a;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
 header{padding:22px 20px 10px;border-bottom:1px solid var(--line)}
 h1{margin:0 0 4px;font-size:20px} .sub{color:var(--mut);font-size:13px}
 .bar{display:flex;gap:8px;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(11,18,32,.96);backdrop-filter:blur(6px);z-index:3}
 .bar input,.bar select{background:var(--card);border:1px solid var(--line);color:var(--ink);padding:8px 10px;border-radius:8px;font-size:13px}
 .bar input{min-width:220px;flex:1}
 .kpis{display:flex;gap:10px;flex-wrap:wrap;padding:14px 20px}
 .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;min-width:104px}
 .kpi b{display:block;font-size:20px} .kpi span{color:var(--mut);font-size:12px}
 main{padding:0 20px 40px;display:grid;gap:10px}
 .row{display:grid;grid-template-columns:64px 1fr 190px;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px}
 .score{text-align:center;font-size:20px;font-weight:700}
 .score small{display:block;font-size:10px;color:var(--mut);font-weight:500;letter-spacing:.4px}
 h3{margin:0 0 3px;font-size:15px} .meta{color:var(--mut);font-size:12.5px}
 .tags{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}
 .tag{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--mut)}
 .tag.full{color:var(--ok);border-color:#14532d} .tag.money{color:#a7f3d0;border-color:#065f46}
 .warn{color:var(--warn);font-size:12px;margin-top:4px}
 .right{text-align:right;font-size:12.5px;color:var(--mut)}
 .dl{font-size:15px;color:var(--ink);font-weight:600} .dl.soon{color:var(--bad)} .dl.mid{color:var(--warn)}
 a.btn{display:inline-block;margin-top:8px;background:#1d4ed8;color:#fff;text-decoration:none;border-radius:8px;padding:6px 10px;font-size:12.5px}
 .break{margin-top:6px;display:flex;height:6px;border-radius:4px;overflow:hidden;background:#0f172a}
 .break i{display:block} .foot{padding:14px 20px;color:var(--mut);font-size:12px;border-top:1px solid var(--line)}
 @media(max-width:820px){.row{grid-template-columns:56px 1fr}.right{text-align:left}}
</style></head><body>
<header><h1>🎓 Scholarship tracker</h1><div class="sub" id="stamp"></div></header>
<div class="bar">
  <input id="q" placeholder="Search title, university, field…">
  <select id="tier"><option value="">all tiers</option></select>
  <select id="country"><option value="">all countries</option></select>
  <select id="degree"><option value="">all degrees</option></select>
  <select id="fund"><option value="">any funding</option><option>Full</option><option>Partial</option><option>Tuition-only</option></select>
  <select id="sort"><option value="score">sort: score</option><option value="deadline">sort: deadline</option><option value="amount">sort: value</option></select>
</div>
<div class="kpis" id="kpis"></div>
<main id="list"></main>
<div class="foot" id="foot"></div>
<script>
const D = __DATA__;
const COLORS = {country:'#2563eb',field:'#7c3aed',funding:'#059669',amount:'#0d9488',degree:'#ea580c',competition:'#64748b',english_ready:'#eab308',research:'#db2777'};
document.getElementById('stamp').textContent = 'generated ' + D.generated_at + ' · ' + D.rows.length + ' tracked programmes';
const uniq = k => [...new Set(D.rows.map(r=>r[k]).filter(Boolean))].sort();
for(const [id,key] of [['tier','tier'],['country','country'],['degree','degree_level']]){
  const el=document.getElementById(id); uniq(key).forEach(v=>v.split(',').forEach(x=>{
    if(![...el.options].some(o=>o.value===x)) el.insertAdjacentHTML('beforeend',`<option>${x}</option>`)}));
}
const fmt = n => n==null?'—':'$'+Math.round(n).toLocaleString();
function dl(r){ if(r.deadline_rolling) return 'rolling'; if(!r.deadline) return 'unknown';
  const d=(new Date(r.deadline)-Date.now())/864e5; return d<0? 'closed' : (d<45? Math.round(d)+'d left' : r.deadline.slice(0,10)); }
function render(){
  const q=document.getElementById('q').value.toLowerCase(), t=document.getElementById('tier').value,
        c=document.getElementById('country').value, dg=document.getElementById('degree').value,
        f=document.getElementById('fund').value, s=document.getElementById('sort').value;
  let rows=D.rows.filter(r=>(!t||r.tier===t)&&(!c||(r.country||'').includes(c))&&(!f||r.funding_type===f)
      &&(!dg||(r.degree_level||'').includes(dg))&&(!q||(r.title+' '+(r.university||'')+' '+(r.field||'')+' '+(r.country||'')).toLowerCase().includes(q)));
  rows.sort((a,b)=> s==='deadline' ? (a.deadline||'9999')>(b.deadline||'9999')?1:-1 : s==='amount' ? (b.amount_usd||0)-(a.amount_usd||0) : (b.match_score||0)-(a.match_score||0));
  document.getElementById('list').innerHTML = rows.slice(0,400).map(r=>{
    const bd=r.breakdown||{}, tot=Object.values(bd).reduce((a,b)=>a+b,0)||1;
    const bars=Object.entries(bd).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<i title="${k} ${v}" style="width:${100*v/tot}%;background:${COLORS[k]||'#334155'}"></i>`).join('');
    const soon=r.days_left!=null&&r.days_left<45?'soon':(r.days_left!=null&&r.days_left<90?'mid':'');
    const warn=(r.gate_reasons&&r.gate_reasons.warnings||[]).slice(0,2).join(' · ');
    return `<div class="row"><div class="score">${Math.round(r.match_score||0)}<small>${(r.confidence*100||0)|0}% conf</small></div>
      <div><h3>${r.title}</h3><div class="meta">${r.country||'?'} · ${r.degree_level||'?'} · ${r.university||r.provider||''}</div>
      <div class="tags"><span class="tag ${r.funding_type==='Full'?'full':''}">${r.funding_type||'funding ?'}</span>
      <span class="tag money">${fmt(r.amount_usd)}</span><span class="tag">${r.tier||''}</span>
      ${r.deadline_estimated?'<span class="tag">deadline est.</span>':''}${r.llm_used?'<span class="tag">llm-assisted</span>':''}</div>
      <div class="break">${bars}</div>${warn?`<div class="warn">⚠ ${warn}</div>`:''}</div>
      <div class="right"><div class="dl ${soon}">${dl(r)}</div><div>${r.deadline?r.deadline.slice(0,10):(r.deadline_text||'').slice(0,28)}</div>
      <a class="btn" href="${r.url}" target="_blank" rel="noopener">Apply →</a></div></div>`}).join('') || '<p style="color:#93a1bd">nothing matches those filters</p>';
  const soon=D.rows.filter(r=>r.days_left!=null&&r.days_left>=0&&r.days_left<30).length;
  const full=D.rows.filter(r=>r.funding_type==='Full').length;
  document.getElementById('kpis').innerHTML=[['tracked',D.rows.length],['must apply',D.rows.filter(r=>r.tier==='must_apply').length],
    ['fully funded',full],['closing <30d',soon],['sources',D.sources.length],['LLM cost','$'+(D.stats.llm_cost_usd||0).toFixed(3)]]
    .map(([k,v])=>`<div class="kpi"><b>${v}</b><span>${k}</span></div>`).join('');
  document.getElementById('foot').textContent=`${D.sources.length} sources · last run ${D.stats.run_id||'—'} · `+D.sources.map(s=>`${s.source}:${s.items_new}n/${s.items_found}f${s.status!=='ok'?'⚠':''}`).join('  ');
}
['q','tier','country','degree','fund','sort'].forEach(id=>document.getElementById(id).addEventListener('input',render));
render();
</script></body></html>
"""


def rows_to_dicts(rows: Iterable[Any], *, include_text: bool = False) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = r.as_dict()
        if not include_text:
            for k in ("description", "eligibility", "cover"):
                d.pop(k, None)
        for key in ("gate_reasons", "score_breakdown", "coverage", "eligible_countries", "excluded_countries", "extras"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except json.JSONDecodeError:
                    pass
        if d.get("score_breakdown") and isinstance(d["score_breakdown"], dict):
            d["breakdown"] = d["score_breakdown"].get("breakdown", {})
        out.append(d)
    return out


def build_data(session: Any, *, limit: int = 400, statuses: tuple[str, ...] | None = ("active",)) -> dict[str, Any]:
    from db.models import Scholarship, SourceRun

    q = session.query(Scholarship)
    if statuses:
        q = q.filter(Scholarship.status.in_(list(statuses)))
    rows = q.order_by(Scholarship.match_score.desc(), Scholarship.deadline.asc().nullslast()).limit(limit).all()
    runs = session.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(60).all()
    latest_run_id = runs[0].run_id if runs else None
    run_stats = [
        {
            "source": r.source,
            "status": r.status,
            "items_found": r.items_found,
            "items_new": r.items_new,
            "items_changed": r.items_changed,
            "error": (r.error or "")[:180],
            "llm_calls": r.llm_calls,
            "cache_hits": r.cache_hits,
        }
        for r in runs
        if (latest_run_id is None or r.run_id == latest_run_id)
    ]
    totals = session.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(1).first()
    stats = {
        "run_id": totals.run_id if totals else None,
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "llm_calls": totals.llm_calls if totals else 0,
        "llm_cost_usd": round(totals.llm_cost_usd, 4) if totals else 0.0,
        "total_rows": session.query(Scholarship).count(),
    }
    return {
        "generated_at": utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "stats": stats,
        "sources": run_stats,
        "rows": rows_to_dicts(rows),
    }


def export(session: Any, out_dir: Path, *, limit: int = 400, statuses=("active",)) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = build_data(session, limit=limit, statuses=statuses)
    files: dict[str, Path] = {}

    json_path = out_dir / "data.json"
    json_path.write_text(json.dumps(data, default=str, ensure_ascii=False), encoding="utf-8")
    files["json"] = json_path

    html_path = out_dir / "index.html"
    html_path.write_text(TEMPLATE.replace("__DATA__", json.dumps(data, default=str)), encoding="utf-8")
    files["html"] = html_path

    csv_path = out_dir / "scholarships.csv"
    cols = [
        "match_score", "tier", "confidence", "title", "country", "degree_level", "funding_type",
        "amount_usd", "amount_text", "deadline", "days_left", "url", "source", "field", "university", "status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in data["rows"]:
            writer.writerow(row)
    files["csv"] = csv_path

    md_path = out_dir / "summary.md"
    md_path.write_text(markdown_summary(data), encoding="utf-8")
    files["md"] = md_path
    return files


def markdown_summary(data: dict[str, Any]) -> str:
    rows = data["rows"]
    lines = [f"# Scholarship matches — {data['generated_at']}", ""]
    if not rows:
        lines.append("_No tracked rows yet._")
    for i, r in enumerate(rows[:40], 1):
        dl = r.get("deadline") or ("rolling" if r.get("deadline_rolling") else "unknown")
        lines.append(
            f"{i}. **{r['title']}** — {r.get('country') or '?'} · {r.get('funding_type') or '?'} · {dl} · "
            f"score {r.get('match_score', 0):.0f} ({r.get('tier')})\n   <{r['url']}>"
        )
    return "\n".join(lines)


def serve(out_dir: Path, port: int = 8000, host: str = "0.0.0.0") -> None:  # pragma: no cover
    """Dev-only static server (the CI artifact workflow publishes instead)."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(Path(out_dir)))
    ThreadingHTTPServer.allow_reuse_address = True
    print(f"dashboard → http://{host}:{port}  (Ctrl-C to stop)", flush=True)
    try:
        ThreadingHTTPServer((host, port), handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
