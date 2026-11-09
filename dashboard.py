#!/usr/bin/env python3
"""Local dashboard for the quote database. Standard library only.

    python dashboard.py                 # then open http://localhost:8000
    python dashboard.py --db quotes.db --port 8000

Run it in a second terminal while `collector.py run` is collecting.
It opens the database read-only, so it never interferes with the collector.
"""
import argparse
import json
import sqlite3
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB = "quotes.db"

LATEST = """SELECT q.* FROM quotes q JOIN
  (SELECT MAX(id) mid FROM quotes WHERE game_key IS NOT NULL
   GROUP BY source, game_key, team_code) m ON q.id = m.mid"""


def query(sql, args=()):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def api_games():
    games = {}
    for r in query(LATEST):
        g = games.setdefault(r["game_key"], {"key": r["game_key"], "date": r["game_date"],
                                             "quotes": [], "pregame": r["pregame"]})
        g["quotes"].append({k: r[k] for k in
                            ("source", "team_code", "buy_price", "buy_size", "best_bid", "ts")})
    out = []
    for g in games.values():
        a, b = g["key"].split(":")[1].split("-")
        best = {}
        for q in g["quotes"]:
            if q["buy_price"] is not None and (q["team_code"] not in best
                                               or q["buy_price"] < best[q["team_code"]]["price"]):
                best[q["team_code"]] = {"price": q["buy_price"], "source": q["source"]}
        g.update(teams=[a, b], best=best,
                 gap=round(1 - best[a]["price"] - best[b]["price"], 4) if a in best and b in best else None,
                 sources=sorted({q["source"] for q in g["quotes"]}))
        out.append(g)
    stats = query("""SELECT source, COUNT(*) n, MAX(ts) last FROM quotes GROUP BY source""")
    unmatched = query("SELECT COUNT(*) n FROM quotes WHERE game_key IS NULL")[0]["n"]
    return {"games": out, "stats": stats, "unmatched": unmatched}


