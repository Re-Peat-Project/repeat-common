"""Config helpers shared by every module: merge dicts, and expand ${NAME} from the environment."""
from __future__ import annotations

import os
import re


def deep_update(base: dict, upd: dict | None) -> dict:
    """Merge `upd` into `base` in place, descending into dicts; anything else is replaced."""
    for k, v in (upd or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base


def expand_env(obj):
    """Replace ${NAME} in every string of a config with the environment variable NAME, so a shared config can
    point at data kept outside the repo (e.g. ${REPEAT_DATA}/...). An unset variable stops with a clear message."""
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        missing = [m for m in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", obj) if m not in os.environ]
        if missing:
            raise SystemExit(f"config uses ${{{missing[0]}}} but the environment variable {missing[0]} is not set "
                             f"(value: {obj})")
        return os.path.expandvars(obj)
    return obj
