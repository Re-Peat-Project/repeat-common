"""
Daily rain and evaporation from ERA5 on local days, cached per KHG window.

Hourly accumulations at time t cover the hour ending at t, and ERA5 counts evaporation as negative. The local day d
(UTC+tz) starts at d 00:00 local = d - tz hours UTC, so it holds the hourly values at d - tz + 1 h ... d - tz + 24 h.
Rain uses all 24 hours; evaporation is smooth through the day and uses every `stride` hour (default 3), scaled to 24.
A day with any missing hour is NaN (ERA5 reaches about 5 days before today).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio import Affine
from rasterio.enums import Resampling

from .grid import Grid, array_to_grid
from .sources import Era5

log = logging.getLogger("repeat_common.forcing")


def daily_forcing(grid: Grid, era5_cfg: dict, start, end, cache_dir: Path, tz_hours: int = 7, stride: int = 3) -> dict:
    """{'dates', 'P', 'E', 'transform', 'valid'}: P, E in mm/day, shape (days, ny, nx), north to south."""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    cache_dir = Path(cache_dir)
    path = cache_dir / f"era5_daily_{start:%Y%m%d}_{end:%Y%m%d}_tz{tz_hours}_s{stride}.npz"
    if path.exists():
        z = np.load(path)
        log.info("ERA5 daily forcing %s..%s from cache", start.date(), end.date())
        return {"dates": pd.DatetimeIndex(z["dates"]), "P": z["P"], "E": z["E"],
                "transform": Affine(*z["transform"]), "valid": z["valid"]}
    e = Era5(grid, era5_cfg)
    days = pd.date_range(start, end, freq="D")
    base = (days - pd.Timedelta(hours=tz_hours)).values.astype("datetime64[ns]")
    hours = np.arange(1, 25) * np.timedelta64(1, "h")
    log.info("ERA5 daily forcing %s..%s: %d days, %d hourly reads for rain and %d for evaporation",
             start.date(), end.date(), len(days), len(days) * 24, len(days) * len(hours[::stride]))
    hp = e.read("total_precipitation", (base[:, None] + hours[None, :]).ravel(), f"rain {start.year}")
    hp = hp.reshape((len(days), 24) + e.shape)
    he = e.read("evaporation", (base[:, None] + hours[None, ::stride]).ravel(), f"evaporation {start.year}")
    he = he.reshape((len(days), -1) + e.shape)
    P = np.where(np.isnan(hp).any(axis=1), np.nan, np.clip(hp.sum(axis=1) * 1000.0, 0, None))     # m -> mm
    E = np.where(np.isnan(he).any(axis=1), np.nan, -he.mean(axis=1) * 24 * 1000.0)
    P, E = e.oriented(P).astype("float32"), e.oriented(E).astype("float32")
    valid = np.isfinite(P).all(axis=(1, 2)) & np.isfinite(E).all(axis=(1, 2))
    out = {"dates": pd.DatetimeIndex(days), "P": P, "E": E, "transform": e.transform, "valid": valid}
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, dates=days.values.astype("datetime64[ns]"), P=P, E=E,
                        transform=np.array(tuple(e.transform)[:6]), valid=valid)
    log.info("ERA5 daily forcing: mean rain %.1f mm/day, evaporation %.1f mm/day, %d of %d days valid",
             float(np.nanmean(P)), float(np.nanmean(E)), int(valid.sum()), len(days))
    return out


def last_valid(f: dict):
    """Last local day on which all cells have data, or None."""
    v = np.flatnonzero(f["valid"])
    return None if not len(v) else f["dates"][v[-1]]


def cell_of(lon, lat, transform: Affine, shape: tuple) -> tuple[np.ndarray, np.ndarray]:
    """Row and column of the ERA5 cells that hold the points (clipped to the window)."""
    col = np.floor((np.asarray(lon) - transform.c) / transform.a).astype(int)
    row = np.floor((np.asarray(lat) - transform.f) / transform.e).astype(int)
    return np.clip(row, 0, shape[0] - 1), np.clip(col, 0, shape[1] - 1)


def unit_weights(label: np.ndarray, n_units: int, grid: Grid, transform: Affine, shape: tuple) -> np.ndarray:
    """(n_units, ncells): share of each sub-KHG's pixels in each ERA5 cell (rows sum to 1 over covered pixels)."""
    ids = np.arange(shape[0] * shape[1], dtype="float32").reshape(shape)
    px = array_to_grid(ids, transform, "EPSG:4326", grid, Resampling.nearest)
    ok = (label >= 0) & np.isfinite(px)
    ncell = shape[0] * shape[1]
    cnt = np.bincount((label[ok].astype(np.int64) * ncell + px[ok].astype(np.int64)), minlength=n_units * ncell)
    w = cnt.reshape(n_units, ncell).astype("float64")
    s = w.sum(axis=1, keepdims=True)
    return np.divide(w, s, out=np.zeros_like(w), where=s > 0)


