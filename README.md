# slatedb-stats

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

- `data/slatedb_monthly.csv` - cumulative total per capture (the robust series)
- `data/slatedb_versions_monthly.csv` - per-version cumulative totals
- `data/snapshots/*.json` - raw daily API responses, one per capture
- `data/daily_combined.csv` - permanent daily series, deduped across snapshots
- `docs/index.html` - regenerated dashboard (GitHub Pages)
- `scripts/capture.py` / `scripts/render.py` - the two job steps

Run locally: `python3 scripts/capture.py && python3 scripts/render.py`

## Current numbers

<!--STATS-->
_Updated 2026-06-23 18:44 UTC_

- All-time downloads: **453,849**
- Daily history reconstructed: **90 days**
- Full calendar months: **2**
- 90-day-window exponential fit: _pending_ (1 of ~6 windows captured)

See the live dashboard: **https://responsivedev.github.io/slatedb-stats/**
<!--/STATS-->
