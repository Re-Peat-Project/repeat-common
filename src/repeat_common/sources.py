"""
Data layers for prioritisation, written to a per-KHG cache on the analysis grid.

cache/<khg_id>/
    annual/ndmi_<year>.tif       dry-season median NDMI (Sentinel-2 L2A)
    annual/ndvi_<year>.tif       dry-season median NDVI (same scenes; B8A and B04)
    annual/burned_<year>.tif     1 = burned in that year (MODIS MCD64A1, or local dNBR maps)
    annual/deficit_<year>.tif    dry-season climatic water deficit, mm (TerraClimate 'def')
    annual/dryspell_<year>.tif   longest run of dry days in the dry months (ERA5 hourly, or CHIRPS daily)
    annual/spell14_<year>.tif    number of dry spells >= N days (same)
    annual/drymean_<year>.tif    mean length of the dry runs, days (same)
    static/worldcover.tif        ESA WorldCover classes
    static/water_season.tif      months per year with surface water (JRC GSW seasonality)
    hotspots.csv                 VIIRS active fires inside the KHG (FIRMS)

Each layer can come from the cloud (default) or from local files (`local` key with an optional
{year} placeholder). Cloud access uses STAC (Microsoft Planetary Computer by default) and direct
COG / Zarr reads, so only the KHG window is transferred. Existing cache files are never refetched.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.windows import from_bounds

from .grid import Grid, array_to_grid, warp_to_grid, write

log = logging.getLogger("repeat_common.sources")

SOURCE_DEFAULTS = {
    "stac_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
    "planetary_computer": True,            # sign asset URLs with the planetary-computer package
    "ndmi": {"enabled": True, "local": None, "collection": "sentinel-2-l2a",
             "bands": {"nir": "B8A", "swir": "B11", "scl": "SCL", "red": "B04"}, "res_m": 20,
             "ndvi": True, "ndvi_local": None,       # NDVI from the same scenes (red band added); ndvi_local: file template with {year}
             "max_cloud": 60, "scl_keep": [4, 5, 6, 7], "apply_boa_offset": True},
    "burned": {"enabled": True, "local": None, "collection": "modis-64A1-061", "asset": "Burn_Date"},
    "deficit": {"enabled": True, "local": None, "collection": "terraclimate", "variable": "def",
                "fallback": "era5",                            # years TerraClimate lacks (it ends 2021) come from ERA5; None = leave empty
                "monthly_variables": ["ppt", "aet"],           # for the water balance per unit
                "monthly_local": None},                         # e.g. "data/clim/{var}_{year}.tif" (12 bands)
    # Rainfall for the dry-spell indicators. ERA5 is the default (CHIRPS COGs on data.chc.ucsb.edu returned 403 to a
    # full-year fetch). If both are enabled, ERA5 is used. See docs/data_sources.md for the caveats of ERA5 rainfall.
    "era5": {"enabled": True, "local": None,
             "url": "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3",
             "variable": "total_precipitation", "months": None,    # months: None = the dry_months of the config
             "dry_day_mm": 1.0, "spell_days": 14, "workers": 8, "retries": 6,
             "evap_stride": 3},     # water-balance fallback: evaporation sampled every N hours
    "chirps": {"enabled": False, "local": None,
               "url": ("https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/cogs/p05/"
                       "{year}/chirps-v2.0.{year}.{month:02d}.{day:02d}.cog"),
               "dry_day_mm": 1.0, "spell_days": 14, "workers": 8},
    "worldcover": {"enabled": True, "local": None, "collection": "esa-worldcover", "asset": "map"},
    "water": {"enabled": True, "local": None, "collection": "jrc-gsw", "asset": "seasonality"},
    "firms": {"enabled": True, "local": None,
              "url": ("https://firms.modaps.eosdis.nasa.gov/data/country/viirs-snpp/{year}/"
                      "viirs-snpp_{year}_Indonesia.csv"),
              "confidence": ["n", "h"], "exclude_types": [2, 3]},
}


# --------------------------------------------------------------------------------------
# STAC helpers
# --------------------------------------------------------------------------------------
def _client(scfg):
    import pystac_client
    if scfg["planetary_computer"]:
        import planetary_computer
        return pystac_client.Client.open(scfg["stac_url"], modifier=planetary_computer.sign_inplace)
    return pystac_client.Client.open(scfg["stac_url"])


def _geobox(grid: Grid):
    from odc.geo.geobox import GeoBox
    return GeoBox(grid.shape, grid.transform, grid.crs.to_string())


def _search(scfg, collection, grid: Grid, datetime=None, query=None):
    items = list(_client(scfg).search(collections=[collection], bbox=grid.bounds_ll(),
                                      datetime=datetime, query=query).items())
    log.info("  %s %s: %d items", collection, datetime or "", len(items))
    return items


def _gdal_env():
    return rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.cog",
                        GDAL_HTTP_MAX_RETRY="4", GDAL_HTTP_RETRY_DELAY="2", VSI_CACHE="TRUE")


# --------------------------------------------------------------------------------------
# Layer fetchers (cloud)
# --------------------------------------------------------------------------------------
def ndmi_cloud(year, months, grid: Grid, scfg) -> tuple[np.ndarray, np.ndarray | None]:
    """Dry-season median NDMI and (unless sources.ndmi.ndvi is false) NDVI from the same Sentinel-2 scenes."""
    import odc.stac
    c = scfg["ndmi"]
    b = c["bands"]
    want_v = bool(c.get("ndvi", True)) and bool(b.get("red"))
    m0, m1 = min(months), max(months)
    last = pd.Timestamp(year=year, month=m1, day=1) + pd.offsets.MonthEnd(0)
    dt = f"{year}-{m0:02d}-01/{last.date()}"
    items = _search(scfg, c["collection"], grid, dt, {"eo:cloud_cover": {"lt": c["max_cloud"]}})
    if not items:
        raise RuntimeError("no Sentinel-2 items")
    gb = _geobox(grid)
    if c["res_m"] and c["res_m"] > grid.res:   # load coarser for speed, then resample to grid
        from odc.geo.geobox import GeoBox
        gb = GeoBox.from_bbox(grid.bounds, crs=grid.crs.to_string(), resolution=c["res_m"])
    ds = odc.stac.load(items, bands=[b["nir"], b["swir"], b["scl"]] + ([b["red"]] if want_v else []), geobox=gb, groupby="solar_day",
                       chunks={"x": 2048, "y": 2048}, resampling={"*": "bilinear", b["scl"]: "nearest"})
    # processing baseline >= 04.00 adds a +1000 offset to L2A reflectance
    off = {}
    for it in items:
        day = pd.Timestamp(it.datetime).strftime("%Y-%m-%d")
        pb = it.properties.get("s2:processing_baseline")
        o = 1000.0 if (c["apply_boa_offset"] and pb and float(pb) >= 4.0) else 0.0
        off[day] = max(off.get(day, 0.0), o)
    offs = np.array([off.get(pd.Timestamp(t).strftime("%Y-%m-%d"), 0.0) for t in ds.time.values], "float32")
    import xarray as xr
    o = xr.DataArray(offs, dims="time", coords={"time": ds.time})
    nir = ds[b["nir"]].astype("float32") - o
    swir = ds[b["swir"]].astype("float32") - o
    ok = ds[b["scl"]].isin(c["scl_keep"]) & (nir > 0) & (swir > 0)
    ndmi = ((nir - swir) / (nir + swir)).where(ok)
    med = ndmi.median("time", skipna=True).compute().values.astype("float32")
    vmed = None
    if want_v:
        red = ds[b["red"]].astype("float32") - o
        okv = ds[b["scl"]].isin(c["scl_keep"]) & (nir > 0) & (red > 0)
        ndvi = ((nir - red) / (nir + red)).where(okv)
        vmed = ndvi.median("time", skipna=True).compute().values.astype("float32")
    if med.shape != grid.shape:
        med = array_to_grid(med, gb.affine, grid.crs, grid, Resampling.bilinear)
        if vmed is not None:
            vmed = array_to_grid(vmed, gb.affine, grid.crs, grid, Resampling.bilinear)
    return med, vmed


def burned_cloud(year, grid: Grid, scfg) -> np.ndarray:
    import odc.stac
    c = scfg["burned"]
    items = _search(scfg, c["collection"], grid, f"{year}-01-01/{year}-12-31")
    if not items:
        raise RuntimeError("no burned-area items")
    ds = odc.stac.load(items, bands=[c["asset"]], geobox=_geobox(grid), resampling="nearest",
                       chunks={"x": 2048, "y": 2048})
    bd = ds[c["asset"]]
    return (bd > 0).any("time").compute().values.astype("float32")


def static_cloud(key, grid: Grid, scfg) -> np.ndarray:
    import odc.stac
    c = scfg[key]
    items = _search(scfg, c["collection"], grid)
    if not items:
        raise RuntimeError(f"no {key} items")
    items = sorted(items, key=lambda i: str(i.datetime or i.properties.get("start_datetime")))
    ds = odc.stac.load(items, bands=[c["asset"]], geobox=_geobox(grid), resampling="nearest",
                       groupby="id", chunks={"x": 2048, "y": 2048})
    arr = ds[c["asset"]].isel(time=-1) if "time" in ds.dims else ds[c["asset"]]
    return arr.compute().values.astype("float32")


def deficit_cloud(years, months, grid: Grid, scfg, cache: Path | None = None) -> dict:
    import planetary_computer
    import xarray as xr
    c = scfg["deficit"]
    col = _client(scfg).get_collection(c["collection"])
    asset = planetary_computer.sign(col.assets["zarr-abfs"]) if scfg["planetary_computer"] else col.assets["zarr-abfs"]
    kw = dict(asset.extra_fields.get("xarray:open_kwargs", {}))     # holds storage_options for the abfs asset
    if "storage_options" not in kw and asset.extra_fields.get("xarray:storage_options"):
        kw["storage_options"] = asset.extra_fields["xarray:storage_options"]
    ds = xr.open_dataset(asset.href, **kw)
    last_month = str(ds["time"].values[-1])[:7]
    lon0, lat0, lon1, lat1 = grid.bounds_ll()
    pad = 0.1
    lat = ds["lat"].values
    lat_slice = slice(lat1 + pad, lat0 - pad) if lat[0] > lat[-1] else slice(lat0 - pad, lat1 + pad)
    sub = ds[[c["variable"]] + list(c.get("monthly_variables") or [])].sel(
        lat=lat_slice, lon=slice(lon0 - pad, lon1 + pad),
        time=slice(f"{min(years)}-01-01", f"{max(years)}-12-31")).load().sortby("lat", ascending=False)
    da = sub[c["variable"]]
    dlon = float(abs(da.lon[1] - da.lon[0]))
    dlat = float(abs(da.lat[1] - da.lat[0]))
    tr = from_origin(float(da.lon.min()) - dlon / 2, float(da.lat.max()) + dlat / 2, dlon, dlat)
    out = {}
    for y in years:
        sel = da.sel(time=da.time.dt.year == y)
        sel = sel.sel(time=sel.time.dt.month.isin(months))
        if sel.sizes.get("time", 0) == 0:
            log.warning("  deficit %s: not in TerraClimate (the dataset ends %s); its indicators use the other years",
                        y, last_month)
            continue
        out[y] = array_to_grid(sel.sum("time").values, tr, "EPSG:4326", grid, Resampling.bilinear)
        if cache is not None:
            for v in c.get("monthly_variables") or []:
                m = sub[v].sel(time=sub.time.dt.year == y)
                if m.sizes.get("time", 0) == 12:
                    write_native(cache / "climate" / f"{v}_{y}.tif", m.values.astype("float32"), tr)
    return out


def write_native(path: Path, stack: np.ndarray, transform, crs="EPSG:4326"):
    """Multi-band GeoTIFF on its own (coarse) grid, e.g. 12 monthly climate bands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=stack.shape[2], height=stack.shape[1],
                       count=stack.shape[0], dtype="float32", crs=crs, transform=transform,
                       nodata=np.nan, compress="deflate") as dst:
        dst.write(stack)


