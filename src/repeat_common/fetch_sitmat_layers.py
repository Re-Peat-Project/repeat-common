"""Fetch the map layers the SiTMAT portal (KLH) serves as GeoJSON into _data as GeoPackages.

    python -m repeat_common.fetch_sitmat_layers [--layers peat_2025 dmpg] [--refresh] [DATA_DIR]

Source: https://sitmat.kemenlh.go.id/api/<endpoint>, the layers behind the SiTMAT web map of the Kementerian Lingkungan Hidup.
Each endpoint returns the whole layer as one GeoJSON FeatureCollection in EPSG:4326 and states its feature count in the
x-total-count header; the fetch checks the count. The raw response and its headers are kept in
<DATA_DIR>/raw/sitmat_api/<endpoint>_<date>.json(.headers.txt) for provenance and reused unless --refresh; one request at a time.
Geometry is written as delivered (invalid polygons are kept, not repaired); the fetch date, endpoint and ETag go in the
GeoPackage metadata table `sitmat_source` of each file.

Layers:
  peat_2025  lahan-gambut-indonesia-2025 -> peat/lahan_gambut_indonesia_2025.gpkg, layer lahan_gambut
             peat and non-peat polygons (LAHAN_GAMB) by island, province and district (2023 names)
  dmpg       batas-desa-dmpg             -> dpg/dmpg_villages_sitmat.gpkg, layer dmpg_villages
             boundaries of the Desa Mandiri Peduli Gambut villages with programme (PROG_DMPG) and scheme and year (Skema_DPG)
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

URL = "https://sitmat.kemenlh.go.id/api/{endpoint}"
LAYERS = {
    "peat_2025": ("lahan-gambut-indonesia-2025", "peat/lahan_gambut_indonesia_2025.gpkg", "lahan_gambut"),
    "dmpg": ("batas-desa-dmpg", "dpg/dmpg_villages_sitmat.gpkg", "dmpg_villages"),
}
PAUSE_S = 2


def download(endpoint: str, raw: Path, refresh: bool) -> tuple[Path, dict]:
    """The newest cached response of `endpoint`, or a new one; returns its path and headers."""
    raw.mkdir(parents=True, exist_ok=True)
    cached = sorted(raw.glob(f"{endpoint}_*.json"))
    if cached and not refresh:
        f = cached[-1]
    else:
        f = raw / f"{endpoint}_{date.today().isoformat()}.json"
        req = urllib.request.Request(URL.format(endpoint=endpoint), headers={"Accept-Encoding": "identity"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=900) as r, open(f.with_suffix(".part"), "wb") as out:
                    hdr = dict(r.headers.items())
                    while chunk := r.read(1 << 20):
                        out.write(chunk)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(20 * 2 ** attempt)
        f.with_suffix(".part").replace(f)
        Path(str(f) + ".headers.txt").write_text("".join(f"{k}: {v}\n" for k, v in hdr.items()), encoding="utf-8")
        time.sleep(PAUSE_S)
    hdr_file = f.with_name(f.stem + ".headers.txt")
    hdr_file = hdr_file if hdr_file.exists() else Path(str(f) + ".headers.txt")
    hdr = {}
    if hdr_file.exists():
        for line in hdr_file.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition(":")
            hdr[k.strip().lower()] = v.strip()
    return f, hdr


def fetch(key: str, data: Path, refresh: bool = False) -> Path:
    endpoint, rel, layer = LAYERS[key]
    raw, hdr = download(endpoint, data / "raw" / "sitmat_api", refresh)
    g = gpd.read_file(raw)
    expected = int(hdr.get("x-total-count", len(g)))
    if len(g) != expected:
        raise RuntimeError(f"{endpoint}: {len(g)} features, the server announced {expected}")
    if g.crs is None:
        g = g.set_crs(4326)
    out = data / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.gpkg")
    g.to_file(tmp, layer=layer, driver="GPKG")
    meta = pd.DataFrame([{"endpoint": URL.format(endpoint=endpoint), "raw_file": str(raw.relative_to(data)).replace("\\", "/"),
                          "fetched": raw.stem.rsplit("_", 1)[-1], "etag": hdr.get("etag", ""), "features": len(g),
                          "invalid_geometries": int((~g.is_valid).sum())}])
    import pyogrio
    pyogrio.write_dataframe(meta, tmp, layer="sitmat_source", driver="GPKG")
    tmp.replace(out)
    print(f"{key}: {len(g)} features -> {out}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", default=os.environ.get("REPEAT_DATA", "../_data"))
    ap.add_argument("--layers", nargs="*", choices=list(LAYERS), default=list(LAYERS))
    ap.add_argument("--refresh", action="store_true", help="download again instead of reusing the newest raw response")
    a = ap.parse_args(argv)
    for k in a.layers:
        fetch(k, Path(a.data), a.refresh)


if __name__ == "__main__":
    main()
