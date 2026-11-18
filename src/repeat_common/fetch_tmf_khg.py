"""JRC Tropical Moist Forest annual change (v1_2024), bands Dec2014 and Dec2024, for every KHG on the product's native grid.

    EE_PROJECT=<project> python -m repeat_common.fetch_tmf_khg [--workers 3]

Writes $REPEAT_DATA/forest/tmf_v1_2024/<khg_code>.tif (2 bands, uint8; 1 undisturbed forest, 2 degraded, 3 deforested,
4 regrowth, 5 water, 6 other land). Resumable: existing files are skipped. Used by repeat-prioritisation's
repeat-national-final-cover (config final.cover.tmf). Large KHG are fetched in tiles under Earth Engine's request limit.
"""
import os, sys, argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pyogrio, shapely
from repeat_common import gee
import ee

DATA = Path(os.environ["REPEAT_DATA"])
OUT = DATA / "forest" / "tmf_v1_2024"
ap = argparse.ArgumentParser(); ap.add_argument("--workers", type=int, default=3); a = ap.parse_args()
gee.init(os.environ.get("EE_PROJECT"))
k = pyogrio.read_dataframe(DATA / "derived" / "khg_klh_priority.gpkg", layer="khg", columns=["khg_code"])
col = ee.ImageCollection("projects/JRC/TMF/v1_2024/AnnualChanges")
img = col.mosaic().select(["Dec2014", "Dec2024"]).toUint8()
proj = col.first().projection().getInfo()
OUT.mkdir(parents=True, exist_ok=True)

def one(row):
    code, g = row
    dest = OUT / f"{code}.tif"
    if dest.exists() and not Path(str(dest) + ".progress").exists():
        return code, "skip"
    try:
        gee.to_geotiff(img, shapely.make_valid(g).buffer(0.002), dest, crs=proj["crs"], transform=proj["transform"], dtype="uint8", nodata=0,
                       source="projects/JRC/TMF/v1_2024/AnnualChanges (mosaic) bands Dec2014, Dec2024", max_bytes=12 * 1024 * 1024)
        return code, "ok"
    except Exception as e:
        return code, f"ERR {type(e).__name__}: {str(e)[:120]}"

rows = list(zip(k.khg_code, k.geometry.values))
done = 0
with ThreadPoolExecutor(a.workers) as ex:
    for code, st in ex.map(one, rows):
        done += 1
        if st.startswith("ERR") or done % 50 == 0:
            print(done, code, st, flush=True)
print("finished", done)
