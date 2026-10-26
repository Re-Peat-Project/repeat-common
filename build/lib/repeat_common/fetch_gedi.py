"""Fetch GEDI L2A ground elevations over a KHG from Earth Engine into _data/gedi/, as points.

    EE_PROJECT=<cloud project> python -m repeat_common.fetch_gedi --khg "KHG Sungai Sugihan - Sungai Lumpur" [--min-sensitivity 0.98] [DATA_DIR]

Source: LARSE/GEDI/GEDI02_A_002_MONTHLY (GEDI L2A v2 shots rasterised per month on 25 m UTM cells). Shots are kept where
quality_flag = 1, degrade_flag = 0 and sensitivity >= --min-sensitivity (the share of canopy cover the beam can penetrate;
high values are needed under dense tropical forest). Where a 25 m cell holds shots from several months the one with the
highest sensitivity is kept (qualityMosaic). The cells are fetched with repeat_common.gee on the 25 m UTM lattice of the
monthly images (nothing resampled), kept as GeoTIFF next to the points, and turned into one point per cell centre.
Heights are elev_lowestmode, ellipsoidal (WGS84), like the ICESat-2 file: convert with the DEM's geoid before comparing.
Output: <DATA_DIR>/gedi/gedi_l2a_<slug>.gpkg with lon, lat, h_ell, source, time, track (beam), sensitivity, as
repeat_common.fetch_icesat2, so repeat-dem reads both the same way.
"""
import argparse
import os
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from repeat_common import gee, khg

ASSET = "LARSE/GEDI/GEDI02_A_002_MONTHLY"
BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
BANDS = ["elev_lowestmode", "sensitivity", "delta_time", "beam"]
GEDI_EPOCH = pd.Timestamp("2018-01-01")  # delta_time is seconds since this epoch


def fetch(name, data, min_sensitivity=0.98, buffer_m=2000.0):
    import ee
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    sel = b[b[BOUNDARY[2]].map(khg.norm) == khg.norm(name)]
    if sel.empty:
        sys.exit(f"KHG not found: {name}")
    utm = sel.estimate_utm_crs()
    region = sel.to_crs(utm).buffer(buffer_m).to_crs(4326).union_all()
    slug = "_".join(w.capitalize() for w in khg.slug(name).split("_"))
    tif = data / "gedi" / f"gedi_l2a_{slug}.tif"
    gpkg = tif.with_suffix(".gpkg")
    roi = ee.Geometry.Rectangle(list(region.bounds))

    def good(i):
        ok = i.select("quality_flag").eq(1).And(i.select("degrade_flag").eq(0)).And(i.select("sensitivity").gte(min_sensitivity))
        return i.select(BANDS).toDouble().updateMask(ok)

    img = ee.ImageCollection(ASSET).filterBounds(roi).map(good).qualityMosaic("sensitivity").unmask(-9999)
    gee.to_geotiff(img, region, tif, crs=f"EPSG:{utm.to_epsg()}", transform=[25, 0, 0, 0, -25, 0], dtype="float64",
                   nodata=-9999, source=f"{ASSET} quality_flag=1 degrade_flag=0 sensitivity>={min_sensitivity} qualityMosaic(sensitivity)")
    with rasterio.open(tif) as d:
        a = d.read()
        r, c = np.nonzero(a[0] != -9999)
        x, y = d.transform * (c + 0.5, r + 0.5)
    pts = gpd.GeoDataFrame({"h_ell": a[0, r, c], "source": "gedi_l2a", "time": GEDI_EPOCH + pd.to_timedelta(a[2, r, c], unit="s"),
                            "track": a[3, r, c].astype(int).astype(str), "sensitivity": a[1, r, c]},
                           geometry=gpd.points_from_xy(x, y), crs=utm).to_crs(4326)
    pts.insert(0, "lat", pts.geometry.y)
    pts.insert(0, "lon", pts.geometry.x)
    pts.to_file(gpkg, driver="GPKG")
    print(f"{gpkg.name}: {len(pts)} shots, {pts.time.min():%Y-%m} to {pts.time.max():%Y-%m}", flush=True)
    return gpkg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", action="append", required=True)
    ap.add_argument("--min-sensitivity", type=float, default=0.98)
    ap.add_argument("--buffer-m", type=float, default=2000.0)
    ap.add_argument("--project")
    a = ap.parse_args()
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    gee.init(a.project)
    for k in a.khg:
        fetch(k, data, a.min_sensitivity, a.buffer_m)


if __name__ == "__main__":
    main()
