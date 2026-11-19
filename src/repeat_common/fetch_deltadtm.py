"""DeltaDTM v1.1 for a KHG straight from its publisher, without Earth Engine.

    python -m repeat_common.fetch_deltadtm --khg "KHG Sungai Mendahara - Sungai Batanghari" \
        --out dem/open/deltadtm/deltadtm_v1-1_Mendahara_Batanghari.tif [DATA_DIR]

Source: Pronk et al. (2024), DeltaDTM v1.1, 4TU.ResearchData, doi 10.4121/21997565, CC-BY 4.0 (tile tags). 1 x 1 degree
Float32 tiles, 1 arcsecond, EPSG:4326 with EGM2008 heights, nodata -9999, inside one zip per continent (Asia 17 GB). The zips
store the tiles uncompressed, so each tile needed is read out of the remote zip with HTTP range requests (6-50 MB a tile) and
kept in `<DATA_DIR>/dem/open/deltadtm/tiles/4tu_v<version>/`; nothing else is downloaded. The member list of the zips is cached there as
`index.json`. A 1-degree cell with no tile lies outside DeltaDTM's coverage and stays nodata.

The output matches `repeat_common.gee --asset projects/sat-io/open-datasets/DELTARES/deltadtm_v1-1` (the Earth Engine copy
of the same tiles, which the pipeline used before): the KHG plus --buffer-m, the same grid arithmetic on the same native grid
(origin -180.000139, 84.000139; the tiles sit on it), no resampling. `--check` compares a written file with an existing Earth
Engine clip pixel by pixel. Record version 3 (the default) gave the Earth Engine clips back exactly at Balangan-Batangalai,
Mempawah-Peniti and Buru Mappi-Buru Obaa, and at Sugihan-Lumpur up to 3 pixels in 10.6 million that differ by 4e-11 m (float32
rounding near 0); record version 4 differs (median 0.6-5 cm, 0.1% of pixels by more than 7 m), so it is a different edition.
"""
import argparse
import io
import json
import math
import os
import struct
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window, from_bounds

ARTICLE = "1da2e70f-6c4d-4b03-86bd-b53e789cc629"
VERSION_API = "https://data.4tu.nl/v2/articles/" + ARTICLE + "/versions/{v}"   # 4TU record versions: 1-2 DeltaDTM v1.0 (2023),
VERSION = 3                                  # 3 v1.1 (2024-11-21), 4 v1.1 with tiles tagged 1.1.1 (2024-12-12)
ZIPS = ("Asia.zip", "Oceania.zip")          # Indonesia lies in these two (Papua may be filed under Oceania)
DOI = "10.4121/21997565"
NATIVE = [0.0002777777777777778, 0, -180.0001388888889, 0, -0.0002777777777777778, 84.00013888888888]  # as Earth Engine reports it
NODATA = -9999.0
TILE_DIR = Path("dem/open/deltadtm/tiles")


def _get(url, start=None, end=None, tries=4):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"} if start is not None else {})
    for k in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:                                  # transient network errors: wait and retry
            if k == tries - 1:
                raise
            print(f"  retry {k + 1} after {e}", flush=True)
            time.sleep(5 * (k + 1))


class _Remote(io.RawIOBase):
    """Seekable read-only view of a remote file through HTTP range requests (enough for zipfile to read the directory)."""
    def __init__(self, url, size):
        self.url, self.size, self.pos = url, size, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        d = _get(self.url, self.pos, min(self.pos + len(b), self.size) - 1)
        b[:len(d)] = d
        self.pos += len(d)
        return len(d)


def index(tile_dir: Path, version: int = VERSION) -> dict:
    """{tile file name: {url, header, size}} for the continent zips of one 4TU record version; read once, then from index.json."""
    import zipfile
    p = tile_dir / "index.json"
    if p.is_file():
        return json.loads(p.read_text())
    files = {f["name"]: f for f in json.loads(_get(VERSION_API.format(v=version)))["files"]}
    out = {}
    for z in ZIPS:
        f = files[z]
        with zipfile.ZipFile(io.BufferedReader(_Remote(f["download_url"], f["size"]), buffer_size=1 << 20)) as zf:
            for i in zf.infolist():
                if i.compress_type != zipfile.ZIP_STORED:
                    sys.exit(f"{z}/{i.filename} is compressed inside the zip; range reads assume stored tiles.")
                out[i.filename] = {"url": f["download_url"], "header": i.header_offset, "size": i.file_size}
        print(f"{z}: {sum(1 for v in out.values() if v['url'] == f['download_url'])} tiles listed", flush=True)
    tile_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"source": f"doi {DOI}, record version {version}", "listed": str(date.today()), "tiles": out}, indent=0))
    return json.loads(p.read_text())


def cell(lat: int, lon: int) -> str:
    """Tiles are named by their south-west corner: ..._N00E006.tif covers 0..1 N, 6..7 E."""
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tile(code: str, idx: dict, tile_dir: Path):
    """Local path of the tile of one 1-degree cell, read out of the remote zip on first use; None when DeltaDTM has none there."""
    names = [n for n in idx["tiles"] if n.endswith(f"_{code}.tif")]
    if not names:
        return None
    name = names[0]
    t = idx["tiles"][name]
    p = tile_dir / name
    if p.is_file() and p.stat().st_size == t["size"]:
        return p
    h = _get(t["url"], t["header"], t["header"] + 29)          # local file header: the data follow its name and extra field
    n, e = struct.unpack("<HH", h[26:30])
    start = t["header"] + 30 + n + e
    tmp = p.with_suffix(".part")
    tmp.write_bytes(_get(t["url"], start, start + t["size"] - 1))
    if tmp.stat().st_size != t["size"]:
        sys.exit(f"{name}: got {tmp.stat().st_size} bytes, expected {t['size']}")
    os.replace(tmp, p)
    print(f"  tile {name} ({t['size'] / 1e6:.0f} MB)", flush=True)
    return p


