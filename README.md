# repeat-common

Shared code of the RE-PEAT modules (GitHub org Re-Peat-Project). A library: other modules import it, they do not exchange files with it. What is shared, and nothing a single module alone uses.

| Module | Content |
|---|---|
| `repeat_common.grid` | analysis grid, raster read/warp/write, vector to grid mask, UTM choice, units from KHG polygons |
| `repeat_common.sources` | open-data readers (cloud-native STAC/COG/Zarr): Sentinel-2 NDMI, burned area, land cover, CHIRPS, ERA5, FIRMS, cache building |
| `repeat_common.forcing` | daily rain and evaporation on local days (ERA5; CHIRPS and WaPOR as finer tests), per-sub-KHG weights |
| `repeat_common.tma` | SiTMAT / CSV water-table reader (canonical table `station_id, date, depth_m, lon, lat`, depth positive down) and per-unit TMA indicators |
| `repeat_common.config` | `deep_update`, `expand_env` (`${REPEAT_DATA}` in configs) |
| `repeat_common.htmlkit` | shared parts of the HTML dashboards |

File formats the modules hand to each other are in the hub: `re-peat/docs/11-data-contracts.md`.

## Install

```bash
pip install -e ../repeat-common      # while developing a module next to it
```

Python 3.11 or newer. Tests: `python -m tests.test_config_env`, `python -m tests.test_htmlkit` (the second needs `node`).

## Rules

- Move code here only when two modules use it. One-module code stays in that module.
- Changing a function's behaviour here changes every module; say so in the change description and re-run the Kampar-Gaung and Sugihan-Lumpur checks of the modules that use it.
- Logger names are `repeat_common.<module>`.
