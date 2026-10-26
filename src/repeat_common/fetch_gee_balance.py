"""Rain and evapotranspiration per sub-KHG per dekad (or month) from Google Earth Engine, for the neraca air balance.

    python -m repeat_common.fetch_gee_balance --khg "Kampar - Gaung" --products chirps,wapor,mod16 --start 2018-01-01 --end 2026-10-01
    python -m repeat_common.fetch_gee_balance --units users/akramsp/BatasSubKHG_KahayanSebangau --id-field SubKHG \
        --products terraclimate_pr,terraclimate_aet --step month --start 2010-01-01 --end 2021-01-01 --out <dir>

--khg reads the units from `_khg/<code>_<name>/02_delineation/subkhg_units.gpkg` and writes to `03_neraca/inputs/`;
--khg with --whole-khg takes the KHG boundary from `$REPEAT_DATA/batas_khg_indonesia.gpkg` as one unit (unit_id = KHG code)
and writes to `03_neraca/inputs/khg_level/` (for a KHG without sub-KHG units);
--units takes a polygon file (unit_id column, or --id-field) or an Earth Engine table id. Needs `EE_PROJECT` (AGENTS.md).

For every period (dekads 1-10, 11-20, 21-end of month; or calendar months) the period total in mm is the sum over the source
images that overlap it of (daily rate of the image x days of overlap). The daily rate is the image value x scale, divided
by the image length when the value is a total over the image (CHIRPS day, MOD16 8-day composite, TerraClimate month) and
as is when it is a mean rate (WaPOR AETI: mean mm/day of the dekad). Image lengths follow the catalogue: CHIRPS 1 day,
WaPOR the dekad, MOD16 8 days or up to 1 January (the last composite of a year is 5 or 6 days), TerraClimate the month.
The sum is reduced over each unit on the product's own grid (CRS and transform of its first image), never resampled.

Written into --out:
  balance_<product>_<step>.csv   unit_id, start, end, days, mm, n_px, time_cov
                                 mm: period total, mean over the unit (area weighted); n_px: valid pixels counted
                                 (ee.Reducer.count); time_cov: share of the period days covered by source images
  balance_<product>_<step>.json  provenance: collection, band, scale, period, Earth Engine API version, date each year was fetched
                                 (the products are revised upstream; the CSV is the record of what a run used)
  units_landcover.csv            unit_id, area_km2, wc_<class> (share of the unit in each ESA WorldCover v200 class, 10 m;
                                 each unit is cut into 0.1 degree pieces so no request times out)
Each year is fetched separately and kept in --cache (default `<khg folder>/cache/gee_balance`, or `<out>/_gee_cache`), so
a failed run resumes. Delete the cache to refetch.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PRODUCTS = {
    # name: collection, band, scale, kind ("total" over the image or "rate" per day), image length rule, valid maximum (raw)
    "chirps":           {"collection": "UCSB-CHG/CHIRPS/DAILY", "band": "precipitation", "scale": 1.0, "kind": "total", "length": "day", "var": "P"},
    "wapor":            {"collection": "FAO/WAPOR/3/L1_AETI_D", "band": "L1-AETI-D", "scale": 0.1, "kind": "rate", "length": "dekad", "var": "ET"},
    "mod16":            {"collection": "MODIS/061/MOD16A2GF", "band": "ET", "scale": 0.1, "kind": "total", "length": "mod16", "var": "ET",
                         "valid_max": 32700},    # 32761-32767 are fill codes (water, urban, barren...)
    # reference rain for checks: IMERG Final monthly, merged satellite-gauge, mm/hr (x 24 = mm/day), 0.1 degree, 1998 to 2025-09
    "imerg":            {"collection": "NASA/GPM_L3/IMERG_MONTHLY_V07", "band": "precipitation", "scale": 24.0, "kind": "rate", "length": "month", "var": "P"},
    "terraclimate_pr":  {"collection": "IDAHO_EPSCOR/TERRACLIMATE", "band": "pr", "scale": 1.0, "kind": "total", "length": "month", "var": "P"},
    "terraclimate_aet": {"collection": "IDAHO_EPSCOR/TERRACLIMATE", "band": "aet", "scale": 0.1, "kind": "total", "length": "month", "var": "ET"},
}
WORLDCOVER = "ESA/WorldCover/v200"
WC_CLASSES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)


# ---------------- period arithmetic (pure Python, tested) ----------------
def periods(start: date, end: date, step: str) -> list[tuple[date, date]]:
    """[start, end) split into dekads or months, end exclusive. start must be the first day of a period."""
    out, d = [], start
    while d < end:
        if step == "month":
            e = date(d.year + d.month // 12, d.month % 12 + 1, 1)
        elif step == "dekad":
            e = d + timedelta(days=10) if d.day in (1, 11) else date(d.year + d.month // 12, d.month % 12 + 1, 1)
            if d.day not in (1, 11, 21):
                raise ValueError(f"{d} is not the first day of a dekad")
        else:
            raise ValueError(f"step must be dekad or month (got {step})")
        out.append((d, min(e, end)))
        d = e
    return out


def image_end(start: date, rule: str) -> date:
    """Exclusive end of a source image that starts on `start`."""
    if rule == "day":
        return start + timedelta(days=1)
    if rule == "dekad":
        return start + timedelta(days=10) if start.day < 21 else date(start.year + start.month // 12, start.month % 12 + 1, 1)
    if rule == "mod16":
        return min(start + timedelta(days=8), date(start.year + 1, 1, 1))
    if rule == "month":
        return date(start.year + start.month // 12, start.month % 12 + 1, 1)
    raise ValueError(rule)


def weights(image_starts: dict[str, date], p0: date, p1: date, rule: str, kind: str) -> tuple[dict[str, float], float]:
    """({image id: factor on its scaled value}, days of the period covered). Factor = overlap days / image days for a total,
    overlap days for a rate."""
    w, covered = {}, 0
    for iid, s in image_starts.items():
        e = image_end(s, rule)
        ov = (min(e, p1) - max(s, p0)).days
        if ov > 0:
            w[iid] = ov / (e - s).days if kind == "total" else float(ov)
            covered += ov
    return w, covered


def khg_unit(boundaries, name_field: str, row: dict):
    """The KHG of a registry row as one unit (unit_id = KHG code) from the national boundary file. Names are compared after
    repeat_common.khg.norm, because spacing differs between the registry and the boundary file."""
    from . import khg as K
    sel = boundaries[boundaries[name_field].map(K.norm) == K.norm(row["name"])]
    return sel[["geometry"]].assign(unit_id=row["khg_code"]).dissolve("unit_id").reset_index()


# ---------------- Earth Engine ----------------
def _units_fc(ee, units, id_field: str):
    if isinstance(units, str):
        return ee.FeatureCollection(units).map(lambda f: ee.Feature(f.geometry(), {"unit_id": ee.String(f.get(id_field))}))
    g = units.to_crs(4326)
    return ee.FeatureCollection([ee.Feature(ee.Geometry(geom.__geo_interface__), {"unit_id": str(u)})
                                 for u, geom in zip(g[id_field], g.geometry)])


def _grid(ee, coll, band):
    info = coll.first().select(band).projection().getInfo()
    return info.get("crs") or info["wkt"], info["transform"]


def fetch_year(ee, fc, name: str, year: int, start: date, end: date, step: str) -> pd.DataFrame:
    p = PRODUCTS[name]
    y0, y1 = max(start, date(year, 1, 1)), min(end, date(year + 1, 1, 1))
    pers = periods(y0, y1, step)
    coll = ee.ImageCollection(p["collection"]).filterBounds(fc.geometry())
    sub = coll.filterDate(str(y0 - timedelta(days=31)), str(y1))
    ids = sub.aggregate_array("system:index").getInfo()
    t0 = sub.aggregate_array("system:time_start").getInfo()
    starts = {i: (date(1970, 1, 1) + timedelta(milliseconds=t)) for i, t in zip(ids, t0)}
    crs, transform = _grid(ee, coll, p["band"])
    bands, meta = [], []
    for k, (a, b) in enumerate(pers):
        w, covered = weights(starts, a, b, p["length"], p["kind"])
        meta.append((a, b, covered / (b - a).days))
        if not w:
            bands.append(ee.Image.constant(0).updateMask(0).rename(f"p{k}"))   # nothing to sum: masked band
            continue
        wd = ee.Dictionary(w)

        def scaled(im, wd=wd):
            x = im.select(p["band"])
            if "valid_max" in p:
                x = x.updateMask(x.lte(p["valid_max"]))
            return x.multiply(p["scale"]).multiply(ee.Number(wd.get(im.get("system:index")))).toFloat()

        bands.append(sub.filter(ee.Filter.inList("system:index", list(w))).map(scaled).sum().rename(f"p{k}"))
    img = ee.Image.cat(bands)
    red = ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True)
    res = img.reduceRegions(collection=fc, reducer=red, crs=crs, crsTransform=transform, tileScale=4).getInfo()
    rows = []
    for f in res["features"]:
        pr = f["properties"]
        for k, (a, b, cov) in enumerate(meta):
            key = f"p{k}_" if len(meta) > 1 else ""        # a one-band image gives plain `mean` and `count`
            rows.append({"unit_id": pr["unit_id"], "start": a, "end": b, "days": (b - a).days, "mm": pr.get(f"{key}mean"),
                         "n_px": pr.get(f"{key}count"), "time_cov": round(cov, 4)})
    return pd.DataFrame(rows)


def fetch(ee, fc, name: str, start: date, end: date, step: str, cache: Path, workers: int = 4) -> pd.DataFrame:
    from concurrent.futures import ThreadPoolExecutor
    cache.mkdir(parents=True, exist_ok=True)

    def one(y):
        f = cache / f"{name}_{step}_{y}_{start}_{end}.csv"
        if not f.exists():
            print(f"{name} {y}", flush=True)
            fetch_year(ee, fc, name, y, start, end, step).to_csv(f, index=False)
        return pd.read_csv(f, dtype={"unit_id": str})

    with ThreadPoolExecutor(workers) as pool:             # one request per year; Earth Engine runs them in parallel
        frames = list(pool.map(one, range(start.year, (end - timedelta(days=1)).year + 1)))
    return pd.concat(frames, ignore_index=True)


def tiles(geom, size: float = 0.1) -> list:
    """Pieces of a shapely geometry (EPSG:4326) cut by a grid of `size` degrees, so each Earth Engine request stays small."""
    import math
    from shapely.geometry import box
    x0, y0, x1, y1 = geom.bounds
    out = []
    for i in range(math.floor(x0 / size), math.ceil(x1 / size)):
        for j in range(math.floor(y0 / size), math.ceil(y1 / size)):
            piece = geom.intersection(box(i * size, j * size, (i + 1) * size, (j + 1) * size))
            if not piece.is_empty and piece.area > 0:
                out.append(piece)
    return out


def landcover(ee, fc, workers: int = 8) -> pd.DataFrame:
    """WorldCover class shares per unit at 10 m. Each unit is cut into 0.1 degree pieces; the pixel counts of the pieces
    (ee.Reducer.frequencyHistogram, area weighted at the edges) are summed, so no request times out on a large unit."""
    from concurrent.futures import ThreadPoolExecutor
    from shapely.geometry import shape
    wc = ee.ImageCollection(WORLDCOVER).first().select("Map")
    proj = wc.projection().getInfo()
    feats = fc.getInfo()["features"]
    jobs = [(f["properties"]["unit_id"], piece) for f in feats for piece in tiles(shape(f["geometry"]))]

    def one(job):
        h = wc.reduceRegion(reducer=ee.Reducer.frequencyHistogram(), geometry=ee.Geometry(job[1].__geo_interface__), crs=proj["crs"],
                            crsTransform=proj["transform"], maxPixels=1e10, tileScale=4).get("Map").getInfo()
        return job[0], {int(float(k)): v for k, v in (h or {}).items()}

    with ThreadPoolExecutor(workers) as pool:
        parts = list(pool.map(one, jobs))
    rows = []
    for f in feats:
        u = f["properties"]["unit_id"]
        h = {}
        for uid, ph in parts:
            if uid == u:
                for k, v in ph.items():
                    h[k] = h.get(k, 0) + v
        n = sum(h.values()) or float("nan")
        area = ee.Geometry(f["geometry"]).area(10).divide(1e6).getInfo()
        rows.append({"unit_id": u, "area_km2": area, **{f"wc_{c}": h.get(c, 0) / n for c in WC_CLASSES}})
    return pd.DataFrame(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--khg", help="KHG name or code; units and output folder from the _khg work folder")
    src.add_argument("--units", help="polygon file or Earth Engine table id")
    ap.add_argument("--whole-khg", action="store_true", help="with --khg: the whole KHG as one unit")
    ap.add_argument("--id-field", default="unit_id")
    ap.add_argument("--products", default="chirps,wapor", help=", ".join(PRODUCTS))
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", required=True, help="exclusive")
    ap.add_argument("--step", choices=["dekad", "month"], default="dekad")
    ap.add_argument("--no-landcover", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--cache")
    ap.add_argument("--project")
    ap.add_argument("--workers", type=int, default=4, help="years fetched at the same time")
    a = ap.parse_args(argv)
    from . import gee
    ee = gee.init(a.project or os.environ.get("EE_PROJECT"))
    if a.khg:
        from . import khg as K
        kf = K.folder(a.khg)
        if a.whole_khg:
            import geopandas as gpd
            data = Path(os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
            b = gpd.read_file(data / gee.BOUNDARY[0], layer=gee.BOUNDARY[1])
            r = K.lookup(a.khg)
            units = khg_unit(b, gee.BOUNDARY[2], r)
            if units.empty:
                raise SystemExit(f"{r['name']} not in {gee.BOUNDARY[0]}")
            out, cache = Path(a.out or kf / "03_neraca" / "inputs" / "khg_level"), Path(a.cache or kf / "cache" / "gee_balance_khg_level")
        else:
            units, out, cache = str(kf / "02_delineation" / "subkhg_units.gpkg"), Path(a.out or kf / "03_neraca" / "inputs"), Path(a.cache or kf / "cache" / "gee_balance")
    else:
        units, out = a.units, Path(a.out or ".")
        cache = Path(a.cache or out / "_gee_cache")
    if isinstance(units, str) and Path(units).suffix.lower() in (".gpkg", ".shp", ".geojson"):
        import geopandas as gpd
        units = gpd.read_file(units)
    fc = _units_fc(ee, units, a.id_field)
    out.mkdir(parents=True, exist_ok=True)
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    for name in [s.strip() for s in a.products.split(",") if s.strip()]:
        d = fetch(ee, fc, name, start, end, a.step, cache, a.workers)
        f = out / f"balance_{name}_{a.step}.csv"
        d.to_csv(f, index=False, float_format="%.3f")
        chunks = sorted(cache.glob(f"{name}_{a.step}_*_{start}_{end}.csv"))
        meta = {**PRODUCTS[name], "product": name, "step": a.step, "start": str(start), "end": str(end), "units": str(a.units or a.khg),
                "earthengine_api": ee.__version__, "written": str(date.today()),
                "fetched": {c.name: date.fromtimestamp(c.stat().st_mtime).isoformat() for c in chunks}}
        f.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"wrote {f} ({d.unit_id.nunique()} units, {d.start.nunique()} periods)")
    if not a.no_landcover:
        f = out / "units_landcover.csv"
        landcover(ee, fc, 2 * a.workers).to_csv(f, index=False, float_format="%.5f")
        print(f"wrote {f}")


if __name__ == "__main__":
    main()
