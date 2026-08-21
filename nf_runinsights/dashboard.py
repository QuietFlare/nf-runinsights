"""
nf-runinsights dashboard, a zero-dependency local web UI for the run
history store.

    pipx run nf-runinsights-dashboard                     # http://localhost:8765
    nf-runinsights-dashboard --history /shared/team/runinsights
    nf-runinsights-dashboard --port 9000
    NF_RUNINSIGHTS_HISTORY=s3-synced/dir nf-runinsights-dashboard
    python3 dashboard/app.py                              # from a repo checkout

Store resolution (mirrors the plugin's default so zero config agrees):
--history flag > NF_RUNINSIGHTS_HISTORY env > ~/.nf-runinsights/history

Pure Python stdlib: no pip installs, no node, no build step. Pick runs,
compare them side by side, and (optionally) ask free-form questions -
the Ask feature lights up when the `anthropic` package is installed and
ANTHROPIC_API_KEY is set; without them everything else still works.

Read-only by design: this server can only read the history directory.

All data logic lives in the shared module store.py (also used by
mcp_server.py), this file is only the web door.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nf_runinsights import store


# ---------------------------------------------------------------------------
# The page (vanilla JS, self-contained, dark)
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>nf-runinsights</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { background:#0f1117; color:#e2e8f0; font:15px/1.5 -apple-system, system-ui, sans-serif;
         max-width:960px; margin:0 auto; padding:1.5rem 1rem; }
  h1 { font-size:1.5rem; margin:0; } h1 small { color:#64748b; font-weight:400; font-size:0.9rem; }
  h2 { font-size:1.05rem; border-bottom:1px solid #2d3748; padding-bottom:0.3rem; margin-top:1.6rem; }
  select, input, button { background:#1a1f2b; color:#e2e8f0; border:1px solid #2d3748;
         border-radius:6px; padding:0.4rem 0.7rem; font-size:0.9rem; }
  button { cursor:pointer; } button:hover:not(:disabled) { border-color:#3b82f6; }
  button:disabled { opacity:0.4; cursor:default; }
  .primary { background:#3b82f6; border-color:#3b82f6; font-weight:600; }
  .row { display:flex; gap:0.5rem; flex-wrap:wrap; align-items:center; margin:0.7rem 0; }
  .runs { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:0.5rem; }
  .run { text-align:left; position:relative; padding:0.55rem 0.7rem; border-radius:8px; }
  .run.sel { border-color:#3b82f6; background:#1e2a45; }
  .run .ord { position:absolute; top:0.4rem; right:0.6rem; color:#3b82f6; font-weight:700; font-size:0.8rem; }
  .run .meta { color:#64748b; font-size:0.78rem; display:block; }
  table { border-collapse:collapse; width:100%; font-size:0.85rem; margin-top:0.6rem; }
  th, td { border-bottom:1px solid #2d3748; padding:0.45rem 0.7rem; text-align:left; vertical-align:top; }
  .proc { font-family:ui-monospace,monospace; color:#cbd5e1; }
  .t { font-weight:600; margin-right:0.35rem; } .rss { color:#64748b; font-size:0.78rem; display:block; }
  .d { font-size:0.78rem; color:#64748b; } .worse { color:#f87171; font-weight:600; } .better { color:#4ade80; font-weight:600; }
  .note { color:#64748b; font-size:0.8rem; }
  .err { color:#f87171; }
  #answer { background:#1a1f2b; border:1px solid #2d3748; border-radius:8px; padding:0.8rem 1rem;
            white-space:pre-wrap; margin-top:0.7rem; display:none; }
  #ask-q { flex:1; min-width:260px; }
</style></head><body>
<h1>nf-runinsights <small>cross-run benchmarks</small></h1>
<p class="note">History: <span id="store"></span></p>

<h2>Runs</h2>
<div class="row">
  <label class="note">Pipeline</label><select id="pipeline"></select>
  <button onclick="lastN(2)">Last 2</button>
  <button onclick="lastN(3)">Last 3</button>
  <button onclick="clearSel()">Clear</button>
  <button class="primary" id="cmp" onclick="doCompare()" disabled>Compare</button>
</div>
<div class="runs" id="runs"></div>

<div id="result"></div>

<h2>Ask</h2>
<p class="note">Answered from the recorded metrics of the selected runs (or the whole
pipeline history if none selected). Needs ANTHROPIC_API_KEY on the server; read-only either way.</p>
<div class="row">
  <input id="ask-q" placeholder="e.g. why was the latest run slower? what should I optimize first?"
         onkeydown="if(event.key==='Enter')doAsk()">
  <button class="primary" id="ask-b" onclick="doAsk()">Ask</button>
</div>
<div id="ask-err" class="err"></div>
<div id="answer"></div>

<script>
let all = [], sel = [];
const $ = id => document.getElementById(id);

function fmtMs(ms){ if(ms==null) return "–"; const s=ms/1000;
  if(s<60) return s.toFixed(1)+"s"; const m=Math.floor(s/60); return m+"m "+Math.round(s-m*60)+"s"; }
function fmtB(b){ if(b==null) return "–"; const u=["B","KB","MB","GB","TB"]; let v=b,i=0;
  while(v>=1024&&i<u.length-1){v/=1024;i++;} return (v<10&&i?v.toFixed(1):Math.round(v))+" "+u[i]; }
function fmtTs(ts){ if(!ts) return ""; const d=new Date(ts);
  return isNaN(d)?ts.slice(0,16):d.toLocaleString(undefined,{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"}); }

function visible(){ return all.filter(r=>r.pipeline===$("pipeline").value); }

function renderRuns(){
  const box=$("runs"); box.innerHTML="";
  for(const r of visible().slice().reverse()){   // newest first
    const i=sel.indexOf(r.run_name);
    const b=document.createElement("button");
    b.className="run"+(i>=0?" sel":"");
    b.innerHTML=(i>=0?`<span class="ord">#${i+1}</span>`:"")+
      `<strong>${r.run_name}</strong><span class="meta">${fmtTs(r.ts)} · ${r.process_count} processes</span>`;
    b.onclick=()=>{ const j=sel.indexOf(r.run_name);
      if(j>=0) sel.splice(j,1); else sel.push(r.run_name);
      $("result").innerHTML=""; renderRuns(); };
    box.appendChild(b);
  }
  $("cmp").disabled = sel.length<2;
  $("cmp").textContent = "Compare"+(sel.length?" "+sel.length:"");
}

function lastN(n){ sel = visible().slice(-n).map(r=>r.run_name); $("result").innerHTML=""; renderRuns(); }
function clearSel(){ sel=[]; $("result").innerHTML=""; renderRuns(); }

async function doCompare(){
  const res=await fetch("/api/compare?runs="+encodeURIComponent(sel.join(",")));
  const d=await res.json();
  if(d.error){ $("result").innerHTML=`<p class="err">${d.error}</p>`; return; }
  let h=`<h2>${d.pipeline}, ${d.runs.length} runs</h2><table><tr><th>Process</th>`;
  d.runs.forEach((r,i)=>h+=`<th>${r.run_name}<span class="rss">${i? fmtTs(r.ts):"baseline"}</span></th>`);
  h+="</tr>";
  for(const p of d.processes){
    h+=`<tr><td class="proc" title="${p.process}">${p.process.split(":").pop()}</td>`;
    const base=p.runs[0]&&p.runs[0].median_ms;
    p.runs.forEach((c,i)=>{
      if(!c){ h+="<td>–</td>"; return; }
      let delta="";
      if(i&&base&&c.median_ms!=null){
        const pct=(c.median_ms-base)/base*100, diff=c.median_ms-base;
        // colour only when >=10% AND >=2s absolute, sub-second jitter is noise
        const cls=(Math.abs(pct)>=10&&Math.abs(diff)>=2000)?(pct>0?"worse":"better"):"";
        delta=` <span class="d ${cls}">${pct>0?"+":""}${pct.toFixed(1)}%</span>`;
      }
      h+=`<td><span class="t">${fmtMs(c.median_ms)}</span>${delta}<span class="rss">${fmtB(c.peak_rss)}</span></td>`;
    });
    h+="</tr>";
  }
  h+=`</table><p class="note">Deltas vs #1 (baseline); coloured only at ±10% and 2s+ absolute change.</p>`;
  $("result").innerHTML=h;
}

async function doAsk(){
  const q=$("ask-q").value.trim(); if(!q) return;
  $("ask-b").disabled=true; $("ask-b").textContent="Asking…";
  $("ask-err").textContent=""; $("answer").style.display="none";
  try{
    const res=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({question:q,pipeline:$("pipeline").value,runs:sel})});
    const d=await res.json();
    if(d.error) $("ask-err").textContent=d.error;
    else { $("answer").textContent=d.answer; $("answer").style.display="block"; }
  } catch(e){ $("ask-err").textContent=String(e); }
  $("ask-b").disabled=false; $("ask-b").textContent="Ask";
}

async function init(){
  const d=await (await fetch("/api/runs")).json();
  all=d.runs; $("store").textContent=d.store;
  const pipes=[...new Set(all.map(r=>r.pipeline))];
  $("pipeline").innerHTML=pipes.map(p=>`<option>${p}</option>`).join("");
  if(pipes.length) $("pipeline").value=all.length?all[all.length-1].pipeline:pipes[0];
  $("pipeline").onchange=()=>{ sel=[]; $("result").innerHTML=""; renderRuns(); };
  renderRuns();
}
init();
</script></body></html>
"""


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/runs":
            self._json({"runs": store.runs_summary(), "store": str(store.HISTORY_DIR)})
        elif url.path == "/api/compare":
            names = [
                n for n in parse_qs(url.query).get("runs", [""])[0].split(",") if n
            ]
            if len(names) < 2:
                self._json({"error": "pick at least two runs"}, 400)
            else:
                self._json(store.compare(names))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path != "/api/ask":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON"}, 400)
            return
        question = (body.get("question") or "").strip()
        if not question:
            self._json({"error": "ask a question"}, 400)
            return
        result = store.ask(question, body.get("pipeline"), body.get("runs") or [])
        self._json(result, 200 if "answer" in result else 503)

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("dashboard: %s\n" % (fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description="nf-runinsights local dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--history",
        help="history store directory (default: NF_RUNINSIGHTS_HISTORY env, "
        "then ~/.nf-runinsights/history)",
    )
    args = parser.parse_args()
    if args.history:
        store.set_history(args.history)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"nf-runinsights dashboard: http://{args.host}:{args.port}")
    print(f"history store: {store.HISTORY_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