def to_geotiff(region_ll, dest: Path, data: Path, version: int = VERSION):
    """Write DeltaDTM over region_ll (shapely, EPSG:4326) to dest; grid arithmetic as repeat_common.gee.to_geotiff."""
    a, _, c, _, e, f = NATIVE
    x0, y0, x1, y1 = region_ll.bounds
    c0, c1 = math.floor((x0 - c) / a), math.ceil((x1 - c) / a)
    r0, r1 = math.floor((y1 - f) / e), math.ceil((y0 - f) / e)
    W, H = c1 - c0, r1 - r0
    out_t = Affine(a, 0, c + c0 * a, 0, e, f + r0 * e)
    arr = np.full((H, W), NODATA, dtype="float32")
    tile_dir = data / TILE_DIR / f"4tu_v{version}"
    idx = index(tile_dir, version)
    left, top = out_t.c, out_t.f
    right, bottom = left + W * a, top + H * e
    used = []
    for lat in range(math.floor(bottom), math.ceil(top)):
        for lon in range(math.floor(left), math.ceil(right)):
            p = tile(cell(lat, lon), idx, tile_dir)
            if p is None:
                continue
            with rasterio.open(p) as s:
                if abs(((s.transform.c - c) / a) - round((s.transform.c - c) / a)) > 1e-6 or \
                        abs(((s.transform.f - f) / e) - round((s.transform.f - f) / e)) > 1e-6:
                    sys.exit(f"{p.name} is not on the native grid; refusing to resample.")
                # overlap of the tile and the output, in output pixels
                oc0 = max(0, round((s.transform.c - left) / a)); or0 = max(0, round((s.transform.f - top) / e))
                oc1 = min(W, round((s.transform.c + s.width * a - left) / a)); or1 = min(H, round((s.transform.f + s.height * e - top) / e))
                if oc1 <= oc0 or or1 <= or0:
                    continue
                tc0 = round((left + oc0 * a - s.transform.c) / a); tr0 = round((top + or0 * e - s.transform.f) / e)
                block = s.read(1, window=Window(tc0, tr0, oc1 - oc0, or1 - or0))
                sub = arr[or0:or1, oc0:oc1]
                ok = block != (s.nodata if s.nodata is not None else NODATA)
                sub[ok] = block[ok]
                used.append(p.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    prof = {"driver": "GTiff", "width": W, "height": H, "count": 1, "dtype": "float32", "crs": "EPSG:4326", "transform": out_t,
            "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER", "nodata": NODATA}
    tmp = dest.with_suffix(".part.tif")
    with rasterio.open(tmp, "w", **prof) as o:
        o.write(arr, 1)
        o.update_tags(source=f"DeltaDTM, 4TU.ResearchData doi {DOI} record version {version}, CC-BY 4.0", tiles=",".join(sorted(used)),
                      native_crs="EPSG:4326", native_transform=json.dumps(NATIVE), bands="b1", fetched=str(date.today()))
    os.replace(tmp, dest)
    print(f"{dest.name}: {W} x {H} px from {len(used)} tile(s)", flush=True)
    return dest


def check(new: Path, old: Path) -> bool:
    """Pixel comparison with an Earth Engine clip of the same KHG: grid, nodata pattern and values."""
    with rasterio.open(new) as n, rasterio.open(old) as o:
        same_grid = (n.width, n.height, n.crs) == (o.width, o.height, o.crs) and n.transform.almost_equals(o.transform, 1e-9)
        if not same_grid:
            print(f"grid differs: {n.width}x{n.height} {n.transform} vs {o.width}x{o.height} {o.transform}")
            win = from_bounds(*n.bounds, transform=o.transform).round_offsets().round_lengths()
            a, b = n.read(1), o.read(1, window=win, boundless=True, fill_value=NODATA)
        else:
            a, b = n.read(1), o.read(1)
    nd_a, nd_b = a == NODATA, b == NODATA
    both = ~nd_a & ~nd_b
    diff = np.abs(a[both] - b[both])
    # Earth Engine's float conversion can flip the last float32 bit of values near 0 (seen: 3 of 10.6 million pixels, 4e-11 m)
    print(f"pixels {a.size}; nodata here {nd_a.sum()}, there {nd_b.sum()}, differ in nodata {int((nd_a != nd_b).sum())}; "
          f"valid in both {both.sum()}, identical {int((diff == 0).sum())}, max |difference| {diff.max() if diff.size else 0:.3g} m")
    return same_grid and not (nd_a != nd_b).any() and (diff.size == 0 or diff.max() <= 1e-9)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", required=True)
    ap.add_argument("--buffer-m", type=float, default=2000.0)
    ap.add_argument("--out", required=True, help="path relative to DATA_DIR")
    ap.add_argument("--version", type=int, default=VERSION, help=f"4TU record version (default {VERSION})")
    ap.add_argument("--check", help="an Earth Engine clip of the same KHG to compare with (path relative to DATA_DIR)")
    a = ap.parse_args()
    import geopandas as gpd
    from .gee import BOUNDARY
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    norm = lambda s: " ".join(str(s).split())
    sel = b[b[BOUNDARY[2]].map(norm) == norm(a.khg)]
    if sel.empty:
        sys.exit(f"KHG not found: {a.khg}")
    region = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()   # as repeat_common.gee
    out = to_geotiff(region, data / a.out, data, a.version)
    print("wrote", out)
    if a.check:
        sys.exit(0 if check(out, data / a.check) else 1)


if __name__ == "__main__":
    main()
