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
PINNED_TRAFFIC_VERSION = "0.10.1"


def reconcile_counts(counts, target):
    """Scale per-version counts to an authoritative daily total."""
    total_counts = sum(counts.values())
    if target is None or total_counts == target or total_counts == 0:
        return dict(counts)
    scaled = []
    for num, count in counts.items():
        raw = count * target / total_counts
        whole = math.floor(raw)
        scaled.append([raw - whole, num, whole])
    remainder = target - sum(x[2] for x in scaled)
    for _, num, _ in sorted(scaled, reverse=True)[:remainder]:
        for item in scaled:
            if item[1] == num:
                item[2] += 1
                break
    return {num: count for _, num, count in scaled if count}


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


def load_versions():
    """All-time downloads per version, from the latest raw crate snapshot.

    Falls back to the per-version CSV's most recent date if no crate snapshot
    exists. Sorted most-downloaded first.
    """
    crate_dir = SNAPS / "crate"
    files = sorted(crate_dir.glob("*.json")) if crate_dir.exists() else []
    if files:
        d = json.loads(files[-1].read_text())
        vs = [(v["num"], v["downloads"]) for v in d.get("versions", [])]
    else:
        p = DATA / "slatedb_versions_monthly.csv"
        if not p.exists():
            return []
        rows = list(csv.DictReader(open(p)))
        if not rows:
            return []
        latest = max(r["date"] for r in rows)
        vs = [(r["version"], int(r["cumulative_downloads"]))
              for r in rows if r["date"] == latest]
    vs.sort(key=lambda x: x[1], reverse=True)
    return [{"num": n, "downloads": dl} for n, dl in vs]


