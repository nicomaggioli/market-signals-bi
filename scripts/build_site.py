"""Build the Market Signals status site (several static pages, one per tab) from the market-signals-dbt
controller tables, read-only.

    python -I build_site.py <repo root> <output folder>
"""
import datetime as dt
import html
import os
import sys

import duckdb

root, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)
con = duckdb.connect()
con.sql("SET TimeZone='UTC'")
R = lambda t: f"read_parquet('{root}/database/controller/controller_{t}/**/*.parquet', union_by_name=true)"
L = lambda t, k: f"(select * from {R(t)} qualify row_number() over(partition by {k} order by recorded_at desc)=1)"
one = lambda s: con.sql(s).fetchone()[0]
rows = lambda s: con.sql(s).fetchall()
con.sql(f"create temp table ideas as select * from {L('ideas', 'idea_id')}")
con.sql(f"create temp table res as select * from {R('results')}")
con.sql(f"create temp table jobs as select * from {L('jobs', 'job_id')}")
con.sql(f"create temp table fnd as select * from {L('findings', 'finding_id')}")
NYDATE = "cast(timezone('America/New_York', cast({} as timestamptz)) as date)"

# ---- numbers -------------------------------------------------------------------------------------------------------
n_ideas = one("select count(*) from ideas")
n_tested = one("select count(distinct idea_id) from res where not is_control and verdict in ('pass','fail')")
n_passed = one("select count(distinct idea_id) from res where not is_control and verdict='pass'")
n_findings = one("select count(*) from fnd where finding_id like 'F%'")
n_confirmed = one("select count(*) from fnd where status='confirmed'")
n_leads = one("select count(*) from fnd where status='open lead'")
n_noeffect = one("select count(*) from fnd where status in ('no effect','spent')")
n_lessons = one("select count(*) from fnd where status='method lesson'")
n_results = one("select count(*) from res where verdict in ('pass','fail')")
n_luck_tests = one("select count(*) from res where is_control and verdict in ('pass','fail')")
n_jobs = one("select count(*) from jobs")
n_machines = one("select count(distinct claimed_by) from jobs where claimed_by is not null")
newest = one("select max(finished_at) from res")
first = one("select min(finished_at) from res")

import re
didnt = [(i, re.sub(r"\.?:\s*no effect$", ".", t)) for i, t in rows("select finding_id, title from fnd where status in ('no effect','spent') order by finding_id")
         if len(t) <= 100 and not any(w in t.lower() for w in ("mesh", "called cell", "layer"))]

stage_names = {"screen": "First screen", "views": "Second look", "find_battery": "Battery check"}
noise = []
for stage, label in stage_names.items():
    rj, rp, lj, lp = con.sql(f"""select count(*) filter (where not is_control and verdict in ('pass','fail')),
        count(*) filter (where not is_control and verdict='pass'),
        count(*) filter (where is_control and control_kind<>'planted' and verdict in ('pass','fail')),
        count(*) filter (where is_control and control_kind<>'planted' and verdict='pass')
      from res where stage='{stage}'""").fetchone()
    if rj and lj:
        noise.append((label, rp / rj, lp / lj, rj, lj))
screen = next(x for x in noise if x[0] == "First screen")
battery = next((x for x in noise if x[0] == "Battery check"), None)

grp = """case when source='person' then 'People on the team' when source='scout' then 'AI idea scout'
  when source in ('watch_lead','case_lead') then 'AI leads from news and cases' when source='follow_up' then 'Follow-ups of findings'
  else 'Older idea lists, imported' end"""
sources = rows(f"select {grp} g, count(*) n, count(*) filter (where status='tested') t from ideas group by 1 order by 2 desc")
status_names = {"tested": "Tested", "new": "Waiting, not looked at yet", "routed": "Sent to a test, waiting for its turn",
                "needs_data": "Needs data we don't have", "question": "Open question", "needs_person": "Needs a person to decide"}
statuses = rows("select status, count(*) from ideas group by 1 order by 2 desc")
st_items, st_other = [], 0
for s, c in statuses:
    if s in status_names:
        st_items.append((status_names[s], c, ""))
    else:
        st_other += c
if st_other:
    st_items.append(("Other", st_other, ""))
people = one("select count(*) from ideas where source='person'")
people_tested = one("select count(*) from ideas i where source='person' and exists (select 1 from jobs j where j.idea_id=i.idea_id)")

nights = rows(f"select night, avg(gpu_busy_share) g from {R('window_beats')} where gpu_busy_share is not null group by 1 order by 1")
jobs_day = rows(f"select {NYDATE.format('finished_at')} d, count(*) from jobs where finished_at is not null group by 1 having count(*) >= 20 order by 1")
first_night, last_night = nights[0][0], nights[-1][0]
early_gpu = sum(g for d, g in nights if d <= dt.date(2026, 11, 19)) / max(1, sum(1 for d, g in nights if d <= dt.date(2026, 11, 19)))

# ---- series for the line charts ------------------------------------------------------------------------------------
import calendar
import json

ep = lambda d: calendar.timegm(d.timetuple())
hours = rows("select date_trunc('hour', finished_at) h, count(*) from res where verdict in ('pass','fail') and finished_at is not null group by 1 order by 1")
by_h = {h: c for h, c in hours}
h, tot, cum_t, cum_v = hours[0][0], 0, [], []
while h <= hours[-1][0]:
    tot += by_h.get(h, 0)
    cum_t.append(ep(h)); cum_v.append(tot)
    h += dt.timedelta(hours=1)
