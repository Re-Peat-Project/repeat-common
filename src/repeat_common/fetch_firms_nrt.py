"""Fetch near-real-time FIRMS active fires (MODIS) for Indonesia from the FIRMS area API into _data.

    python -m repeat_common.fetch_firms_nrt [--start 2026-01-01] [--end today] [--product MODIS_NRT] [DATA_DIR]

Source: https://firms.modaps.eosdis.nasa.gov/api/area/csv/<MAP_KEY>/<product>/<bbox>/<days>/<date>. The key is read from the
environment variable FIRMS_MAP_KEY and is never written to a file or a log. The API returns at most 5 days per request, so
the period is fetched in 5-day windows, one request at a time with a pause. Raw CSVs go to <DATA_DIR>/raw/firms_nrt/
(windows that are complete, i.e. end before today, are reused; the open window is always refetched). The combined layer is
<DATA_DIR>/hotspot_<product>_<year>.gpkg (layer hotspots, EPSG:4326): every column of the API plus `acq_datetime` (UTC) and
`fetched`. This is the same MODIS Collection 6.1 NRT stream as the NASA FIRMS web map; the KLH 2026 file
(_data_hotspot_Januari_September_2026.gpkg) is a curated MODIS extract and is compared with it by
repeat_prioritisation.investigations.hotspot_splice.
"""
import argparse
import io
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd

URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{product}/{bbox}/{days}/{day}"
BBOX = "94,-11,142,6"          # Indonesia incl. Papua
WINDOW = 5
PAUSE_S = 3


def fetch(url: str) -> str:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return r.read().decode("utf-8")
        except (urllib.error.HTTPError, OSError) as e:
            code = getattr(e, "code", None)
            if code in (400, 401, 403):
                raise SystemExit(f"FIRMS refused the request (HTTP {code}); check FIRMS_MAP_KEY and the date range")
            time.sleep(30 * 2 ** attempt)
    raise SystemExit("FIRMS did not answer after 5 tries")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir", nargs="?", default=os.environ.get("REPEAT_DATA"))
    ap.add_argument("--product", default="MODIS_NRT")
    ap.add_argument("--start", default=f"{date.today().year}-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    a = ap.parse_args(argv)
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        raise SystemExit("set FIRMS_MAP_KEY (free key from https://firms.modaps.eosdis.nasa.gov/api/map_key/)")
    data = Path(a.data_dir)
    raw = data / "raw" / "firms_nrt"
    raw.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    frames, day = [], start
    while day <= end:
        n = min(WINDOW, (end - day).days + 1)
        f = raw / f"{a.product}_{day.isoformat()}_{n}d.csv"
        closed = day + timedelta(days=n) <= date.today()
        if not (f.exists() and closed):
            f.write_text(fetch(URL.format(key=key, product=a.product, bbox=BBOX, days=n, day=day.isoformat())), encoding="utf-8")
            time.sleep(PAUSE_S)
        txt = f.read_text(encoding="utf-8")
        if txt.startswith("latitude"):
            frames.append(pd.read_csv(io.StringIO(txt)))
        else:
            raise SystemExit(f"{f.name}: unexpected response: {txt[:120]!r}")
        day += timedelta(days=n)
    df = pd.concat(frames, ignore_index=True).drop_duplicates()
    t = df["acq_time"].astype(int).astype(str).str.zfill(4)
    df["acq_datetime"] = pd.to_datetime(df["acq_date"] + " " + t.str[:2] + ":" + t.str[2:], utc=True)
    df["fetched"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs=4326)
    out = data / f"hotspot_{a.product.lower()}_{start.year}.gpkg"
    gdf.to_file(out, layer="hotspots", driver="GPKG")
    print(f"{out}: {len(gdf)} detections, {df.acq_date.min()} to {df.acq_date.max()}")


if __name__ == "__main__":
    main()
