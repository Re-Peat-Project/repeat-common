"""Fetch ICESat-2 ATL08 ground heights over a KHG from SlideRule into _data/icesat2/, as returned.

    python -m repeat_common.fetch_icesat2 --khg "KHG Sungai Sugihan - Sungai Lumpur" [--t0 2019-01-01] [--t1 2025-12-31] [DATA_DIR]

Same request as repeat-dem's ground_truth.fetch_sliderule (ATL08 through PhoREAL, 100 m segments, classes ground,
canopy and top of canopy; terrain height h_te_median, segments with fewer than 10 ground photons dropped), so the
points can feed demnas_dtm.py with ground.source: local. The KHG plus --buffer-m is split into 0.5 degree tiles
because SlideRule refuses requests that match more than 300 ATL03 granules; tiles are fetched one at a time.
Heights are ellipsoidal (WGS84); convert with the DEM's geoid before comparing.
Output: <DATA_DIR>/icesat2/atl08_<slug>_<t0>_<t1>.gpkg with lon, lat, h_ell, source, time, track.
Needs the sliderule client (pip install sliderule).
"""
import argparse
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
PARMS = {"len": 100.0, "res": 100.0, "atl08_class": ["atl08_ground", "atl08_canopy", "atl08_top_of_canopy"],
         "phoreal": {"binsize": 1.0, "geoloc": "center", "use_abs_h": False, "send_waveform": False, "above_classifier": False}}
MIN_GROUND_PHOTONS = 10
TILE_DEG = 0.5


def fetch(aoi, t0, t1):
    from sliderule import icesat2, sliderule
    sliderule.init(verbose=False)
    x0, y0, x1, y1 = aoi.bounds
    frames, failed = [], 0
    for x in np.arange(x0, x1, TILE_DEG):
        for y in np.arange(y0, y1, TILE_DEG):
            part = aoi.intersection(box(x, y, min(x + TILE_DEG, x1), min(y + TILE_DEG, y1)))
            if part.is_empty or part.area < 1e-6:
                continue
            region = sliderule.toregion(gpd.GeoDataFrame(geometry=[part.simplify(0.001)], crs="EPSG:4326"))
            try:
                g = icesat2.atl08p({"poly": region["poly"], "t0": f"{t0}T00:00:00Z", "t1": f"{t1}T23:59:59Z", **PARMS})
            except Exception as e:
                failed += 1
                print(f"  tile ({x:.2f}, {y:.2f}) failed: {e}", flush=True)
                continue
            print(f"  tile ({x:.2f}, {y:.2f}): {len(g)} segments", flush=True)
            if not len(g):
                continue
            h = g["h_te_median"].to_numpy("float64")
            keep = np.isfinite(h) & (g["ground_photon_count"].to_numpy() >= MIN_GROUND_PHOTONS if "ground_photon_count" in g else True)
            g = g[keep]
            frames.append(pd.DataFrame({"lon": g.geometry.x.to_numpy(), "lat": g.geometry.y.to_numpy(),
                                        "h_ell": g["h_te_median"].to_numpy("float64"), "source": "icesat2_atl08",
                                        "time": pd.Series(g.index if "time" not in g.columns else g["time"]).astype(str).to_numpy(),
                                        "track": (g["rgt"].astype(str) + "_" + g["cycle"].astype(str)).to_numpy()
                                        if {"rgt", "cycle"} <= set(g.columns) else ""}))
            time.sleep(1)
    if failed:
        print(f"WARNING: {failed} tile(s) failed; points are incomplete", flush=True)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["lon", "lat", "h_ell", "source", "time", "track"])
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326"), failed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", required=True)
    ap.add_argument("--buffer-m", type=float, default=500.0)
    ap.add_argument("--t0", default="2019-01-01")
    ap.add_argument("--t1", default="2025-12-31")
    a = ap.parse_args()
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    sel = b[b[BOUNDARY[2]] == a.khg]
    if sel.empty:
        sys.exit(f"KHG not found: {a.khg}")
    aoi = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()
    slug = a.khg.replace("KHG ", "").replace("Sungai ", "").replace(" - ", "_").replace(" ", "")
    dest = data / "icesat2" / f"atl08_{slug}_{a.t0[:4]}-{a.t1[:4]}.gpkg"
    if dest.exists():
        sys.exit(f"{dest} exists; delete it to fetch again")
    dest.parent.mkdir(parents=True, exist_ok=True)
    g, failed = fetch(aoi, a.t0, a.t1)
    g.to_file(dest, driver="GPKG")
    print(f"wrote {dest}: {len(g)} points{' (incomplete)' if failed else ''}")


if __name__ == "__main__":
    main()
