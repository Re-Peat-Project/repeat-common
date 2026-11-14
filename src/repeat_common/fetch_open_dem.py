"""Fetch open global DEMs (and the land-cover and canopy layers repeat-dem uses) for KHG into _data, as delivered.

Output: _data/dem/open/<product>/, except chm -> _data/canopy/meta_chm_v1/.

    python -m repeat_common.fetch_open_dem --khg "KHG Sungai Mendahara - Sungai Batanghari" [--product glo30 fabdem gedtm30] [DATA_DIR]

Products (1-degree tiles are kept whole, under the upstream file name; nothing is resampled):
- glo30: Copernicus DEM GLO-30 (DSM, EGM2008), public COGs on AWS. Licence: Copernicus DEM licence (free, attribution).
- fabdem: FABDEM v1.2 (GLO-30 with forests and buildings removed; Hawker et al. 2022), Hugging Face mirror links-ads/fabdem.
  Licence CC BY-NC-SA 4.0 (Bristol) / Non-Commercial Government Licence v2.0 (mirror).
- worldcover: ESA WorldCover 10 m 2021 v200 land cover (3-degree tiles, public COGs on AWS; CC BY 4.0), the classes
  repeat-dem uses to split DEM errors.
- chm: Meta/WRI global canopy height v1 (Tolan et al. 2024; 1 m, quadkey zoom-9 COG tiles on AWS dataforgood-fb-data,
  CC BY 4.0), the canopy layer repeat-dem subtracts; tiles touching the KHG plus --buffer-m.
- gem15: Google GEM-15 v1 (experimental, under review) 15 m, bare-earth DTM layer (Batchu et al. 2026, EarthArXiv
  doi:10.31223/X55N75; CC BY 4.0, attribution "This dataset is produced by Google"). Public COG mosaics in local UTM zones
  (gs://climate-and-sustainability/gem-15/<10-degree block>/<mosaic>/dtm/zall/mosaic.tif, also Earth Engine
  projects/climate-and-sustainability/assets/gem-15_v1). The window over the KHG (plus --buffer-m) is cut from every mosaic
  that overlaps it, on the mosaics' own 15 m grid (no resampling); heights are moved from EGM96 to EGM2008 (the datum of the
  other DEMs here) with the PROJ geoid grids, the shift evaluated every 750 m and interpolated bilinearly (it varies by
  centimetres over a km). One file per UTM zone: gem15_v1_dtm_<KHG>_<epsg>.tif.
- gedtm30: GEDTM30 v1.1 30 m (OpenGeoHub; Ho et al. 2025), one global COG; the window over the KHG (plus --buffer-m) is cut
  at native resolution, with the per-cell uncertainty layer (std). Values are int32 with the upstream band scale kept
  (m layer: decimetres, scale 0.1; heights on EGM2008, EPSG:3855). Licence CC BY 4.0. In 2026-10 the host's TLS certificate
  had expired; the fetch then stops with that error rather than skipping verification.
Tiles already present are skipped. One request at a time with a pause.
"""
import argparse
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds

BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
GLO30 = "https://copernicus-dem-30m.s3.amazonaws.com/{n}/{n}.tif"  # n = Copernicus_DSM_COG_10_S02_00_E103_00_DEM
FABDEM = "https://huggingface.co/buckets/links-ads/fabdem/resolve/tiles/{block}_FABDEM_V1-2/{tile}_FABDEM_V1-2.tif"
WORLDCOVER = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/{n}.tif"  # n = ESA_WorldCover_10m_2021_v200_S03E105_Map
CHM = "https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/chm/{q}.tif"
GEDTM = "https://s3.opengeohub.org/global/edtm/gedtm_rf_{layer}_30m_s_20060101_20151231_go_epsg.4326.3855_v20250611.tif"
PAUSE_S = 1.0


def hemi(v, ns):
    return ("N" if v >= 0 else "S") if ns else ("E" if v >= 0 else "W")


