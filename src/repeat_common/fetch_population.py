"""Fetch a WorldPop population grid of Indonesia into _data/population/.

    python -m repeat_common.fetch_population [DATA_DIR] [--product 2025_100m|2020_1km]

Products (WorldPop, www.worldpop.org; licence CC BY 4.0; each cell holds the estimated number of people in it, EPSG:4326):
  2025_100m  (default) Global 2015-2030 R2025A v1, 2025, constrained to built settlements, 3 arc-seconds (about 100 m);
             cite Bondarenko et al. (2025), doi:10.5258/SOTON/WP00839. About 170 MB.
  2020_1km   Global 2000-2020, 2020, UN-adjusted, 30 arc-seconds (about 1 km); cite WorldPop (2018), doi:10.5258/SOTON/WP00671.
A finer alternative, IHME Populace (40 m, quarterly from 2020), needs a personal registration and IHME's non-commercial
terms, so it is not fetched here.
Output: <DATA_DIR>/population/<file>.tif and source_<product>.json beside it (URL, date, size, sha256).
An existing file is not overwritten: rename it first.
"""
import argparse
import hashlib
import json
import os
import urllib.request
from datetime import date
from pathlib import Path

PRODUCTS = {
    "2025_100m": ("https://data.worldpop.org/GIS/Population/Global_2015_2030/R2025A/2025/IDN/v1/100m/constrained/idn_pop_2025_CN_100m_R2025A_v1.tif",
                  "Bondarenko et al. (2025), WorldPop R2025A, doi:10.5258/SOTON/WP00839"),
    "2020_1km": ("https://data.worldpop.org/GIS/Population/Global_2000_2020_1km_UNadj/2020/IDN/idn_ppp_2020_1km_Aggregated_UNadj.tif",
                 "WorldPop (2018), doi:10.5258/SOTON/WP00671"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_dir", nargs="?", default=os.environ.get("REPEAT_DATA", "../_data"))
    ap.add_argument("--product", choices=list(PRODUCTS), default="2025_100m")
    a = ap.parse_args()
    URL, cite = PRODUCTS[a.product]
    d = Path(a.data_dir) / "population"
    d.mkdir(parents=True, exist_ok=True)
    out = d / URL.rsplit("/", 1)[1]
    if out.exists():
        print(f"{out} exists: rename it to refetch")
        return
    tmp = out.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "RE-PEAT research"}), timeout=600) as r, open(tmp, "wb") as f:
        while b := r.read(1 << 20):
            f.write(b)
    tmp.rename(out)
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    (d / f"source_{a.product}.json").write_text(json.dumps({"url": URL, "fetched": str(date.today()), "bytes": out.stat().st_size, "sha256": sha,
                                               "licence": "CC BY 4.0", "citation": cite,
                                               "tool": "repeat_common.fetch_population"}, indent=1), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
