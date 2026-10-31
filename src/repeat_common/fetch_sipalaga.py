"""Fetch what is openly available of SIPALAGA, the BRG peat water monitoring network, into _data/sipalaga.

    python -m repeat_common.fetch_sipalaga [--refresh] [DATA_DIR]

SIPALAGA (Sistem Pemantauan Air Lahan Gambut, Badan Restorasi Gambut, sipalaga.brg.go.id) logged groundwater level, soil
moisture and rain hourly at BRG stations coded BRG_<6-digit area code>_<nn>. The portal no longer serves data. Three open
sources remain, all fetched here:

  crawler   github.com/danylaksono/sipalaga-crawler, pinned to commit CRAWLER_SHA: the daily PDF reports of 2019-02-19 to
            2019-03-17 converted to one CSV per province (hourly gwater, smoisture, precip and status per station; no
            coordinates) and the download URL lists per province. PDFs are not fetched.
            -> crawler_2019/csv/<PROVINCE>.csv, crawler_2019/url/url_<prov>.txt
  taufik2023  Taufik et al. 2023, Data in Brief 46, 108889 (doi:10.1016/j.dib.2023.108889, CC BY 4.0): table 1 lists 21 BRG
            stations in Riau, Jambi and South Sumatra with code, KHG, longitude and latitude (two decimals) and moisture
            statistics; read from the Europe PMC full text (PMC9936326).        -> stations_taufik2023.csv
  mendeley  the article's three Mendeley Data sets (CC BY 4.0): monthly peat moisture of the 21 stations, 2018-10 to 2019-12
            (27y943wbkw); water retention at pF 1, 2, 2.54 and 4.2 of 105 samples from Riau (jj35xcx98v) and the sample
            plot coordinates (9stvfxystv).                                       -> mendeley/<dataset>/<file>
  apers2022 Apers et al. 2022, J. Adv. Model. Earth Syst. 14, e2021MS002784 (doi:10.1029/2021MS002784), table B1: the
            Indonesian SIPALAGA (59) and SATREPS sites with code, village, coordinates to 0.001 degree, drained or undrained
            and record period; read from the Europe PMC full text (PMC9285420).  -> stations_apers2022.csv

Then one station table is built, sipalaga_stations.gpkg (layer stations, one point per station code), from every station in
the three sources and in the SiTMAT station list ($SITMAT_PROCESSED/stations.csv), where community stations named BRG_...
are former SIPALAGA stations. Coordinates are taken from SiTMAT (5 decimals), then Apers 2022 (3 decimals), then Taufik 2023
(2 decimals), then from
the village the crawler names (representative point of the KLH 2023 village polygon matched on village and district name,
sub-district breaking ties); coord_source says which. SiTMAT points at 0,0 or outside the station's province
are not used (coord_note). Each station gets the KHG it falls in (batas_khg_indonesia.gpkg).
Files already present are reused unless --refresh. One request at a time.
"""
import argparse
import html
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import geopandas as gpd
import pandas as pd

CRAWLER = "https://raw.githubusercontent.com/danylaksono/sipalaga-crawler/{sha}/{path}"
CRAWLER_SHA = "c5bb2f17a6589786d80be0beb78795579a4d9efa"
CRAWLER_PROVINCES = ["JAMBI", "KALIMANTAN_BARAT", "KALIMANTAN_SELATAN", "KALIMANTAN_TENGAH", "PAPUA", "RIAU", "SUMATERA_SELATAN"]
CRAWLER_URLS = ["jambi", "kalbar", "kalsel", "kalteng", "papua", "riau", "sumsel"]
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9936326/fullTextXML"
EPMC_APERS = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9285420/fullTextXML"
MENDELEY = "https://data.mendeley.com/public-api/datasets/{id}{tail}"
MENDELEY_SETS = {"27y943wbkw": "peat moisture, 21 stations", "jj35xcx98v": "water retention, Riau",
                 "9stvfxystv": "retention sample locations"}
PROVINCE_CODE = {"14": "RIAU", "15": "JAMBI", "16": "SUMATERA SELATAN", "61": "KALIMANTAN BARAT", "62": "KALIMANTAN TENGAH",
                 "63": "KALIMANTAN SELATAN", "91": "PAPUA", "94": "PAPUA"}