# ---------------------------------------------------------------------------------------------------------------
# Finer rain and evaporation than ERA5 (a test of the retired repeat-neraca bucket experiments; the neraca balance now reads CHIRPS and WaPOR through repeat_common.fetch_gee_balance)
#   chirps  CHIRPS v3 daily, 0.05 degrees (about 5 km), mm/day, read window by window from data.chc.ucsb.edu (UTC days)
#   wapor   FAO WaPOR v3 level 1 actual evapotranspiration and interception, dekadal, 0.0029 degrees (about 300 m), mm/day,
#           read from the public bucket; every day of a dekad gets the value of that dekad
# Both are brought to a 0.01 degree grid (CHIRPS by nearest cell, WaPOR by area average, ERA5 by nearest cell) so rain and
# evaporation can be mixed.
# ---------------------------------------------------------------------------------------------------------------
CHIRPS_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/{y}/chirps-v3.0.sat.{y}.{m:02d}.{d:02d}.tif"
WAPOR_URL = ("https://storage.googleapis.com/fao-gismgr-wapor-3-data/DATA/WAPOR-3/MAPSET/L1-AETI-D/"
             "WAPOR-3.L1-AETI-D.{y}-{m:02d}-D{k}.tif")
FINE_RES = 0.01


def _fine_grid(grid: Grid, pad: float = 0.05):
    """Affine transform and (rows, columns) of a 0.01 degree grid covering the analysis grid plus a margin."""
    w, s, e, n = grid.bounds_ll()
    lon0, lon1 = np.floor((w - pad) / FINE_RES) * FINE_RES, np.ceil((e + pad) / FINE_RES) * FINE_RES
    lat0, lat1 = np.floor((s - pad) / FINE_RES) * FINE_RES, np.ceil((n + pad) / FINE_RES) * FINE_RES
    return Affine(FINE_RES, 0, lon0, 0, -FINE_RES, lat1), (int(round((lat1 - lat0) / FINE_RES)), int(round((lon1 - lon0) / FINE_RES)))


def _read_window(url: str, transform: Affine, shape: tuple, resampling, nodata_below=None, scale=1.0, nodata=None):
    """One remote GeoTIFF read in a window and reprojected onto the fine grid; NaN where there is no data."""
    import rasterio
    from rasterio.warp import reproject
    from rasterio.windows import from_bounds
    out = np.full(shape, np.nan, "float32")
    with rasterio.open("/vsicurl/" + url) as src:
        left, top = transform.c, transform.f
        right, bottom = left + shape[1] * transform.a, top + shape[0] * transform.e
        pad = 4 * abs(src.res[0])
        win = from_bounds(left - pad, bottom - pad, right + pad, top + pad, src.transform).round_offsets().round_lengths()
        a = src.read(1, window=win).astype("float32")
        t = src.window_transform(win)
        nod = src.nodata if nodata is None else nodata
        if nod is not None:
            a[a == nod] = np.nan
        if nodata_below is not None:
            a[a < nodata_below] = np.nan
        reproject(a * scale, out, src_transform=t, src_crs="EPSG:4326", dst_transform=transform, dst_crs="EPSG:4326",
                  resampling=resampling, src_nodata=np.nan, dst_nodata=np.nan)
    return out