def tiles(bounds):
    x0, y0, x1, y1 = bounds
    return [(la, lo) for la in range(math.floor(y0), math.floor(y1) + 1) for lo in range(math.floor(x0), math.floor(x1) + 1)]


def tile_url(product, la, lo):
    ns, ew = hemi(la, True), hemi(lo, False)
    if product == "worldcover":
        n = f"ESA_WorldCover_10m_2021_v200_{ns}{abs(la):02d}{ew}{abs(lo):03d}_Map"
        return WORLDCOVER.format(n=n), f"{n}.tif"
    if product == "glo30":
        n = f"Copernicus_DSM_COG_10_{ns}{abs(la):02d}_00_{ew}{abs(lo):03d}_00_DEM"
        return GLO30.format(n=n), f"{n}.tif"
    la0, lo0 = (la // 10) * 10, (lo // 10) * 10
    block = f"{hemi(la0, True)}{abs(la0):02d}{hemi(lo0, False)}{abs(lo0):03d}-{hemi(la0 + 10, True)}{abs(la0 + 10):02d}{hemi(lo0 + 10, False)}{abs(lo0 + 10):03d}"
    t = f"{ns}{abs(la):02d}{ew}{abs(lo):03d}"
    return FABDEM.format(block=block, tile=t), f"{t}_FABDEM_V1-2.tif"


def fetch_tile(url, dest):
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    os.replace(tmp, dest)


def cut_cog(url, bounds, dest):
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="120"):
        with rasterio.open("/vsicurl/" + url) as d:
            w = from_bounds(*bounds, d.transform).round_offsets().round_lengths()
            a = d.read(1, window=w)
            prof = d.profile | {"driver": "GTiff", "width": a.shape[1], "height": a.shape[0], "transform": d.window_transform(w),
                                "compress": "deflate", "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER"}
            with rasterio.open(dest, "w", **prof) as o:
                o.write(a, 1)
                o.scales, o.offsets = d.scales, d.offsets
                o.update_tags(source=url, window_bounds=",".join(map(str, bounds)), scales=",".join(map(str, d.scales)),
                              **{k: v for k, v in d.tags().items() if k == "VERTICAL_DATUM"})


GEM15_BUCKET = "https://storage.googleapis.com/climate-and-sustainability/"
GEM15_LIST = "https://storage.googleapis.com/storage/v1/b/climate-and-sustainability/o?prefix=gem-15/{block}/&fields=items(name),nextPageToken&maxResults=1000"


def gem15_mosaics(bounds) -> list[str]:
    """URLs of the GEM-15 DTM mosaics in the 10-degree blocks the bounds touch (block m10_p100: lat -10..0, lon 100..110)."""
    import json
    import math
    import urllib.request
    lo0, la0, lo1, la1 = bounds
    out = []
    for la in range(math.floor(la0 / 10) * 10, math.floor(la1 / 10) * 10 + 1, 10):
        for lo in range(math.floor(lo0 / 10) * 10, math.floor(lo1 / 10) * 10 + 1, 10):
            block = f"{'m' if la < 0 else 'p'}{abs(la)}_{'m' if lo < 0 else 'p'}{abs(lo)}"
            token = ""
            while True:
                r = json.load(urllib.request.urlopen(GEM15_LIST.format(block=block) + (f"&pageToken={token}" if token else ""), timeout=120))
                out += [GEM15_BUCKET + i["name"] for i in r.get("items", []) if i["name"].endswith("/dtm/zall/mosaic.tif")]
                token = r.get("nextPageToken")
                if not token:
                    break
    return out