CODE = re.compile(r"BRG_(\d{6})_(\d{2})")
PAUSE_S = 2


def get(url: str, out: Path, refresh: bool) -> Path:
    if out.exists() and not refresh:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "repeat-common"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    out.with_suffix(out.suffix + ".part").write_bytes(data)
    out.with_suffix(out.suffix + ".part").replace(out)
    time.sleep(PAUSE_S)
    return out


def fetch_crawler(d: Path, refresh: bool):
    for p in CRAWLER_PROVINCES:
        get(CRAWLER.format(sha=CRAWLER_SHA, path=f"csv/{p}.csv"), d / "crawler_2019" / "csv" / f"{p}.csv", refresh)
    for p in CRAWLER_URLS:
        get(CRAWLER.format(sha=CRAWLER_SHA, path=f"url/url_{p}.txt"), d / "crawler_2019" / "url" / f"url_{p}.txt", refresh)
    (d / "crawler_2019" / "SOURCE.txt").write_text(
        f"https://github.com/danylaksono/sipalaga-crawler commit {CRAWLER_SHA}\n", encoding="utf-8")


def fetch_mendeley(d: Path, refresh: bool):
    for ds in MENDELEY_SETS:
        meta = d / "mendeley" / ds / "dataset.json"
        get(MENDELEY.format(id=ds, tail="?version=1"), meta, refresh)
        files = get(MENDELEY.format(id=ds, tail="/files?folder_id=root&version=1"), d / "mendeley" / ds / "files.json", refresh)
        for f in json.loads(files.read_text(encoding="utf-8")):
            get(f["content_details"]["download_url"], d / "mendeley" / ds / f["filename"], refresh)


def fetch_taufik_table(d: Path, refresh: bool) -> Path:
    xml = get(EPMC, d / "raw" / "taufik2023_PMC9936326.xml", refresh).read_text(encoding="utf-8")
    clean = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
    for tw in re.findall(r"<table-wrap.*?</table-wrap>", xml, re.S):
        rows = [[clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)] for r in re.findall(r"<tr.*?</tr>", tw, re.S)]
        if rows and rows[0][:3] == ["No", "Code", "Peat Hydrological Unit"]:
            body = [r for r in rows if r and r[0].isdigit()]
            t = pd.DataFrame(body, columns=["no", "code", "khg", "lon", "lat", "province", "vwc_mean_pct", "vwc_min_pct", "vwc_max_pct"])
            out = d / "stations_taufik2023.csv"
            t.drop(columns="no").to_csv(out, index=False)
            return out
    raise RuntimeError("station table not found in the Taufik 2023 full text")


def fetch_apers_table(d: Path, refresh: bool) -> Path:
    """Indonesian SIPALAGA and SATREPS sites of table B1, Apers et al. 2022 (code, village, coordinates to 0.001 degree)."""
    xml = get(EPMC_APERS, d / "raw" / "apers2022_PMC9285420.xml", refresh).read_text(encoding="utf-8")
    clean = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
    for tw in re.findall(r"<table-wrap.*?</table-wrap>", xml, re.S):
        lab = re.search(r"<label>(.*?)</label>", tw)
        if not (lab and clean(lab.group(1)) == "Table B1"):
            continue
        rows = [[clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)] for r in re.findall(r"<tr.*?</tr>", tw, re.S)]
        rows = [[c.replace("‐", "-").replace("–", "-") for c in r] for r in rows]
        body = [r for r in rows[1:] if r[7] in ("SIPALAGA", "SATREPS") and r[0].startswith("IN_")]
        num = lambda s: float(s.replace(" ", "").replace("−", "-"))
        t = pd.DataFrame([{"code": r[0][3:].upper(), "site": r[1].split(";")[0].strip(), "province": r[1].split(";")[-1].strip(),
                           "lon": num(r[2]), "lat": num(r[3]), "drained": r[4], "period": r[5], "source": r[7], "land_cover": r[8]}
                          for r in body])
        out = d / "stations_apers2022.csv"
        t.to_csv(out, index=False)
        return out
    raise RuntimeError("table B1 not found in the Apers 2022 full text")