def chirps_cloud(year, grid: Grid, scfg) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c = scfg["chirps"]
    lon0, lat0, lon1, lat1 = grid.bounds_ll()
    pad = 0.1
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")

    def read_day(d):
        url = "/vsicurl/" + c["url"].format(year=d.year, month=d.month, day=d.day)
        with rasterio.open(url) as src:
            w = from_bounds(lon0 - pad, lat0 - pad, lon1 + pad, lat1 + pad, src.transform).round_offsets().round_lengths()
            a = src.read(1, window=w).astype("float32")
            if src.nodata is not None:
                a[a == src.nodata] = np.nan
            return a, src.window_transform(w)

    with _gdal_env(), ThreadPoolExecutor(int(c["workers"])) as ex:
        res = list(ex.map(read_day, days))
    stack = np.stack([r[0] for r in res])
    tr = res[0][1]
    return _dry_spells(stack, float(c["dry_day_mm"]), int(c["spell_days"]), tr, grid)


_ERA5 = {}


def _retry(fn, tries: int, what: str):
    """Call fn(); on an error (e.g. HTTP 503 under load) wait 1, 2, 4 ... s and try again."""
    import time
    for k in range(tries):
        try:
            return fn()
        except Exception as e:
            if k == tries - 1:
                raise RuntimeError(f"{what}: {type(e).__name__}: {str(e)[:120]} (after {tries} tries)") from e
            time.sleep(min(30, 2 ** k))