week_ago = cum_v[-1] - next(v for t, v in zip(reversed(cum_t), reversed(cum_v)) if t <= cum_t[-1] - 7 * 86400)
idea_days = rows(f"select {NYDATE.format('coalesce(created_at, recorded_at)')} d, count(*) from ideas group by 1 order by 1")
idea_t, idea_v, s = [], [], 0
for d, c in idea_days:
    s += c
    idea_t.append(ep(d)); idea_v.append(s)

# ---- helpers -------------------------------------------------------------------------------------------------------
e = lambda s: html.escape(str(s))
n = lambda v: f"{v:,}"
p = lambda v, d=1: f"{v * 100:.{d}f}%"
day = lambda d: f"{d:%b %-d}"
built = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-5)))
VW, VH, PAD = 1000, 200, 10


def fmt(v, kind):
    return f"{round(v * 100)}%" if kind == "pct" else n(round(v))


def change(d, kind):
    sign = "+" if d > 0 else ("−" if d < 0 else "")
    return sign + (f"{round(abs(d) * 100)} pts" if kind == "pct" else n(round(abs(d))))


def chart(cid, title, ts, vs, kind, gran, all_label, big=True, sub=""):
    """A quote header (big number + change) over a line chart, with range buttons and a scrubber (enhanced by JS)."""
    span = (ts[-1] - ts[0]) / 86400
    ranges = [(lbl, d) for lbl, d in (("1W", 7), ("1M", 30)) if span > d * 1.2] + [("ALL", 0)]
    t0, t1, mn, mx = ts[0], ts[-1], min(vs), max(vs)
    xs = [(t - t0) / ((t1 - t0) or 1) * VW for t in ts]
    ys = [VH / 2 if mx == mn else PAD + (1 - (v - mn) / (mx - mn)) * (VH - 2 * PAD) for v in vs]
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    d = vs[-1] - vs[0]
    tone = "up" if d >= 0 else "down"
    head = f'<h1>{e(title)}</h1>' if big else f'<h2 class="chart-title">{e(title)}</h2>'
    btns = "".join(f'<button type="button" data-range="{dd}" data-label="{e(lab)}" aria-pressed="{"true" if dd == 0 else "false"}">{lbl}</button>'
                   for (lbl, dd), lab in zip(ranges, [{"1W": "Past week", "1M": "Past month"}.get(l, all_label) for l, _ in ranges]))
    data = json.dumps({"t": ts, "v": vs, "k": kind, "g": gran}, separators=(",", ":"))
    return f'''<section class="chart-block {tone}{' big' if big else ''}" id="{cid}">
  {head}
  <div class="q-value" data-v>{fmt(vs[-1], kind)}</div>
  <div class="q-change"><span data-c>{change(d, kind)}</span> <span class="q-period" data-p>{e(all_label)}</span></div>
  {f'<p class="q-sub">{sub}</p>' if sub else ''}
  <div class="chart" role="img" aria-label="{e(title)}, line chart, from {fmt(vs[0], kind)} to {fmt(vs[-1], kind)}">
    <svg viewBox="0 0 {VW} {VH}" preserveAspectRatio="none" aria-hidden="true"><line class="base" x1="0" x2="{VW}" y1="{ys[0]:.1f}" y2="{ys[0]:.1f}"/><path class="line" d="{path}"/></svg>
    <span class="hair" hidden></span><span class="tip" hidden></span><span class="dot" hidden></span>
  </div>
  {f'<div class="ranges" role="group" aria-label="Time range">{btns}</div>' if len(ranges) > 1 else '<div class="ranges"></div>'}
  <script type="application/json">{data}</script>
</section>'''


def bars(items, fmtf, tone="up"):
    m = max(v for _, v, _ in items) or 1
    out = [f'<div class="bars {tone}">']
    for label, v, note in items:
        out.append(f'<div class="bar"><span class="bar-label">{e(label)}</span>'
                   f'<span class="bar-track"><span class="bar-fill" style="width:{max(0.6, v / m * 100):.2f}%"></span></span>'
                   f'<span class="bar-value">{fmtf(v)}</span>' + (f'<span class="bar-note">{e(note)}</span>' if note else '') + '</div>')
    out.append('</div>')
    return "\n".join(out)


def noise_chart():
    mx = max(max(r, l) for _, r, l, _, _ in noise)
    out = ['<div class="pairs">']
    for label, r, l, rj, lj in noise:
        out.append(f'''<div class="pair"><span class="pair-label">{e(label)}</span><div class="pair-bars">
  <div class="bar"><span class="bar-track"><span class="bar-fill" style="width:{r / mx * 100:.2f}%"></span></span><span class="bar-value">{p(r)}</span><span class="bar-note">real ideas · {n(rj)} tests</span></div>
  <div class="bar luck"><span class="bar-track"><span class="bar-fill" style="width:{l / mx * 100:.2f}%"></span></span><span class="bar-value">{p(l)}</span><span class="bar-note">luck only · {n(lj)} tests</span></div>
</div></div>''')
    out.append('</div>')
    return "".join(out)


def stats(items):
    return '<dl class="stats">' + "".join(f'<div><dt>{e(k)}</dt><dd>{v}</dd></div>' for k, v in items) + '</dl>'


PILL = {"up": "Confirmed", "flat": "Checking", "down": "No effect"}