def read_crawler(d: Path) -> pd.DataFrame:
    parts = []
    for p in CRAWLER_PROVINCES:
        f = d / "crawler_2019" / "csv" / f"{p}.csv"
        parts.append(pd.read_csv(f, dtype=str, on_bad_lines="skip"))
    return pd.concat(parts, ignore_index=True)


def key(s) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(s).lower())
    return re.sub(r"^(kabupaten|kab|kota)", "", s)


def village_names(location: str) -> list[str]:
    """Village names to try for a crawler location: as given, then without a site prefix (PS1-, ST-) and station suffix."""
    n = re.sub(r"^[A-Z0-9]+-", "", str(location))
    m = re.sub(r"(_?\(SM\))|(_?\d+)$", "", n)
    return list(dict.fromkeys([location, n, m]))


def build_stations(d: Path, data: Path) -> Path:
    crawl = read_crawler(d)
    crawl = crawl[crawl.station.notna()]                            # BRG_ codes and older names (BRG9, LUMPUR1, ...)
    cs = crawl.groupby("station").agg(location=("location", "first"), subdistrict=("subdistrict", "first"),
                                      district=("district", "first"), crawler_province=("province", "first"),
                                      crawler_hours=("time", "size"), crawler_start=("date", "min"), crawler_end=("date", "max"))
    urls = set()
    for f in (d / "crawler_2019" / "url").glob("url_*.txt"):
        urls |= {c.upper() for c in re.findall(r"in_kode=([^&\s]+)", f.read_text(encoding="utf-8"))}
    tf = pd.read_csv(d / "stations_taufik2023.csv").set_index("code")
    ap = pd.read_csv(d / "stations_apers2022.csv").set_index("code")
    sit_f = Path(os.environ.get("SITMAT_PROCESSED", data.parent / "repeat-sitmat/sitmat_out/processed")) / "stations.csv"
    sit = pd.read_csv(sit_f) if sit_f.exists() else pd.DataFrame(columns=["name", "station_id", "lon", "lat", "kind"])
    sit = sit[sit.name.fillna("").str.fullmatch(CODE)].drop_duplicates("name").set_index("name")

    codes = sorted(set(cs.index) | urls | set(tf.index) | set(ap.index) | set(sit.index))
    s = pd.DataFrame({"code": codes})
    s["area_code"] = s.code.str.extract(CODE)[0]
    s["in_crawler_csv"] = s.code.isin(cs.index)
    s["in_crawler_urls"] = s.code.isin(urls)
    s["in_taufik2023"] = s.code.isin(tf.index)
    s["in_apers2022"] = s.code.isin(ap.index)
    s["in_sitmat"] = s.code.isin(sit.index)
    s = s.join(cs, on="code").join(tf[["khg", "lon", "lat"]].rename(columns={"khg": "taufik_khg", "lon": "taufik_lon", "lat": "taufik_lat"}), on="code")
    s = s.join(ap[["site", "province", "lon", "lat"]].rename(columns={"site": "apers_site", "province": "apers_province",
                                                                       "lon": "apers_lon", "lat": "apers_lat"}), on="code")
    s = s.join(sit[["station_id", "lon", "lat"]].rename(columns={"station_id": "sitmat_station_id", "lon": "sitmat_lon", "lat": "sitmat_lat"}), on="code")
    s["province"] = (s.area_code.str[:2].map(PROVINCE_CODE).fillna(s.crawler_province.str.replace("_", " "))
                     .fillna(s.apers_province.str.upper().replace({"CENTRAL KALIMANTAN": "KALIMANTAN TENGAH",
                             "WEST KALIMANTAN": "KALIMANTAN BARAT", "SOUTH KALIMANTAN": "KALIMANTAN SELATAN",
                             "SOUTH SUMATRA": "SUMATERA SELATAN"})))

    vil = gpd.read_file(data / "batas_desa_indonesia_2023_KLH.gpkg", columns=["PROV_2023", "KABKO_2023", "KEC_2023", "DESA_2023"])
    vil = vil[vil.PROV_2023.str.upper().isin(set(PROVINCE_CODE.values()))]
    prov_shapes = vil.dissolve("PROV_2023").geometry
    prov_shapes.index = prov_shapes.index.str.upper()

    # SiTMAT point, kept when it lies in the province the code starts with
    s["coord_note"] = ""
    ok = pd.Series(False, index=s.index)
    for i, r in s[s.sitmat_lon.notna()].iterrows():
        pt = gpd.points_from_xy([r.sitmat_lon], [r.sitmat_lat], crs=4326)[0]
        if r.sitmat_lon == 0 and r.sitmat_lat == 0:
            s.at[i, "coord_note"] = "SiTMAT point at 0,0"
        elif r.province not in prov_shapes.index or not prov_shapes[r.province].contains(pt):
            s.at[i, "coord_note"] = f"SiTMAT point outside {r.province}"
        else:
            ok[i] = True
    s["lon"] = s.sitmat_lon.where(ok)
    s["lat"] = s.sitmat_lat.where(ok)
    s["coord_source"] = pd.Series("sitmat", index=s.index).where(ok, "")
    t = s.lon.isna() & s.apers_lon.notna()
    s.loc[t, ["lon", "lat"]] = s.loc[t, ["apers_lon", "apers_lat"]].to_numpy()
    s.loc[t, "coord_source"] = "apers2022"
    t = s.lon.isna() & s.taufik_lon.notna()
    s.loc[t, ["lon", "lat"]] = s.loc[t, ["taufik_lon", "taufik_lat"]].to_numpy()
    s.loc[t, "coord_source"] = "taufik2023"

    # village the crawler names: desa + kabupaten, kecamatan breaks ties
    vil["k"] = vil.DESA_2023.map(key) + "|" + vil.KABKO_2023.map(key)
    vil["kc"] = vil.KEC_2023.map(key)
    pts = vil.to_crs(6933).representative_point().to_crs(4326)
    for i, r in s[s.lon.isna() & s.location.notna()].iterrows():
        m = vil[vil.k.isin([key(n) + "|" + key(r.district) for n in village_names(r.location)])]
        if len(m) > 1:
            m = m[m.kc == key(r.subdistrict)] if (m.kc == key(r.subdistrict)).any() else m
        if len(m) == 1:
            p = pts[m.index[0]]
            s.loc[i, ["lon", "lat", "coord_source"]] = [p.x, p.y, "village"]
        else:
            s.at[i, "coord_note"] = (s.at[i, "coord_note"] + "; " if s.at[i, "coord_note"] else "") + \
                f"village {r.location} ({r.district}): {len(m)} matches"

    g = gpd.GeoDataFrame(s, geometry=gpd.points_from_xy(s.lon, s.lat), crs=4326)
    khg = gpd.read_file(data / "batas_khg_indonesia.gpkg", columns=["NAMA_KHG"])
    g = g.join(gpd.sjoin(g[g.lon.notna()], khg, predicate="within")[["NAMA_KHG"]].groupby(level=0).first())
    g = g.rename(columns={"NAMA_KHG": "khg"})
    for c in ["in_crawler_csv", "in_crawler_urls", "in_taufik2023", "in_apers2022", "in_sitmat"]:
        g[c] = g[c].astype(int)
    out = d / "sipalaga_stations.gpkg"
    tmp = out.with_name(out.stem + ".tmp.gpkg")
    g.to_file(tmp, layer="stations", driver="GPKG")
    tmp.replace(out)
    print(f"{len(g)} station codes, {int(g.lon.notna().sum())} with coordinates -> {out}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?", default=os.environ.get("REPEAT_DATA", "../_data"))
    ap.add_argument("--refresh", action="store_true", help="download again instead of reusing files on disk")
    a = ap.parse_args(argv)
    data = Path(a.data)
    d = data / "sipalaga"
    fetch_crawler(d, a.refresh)
    fetch_mendeley(d, a.refresh)
    fetch_taufik_table(d, a.refresh)
    fetch_apers_table(d, a.refresh)
    build_stations(d, data)


if __name__ == "__main__":
    main()
