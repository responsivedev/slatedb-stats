#!/usr/bin/env python3
"""Fetch slatedb download stats from crates.io and append them to the data files.

crates.io retains daily download data for only ~90 days, but cumulative
per-crate / per-version totals persist forever. So we capture both:

  * the cumulative total (robust, never rolls off) -> month-over-month diffs
    give monthly download volume, the figure comparable to other ecosystems.
  * the raw crate daily endpoint (last 90 days) -> archived per run; deduped
    across snapshots by render.py these reconstruct a permanent daily history.
  * per-version daily endpoints -> archived per run; these attribute the
    crate endpoint's extra_downloads bucket back to specific old versions.

Pure stdlib so the CI job needs no pip installs.
"""
import json, os, csv, urllib.request, urllib.parse, datetime
from pathlib import Path

CRATE = "slatedb"
UA = os.environ.get("CRATES_UA", "slatedb-stats (https://github.com/responsivedev/slatedb-stats)")
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SNAPS = DATA / "snapshots"
DL_SNAPS = SNAPS / "downloads"   # raw /downloads responses (daily, last 90d)
CRATE_SNAPS = SNAPS / "crate"    # raw crate-metadata responses (cumulative totals)
VDL_SNAPS = SNAPS / "version_downloads"  # per-version /downloads responses


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def date_already_recorded(path, stamp):
    if not path.exists():
        return False
    with open(path, newline="") as f:
        return any(r and r[0] == stamp for r in csv.reader(f))


def append_row(path, header, row):
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def main():
    DL_SNAPS.mkdir(parents=True, exist_ok=True)
    CRATE_SNAPS.mkdir(parents=True, exist_ok=True)
    VDL_SNAPS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    crate = get(f"https://crates.io/api/v1/crates/{CRATE}")
    downloads = get(f"https://crates.io/api/v1/crates/{CRATE}/downloads")
    version_downloads = {}
    for v in crate["versions"]:
        version = urllib.parse.quote(v["num"], safe="")
        version_downloads[v["num"]] = get(
            f"https://crates.io/api/v1/crates/{CRATE}/{version}/downloads"
        )

    # archive raw responses so everything is reconstructable from data/snapshots
    # alone (overwrites on a same-day re-run).
    (DL_SNAPS / f"{stamp}.json").write_text(json.dumps(downloads, separators=(",", ":")))
    (CRATE_SNAPS / f"{stamp}.json").write_text(json.dumps(crate, separators=(",", ":")))
    (VDL_SNAPS / f"{stamp}.json").write_text(json.dumps({
        "crate": CRATE,
        "date": stamp,
        "versions": version_downloads,
    }, separators=(",", ":")))

    total = crate["crate"]["downloads"]
    recent = crate["crate"].get("recent_downloads")

    # idempotent per date: don't append a second row if today is already recorded
    monthly = DATA / "slatedb_monthly.csv"
    if date_already_recorded(monthly, stamp):
        print(f"{stamp} already recorded; refreshed snapshot only "
              f"(cumulative_total={total:,})")
        return

    append_row(monthly, ["date", "cumulative_total", "recent_90d"],
               [stamp, total, recent])
    for v in crate["versions"]:
        append_row(DATA / "slatedb_versions_monthly.csv",
                   ["date", "version", "cumulative_downloads"],
                   [stamp, v["num"], v["downloads"]])

    print(f"captured {stamp}: cumulative_total={total:,} recent_90d={recent:,} "
          f"versions={len(crate['versions'])} version_downloads={len(version_downloads)}")


if __name__ == "__main__":
    main()