def reconstruct_cumulative(exclude_nums=None):
    """Estimated cumulative-download trajectory from a single crate snapshot.

    A version can only be downloaded after it ships, so at any past date t the
    cumulative total was AT MOST:  total - (all-time downloads of every version
    released on/after t). We sample that ceiling at each release date, ending at
    the snapshot date itself (where nothing is released later, so cap == total).

    It's an upper bound, not exact: versions released before t keep accruing
    downloads after t (CI caches, lockfile pins, transitive deps), and this
    construction credits all of those to before t. True history runs at or below
    the returned curve. The exact curve only emerges from repeated cumulative
    snapshots over time.
    Archived /downloads snapshots tighten that ceiling. If a download is known
    to have happened after t, it also cannot be part of cumulative(t).
    Per-version download snapshots can be attributed exactly; crates.io's
    aggregate extra_downloads bucket cannot, so we subtract only the minimum
    amount that must belong to versions released before t. Overlapping snapshots
    are merged date-by-date with the newest snapshot authoritative for each date.
    """
    exclude_nums = set(exclude_nums or [])
    crate_dir = SNAPS / "crate"
    files = sorted(crate_dir.glob("*.json")) if crate_dir.exists() else []
    if not files:
        return [], {}
    snap = files[-1]
    d = json.loads(snap.read_text())
    vs = [
        {
            "date": v["created_at"][:10],
            "num": v["num"],
            "downloads": v["downloads"],
            "id": v.get("id"),
        }
        for v in d.get("versions", [])
        if v.get("created_at")
    ]
    if not vs:
        return [], {}
    excluded = [v for v in vs if v["num"] in exclude_nums]
    excluded_lifetime = sum(v["downloads"] for v in excluded)
    total = d["crate"]["downloads"] - excluded_lifetime
    by_id = {v["id"]: v for v in vs if v["id"] is not None}
    daily_evidence = {}

    for dl_path in sorted((SNAPS / "downloads").glob("*.json")):
        dd = json.loads(dl_path.read_text())
        by_date = defaultdict(lambda: {"explicit": defaultdict(int), "extra": 0})
        for r in dd.get("version_downloads", []):
            v = by_id.get(r.get("version"))
            if v:
                by_date[r["date"]]["explicit"][v["num"]] += r["downloads"]
        for r in dd.get("meta", {}).get("extra_downloads", []):
            by_date[r["date"]]["extra"] += r["downloads"]
        # Later snapshots win for overlapping dates, matching load_daily().
        for date, day in by_date.items():
            daily_evidence[date] = {
                "snapshot": dl_path.stem,
                "source": "crate",
                "explicit": dict(day["explicit"]),
                "extra": day["extra"],
                "total": sum(day["explicit"].values()) + day["extra"],
            }

    for vdl_path in sorted((SNAPS / "version_downloads").glob("*.json")):
        vd = json.loads(vdl_path.read_text())
        by_date = defaultdict(lambda: defaultdict(int))
        for num, raw in vd.get("versions", {}).items():
            if num not in {v["num"] for v in vs}:
                continue
            for r in raw.get("version_downloads", []):
                by_date[r["date"]][num] += r["downloads"]
        # Exact per-version evidence supersedes crate-level extra_downloads for
        # overlapping dates, while preserving crate-level daily totals when they
        # are available from the same date.
        for date, explicit in by_date.items():
            target = daily_evidence.get(date, {}).get("total")
            explicit = reconcile_counts(explicit, target)
            daily_evidence[date] = {
                "snapshot": vdl_path.stem,
                "source": "version",
                "explicit": dict(explicit),
                "extra": 0,
                "total": sum(explicit.values()),
            }

    vs = [v for v in vs if v["num"] not in exclude_nums]
    included_nums = {v["num"] for v in vs}

    explicit_rows = [
        {"date": date, "num": num, "downloads": downloads}
        for date, day in daily_evidence.items()
        for num, downloads in day["explicit"].items()
        if num in included_nums
    ]
    extra_rows = [
        {
            "date": date,
            "downloads": 0 if exclude_nums else day["extra"],
            "explicit_nums": set(day["explicit"]),
        }
        for date, day in daily_evidence.items()
        if day["extra"] and not exclude_nums
    ]

    explicit_nums = {r["num"] for r in explicit_rows}
    release_dates = sorted({v["date"] for v in vs})
    daily_dates = [r["date"] for r in explicit_rows] + [r["date"] for r in extra_rows]
    meta = {
        "crate_snapshot": snap.stem,
        "downloads_snapshots": len(list((SNAPS / "downloads").glob("*.json"))),
        "version_download_snapshots": len(list((SNAPS / "version_downloads").glob("*.json"))),
        "daily_start": min(daily_dates) if daily_dates else None,
        "daily_end": max(daily_dates) if daily_dates else None,
        "daily_days": len(daily_evidence),
        "exact_version_days": sum(1 for day in daily_evidence.values()
                                  if day.get("source") == "version"),
        "daily_total": sum(r["downloads"] for r in explicit_rows) +
                       sum(r["downloads"] for r in extra_rows),
        "explicit_version_count": len(explicit_nums),
        "explicit_daily_total": sum(r["downloads"] for r in explicit_rows),
        "extra_daily_total": sum(r["downloads"] for r in extra_rows),
        "excluded_versions": sorted(exclude_nums),
        "excluded_lifetime_total": excluded_lifetime,
        "latest_total": total,
    }

    def cap_at(t):
        future = [v for v in vs if v["date"] >= t]
        release_only_after = sum(v["downloads"] for v in future)
        loose = total - release_only_after

        explicit_after = 0
        for v in vs:
            if v["date"] >= t or v["num"] not in explicit_nums:
                continue
            known_after = sum(r["downloads"] for r in explicit_rows
                              if r["num"] == v["num"] and r["date"] >= t)
            explicit_after += min(known_after, v["downloads"])

        extra_after_for_old_versions = 0
        for r in extra_rows:
            if r["date"] < t:
                continue
            future_extra_capacity = sum(
                v["downloads"] for v in future
                if v["num"] not in r["explicit_nums"]
            )
            extra_after_for_old_versions += max(
                0,
                r["downloads"] - future_extra_capacity,
            )

        tight = total - release_only_after - explicit_after - extra_after_for_old_versions
        return loose, min(loose, max(0, tight))

    by_date = defaultdict(list)  # release date -> [(version, all-time downloads), ...]
    for v in vs:
        by_date[v["date"]].append((v["num"], v["downloads"]))
    pts = []
    for t in release_dates:
        cap, cap_tight = cap_at(t)
        rel = by_date[t]
        pts.append({
            "date": t,
            "cap": cap,
            "cap_tight": cap_tight,
            "versions": [v for v, _ in rel],
            # this release's lifetime downloads = the climb to the next point
            "added": sum(dl for _, dl in rel),
        })
    # snapshot day anchors each curve to its true total
    pts.append({"date": snap.stem, "cap": total, "cap_tight": total,
                "versions": [], "added": 0})
    return pts, meta