def fetch_gem15(geom, khg: str, out: Path) -> list[Path]:
    """Cut the GEM-15 DTM over geom (EPSG:4326) from every overlapping mosaic, one file per UTM zone, heights on EGM2008."""
    import numpy as np
    import pyproj
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds
    from scipy.ndimage import map_coordinates
    slug = khg.replace("KHG ", "").replace(" - ", "_").replace(" ", "")
    groups: dict = {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="120", GDAL_HTTP_MAX_RETRY="4"):
        for url in gem15_mosaics(geom.bounds):
            with rasterio.open("/vsicurl/" + url) as d:
                b = transform_bounds(d.crs, 4326, *d.bounds)
                if b[0] > geom.bounds[2] or b[2] < geom.bounds[0] or b[1] > geom.bounds[3] or b[3] < geom.bounds[1]:
                    continue
                groups.setdefault(d.crs.to_epsg(), []).append((url, d.transform, d.width, d.height))
        written = []
        for epsg, srcs in groups.items():
            dest = out / f"gem15_v1_dtm_{slug}_{epsg}.tif"
            if dest.exists() and dest.stat().st_size > 0:
                written.append(dest)
                continue
            x0, y0, x1, y1 = transform_bounds(4326, epsg, *geom.bounds, densify_pts=41)
            t0 = srcs[0][1]
            res = t0.a
            if any(abs(t.a - res) > 1e-9 or abs(((t.c - t0.c) / res) % 1) > 1e-6 or abs(((t.f - t0.f) / res) % 1) > 1e-6 for _, t, _, _ in srcs):
                sys.exit(f"{khg}: GEM-15 mosaics of EPSG:{epsg} are not on one grid; not mosaicking them")
            # the output grid: the mosaics' own grid, snapped outward to the KHG window
            gx0 = t0.c + np.floor((x0 - t0.c) / res) * res
            gy1 = t0.f - np.floor((t0.f - y1) / res) * res
            W, H = int(np.ceil((x1 - gx0) / res)), int(np.ceil((gy1 - y0) / res))
            arr = np.full((H, W), np.nan, dtype="float32")
            for url, t, w, h in srcs:
                col0, row0 = int(round((gx0 - t.c) / res)), int(round((t.f - gy1) / res))
                c0, r0 = max(col0, 0), max(row0, 0)
                c1, r1 = min(col0 + W, w), min(row0 + H, h)
                if c1 <= c0 or r1 <= r0:
                    continue
                with rasterio.open("/vsicurl/" + url) as d:
                    a = d.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype("float32")
                    if d.nodata is not None and not np.isnan(d.nodata):
                        a[a == d.nodata] = np.nan
                sub = arr[r0 - row0:r1 - row0, c0 - col0:c1 - col0]
                fill = np.isnan(sub) & ~np.isnan(a)       # overlapping mosaics: the first valid value is kept
                sub[fill] = a[fill]
                print(f"{khg}: GEM-15 {url.split('/')[-4]}: {int(fill.sum()):,} cells", flush=True)
            # EGM96 -> EGM2008: N96 - N08 on a 750 m lattice, interpolated bilinearly to every cell
            pyproj.network.set_network_enabled(True)
            step = 50
            ys, xs = np.arange(0, H + step, step), np.arange(0, W + step, step)
            X, Y = np.meshgrid(gx0 + (xs + 0.5) * res, gy1 - (ys + 0.5) * res)
            lon, lat = pyproj.Transformer.from_crs(epsg, 4326, always_xy=True).transform(X, Y)
            shift = pyproj.Transformer.from_crs("EPSG:4326+5773", "EPSG:4326+3855", always_xy=True).transform(lon, lat, np.zeros_like(lon))[2]
            if not np.all(np.isfinite(shift)) or np.all(shift == 0):
                sys.exit("PROJ could not apply EGM96 and EGM2008 (geoid grids unavailable); GEM-15 not written")
            rr, cc = np.mgrid[0:H, 0:W].astype("float32") / step
            for r in range(0, H, 2048):                    # in row bands, to keep memory low
                band = map_coordinates(shift, [rr[r:r + 2048], cc[r:r + 2048]], order=1, mode="nearest").astype("float32")
                arr[r:r + 2048] += band
            from rasterio.transform import from_origin
            prof = {"driver": "GTiff", "width": W, "height": H, "count": 1, "dtype": "float32", "crs": f"EPSG:{epsg}",
                    "transform": from_origin(gx0, gy1, res, res), "nodata": float("nan"), "compress": "deflate", "predictor": 3,
                    "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER"}
            with rasterio.open(dest, "w", **prof) as o:
                o.write(arr, 1)
                o.update_tags(source=";".join(u for u, *_ in srcs), product="GEM-15 v1 dtm (Google, experimental)",
                              licence="CC BY 4.0; attribution: This dataset is produced by Google", doi="10.31223/X55N75",
                              VERTICAL_DATUM="EGM2008 (moved from EGM96 with PROJ geoid grids)",
                              egm96_to_egm2008_shift_m=f"{float(np.nanmin(shift)):.3f}..{float(np.nanmax(shift)):.3f}")
            print(f"{khg}: EGM96 -> EGM2008 shift {np.nanmin(shift):+.2f} to {np.nanmax(shift):+.2f} m", flush=True)
            written.append(dest)
    return written