class Era5:
    """ARCO-ERA5 hourly store (Google, public Zarr, 0.25 degrees) cut to the window around one KHG.

    The store holds one hour of the whole globe per chunk, so reading an hour costs ~0.3 s whatever the window.
    Hourly accumulations (precipitation, evaporation) at time t cover the hour ending at t; ERA5 counts
    evaporation as negative.
    """

    def __init__(self, grid: Grid, c: dict, pad: float = 0.25):
        import xarray as xr
        if c["url"] not in _ERA5:
            _ERA5[c["url"]] = xr.open_zarr(c["url"], chunks=None, consolidated=None)
        self.ds = _ERA5[c["url"]]
        self.c = c
        lat, lon = self.ds["latitude"].values, self.ds["longitude"].values % 360
        self.tt = self.ds["time"].values
        lon0, lat0, lon1, lat1 = grid.bounds_ll()
        ii = np.flatnonzero((lat >= lat0 - pad) & (lat <= lat1 + pad))
        jj = np.flatnonzero((lon >= (lon0 - pad) % 360) & (lon <= (lon1 + pad) % 360))
        if not len(ii) or not len(jj):
            raise RuntimeError("ERA5: no cells inside the KHG window")
        self.si = slice(int(ii[0]), int(ii[-1]) + 1)
        self.sj = slice(int(jj[0]), int(jj[-1]) + 1)
        d = float(abs(lat[1] - lat[0]))
        self.flip = bool(lat[self.si][0] < lat[self.si][-1])          # ERA5 latitude runs north to south
        self.transform = from_origin(float(lon[self.sj].min()) - d / 2, float(lat[self.si].max()) + d / 2, d, d)
        self.shape = (self.si.stop - self.si.start, self.sj.stop - self.sj.start)

    def read(self, var: str, hours, what: str) -> np.ndarray:
        """Hourly fields (n, ny, nx) in the store's units; NaN for hours the store does not have."""
        hours = np.asarray(hours, dtype="datetime64[ns]")
        idx = np.searchsorted(self.tt, hours)
        ok = (idx < len(self.tt)) & (self.tt[np.minimum(idx, len(self.tt) - 1)] == hours)
        out = np.full((len(hours),) + self.shape, np.nan, "float32")
        da = self.ds[var]
        retries = int(self.c.get("retries", 6))

        def task(b):
            b = b[ok[b]]
            if not len(b):
                return b, None
            a = _retry(lambda: da.isel(time=idx[b], latitude=self.si, longitude=self.sj).values.astype("float32"),
                       retries, f"ERA5 {what}")
            return b, a

        blocks = [np.arange(k, min(k + 24, len(hours))) for k in range(0, len(hours), 24)]
        with ThreadPoolExecutor(int(self.c["workers"])) as ex:
            for b, a in ex.map(task, blocks):
                if a is not None:
                    out[b] = a
        return out

    def oriented(self, a: np.ndarray) -> np.ndarray:
        return a[..., ::-1, :] if self.flip else a