def dominant_release():
    """The release with the largest share of recent (windowed) downloads, with
    the figures the caveat needs. Returns None unless an OLDER line (released
    before the daily window) dominates recent traffic - that's the only case the
    "pinned deps, not new adoption" framing applies to. Needs per-version daily
    snapshots; returns None until those exist.
    """
    cfiles = sorted((SNAPS / "crate").glob("*.json")) if (SNAPS / "crate").exists() else []
    vfiles = sorted((SNAPS / "version_downloads").glob("*.json")) if (SNAPS / "version_downloads").exists() else []
    if not cfiles or not vfiles:
        return None
    crate = json.loads(cfiles[-1].read_text())
    vd = json.loads(vfiles[-1].read_text())
    total_life = crate["crate"]["downloads"]
    life = {v["num"]: v["downloads"] for v in crate.get("versions", [])}
    created = {v["num"]: v["created_at"][:10] for v in crate.get("versions", [])
               if v.get("created_at")}
    window, dates = {}, []
    for num, raw in vd.get("versions", {}).items():
        rows = raw.get("version_downloads", [])
        window[num] = sum(r["downloads"] for r in rows)
        dates += [r["date"] for r in rows]
    total_window = sum(window.values())
    if not dates or total_window == 0 or total_life == 0:
        return None
    top = max(window, key=window.get)
    win_start = min(dates)
    # only a meaningful caveat when an older line still dominates recent pulls
    if created.get(top, "9999") >= win_start or window[top] / total_window < 0.30:
        return None
    major_minor = ".".join(top.split(".")[:2])
    return {
        "num": top,
        "line": f"{major_minor}.x",
        "lifetime": life.get(top, 0),
        "lifetime_share": life.get(top, 0) / total_life,
        "window_downloads": window[top],
        "window_share": window[top] / total_window,
        "window_start": win_start,
        "window_end": max(dates),
    }


def measured_cumulative(daily, total):
    """Exact cumulative total at each day we have daily data for.

    Unlike reconstruct_cumulative() (an upper bound), this is measured: walk
    backward from the known all-time total, subtracting downloads that occurred
    later. cumulative(d) = total - (downloads strictly after d). Defined only
    over the daily window (~last 90 days), where the gap below the upper-bound
    ceiling is the bias B(t) = downloads that older versions earned after d.
    """
    if not daily or not total:
        return []
    out, after = [], 0
    for d, v in reversed(sorted(daily.items())):
        out.append({"date": d, "cum": total - after})
        after += v
    out.reverse()
    return out


def daily_excluding_version(exclude_num, daily):
    """Exact recent daily series with one version removed.

    This only uses archived per-version daily snapshots. Aggregate-only
    crates.io days are skipped because their version mix is unknowable.
    """
    by_date = {}
    excluded_by_date = {}
    for vdl_path in sorted((SNAPS / "version_downloads").glob("*.json")):
        vd = json.loads(vdl_path.read_text())
        snap_dates = defaultdict(lambda: defaultdict(int))
        for num, raw in vd.get("versions", {}).items():
            for r in raw.get("version_downloads", []):
                snap_dates[r["date"]][num] += r["downloads"]
        for date, counts in snap_dates.items():
            counts = reconcile_counts(counts, daily.get(date))
            excluded_by_date[date] = counts.get(exclude_num, 0)
            by_date[date] = sum(v for num, v in counts.items() if num != exclude_num)
    series = dict(sorted(by_date.items()))
    meta = {
        "excluded_version": exclude_num,
        "daily_start": min(series) if series else None,
        "daily_end": max(series) if series else None,
        "daily_days": len(series),
        "excluded_daily_total": sum(excluded_by_date.values()),
    }
    return series, meta


def excluded_view(daily, latest_total):
    versions = load_versions()
    excluded = next((v for v in versions if v["num"] == PINNED_TRAFFIC_VERSION), None)
    if not excluded:
        return None
    filtered_daily, meta = daily_excluding_version(PINNED_TRAFFIC_VERSION, daily)
    filtered_total = latest_total - excluded["downloads"]
    reconstruction, reconstruction_meta = reconstruct_cumulative({PINNED_TRAFFIC_VERSION})
    meta.update({
        "excluded_lifetime": excluded["downloads"],
        "unobserved_excluded_lifetime": excluded["downloads"] - meta["excluded_daily_total"],
    })
    return {
        "num": PINNED_TRAFFIC_VERSION,
        "latest_total": filtered_total,
        "daily": [[d, v] for d, v in filtered_daily.items()],
        "measured": measured_cumulative(filtered_daily, filtered_total),
        "reconstruction": reconstruction,
        "reconstruction_meta": reconstruction_meta,
        "meta": meta,
    }


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

    reconstruction, reconstruction_meta = reconstruct_cumulative()

    payload = {
        "generated": generated,
        "daily": [[d, v] for d, v in daily.items()],
        "months": months,
        "windows": wins,
        "fit": fit,
        "versions": load_versions(),
        "reconstruction": reconstruction,
        "reconstruction_meta": reconstruction_meta,
        "measured": measured_cumulative(daily, latest_total),
        "excluded_view": excluded_view(daily, latest_total),
        "dominant_release": dominant_release(),
        "cumulative": cumulative,
        "latest_total": latest_total,
        "enough_windows": 6,   # show the exponential-test chart only past this
    }

    DOCS.joinpath("data.json").write_text(json.dumps(payload, separators=(",", ":")))
    DOCS.joinpath("index.html").write_text(render_html(payload))
    update_readme(payload, len(full_months), len(wins))
    print(f"rendered: {len(daily)} daily pts, {len(months)} months "
          f"({len(full_months)} full), {len(wins)} 90d windows, "
          f"{len(payload['versions'])} versions, "
          f"fit={'R2=%.3f' % fit['r2'] if fit else 'pending'}")


