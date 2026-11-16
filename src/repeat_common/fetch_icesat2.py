"""Fetch ICESat-2 ATL08 ground heights over a KHG from SlideRule into _data/icesat2/, as returned.

    python -m repeat_common.fetch_icesat2 --khg "KHG Sungai Sugihan - Sungai Lumpur" [--t0 2019-01-01] [--t1 2025-12-31] [DATA_DIR]

Same request as repeat-dem's ground_truth.fetch_sliderule (ATL08 through PhoREAL, 100 m segments, classes ground,
canopy and top of canopy; terrain height h_te_median, segments with fewer than 10 ground photons dropped), so the
points can feed demnas_dtm.py with ground.source: local. The KHG plus --buffer-m is split into 0.5 degree tiles
because SlideRule refuses requests that match more than 300 ATL03 granules; tiles are fetched one at a time.
Heights are ellipsoidal (WGS84); convert with the DEM's geoid before comparing.
Output: <DATA_DIR>/icesat2/atl08_<slug>_<t0>_<t1>.gpkg with lon, lat, h_ell, source, time, track.
With --attributes: <slug>_<t0>_<t1>_attrs.gpkg instead, every segment kept (no photon-count cut) with the columns a quality
filter needs: spot (1, 3, 5 are the strong beams), strong, solar_elevation (night < 0), gnd_ph_count, ph_count, h_canopy,
canopy_openness. Existing point files are not touched.
SlideRule sometimes fails to read granules on its side and returns the rest without an error (2026-11-16: the same tile gave
13,625 and 31,632 segments on two requests). --passes N asks every tile N times and joins the answers; --merge F1 F2 ...
joins earlier _attrs files of the same KHG instead of fetching (duplicates dropped on track, spot and time).
--from-attrs writes <name>_ph10.gpkg from <name>_attrs.gpkg: the usual point file, with the 10-photon cut, without fetching.
Fixed 2026-11-13: SlideRule names the ground photon count gnd_ph_count, not ground_photon_count, so the 10-photon cut was never
applied to files fetched before that date (every finite segment was kept).
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


ATTRS = ["segment_id", "spot", "solar_elevation", "gnd_ph_count", "ph_count", "h_canopy", "canopy_openness"]


def fetch(aoi, t0, t1, attributes=False, passes=1):
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
            # SlideRule sometimes fails to read single granules on its side and returns the rest without an error, so a tile
            # can be asked several times (--passes) and the segments of all answers are joined (duplicates dropped)
            parts = []
            for k in range(passes):
                try:
                    parts.append(icesat2.atl08p({"poly": region["poly"], "t0": f"{t0}T00:00:00Z", "t1": f"{t1}T23:59:59Z", **PARMS}))
                except Exception as e:
                    print(f"  tile ({x:.2f}, {y:.2f}) pass {k + 1} failed: {e}", flush=True)
                if passes > 1:
                    time.sleep(5)
            if not parts:
                failed += 1
                continue
            g = pd.concat(parts)
            key = [c for c in ("rgt", "cycle", "spot", "segment_id") if c in g.columns]
            if passes > 1 and len(key) == 4:
                g = g.reset_index().drop_duplicates(subset=key).set_index(g.index.name or "time")
            print(f"  tile ({x:.2f}, {y:.2f}): {len(g)} segments" + (f" from {len(parts)} passes ({', '.join(str(len(p_)) for p_ in parts)})" if passes > 1 else ""), flush=True)
            if not len(g):
                continue
            h = g["h_te_median"].to_numpy("float64")
            pc = next((c for c in ("gnd_ph_count", "ground_photon_count") if c in g.columns), None)
            if pc is None and not attributes:
                sys.exit("SlideRule returned no ground photon count; cannot apply the 10-photon cut")
            keep = np.isfinite(h) & (True if attributes else g[pc].to_numpy() >= MIN_GROUND_PHOTONS)
            g = g[keep]
            extra = {c: g[c].to_numpy() for c in ATTRS if attributes and c in g.columns}
            if attributes and "spot" in g.columns:
                extra["strong"] = np.isin(g["spot"].to_numpy(), [1, 3, 5])
            frames.append(pd.DataFrame({**extra, "lon": g.geometry.x.to_numpy(), "lat": g.geometry.y.to_numpy(),
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
    ap.add_argument("--passes", type=int, default=1, help="ask SlideRule this many times per tile and join the answers")
    ap.add_argument("--from-attrs", action="store_true", help="write <name>_ph10.gpkg (10-photon cut) from <name>_attrs.gpkg")
    ap.add_argument("--merge", nargs="+", type=Path, help="join these _attrs files (same KHG) into <name>_attrs.gpkg, no fetch")
    ap.add_argument("--attributes", action="store_true", help="keep every segment with its quality columns, in <name>_attrs.gpkg")
    a = ap.parse_args()
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    norm = lambda s: " ".join(str(s).split())          # the boundary file has double spaces in some names
    sel = b[b[BOUNDARY[2]].map(norm) == norm(a.khg)]
    if sel.empty:
        sys.exit(f"KHG not found: {a.khg}")
    aoi = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()
    slug = a.khg.replace("KHG ", "").replace("Sungai ", "").replace(" - ", "_").replace(" ", "")
    dest = data / "icesat2" / f"atl08_{slug}_{a.t0[:4]}-{a.t1[:4]}{'_attrs' if a.attributes else ''}.gpkg"
    if a.from_attrs:
        src = dest.with_name(dest.stem + "_attrs.gpkg")
        dest = dest.with_name(dest.stem + "_ph10.gpkg")
        if dest.exists():
            sys.exit(f"{dest} exists")
        g = gpd.read_file(src)
        g = g[g.gnd_ph_count >= MIN_GROUND_PHOTONS][["lon", "lat", "h_ell", "source", "time", "track", "geometry"]]
        g.to_file(dest, driver="GPKG")
        print(f"wrote {dest}: {len(g)} points (>= {MIN_GROUND_PHOTONS} ground photons) from {src.name}")
        return
    if dest.exists():
        sys.exit(f"{dest} exists; delete it to fetch again")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if a.merge:
        parts = [gpd.read_file(f) for f in a.merge]
        g = pd.concat(parts, ignore_index=True)
        g = gpd.GeoDataFrame(g.drop_duplicates(subset=["track", "spot", "time"]), crs="EPSG:4326")
        g.to_file(dest, driver="GPKG")
        print(f"wrote {dest}: {len(g)} points from {', '.join(f'{f.name} ({len(p_)})' for f, p_ in zip(a.merge, parts))}")
        return
    g, failed = fetch(aoi, a.t0, a.t1, a.attributes, a.passes)
    g.to_file(dest, driver="GPKG")
    print(f"wrote {dest}: {len(g)} points{' (incomplete)' if failed else ''}")


if __name__ == "__main__":
    main()
