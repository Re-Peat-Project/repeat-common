"""Per-KHG work folders: _khg/<code>_<name>/ next to _data (see the workspace AGENTS.md, "Per-KHG work folders").

    from repeat_common import khg
    khg.folder("KHG Sungai Sugihan - Sungai Lumpur")   # -> <workspace>/_khg/16.02.01_sugihan_lumpur
    khg.folder("16.02.01")                               # same folder, by code

The code comes from re-peat/sites/khg_registry.csv (SK.129/MENLHK/2017) without the "KHG." prefix; the name part is the
KHG name in lower case without "KHG" and "Sungai", words joined by "_". The root is $REPEAT_KHG, else <workspace>/_khg; the registry is
read from $REPEAT_HUB/sites/, else <workspace>/re-peat/sites/.
"""
from __future__ import annotations

import csv
import os
import re
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[3]
REGISTRY = Path(os.environ.get("REPEAT_HUB") or WORKSPACE / "re-peat") / "sites" / "khg_registry.csv"   # REPEAT_HUB when installed outside the workspace
STEPS = ["00_inputs", "01_dem", "02_delineation", "03_neraca", "04_prioritisation", "05_pump", "cache", "logs"]


def root() -> Path:
    return Path(os.environ.get("REPEAT_KHG") or WORKSPACE / "_khg")


def norm(name: str) -> str:
    """Comparable form of a KHG name: lower case, single spaces, no 'KHG ' prefix."""
    return re.sub(r"\s+", " ", re.sub(r"^\s*KHG\s+", "", str(name), flags=re.I)).strip().lower()


def slug(name: str) -> str:
    s = re.sub(r"\bsungai\b", " ", norm(name))
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def registry() -> list[dict]:
    with open(REGISTRY, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def lookup(name_or_code: str) -> dict:
    """Registry row for a KHG name (any spacing or case) or code (with or without 'KHG.')."""
    rows = registry()
    key = str(name_or_code).strip()
    code = key[4:] if key.upper().startswith("KHG.") else key
    for r in rows:
        if r["khg_code"][4:] == code:
            return r
    hits = [r for r in rows if norm(r["name"]) == norm(key)]
    if len(hits) == 1:
        return hits[0]
    raise KeyError(f"KHG not found (or not unique) in {REGISTRY.name}: {name_or_code}")


def folder_name(name_or_code: str) -> str:
    r = lookup(name_or_code)
    return f"{r['khg_code'][4:]}_{slug(r['name'])}"


def folder(name_or_code: str, create: bool = False) -> Path:
    p = root() / folder_name(name_or_code)
    if create:
        for s in STEPS:
            (p / s).mkdir(parents=True, exist_ok=True)
    return p