def render_html(p):
    return HTML.replace("/*DATA*/", json.dumps(p))


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

- `data/snapshots/downloads/<date>.json` - raw crate `/downloads` responses (daily counts, last 90d)
- `data/snapshots/crate/<date>.json` - raw crate-metadata responses (cumulative totals, versions)
- `data/snapshots/version_downloads/<date>.json` - raw per-version `/downloads` responses

Derived (rebuildable from the snapshots above):

- `data/slatedb_monthly.csv` - cumulative total per capture (the robust series)
- `data/slatedb_versions_monthly.csv` - per-version cumulative totals
- `data/daily_combined.csv` - permanent daily series, deduped across snapshots
- `docs/index.html` - regenerated dashboard (GitHub Pages)

For the historical cumulative estimate, `scripts/render.py` starts with each
version's release date and lifetime download total, then subtracts downloads
that archived daily snapshots prove happened later. Per-version daily snapshots
attribute old-version traffic directly; the crate-level daily endpoint remains
the authoritative source for total daily volume.

The dashboard also has a traffic filter for v0.10.1, which appears to be
dominated by pinned old-version traffic. In that filtered view, cumulative
estimates remove the full v0.10.1 lifetime total. Daily and download-rate charts
remove v0.10.1 only on days with exact per-version attribution.

