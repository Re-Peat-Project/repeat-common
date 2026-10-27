"""Village boundaries for the BRG Atlas DPG village list.

    python -m repeat_common.fetch_dpg_boundaries [DATA_DIR]

Input: <DATA_DIR>/dpg/dpg_atlas_villages.csv (from fetch_dpg_atlas).
Sources, in order:
  1. KLH village boundaries 2023, <DATA_DIR>/batas_desa_indonesia_2023_KLH.gpkg (names only, no codes), the closest in
     time to the 2018-2019 atlas.
  2. BIG village boundaries, edisi Juni 2026 (ArcGIS REST BATASWILAYAH/BATAS_DESAKEL_AR,
     https://geoservices.big.go.id/rbi/rest/services/BATASWILAYAH/BATAS_DESAKEL_AR/MapServer/0), for the villages source 1
     does not match. Asked per kabupaten for village names (attributes only, paged), then per OBJECTID for geometry.
Matching (names compared ignoring case, spaces and punctuation; first hit wins):
  exact                  desa and kabupaten (and kecamatan) agree
  exact_other_kecamatan  desa and kabupaten agree, kecamatan spelled differently
  exact_other_kabupaten  desa and kecamatan agree in the same province, kabupaten differs (atlas error or a moved village)
  prefix                 the atlas name is the leading words of a longer name in the same kecamatan (Papuyu I -> Papuyu I Sei. Pasanan)
  fuzzy_<ratio>          close spelling (difflib), ratio >= 0.80 within the kecamatan or >= 0.85 within the kabupaten; check by hand
Output: <DATA_DIR>/dpg/dpg_villages_boundaries.gpkg (layer dpg_villages: the atlas columns, source, match, the matched names
and id, polygons; unmatched villages are kept without geometry) and <DATA_DIR>/dpg/cache/big_desa/ (one JSON per BIG
request, so a rerun makes none). One BIG request at a time with a pause. Boundaries are from 2023 or 2026; villages split
or renamed since 2018-2019 can differ from the atlas village.
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import shape

KLH = "batas_desa_indonesia_2023_KLH.gpkg"
URL = "https://geoservices.big.go.id/rbi/rest/services/BATASWILAYAH/BATAS_DESAKEL_AR/MapServer/0/query"
ATTRS = "OBJECTID,WADMKD,WADMKC,WADMKK,WADMPR"
PAGE = 1000
CHUNK = 40
FUZZY_KEC, FUZZY_KAB = 0.80, 0.85
PAUSE_S = 1
PROV = {"Riau": "riau", "Jambi": "jambi", "Sumsel": "sumateraselatan", "Kalbar": "kalimantanbarat",
        "Kalteng": "kalimantantengah", "Kalsel": "kalimantanselatan"}


def norm(s):
    s = re.sub(r"\b(desa|kelurahan|kel|kampung)\b", " ", str(s).lower())
    return re.sub(r"[^a-z0-9]", "", s)


def kab_key(s):
    return norm(re.sub(r"^(kabupaten|kota)\s+", "", str(s).lower()))


def tokens(s):
    return re.findall(r"[a-z0-9]+", re.sub(r"\b(desa|kelurahan|kel|kampung)\b", " ", str(s).lower()))


def match(row, cands):
    """(id, how) of the best candidate (dicts with id, desa, kec, kab, prov), or (None, 'none')."""
    n, kk, kab = norm(row.desa), norm(row.kecamatan), kab_key(row.kabupaten)
    in_kab = [c for c in cands if kab_key(c["kab"]) == kab]
    exact = [c for c in in_kab if norm(c["desa"]) == n]
    if exact:
        same = [c for c in exact if norm(c["kec"]) == kk]
        if same:
            return same[0]["id"], "exact"
        return exact[0]["id"], "exact_other_kecamatan" if len(exact) == 1 else "ambiguous"
    other = [c for c in cands if norm(c["kec"]) == kk and c["prov"] == row.prov_key and norm(c["desa"]) == n]
    if len(other) == 1:
        return other[0]["id"], "exact_other_kabupaten"
    t = tokens(row.desa)
    pre = [c for c in in_kab if norm(c["kec"]) == kk and len(tokens(c["desa"])) > len(t) and tokens(c["desa"])[:len(t)] == t]
    if len(pre) == 1:
        return pre[0]["id"], "prefix"
    best = (0, None)
    for pool, floor in ((in_kab, FUZZY_KAB), ([c for c in in_kab if norm(c["kec"]) == kk], FUZZY_KEC)):
        for c in pool:
            r = difflib.SequenceMatcher(None, n, norm(c["desa"])).ratio()
            if r >= floor and r > best[0]:
                best = (r, c)
    if best[1]:
        return best[1]["id"], f"fuzzy_{best[0]:.2f}"
    return None, "none"


def klh_candidates(path):
    a = pyogrio.read_dataframe(path, read_geometry=False, fid_as_index=True)
    return [{"id": int(i), "desa": r.DESA_2023, "kec": r.KEC_2023, "kab": r.KABKO_2023, "prov": norm(r.PROV_2023)}
            for i, r in a.iterrows() if r.DESA_2023 and r.KABKO_2023]


def klh_geometries(path, ids):
    g = pyogrio.read_dataframe(path, fids=sorted(ids), fid_as_index=True)
    return {int(i): (r.geometry, {"desa": r.DESA_2023, "kec": r.KEC_2023, "kab": r.KABKO_2023}) for i, r in g.iterrows()}


def big_query(params, cache):
    key = cache / (hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest() + ".json")
    if key.exists():
        return json.loads(key.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({**params, "f": "json" if params.get("returnGeometry") == "false" else "geojson"})
    with urllib.request.urlopen(URL + "?" + body, timeout=180) as r:
        data = json.loads(r.read())
    if "error" in data:
        raise RuntimeError(data["error"])
    cache.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(data), encoding="utf-8")
    time.sleep(PAUSE_S)
    return data


def big_candidates(kab, cache):
    """BIG villages whose kabupaten name contains `kab` (attributes only); the matcher keeps the exact kabupaten."""
    rows, offset = [], 0
    where = "UPPER(WADMKK) LIKE '%" + kab.upper().replace("'", "''") + "%'"
    while True:
        d = big_query({"where": where, "outFields": ATTRS, "returnGeometry": "false", "resultOffset": offset,
                       "resultRecordCount": PAGE, "orderByFields": "OBJECTID"}, cache)
        rows += [{"id": f["attributes"]["OBJECTID"], "desa": f["attributes"]["WADMKD"], "kec": f["attributes"]["WADMKC"],
                  "kab": f["attributes"]["WADMKK"], "prov": norm(f["attributes"]["WADMPR"])} for f in d.get("features", [])]
        if not d.get("exceededTransferLimit"):
            return rows
        offset += PAGE


def big_geometries(ids, cache):
    out, ids = {}, sorted(ids)
    for k in range(0, len(ids), CHUNK):
        d = big_query({"where": "OBJECTID IN (" + ",".join(map(str, ids[k:k + CHUNK])) + ")", "outFields": ATTRS,
                       "returnGeometry": "true", "outSR": 4326}, cache)
        for f in d["features"]:
            p = f["properties"]
            out[p["OBJECTID"]] = (shape(f["geometry"]), {"desa": p["WADMKD"], "kec": p["WADMKC"], "kab": p["WADMKK"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    data = Path(ap.parse_args().data or os.environ["REPEAT_DATA"])
    cache = data / "dpg" / "cache" / "big_desa"
    df = pd.read_csv(data / "dpg" / "dpg_atlas_villages.csv")
    df["prov_key"] = df.province.map(PROV)

    klh = klh_candidates(data / KLH)
    res = {}
    for r in df.itertuples():
        i, how = match(r, klh)
        res[r.Index] = ("klh2023", i, how) if i is not None else None
    todo = [r for r in df.itertuples() if res[r.Index] is None]
    print(f"KLH 2023: {len(df) - len(todo)} matched, {len(todo)} to BIG")
    big = {kab: big_candidates(kab, cache) for kab in sorted({r.kabupaten for r in todo})}
    for r in todo:
        i, how = match(r, big[r.kabupaten])
        res[r.Index] = ("big2026", i, how)

    geoms = {"klh2023": klh_geometries(data / KLH, {v[1] for v in res.values() if v[0] == "klh2023"}),
             "big2026": big_geometries({v[1] for v in res.values() if v[0] == "big2026" and v[1] is not None}, cache)}
    geo, rows = [], []
    for idx in df.index:
        src, i, how = res[idx]
        g, nm = geoms[src].get(i, (None, {}))
        geo.append(g)
        rows.append({"boundary_source": src if i is not None else None, "boundary_match": how, "boundary_id": i,
                     "boundary_desa": nm.get("desa"), "boundary_kecamatan": nm.get("kec"),
                     "boundary_kabupaten": nm.get("kab")})
    out_df = pd.concat([df.drop(columns="prov_key"), pd.DataFrame(rows)], axis=1)
    gdf = gpd.GeoDataFrame(out_df, geometry=geo, crs=4326)
    out = data / "dpg" / "dpg_villages_boundaries.gpkg"
    gdf.to_file(out, layer="dpg_villages", driver="GPKG")
    print(f"{out}: {gdf.geometry.notna().sum()} of {len(gdf)} villages with a boundary")
    print(gdf.assign(m=gdf.boundary_match.str.replace(r"_\d\.\d+", "_*", regex=True))
          .groupby(["boundary_source", "m"], dropna=False).size().to_string())


if __name__ == "__main__":
    main()