def wrow(fid, title, tone, href="findings.html", note=""):
    return (f'<a class="wrow" href="{href}"><span class="w-main"><span class="w-id">{e(fid)}</span>'
            f'<span class="w-title">{e(title)}</span>{f"<span class=w-note>{e(note)}</span>" if note else ""}</span>'
            f'<span class="pill {tone}">{PILL[tone]}</span></a>')


LEADS = [("F02", "Europe and Asia follow the US move a session later", "Probably too small to trade after costs."),
         ("F08", "Thinly traded stocks react to the market a day late", "Small banks and municipal-bond funds."),
         ("F17", "The Treasury trend says something about a few bond funds", "Also some small banks.")]


def sidebar(file):
    find_rows = (wrow("F01", "Recent path predicts next week's move size", "up")
                 + wrow("F03", "Recent extreme days, same signal as F01", "up")
                 + "".join(wrow(i, t, "flat") for i, t, _ in LEADS))
    luck_rows = "".join(f'<a class="wrow" href="how-it-works.html"><span class="w-main"><span class="w-id">{e(lbl)}</span>'
                        f'<span class="w-title">Luck passes {p(l)}</span></span><span class="pill {"up" if r > l * 1.25 else "flat"}">{p(r)}</span></a>'
                        for lbl, r, l, _, _ in noise)
    parts = []
    if file != "findings.html":
        parts.append(f'<div class="card-head"><h2>Findings</h2><span>{n_findings} total</span></div><div class="card-list">{find_rows}</div>')
    parts.append(f'<div class="card-head"><h2>Real ideas passing</h2><span>by stage</span></div><div class="card-list">{luck_rows}</div>')
    return f'<aside class="side"><div class="card">{"".join(parts)}<p class="card-foot">Results to {day(newest)}, {newest:%Y}. Snapshot, not live.</p></div></aside>'


TABS = [("index.html", "Overview"), ("how-it-works.html", "How it works"), ("findings.html", "Findings"),
        ("ideas.html", "Ideas"), ("machine.html", "The machine"), ("next.html", "What's next")]

JS = r'''(function(){
var NF=new Intl.NumberFormat('en-US');
function fmt(v,k){return k==='pct'?Math.round(v*100)+'%':NF.format(Math.round(v));}
function chg(d,k){var s=d>0?'+':(d<0?'−':'');d=Math.abs(d);return s+(k==='pct'?Math.round(d*100)+' pts':NF.format(Math.round(d)));}
function lab(t,g){var o=g==='hour'?{month:'short',day:'numeric',hour:'numeric',timeZone:'America/New_York'}:{month:'short',day:'numeric',timeZone:'UTC'};return new Date(t*1000).toLocaleString('en-US',o)+(g==='hour'?' ET':'');}
var a=document.querySelector('.tabs a[aria-current]'),nv=document.querySelector('.tabs');if(a&&nv&&a.offsetLeft+a.offsetWidth>nv.clientWidth)nv.scrollLeft=a.offsetLeft-16;
document.querySelectorAll('.chart-block').forEach(function(b){
  var D=JSON.parse(b.querySelector('script[type="application/json"]').textContent);
  var box=b.querySelector('.chart'),svg=box.querySelector('svg'),line=svg.querySelector('.line'),base=svg.querySelector('.base');
  var dot=box.querySelector('.dot'),hair=box.querySelector('.hair'),tip=box.querySelector('.tip');
  var V=b.querySelector('[data-v]'),C=b.querySelector('[data-c]'),P=b.querySelector('[data-p]');
  var W=1000,H=200,PAD=10,S=null;
  function draw(days,label){
    var n=D.t.length,t1=D.t[n-1],t0=days?t1-days*86400:-Infinity,I=[];
    for(var i=0;i<n;i++)if(D.t[i]>=t0)I.push(i);
    var mn=Infinity,mx=-Infinity;I.forEach(function(i){mn=Math.min(mn,D.v[i]);mx=Math.max(mx,D.v[i]);});
    var a=D.t[I[0]],z=D.t[I[I.length-1]];
    var xs=I.map(function(i){return (D.t[i]-a)/((z-a)||1)*W;});
    var ys=I.map(function(i){return mx===mn?H/2:PAD+(1-(D.v[i]-mn)/(mx-mn))*(H-2*PAD);});
    line.setAttribute('d','M'+xs.map(function(x,j){return x.toFixed(1)+','+ys[j].toFixed(1);}).join(' L'));
    base.setAttribute('y1',ys[0].toFixed(1));base.setAttribute('y2',ys[0].toFixed(1));
    S={I:I,xs:xs,ys:ys,label:label};
    var d=D.v[I[I.length-1]]-D.v[I[0]];
    b.classList.toggle('up',d>=0);b.classList.toggle('down',d<0);
    show(I.length-1,label);
  }
  function show(j,label){
    var i=S.I[j];V.textContent=fmt(D.v[i],D.k);C.textContent=chg(D.v[i]-D.v[S.I[0]],D.k);P.textContent=label;
  }
  function move(ev){
    if(!S)return;var r=box.getBoundingClientRect(),x=(ev.clientX-r.left)/r.width*W,lo=0,hi=S.xs.length-1;
    while(hi-lo>1){var m=(lo+hi)>>1;if(S.xs[m]<x)lo=m;else hi=m;}
    var j=Math.abs(S.xs[lo]-x)<Math.abs(S.xs[hi]-x)?lo:hi,px=S.xs[j]/W*100,py=S.ys[j]/H*100;
    dot.hidden=hair.hidden=tip.hidden=false;
    dot.style.left=hair.style.left=tip.style.left=px+'%';dot.style.top=py+'%';
    tip.textContent=lab(D.t[S.I[j]],D.g);tip.classList.toggle('right',px>80);tip.classList.toggle('left',px<20);
    var d=D.v[S.I[j]]-D.v[S.I[0]];b.classList.toggle('up',d>=0);b.classList.toggle('down',d<0);
    show(j,lab(D.t[S.I[j]],D.g));
  }
  function leave(){dot.hidden=hair.hidden=tip.hidden=true;var d=D.v[S.I[S.I.length-1]]-D.v[S.I[0]];b.classList.toggle('up',d>=0);b.classList.toggle('down',d<0);show(S.I.length-1,S.label);}
  box.addEventListener('pointermove',move);box.addEventListener('pointerdown',move);
  box.addEventListener('pointerleave',leave);box.addEventListener('pointercancel',leave);box.addEventListener('pointerup',function(ev){if(ev.pointerType!=='mouse')leave();});
  var btns=b.querySelectorAll('[data-range]');
  btns.forEach(function(x){x.addEventListener('click',function(){btns.forEach(function(y){y.setAttribute('aria-pressed',y===x?'true':'false');});draw(+x.dataset.range,x.dataset.label);});});
  var on=b.querySelector('[data-range][aria-pressed="true"]');draw(on?+on.dataset.range:0,on?on.dataset.label:P.textContent);
});
})();'''