def _month_hours(year: int, month: int) -> np.ndarray:
    """The hourly values of one UTC month: 01:00 on the 1st to 00:00 on the 1st of the next month."""
    first = pd.Timestamp(year=year, month=month, day=1)
    nxt = first + pd.offsets.MonthBegin(1)
    return pd.date_range(first + pd.Timedelta(hours=1), nxt, freq="h").values


def era5_cloud(year, months, grid: Grid, scfg) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dry-spell layers from ERA5 hourly precipitation.

    Only the months asked for are read (default: the dry months). Days are UTC days: the hourly values at
    01:00 .. 24:00 UTC. Indonesia's local day starts 7 hours earlier (WIB).
    """
    c = scfg["era5"]
    e = Era5(grid, c)
    days = []
    for m in sorted(months):
        first = pd.Timestamp(year=year, month=m, day=1)
        days += list(pd.date_range(first, first + pd.offsets.MonthEnd(0), freq="D"))
    hours = np.concatenate([(d + pd.Timedelta(hours=1) + pd.to_timedelta(np.arange(24), unit="h")).values for d in days])
    h = e.read(c["variable"], hours, f"precipitation {year}").reshape((len(days), 24) + e.shape)
    tot = np.where(np.isnan(h).any(axis=1), np.nan, np.clip(h.sum(axis=1) * 1000.0, 0, None))     # m -> mm per day
    stack, prev = [], None
    for d, t in zip(days, tot):
        if prev is not None and (d - prev).days > 1:                    # months not adjacent: break dry spells
            stack.append(np.full_like(t, np.nan))
        stack.append(t)
        prev = d
    stack = e.oriented(np.stack(stack))
    if not np.isfinite(stack).any():
        raise RuntimeError(f"ERA5 has no precipitation for {year}")
    log.info("  ERA5 %s: %d days, %dx%d cells, mean %.1f mm/day", year, len(days), stack.shape[1], stack.shape[2],
             float(np.nanmean(stack)))
    return _dry_spells(stack, float(c["dry_day_mm"]), int(c["spell_days"]), e.transform, grid)


def era5_water_balance(year, dry_months, grid: Grid, scfg, months=range(1, 13)) -> dict:
    """Monthly rain (ppt), actual evaporation (aet) and the dry-season deficit from ERA5, to fill years that
    TerraClimate does not cover. Same names and units as the TerraClimate layers (mm).

    ppt and aet are monthly totals. deficit = sum over dry_months of max(PET - AET, 0).
    Evaporation fields are smooth through the day, so they are sampled every `evap_stride` hours (default 3) to
    cut the reads; precipitation is intermittent and read every hour.
    ERA5 PET and AET are not TerraClimate's (different models), so the two sources are not interchangeable;
    see docs/data_sources.md.
    """
    c = scfg["era5"]
    stride = max(1, int(c.get("evap_stride", 3)))
    e = Era5(grid, c)
    months = sorted(months)
    ppt, aet, deficit = [], [], np.zeros(e.shape, "float32")
    for m in months:
        hrs = _month_hours(year, m)
        p = e.read("total_precipitation", hrs, f"rain {year}-{m:02d}")
        ev = e.read("evaporation", hrs[::stride], f"evaporation {year}-{m:02d}")
        ppt.append(np.nansum(p, axis=0) * 1000.0)
        a = -np.nanmean(ev, axis=0) * len(hrs) * 1000.0
        aet.append(a)
        if m in dry_months:
            pev = e.read("potential_evaporation", hrs[::stride], f"potential evaporation {year}-{m:02d}")
            deficit += np.clip(-np.nanmean(pev, axis=0) * len(hrs) * 1000.0 - a, 0, None)
        log.info("  ERA5 %s-%02d: rain %.0f mm, evaporation %.0f mm", year, m, float(np.mean(ppt[-1])), float(np.mean(a)))
    return {"months": months, "ppt": e.oriented(np.stack(ppt)), "aet": e.oriented(np.stack(aet)),
            "deficit": e.oriented(deficit), "transform": e.transform}


def _dry_spells(stack, thr, spell_days, tr, grid):
    """Longest dry run, number of runs reaching spell_days, and mean run length (all in days), on the grid."""
    dry = stack < thr
    run = np.zeros(stack.shape[1:], "int32")
    best = np.zeros_like(run)
    count = np.zeros_like(run)
    total = np.zeros(run.shape, "float32")       # summed length of the finished runs
    nruns = np.zeros_like(run)
    for d in dry:
        prev = run
        run = np.where(d, run + 1, 0)
        ended = (prev > 0) & (run == 0)
        total += np.where(ended, prev, 0)
        nruns += ended.astype("int32")
        best = np.maximum(best, run)
        count += (run == spell_days).astype("int32")       # count each spell once, when it reaches N days
    total += np.where(run > 0, run, 0)                      # a run still open at the end
    nruns += (run > 0).astype("int32")
    mean = total / np.maximum(nruns, 1)
    valid = np.isfinite(stack).any(axis=0)
    out = [np.where(valid, a, np.nan).astype("float32") for a in (best, count, mean)]
    return tuple(array_to_grid(a, tr, "EPSG:4326", grid, Resampling.bilinear) for a in out)


def firms_points(years, geom_ll, scfg) -> pd.DataFrame:
    c = scfg["firms"]
    lon0, lat0, lon1, lat1 = geom_ll.bounds
    frames = []
    srcs = [c["local"]] if c.get("local") else [c["url"].format(year=y) for y in years]
    for src in srcs:
        try:
            for chunk in pd.read_csv(src, chunksize=500_000, low_memory=False):
                ch = chunk[(chunk.longitude >= lon0) & (chunk.longitude <= lon1) &
                           (chunk.latitude >= lat0) & (chunk.latitude <= lat1)]
                if len(ch):
                    frames.append(ch)
            log.info("  FIRMS read %s", src)
        except Exception as e:
            log.error("  FIRMS %s: %s", src, e)
    if not frames:
        return pd.DataFrame(columns=["longitude", "latitude", "acq_date", "confidence"])
    df = pd.concat(frames, ignore_index=True)
    if "confidence" in df and c["confidence"]:
        df = df[df["confidence"].astype(str).str.lower().isin([x.lower() for x in c["confidence"]])]
    if "type" in df and c["exclude_types"]:
        df = df[~df["type"].isin(c["exclude_types"])]
    import geopandas as gpd
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs=4326)
    g = g[g.within(geom_ll)]
    g["year"] = pd.to_datetime(g["acq_date"]).dt.year
    return pd.DataFrame(g.drop(columns="geometry"))


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def _local_or_cloud(name, path: Path, grid: Grid, local_tmpl, year, cloud_fn, resampling):
    if path.exists():
        return
    try:
        if local_tmpl:
            src = local_tmpl.format(year=year) if year is not None else local_tmpl
            arr = warp_to_grid(src, grid, resampling)
            log.info("  %s %s from local %s", name, year or "", src)
        else:
            arr = cloud_fn()
            log.info("  %s %s from cloud", name, year or "")
        write(path, arr, grid)
    except Exception as e:
        log.error("  %s %s unavailable: %s", name, year or "", e)


def build_cache(cfg: dict, khg_id: str, geom_ll, grid: Grid, cache: Path):
    s = cfg["sources"]
    years = sorted(set(cfg["years"]["all"]))
    ann, sta = cache / "annual", cache / "static"
    log.info("KHG %s: building cache in %s", khg_id, cache)

    if s["ndmi"]["enabled"]:
        want_v = bool(s["ndmi"].get("ndvi", True))
        for y in years:
            pn, pv = ann / f"ndmi_{y}.tif", ann / f"ndvi_{y}.tif"
            if s["ndmi"]["local"]:
                _local_or_cloud("ndmi", pn, grid, s["ndmi"]["local"], y, None, Resampling.bilinear)
                if want_v and s["ndmi"].get("ndvi_local"):
                    _local_or_cloud("ndvi", pv, grid, s["ndmi"]["ndvi_local"], y, None, Resampling.bilinear)
            elif not (pn.exists() and (pv.exists() or not want_v)):
                try:
                    n_, v_ = ndmi_cloud(y, cfg["ndmi_months"], grid, s)
                    if not pn.exists():
                        write(pn, n_, grid)
                    if want_v and v_ is not None and not pv.exists():
                        write(pv, v_, grid)
                    log.info("  ndmi%s %s from cloud", " and ndvi" if want_v else "", y)
                except Exception as e:
                    log.error("  ndmi %s unavailable: %s", y, e)
    if s["burned"]["enabled"]:
        for y in years:
            loc = s["burned"]["local"]
            p = ann / f"burned_{y}.tif"
            _local_or_cloud("burned", p, grid, loc, y, lambda y=y: burned_cloud(y, grid, s), Resampling.nearest)
            if loc and p.exists():   # local maps: any positive value = burned
                from .grid import read
                a = read(p)
                write(p, np.where(np.isfinite(a), (a > 0).astype("float32"), np.nan), grid)
    if s["deficit"]["enabled"]:
        missing = [y for y in years if not (ann / f"deficit_{y}.tif").exists()]
        if missing:
            if s["deficit"]["local"]:
                for y in missing:
                    _local_or_cloud("deficit", ann / f"deficit_{y}.tif", grid, s["deficit"]["local"], y,
                                    None, Resampling.bilinear)
            else:
                try:
                    for y, a in deficit_cloud(missing, cfg["dry_months"], grid, s, cache).items():
                        write(ann / f"deficit_{y}.tif", a, grid)
                        log.info("  deficit %s from cloud", y)
                except Exception as e:
                    log.error("  deficit unavailable: %s", e)
    if s["deficit"].get("fallback") == "era5" and s["era5"]["enabled"] and not s["deficit"]["local"]:
        for y in years:
            if (ann / f"deficit_{y}.tif").exists():
                continue
            try:
                r = era5_water_balance(y, cfg["dry_months"], grid, s)
                write(ann / f"deficit_{y}.tif", array_to_grid(r["deficit"], r["transform"], "EPSG:4326", grid,
                                                              Resampling.bilinear), grid)
                for v in ("ppt", "aet"):
                    if not (cache / "climate" / f"{v}_{y}.tif").exists():
                        write_native(cache / "climate" / f"{v}_{y}.tif", r[v].astype("float32"), r["transform"])
                log.info("  deficit, ppt, aet %s from ERA5 (TerraClimate fallback)", y)
            except Exception as e:
                log.error("  deficit %s unavailable (ERA5 fallback): %s", y, e)
    ml = s["deficit"].get("monthly_local")
    if ml:
        for y in years:
            for v in s["deficit"].get("monthly_variables") or []:
                dst = cache / "climate" / f"{v}_{y}.tif"
                src = ml.format(var=v, year=y)
                if not dst.exists() and Path(src).exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy(src, dst)
    rain = "era5" if s["era5"]["enabled"] else ("chirps" if s["chirps"]["enabled"] else None)
    if rain:
        rc = s[rain]
        for y in years:
            p1, p2, p3 = ann / f"dryspell_{y}.tif", ann / f"spell14_{y}.tif", ann / f"drymean_{y}.tif"
            if p1.exists() and p2.exists() and p3.exists():
                continue
            try:
                if rc["local"]:
                    write(p1, warp_to_grid(rc["local"].format(year=y, var="dryspell"), grid), grid)
                    write(p2, warp_to_grid(rc["local"].format(year=y, var="spell14"), grid), grid)
                    if Path(rc["local"].format(year=y, var="drymean")).exists():
                        write(p3, warp_to_grid(rc["local"].format(year=y, var="drymean"), grid), grid)
                else:
                    months = (rc.get("months") or cfg["dry_months"]) if rain == "era5" else None
                    if rain == "era5":
                        a_, b_, c_ = era5_cloud(y, months, grid, {"era5": {**rc, "months": months}})
                    else:
                        a_, b_, c_ = chirps_cloud(y, grid, s)
                    write(p1, a_, grid)
                    write(p2, b_, grid)
                    write(p3, c_, grid)
                log.info("  dry spells %s from %s", y, rain)
            except Exception as e:
                log.error("  dry spells %s unavailable (%s): %s", y, rain, e)
    for key, fname in (("worldcover", "worldcover.tif"), ("water", "water_season.tif")):
        if s[key]["enabled"]:
            _local_or_cloud(key, sta / fname, grid, s[key]["local"], None,
                            lambda key=key: static_cloud(key, grid, s), Resampling.nearest)
    hp = cache / "hotspots.csv"
    if s["firms"]["enabled"] and not hp.exists():
        df = firms_points(years, geom_ll, s)
        df.to_csv(hp, index=False)
        log.info("  hotspots: %d points", len(df))
