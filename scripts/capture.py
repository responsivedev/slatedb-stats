#!/usr/bin/env python3
"""Fetch slatedb download stats from crates.io and append them to the data files.

crates.io retains daily download data for only ~90 days, but cumulative
per-crate / per-version totals persist forever. So we capture both:

  * the cumulative total (robust, never rolls off) -> month-over-month diffs
    give monthly download volume, the figure comparable to other ecosystems.
  * the raw daily endpoint (last 90 days) -> archived per run; deduped across
    snapshots by render.py these reconstruct a permanent daily history.

Pure stdlib so the CI job needs no pip installs.
"""
import json, os, csv, urllib.request, datetime
from pathlib import Path

CRATE = "slatedb"
UA = os.environ.get("CRATES_UA", "slatedb-stats (https://github.com/responsivedev/slatedb-stats)")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snapshots"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def append_row(path, header, row):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def main():
    SNAPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    crate = get(f"https://crates.io/api/v1/crates/{CRATE}")
    downloads = get(f"https://crates.io/api/v1/crates/{CRATE}/downloads")

    # archive the raw daily snapshot (idempotent per day)
    (SNAPS / f"{stamp}.json").write_text(json.dumps(downloads, separators=(",", ":")))

    total = crate["crate"]["downloads"]
    recent = crate["crate"].get("recent_downloads")
    append_row(DATA / "slatedb_monthly.csv",
               ["date", "cumulative_total", "recent_90d"],
               [stamp, total, recent])

    # per-version cumulative totals (wide history in one file)
    for v in crate["versions"]:
        append_row(DATA / "slatedb_versions_monthly.csv",
                   ["date", "version", "cumulative_downloads"],
                   [stamp, v["num"], v["downloads"]])

    print(f"captured {stamp}: cumulative_total={total:,} recent_90d={recent:,} "
          f"versions={len(crate['versions'])}")


if __name__ == "__main__":
    main()
