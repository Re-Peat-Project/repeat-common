"""Fetch the BRG Atlas Desa Peduli Gambut (DPG, later Desa Mandiri Peduli Gambut) into _data.

    python -m repeat_common.fetch_dpg_atlas [DATA_DIR]

Source: Badan Restorasi Gambut, Atlas Desa Peduli Gambut, one PDF per province (Riau, Jambi, Sumsel, Kalbar, Kalteng, Kalsel),
hosted by Djoko Luknanto (UGM) at https://luk.staff.ugm.ac.id/rawa/BRG/atlas/ (introduction: isi.html). The atlases hold
participatory village maps as images, not vector data. Also fetched: the cover JPG per province and the village index JPG
(indeks/). No national DPG/DMPG vector layer was found online on 2026-10-26; on 2026-10-28 the SiTMAT portal was found to serve one
(repeat_common.fetch_sitmat_layers). The Sumsel provincial GeoNode
(datagambut.sumselprov.go.id, layers geonode:BRGM_DPG2017..2021) lists vectors but its WFS fails server side.
Files already present with the server's size are skipped. One request at a time with a pause.
After the download, the village list is read from each atlas: the table of contents (Daftar Isi) gives desa, kecamatan,
kabupaten and atlas page; the village cover page at that atlas page gives the participatory mapping date.
Each village is then matched by name to the Bluebook target village polygons (<DATA_DIR>/desa_target_pemulihan_ekosistem_gambut.gpkg)
on desa + kabupaten, ignoring case, spaces and punctuation; kecamatan breaks ties. Matched rows get the polygon's
OBJECTID in target_objectid; unmatched rows have none and need another boundary source.
Output: <DATA_DIR>/dpg/atlas_brg/ (files) and <DATA_DIR>/dpg/dpg_atlas_villages.csv (one row per village).
Needs pypdf.
"""
import argparse
import os
import re
import time
import urllib.request
from pathlib import Path

import pandas as pd

BASE = "https://luk.staff.ugm.ac.id/rawa/BRG/atlas/"
PROVINCES = ["1-DPG-Riau", "2-DPG-Jambi", "3-DPG-Sumsel", "4-DPG-Kalbar", "5-DPG-Kalteng", "6-DPG-Kalsel"]
FILES = [f"{p}{ext}" for p in PROVINCES for ext in (".pdf", ".jpg")] + [f"indeks/{p}.jpg" for p in PROVINCES]
PAUSE_S = 2


def fetch(name, out):
    url = BASE + name
    dest = out / name
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
        size = int(r.headers.get("Content-Length", -1))
    if dest.exists() and dest.stat().st_size == size:
        print(f"  have {name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)
    print(f"  {name}: {dest.stat().st_size / 1e6:.1f} MB")
    time.sleep(PAUSE_S)


ENTRY = re.compile(r"^((?:Desa|Kelurahan|Kel\.|Kampung)\s.*?)\s*\.{3,}\s*(\d+)?\s*$")
MAPPED = re.compile(r"Pemetaan Partisipatif\s+Desa Peduli Gambut,?\s*([A-Za-z]*,?\s*(?:19|20)\d\d)")


def villages(pdf):
    """Village rows of one atlas: TOC entries paired in order with the TOC page numbers, mapping date from the cover page."""
    from pypdf import PdfReader
    pages = [p.extract_text() or "" for p in PdfReader(pdf).pages]
    names, nums = [], []
    for t in pages[:15]:
        if "Daftar Isi" not in t:
            continue
        lines, pending = [l.strip() for l in t.splitlines() if l.strip()], ""
        for l in lines[2:]:
            if re.fullmatch(r"\d+", l):
                nums.append(int(l))
                continue
            m = ENTRY.match(f"{pending} {l}".strip())
            if m:
                names.append(m.group(1))
                if m.group(2):
                    nums.append(int(m.group(2)))
                pending = ""
            else:   # entry wrapped onto the next line
                pending = f"{pending} {l}".strip()
    if len(names) != len(nums):
        raise RuntimeError(f"{pdf.name}: {len(names)} TOC entries but {len(nums)} page numbers")
    dates = {}
    for t in pages:
        m, n = MAPPED.search(re.sub(r"\s+", " ", t)), re.search(r"^\s*(\d+)\s*$", t, re.M)
        if m and n:
            dates.setdefault(int(n.group(1)), re.sub(r"[,\s]+", " ", m.group(1)).strip())
    rows = []
    for name, page in zip(names, nums):
        parts = [s.strip() for s in name.split(",")]
        unit, desa = parts[0].split(" ", 1)
        if desa.startswith("Kelurahan "):   # "Desa Kelurahan Terkul" in the Riau TOC
            unit, desa = desa.split(" ", 1)
        rows.append({"province": pdf.stem.split("-", 2)[2], "unit": unit.rstrip("."), "desa": desa,
                     "kecamatan": parts[1] if len(parts) > 1 else "", "kabupaten": ", ".join(parts[2:]),
                     "atlas_page": page, "mapped": next((dates[p] for p in range(page, page - 4, -1) if p in dates), ""),
                     "file": pdf.name})
    return rows


def match_targets(df, gpkg):
    """target_objectid of the Bluebook target village with the same desa and kabupaten (kecamatan breaks ties)."""
    import pyogrio
    key = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    v = pyogrio.read_dataframe(gpkg, columns=["OBJECTID", "DESA_2023", "KEC_2023", "KABKO_2023"], read_geometry=False)
    v["k"] = v["DESA_2023"].map(key) + "|" + v["KABKO_2023"].map(key)
    ids = []
    for r in df.itertuples():
        c = v[v["k"] == key(r.desa) + "|" + key(r.kabupaten)]
        if len(c) > 1:
            c = c[c["KEC_2023"].map(key) == key(r.kecamatan)]
        ids.append(int(c["OBJECTID"].iloc[0]) if len(c) == 1 else pd.NA)
    return df.assign(target_objectid=pd.array(ids, dtype="Int64"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", nargs="?")
    a = ap.parse_args()
    data = a.data or os.environ.get("REPEAT_DATA")
    if not data:
        raise SystemExit("give the data folder as an argument or set REPEAT_DATA")
    out = Path(data) / "dpg" / "atlas_brg"
    for name in FILES:
        fetch(name, out)
    df = match_targets(pd.DataFrame([r for p in PROVINCES for r in villages(out / f"{p}.pdf")]),
                       Path(data) / "desa_target_pemulihan_ekosistem_gambut.gpkg")
    df.to_csv(out.parent / "dpg_atlas_villages.csv", index=False)
    print(f"-> {out}, {len(df)} villages ({df['target_objectid'].notna().sum()} matched to target villages)"
          f" -> {out.parent / 'dpg_atlas_villages.csv'}")


if __name__ == "__main__":
    main()
