"""Fetch DEMNAS tiles from BIG (Ina-Geoportal / Tanahair) for a KHG into _data, as delivered.

    TANAHAIR_LINK="<one fresh DEMNAS download link>" python -m repeat_common.fetch_demnas --khg "KHG Sungai Sugihan - Sungai Lumpur" [DATA_DIR]

Port of the repeat-dem QGIS algorithm "0. Download DEMNAS" without QGIS. The public ImageServer of BIG serves a placeholder
instead of heights, so the real GeoTIFF sheets are fetched from tanahair.indonesia.go.id with the user's own session token:
log in at tanahair.indonesia.go.id, open the DEMNAS download map, click any one tile and copy the link that opens. The token
in it is reused for every sheet and is valid for about an hour. It is read from the TANAHAIR_LINK environment variable (or
stdin) and never written to disk or logs. The sheets that touch the KHG boundary (plus --buffer-m) are found from the
ImageServer footprint index (no token needed). Sheets already in the output folder are skipped, so a second fresh link
finishes an interrupted run. Output: <DATA_DIR>/dem/demnas_tiles/DEMNAS_<sheet>_v1.0.tif, unmodified (they carry no CRS;
DEMNAS is EPSG:4326, heights in metres above EGM2008).
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import shapely

from repeat_common import khg

SERVICE = "https://geoservices.big.go.id/raster/rest/services/DEMNAS/DEM_Indonesia/ImageServer"
SHEET = re.compile(r"(\d{4}-\d{2,3})")
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
BOUNDARY = ("batas_khg_indonesia.gpkg", "nama__khg__indonesia__2017__rev_tipologi", "NAMA_KHG")
PAUSE_S = 2.0


def parse_link(link):
    u = urllib.parse.urlparse(link.strip().strip('"').strip("'"))
    token = (urllib.parse.parse_qs(u.query).get("token") or [""])[0]
    if "unduh" not in u.path or not token:
        sys.exit("TANAHAIR_LINK is not an Ina-Geoportal DEMNAS download link with a token.")
    try:
        p = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))).get("exp")
        left = exp - time.time()
        if left <= 0:
            sys.exit("The token has expired: copy a fresh link.")
        print(f"token valid for about {left / 60:.0f} more minutes")
    except (IndexError, ValueError, TypeError):
        print("could not read the token expiry; continuing")
    return token, f"{u.scheme}://{u.netloc}{u.path}"


def sheets_for(geom):
    b = geom.bounds
    q = {"where": "Category = 1", "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
         "geometry": ",".join(map(str, b)), "spatialRel": "esriSpatialRelIntersects", "outFields": "Name",
         "returnGeometry": "true", "f": "json"}
    js = json.load(urllib.request.urlopen(f"{SERVICE}/query?{urllib.parse.urlencode(q)}", timeout=120))
    if "error" in js:
        sys.exit(f"tile index query failed: {js['error']}")
    out = {}
    for f in js.get("features", []):
        m = SHEET.search(str(f.get("attributes", {}).get("Name", "")))
        rings = f.get("geometry", {}).get("rings")
        if m and rings and m.group(1) not in out and shapely.Polygon(rings[0]).intersects(geom):
            out[m.group(1)] = True
    return sorted(out)


def download(endpoint, token, filename, dest, retries=3):
    url = f"{endpoint}?{urllib.parse.urlencode({'token': token, 'filename': filename})}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            break
        except OSError as e:
            if attempt == retries:
                tmp.unlink(missing_ok=True)
                raise SystemExit(f"{filename}: download failed ({type(e).__name__})")
            time.sleep(4 * attempt)
    with open(tmp, "rb") as fh:
        head = fh.read(300)
    if head[:4] not in TIFF_MAGIC:
        tmp.unlink()
        raise SystemExit(f"{filename}: not a GeoTIFF (token expired or sheet missing?): {head.decode('utf-8', 'replace')[:200]}")
    os.replace(tmp, dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", action="append", required=True, help="NAMA_KHG in batas_khg_indonesia.gpkg (repeatable)")
    ap.add_argument("--buffer-m", type=float, default=500.0)
    ap.add_argument("--version", default="v1.0")
    a = ap.parse_args()
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    link = os.environ.get("TANAHAIR_LINK") or sys.stdin.readline()
    token, endpoint = parse_link(link)
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    out = data / "dem" / "demnas_tiles"
    out.mkdir(parents=True, exist_ok=True)
    for k in a.khg:
        sel = b[b[BOUNDARY[2]].map(khg.norm) == khg.norm(k)]  # tolerant of spacing and case
        if sel.empty:
            sys.exit(f"KHG not found: {k}")
        geom = sel.to_crs(sel.estimate_utm_crs()).buffer(a.buffer_m).to_crs(4326).union_all()
        sheets = sheets_for(geom)
        print(f"{k}: {len(sheets)} sheets: {' '.join(sheets)}", flush=True)
        for i, s in enumerate(sheets, 1):
            name = f"DEMNAS_{s}_{a.version}.tif"
            dest = out / name
            if dest.exists() and dest.stat().st_size > 0:
                print(f"  {i}/{len(sheets)} {name} already there", flush=True)
                continue
            time.sleep(PAUSE_S)
            download(endpoint, token, name, dest)
            print(f"  {i}/{len(sheets)} {name} {dest.stat().st_size / 1e6:.0f} MB", flush=True)
    print("tiles in", out)


if __name__ == "__main__":
    main()
