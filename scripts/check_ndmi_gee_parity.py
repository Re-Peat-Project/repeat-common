"""Parity test: Sentinel-2 dry-season NDMI / NDVI from Earth Engine (repeat_common.gee_s2) against the composites the
prioritisation made locally from Planetary Computer (sources.ndmi_cloud), on the same grid.

    python scripts/check_ndmi_gee_parity.py --khg 15.05-07-10.01 [--dem lidar] [--years 2015 2023] [--months 8 9 10]

Reads $REPEAT_KHG/<khg>/cache/<code>/annual/ndmi_<year>.tif and ndvi_<year>.tif, computes the Earth Engine composite on each
file's grid, and reports, per year and band: cells valid in both / only one, mean and 95th percentile of the absolute difference,
correlation; and per sub-KHG unit (02_delineation/<dem>/subkhg_units.gpkg): the baseline mean NDMI (the u_ndmi of the
prioritisation: mean over the years of the unit means), its largest difference, the Spearman correlation of the unit order, and the
units that change side of the interventions' dryness threshold (ndmi_dry, 0.30). Writes the table to
$REPEAT_KHG/<khg>/cache/gee_parity/ndmi_parity.csv and the report to repeat-common/docs/reports/ndmi_gee_parity_<code>.md.
Needs EE_PROJECT.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from repeat_common import gee, gee_s2, khg as K  # noqa: E402

NDMI_DRY = 0.30


def md_table(t: pd.DataFrame) -> str:
    rows = [list(map(str, t.columns))] + [[f"{v:.4f}" if isinstance(v, float) else str(v) for v in r] for r in t.itertuples(index=False)]
    return "\n".join("| " + " | ".join(r) + " |" for r in rows[:1] + [["---"] * len(rows[0])] + rows[1:])


def unit_means(arr: np.ndarray, label: np.ndarray, n: int) -> np.ndarray:
    ok = np.isfinite(arr) & (label > 0)
    s = np.bincount(label[ok], weights=arr[ok], minlength=n + 1)
    c = np.bincount(label[ok], minlength=n + 1)
    with np.errstate(invalid="ignore"):
        return (s / c)[1:]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--khg", required=True)
    ap.add_argument("--dem", default="lidar", help="the variant whose sub-KHG units are used")
    ap.add_argument("--years", nargs=2, type=int, default=[2015, 2023])
    ap.add_argument("--months", nargs="+", type=int, default=[8, 9, 10])
    a = ap.parse_args()
    import geopandas as gpd
    ee = gee.init(os.environ.get("EE_PROJECT"))
    code = K.lookup(a.khg)["khg_code"][4:]
    kf = K.folder(a.khg)
    ann = kf / "cache" / code / "annual"
    units = gpd.read_file(K.step_dir(code, "02_delineation", a.dem) / "subkhg_units.gpkg")
    out = kf / "cache" / "gee_parity"
    out.mkdir(parents=True, exist_ok=True)
    rows, u_local, u_gee = [], [], []
    for year in range(a.years[0], a.years[1] + 1):
        f = ann / f"ndmi_{year}.tif"
        if not f.is_file():
            continue
        with rasterio.open(f) as r:
            crs, tr, shape = r.crs.to_string(), r.transform, r.shape
            local = {"ndmi": r.read(1).astype("float32")}
            bounds = r.bounds
        nv = ann / f"ndvi_{year}.tif"
        if nv.is_file():
            with rasterio.open(nv) as r:
                local["ndvi"] = r.read(1).astype("float32")
        region = ee.Geometry.Rectangle(list(bounds), crs, False)
        t0 = time.time()
        try:
            g = gee_s2.to_grid(ee, gee_s2.composite(ee, year, a.months, region), crs, tr, shape, bands=tuple(local))
        except ee.EEException as e:                        # e.g. no Sentinel-2 L2A scenes in Earth Engine for that year
            print(f"{year}: no Earth Engine composite ({str(e)[:90]})", flush=True)
            rows += [{"year": year, "band": b, "valid_local": int(np.isfinite(local[b]).sum()), "valid_gee": 0, "note": "no scenes in Earth Engine"}
                     for b in local]
            continue
        secs = time.time() - t0
        u = units.to_crs(crs)
        label = rasterize([(geom, i + 1) for i, geom in enumerate(u.geometry)], out_shape=shape, transform=tr, fill=0, dtype="int32")
        for b in local:
            x, y = local[b], g[b]
            both = np.isfinite(x) & np.isfinite(y)
            d = np.abs(x[both] - y[both])
            rows.append({"year": year, "band": b, "gee_seconds": round(secs, 1), "valid_local": int(np.isfinite(x).sum()),
                         "valid_gee": int(np.isfinite(y).sum()), "valid_both": int(both.sum()),
                         "only_local": int((np.isfinite(x) & ~np.isfinite(y)).sum()), "only_gee": int((~np.isfinite(x) & np.isfinite(y)).sum()),
                         "mean_abs_diff": float(d.mean()) if d.size else np.nan, "p95_abs_diff": float(np.percentile(d, 95)) if d.size else np.nan,
                         "max_abs_diff": float(d.max()) if d.size else np.nan, "r": float(np.corrcoef(x[both], y[both])[0, 1]) if d.size > 1 else np.nan})
            if b == "ndmi":
                u_local.append(unit_means(x, label, len(u)))
                u_gee.append(unit_means(y, label, len(u)))
        print(f"{year}: {secs:.0f} s  " + "  ".join(f"{r['band']} mean|d| {r['mean_abs_diff']:.4f} p95 {r['p95_abs_diff']:.4f} r {r['r']:.4f}"
                                                    for r in rows if r["year"] == year), flush=True)
    t = pd.DataFrame(rows)
    t.to_csv(out / "ndmi_parity.csv", index=False)
    # u_ndmi over the years both routes have; the local value over every year it has, to show what the missing years change
    ul, ug = np.nanmean(np.vstack(u_local), axis=0), np.nanmean(np.vstack(u_gee), axis=0)
    rho = pd.Series(ul).rank().corr(pd.Series(ug).rank())
    flips = int(np.sum((ul < NDMI_DRY) != (ug < NDMI_DRY)))
    uid = units["unit_id"].astype(str).tolist() if "unit_id" in units else list(range(len(units)))
    ut = pd.DataFrame({"unit_id": uid, "u_ndmi_local": ul.round(4), "u_ndmi_gee": ug.round(4), "diff": (ug - ul).round(4)})
    ut.to_csv(out / "ndmi_parity_units.csv", index=False)
    rep = Path(__file__).resolve().parents[1] / "docs" / "reports" / f"ndmi_gee_parity_{code}.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(
        f"# Sentinel-2 NDMI/NDVI: Earth Engine against Planetary Computer, {K.lookup(a.khg)['name']}\n\n"
        f"`python scripts/check_ndmi_gee_parity.py --khg {code} --dem {a.dem} --years {a.years[0]} {a.years[1]}`; months "
        f"{', '.join(map(str, a.months))}; grid of the cached composites ({shape[1]} x {shape[0]} cells). Local: sources.ndmi_cloud "
        f"(Planetary Computer, computed on this machine). Earth Engine: gee_s2.composite (same method).\n\n"
        "## Per year and band\n\n" + md_table(t) + "\n\n"
        f"## Per sub-KHG ({len(units)} units of the {a.dem} variant), baseline mean NDMI (u_ndmi)\n\n"
        f"- Largest difference of a unit mean: {np.nanmax(np.abs(ug - ul)):.4f}; mean {np.nanmean(np.abs(ug - ul)):.4f}\n"
        f"- Spearman correlation of the unit order: {rho:.4f}\n"
        f"- Units on the other side of the dryness threshold ({NDMI_DRY}): {flips}\n"
        f"- Earth Engine time per year: {t['gee_seconds'].drop_duplicates().median():.0f} s (median)\n\n"
        f"Table per unit: `$REPEAT_KHG/{kf.name}/cache/gee_parity/ndmi_parity_units.csv`.\n", encoding="utf-8")
    print(f"\nunits: max |diff| {np.nanmax(np.abs(ug - ul)):.4f}, Spearman {rho:.4f}, threshold flips {flips}\nwrote {rep}")


if __name__ == "__main__":
    main()