def layout(file, title, desc, body):
    nav = "".join(f'<a href="{f}"' + (' aria-current="page"' if f == file else '') + f'>{e(t)}</a>' for f, t in TABS)
    page_title = "Market Signals" if file == "index.html" else f"{title} · Market Signals"
    i = [f for f, _ in TABS].index(file)
    nxt = TABS[i + 1] if i + 1 < len(TABS) else None
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(page_title)}</title>
<meta name="description" content="{e(desc)}">
<link rel="stylesheet" href="style.css">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2300c805'/%3E%3Cpath d='M6 22l6-7 5 4 9-10' stroke='black' stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
</head>
<body>
<header class="site">
  <div class="site-inner">
    <a class="brand" href="index.html"><svg viewBox="0 0 32 32" aria-hidden="true"><path d="M5 23l7-8 5 4 10-11" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>Market Signals</a>
    <nav class="tabs" aria-label="Sections"><div class="tabs-inner">{nav}</div></nav>
  </div>
</header>
<div class="wrap">
<main>
{body}
{f'<p class="next-link"><a href="{nxt[0]}">Next: {e(nxt[1])} <span aria-hidden="true">→</span></a></p>' if nxt else ''}
</main>
{sidebar(file)}
</div>
<footer class="site-foot"><div class="site-inner">
  <span>Updated {built:%b %-d, %Y} from the project's research tables. A snapshot, not live.</span>
  <span>Not investment advice. Nothing here is traded.</span>
</div></footer>
<script>{JS}</script>
</body>
</html>
'''


# ---- pages -----------------------------------------------------------------------------------------------------------
pages = {}

pages["index.html"] = ("Overview", "Where a research project testing thousands of trading ideas stands: what was tested, what held up, and how much was luck.", f'''
{chart("c-results", "Test results recorded", cum_t, cum_v, "int", "hour", "All time")}
{stats([("Ideas written", n(n_ideas)), ("Ideas tested", n(n_tested)), ("Findings", n(n_findings)), ("Confirmed", n(n_confirmed)),
        ("Luck tests", n(n_luck_tests)), ("Jobs run", n(n_jobs)), ("Past week", "+" + n(week_ago) + " results"), ("Last result", f"{day(newest)}, {newest:%H:%M} UTC")])}
<section>
  <h2>About</h2>
  <p>A small team is testing ideas about the stock market on years of price data. Every test is also run on shuffled data, so we know how often a meaningless idea would pass by chance. Only ideas that clear several checks in a row, and then hold up on years they were never tuned on, count as confirmed.</p>
</section>
<section>
  <h2>Where things stand</h2>
  <div class="news">
    <a class="item" href="how-it-works.html"><span class="kicker">How it works</span><span class="headline">Most first-round passes are luck</span><span class="text">At the first screen, real ideas pass {p(screen[1])} of the time and luck-only tests {p(screen[2])}. Only ideas that clear several stages count.</span></a>
    <a class="item" href="findings.html"><span class="kicker">Findings</span><span class="headline">One signal has held up on new data</span><span class="text">A stock's recent path predicts how much it will move next week. Not which way, just how much. It isn't traded yet.</span></a>
    <a class="item" href="ideas.html"><span class="kicker">Ideas</span><span class="headline">Ideas mostly come from AI and older lists</span><span class="text">{n(n_ideas)} ideas so far. {n(dict(statuses).get("new", 0))} are still waiting to be looked at.</span></a>
    <a class="item" href="machine.html"><span class="kicker">The machine</span><span class="headline">It runs overnight on one PC</span><span class="text">{n(n_jobs)} jobs and {n(n_results)} test results since {day(first)}, on a home PC's graphics card.</span></a>
    <a class="item" href="next.html"><span class="kicker">What's next</span><span class="headline">Trading costs and more computers</span><span class="text">Checking whether the signal survives costs, connecting more machines, and putting people's requests first.</span></a>
  </div>
