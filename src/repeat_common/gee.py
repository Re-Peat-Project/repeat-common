"""Google Earth Engine bridge: bring an Earth Engine image into _data as GeoTIFF on its native grid.

    python -m repeat_common.gee --asset projects/sat-io/open-datasets/DELTARES/deltadtm_v1-1 \
        --khg "KHG Sungai Mendahara - Sungai Batanghari" --out dem/open/deltadtm/deltadtm_v1-1_Mendahara.tif [DATA_DIR]

    from repeat_common import gee
    gee.init()                                   # project from --project, $EE_PROJECT or the saved credentials
    gee.to_geotiff(ee.Image(...), region, path)  # any ee.Image, e.g. a composite built in Python

Setup, once per machine: `pip install -e repeat-common[gee]`, then `earthengine authenticate` (opens a browser) and set
EE_PROJECT to a Cloud project registered for Earth Engine.

Pixels come through ee.data.computePixels on a grid that is the image's own projection (CRS and affine transform of its
first band, or --crs/--transform when the image is a mosaic without one), cut to the region plus --buffer-m and split
into chunks under the 48 MB request limit, so nothing is resampled. Chunks are written into one tiled GeoTIFF; finished
chunks are recorded in a .progress file next to it, so an interrupted run resumes. The asset id, native grid, date and
band names go into the file's tags.
"""
import argparse
import io
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
MAX_BYTES = 40 * 1024 * 1024  # below the 48 MB computePixels limit


def init(project=None):
    import ee
    project = project or os.environ.get("EE_PROJECT")
    try:
        ee.Initialize(project=project) if project else ee.Initialize()
    except Exception as e:
        sys.exit(f"Earth Engine init failed ({e}). Run `earthengine authenticate` and set EE_PROJECT.")
    return ee


def native_grid(img, crs=None, transform=None):
    """CRS code and affine (a, b, c, d, e, f) of the image's first band, unless given."""
    if crs and transform:
        return crs, list(transform)
    p = img.select(0).projection().getInfo()
    if "transform" not in p:
        sys.exit("The image has no fixed native grid (a mosaic?); pass --crs and --transform.")
    return p["crs"], p["transform"]


def to_geotiff(img, region_ll, dest, crs=None, transform=None, dtype=None, nodata=None, source=None):
    """Write img over region_ll (shapely geometry, EPSG:4326) to dest on the image's native grid.
    source: asset id for the tags (operations such as unmask() drop system:id)."""
    import ee
    from pyproj import Transformer
    crs, t = native_grid(img, crs, transform)
    a, b, c, d, e, f = t
    if b or d:
        sys.exit("Rotated grids are not supported.")
    x0, y0, x1, y1 = Transformer.from_crs(4326, crs, always_xy=True).transform_bounds(*region_ll.bounds)
    c0, c1 = math.floor((x0 - c) / a), math.ceil((x1 - c) / a)
    r0, r1 = math.floor((y1 - f) / e), math.ceil((y0 - f) / e)  # e < 0
    W, H = c1 - c0, r1 - r0
    bands = img.bandNames().getInfo()
    info = img.select(0).getInfo()["bands"][0]["data_type"]
    np_dtype = np.dtype(dtype or {"float": "float32", "double": "float64"}.get(info.get("precision"), "float32"))
    n_b = len(bands)
    side = int(math.sqrt(MAX_BYTES / (np_dtype.itemsize * n_b)))
    side = max(256, (side // 256) * 256)
    out_t = Affine(a, 0, c + c0 * a, 0, e, f + r0 * e)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    prog = dest.with_suffix(".progress")
    done = set(json.loads(prog.read_text())) if prog.exists() and dest.exists() else set()
    prof = {"driver": "GTiff", "width": W, "height": H, "count": n_b, "dtype": np_dtype.name, "crs": crs, "transform": out_t,
            "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER",
            "nodata": nodata}
    mode = "r+" if done else "w"
    chunks = [(rr, cc) for rr in range(0, H, side) for cc in range(0, W, side)]
    print(f"{dest.name}: {W} x {H} px on {crs} ({abs(a):g} x {abs(e):g}), {len(chunks)} chunks", flush=True)
    with rasterio.open(dest, mode, **({} if done else prof)) as o:
        if not done:
            o.update_tags(ee_asset=source or str(img.get("system:id").getInfo() or ""), native_crs=crs, native_transform=json.dumps(t),
                          bands=",".join(bands), fetched=str(date.today()))
        for k, (rr, cc) in enumerate(chunks, 1):
            key = f"{rr},{cc}"
            if key in done:
                continue
            h, w = min(side, H - rr), min(side, W - cc)
            req = {"expression": img, "fileFormat": "GEO_TIFF",
                   "grid": {"dimensions": {"width": w, "height": h}, "crsCode": crs,
                            "affineTransform": {"scaleX": a, "shearX": 0, "translateX": c + (c0 + cc) * a,
                                                "shearY": 0, "scaleY": e, "translateY": f + (r0 + rr) * e}}}
            raw = ee.data.computePixels(req)
            with rasterio.open(io.BytesIO(raw)) as src:
                o.write(src.read().astype(np_dtype), window=rasterio.windows.Window(cc, rr, w, h))
            done.add(key)
            prog.write_text(json.dumps(sorted(done)))
            print(f"  chunk {k}/{len(chunks)}", flush=True)
    prog.unlink(missing_ok=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--asset", required=True, help="Earth Engine image id")
    ap.add_argument("--band", nargs="*", help="bands to keep (default all)")
    ap.add_argument("--khg", required=True)
    ap.add_argument("--buffer-m", type=float, default=2000.0)
    ap.add_argument("--out", required=True, help="path relative to DATA_DIR")
    ap.add_argument("--project")
    ap.add_argument("--crs")
    ap.add_argument("--transform", type=float, nargs=6)
    ap.add_argument("--nodata", type=float)
    a = ap.parse_args()
    import geopandas as gpd
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    norm = lambda s: " ".join(str(s).split())          # the boundary file has double spaces in some names
    sel = b[b[BOUNDARY[2]].map(norm) == norm(a.khg)]
    if sel.empty:
        sys.exit(f"KHG not found: {a.khg}")
    region = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()
    ee = init(a.project)
    img = ee.Image(a.asset)
    if a.band:
        img = img.select(a.band)
    if a.nodata is not None:  # masked pixels would otherwise arrive as 0
        img = img.unmask(a.nodata)
    to_geotiff(img, region, data / a.out, a.crs, a.transform, nodata=a.nodata, source=a.asset)
    print("wrote", data / a.out)


if __name__ == "__main__":
    main()