def _fill_gaps(a: np.ndarray, min_share: float = 0.3) -> np.ndarray:
    """Cells without data (sea, cloud) take the value of the nearest cell with data; a field with too few cells stays NaN."""
    from scipy import ndimage
    ok = np.isfinite(a)
    if ok.all() or ok.mean() < min_share:
        return a if ok.all() else np.full(a.shape, np.nan, "float32")
    idx = ndimage.distance_transform_edt(~ok, return_distances=False, return_indices=True)
    return a[tuple(idx)].astype("float32")


def _parallel(fn, items, workers=8):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))


def chirps_daily(days: pd.DatetimeIndex, transform: Affine, shape: tuple, cache_dir: Path | None = None, workers: int = 2) -> np.ndarray:
    """CHIRPS v3 rain in mm/day for each day; NaN for a day that is not published yet (404).
    The server refuses clients that read many files at once (HTTP 403, for hours), so this reads two at a time, keeps every day on disk
    (an interrupted run resumes) and backs off on a refusal. If the server keeps refusing it stops with an error, never with NaN."""
    import time
    tag = f"{transform.c:.2f}_{transform.f:.2f}_{shape[0]}x{shape[1]}"
    folder = None if cache_dir is None else Path(cache_dir) / "chirps_days"
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)

    def one(d):
        f = None if folder is None else folder / f"{d:%Y%m%d}_{tag}.npy"
        if f is not None and f.exists():
            return np.load(f)
        url = CHIRPS_URL.format(y=d.year, m=d.month, d=d.day)
        for wait in (0, 5, 15, 45, 120):
            time.sleep(wait)
            try:
                a = _fill_gaps(_read_window(url, transform, shape, Resampling.nearest, nodata_below=0.0))
                break
            except Exception as ex:
                if "404" in str(ex):
                    a = np.full(shape, np.nan, "float32")                 # not published (yet)
                    break
                last = ex
        else:
            raise RuntimeError(f"CHIRPS refused {url}: {last}. The server blocks heavy clients for a while; wait and rerun (days already read are kept).")
        if f is not None:
            np.save(f, a)
        return a
    log.info("CHIRPS v3: reading %d days (%d at a time)", len(days), workers)
    return np.stack(_parallel(one, list(days), workers))


