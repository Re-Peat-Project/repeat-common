"""
Water-table (TMA) readings: adapters that give one canonical table, and the per-unit indicators.

Canonical table: station_id, date, depth_m, lon, lat      depth_m > 0 = water table below the surface.

Two inputs, chosen by what `tma.path` points to (or the older `vectors.tma_csv`):
  folder  SiTMAT extract (sitmat-extract: sitmat_out/processed/): daily_long.csv + stations.csv, and
          stations_geo.csv when present. water_level_cm is negative below the surface, so depth_m = -cm / 100.
  file    CSV with station_id, date, wtd_m, lon, lat (sign set by params.tma_depth_positive).

Config (`tma:`):
  path          folder or CSV
  kinds         SiTMAT only: ["concession"] (default) and/or "community" (low coverage, noisy)
  drop_flags    SiTMAT only: readings with these QC flags are dropped (default range, spike)
  min_coverage  keep stations with valid readings on at least this share of the days in the period (0.5)
  period        [start, end] dates. Default: all dates in a SiTMAT folder; the baseline years for a CSV.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("repeat_common.tma")

TMA_DEFAULTS = {"path": None, "kinds": ["concession"], "drop_flags": ["range", "spike"],
                "min_coverage": 0.5, "period": None}

# Indonesia, generous: catches (0, 0), swapped or garbage coordinates
_LON, _LAT = (94.0, 142.0), (-12.0, 7.0)


def _good_location(lon, lat) -> pd.Series:
    lon, lat = pd.to_numeric(lon, errors="coerce"), pd.to_numeric(lat, errors="coerce")
    return (lon.between(*_LON) & lat.between(*_LAT) & ~((lon == 0) & (lat == 0)) & ~np.isclose(lon, lat))


def _sitmat(folder: Path, c: dict) -> pd.DataFrame:
    d = pd.read_csv(folder / "daily_long.csv", usecols=lambda x: x in
                    {"kind", "station_id", "date", "water_level_cm", "flag"}, dtype={"flag": "string"})
    st = pd.read_csv(folder / "stations.csv", usecols=["kind", "station_id", "lon", "lat"])
    n0 = d["station_id"].nunique()
    d = d[d["kind"].isin(c["kinds"]) & d["water_level_cm"].notna()]
    log.info("SiTMAT: %d stations in file, %d with readings of kind %s", n0, d["station_id"].nunique(), c["kinds"])
    bad = d["flag"].isin(c["drop_flags"])
    if bad.any():
        log.info("SiTMAT: %d readings dropped for flags %s", int(bad.sum()), c["drop_flags"])
    d = d[~bad].merge(st, on=["kind", "station_id"], how="left")
    ok = _good_location(d["lon"], d["lat"])
    geo = folder / "stations_geo.csv"
    if geo.exists():
        g = pd.read_csv(geo, usecols=["kind", "station_id", "has_loc"])
        d = d.merge(g, on=["kind", "station_id"], how="left")
        ok &= d["has_loc"].fillna(1).astype(int) == 1
    if (~ok).any():
        log.info("SiTMAT: %d stations dropped for missing or invalid coordinates",
                 int(d.loc[~ok, ["kind", "station_id"]].drop_duplicates().shape[0]))
    d = d[ok]
    return pd.DataFrame({"station_id": d["kind"] + "/" + d["station_id"], "date": pd.to_datetime(d["date"]),
                         "depth_m": -d["water_level_cm"].astype(float) / 100.0,
                         "lon": d["lon"].astype(float), "lat": d["lat"].astype(float)})


def _csv(path: Path, depth_positive) -> pd.DataFrame:
    t = pd.read_csv(path)
    if depth_positive is None:
        log.warning("%s: params.tma_depth_positive is not set, so the sign of wtd_m is unknown and its absolute value is "
                    "used. Flooded readings then count as deep. Set it to true (wtd_m is a depth) or false (negative "
                    "below the surface).", path.name)
        depth = t["wtd_m"].abs()
    else:
        depth = t["wtd_m"] if depth_positive else -t["wtd_m"]
    return pd.DataFrame({"station_id": t["station_id"].astype(str), "date": pd.to_datetime(t["date"]),
                         "depth_m": depth.astype(float), "lon": t["lon"].astype(float), "lat": t["lat"].astype(float)})


def load(cfg: dict, baseline_years) -> pd.DataFrame | None:
    """Canonical table for the configured source, or None when no TMA source is set."""
    c = {**TMA_DEFAULTS, **(cfg.get("tma") or {})}
    path = c["path"] or (cfg.get("vectors") or {}).get("tma_csv")
    if not path:
        return None
    path = Path(path)
    folder = path.is_dir()
    t = _sitmat(path, c) if folder else _csv(path, cfg["params"].get("tma_depth_positive"))
    if c["period"]:
        start, end = pd.Timestamp(c["period"][0]), pd.Timestamp(c["period"][1])
    elif folder:
        start, end = t["date"].min(), t["date"].max()
    else:
        t = t[t["date"].dt.year.isin(list(baseline_years))]
        start, end = (t["date"].min(), t["date"].max()) if len(t) else (None, None)
    if len(t) == 0 or start is None:
        log.warning("TMA: no readings in the period")
        return t
    t = t[(t["date"] >= start) & (t["date"] <= end)].drop_duplicates(["station_id", "date"])
    days = (end - start).days + 1
    cov = t.groupby("station_id")["date"].nunique() / days
    keep = cov[cov >= float(c["min_coverage"])].index
    log.info("TMA: %s to %s (%d days); %d of %d stations have >= %.0f%% coverage", start.date(), end.date(), days,
             len(keep), len(cov), 100 * float(c["min_coverage"]))
    return t[t["station_id"].isin(keep)]


def per_unit(t: pd.DataFrame | None, up, threshold_m: float, max_gap_days: int) -> pd.DataFrame:
    """u_tma, u_tma_recession and n_tma_stations per unit. up: projected units with a unit_id column.

    u_tma = share of days with the water table deeper than threshold_m, averaged over the stations in the unit.
    u_tma_recession = mean deepening rate (mm/day) between readings at most max_gap_days apart, rises ignored.
    Units without a station get NaN. n_tma_stations is 0 there, or NaN when no TMA source is configured.
    """
    import geopandas as gpd
    out = pd.DataFrame({"unit_id": up["unit_id"].values, "u_tma": np.nan, "u_tma_recession": np.nan,
                        "n_tma_stations": np.nan if t is None else 0})
    if t is None or len(t) == 0:
        return out
    t = t.sort_values(["station_id", "date"]).copy()
    t["exceed"] = (t["depth_m"] > threshold_m).astype(float)
    t["dd"] = t.groupby("station_id")["depth_m"].diff()
    t["dt"] = t.groupby("station_id")["date"].diff().dt.days
    r = t[(t["dd"] > 0) & (t["dt"] > 0) & (t["dt"] <= max_gap_days)]
    st = t.groupby("station_id").agg(exceed=("exceed", "mean"), lon=("lon", "first"), lat=("lat", "first"))
    st["recession"] = (r["dd"] / r["dt"] * 1000).groupby(r["station_id"]).mean()
    sg = gpd.GeoDataFrame(st, geometry=gpd.points_from_xy(st["lon"], st["lat"]), crs=4326).to_crs(up.crs)
    j = gpd.sjoin(sg, up[["unit_id", "geometry"]], predicate="within", how="inner")
    g = j.groupby("unit_id")
    out["u_tma"] = out["unit_id"].map(g["exceed"].mean()).values
    out["u_tma_recession"] = out["unit_id"].map(g["recession"].mean()).values
    out["n_tma_stations"] = out["unit_id"].map(g.size()).fillna(0).astype(int).values
    log.info("TMA: %d stations inside %d of %d units", len(j), int((out["n_tma_stations"] > 0).sum()), len(out))
    return out
