"""Analysis grid per KHG, analysis units (hexagons or sub-KHG polygons), raster helpers."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds

log = logging.getLogger("repeat_common.grid")


@dataclass
class Grid:
    crs: rasterio.crs.CRS
    transform: rasterio.Affine
    width: int
    height: int

    @property
    def res(self) -> float:
        return self.transform.a

    @property
    def shape(self):
        return (self.height, self.width)

    @property
    def bounds(self):
        l, t = self.transform.c, self.transform.f
        return (l, t - self.height * self.res, l + self.width * self.res, t)

    def bounds_ll(self):
        return transform_bounds(self.crs, "EPSG:4326", *self.bounds, densify_pts=21)

    def profile(self, dtype="float32", nodata=np.nan):
        return dict(driver="GTiff", crs=self.crs, transform=self.transform, width=self.width,
                    height=self.height, count=1, dtype=dtype, nodata=nodata, tiled=True,
                    blockxsize=512, blockysize=512, compress="deflate")


def auto_utm(lon: float, lat: float) -> str:
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def make_grid(geom_ll, res_m: float, buffer_m: float = 1000) -> Grid:
    """UTM grid covering a KHG geometry (given in EPSG:4326) plus a buffer."""
    import geopandas as gpd
    c = geom_ll.centroid
    crs = auto_utm(c.x, c.y)
    g = gpd.GeoSeries([geom_ll], crs=4326).to_crs(crs).buffer(buffer_m)
    minx, miny, maxx, maxy = g.total_bounds
    minx, miny = math.floor(minx / res_m) * res_m, math.floor(miny / res_m) * res_m
    maxx, maxy = math.ceil(maxx / res_m) * res_m, math.ceil(maxy / res_m) * res_m
    w, h = int(round((maxx - minx) / res_m)), int(round((maxy - miny) / res_m))
    return Grid(rasterio.crs.CRS.from_string(crs), from_origin(minx, maxy, res_m, res_m), w, h)


def hex_units(geom_proj, area_km2: float, min_fraction: float = 0.2):
    """Hexagon tessellation of a projected polygon. Returns list of (geometry, full_area_fraction)."""
    from shapely.geometry import Polygon
    a = area_km2 * 1e6
    s = math.sqrt(2 * a / (3 * math.sqrt(3)))          # side length
    w, h = 2 * s, math.sqrt(3) * s
    minx, miny, maxx, maxy = geom_proj.bounds
    out = []
    col = 0
    x = minx - w
    while x < maxx + w:
        y0 = miny - h + (h / 2 if col % 2 else 0)
        y = y0
        while y < maxy + h:
            hexagon = Polygon([(x + s * math.cos(math.radians(k)), y + s * math.sin(math.radians(k)))
                               for k in range(0, 360, 60)])
            if hexagon.intersects(geom_proj):
                part = hexagon.intersection(geom_proj)
                frac = part.area / hexagon.area
                if frac >= min_fraction:
                    out.append((part, frac))
            y += h
        x += 1.5 * s
        col += 1
    return out


def load_units(cfg: dict, khg_gdf):
    """GeoDataFrame of analysis units with columns khg_id, unit_id, area_km2 (in EPSG:4326)."""
    import geopandas as gpd
    import pandas as pd
    ucfg = cfg["units"]
    idf = cfg["khg_id_field"]
    if ucfg["mode"] == "khg":
        u = khg_gdf[[idf, "geometry"]].rename(columns={idf: "khg_id"}).copy()
        u["unit_id"] = u["khg_id"].astype(str)
        return gpd.GeoDataFrame(u[["khg_id", "unit_id", "geometry"]], crs=4326)
    if ucfg["mode"] == "vector":
        u = gpd.read_file(ucfg["path"]).to_crs(4326)
        if ucfg["id_field"] not in u.columns:
            u[ucfg["id_field"]] = [f"u{i}" for i in range(len(u))]
        u = u.rename(columns={ucfg["id_field"]: "unit_id"})
        if "khg_id" not in u.columns:
            j = gpd.sjoin(u.set_geometry(u.representative_point()), khg_gdf[[idf, "geometry"]],
                          how="left", predicate="within")
            u["khg_id"] = j[idf].astype(str).values
        frames = [u[["khg_id", "unit_id", "geometry"]]]
    else:
        frames = []
        for _, r in khg_gdf.iterrows():
            kid = str(r[idf])
            c = r.geometry.centroid
            crs = auto_utm(c.x, c.y)
            gp = gpd.GeoSeries([r.geometry], crs=4326).to_crs(crs).iloc[0]
            hexes = hex_units(gp, float(ucfg["hex_area_km2"]), float(ucfg["min_fraction"]))
            g = gpd.GeoDataFrame({"khg_id": kid, "unit_id": [f"{kid}_{i:05d}" for i in range(len(hexes))]},
                                 geometry=[h[0] for h in hexes], crs=crs).to_crs(4326)
            frames.append(g)
            log.info("KHG %s: %d hexagon units of ~%.2f km2", kid, len(g), ucfg["hex_area_km2"])
    units = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=4326)
    return units


def label_raster(units_proj, grid: Grid) -> np.ndarray:
    """Rasterize unit index (0..n-1) onto the grid; -1 outside units."""
    shapes = [(geom, i) for i, geom in enumerate(units_proj.geometry)]
    return rasterize(shapes, out_shape=grid.shape, transform=grid.transform, fill=-1, dtype="int32")


def warp_to_grid(src_path: str, grid: Grid, resampling=Resampling.bilinear, band=1,
                 nodata_override=None) -> np.ndarray:
    out = np.full(grid.shape, np.nan, dtype="float32")
    with rasterio.open(src_path) as src:
        nod = nodata_override if nodata_override is not None else src.nodata
        reproject(rasterio.band(src, band), out, src_nodata=nod, dst_transform=grid.transform,
                  dst_crs=grid.crs, dst_nodata=np.nan, resampling=resampling)
    return out


def array_to_grid(arr: np.ndarray, src_transform, src_crs, grid: Grid,
                  resampling=Resampling.bilinear) -> np.ndarray:
    out = np.full(grid.shape, np.nan, dtype="float32")
    reproject(arr.astype("float32"), out, src_transform=src_transform, src_crs=src_crs,
              src_nodata=np.nan, dst_transform=grid.transform, dst_crs=grid.crs,
              dst_nodata=np.nan, resampling=resampling)
    return out


def write(path: Path, arr: np.ndarray, grid: Grid, dtype="float32", nodata=np.nan):
    path.parent.mkdir(parents=True, exist_ok=True)
    prof = grid.profile(dtype, nodata)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(arr.astype(dtype), 1)


def read(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        a = src.read(1).astype("float32")
        if src.nodata is not None and not (isinstance(src.nodata, float) and math.isnan(src.nodata)):
            a[a == src.nodata] = np.nan
    return a


def vector_to_grid_mask(path_or_gdf, grid: Grid, buffer_m: float = 0.0, all_touched=True) -> np.ndarray:
    import geopandas as gpd
    g = gpd.read_file(path_or_gdf) if isinstance(path_or_gdf, (str, Path)) else path_or_gdf
    g = g.to_crs(grid.crs)
    geoms = g.geometry.buffer(buffer_m) if buffer_m > 0 else g.geometry
    geoms = [x for x in geoms if x is not None and not x.is_empty]
    if not geoms:
        return np.zeros(grid.shape, dtype=bool)
    return rasterize([(x, 1) for x in geoms], out_shape=grid.shape, transform=grid.transform,
                     fill=0, dtype="uint8", all_touched=all_touched).astype(bool)