`scripts/capture.py` / `scripts/render.py` are the two job steps.
Run locally: `python3 scripts/capture.py && python3 scripts/render.py`
"""

# --- self-contained dashboard template (inline SVG, no external deps) ---
HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>slatedb downloads</title>
<style>
:root{--ink:#1a1a1a;--mut:#6b6b6b;--line:#d8d8d8;--accent:#2f6f4f;--accent2:#3b6fb0;--fit:#b04a2f;}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;font-weight:650;margin:0 0 2px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-bottom:24px}
.headline{margin:6px 0 34px}
.hn{font-size:38px;font-weight:680;letter-spacing:-.02em}
.hl{color:var(--mut);font-size:13px;text-transform:uppercase;letter-spacing:.05em;margin-left:10px}
h2{font-size:14px;font-weight:600;margin:34px 0 4px}.note{font-size:12px;color:var(--mut);margin:0 0 14px}
.box{border:1px solid var(--line);border-radius:10px;padding:16px 14px 10px;overflow-x:auto}
svg{display:block;max-width:100%}.tk{fill:var(--mut);font-size:10px}.gl{stroke:#eee}.axis{stroke:var(--line)}
.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:24px 0 12px}
.control-label{font-size:12px;color:var(--mut);font-weight:600}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
.seg button{appearance:none;border:0;border-right:1px solid var(--line);background:#fff;color:var(--ink);font:12px/1.2 inherit;padding:7px 10px;cursor:pointer}
.seg button:last-child{border-right:0}.seg button.active{background:#1f1f1f;color:#fff}.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.foot{color:var(--mut);font-size:11px;margin-top:32px}.foot a{color:var(--mut)}
.empty{color:var(--mut);font-size:13px;padding:24px 8px;text-align:center}
.tip{position:fixed;pointer-events:none;background:#1a1a1a;color:#fff;font-size:12px;line-height:1.45;padding:6px 9px;border-radius:6px;white-space:pre;opacity:0;transition:opacity .08s;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.25)}
</style></head><body><div class="wrap">
<h1>slatedb &mdash; crates.io downloads</h1>
<div class="sub" id="sub"></div>
<div class="headline"><span class="hn" id="alltime"></span><span class="hl">all-time downloads</span></div>

<h2>Monthly download volume</h2>
<p class="note">Downloads per calendar month. Hollow bars are partial months (incomplete data at the edges of the captured window).</p>
<div class="box"><svg id="months" width="920" height="280" viewBox="0 0 920 280"></svg><div id="months-empty"></div></div>

<h2>Most-downloaded versions (all-time)</h2>
<p class="note">Cumulative downloads per release since publication.</p>
<div class="box"><svg id="versions" width="920" height="320"></svg><div id="versions-empty"></div></div>

<h2>Daily downloads (last 90 days)</h2>
<p class="note">Weekly sawtooth = weekday vs weekend (CI traffic).</p>
<div class="controls" id="traffic-controls" hidden>
  <span class="control-label">Traffic</span>
  <span class="seg" role="group" aria-label="Download traffic filter">
    <button type="button" data-view="all" class="active">All downloads</button>
    <button type="button" data-view="filtered">Exclude v0.10.1</button>
  </span>
</div>
<p class="note" id="traffic-note" style="max-width:760px"></p>
<div class="box"><svg id="daily" width="920" height="240" viewBox="0 0 920 240"></svg><div id="daily-empty"></div></div>

<h3 style="font-size:13px;font-weight:600;margin:24px 0 4px">Download rate</h3>
<p class="note"><span style="color:var(--accent);font-weight:600">Green</span>: 7-day average. <span style="color:var(--accent2);font-weight:600">Blue dashed</span>: 28-day average. Measured daily data only.</p>
<div class="box"><svg id="rate" width="920" height="240" viewBox="0 0 920 240"></svg><div id="rate-empty"></div></div>

<h2 style="margin-top:48px">Estimated growth</h2>
<p class="note" style="max-width:760px">crates.io keeps daily downloads for only ~90 days. For earlier history, this chart estimates cumulative downloads from the data crates.io does keep: each version's release date, each version's lifetime download count, and every daily download snapshot archived by this repo. Where available, per-version daily snapshots attribute old-version downloads exactly. At each release date, the estimate removes downloads that must have happened later: downloads of versions that did not exist yet, plus archived daily downloads after that date. Because old versions can keep getting pulled by lockfiles, downstream dependencies, and CI, the true historical cumulative total may be lower than the estimate.</p>
<p class="note" id="reconmeta" style="max-width:760px"></p>
<p class="note" id="caveat" style="max-width:760px"></p>

<h3 style="font-size:13px;font-weight:600;margin:24px 0 4px">Cumulative downloads over time</h3>
<p class="note"><span style="color:var(--accent);font-weight:600">Green</span>: estimated cumulative downloads before exact daily data begins. <span style="color:var(--fit);font-weight:600">Red</span>: exact cumulative downloads measured from archived daily data. Hover any point for the version, release date, and cumulative total.</p>
<div class="box"><svg id="recon" width="920" height="300" viewBox="0 0 920 300"></svg><div id="recon-empty"></div></div>

<section id="winsec" hidden>
<h3 style="font-size:13px;font-weight:600;margin:24px 0 4px">90-day-window growth fit</h3>
<p class="note">Consecutive 90-day windows on a log axis. A pure exponential is a perfectly straight line (R&sup2;&nbsp;=&nbsp;1). The <span style="color:var(--fit);font-weight:600">red dashed</span> line is the best-fit exponential through slatedb's own windows.</p>
<div class="box"><svg id="windows" width="920" height="260" viewBox="0 0 920 260"></svg></div>
</section>

<div class="foot" id="foot"></div>
</div>
<div class="tip" id="tip"></div>
<script>
const P=/*DATA*/;
const NS="http://www.w3.org/2000/svg";
const el=(n,a)=>{const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);return e;};
const fmt=n=>Math.round(n).toLocaleString();
const TIP=document.getElementById('tip');
const bindTip=(node,text)=>{node.style.cursor='pointer';
node.addEventListener('mouseenter',()=>{TIP.textContent=text;TIP.style.opacity=1;});
node.addEventListener('mousemove',e=>{TIP.style.left=(e.clientX+12)+'px';TIP.style.top=(e.clientY+12)+'px';});
node.addEventListener('mouseleave',()=>{TIP.style.opacity=0;});};
document.getElementById('sub').textContent=`Organic adoption on crates.io · updated ${P.generated}`;
document.getElementById('alltime').textContent=P.latest_total.toLocaleString();
if(P.reconstruction_meta&&P.reconstruction_meta.daily_start){
const m=P.reconstruction_meta;
document.getElementById('reconmeta').textContent=`Archived daily data currently covers ${m.daily_start} to ${m.daily_end} across ${m.daily_days.toLocaleString()} days; ${m.exact_version_days.toLocaleString()} of those days have exact per-version attribution. The estimate attributes ${m.explicit_daily_total.toLocaleString()} downloads across ${m.explicit_version_count} versions and leaves ${m.extra_daily_total.toLocaleString()} downloads in aggregate extra_downloads where per-version detail is unavailable.`;
}
(function(){const c=P.dominant_release,node=document.getElementById('caveat');if(!node)return;
if(!c){node.remove();return;}
const pct=v=>`${Math.round(v*100)}%`;
node.innerHTML=`<strong>The ${c.num} caveat.</strong> v${c.num} is the dominant active release line &mdash; ${pct(c.lifetime_share)} of all lifetime downloads, and ${pct(c.window_share)} of all measured downloads in the recent window (${fmt(c.window_downloads)} of its ${fmt(c.lifetime)} lifetime downloads landed between ${c.window_start} and ${c.window_end}). That likely reflects pinned dependencies or a long-lived ${c.line} line, not necessarily new adoption accelerating.`;
})();

let trafficView='all';
const hasFiltered=!!P.excluded_view;
const selected=()=>trafficView==='filtered'&&hasFiltered?P.excluded_view:{
latest_total:P.latest_total,
daily:P.daily,
reconstruction:P.reconstruction,
reconstruction_meta:P.reconstruction_meta,
measured:P.measured,
meta:null
};
const clearSvg=svg=>{while(svg.firstChild)svg.removeChild(svg.firstChild);};
const empty=(id,text)=>{const n=document.getElementById(id);n.className='empty';n.textContent=text;};
const resetEmpty=id=>{const n=document.getElementById(id);n.className='';n.textContent='';};
function updateTrafficCopy(){
const note=document.getElementById('traffic-note');
const rn=document.getElementById('reconmeta');
if(trafficView==='filtered'&&hasFiltered){
const e=P.excluded_view,m=e.meta||{};
note.textContent=`Filtered view removes v${e.num}. Daily and rate charts use exact per-version data from ${m.daily_start} to ${m.daily_end}; the aggregate-only day before that is omitted because it cannot be split by version.`;
rn.textContent=`Filtered cumulative total removes all ${fmt(m.excluded_lifetime)} lifetime v${e.num} downloads. ${fmt(m.excluded_daily_total)} of those downloads are observed in exact per-version daily data; the remaining ${fmt(m.unobserved_excluded_lifetime)} older downloads are removed from the cumulative total but cannot be placed on exact days.`;
}else{
note.textContent=hasFiltered?'Use the filter to remove the dominant old pinned-version traffic from the daily, rate, and estimated-growth charts.':'';
if(P.reconstruction_meta&&P.reconstruction_meta.daily_start){
const m=P.reconstruction_meta;
rn.textContent=`Archived daily data currently covers ${m.daily_start} to ${m.daily_end} across ${m.daily_days.toLocaleString()} days; ${m.exact_version_days.toLocaleString()} of those days have exact per-version attribution. The estimate attributes ${m.explicit_daily_total.toLocaleString()} downloads across ${m.explicit_version_count} versions and leaves ${m.extra_daily_total.toLocaleString()} downloads in aggregate extra_downloads where per-version detail is unavailable.`;
}
}
}
if(hasFiltered){
const controls=document.getElementById('traffic-controls');
controls.hidden=false;
controls.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{
trafficView=btn.dataset.view;
controls.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b===btn));
updateTrafficCopy();renderRecon();renderDaily();renderRate();
}));
}
updateTrafficCopy();

// cumulative estimate from version release dates and archived daily data
function renderRecon(){const svg=document.getElementById('recon'),W=920,H=300,m={t:14,r:16,b:44,l:62};
clearSvg(svg);resetEmpty('recon-empty');
const S=selected(),R=S.reconstruction||[];
if(R.length<2){empty('recon-empty','No version-date data yet.');return;}
const iw=W-m.l-m.r,ih=H-m.t-m.b;
const day=s=>{const a=s.split('-').map(Number);return Date.UTC(a[0],a[1]-1,a[2])/864e5;};
const t0=day(R[0].date),t1=day(R[R.length-1].date),span=Math.max(1,t1-t0);
const max=Math.max(S.latest_total,...R.map(r=>r.cap_tight??r.cap)),step=Math.max(50000,Math.round(max/5/50000)*50000),maxR=Math.ceil(max/step)*step;
const xd=dn=>m.l+iw*(dn-t0)/span,x=s=>xd(day(s)),y=v=>m.t+ih-ih*v/maxR;
for(let g=0;g<=maxR;g+=step){const yy=y(g);
svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=(g/1000)+'k';}
// the estimate is only drawn for dates BEFORE measured daily data begins; past that, the red line is exact
const Mu=S.measured||[];
const cut=Mu.length?day(Mu[0].date):Infinity;
const val=(r,field)=>r[field]??r.cap;
const clip=field=>{const out=[];for(let i=0;i<R.length;i++){const di=day(R[i].date),vi=val(R[i],field);
if(di<cut)out.push([di,vi]);
else{const p=out[out.length-1];if(p&&p[0]<cut){const f=(cut-p[0])/(di-p[0]);out.push([cut,p[1]+(vi-p[1])*f]);}break;}}
return out;};
const capPts=clip('cap_tight');
let lp=`M ${xd(capPts[0][0])} ${y(capPts[0][1])}`;
for(let i=1;i<capPts.length;i++)lp+=` L ${xd(capPts[i][0])} ${y(capPts[i][1])}`;
svg.appendChild(el('path',{d:lp,fill:'none',stroke:'#2f6f4f','stroke-width':1.6}));
// measured exact cumulative over the daily window (takes over where the estimate stops)
if(Mu.length>1){let mp=`M ${x(Mu[0].date)} ${y(Mu[0].cum)}`;
for(let i=1;i<Mu.length;i++)mp+=` L ${x(Mu[i].date)} ${y(Mu[i].cum)}`;
svg.appendChild(el('path',{d:mp,fill:'none',stroke:'#b04a2f','stroke-width':2}));
const m0=Mu[0];svg.appendChild(el('text',{class:'tk',x:x(m0.date)+4,y:y(m0.cum)+13,fill:'#b04a2f'})).textContent='measured';
// mark in-window releases on the red line, with the exact cumulative at that date
const cumAt={};Mu.forEach(p=>cumAt[p.date]=p.cum);
R.forEach(r=>{if(!r.versions.length||!(r.date in cumAt))return;const cx=x(r.date),cy=y(cumAt[r.date]);
svg.appendChild(el('circle',{cx,cy,r:3,fill:'#b04a2f'}));
const hit=el('circle',{cx,cy,r:9,fill:'transparent'});
bindTip(hit,`v${r.versions.join(', v')} — released ${r.date}\ncumulative = ${fmt(cumAt[r.date])} (exact, measured)\n+${fmt(r.added)} lifetime downloads`);
svg.appendChild(hit);});}
R.forEach(r=>{if(day(r.date)>=cut)return;const bound=r.cap_tight??r.cap,cx=x(r.date),cy=y(bound);
svg.appendChild(el('circle',{cx,cy,r:2.6,fill:'#2f6f4f'}));
const hit=el('circle',{cx,cy,r:9,fill:'transparent'});
const lbl=r.versions.length?`v${r.versions.join(', v')} — released ${r.date}`:`today (${r.date})`;
const tip=r.versions.length?`${lbl}\n+${fmt(r.added)} lifetime downloads\ncumulative ≤ ${fmt(bound)} before this release`:`${lbl}\ncumulative = ${fmt(bound)} (exact)`;
bindTip(hit,tip);svg.appendChild(hit);});
if(trafficView==='all')(P.cumulative||[]).forEach(c=>{const dd=day(c.date);if(dd>=t0&&dd<=t1){const dot=el('circle',{cx:x(c.date),cy:y(c.total),r:4,fill:'#b04a2f'});bindTip(dot,`live capture ${c.date}\ncumulative = ${fmt(c.total)} (exact)`);svg.appendChild(dot);}});
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));
[[R[0].date,'start'],[R[Math.floor(R.length/2)].date,'middle'],[R[R.length-1].date,'end']].forEach(([dt,anc])=>svg.appendChild(el('text',{class:'tk',x:x(dt),y:H-12,'text-anchor':anc})).textContent=dt);}

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

// version distribution (horizontal bars, top N + other)
(function(){const svg=document.getElementById('versions');
const V=P.versions||[];if(!V.length){document.getElementById('versions-empty').className='empty';document.getElementById('versions-empty').textContent='No version data yet.';svg.remove();return;}
const total=V.reduce((s,v)=>s+v.downloads,0),TOP=12;
let rows=V.slice(0,TOP).map(v=>({num:v.num,dl:v.downloads}));
const rest=V.slice(TOP).reduce((s,v)=>s+v.downloads,0);
if(rest>0)rows.push({num:`other (${V.length-TOP})`,dl:rest,other:true});
const W=920,rh=24,m={t:8,r:96,b:8,l:64},iw=W-m.l-m.r,H=m.t+m.b+rows.length*rh;
svg.setAttribute('height',H);svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
const max=Math.max(...rows.map(r=>r.dl)),bar=rh*0.62;
rows.forEach((r,i)=>{const y=m.t+i*rh,bw=Math.max(iw*r.dl/max,1),pct=r.dl/total*100;
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:y+bar*0.78,'text-anchor':'end'})).textContent=r.num;
svg.appendChild(el('rect',{x:m.l,y,width:bw,height:bar,rx:2,fill:r.other?'#bcbcbc':'#3a3a3a'}));
svg.appendChild(el('text',{class:'tk',x:m.l+bw+6,y:y+bar*0.78})).textContent=`${fmt(r.dl)} · ${pct.toFixed(pct<10?1:0)}%`;});
})();

// daily line
function renderDaily(){const svg=document.getElementById('daily'),W=920,H=240,m={t:12,r:14,b:28,l:48};
clearSvg(svg);resetEmpty('daily-empty');
const D=selected().daily||[];if(D.length<2){empty('daily-empty','Need more days.');return;}
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
const anc=['start','middle','end'];[0,Math.floor(D.length/2),D.length-1].forEach((i,k)=>svg.appendChild(el('text',{class:'tk',x:x(i),y:H-10,'text-anchor':anc[k]})).textContent=D[i][0]);}

// measured download-rate rolling averages
function renderRate(){const svg=document.getElementById('rate'),W=920,H=240,m={t:12,r:58,b:28,l:48};
clearSvg(svg);resetEmpty('rate-empty');
const D=selected().daily||[];if(D.length<7){empty('rate-empty','Need at least 7 days.');return;}
const roll=w=>{const out=[];let sum=0;D.forEach((d,i)=>{sum+=d[1];if(i>=w)sum-=D[i-w][1];if(i>=w-1)out.push({i,date:d[0],v:sum/w,w});});return out;};
const R7=roll(7),R28=D.length>=28?roll(28):[];
const vals=R7.concat(R28).map(p=>p.v),max=Math.max(...vals),step=Math.max(500,Math.round(max/5/500)*500),maxR=Math.ceil(max/step)*step;
const iw=W-m.l-m.r,ih=H-m.t-m.b,x=i=>m.l+iw*i/(D.length-1),y=v=>m.t+ih-ih*v/maxR;
svg.appendChild(el('text',{class:'tk',x:14,y:H/2,'text-anchor':'middle',transform:`rotate(-90 14 ${H/2})`})).textContent='downloads/day';
for(let g=0;g<=maxR;g+=step){const yy=y(g);
svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=(g/1000)+'k';}
const draw=(pts,color,dash)=>{if(!pts.length)return;let path=`M ${x(pts[0].i)} ${y(pts[0].v)}`;
for(let i=1;i<pts.length;i++)path+=` L ${x(pts[i].i)} ${y(pts[i].v)}`;
svg.appendChild(el('path',{d:path,fill:'none',stroke:color,'stroke-width':dash?1.5:1.8,'stroke-dasharray':dash?'5 4':''}));
const last=pts[pts.length-1];svg.appendChild(el('text',{class:'tk',x:x(last.i)+6,y:y(last.v)+3,fill:color})).textContent=`${last.w}d`;};
draw(R7,'#2f6f4f',false);draw(R28,'#3b6fb0',true);
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));
const anc=['start','middle','end'];[0,Math.floor(D.length/2),D.length-1].forEach((i,k)=>svg.appendChild(el('text',{class:'tk',x:x(i),y:H-10,'text-anchor':anc[k]})).textContent=D[i][0]);}

// 90-day window growth fit — only shown once enough windows have accumulated
(function(){const enough=P.enough_windows||6,Wd=P.windows;
if(!Wd||Wd.length<enough)return;
document.getElementById('winsec').hidden=false;
const svg=document.getElementById('windows'),W=920,H=260,m={t:12,r:14,b:42,l:54};
const iw=W-m.l-m.r,ih=H-m.t-m.b,vals=Wd.map(w=>w.total);
const lo=Math.log10(Math.max(1,Math.min(...vals)*0.8)),hi=Math.log10(Math.max(...vals)*1.25);
const x=i=>m.l+iw*i/(Wd.length-1),y=v=>m.t+ih-ih*(Math.log10(v)-lo)/(hi-lo);
const ticks=[];for(let e=Math.floor(lo);e<=Math.ceil(hi);e++){[1,2,5].forEach(mul=>{const v=mul*10**e;if(Math.log10(v)>=lo&&Math.log10(v)<=hi)ticks.push(v);});}
ticks.forEach(v=>{const yy=y(v);svg.appendChild(el('line',{class:'gl',x1:m.l,y1:yy,x2:W-m.r,y2:yy}));
svg.appendChild(el('text',{class:'tk',x:m.l-6,y:yy+3,'text-anchor':'end'})).textContent=v>=1e6?(v/1e6+'M'):v>=1e3?(v/1e3+'k'):v;});
if(P.fit){let kp=`M ${x(0)} ${y(Math.exp(P.fit.a))}`;
for(let i=1;i<Wd.length;i++)kp+=` L ${x(i)} ${y(Math.exp(P.fit.a+P.fit.b*i))}`;
svg.appendChild(el('path',{d:kp,fill:'none',stroke:'#b04a2f','stroke-width':1.3,'stroke-dasharray':'5 4'}));}
let lp=`M ${x(0)} ${y(vals[0])}`;Wd.forEach((w,i)=>{if(i)lp+=` L ${x(i)} ${y(w.total)}`;});
svg.appendChild(el('path',{d:lp,fill:'none',stroke:'#2f6f4f','stroke-width':1.4}));
Wd.forEach((w,i)=>{svg.appendChild(el('circle',{cx:x(i),cy:y(w.total),r:3.2,fill:'#2f6f4f'}));
svg.appendChild(el('text',{class:'tk',x:x(i),y:H-26,'text-anchor':'middle'})).textContent=w.start.slice(2,7);});
if(P.fit)svg.appendChild(el('text',{class:'tk',x:W-m.r,y:m.t+10,'text-anchor':'end','font-weight':600})).textContent=`R² ${P.fit.r2.toFixed(3)}`;
svg.appendChild(el('line',{class:'axis',x1:m.l,y1:m.t+ih,x2:W-m.r,y2:m.t+ih}));})();

renderRecon();renderDaily();renderRate();
document.getElementById('foot').innerHTML='Source: <a href="https://crates.io/crates/slatedb">crates.io</a>';
</script></body></html>"""


if __name__ == "__main__":
    main()
