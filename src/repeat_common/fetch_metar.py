"""Download airport weather reports (METAR) from the Iowa Environmental Mesonet archive into _data.

    python -m repeat_common.fetch_metar --station WAOO [--years 2012 2026] [DATA_DIR]

Source: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py (IEM ASOS/METAR archive, free, no key). Fields: visibility
(vsby, statute miles), wind direction (drct, degrees from) and speed (sknt, knots), present weather codes (wxcodes; FU = smoke,
HZ = haze, BR = mist) and the raw METAR. Times are UTC. One request per year with a pause; years already on disk are skipped,
except the current one. Output: <DATA_DIR>/raw/metar/<station>_<year>.csv and <DATA_DIR>/metar_<station>_<y0>_<y1>.csv.
"""
import argparse
import datetime as dt
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
FIELDS = ["vsby", "drct", "sknt", "wxcodes", "metar"]
PAUSE_S = 5


def download(station, year, dest):
    q = [("station", station), *[("data", f) for f in FIELDS], ("year1", year), ("month1", 1), ("day1", 1),
         ("year2", year + 1), ("month2", 1), ("day2", 1), ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
         ("missing", "empty"), ("trace", "empty"), ("report_type", 3), ("report_type", 4)]
    url = URL + "?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                dest.write_bytes(r.read())
                return
        except (urllib.error.URLError, OSError):
            wait = 30 * 2 ** attempt
        print(f"  retry in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"gave up on {url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    ap.add_argument("--station", required=True)
    ap.add_argument("--years", nargs=2, type=int, default=[2012, dt.date.today().year])
    a = ap.parse_args()
    data = a.data or os.environ.get("REPEAT_DATA")
    if not data:
        raise SystemExit("give the data folder as an argument or set REPEAT_DATA")
    raw = Path(data) / "raw" / "metar"
    raw.mkdir(parents=True, exist_ok=True)
    y0, y1 = a.years
    parts = []
    for y in range(y0, y1 + 1):
        f = raw / f"{a.station}_{y}.csv"
        if not f.exists() or y == dt.date.today().year:
            print(f"{a.station} {y}", flush=True)
            download(a.station, y, f)
            time.sleep(PAUSE_S)
        parts.append(pd.read_csv(f))
    out = Path(data) / f"metar_{a.station}_{y0}_{y1}.csv"
    df = pd.concat(parts, ignore_index=True).drop_duplicates(["station", "valid"])
    df.to_csv(out, index=False)
    print(f"done: {out} ({len(df)} reports)")


if __name__ == "__main__":
    main()
