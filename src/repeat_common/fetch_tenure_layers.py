"""Fetch the public tenure-claim layers (social forestry, agrarian reform in forest areas, forest release, customary forest) and
the BRWA register of customary territories into _data/tenure/.

    python -m repeat_common.fetch_tenure_layers [--only NAME ...] [DATA_DIR]

Sources (all public, no login):
  BIG Satu Peta mirror of the Ministry of Forestry layers, https://kspservices.big.go.id/satupeta/rest/services/PUBLIK/
    hutan_desa   KEHUTANAN/13  Persetujuan Pengelolaan Hutan Desa (PPHD): village forest permits (LPHD, SK number and date, area)
    htr          KEHUTANAN/2   Hutan Tanaman Rakyat: community plantation forest permits (PPHTR)
    pptpkh       KEHUTANAN/11  Peta Indikatif Penyelesaian Penguasaan Tanah dalam rangka Penataan Kawasan Hutan: land held inside
                               the forest estate that is being settled (the forest-area part of TORA), with criterion (kriteria)
    pelepasan    KEHUTANAN/7   Peta Pelepasan Kawasan Hutan: forest estate released for other uses (plantations, transmigration)
    hutan_adat   KEHUTANAN/3   Hutan Adat (customary forest decrees); the mirror served 0 features on 2026-11-16, kept so a later
                               run picks them up
    iphps        PERIZINAN_DAN_PERTANAHAN/9  Izin Pemanfaatan Hutan Perhutanan Sosial (Java, Perhutani area)
  Not on the mirror: Hutan Kemasyarakatan (KEHUTANAN/12 fails to query; PPHKM is in the licence file
  landstatus/ALL_Updade__IZIN_USAHA_..._2025.gpkg), PIAPS, kemitraan konservasi and the newer releases on the Ministry's SIGAP
  (sigap.kehutanan.go.id: WMS only, WFS disabled; downloads need SIGAP's IGT download verification, a person's request).
  BRWA register, https://brwa.id/api/data/wa: the public table of registered customary territories (name, province, district,
  area, registration date, status, recognition policy). No geometry: BRWA serves its maps as vector tiles only; the polygons are
  available from BRWA on request.
Pages of 1000 features ordered by objectid, one request at a time with a pause. Output: <DATA_DIR>/tenure/<name>.gpkg (EPSG:4326,
all fields, layer <name>, plus a layer `source` with URL, service name, description, copyright, fetch date and feature count) and
<DATA_DIR>/tenure/brwa_wilayah_adat.csv. An existing file is not overwritten: rename it first.
"""
import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

BIG = "https://kspservices.big.go.id/satupeta/rest/services/PUBLIK/"
LAYERS = {
    "hutan_desa": "KEHUTANAN/MapServer/13",
    "htr": "KEHUTANAN/MapServer/2",
    "pptpkh": "KEHUTANAN/MapServer/11",
    "pelepasan": "KEHUTANAN/MapServer/7",
    "hutan_adat": "KEHUTANAN/MapServer/3",
    "iphps": "PERIZINAN_DAN_PERTANAHAN/MapServer/9",
}
BRWA = "https://brwa.id/api/data/wa"
PAGE = 1000
PAUSE_S = 2
UA = {"User-Agent": "Mozilla/5.0 (RE-PEAT research; repeat_common.fetch_tenure_layers)"}


def get(url, as_json=True):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
                b = r.read()
                return json.loads(b) if as_json else b
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            wait = 20 * 2 ** attempt
            print(f"  {type(e).__name__}; retry in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"gave up on {url}")


def fetch_layer(name, path, out):
    meta = get(BIG + path + "?f=json")
    n = get(BIG + path + "/query?" + urllib.parse.urlencode({"where": "1=1", "returnCountOnly": "true", "f": "json"})).get("count", 0)
    print(f"{name}: {meta.get('name')} - {n} features")
    parts = []
    for off in range(0, n, PAGE):
        q = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "geojson",
             "orderByFields": "objectid", "resultOffset": off, "resultRecordCount": PAGE}
        fc = get(BIG + path + "/query?" + urllib.parse.urlencode(q))
        if fc.get("features"):
            parts.append(gpd.GeoDataFrame.from_features(fc["features"], crs=4326))
        print(f"  {min(off + PAGE, n)}/{n}")
        time.sleep(PAUSE_S)
    gdf = pd.concat(parts, ignore_index=True) if parts else gpd.GeoDataFrame({"objectid": []}, geometry=[], crs=4326)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=4326).drop_duplicates(subset="objectid")
    if len(gdf) != n:
        print(f"  WARNING: {len(gdf)} features read, the service reports {n}")
    for c in gdf.columns:
        if c.startswith("tgl_") and pd.api.types.is_numeric_dtype(gdf[c]):
            gdf[c] = pd.to_datetime(gdf[c], unit="ms", errors="coerce").dt.date.astype(str)
    gdf.to_file(out, layer=name, driver="GPKG")
    src = pd.DataFrame([{"url": BIG + path, "service_name": meta.get("name"), "description": meta.get("description") or "",
                         "copyright": meta.get("copyrightText") or "", "fetched": str(date.today()), "features": len(gdf),
                         "service_count": n, "tool": "repeat_common.fetch_tenure_layers"}])
    gpd.GeoDataFrame(src, geometry=[None], crs=4326).to_file(out, layer="source", driver="GPKG")
    return len(gdf)


def strip(v):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(v))).strip() if isinstance(v, str) else v


def fetch_brwa(out):
    first = get(BRWA)
    total = int(first.get("recordsTotal", 0))
    rows = first.get("data", [])
    if len(rows) < total:      # DataTables endpoint: ask for everything at once
        rows = get(BRWA + "?" + urllib.parse.urlencode({"start": 0, "length": total, "draw": 1})).get("data", rows)
    d = pd.DataFrame(rows)
    for c in d.columns:
        d[c] = d[c].map(strip).map(lambda s: re.sub(r"\s+", " ", s) if isinstance(s, str) else s)
    if "luas" in d:
        d["luas_ha"] = pd.to_numeric(d["luas"].str.replace(".", "", regex=False).str.replace(",", ".", regex=False), errors="coerce")
    d["fetched"] = str(date.today())
    d.to_csv(out, index=False)
    print(f"brwa: {len(d)} of {total} registered customary territories")
    return len(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir", nargs="?", default=os.environ.get("REPEAT_DATA", "../_data"))
    ap.add_argument("--only", nargs="*", choices=list(LAYERS) + ["brwa"])
    a = ap.parse_args()
    d = Path(a.data_dir) / "tenure"
    d.mkdir(parents=True, exist_ok=True)
    for name in a.only or list(LAYERS) + ["brwa"]:
        out = d / ("brwa_wilayah_adat.csv" if name == "brwa" else f"{name}.gpkg")
        if out.exists():
            print(f"{out.name} exists: rename it to refetch")
            continue
        fetch_brwa(out) if name == "brwa" else fetch_layer(name, LAYERS[name], out)


if __name__ == "__main__":
    main()