</section>''')

pages["how-it-works.html"] = ("How it works", "How an idea is tested, and how the project measures luck.", f'''
<section class="quote">
  <h1>How it works</h1>
  <div class="q-value">{p(screen[1])} <span class="vs">vs</span> {p(screen[2])}</div>
  <div class="q-change flat">Real ideas vs luck at the first screen</div>
</section>
<section>
  <h2>About</h2>
  <p>An idea is something like "when oil jumps, airline stocks fall the next day". The system turns it into a test, runs it on years of data, and keeps only the ideas that survive several checks in a row.</p>
</section>
<section>
  <h2>The steps</h2>
  <ol class="steps">
    <li><b>Write the idea.</b> From people on the team, from AI that reads the news, or from older idea lists.</li>
    <li><b>First screen.</b> A fast test across thousands of companies. Cheap, so most ideas get one.</li>
    <li><b>Second look.</b> The same test on companies the first screen never saw.</li>
    <li><b>Battery check.</b> Harder tests: does it survive earnings dates, simple explanations and shuffled controls?</li>
    <li><b>Confirm on new data.</b> Run once on years the idea was never tuned on (2021). Only then is it a confirmed finding.</li>
    <li><b>Follow it live.</b> Log a prediction each time the event happens, and score it later.</li>
  </ol>
</section>
<section>
  <h2>How many make it</h2>
  {bars([("Ideas written", n_ideas, ""), ("Got a test result", n_tested, ""), ("Passed a test", n_passed, ""), ("Became a finding", n_findings, ""), ("Confirmed", n_confirmed, "")], n)}
</section>
<section>
  <h2>Real, or luck?</h2>
  <p>Alongside every real test, the system runs luck-only tests on fake or shuffled data. Those can only pass by chance, so they show how often noise gets through. So far it has run <b>{n(n_luck_tests)}</b> of them.</p>
  <div class="legend"><span><i></i>Real ideas</span><span class="luck"><i></i>Luck only</span></div>
  {noise_chart()}
  <p>At the first screen, real ideas pass <b>{p(screen[1])}</b> of the time and luck-only tests <b>{p(screen[2])}</b>: no real difference.{f" The battery check only sees ideas that already passed a screen, and there real ideas pass <b>{p(battery[1])}</b> against <b>{p(battery[2])}</b> for luck. That's where real signals start to show." if battery else ""}</p>
</section>''')

pages["findings.html"] = ("Findings", "What has held up so far, what's still being checked, and what didn't work.", f'''
<section class="quote">
  <h1>Findings</h1>
  <div class="q-value">{n_confirmed} confirmed</div>
  <div class="q-change flat">of {n_findings} findings · {n_leads} being checked · {n_noeffect} ruled out</div>
</section>
<section>
  <h2>Confirmed</h2>
  <article class="finding">
    <div class="f-head"><span class="w-id">F01</span><h3>A stock's recent path predicts how much it will move next week</h3><span class="pill up">Confirmed</span></div>
    <p>How long it has been since a stock's 60-day high and low, how far it sits below the high, and how much of its recent move came on a handful of big days together forecast how much it will move over the next one to two weeks. Its own past volatility doesn't capture this. It predicts the size of the move, not the direction.</p>
    <p>It held on 2021 data it was never tuned on, and it isn't explained by earnings dates or by simply being down from the high. It also shows up in Europe, more weakly.</p>
    <p class="small">Possible use: sizing positions or pricing options. Nothing has been built to trade it yet.</p>
  </article>
  <article class="finding">
    <div class="f-head"><span class="w-id">F03</span><h3>Recent extreme days point the same way, but it's the same signal</h3><span class="pill up">Confirmed</span></div>
    <p>The number and timing of a stock's recent extreme days also forecast next week's volatility, and that held on 2021 data. Once F01 is accounted for, though, it adds nothing new, so it's treated as part of F01.</p>
  </article>
</section>
<section>
  <h2>Still being checked</h2>
  <div class="list">{"".join(wrow(i, t, "flat", "#", note) for i, t, note in LEADS)}</div>
  <p class="small">The other {n_leads - 3} are narrower checks of the method itself.</p>
</section>
<section>
  <h2>What didn't work</h2>
  <p>Ruling ideas out is most of the work. Each of these was tested properly and found to be luck, so it won't be tested again in the same form.</p>
  <div class="list">{"".join(wrow(i, t, "down", "#") for i, t in didnt)}</div>
  <p class="small">Plus {n_noeffect - len(didnt)} more narrow ones, and {n_lessons} lessons about the method itself.</p>
</section>''')

pages["ideas.html"] = ("Ideas", "Where the ideas come from and what happens to them.", f'''
{chart("c-ideas", "Ideas written", idea_t, idea_v, "int", "day", "All time")}
{stats([(s, n(c)) for s, c, _ in st_items[:4]])}
<section>
  <h2>Where they come from</h2>
  {bars([(g, c, f"{n(t)} tested" if t else "") for g, c, t in sources], n)}
  <p>People on the team have typed in <b>{people}</b> ideas, and {"none has" if people_tested == 0 else f"{people_tested} have"} been tested yet. Making their requests go first is on the <a href="next.html">list of what's next</a>.</p>
</section>
<section>
  <h2>Where they are now</h2>
  {bars(st_items, n)}
  <p>"Needs data" means the idea needs a kind of data the project doesn't collect yet.</p>
