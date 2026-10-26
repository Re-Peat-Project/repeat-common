"""Download the FIRMS country archive for Indonesia and store it in _data.

    python -m repeat_common.fetch_firms [--years 2012 2025] [--sensor viirs-snpp] [DATA_DIR]

Source: https://firms.modaps.eosdis.nasa.gov/data/country/<sensor>/<year>/ (static yearly CSV, no MAP_KEY).
Polite by design: one file at a time, a pause between files, retries with backoff, files already
downloaded are skipped. Raw CSVs go to <DATA_DIR>/raw/firms/; the combined layer is
<DATA_DIR>/hotspot_<sensor>_<y0>_<y1>.gpkg with every column kept (confidence l/n/h and type 0-3 are
filtered later, in the analysis, so the choice is visible and changeable). Years not yet published
(the current year) return 404 and are reported, not fatal.
"""
import argparse
import os
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import urllib.error
import urllib.request

URL = "https://firms.modaps.eosdis.nasa.gov/data/country/{s}/{y}/{s}_{y}_Indonesia.csv"
PAUSE_S = 5


def download(url, dest):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                dest.write_bytes(r.read())
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            wait = 30 * 2 ** attempt
            print(f"  {url}: {e}; retry in {wait}s")
            time.sleep(wait)
        except OSError as e:
            wait = 30 * 2 ** attempt
            print(f"  {url}: {e}; retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"gave up on {url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    ap.add_argument("--years", nargs=2, type=int, default=[2012, 2025])
    ap.add_argument("--sensor", default="viirs-snpp")
    a = ap.parse_args()
    data = a.data or os.environ.get("REPEAT_DATA")
    if not data:
        raise SystemExit("give the data folder as an argument or set REPEAT_DATA")
    data = Path(data)
    raw = data / "raw" / "firms"
    raw.mkdir(parents=True, exist_ok=True)
    frames, got = [], []
    for y in range(a.years[0], a.years[1] + 1):
        f = raw / f"{a.sensor}_{y}_Indonesia.csv"
        if not f.exists():
            if not download(URL.format(s=a.sensor, y=y), f):
                print(f"{y}: not published")
                continue
            print(f"{y}: downloaded {f.stat().st_size / 1e6:.1f} MB")
            time.sleep(PAUSE_S)
        d = pd.read_csv(f, low_memory=False, dtype={"acq_time": str})
        d["year"] = y
        frames.append(d)
        got.append(y)
    df = pd.concat(frames, ignore_index=True)
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs=4326)
    out = data / f"hotspot_{a.sensor.replace('-', '_')}_{got[0]}_{got[-1]}.gpkg"
    g.to_file(out, driver="GPKG", layer=out.stem)
    print(f"{len(g)} points, years {got[0]}-{got[-1]} -> {out}")


if __name__ == "__main__":
    main()