def api_history(game):
    """Cheapest-of-each gap over time, carrying the latest quote of each source forward."""
    a, b = game.split(":")[1].split("-")
    latest, pts = {}, []
    for r in query("SELECT ts, source, team_code, buy_price FROM quotes WHERE game_key=? ORDER BY id",
                   (game,)):
        latest[(r["source"], r["team_code"])] = r["buy_price"]
        pa = [p for (s, t), p in latest.items() if t == a and p is not None]
        pb = [p for (s, t), p in latest.items() if t == b and p is not None]
        if pa and pb:
            pts.append({"ts": r["ts"], "gap": round(1 - min(pa) - min(pb), 4)})
    step = max(1, len(pts) // 300)
    return {"points": pts[::step]}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>NBA price-gap scanner</title>
<style>
:root{--bg:#fafaf8;--card:#fff;--fg:#1c1c1a;--mute:#75746e;--line:#e4e3dd;--good:#1a7f4b;--bad:#b3261e;--acc:#2b5fd9}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1d;--fg:#ecebe6;--mute:#9a9992;--line:#34332f;--good:#4cc38a;--bad:#f2766d;--acc:#7fa5ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:20px}h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mute);margin-bottom:16px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.bar{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:12px 0}
.chip{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{width:100%;border-collapse:collapse}th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:500;font-size:12px}
tr.g{cursor:pointer}tr.g:hover{background:var(--bg)}tr.sel{background:var(--bg)}
.pos{color:var(--good);font-weight:600}.neg{color:var(--bad)}.mute{color:var(--mute)}
input{width:70px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
#detail{margin-top:16px;padding:16px;display:none}#detail h2{font-size:16px;margin:0 0 10px}
svg{width:100%;height:140px;display:block;margin-top:12px}.note{color:var(--mute);font-size:12px;margin-top:14px}
</style></head><body><main>
<h1>NBA price-gap scanner</h1>
<div class="sub">Cheapest way to buy both teams across platforms. A positive gap is only a <b>candidate</b>: fees, size and rule matching are not yet applied.</div>
<div class="bar" id="status"></div>
<div class="bar"><label>Min gap <input id="min" type="number" step="0.005" value="-1"></label>
<label><input id="multi" type="checkbox" checked style="width:auto"> Only games on 2+ sources</label></div>
<div class="card"><table><thead><tr><th>Game</th><th>Date</th><th>Sources</th><th>Best A</th><th>Best B</th><th>Cost of both</th><th>Gap</th></tr></thead>
<tbody id="rows"></tbody></table></div>
<div class="card" id="detail"></div>
<div class="note">Sportsbook prices include the bookmaker's margin and have no published size. Prices are dollars per $1 payout. Refreshes every 15 seconds.</div>
</main><script>
const $=id=>document.getElementById(id);let sel=null,data=null;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const age=ts=>{const s=(Date.now()-new Date(ts))/1000;return s<90?Math.round(s)+"s":s<5400?Math.round(s/60)+"m":Math.round(s/3600)+"h"};
const gapCell=g=>g==null?'<span class="mute">--</span>':`<span class="${g>0?'pos':'neg'}">${g>0?'+':''}${g.toFixed(3)}</span>`;
function render(){
  const min=parseFloat($("min").value),multi=$("multi").checked;
  const gs=data.games.filter(g=>(!multi||g.sources.length>1)&&(g.gap==null?min<=-1:g.gap>=min))
    .sort((a,b)=>(b.gap??-9)-(a.gap??-9));
  $("rows").innerHTML=gs.map(g=>{const[a,b]=g.teams,x=g.best[a],y=g.best[b];
    return `<tr class="g ${g.key===sel?'sel':''}" data-k="${esc(g.key)}"><td>${a} vs ${b}</td><td>${esc(g.date)}</td>
    <td>${g.sources.length}</td><td>${x?x.price.toFixed(3)+' <span class="mute">'+esc(x.source)+'</span>':'--'}</td>
    <td>${y?y.price.toFixed(3)+' <span class="mute">'+esc(y.source)+'</span>':'--'}</td>
    <td>${x&&y?(x.price+y.price).toFixed(3):'--'}</td><td>${gapCell(g.gap)}</td></tr>`}).join("")
    ||'<tr><td colspan="7" class="mute" style="text-align:left">No games match. Is the collector running?</td></tr>';
  document.querySelectorAll("tr.g").forEach(r=>r.onclick=()=>{sel=r.dataset.k;render();detail()});
}
async function detail(){
  const g=data.games.find(x=>x.key===sel);if(!g){$("detail").style.display="none";return}
  const[a,b]=g.teams,srcs=g.sources,d=$("detail");d.style.display="block";
  const cell=(s,t)=>{const q=g.quotes.find(z=>z.source===s&&z.team_code===t);
    return q&&q.buy_price!=null?[q.buy_price.toFixed(3),q.buy_size==null?"n/a":Math.round(q.buy_size),age(q.ts)]:["--","","" ]};
  d.innerHTML=`<h2>${a} vs ${b} <span class="mute">${esc(g.date)}</span></h2><table><thead><tr><th>Source</th>
    <th>${a} price</th><th>size</th><th>${b} price</th><th>size</th><th>age</th></tr></thead><tbody>`+
    srcs.map(s=>{const x=cell(s,a),y=cell(s,b);return `<tr><td>${esc(s)}</td><td>${x[0]}</td><td>${x[1]}</td><td>${y[0]}</td><td>${y[1]}</td><td>${x[2]||y[2]}</td></tr>`}).join("")+
    `</tbody></table><div class="mute" style="margin-top:12px">Gap over time (cheapest of each team across sources)</div><svg id="chart"></svg>`;
  const h=await (await fetch("/api/history?game="+encodeURIComponent(sel))).json(),p=h.points,svg=$("chart");
  if(p.length<2){svg.outerHTML='<div class="mute">Not enough history yet.</div>';return}
  const W=900,H=140,lo=Math.min(0,...p.map(z=>z.gap)),hi=Math.max(0.001,...p.map(z=>z.gap)),
    X=i=>i/(p.length-1)*W,Y=v=>H-8-(v-lo)/(hi-lo)*(H-16);
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("preserveAspectRatio","none");
  svg.innerHTML=`<line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="currentColor" stroke-opacity=".3" stroke-dasharray="4"/>
    <polyline fill="none" stroke="var(--acc)" stroke-width="2" vector-effect="non-scaling-stroke" points="${p.map((z,i)=>X(i)+","+Y(z.gap)).join(" ")}"/>`;
}
async function load(){
  try{data=await (await fetch("/api/games")).json()}catch(e){$("status").innerHTML='<span class="chip"><span class="dot" style="background:var(--bad)"></span>Dashboard cannot reach the server</span>';return}
  const last=data.stats.map(s=>s.last).sort().pop(),fresh=last&&(Date.now()-new Date(last))<300000;
  $("status").innerHTML=`<span class="chip"><span class="dot" style="background:${fresh?'var(--good)':'var(--bad)'}"></span>${last?(fresh?"Collecting":"Collector idle")+" · last quote "+age(last)+" ago":"No data yet"}</span>`+
    data.stats.map(s=>`<span class="chip">${esc(s.source)} <b>${s.n}</b></span>`).join("")+
    (data.unmatched?`<span class="chip" title="Quotes whose teams or date could not be mapped">Unmatched <b>${data.unmatched}</b></span>`:"");
  render();if(sel)detail();
}
$("min").oninput=$("multi").onchange=render;load();setInterval(load,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            elif u.path == "/api/games":
                body, ctype = json.dumps(api_games()).encode(), "application/json"
            elif u.path == "/api/history":
                game = parse_qs(u.query).get("game", [""])[0]
                body, ctype = json.dumps(api_history(game)).encode(), "application/json"
            else:
                return self.send_error(404)
        except sqlite3.Error as e:
            body, ctype = json.dumps({"error": str(e)}).encode(), "application/json"
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    global DB
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="quotes.db")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    DB = args.db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Dashboard on {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