</section>''')

pages["machine.html"] = ("The machine", "How the research runs overnight, and how busy the computer is.", f'''
{chart("c-gpu", "Graphics card busy", [ep(d) for d, _ in nights], [g for _, g in nights], "pct", "day", f"Since {day(first_night)}",
       sub=f"Average share of each night the graphics card was busy. It averaged {p(early_gpu, 0)} up to Nov 19 and about 40% since.")}
{stats([("Jobs finished", n(n_jobs)), ("Test results", n(n_results)), ("Computers", str(n_machines)), ("Decides what's next", "Every 10 min")])}
<section>
  <h2>About</h2>
  <p>The tests run overnight on one home PC, mostly on its graphics card, which can run thousands of small tests at once. Every 10 minutes the system decides what to run next.</p>
</section>
{chart("c-jobs", "Jobs finished per day", [ep(d) for d, _ in jobs_day], [c for _, c in jobs_day], "int", "day", f"Since {day(jobs_day[0][0])}", big=False, sub="New York days with at least 20 jobs.")}''')

pages["next.html"] = ("What's next", "What isn't done yet and what comes next.", '''
<section class="quote">
  <h1>What's next</h1>
  <div class="q-value">4 in progress</div>
  <div class="q-change flat">The research side works. Next is making a confirmed signal usable.</div>
</section>
<section>
  <h2>Not done yet</h2>
  <ul class="plain">
    <li>No finding is traded, and there's no code to trade one.</li>
    <li>Whether the confirmed finding still makes money after trading costs hasn't been checked yet.</li>
    <li>Only one computer does the work. The team's other computers aren't connected yet.</li>
    <li>Requests typed in by people don't jump the queue yet.</li>
  </ul>
</section>
<section>
  <h2>Being built</h2>
  <ol class="steps">
    <li><b>People first.</b> A request from someone on the team ("test this tonight") goes to the front of the queue. If no test fits it, the system writes a short report saying why.</li>
    <li><b>Backtesting.</b> A step that trades confirmed findings on past data, costs included.</li>
    <li><b>Re-checking every day.</b> Past findings get re-run on new data each day, so one that stops working is caught.</li>
    <li><b>More machines.</b> The team's other computers join, each reporting how busy it is.</li>
  </ol>
</section>''')

LIGHT = "--bg:#ffffff; --ink:#000000; --muted:#6b7177; --rule:#e3e6e9; --hover:#f5f7f8; --track:#eceff1; --green:#00c805; --green-ink:#007a02; --red:#ff5000; --red-ink:#c43b00; --luck:#b9bfc4; color-scheme:light;"
DARK = "--bg:#000000; --ink:#ffffff; --muted:#9aa1a7; --rule:#23272b; --hover:#0f1213; --track:#1e2226; --green:#00c805; --green-ink:#00c805; --red:#ff5000; --red-ink:#ff5000; --luck:#4a5056; color-scheme:dark;"
css = f'''
:root {{ {LIGHT} --accent:var(--green); --accent-ink:var(--green-ink); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {DARK} }} }}
:root[data-theme="dark"] {{ {DARK} }}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", Roboto, Arial, sans-serif; -webkit-font-smoothing: antialiased; }}
a {{ color: var(--accent-ink); }}
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }}
b, .q-value, dd, .bar-value, .pill {{ font-variant-numeric: tabular-nums; }}

header.site {{ position: sticky; top: 0; z-index: 10; background: var(--bg); border-bottom: 1px solid var(--rule); }}
.site-inner {{ max-width: 1120px; margin: 0 auto; padding: 0 24px; }}
header .site-inner {{ display: flex; align-items: center; justify-content: space-between; gap: 24px; height: 64px; }}
.brand {{ display: flex; align-items: center; gap: 8px; color: var(--ink); text-decoration: none; font-weight: 700; font-size: 1.05rem; letter-spacing: -0.01em; white-space: nowrap; }}
.brand svg {{ width: 22px; height: 22px; color: var(--green); }}
.tabs {{ overflow-x: auto; scrollbar-width: none; }}
.tabs::-webkit-scrollbar {{ display: none; }}
.tabs-inner {{ display: flex; gap: 28px; }}
.tabs a {{ flex: none; padding: 21px 0 19px; color: var(--ink); text-decoration: none; font-size: 0.9rem; font-weight: 600; white-space: nowrap; border-bottom: 2px solid transparent; }}
.tabs a:hover {{ color: var(--green-ink); }}
.tabs a[aria-current="page"] {{ color: var(--green-ink); border-bottom-color: var(--green); }}

.wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 24px; display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 64px; align-items: start; }}
main {{ padding: 40px 0 64px; min-width: 0; }}
h1 {{ font-weight: 500; font-size: 2.25rem; line-height: 1.15; letter-spacing: -0.02em; margin: 0; }}
h2 {{ font-weight: 500; font-size: 1.45rem; line-height: 1.25; letter-spacing: -0.01em; margin: 0 0 4px; padding-bottom: 16px; border-bottom: 1px solid var(--rule); }}
h3 {{ font-size: 1rem; font-weight: 600; line-height: 1.35; margin: 0; }}
p {{ margin: 16px 0 0; text-wrap: pretty; }}
section {{ margin-top: 48px; }}
main > section:first-child {{ margin-top: 0; }}
.small {{ color: var(--muted); font-size: 0.85rem; }}

