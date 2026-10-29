"""Sentinel-1 backscatter per month and ESA WorldCover fractions on the licence-edge grid, computed in Google Earth Engine.

    python -m repeat_common.gee_s1 --khg 14.02-04-05.01 --start 2025-11 --end 2026-10 [--cell 200] [--pass DESCENDING]

Method, step by step (Earth Engine does the work; only the 200 m cells are downloaded):
- COPERNICUS/S1_GRD, IW mode, VV and VH, one orbit direction (`--pass`, default the one with more scenes over the KHG) so the incidence angle does not change between months;
  the GEE GRD products are already border-noise, thermal-noise and radiometrically terrain corrected;
- per calendar month: mean of the linear power (10^(dB/10)) over the scenes, back to dB;
- the 10 m pixels are averaged in linear power onto the analysis grid (reduceResolution), never resampled; the grid is the one of
  `repeat_prioritisation.investigations.licence_edge` (AEA, cell `--cell`, origin at the KHG bounds), so the values join by cell;
- ESA WorldCover v200 class fractions per cell (10 tree, 20 shrub, 30 grass, 40 crop, 50 built, 60 bare, 80 water, 90 wetland,
  95 mangrove) from the same reduceResolution: backscatter over peat depends on cover first, so the analysis compares within a class.

Output: `<khg>/03_neraca/inputs/s1/s1_<start>_<end>_<pass>_<cell>m.tif` (bands vv_YYYY-MM, vh_YYYY-MM, n_YYYY-MM scene counts,
wc10 ... wc95) with the grid in the tags; a sidecar `.json` records collection, dates and the Earth Engine fetch date.
C-band backscatter responds to peat soil moisture and water table in open peat, weakly under dense canopy and where the water table
is deeper than about 0.6 m (Asmuss et al. 2019, Remote Sens. 11, 1002; Raesaenen et al. 2022; Mleczko et al. 2025 for Kalimantan).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import rasterio
import shapely
from affine import Affine
from pyproj import Transformer

from repeat_common import gee, khg as K

AEA = "+proj=aea +lat_1=-8 +lat_2=4 +lat_0=-2 +lon_0=118 +datum=WGS84 +units=m +no_defs"
AEA_WKT = Transformer.from_crs(4326, AEA, always_xy=True).target_crs.to_wkt("WKT1_GDAL")
WC_CLASSES = [10, 20, 30, 40, 50, 60, 80, 90, 95]


def months(start: str, end: str):
    """(first day, first day after) of each calendar month from start to end ('YYYY-MM', both included)."""
    y, m = map(int, start.split("-"))
    y1, m1 = map(int, end.split("-"))
    out = []
    while (y, m) <= (y1, m1):
        ny, nm = (y + (m == 12), m % 12 + 1)
        out.append((date(y, m, 1), date(ny, nm, 1)))
        y, m = ny, nm
    return out


def grid_of(khg_gpkg: Path, cell: float):
    """Origin and shape of the licence-edge lattice: cell centres at bounds_min + cell/2 + k*cell inside the AEA bounds of the KHG."""
    import geopandas as gpd
    g = shapely.make_valid(gpd.read_file(khg_gpkg).to_crs(4326).geometry.union_all())
    tf = Transformer.from_crs(4326, AEA, always_xy=True)
    ga = shapely.transform(g, lambda c: np.column_stack(tf.transform(c[:, 0], c[:, 1])))
    x0, y0, x1, y1 = ga.bounds
    nx, ny = len(np.arange(x0 + cell / 2, x1, cell)), len(np.arange(y0 + cell / 2, y1, cell))
    return x0, y0, nx, ny, g


def build_image(ee, region, mm, pas, proj_aea, transform):
    col = (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(region).filter(ee.Filter.eq("instrumentMode", "IW"))
           .filter(ee.Filter.eq("orbitProperties_pass", pas)).filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
           .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH")).select(["VV", "VH"]))
    native = col.first().select("VV").projection()
    bands = []
    for a, b in mm:
        tag = f"{a.year}-{a.month:02d}"
        sub = col.filterDate(str(a), str(b))
        mean = sub.map(lambda im: ee.Image(10).pow(im.divide(10)).copyProperties(im, ["system:time_start"])).mean()
        empty = ee.Image.constant([1, 1]).rename(["VV", "VH"]).updateMask(ee.Image(0))      # a month with no scene is masked, not an error
        power = ee.Image(ee.Algorithms.If(sub.size().gt(0), mean, empty)).setDefaultProjection(native)
        agg = power.reduceResolution(ee.Reducer.mean(), maxPixels=4096).reproject(crs=AEA_WKT, crsTransform=transform)
        db = agg.log10().multiply(10).rename([f"vv_{tag}", f"vh_{tag}"])
        n = ee.Image.constant(sub.size()).rename(f"n_{tag}").toFloat()
        bands.append(ee.Image.cat([db, n.reproject(crs=AEA_WKT, crsTransform=transform)]))
    wc = ee.ImageCollection("ESA/WorldCover/v200").first().select("Map")
    wcn = wc.projection()
    fr = ee.Image.cat([wc.eq(c).rename(f"wc{c}").toFloat() for c in WC_CLASSES]).setDefaultProjection(wcn)
    bands.append(fr.reduceResolution(ee.Reducer.mean(), maxPixels=4096).reproject(crs=AEA_WKT, crsTransform=transform))
    return bands


def fetch(code: str, start: str, end: str, cell: float, pas: str, project=None, force=False):
    f = K.folder(code)
    x0, y0, nx, ny, g = grid_of(f / "00_inputs" / "khg.gpkg", cell)
    dest = f / "03_neraca" / "inputs" / "s1" / "s1.tif"
    ee = gee.init(project)
    mm = months(start, end)
    region0 = ee.Geometry.Rectangle(list(g.bounds), proj="EPSG:4326", geodesic=False)
    if pas == "AUTO":      # the direction with more scenes over the KHG (Sumatra: ascending; Kahayan-Sebangau: descending only)
        cnt = {p: ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(region0.centroid(1)).filter(ee.Filter.eq("instrumentMode", "IW"))
               .filter(ee.Filter.eq("orbitProperties_pass", p)).filterDate(str(mm[0][0]), str(mm[-1][1])).size().getInfo()
               for p in ("ASCENDING", "DESCENDING")}
        pas = max(cnt, key=cnt.get)
        print(f"{code}: scenes over the KHG centre {cnt}, using {pas}", flush=True)
    dest = dest.with_name(f"s1_{start}_{end}_{pas.lower()}_{int(cell)}m.tif")
    if dest.exists() and not force:
        print(f"{dest} exists", flush=True)
        return dest
    ytop = y0 + ny * cell                      # raster rows run north to south; cell centres sit at y0 + cell/2 + k*cell
    transform = [cell, 0, x0, 0, -cell, ytop]
    region = ee.Geometry.Rectangle(list(g.bounds), proj="EPSG:4326", geodesic=False)
    imgs = build_image(ee, region, mm, pas, AEA, transform)      # one image per month and one for WorldCover: small requests
    names = [b for im in imgs for b in im.bandNames().getInfo()]
    dest.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((len(names), ny, nx), np.nan, dtype="float32")
    tile = 64            # 64 cells of 200 m = 12.8 km: a tile holds 1.6 million 10 m pixels, small enough for the request memory limit
    k = 0
    for im in imgs:
        nb = im.bandNames().getInfo()
        for r0 in range(0, ny, tile):
            for c0 in range(0, nx, tile):
                h, w = min(tile, ny - r0), min(tile, nx - c0)
                req = {"expression": im.unmask(-9999), "fileFormat": "NUMPY_NDARRAY",
                       "grid": {"dimensions": {"width": w, "height": h}, "crsWkt": AEA_WKT,
                                "affineTransform": {"scaleX": cell, "shearX": 0, "translateX": x0 + c0 * cell, "shearY": 0, "scaleY": -cell,
                                                    "translateY": ytop - r0 * cell}}}
                out = ee.data.computePixels(req)
                for j, b in enumerate(nb):
                    v = out[b].astype("float32")
                    v[v <= -9998] = np.nan
                    arr[k + j, r0:r0 + h, c0:c0 + w] = v
        k += len(nb)
        print(f"  {nb[0]} done", flush=True)
    prof = {"driver": "GTiff", "width": nx, "height": ny, "count": len(names), "dtype": "float32", "crs": AEA,
            "transform": Affine(cell, 0, x0, 0, -cell, ytop), "compress": "deflate", "nodata": np.nan}
    with rasterio.open(dest, "w", **prof) as o:
        o.write(arr)
        o.descriptions = tuple(names)
    dest.with_suffix(".json").write_text(json.dumps({"collection": "COPERNICUS/S1_GRD", "pass": pas, "months": [str(a) for a, _ in mm],
                                                      "worldcover": "ESA/WorldCover/v200", "cell_m": cell, "fetched": str(date.today()),
                                                      "origin_x0": x0, "origin_y0": y0, "nx": nx, "ny": ny}, indent=1))
    print(f"wrote {dest}", flush=True)
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--khg", nargs="+", required=True)
    ap.add_argument("--start", default="2025-11")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--cell", type=float, default=200.0)
    ap.add_argument("--pass", dest="pas", default="AUTO", choices=["AUTO", "ASCENDING", "DESCENDING"])
    ap.add_argument("--project", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    for code in a.khg:
        try:
            fetch(code, a.start, a.end, a.cell, a.pas, a.project, a.force)
        except Exception as e:      # noqa: batch: one KHG failing must not stop the others
            print(f"{code} failed: {e}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