def set_band_scale(dest):
    """Files cut before the band scale was carried over: set it from the `scales` tag written at the cut (values untouched)."""
    with rasterio.open(dest, "r+") as o:
        s = float(o.tags().get("scales", "1").split(",")[0])
        if o.scales[0] != s:
            o.scales = (s,)
            print(f"{dest.name}: band scale set to {s}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", action="append", required=True)
    ap.add_argument("--product", nargs="+", default=["glo30", "fabdem"], choices=["glo30", "fabdem", "worldcover", "chm", "gedtm30", "gem15"])
    ap.add_argument("--buffer-m", type=float, default=2000.0, help="margin around the KHG for gedtm30 windows")
    a = ap.parse_args()
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    for k in a.khg:
        sel = b[b[BOUNDARY[2]] == k]
        if sel.empty:
            sys.exit(f"KHG not found: {k}")
        geom = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()
        for p in a.product:
            out = data / "canopy" / "meta_chm_v1" if p == "chm" else data / "dem" / "open" / p
            out.mkdir(parents=True, exist_ok=True)
            if p == "chm":
                import mercantile
                qs = sorted({mercantile.quadkey(t) for t in mercantile.tiles(*geom.bounds, zooms=9)})
                for q in qs:
                    dest = out / f"{q}.tif"
                    if dest.exists() and dest.stat().st_size > 0:
                        print(f"{k}: chm {q} already there", flush=True)
                        continue
                    time.sleep(PAUSE_S)
                    fetch_tile(CHM.format(q=q), dest)
                    print(f"{k}: chm {q} {dest.stat().st_size / 1e6:.0f} MB", flush=True)
                continue
            if p == "gem15":
                for dest in fetch_gem15(geom, k, out):
                    print(f"{k}: {dest.name}", flush=True)
                continue
            if p == "gedtm30":
                slug = k.replace("KHG ", "").replace(" - ", "_").replace(" ", "")
                for layer in ("m", "std"):
                    dest = out / f"gedtm30_v1.1_{layer}_{slug}.tif"
                    if not dest.exists():
                        cut_cog(GEDTM.format(layer=layer), geom.bounds, dest)
                    set_band_scale(dest)
                    print(f"{k}: {dest.name}", flush=True)
                continue
            step = 3 if p == "worldcover" else 1
            for la, lo in sorted({(la // step * step, lo // step * step) for la, lo in tiles(geom.bounds)}):
                url, name = tile_url(p, la, lo)
                dest = out / name
                if dest.exists() and dest.stat().st_size > 0:
                    print(f"{k}: {p} {name} already there", flush=True)
                    continue
                time.sleep(PAUSE_S)
                fetch_tile(url, dest)
                print(f"{k}: {p} {name} {dest.stat().st_size / 1e6:.0f} MB", flush=True)


if __name__ == "__main__":
    main()
