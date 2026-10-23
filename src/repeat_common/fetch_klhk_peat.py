"""Fetch the KLHK peat hydrological units (KHG) and peat ecosystem function (FEG 1:50,000) into _data as one GeoPackage.

    python -m repeat_common.fetch_klhk_peat [--bbox XMIN YMIN XMAX YMAX] [DATA_DIR]

Source: the BIG Satu Peta mirror of the KLHK layers (producer: Direktorat Pengendalian Kerusakan Ekosistem Gambut, Ditjen PPKL),
https://kspservices.big.go.id/satupeta/rest/services/PUBLIK/SUMBER_DAYA_ALAM_DAN_LINGKUNGAN/MapServer, layer 37 "Peta Kesatuan
Hidrologis Gambut" (field kode_khg) and layer 48 "Peta Fungsi Ekosistem Gambut Skala 1:50.000" (kode_khg, peat_thick, tnh_gambut,
feg_peat, feg_50k). The FEG layer lists its legal basis as SK.295/2017, SK.296/2019, SK.938/2019, SK.446/2020, SK.270/2022 and
SK.1152/2022, so it carries KHG set after SK.129/2017 (for example KHG.63.01-03-71-72.01, Sungai Maluka - Sungai Martapura).
The KLHK hosts (sippeg/pkgppkl.menlhk.go.id) no longer resolve. Pages of 1000 features, one request at a time with a pause.
Output: <DATA_DIR>/klhk_khg_feg_50k.gpkg, EPSG:4326, layers khg and feg_50k (all fields kept). Without --bbox it takes all of Indonesia.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd

URL = "https://kspservices.big.go.id/satupeta/rest/services/PUBLIK/SUMBER_DAYA_ALAM_DAN_LINGKUNGAN/MapServer/{layer}/query"
LAYERS = {"khg": 37, "feg_50k": 48}
PAGE = 1000
PAUSE_S = 2


def get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            wait = 20 * 2 ** attempt
        print(f"  retry in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"gave up on {url}")


def fetch(layer, bbox):
    q = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "geojson",
         "orderByFields": "objectid_1", "resultRecordCount": PAGE}
    if bbox:
        q.update(geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope", inSR=4326,
                 spatialRel="esriSpatialRelIntersects")
    parts, offset = [], 0
    while True:
        d = get(URL.format(layer=layer) + "?" + urllib.parse.urlencode({**q, "resultOffset": offset}))
        if "error" in d:
            raise RuntimeError(d["error"])
        feats = d.get("features", [])
        if feats:
            parts.append(gpd.GeoDataFrame.from_features(feats, crs=4326))
        print(f"  layer {layer}: {offset + len(feats)} features", flush=True)
        if len(feats) < PAGE:
            break
        offset += PAGE
        time.sleep(PAUSE_S)
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"))
    a = ap.parse_args()
    data = a.data or os.environ.get("REPEAT_DATA")
    if not data:
        raise SystemExit("give the data folder as an argument or set REPEAT_DATA")
    out = Path(data) / "klhk_khg_feg_50k.gpkg"
    for name, layer in LAYERS.items():
        g = fetch(layer, a.bbox)
        g.to_file(out, layer=name, driver="GPKG")
        print(f"{name}: {len(g)} features -> {out}")
        time.sleep(PAUSE_S)


if __name__ == "__main__":
    main()