.up {{ --accent: var(--green); --accent-ink: var(--green-ink); }}
.down {{ --accent: var(--red); --accent-ink: var(--red-ink); }}
.q-value {{ font-size: 2.25rem; font-weight: 400; line-height: 1.2; letter-spacing: -0.02em; margin-top: 2px; }}
.q-value .vs {{ font-size: 1.1rem; color: var(--muted); letter-spacing: 0; margin: 0 4px; }}
.q-change {{ font-size: 0.9rem; font-weight: 600; color: var(--accent-ink); margin-top: 4px; }}
.q-change.flat {{ color: var(--muted); font-weight: 500; }}
.q-period {{ color: var(--muted); font-weight: 500; }}
.q-sub {{ color: var(--muted); font-size: 0.85rem; margin-top: 8px; max-width: 46em; }}
.chart-title {{ border: 0; padding: 0; font-size: 1.2rem; }}
.chart-block:not(.big) .q-value {{ font-size: 1.6rem; }}
.chart {{ position: relative; height: 240px; margin: 28px 0 0; touch-action: pan-y; cursor: crosshair; }}
.chart svg {{ display: block; width: 100%; height: 100%; overflow: visible; }}
.chart .line {{ fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }}
.chart .base {{ stroke: var(--muted); stroke-width: 1.5; stroke-dasharray: 1 5; stroke-linecap: round; vector-effect: non-scaling-stroke; opacity: .7; }}
.dot {{ position: absolute; width: 12px; height: 12px; border-radius: 50%; background: var(--accent); border: 2px solid var(--bg); transform: translate(-50%, -50%); pointer-events: none; }}
.hair {{ position: absolute; top: 0; bottom: 0; width: 1px; background: var(--rule); pointer-events: none; }}
.tip {{ position: absolute; top: -22px; transform: translateX(-50%); font-size: 0.75rem; color: var(--muted); white-space: nowrap; pointer-events: none; }}
.tip.right {{ transform: translateX(-100%); }}
.tip.left {{ transform: none; }}
.ranges {{ display: flex; gap: 22px; border-bottom: 1px solid var(--rule); padding: 0 0 0 2px; margin-top: 12px; min-height: 1px; }}
.ranges button {{ appearance: none; background: none; border: 0; border-bottom: 2px solid transparent; padding: 10px 0 12px; font: inherit; font-size: 0.8rem; font-weight: 700; color: var(--ink); cursor: pointer; letter-spacing: .02em; }}
.ranges button:hover {{ color: var(--accent-ink); }}
.ranges button[aria-pressed="true"] {{ color: var(--accent-ink); border-bottom-color: var(--accent); }}
.quote {{ padding-bottom: 24px; border-bottom: 1px solid var(--rule); }}

.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 24px 24px; margin: 32px 0 0; }}
.stats div {{ min-width: 0; }}
.stats dt {{ font-size: 0.85rem; font-weight: 600; }}
.stats dd {{ margin: 4px 0 0; color: var(--ink); font-size: 0.95rem; }}

.news {{ display: grid; }}
.item {{ display: grid; gap: 4px; padding: 20px 12px; margin: 0 -12px; border-bottom: 1px solid var(--rule); color: var(--ink); text-decoration: none; border-radius: 4px; }}
.item:hover {{ background: var(--hover); }}
.kicker {{ font-size: 0.78rem; font-weight: 700; }}
.headline {{ font-weight: 600; line-height: 1.35; }}
.text {{ color: var(--muted); font-size: 0.92rem; line-height: 1.5; }}

.steps {{ counter-reset: s; list-style: none; padding: 0; margin: 0; }}
.steps li {{ counter-increment: s; position: relative; padding: 16px 0 16px 48px; border-bottom: 1px solid var(--rule); }}
.steps li::before {{ content: counter(s); position: absolute; left: 0; top: 14px; width: 30px; height: 30px; border-radius: 50%; background: var(--track); font-size: 0.82rem; font-weight: 700; display: grid; place-items: center; }}
ul.plain {{ padding-left: 1.1em; margin: 16px 0 0; }}
ul.plain li {{ margin-bottom: 8px; }}

.bars {{ display: grid; gap: 14px; margin: 24px 0 0; }}
.bar {{ display: grid; grid-template-columns: minmax(130px, 34%) 1fr 4.5em; align-items: center; gap: 14px; font-size: 0.9rem; }}
.bar-label {{ font-weight: 600; line-height: 1.25; }}
.bar-track {{ height: 8px; background: var(--track); border-radius: 999px; overflow: hidden; }}
.bar-fill {{ display: block; height: 100%; background: var(--accent); border-radius: 999px; }}
.bar-value {{ text-align: right; font-weight: 600; }}
.bar-note {{ grid-column: 2 / 4; color: var(--muted); font-size: 0.78rem; margin-top: -10px; }}
.legend {{ display: flex; gap: 18px; font-size: 0.8rem; font-weight: 600; margin-top: 20px; }}
.legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--green); margin-right: 6px; vertical-align: -1px; }}
.legend .luck i {{ background: var(--luck); }}
.pairs {{ display: grid; gap: 20px; margin: 16px 0 0; }}
.pair {{ display: grid; grid-template-columns: minmax(110px, 26%) 1fr; gap: 14px; align-items: start; }}
.pair-label {{ font-weight: 600; font-size: 0.9rem; }}
.pair-bars {{ display: grid; gap: 8px; }}
.pair .bar {{ grid-template-columns: 1fr 4em minmax(10em, auto); }}
.pair .bar-note {{ grid-column: auto; margin: 0; }}
.bar.luck .bar-fill {{ background: var(--luck); }}

