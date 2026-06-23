#!/usr/bin/env python3
"""Rebuild the slatedb download trend from accumulated data.

Reads the captured CSV + daily snapshots, then writes:
  * data/daily_combined.csv  - permanent daily series (deduped across snapshots)
  * docs/index.html          - self-contained dashboard (GitHub Pages)
  * README.md                - regenerated stats block between the markers

The "is it exponential?" test: bucket the daily series into consecutive 90-day
windows, fit ln(volume) ~ a + b*t, and report R^2. Exponential growth is a
straight line on a log axis, so R^2 = 1 is the ideal target; the closer the
fit gets to 1, the cleaner the exponential trend.

Pure stdlib.
"""
import json, csv, math, datetime
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snapshots"
DOCS = ROOT / "docs"


def load_daily():
    """Merge all snapshots into one date -> downloads map (latest snapshot wins)."""
    daily = {}
    for snap in sorted((SNAPS / "downloads").glob("*.json")):  # filename = capture date, chronological
        d = json.loads(snap.read_text())
        by_date = defaultdict(int)
        for r in d.get("version_downloads", []):
            by_date[r["date"]] += r["downloads"]
        for r in d.get("meta", {}).get("extra_downloads", []):
            by_date[r["date"]] += r["downloads"]
        daily.update(by_date)  # later snapshot is authoritative for overlapping dates
    return dict(sorted(daily.items()))


def calendar_months(daily):
    """(month, total, ndays) per calendar month + which months are partial."""
    by_month = defaultdict(lambda: [0, 0])
    for date, dl in daily.items():
        m = date[:7]
        by_month[m][0] += dl
        by_month[m][1] += 1
    out = []
    months = sorted(by_month)
    for i, m in enumerate(months):
        total, ndays = by_month[m]
        y, mo = int(m[:4]), int(m[5:7])
        days_in_month = (datetime.date(y + (mo == 12), (mo % 12) + 1, 1) - datetime.date(y, mo, 1)).days
        partial = ndays < days_in_month
        out.append({"month": m, "total": total, "ndays": ndays, "partial": partial})
    return out


def windows90(daily):
    """Consecutive 90-day windows from the earliest captured day."""
    if not daily:
        return []
    dates = list(daily)
    d0 = datetime.date.fromisoformat(dates[0])
    dN = datetime.date.fromisoformat(dates[-1])
    wins = []
    start = d0
    while start + datetime.timedelta(days=89) <= dN:
        end = start + datetime.timedelta(days=89)
        total = sum(dl for ds, dl in daily.items()
                    if start <= datetime.date.fromisoformat(ds) <= end)
        wins.append({"start": start.isoformat(), "end": end.isoformat(), "total": total})
        start = end + datetime.timedelta(days=1)
    return wins


