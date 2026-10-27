"""Dry-season Sentinel-2 NDMI and NDVI medians computed in Google Earth Engine, on a given grid: the same method as
``sources.ndmi_cloud`` (Planetary Computer, computed locally), so the work can run in Earth Engine instead.

Same method, step by step:
- Sentinel-2 L2A scenes of the months, scene cloud cover below ``max_cloud`` (Planetary Computer ``eo:cloud_cover`` ~ Earth Engine
  ``CLOUDY_PIXEL_PERCENTAGE``);
- the processing-baseline offset removed (Earth Engine ``COPERNICUS/S2_SR_HARMONIZED`` has it removed; locally 1000 is subtracted
  for baseline >= 04.00);
- tiles of one day mosaicked (local: ``groupby="solar_day"``);
- reflectance bands resampled bilinearly to the grid, SCL by nearest neighbour;
- pixels kept where SCL is in ``scl_keep`` and the bands are above 0;
- NDMI = (B8A - B11) / (B8A + B11), NDVI = (B8A - B04) / (B8A + B04), median over the days.

Whether the two routes give the same values is tested, not assumed: scripts/check_ndmi_gee_parity.py.
"""
from __future__ import annotations

import datetime

import numpy as np

COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"


def composite(ee, year: int, months: list[int], region, max_cloud: float = 60, scl_keep=(4, 5, 6, 7)):
    """The NDMI / NDVI median image (bands ndmi, ndvi)."""
    m0, m1 = min(months), max(months)
    start = datetime.date(year, m0, 1)
    end = datetime.date(year + (m1 == 12), m1 % 12 + 1, 1)          # exclusive: first day after the last month
    col = (ee.ImageCollection(COLLECTION).filterBounds(region).filterDate(str(start), str(end))
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud)))
    days = col.aggregate_array("system:time_start").map(lambda t: ee.Date(t).format("YYYY-MM-dd")).distinct()

    # resample each scene in its own projection, then mosaic: a mosaic has no native projection (Earth Engine gives it a 1 degree
    # default), so resampling after the mosaic would interpolate over about a degree
    col = col.map(lambda i: i.select(["B8A", "B11", "B4"]).toFloat().resample("bilinear").addBands(i.select("SCL"))
                  .copyProperties(i, ["system:time_start"]))

    def per_day(d):
        d = ee.Date.parse("YYYY-MM-dd", d)
        im = col.filterDate(d, d.advance(1, "day")).mosaic()
        refl = im.select(["B8A", "B11", "B4"])
        scl = im.select("SCL")
        nir, swir, red = refl.select("B8A"), refl.select("B11"), refl.select("B4")
        keep = scl.remap(list(scl_keep), [1] * len(scl_keep), 0).eq(1)
        ndmi = nir.subtract(swir).divide(nir.add(swir)).updateMask(keep.And(nir.gt(0)).And(swir.gt(0))).rename("ndmi")
        ndvi = nir.subtract(red).divide(nir.add(red)).updateMask(keep.And(nir.gt(0)).And(red.gt(0))).rename("ndvi")
        return ndmi.addBands(ndvi)

    return ee.ImageCollection(days.map(per_day)).median()


def to_grid(ee, image, crs: str, transform, shape: tuple, bands=("ndmi", "ndvi"), tile: int = 1024) -> dict:
    """The image computed on the grid (crs, affine transform, (rows, cols)), in tiles: {band: float32 array, NaN = no data}."""
    rows, cols = shape
    out = {b: np.full(shape, np.nan, dtype="float32") for b in bands}
    for r0 in range(0, rows, tile):
        for c0 in range(0, cols, tile):
            h, w = min(tile, rows - r0), min(tile, cols - c0)
            x0, y0 = transform * (c0, r0)
            req = {"expression": image.select(list(bands)).unmask(-9999), "fileFormat": "NUMPY_NDARRAY",
                   "grid": {"dimensions": {"width": w, "height": h}, "crsCode": crs,
                            "affineTransform": {"scaleX": transform.a, "shearX": 0, "translateX": x0, "shearY": 0, "scaleY": transform.e,
                                                "translateY": y0}}}
            arr = ee.data.computePixels(req)
            for b in bands:
                v = arr[b].astype("float32")
                v[v <= -9998] = np.nan
                out[b][r0:r0 + h, c0:c0 + w] = v
    return out
