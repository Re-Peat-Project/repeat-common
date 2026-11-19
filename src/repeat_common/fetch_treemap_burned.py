"""TheTreeMap / Nusantara Atlas annual burned-area maps of Indonesia from Sentinel-2, 20 m (Zenodo record 19104282, CC BY 4.0).

    python -m repeat_common.fetch_treemap_burned [--years 2023 2024]

Writes $REPEAT_DATA/burned/treemap/IDN_BurnedAreaAnnual_<year>_TTM.zip, unpacks it and records source.json (sha256, licence, citation).
Pixel value = number of months the pixel was mapped as burned in the year (0 = not burned).
Cite: Gaveau DLA, Descals A, Salim MA (2026) Nationwide monthly burned area monitoring in Indonesia using Sentinel-2.
PLoS One 21(4): e0331831. https://doi.org/10.1371/journal.pone.0331831
"""
import argparse
import hashlib
import json
import os
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

RECORD = "19104282"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", nargs="*", type=int, default=[2023, 2024])
    a = ap.parse_args(argv)
    out = Path(os.environ["REPEAT_DATA"]) / "burned" / "treemap"
    out.mkdir(parents=True, exist_ok=True)
    rec = json.loads(urllib.request.urlopen(f"https://zenodo.org/api/records/{RECORD}", timeout=60).read())
    files = {f["key"]: f for f in rec["files"]}
    meta = {"record": f"https://zenodo.org/records/{RECORD}", "licence": rec["metadata"].get("license", {}).get("id"),
            "citation": "Gaveau DLA, Descals A, Salim MA (2026) PLoS One 21(4): e0331831", "fetched": str(date.today()), "files": {}}
    for y in a.years:
        key = f"IDN_BurnedAreaAnnual_{y}_TTM.zip"
        dest = out / key
        if not dest.exists():
            urllib.request.urlretrieve(files[key]["links"]["self"], dest)
        h = hashlib.sha256(dest.read_bytes()).hexdigest()
        with zipfile.ZipFile(dest) as z:
            z.extractall(out)
            names = z.namelist()
        meta["files"][key] = {"sha256": h, "zenodo_checksum": files[key].get("checksum"), "contents": names}
        print(key, h[:12], names)
    (out / "source.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