def wapor_daily(days: pd.DatetimeIndex, transform: Affine, shape: tuple, workers: int = 6) -> np.ndarray:
    """WaPOR v3 level 1 evapotranspiration in mm/day, each day taking the value of its dekad."""
    def dekad(d):
        return d.year, d.month, min((d.day - 1) // 10 + 1, 3)
    keys = sorted({dekad(d) for d in days})

    def one(k):
        try:
            return _fill_gaps(_read_window(WAPOR_URL.format(y=k[0], m=k[1], k=k[2]), transform, shape, Resampling.average, scale=0.1, nodata=-9999.0))
        except Exception as ex:
            log.debug("WaPOR %s: %s", k, ex)
            return np.full(shape, np.nan, "float32")
    log.info("WaPOR v3: reading %d dekads (%d at a time)", len(keys), workers)
    got = dict(zip(keys, _parallel(one, keys, workers)))
    return np.stack([got[dekad(d)] for d in days])


def forcing_for(grid: Grid, era5_cfg: dict, start, end, cache_dir: Path, tz_hours: int, stride: int,
                rain: str = "era5", evap: str = "era5") -> dict:
    """The daily forcing with rain from ERA5 (default) or CHIRPS and evaporation from ERA5 (default) or WaPOR."""
    if rain == "era5" and evap == "era5":
        return daily_forcing(grid, era5_cfg, start, end, cache_dir, tz_hours, stride)
    if rain not in ("era5", "chirps") or evap not in ("era5", "wapor"):
        raise ValueError(f"forcing: rain must be era5 or chirps, evap era5 or wapor (got {rain}, {evap})")
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    cache_dir = Path(cache_dir)
    path = cache_dir / f"forcing_{rain}_{evap}_{start:%Y%m%d}_{end:%Y%m%d}_tz{tz_hours}.npz"
    if path.exists():
        z = np.load(path)
        log.info("forcing %s + %s %s..%s from cache", rain, evap, start.date(), end.date())
        return {"dates": pd.DatetimeIndex(z["dates"]), "P": z["P"], "E": z["E"], "transform": Affine(*z["transform"]), "valid": z["valid"]}
    days = pd.date_range(start, end, freq="D")
    tr, shape = _fine_grid(grid)
    base = None
    if rain == "era5" or evap == "era5":
        base = daily_forcing(grid, era5_cfg, start, end, cache_dir, tz_hours, stride)
        r, c = np.indices(shape)
        lon, lat = tr.c + (c + 0.5) * tr.a, tr.f + (r + 0.5) * tr.e
        rr, cc = cell_of(lon.ravel(), lat.ravel(), base["transform"], base["P"].shape[1:])
    P = chirps_daily(days, tr, shape, cache_dir) if rain == "chirps" else base["P"][:, rr, cc].reshape((len(days),) + shape)
    E = wapor_daily(days, tr, shape) if evap == "wapor" else base["E"][:, rr, cc].reshape((len(days),) + shape)
    valid = np.isfinite(P).all(axis=(1, 2)) & np.isfinite(E).all(axis=(1, 2))
    out = {"dates": days, "P": P.astype("float32"), "E": E.astype("float32"), "transform": tr, "valid": valid}
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, dates=days.values.astype("datetime64[ns]"), P=out["P"], E=out["E"], transform=np.array(tuple(tr)[:6]), valid=valid)
    log.info("forcing %s + %s: mean rain %.1f mm/day, evaporation %.1f mm/day, %d of %d days valid", rain, evap,
             float(np.nanmean(P)), float(np.nanmean(E)), int(valid.sum()), len(days))
    return out


def khg_mean(f: dict, label: np.ndarray, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """KHG-mean daily P and E (mm/day) from a forcing dict, weighting its cells by the share of the KHG they cover.
    `label` is the raster of the KHG on `grid` (0 inside, -1 outside, as `grid.label_raster` gives for one polygon)."""
    w = unit_weights(label, 1, grid, f["transform"], f["P"].shape[1:])[0]
    T = len(f["dates"])
    return f["P"].reshape(T, -1).astype("float64") @ w, f["E"].reshape(T, -1).astype("float64") @ w


def khg_table(grid: Grid, label: np.ndarray, era5_cfg: dict, start, end, cache_dir: Path, tz_hours: int = 7, stride: int = 3,
              window: str = "window") -> pd.DataFrame:
    """The KHG-mean daily forcing of one window as a table: date, window, P_era5_mm, E_era5_mm, E_wapor_mm.

    ERA5 rain and evaporation come from `daily_forcing` (read from the cache folder if it is there, otherwise from the cloud, about
    11 minutes per window). E_wapor_mm is filled only where the ERA5 + WaPOR forcing of the same window is already cached. A day
    where rain or evaporation is missing has both empty. Values are rounded to 0.001 mm, the precision of forcing_daily.csv.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    f = daily_forcing(grid, era5_cfg, start, end, cache_dir, tz_hours, stride)
    P, E = khg_mean(f, label, grid)
    df = pd.DataFrame({"date": f["dates"], "window": window, "P_era5_mm": P, "E_era5_mm": E, "E_wapor_mm": np.nan})
    if (Path(cache_dir) / f"forcing_era5_wapor_{start:%Y%m%d}_{end:%Y%m%d}_tz{tz_hours}.npz").exists():
        fw = forcing_for(grid, era5_cfg, start, end, cache_dir, tz_hours, stride, rain="era5", evap="wapor")
        df["E_wapor_mm"] = khg_mean(fw, label, grid)[1]
    df.loc[df[["P_era5_mm", "E_era5_mm"]].isna().any(axis=1), ["P_era5_mm", "E_era5_mm"]] = np.nan
    return df.round({"P_era5_mm": 3, "E_era5_mm": 3, "E_wapor_mm": 3})
