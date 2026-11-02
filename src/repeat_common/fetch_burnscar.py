"""Burn scars of one year over a KHG from Earth Engine into _data/burnscar/: two ready products and a Sentinel-2 dNBR.

    EE_PROJECT=<cloud project> python -m repeat_common.fetch_burnscar --khg 16.02.01 [--year 2026] [--products mcd64a1,vnp64a1,s2_dnbr]
    python -m repeat_common.fetch_burnscar --khg-folders          # every KHG that has a folder in _khg

--khg takes a registry code or name (repeatable). Each product is fetched once per (KHG, year) and kept; --force refetches.

Products, written under <DATA_DIR>/burnscar/<product>/:
  mcd64a1   MODIS/061/MCD64A1, monthly burned area, 500 m sinusoidal grid. Band burn_doy: day of year of the first burn in the
            year (0 = none). NASA, public domain.
  vnp64a1   NASA/VIIRS/002/VNP64A1, the VIIRS monthly burned area on the same grid, same band. Both products lag by about two
            months; the sidecar JSON states the last month in the catalogue at fetch time.
  s2_dnbr   COPERNICUS/S2_SR_HARMONIZED, 20 m (B8A and B12, both native 20 m, nothing resampled) on the KHG's UTM lattice.
            NBR = (B8A - B12) / (B8A + B12). Pixels kept where the scene cloud cover is below --max-cloud and SCL is not
            cloud shadow (3), cloud (8, 9), cirrus (10), snow (11), saturated or no data (0, 1). Band pre = median NBR over
            --pre (x1000, int16); bands dnbr_YYYYMM = pre minus the median NBR of that month (x1000, int16, -32768 = no clear
            observation). A month is written only if its window is at least 10 days long. dNBR is not thresholded here; the threshold is a parameter of the polygon step.
Polygons (derived, local, rerunnable without Earth Engine): <DATA_DIR>/burnscar/burnscar_<year>_<product>_<slug>.gpkg.
  MCD64A1 and VNP64A1: one polygon per group of pixels with the same burn date (fields date, month, area_ha).
  s2_dnbr: only with --dnbr (no default: 0.27, the usual class limit, flags 9% of Sugihan-Lumpur by August where MODIS maps 0.6%). A pixel is burned in the first month its dNBR reaches --dnbr if the
  next month with a clear observation also reaches it, or if no later month is clear; groups of connected pixels under
  --min-ha are dropped. Fields: month, area_ha. Smoke and haze are not masked: check against the ready products
  (re-peat/tools/inventory_burnscar.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio

from repeat_common import gee, khg

BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
READY = {
    "mcd64a1": {"collection": "MODIS/061/MCD64A1", "band": "BurnDate"},
    "vnp64a1": {"collection": "NASA/VIIRS/002/VNP64A1", "band": "Burn_Date"},
}
S2 = "COPERNICUS/S2_SR_HARMONIZED"
S2_BAD_SCL = (0, 1, 3, 8, 9, 10, 11)
NODATA16 = -32768


def region_of(name, data, buffer_m=1000.0):
    """(KHG registry row, UTM crs, region polygon in EPSG:4326 = the KHG plus buffer)."""
    row = khg.lookup(name)
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    sel = b[b[BOUNDARY[2]].map(khg.norm) == khg.norm(row["name"])]
    if sel.empty:
        sys.exit(f"KHG boundary not found: {row['name']}")
    utm = sel.estimate_utm_crs()
    return row, utm, sel.to_crs(utm).buffer(buffer_m).to_crs(4326).union_all()


def months(start: date, end: date):
    """First days of the months from start to end (end exclusive)."""
    out, d = [], date(start.year, start.month, 1)
    while d < end:
        out.append(d)
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return out


def _stamp(path, extra):
    """Sidecar JSON next to a raster: what was fetched, from what, when."""
    import ee
    extra = {**extra, "fetched": str(date.today()), "ee_api": ee.__version__}
    path.with_suffix(".json").write_text(json.dumps(extra, indent=1), encoding="utf-8")


def fetch_ready(ee, product, year, region, dest):
    spec = READY[product]
    coll = ee.ImageCollection(spec["collection"]).filterDate(f"{year}-01-01", f"{year + 1}-01-01")
    n = coll.size().getInfo()
    if n == 0:
        sys.exit(f"{spec['collection']} has no image in {year}")
    last = ee.Date(coll.aggregate_max("system:time_start")).format("YYYY-MM").getInfo()
    p = ee.ImageCollection(spec["collection"]).first().select(spec["band"]).projection().getInfo()
    # first burn of the year: the smallest day of year over the months, 0 where no month burned
    img = coll.select(spec["band"]).map(lambda i: i.updateMask(i.gt(0))).min().rename("burn_doy").unmask(0).toInt16()
    gee.to_geotiff(img, region, dest, crs=p["crs"], transform=p["transform"], dtype="int16", nodata=0,
                   source=f"{spec['collection']} {spec['band']} first burn of {year}")
    _stamp(dest, {"collection": spec["collection"], "band": spec["band"], "year": year, "months_in_catalogue": n,
                  "last_month": last, "grid_crs": p["crs"], "grid_transform": p["transform"],
                  "note": "burn_doy = day of year of the first burn; 0 = none (also unmapped and water)"})
    return last


def s2_collection(ee, region, start, end, max_cloud):
    def nbr(i):
        scl = i.select("SCL")
        ok = scl.remap(list(S2_BAD_SCL), [0] * len(S2_BAD_SCL), 1).eq(1)
        b = i.select(["B8A", "B12"]).toFloat()
        return b.normalizedDifference(["B8A", "B12"]).updateMask(ok.And(b.select("B8A").gt(0)).And(b.select("B12").gt(0))).rename("nbr")
    return (ee.ImageCollection(S2).filterBounds(region).filterDate(str(start), str(end))
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud)).map(nbr))


def fetch_s2(ee, year, region, utm, dest, pre, first_month, end, max_cloud):
    ee_region = ee.Geometry.Rectangle(list(region.bounds))
    pre_img = s2_collection(ee, ee_region, pre[0], pre[1], max_cloud).median()
    imgs, names = [pre_img.multiply(1000).rename("pre")], ["pre"]
    for m in months(first_month, end):
        m1 = min(date(m.year + m.month // 12, m.month % 12 + 1, 1), end)
        if (m1 - m).days < 10:                                  # a window of a few days holds too few scenes to be a month
            continue
        col = s2_collection(ee, ee_region, m, m1, max_cloud)
        post = ee.Image(ee.Algorithms.If(col.size().gt(0), col.median(), ee.Image.constant(0).rename("nbr").selfMask()))
        imgs.append(pre_img.subtract(post).multiply(1000).rename(f"dnbr_{m:%Y%m}"))
        names.append(f"dnbr_{m:%Y%m}")
    img = ee.Image.cat(imgs).round().toInt16().unmask(NODATA16)
    gee.to_geotiff(img, region, dest, crs=f"EPSG:{utm.to_epsg()}", transform=[20, 0, 0, 0, -20, 0], dtype="int16", nodata=NODATA16,
                   source=f"{S2} NBR(B8A,B12) dNBR vs median {pre[0]}..{pre[1]}", max_bytes=16 * 1024 * 1024)
    _stamp(dest, {"collection": S2, "bands": names, "pre_window": [str(pre[0]), str(pre[1])], "max_cloud_pct": max_cloud,
                  "scl_masked": list(S2_BAD_SCL), "end_exclusive": str(end), "scale": "x1000, int16, -32768 = no clear observation",
                  "grid": f"EPSG:{utm.to_epsg()}, 20 m"})


def _shapes(arr, transform, crs, fields):
    """Polygons of the connected groups of pixels with the same value of arr (0 = skip); returns a GeoDataFrame."""
    from rasterio.features import shapes
    from shapely.geometry import shape
    rows = [(shape(g), int(v)) for g, v in shapes(arr, mask=arr > 0, transform=transform, connectivity=8)]
    return gpd.GeoDataFrame({fields: [v for _, v in rows]}, geometry=[g for g, _ in rows], crs=crs)


def polygons_ready(tif, year):
    with rasterio.open(tif) as d:
        a = d.read(1)
        g = _shapes(a.astype("int16"), d.transform, d.crs, "doy")
    g["date"] = (np.datetime64(f"{year}-01-01") + (g["doy"].to_numpy() - 1).astype("timedelta64[D]")).astype(str)
    g["month"] = g["date"].str[:7]
    g["area_ha"] = g.to_crs(g.estimate_utm_crs()).area / 1e4 if len(g) else []
    return g.drop(columns="doy").to_crs(4326)


def polygons_s2(tif, thr, min_ha):
    from scipy import ndimage
    with rasterio.open(tif) as d:
        meta = json.loads(tif.with_suffix(".json").read_text(encoding="utf-8"))
        bands = [b for b in meta["bands"] if b.startswith("dnbr_")]
        stack = np.stack([d.read(meta["bands"].index(b) + 1) for b in bands]).astype("float32")
        tr, crs, px_ha = d.transform, d.crs, abs(d.res[0] * d.res[1]) / 1e4
    stack[stack == NODATA16] = np.nan
    t = thr * 1000
    hit = np.nan_to_num(stack, nan=-1e9) >= t                  # dNBR at or above the threshold
    clear = ~np.isnan(stack)
    first = np.full(stack.shape[1:], 0, dtype="uint8")
    for k in range(len(bands) - 1, -1, -1):                    # earliest crossing month, 1-based
        nxt_clear = clear[k + 1:].any(axis=0) if k + 1 < len(bands) else np.zeros_like(clear[0])
        nxt = clear[k + 1:] if k + 1 < len(bands) else np.zeros((0,) + clear.shape[1:], bool)
        confirmed = ~nxt_clear
        if len(nxt):                                           # the next clear month must also reach the threshold
            idx = np.argmax(nxt, axis=0)
            confirmed = confirmed | np.take_along_axis(hit[k + 1:], idx[None], 0)[0]
        first[hit[k] & confirmed] = k + 1
    keep = np.zeros_like(first, dtype=bool)
    lab, n = ndimage.label(first > 0, structure=np.ones((3, 3)))
    if n:
        size = np.bincount(lab.ravel())
        keep = (size[lab] * px_ha >= min_ha) & (lab > 0)
    first[~keep] = 0
    g = _shapes(first, tr, crs, "k")
    g["month"] = [bands[k - 1][5:9] + "-" + bands[k - 1][9:] for k in g["k"]]
    g["area_ha"] = g.area / 1e4
    return g.drop(columns="k").to_crs(4326)


def run(name, data, year, products, a):
    row, utm, region = region_of(name, data)
    slug = khg.slug(row["name"])
    root = data / "burnscar"
    ee = None
    for p in products:
        tif = root / p / f"{p}_{year}_{slug}.tif"
        gpkg = root / f"burnscar_{year}_{p}_{slug}.gpkg"
        if a.force or not tif.exists() or tif.with_suffix(".progress").exists():
            if ee is None:
                ee = gee.init(a.project)
            if p in READY:
                last = fetch_ready(ee, p, year, region, tif)
                print(f"{row['khg_code']} {p}: months up to {last}", flush=True)
            else:
                end = min(date.today(), date(year + 1, 1, 1)) if a.end is None else date.fromisoformat(a.end)
                fetch_s2(ee, year, region, utm, tif, [date.fromisoformat(x) for x in a.pre], date.fromisoformat(a.s2_start), end, a.max_cloud)
        if p == "s2_dnbr" and a.dnbr is None:
            print(f"{row['khg_code']} s2_dnbr: raster only (no --dnbr threshold given)", flush=True)
            continue
        if a.polygons:
            g = polygons_s2(tif, a.dnbr, a.min_ha) if p == "s2_dnbr" else polygons_ready(tif, year)
            g["source"] = p
            gpkg.parent.mkdir(parents=True, exist_ok=True)
            g.to_file(gpkg, driver="GPKG")
            print(f"{row['khg_code']} {p}: {len(g)} polygons, {g.area_ha.sum():.0f} ha -> {gpkg.name}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", action="append", default=[])
    ap.add_argument("--khg-folders", action="store_true", help="every KHG with a folder in _khg")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--products", default="mcd64a1,vnp64a1,s2_dnbr")
    ap.add_argument("--pre", nargs=2, default=None, help="pre-fire window for s2_dnbr, start and end (exclusive); default 1 Jan to 1 Jun")
    ap.add_argument("--s2-start", default=None, help="first month of dNBR, default 1 Jun")
    ap.add_argument("--end", default=None, help="end (exclusive) of the s2_dnbr months, default today")
    ap.add_argument("--max-cloud", type=float, default=60)
    ap.add_argument("--dnbr", type=float, default=None, help="dNBR threshold for the s2_dnbr polygons; no default, polygons are made only when given")
    ap.add_argument("--min-ha", type=float, default=1.0)
    ap.add_argument("--no-polygons", dest="polygons", action="store_false")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--project")
    a = ap.parse_args()
    a.pre = a.pre or [f"{a.year}-01-01", f"{a.year}-06-01"]
    a.s2_start = a.s2_start or f"{a.year}-06-01"
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    names = list(a.khg)
    if a.khg_folders:
        names += [p.name.split("_")[0] for p in sorted(khg.root().iterdir()) if p.is_dir() and p.name[:1].isdigit()]
    if not names:
        sys.exit("give --khg or --khg-folders")
    for n in names:
        run(n, data, a.year, a.products.split(","), a)


if __name__ == "__main__":
    main()