def exp_fit(wins):
    """Log-linear fit over window totals -> R^2 and implied per-window growth."""
    ys = [math.log(w["total"]) for w in wins if w["total"] > 0]
    if len(ys) < 3:
        return None
    xs = list(range(len(ys)))
    xb = sum(xs) / len(xs); yb = sum(ys) / len(ys)
    b = sum((x - xb) * (y - yb) for x, y in zip(xs, ys)) / sum((x - xb) ** 2 for x in xs)
    a = yb - b * xb
    yhat = [a + b * x for x in xs]
    ss_res = sum((y - yh) ** 2 for y, yh in zip(ys, yhat))
    ss_tot = sum((y - yb) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return {"a": a, "b": b, "r2": r2, "growth90": math.exp(b) - 1, "n": len(ys)}


def load_cumulative():
    p = DATA / "slatedb_monthly.csv"
    if not p.exists():
        return []
    rows = list(csv.DictReader(open(p)))
    return [{"date": r["date"], "total": int(r["cumulative_total"])} for r in rows]


def main():
    DOCS.mkdir(exist_ok=True)
    daily = load_daily()

    # permanent daily series
    with open(DATA / "daily_combined.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "downloads"])
        for d, v in daily.items():
            w.writerow([d, v])

    months = calendar_months(daily)
    wins = windows90(daily)
    fit = exp_fit(wins)
    cumulative = load_cumulative()
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    full_months = [m for m in months if not m["partial"]]
    latest_total = cumulative[-1]["total"] if cumulative else (sum(daily.values()) if daily else 0)

    payload = {
        "generated": generated,
        "daily": [[d, v] for d, v in daily.items()],
        "months": months,
        "windows": wins,
        "fit": fit,
        "cumulative": cumulative,
        "latest_total": latest_total,
    }

    DOCS.joinpath("data.json").write_text(json.dumps(payload, separators=(",", ":")))
    DOCS.joinpath("index.html").write_text(render_html(payload))
    update_readme(payload, len(full_months), len(wins))
    print(f"rendered: {len(daily)} daily pts, {len(months)} months "
          f"({len(full_months)} full), {len(wins)} 90d windows, "
          f"fit={'R2=%.3f' % fit['r2'] if fit else 'pending'}")


def render_html(p):
    fit = p["fit"]
    if fit:
        verdict = (f"<b>{fit['n']} full 90-day windows so far.</b> Log-linear fit: "
                   f"R&sup2; = {fit['r2']:.3f}, implied growth "
                   f"{fit['growth90']*100:+.1f}%/window. "
                   f"<b>R&sup2; = 1.000 is the exponential target</b> &mdash; the closer "
                   f"this gets to 1, the cleaner the exponential trend.")
    else:
        need = max(0, 6 - len(p["windows"]))
        verdict = (f"<b>Accumulating.</b> Have {len(p['windows'])} full 90-day window(s); "
                   f"need ~{need} more before an exponential fit is meaningful. "
                   f"The target is <b>R&sup2; = 1.000</b>: on a log axis a pure "
                   f"exponential is a perfectly straight line.")
    return HTML.replace("/*DATA*/", json.dumps(p)).replace("<!--VERDICT-->", verdict)


def update_readme(p, nfull, nwin):
    readme = ROOT / "README.md"
    text = readme.read_text() if readme.exists() else README_BASE
    fit = p["fit"]
    fitline = (f"- 90-day-window exponential fit: **R² {fit['r2']:.3f}**, "
               f"**{fit['growth90']*100:+.1f}%/window** ({fit['n']} windows)"
               if fit else
               f"- 90-day-window exponential fit: _pending_ ({nwin} of ~6 windows captured)")
    block = (
        f"<!--STATS-->\n"
        f"_Updated {p['generated']}_\n\n"
        f"- All-time downloads: **{p['latest_total']:,}**\n"
        f"- Daily history reconstructed: **{len(p['daily'])} days**\n"
        f"- Full calendar months: **{nfull}**\n"
        f"{fitline}\n\n"
        f"See the live dashboard: **https://responsivedev.github.io/slatedb-stats/**\n"
        f"<!--/STATS-->"
    )
    import re
    if "<!--STATS-->" in text:
        text = re.sub(r"<!--STATS-->.*?<!--/STATS-->", block, text, flags=re.S)
    else:
        text = text.rstrip() + "\n\n## Current numbers\n\n" + block + "\n"
    readme.write_text(text)


README_BASE = """# slatedb-stats

Automated crates.io download tracking and trend analysis for
[slatedb](https://crates.io/crates/slatedb).

A GitHub Action runs monthly: it captures the cumulative download total and the
last-90-days daily series from the crates.io API, archives them under `data/`,
and regenerates the dashboard in `docs/`. crates.io only keeps daily data for
~90 days, so this repo is what preserves the long-run history.

## Why

To tell whether slatedb adoption is on an exponential trajectory, you need a
multi-year monthly series - a single quarter can't distinguish flat from
exponential, because month-to-month noise and seasonal dips swamp the trend
over short spans. This repo builds that series. Exponential growth is a
straight line on a log axis, so the test is simple: fit ln(volume) over
consecutive 90-day windows and see how close R² gets to 1.

## Layout

`data/snapshots/` holds the raw, verbatim crates.io API responses - everything
else under `data/` is derived from them and can be regenerated by re-running
`scripts/render.py`:

- `data/snapshots/downloads/<date>.json` - raw `/downloads` responses (daily counts, last 90d)
- `data/snapshots/crate/<date>.json` - raw crate-metadata responses (cumulative totals, versions)

Derived (rebuildable from the snapshots above):

- `data/slatedb_monthly.csv` - cumulative total per capture (the robust series)
- `data/slatedb_versions_monthly.csv` - per-version cumulative totals
- `data/daily_combined.csv` - permanent daily series, deduped across snapshots
- `docs/index.html` - regenerated dashboard (GitHub Pages)

`scripts/capture.py` / `scripts/render.py` are the two job steps.
Run locally: `python3 scripts/capture.py && python3 scripts/render.py`
"""

# --- self-contained dashboard template (inline SVG, no external deps) ---
HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>slatedb downloads</title>
<style>
:root{--ink:#1a1a1a;--mut:#6b6b6b;--line:#d8d8d8;--accent:#2f6f4f;--fit:#b04a2f;}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;font-weight:650;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.verdict{border:1px solid #cfe3d6;background:#f1f8f3;border-radius:10px;padding:14px 16px;margin:0 0 26px;font-size:14px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0 28px}
.card{flex:1;min-width:120px;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .n{font-size:20px;font-weight:650;letter-spacing:-.02em}
.card .l{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-top:3px}
h2{font-size:14px;font-weight:600;margin:32px 0 4px}.note{font-size:12px;color:var(--mut);margin:0 0 14px}
.box{border:1px solid var(--line);border-radius:10px;padding:16px 14px 10px;overflow-x:auto}
svg{display:block;max-width:100%}.tk{fill:var(--mut);font-size:10px}.gl{stroke:#eee}.axis{stroke:var(--line)}
.foot{color:var(--mut);font-size:11px;margin-top:28px}
.empty{color:var(--mut);font-size:13px;padding:24px 8px;text-align:center}
</style></head><body><div class="wrap">
<h1>slatedb &mdash; crates.io downloads</h1>
<div class="sub" id="sub"></div>
<div class="verdict"><!--VERDICT--></div>
<div class="cards" id="cards"></div>
<h2>Monthly download volume</h2>
<p class="note">Downloads per calendar month from the reconstructed daily series. Hollow bars are partial months (incomplete data at the edges of the 90-day window).</p>
<div class="box"><svg id="months" width="920" height="280"></svg><div id="months-empty"></div></div>
<h2>Daily downloads (reconstructed history)</h2>
<p class="note">All snapshots merged and deduped. Weekly sawtooth = weekday vs weekend (CI traffic).</p>
<div class="box"><svg id="daily" width="920" height="240"></svg><div id="daily-empty"></div></div>
<h2>90-day window exponential test</h2>
<p class="note">Consecutive 90-day windows on a log axis. A pure exponential is a perfectly straight line (R&sup2;&nbsp;=&nbsp;1). The <span style="color:var(--fit);font-weight:600">red dashed</span> line is the best-fit exponential through slatedb's own windows; how tightly the points hug it is the R&sup2; reported above.</p>
<div class="box"><svg id="windows" width="920" height="260"></svg><div id="windows-empty"></div></div>
<div class="foot" id="foot"></div>
</div>
<script>
const P=/*DATA*/;
const NS="http://www.w3.org/2000/svg";
const el=(n,a)=>{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);return e;};
const fmt=n=>Math.round(n).toLocaleString();
document.getElementById('sub').textContent=`generated ${P.generated} · ${P.daily.length} daily points · ${P.months.length} months`;
const cards=document.getElementById('cards');
const fullMonths=P.months.filter(m=>!m.partial);
const addCard=(n,l)=>{const c=document.createElement('div');c.className='card';c.innerHTML=`<div class="n">${n}</div><div class="l">${l}</div>`;cards.appendChild(c);};
addCard(P.latest_total.toLocaleString(),'all-time downloads');
addCard(P.daily.length,'days reconstructed');
addCard(fullMonths.length,'full months');
addCard(P.fit?`R² ${P.fit.r2.toFixed(3)}`:'—','exp fit (90d)');

// monthly bars
(function(){const svg=document.getElementById('months'),W=920,H=280,m={t:12,r:14,b:54,l:54};
const M=P.months;if(!M.length){document.getElementById('months-empty').className='empty';document.getElementById('months-empty').textContent='No data yet.';svg.remove();return;}
const iw=W-m.l-m.r,ih=H-m.t-m.b,max=Math.max(...M.map(x=>x.total)),maxR=Math.max(1,Math.ceil(max/10000)*10000);
const bw=iw/M.length,bar=bw*0.66;
for(let g=0;g<=maxR;g+=Math.max(10000,Math.round(maxR/5/10000)*10000)){const yy=m.t+ih-ih*g/maxR;
svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=(g/1000)+'k';}
M.forEach((d,i)=>{const h=ih*d.total/maxR,x=m.l+i*bw+(bw-bar)/2,yy=m.t+ih-h;
svg.appendChild(el('rect',{x,y:yy,width:bar,height:h,rx:2,fill:d.partial?'#fff':'#3a3a3a',stroke:'#3a3a3a','stroke-width':d.partial?1:0,'stroke-dasharray':d.partial?'3 2':''}));
svg.appendChild(el('text',{class:'tk',x:x+bar/2,y:H-40,'text-anchor':'end',transform:`rotate(-50 ${x+bar/2} ${H-40})`})).textContent=d.month;});
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));})();

// daily line
(function(){const svg=document.getElementById('daily'),W=920,H=240,m={t:12,r:14,b:28,l:48};
const D=P.daily;if(D.length<2){document.getElementById('daily-empty').className='empty';document.getElementById('daily-empty').textContent='Need more days.';svg.remove();return;}
const iw=W-m.l-m.r,ih=H-m.t-m.b,max=Math.max(...D.map(x=>x[1])),maxR=Math.max(1,Math.ceil(max/1000)*1000);
const x=i=>m.l+iw*i/(D.length-1),y=v=>m.t+ih-ih*v/maxR;
for(let g=0;g<=maxR;g+=Math.max(1000,Math.round(maxR/5/1000)*1000)){const yy=y(g);
svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=(g/1000)+'k';}
let pa=`M ${x(0)} ${y(D[0][1])}`,ar=`M ${x(0)} ${m.t+ih} L ${x(0)} ${y(D[0][1])}`;
D.forEach((d,i)=>{if(i){pa+=` L ${x(i)} ${y(d[1])}`;ar+=` L ${x(i)} ${y(d[1])}`;}});
ar+=` L ${x(D.length-1)} ${m.t+ih} Z`;
svg.appendChild(el('path',{d:ar,fill:'#2f6f4f',opacity:.10}));
svg.appendChild(el('path',{d:pa,fill:'none',stroke:'#2f6f4f','stroke-width':1.5}));
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));
[0,Math.floor(D.length/2),D.length-1].forEach(i=>svg.appendChild(el('text',{class:'tk',x:x(i),y:H-10,'text-anchor':'middle'})).textContent=D[i][0]);})();