.pill {{ flex: none; display: inline-grid; place-items: center; min-width: 88px; height: 32px; padding: 0 10px; border-radius: 6px; font-size: 0.8rem; font-weight: 700; color: #000; }}
.pill.up {{ background: var(--green); }}
.pill.down {{ background: var(--red); }}
.pill.flat {{ background: var(--track); color: var(--ink); }}
.list {{ display: grid; }}
.wrow {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 12px; margin: 0 -12px; border-bottom: 1px solid var(--rule); color: var(--ink); text-decoration: none; border-radius: 4px; }}
a.wrow[href]:not([href="#"]):hover {{ background: var(--hover); }}
.list a.wrow[href="#"] {{ pointer-events: none; }}
.w-main {{ display: grid; gap: 2px; min-width: 0; }}
.w-id {{ font-size: 0.8rem; font-weight: 700; }}
.w-title {{ font-size: 0.92rem; line-height: 1.4; }}
.w-note {{ color: var(--muted); font-size: 0.8rem; }}
.finding {{ padding: 20px 0; border-bottom: 1px solid var(--rule); }}
.f-head {{ display: grid; grid-template-columns: auto 1fr auto; gap: 4px 12px; align-items: center; }}
.f-head .w-id {{ grid-column: 1 / 3; }}
.f-head h3 {{ grid-column: 1 / 3; grid-row: 2; }}
.f-head .pill {{ grid-column: 3; grid-row: 1 / 3; }}
.finding p {{ margin-top: 12px; }}

.side {{ position: sticky; top: 88px; padding: 40px 0 40px; }}
.card {{ border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; }}
.card-head {{ display: flex; justify-content: space-between; align-items: baseline; padding: 20px 24px 14px; border-bottom: 1px solid var(--rule); }}
.card-head h2 {{ border: 0; padding: 0; margin: 0; font-size: 1.05rem; font-weight: 600; }}
.card-head span {{ color: var(--muted); font-size: 0.8rem; }}
.card-list .wrow {{ margin: 0; padding: 12px 24px; border-radius: 0; }}
.card-list .wrow:last-child {{ border-bottom: 0; }}
.card-list + .card-head {{ border-top: 1px solid var(--rule); }}
.card-list .w-title {{ font-size: 0.82rem; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
.card-list .pill {{ min-width: 78px; height: 30px; font-size: 0.78rem; }}
.card-foot {{ margin: 0; padding: 14px 24px; border-top: 1px solid var(--rule); color: var(--muted); font-size: 0.78rem; }}

.next-link {{ margin-top: 56px; }}
.next-link a {{ display: inline-flex; align-items: center; gap: 6px; height: 44px; padding: 0 24px; border-radius: 999px; background: var(--green); color: #000; text-decoration: none; font-weight: 700; font-size: 0.9rem; }}
.next-link a:hover {{ filter: brightness(1.08); }}
.site-foot {{ border-top: 1px solid var(--rule); color: var(--muted); font-size: 0.8rem; }}
.site-foot .site-inner {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 24px; padding-top: 20px; padding-bottom: 28px; }}

@media (max-width: 1000px) {{
  .wrap {{ grid-template-columns: minmax(0, 1fr); gap: 0; }}
  .side {{ position: static; padding: 0 0 48px; }}
  main {{ padding-bottom: 40px; }}
}}
@media (max-width: 860px) {{
  header .site-inner {{ flex-direction: column; align-items: stretch; height: auto; gap: 0; padding-top: 14px; padding-right: 0; }}
  .tabs {{ -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent); mask-image: linear-gradient(to right, #000 calc(100% - 28px), transparent); }}
  .tabs-inner {{ gap: 22px; padding-right: 32px; }}
  .tabs a {{ padding: 12px 0 10px; }}
}}
@media (max-width: 640px) {{
  .site-inner, .wrap {{ padding-left: 16px; padding-right: 16px; }}
  header .site-inner {{ padding-right: 0; }}
  main {{ padding-top: 28px; }}
  h1 {{ font-size: 1.75rem; }}
  .q-value {{ font-size: 2rem; }}
  .chart {{ height: 200px; }}
  .stats {{ grid-template-columns: repeat(2, 1fr); gap: 18px 16px; }}
  .bar {{ grid-template-columns: 1fr 4em; gap: 8px 12px; }}
  .bar-label {{ grid-column: 1 / 3; margin-bottom: -4px; }}
  .bar-note {{ grid-column: 1 / 3; margin-top: -6px; }}
  .pair {{ grid-template-columns: 1fr; gap: 8px; }}
  .pair .bar {{ grid-template-columns: 1fr 3.6em; }}
  .pair .bar-note {{ grid-column: 1 / 3; margin-top: -4px; }}
  .pill {{ min-width: 76px; }}
  .card-head, .card-list .wrow, .card-foot {{ padding-left: 16px; padding-right: 16px; }}
}}
'''

for file, (title, desc, body) in pages.items():
    open(os.path.join(outdir, file), "w").write(layout(file, title, desc, body))
open(os.path.join(outdir, "style.css"), "w").write(css.lstrip())
open(os.path.join(outdir, ".nojekyll"), "w").write("")
print("pages:", list(pages), "| points:", len(cum_t), len(idea_t), len(nights), len(jobs_day), "| week:", week_ago)
