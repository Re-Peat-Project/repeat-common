# repeat-common

Shared code of the RE-PEAT modules (GitHub org Re-Peat-Project). A library: other modules import it, they do not exchange files with it. What is shared, and nothing a single module alone uses.

| Module | Content |
|---|---|
| `repeat_common.grid` | analysis grid, raster read/warp/write, vector to grid mask, UTM choice, units from KHG polygons |
| `repeat_common.sources` | open-data readers (cloud-native STAC/COG/Zarr): Sentinel-2 NDMI and NDVI, burned area, land cover, surface-water seasonality, TerraClimate deficit, CHIRPS, ERA5, FIRMS, cache building |
| `repeat_common.forcing` | daily rain and evaporation on local days (ERA5; CHIRPS and WaPOR as finer tests from the retired bucket experiments), per-sub-KHG weights. The neraca balance reads rain and ET from `fetch_gee_balance` |
| `repeat_common.khg` | per-KHG work folders: `khg.folder("<name or code>")` gives `_khg/<code>_<name>/`, `khg.step_dir(khg, step, dem)` gives `<step>/<dem>/` for the steps that depend on the DEM. Root `$REPEAT_KHG`, registry from `$REPEAT_HUB/sites/` |
| `repeat_common.gee` | Earth Engine bridge: an image to GeoTIFF on its native grid, chunked and resumable (`gee.init()`, `gee.to_geotiff`; `python -m repeat_common.gee --asset ... --khg ... --out ...`, for example DeltaDTM). Needs `EE_PROJECT` |
| `repeat_common.gee_s2` | dry-season Sentinel-2 NDMI and NDVI medians in Earth Engine, same method as `sources.ndmi_cloud`; parity test `scripts/check_ndmi_gee_parity.py --khg <code>` |
| `repeat_common.fetch_gee_balance` | rain and ET per sub-KHG per dekad (or month) from Earth Engine, for the neraca air: `python -m repeat_common.fetch_gee_balance --khg <name or code> --products chirps,wapor,mod16 --end 2026-10-01` (`--end` required, exclusive; `--start` 2018-01-01; `--products` default `chirps,wapor`). Products `chirps`, `wapor`, `mod16` (MOD16A2GF, gap-filled, ends 2025-12), `mod16nrt` (MOD16A2 without gap filling, from 2021), `imerg`, `terraclimate_pr`, `terraclimate_aet`. `--units` for a polygon file or Earth Engine table, `--whole-khg` for a KHG without sub-KHG units, `--dem` for a DEM variant's units. Writes `balance_<product>_<step>.csv` and `.json` and `units_landcover.csv` into `03_neraca/[<dem>/]inputs/`; years cached, so a failed run resumes |
| `repeat_common.fetch_demnas` | DEMNAS sheets from BIG Tanahair for a KHG into `dem/demnas_tiles/` (needs a fresh download link in `TANAHAIR_LINK`) |
| `repeat_common.fetch_open_dem` | open DEMs and layers for a KHG: `--product glo30 fabdem worldcover chm gedtm30` (default `glo30 fabdem`) into `dem/open/<product>/` (chm into `canopy/meta_chm_v1/`) |
| `repeat_common.fetch_icesat2` | ICESat-2 ATL08 ground heights for a KHG from SlideRule into `icesat2/` (`--t0`, `--t1`; needs `sliderule`) |
| `repeat_common.fetch_gedi` | GEDI L2A ground elevations for a KHG from Earth Engine into `gedi/` (`--min-sensitivity` 0.98) |
| `repeat_common.fetch_dpg_atlas` | BRG Atlas Desa Peduli Gambut PDFs into `dpg/atlas_brg/` and the village list `dpg/dpg_atlas_villages.csv` (needs `pypdf`) |
| `repeat_common.fetch_dpg_boundaries` | village boundaries for the atlas villages (KLH 2023, then BIG) into `dpg/dpg_villages_boundaries.gpkg` |
| `repeat_common.fetch_sitmat_layers` | map layers of the KLH SiTMAT portal: the 2025 peat map into `peat/lahan_gambut_indonesia_2025.gpkg` and the national DMPG village boundaries into `dpg/dmpg_villages_sitmat.gpkg` (raw GeoJSON kept in `raw/sitmat_api/`): `python -m repeat_common.fetch_sitmat_layers [--layers peat_2025 dmpg] [--refresh]` |
| `repeat_common.fetch_sipalaga` | what is open of SIPALAGA, the BRG station network, into `sipalaga/`: the 2019 hourly crawler CSVs (pinned commit of danylaksono/sipalaga-crawler), the station tables of Taufik et al. 2023 and Apers et al. 2022 (table B1), the three Mendeley Data sets of Taufik et al. 2023 (peat moisture, water retention, sample plots), and one station layer `sipalaga_stations.gpkg` with positions from SiTMAT, the articles or the named village: `python -m repeat_common.fetch_sipalaga [--refresh]` |
| `repeat_common.fetch_map_context` | locator inset layers (`basemap/locator_context.gpkg`) and the KLH/BPLH logo (`logos/`) for the print layouts |
| `repeat_common.tma` | SiTMAT / CSV water-table reader (canonical table `station_id, date, depth_m, lon, lat`, depth positive down) and per-unit TMA indicators |
| `repeat_common.fetch_firms` | download the FIRMS country archive (VIIRS S-NPP, yearly CSV) and combine it into one GeoPackage in `REPEAT_DATA`: `python -m repeat_common.fetch_firms`. One file at a time, with a pause and retries |
| `repeat_common.fetch_peat_extent` | build a peat-extent GeoTIFF (GFW global peatland extent, a composite; for Indonesia the union of Miettinen et al. 2016 and Gumbricht et al. 2017) from the public tile cache, only for tiles that touch a KHG: `python -m repeat_common.fetch_peat_extent` |
| `repeat_common.fetch_klhk_peat` | KLHK KHG and FEG 1:50,000 layers (BIG Satu Peta mirror; carries KHG set after SK.129/2017) into `klhk_khg_feg_50k.gpkg`: `python -m repeat_common.fetch_klhk_peat [--bbox XMIN YMIN XMAX YMAX]`. `fetch_peat_extent --extra klhk_khg_feg_50k.gpkg:khg` then adds their GFW tiles |
| `repeat_common.fetch_metar` | airport METAR (visibility, wind, smoke codes) from the Iowa Environmental Mesonet archive: `python -m repeat_common.fetch_metar --station WAOO`. One year per request, with a pause |
| `repeat_common.config` | `deep_update`, `expand_env` (`${REPEAT_DATA}` in configs) |
| `repeat_common.htmlkit` | shared parts of the HTML dashboards |

File formats the modules hand to each other are in the hub: `re-peat/docs/11-data-contracts.md`.

## Install

```bash
pip install -e ../repeat-common      # while developing a module next to it
pip install -e "../repeat-common[gee]"   # with the Earth Engine client (gee, gee_s2, fetch_gee_balance, fetch_gedi); then `earthengine authenticate`
```

Python 3.11 or newer. Tests: `python -m tests.test_config_env`, `python -m tests.test_htmlkit` (the second needs `node`), `python -m tests.test_fetch_peat_extent`, `python -m pytest tests/test_fetch_gee_balance.py`.

## Rules

- Move code here only when two modules use it. One-module code stays in that module.
- Changing a function's behaviour here changes every module; say so in the change description and re-run the Kampar-Gaung and Sugihan-Lumpur checks of the modules that use it.
- Logger names are `repeat_common.<module>`.
