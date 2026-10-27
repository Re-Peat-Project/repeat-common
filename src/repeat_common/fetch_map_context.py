"""Fetch the context layers of the print layouts (locator inset and logo) into _data.

    python -m repeat_common.fetch_map_context [DATA_DIR]

Natural Earth 1:10 million, version on naciscdn.org (public domain, https://www.naturalearthdata.com/about/terms-of-use/):
countries (admin 0) and states/provinces (admin 1), clipped to Southeast Asia (90 to 145 E, 15 S to 12 N). The inset of the KLH
layouts draws the provinces of Indonesia with their names and the neighbouring countries over a sea colour, as the KLH sheets do.
Natural Earth draws de facto boundaries; the layouts carry the note that the map is not a reference for administrative boundaries.
Natural Earth has 33 Indonesian provinces (no Kalimantan Utara), so Indonesia's provinces come from geoBoundaries gbOpen IDN ADM1
(simplified; 34 provinces as of 2017, before the 2022 Papua split; source OpenStreetMap, licence ODbL 1.0: credit
"(c) OpenStreetMap contributors, geoBoundaries" on the map).
Output: <DATA_DIR>/basemap/locator_context.gpkg, layers countries (Natural Earth), provinces (Natural Earth, all countries) and
provinces_idn (geoBoundaries).

KLH/BPLH logo, two files (the logo's use is the ministry's to grant; these are for the ministry's own maps):
- klh_bplh_logo_kemenlh.png: the emblem without ring text, 276 px, from the header of https://kemenlh.go.id/
- klh_bplh_logo_ring_commons.png: the emblem with the ring text, as on the KLH map sheets, 1155 px, from Wikimedia Commons
  (File:Logo_Kementerian_Lingkungan_Hidup_-_Badan_Pengendalian_Lingkungan_Hidup_(2024).png, tagged CC0 by the uploader, cropped
  from a file of mediaplanner.id; not an official download)
Output: <DATA_DIR>/logos/. Files already present are kept.
"""
import argparse
import io
import os
import time
import urllib.request
import zipfile
from pathlib import Path

NE = "https://naciscdn.org/naturalearth/10m/cultural/{name}.zip"
LAYERS = {"countries": "ne_10m_admin_0_countries", "provinces": "ne_10m_admin_1_states_provinces"}
KEEP = {"countries": ["NAME", "NAME_ID", "ISO_A3", "SOVEREIGNT"], "provinces": ["name", "name_id", "admin", "iso_a2", "type"]}
BBOX = (90.0, -15.0, 145.0, 12.0)
GEOB = "https://www.geoboundaries.org/api/current/gbOpen/IDN/ADM1/"
# official Indonesian names of the geoBoundaries (English) province names
NAME_ID = {"Aceh": "Aceh", "Bali": "Bali", "Bangka-Belitung Islands": "Kepulauan Bangka Belitung", "Banten": "Banten", "Bengkulu": "Bengkulu", "Central Java": "Jawa Tengah", "Central Kalimantan": "Kalimantan Tengah", "Central Sulawesi": "Sulawesi Tengah", "East Java": "Jawa Timur", "East Kalimantan": "Kalimantan Timur", "East Nusa Tenggara": "Nusa Tenggara Timur", "Gorontalo": "Gorontalo", "Jakarta Special Capital Region": "DKI Jakarta", "Jambi": "Jambi", "Lampung": "Lampung", "Maluku": "Maluku", "North Kalimantan": "Kalimantan Utara", "North Maluku": "Maluku Utara", "North Sulawesi": "Sulawesi Utara", "North Sumatra": "Sumatera Utara", "Papua": "Papua", "Riau": "Riau", "Riau Islands": "Kepulauan Riau", "South Kalimantan": "Kalimantan Selatan", "South Sulawesi": "Sulawesi Selatan", "South Sumatra": "Sumatera Selatan", "Southeast Sulawesi": "Sulawesi Tenggara", "Special Region of Yogyakarta": "DI Yogyakarta", "West Java": "Jawa Barat", "West Kalimantan": "Kalimantan Barat", "West Nusa Tenggara": "Nusa Tenggara Barat", "West Papua": "Papua Barat", "West Sulawesi": "Sulawesi Barat", "West Sumatra": "Sumatera Barat"}
LOGOS = {"klh_bplh_logo_kemenlh.png": "https://kemenlh.go.id/assets/images/logo/logo-klh-plain.png",
         "klh_bplh_logo_ring_commons.png": "https://upload.wikimedia.org/wikipedia/commons/c/cf/"
                                           "Logo_Kementerian_Lingkungan_Hidup_-_Badan_Pengendalian_Lingkungan_Hidup_%282024%29.png"}
AGENT = "RE-PEAT map-context fetch (repeat-common)"
PAUSE_S = 2


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENT}), timeout=120) as r:
        return r.read()


def natural_earth(out: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import box
    dest = out / "basemap" / "locator_context.gpkg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for layer, name in LAYERS.items():
        print(f"  {name}")
        z = zipfile.ZipFile(io.BytesIO(_get(NE.format(name=name))))
        shp = next(n for n in z.namelist() if n.endswith(".shp"))
        tmp = out / "basemap" / "_tmp"
        tmp.mkdir(exist_ok=True)
        z.extractall(tmp)
        g = gpd.read_file(tmp / shp)
        g = g[[c for c in KEEP[layer] if c in g.columns] + ["geometry"]]
        g = g[g.intersects(box(*BBOX))].clip(box(*BBOX))
        g.to_file(dest, layer=layer, driver="GPKG")
        for f in tmp.iterdir():
            f.unlink()
        tmp.rmdir()
        print(f"    {len(g)} features")
        time.sleep(PAUSE_S)
    import json
    meta = json.loads(_get(GEOB))
    g = gpd.read_file(io.BytesIO(_get(meta["simplifiedGeometryGeoJSON"])))
    g = g.rename(columns={"shapeName": "name"})[["name", "geometry"]]
    g["name_id"] = g["name"].map(NAME_ID)
    missing = sorted(set(g.loc[g["name_id"].isna(), "name"]))
    if missing:
        raise ValueError(f"no Indonesian name for {missing}; add them to NAME_ID")
    g["source"] = f"geoBoundaries gbOpen IDN ADM1, {meta.get('boundaryYearRepresented')}, {meta.get('boundarySource')}, {meta.get('boundaryLicense')}"
    g.to_file(dest, layer="provinces_idn", driver="GPKG")
    print(f"  geoBoundaries IDN ADM1: {len(g)} provinces")
    return dest


def logos(out: Path) -> None:
    d = out / "logos"
    d.mkdir(parents=True, exist_ok=True)
    for name, url in LOGOS.items():
        if (d / name).exists():
            print(f"  have {name}")
            continue
        (d / name).write_bytes(_get(url))
        print(f"  {name}")
        time.sleep(PAUSE_S)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data_dir", nargs="?", default=os.environ.get("REPEAT_DATA", "_data"))
    a = ap.parse_args()
    out = Path(a.data_dir)
    print("Natural Earth:")
    print("  wrote", natural_earth(out))
    print("Logos:")
    logos(out)


if __name__ == "__main__":
    main()
