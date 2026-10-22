"""Fetch the Global Forest Watch peat extent for the KHG into _data as one GeoTIFF.

    python -m repeat_common.fetch_peat_extent [--zoom 11] [DATA_DIR]

Source: GFW "Global peatland extent" v20230315 (CC BY 4.0), public tile cache
https://tiles.globalforestwatch.org/gfw_peatlands/v20230315/is/{z}/{x}/{y}.png. GFW describes it as a composite of five datasets; for
Indonesia it is the union of Miettinen et al. 2016 and Gumbricht et al. 2017, rasterised or resampled to about 30 m, so the peat area
can exceed either source alone. The Miettinen paper covers Peninsular Malaysia, Sumatra and Borneo, so in Papua the layer rests on
Gumbricht et al. 2017 (a modelled map). The full-resolution download needs a free GFW API key; the tile cache does not, but it is a
256 px picture per tile, so zoom 11 (about 76 m) is what we take. Cells with partial transparency count as peat (alpha > 0).
Only tiles that touch a KHG boundary are fetched, one at a time with a pause; tiles already in the output are skipped.
Output: <DATA_DIR>/peat_extent_gfw_v20230315_z<zoom>.tif, EPSG:3857, uint8: 1 = peat, 0 = no peat, 255 = tile not fetched.
"""
import argparse
import io
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pyogrio
import rasterio
import shapely
from PIL import Image
from rasterio.transform import from_origin
from rasterio.windows import Window

URL = "https://tiles.globalforestwatch.org/gfw_peatlands/v20230315/is/{z}/{x}/{y}.png"
PAUSE_S = 0.25
BATCH = 100
ORIGIN = 20037508.342789244


def tile_xy(lon, lat, z):
    n = 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def tile_box(x, y, z):
    n = 2 ** z
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return shapely.box(x / n * 360 - 180, lat0, (x + 1) / n * 360 - 180, lat1)


def needed_tiles(geoms, z):
    tiles = set()
    for g in geoms:
        x0, y1 = tile_xy(g.bounds[0], g.bounds[1], z)
        x1, y0 = tile_xy(g.bounds[2], g.bounds[3], z)
        for x in range(int(x0), int(x1) + 1):
            for y in range(int(y0), int(y1) + 1):
                if (x, y) not in tiles and g.intersects(tile_box(x, y, z)):
                    tiles.add((x, y))
    return sorted(tiles)


def get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            wait = 20 * 2 ** attempt
        except OSError:
            wait = 20 * 2 ** attempt
        print(f"  retry in {wait}s: {url}")
        time.sleep(wait)
    raise RuntimeError(f"gave up on {url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    ap.add_argument("--zoom", type=int, default=11)
    a = ap.parse_args()
    data = a.data or os.environ.get("REPEAT_DATA")
    if not data:
        raise SystemExit("give the data folder as an argument or set REPEAT_DATA")
    data = Path(data)
    z = a.zoom
    khg = pyogrio.read_dataframe(data / "batas_khg_indonesia.gpkg", read_geometry=True)
    tiles = needed_tiles(shapely.make_valid(khg.geometry.values), z)
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    x0, y0 = min(xs), min(ys)
    W, H = (max(xs) - x0 + 1) * 256, (max(ys) - y0 + 1) * 256
    size = 2 * ORIGIN / 2 ** z
    res = size / 256
    out = data / f"peat_extent_gfw_v20230315_z{z}.tif"
    prof = dict(driver="GTiff", width=W, height=H, count=1, dtype="uint8", crs="EPSG:3857", nodata=255, tiled=True,
                blockxsize=256, blockysize=256, compress="lzw", transform=from_origin(-ORIGIN + x0 * size, ORIGIN - y0 * size, res, res))
    state = out.with_suffix(".done")
    done = set(state.read_text().split()) if state.exists() and out.exists() else set()
    mode = "r+" if out.exists() and done else "w"
    todo = [t for t in tiles if f"{t[0]}/{t[1]}" not in done]
    print(f"{len(tiles)} tiles at zoom {z}; {len(done)} already done; raster {W}x{H}", flush=True)
    for start in range(0, len(todo), BATCH):
        # GDAL keeps written blocks in memory until the file is closed, so the file is closed after every batch and the
        # state file is written only then: a crash cannot leave tiles marked done that are not on disk
        with rasterio.open(out, mode, **(prof if mode == "w" else {})) as dst:
            for x, y in todo[start:start + BATCH]:
                b = get(URL.format(z=z, x=x, y=y))
                arr = np.zeros((256, 256), np.uint8) if b is None else (np.asarray(Image.open(io.BytesIO(b)).convert("RGBA"))[..., 3] > 0).astype(np.uint8)
                dst.write(arr, 1, window=Window((x - x0) * 256, (y - y0) * 256, 256, 256))
                done.add(f"{x}/{y}")
                time.sleep(PAUSE_S)
        mode = "r+"
        state.write_text("\n".join(sorted(done)))
        print(f"  {len(done)}/{len(tiles)}", flush=True)
    print(f"done: {out} ({out.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