// 90d windows log scale
(function(){const svg=document.getElementById('windows'),W=920,H=260,m={t:12,r:14,b:42,l:54};
const Wd=P.windows;if(Wd.length<2){document.getElementById('windows-empty').className='empty';
document.getElementById('windows-empty').textContent=`${Wd.length} window(s) so far — the exponential test needs ~6. Target: R² = 1.000 (a perfectly straight line on this log axis).`;svg.remove();return;}
const iw=W-m.l-m.r,ih=H-m.t-m.b;const vals=Wd.map(w=>w.total);
const lo=Math.log10(Math.max(1,Math.min(...vals)*0.8)),hi=Math.log10(Math.max(...vals)*1.25);
const x=i=>m.l+iw*i/(Wd.length-1),y=v=>m.t+ih-ih*(Math.log10(v)-lo)/(hi-lo);
const ticks=[];for(let e=Math.floor(lo);e<=Math.ceil(hi);e++){[1,2,5].forEach(mul=>{const v=mul*10**e;if(Math.log10(v)>=lo&&Math.log10(v)<=hi)ticks.push(v);});}
ticks.forEach(v=>{const yy=y(v);svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=v>=1e6?(v/1e6+'M'):v>=1e3?(v/1e3+'k'):v;});
// best-fit exponential through slatedb's own windows (fit is in natural log: ln(y)=a+b*t)
if(P.fit){let kp=`M ${x(0)} ${y(Math.exp(P.fit.a))}`;
for(let i=1;i<Wd.length;i++)kp+=` L ${x(i)} ${y(Math.exp(P.fit.a+P.fit.b*i))}`;
svg.appendChild(el('path',{d:kp,fill:'none',stroke:'#b04a2f','stroke-width':1.3,'stroke-dasharray':'5 4'}));}
let lp=`M ${x(0)} ${y(vals[0])}`;Wd.forEach((w,i)=>{if(i)lp+=` L ${x(i)} ${y(w.total)}`;});
svg.appendChild(el('path',{d:lp,fill:'none',stroke:'#2f6f4f','stroke-width':1.4}));
Wd.forEach((w,i)=>{svg.appendChild(el('circle',{cx:x(i),cy:y(w.total),r:3.2,fill:'#2f6f4f'}));
svg.appendChild(el('text',{class:'tk',x:x(i),y:H-26,'text-anchor':'middle'})).textContent=w.start.slice(2,7);});
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));})();

document.getElementById('foot').textContent='Source: crates.io API. Daily data older than ~90 days exists only because it was snapshotted here. Exponential target: R² = 1 (a straight line on a log axis).';
</script></body></html>"""


if __name__ == "__main__":
    main()
