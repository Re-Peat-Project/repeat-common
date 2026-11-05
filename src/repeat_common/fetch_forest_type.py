"""Forest type layers for one KHG from Earth Engine into _data/forest_type/, on their native grids (nothing resampled).

    python -m repeat_common.fetch_forest_type --khg "KHG Sungai Sugihan - Sungai Lumpur" [--buffer-m 2000] [DATA_DIR]

Writes:
  forest_type/forty_2020_v1_<slug>.tif   ForTy (Forest Typology) 2020 v1.0, Google DeepMind / WRI / IIASA (Sci. Data 2026,
      doi:10.1038/s41597-026-08299-5; Earth Engine projects/nature-trace/assets/forest_typology/forest_typology_2020_v1_0_collection).
      10 m UTM. Five uint8 bands, quantized probability 0-250 (x 0.004): PrimaryForest, NaturallyRegeneratingForest,
      PlantedForest, PlantationForest, TreeCropsAndAgroforestry; Other land = 250 - sum. Tiles are mosaicked on their common
      10 m UTM grid of the KHG's zone.
  forest_type/tmf_v1_2025_<slug>.tif     JRC Tropical Moist Forest v1_2025 (Vancutsem et al. 2021, doi:10.1126/sciadv.abe1603;
      projects/JRC/TMF/v1_2025): AnnualChanges Dec1990-Dec2025 (1 undisturbed, 2 degraded, 3 deforested incl. conversion to
      plantation, 4 regrowth, 5 water, 6 other), then DeforestationYear and TransitionMap_Subtypes. 0.00025 degree (~30 m),
      EPSG:4326.
Licences: ForTy "freely available ... under an open license" (DeepMind release note, 2026-10-22; exact licence not yet read
from the catalogue page); TMF free with attribution. Check both before redistribution.
"""
import argparse
import os
from pathlib import Path

from repeat_common import gee, khg

FORTY = "projects/nature-trace/assets/forest_typology/forest_typology_2020_v1_0_collection"
TMF = "projects/JRC/TMF/v1_2025/"
BOUNDARY = gee.BOUNDARY


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="?")
    ap.add_argument("--khg", required=True)
    ap.add_argument("--buffer-m", type=float, default=2000.0)
    ap.add_argument("--project")
    ap.add_argument("--only", choices=["forty", "tmf"])
    a = ap.parse_args()
    import geopandas as gpd
    data = Path(a.data or os.environ.get("REPEAT_DATA") or Path(__file__).resolve().parents[3] / "_data")
    b = gpd.read_file(data / BOUNDARY[0], layer=BOUNDARY[1])
    norm = lambda s: " ".join(str(s).split())
    sel = b[b[BOUNDARY[2]].map(norm) == norm(a.khg)]
    if sel.empty:
        raise SystemExit(f"KHG not found: {a.khg}")
    utm = sel.estimate_utm_crs()
    region = sel.to_crs(utm).buffer(a.buffer_m).to_crs(4326).union_all()
    slug = "_".join(w.capitalize() for w in khg.slug(a.khg).split("_"))
    ee = gee.init(a.project)
    geom = ee.Geometry.Rectangle(list(region.bounds))
    out = data / "forest_type"

    if a.only in (None, "forty"):
        col = ee.ImageCollection(FORTY).filterBounds(geom)
        crs = col.first().select(0).projection().getInfo()["crs"]
        # tiles share a 10 m grid aligned to whole metres; north-up transform on that grid
        gee.to_geotiff(col.mosaic().unmask(255), region, out / f"forty_2020_v1_{slug}.tif", crs=crs,
                       transform=[10, 0, 0, 0, -10, 10_000_000], dtype="uint8", nodata=255, source=FORTY,
                       max_bytes=15 * 1024 * 1024)   # Earth Engine returns the mosaic wider than uint8

    if a.only in (None, "tmf"):
        ac = ee.ImageCollection(TMF + "AnnualChanges").filterBounds(geom).mosaic()
        dy = ee.ImageCollection(TMF + "DeforestationYear").filterBounds(geom).mosaic().rename("DeforestationYear")
        ts = ee.ImageCollection(TMF + "TransitionMap_Subtypes").filterBounds(geom).mosaic().rename("TransitionMap_Subtypes")
        p = ee.ImageCollection(TMF + "AnnualChanges").filterBounds(geom).first().select(0).projection().getInfo()
        img = ac.addBands(dy).addBands(ts).toUint16().unmask(0)
        gee.to_geotiff(img, region, out / f"tmf_v1_2025_{slug}.tif", crs=p["crs"], transform=p["transform"],
                       dtype="uint16", nodata=0, source=TMF + "{AnnualChanges,DeforestationYear,TransitionMap_Subtypes}",
                       max_bytes=10 * 1024 * 1024)


if __name__ == "__main__":
    main()
