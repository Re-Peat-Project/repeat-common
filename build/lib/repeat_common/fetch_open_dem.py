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
- gedtm30: GEDTM30 v1.1 30 m (OpenGeoHub; Ho et al. 2025), one global COG; the window over the KHG (plus --buffer-m) is cut
  at native resolution, with the per-cell uncertainty layer (std). Licence CC BY 4.0. In 2026-10 the host's TLS certificate
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
                o.update_tags(source=url, window_bounds=",".join(map(str, bounds)), scales=",".join(map(str, d.scales)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", action="append", required=True)
    ap.add_argument("--product", nargs="+", default=["glo30", "fabdem"], choices=["glo30", "fabdem", "worldcover", "chm", "gedtm30"])
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
            if p == "gedtm30":
                slug = k.replace("KHG ", "").replace(" - ", "_").replace(" ", "")
                for layer in ("m", "std"):
                    dest = out / f"gedtm30_v1.1_{layer}_{slug}.tif"
                    if not dest.exists():
                        cut_cog(GEDTM.format(layer=layer), geom.bounds, dest)
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
