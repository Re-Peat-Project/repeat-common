# Sentinel-2 NDMI/NDVI: Earth Engine against Planetary Computer, KHG Sungai Mendahara - Sungai Batanghari

`python scripts/check_ndmi_gee_parity.py --khg 15.05-07-10.01 --dem lidar --years 2015 2023`; months 8, 9, 10; grid of the cached composites (2332 x 2432 cells). Local: sources.ndmi_cloud (Planetary Computer, computed on this machine). Earth Engine: gee_s2.composite (same method).

## Per year and band

| year | band | valid_local | valid_gee | note | gee_seconds | valid_both | only_local | only_gee | mean_abs_diff | p95_abs_diff | max_abs_diff | r |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2015 | ndmi | 5212782 | 0 | no scenes in Earth Engine | nan | nan | nan | nan | nan | nan | nan | nan |
| 2015 | ndvi | 5212782 | 0 | no scenes in Earth Engine | nan | nan | nan | nan | nan | nan | nan | nan |
| 2016 | ndmi | 4590586 | 96198 | nan | 33.8000 | 96082.0000 | 4494504.0000 | 116.0000 | 0.1029 | 0.2582 | 0.9442 | 0.5560 |
| 2016 | ndvi | 4590586 | 96198 | nan | 33.8000 | 96082.0000 | 4494504.0000 | 116.0000 | 0.3058 | 0.5461 | 0.7791 | 0.1066 |
| 2017 | ndmi | 3891826 | 78230 | nan | 35.6000 | 78218.0000 | 3813608.0000 | 12.0000 | 0.0925 | 0.3095 | 0.7854 | 0.5571 |
| 2017 | ndvi | 3891826 | 78230 | nan | 35.6000 | 78218.0000 | 3813608.0000 | 12.0000 | 0.1334 | 0.2761 | 1.3403 | 0.3270 |
| 2018 | ndmi | 5661049 | 1101348 | nan | 35.4000 | 1101347.0000 | 4559702.0000 | 1.0000 | 0.0454 | 0.1594 | 0.9049 | 0.8834 |
| 2018 | ndvi | 5661049 | 1101348 | nan | 35.4000 | 1101347.0000 | 4559702.0000 | 1.0000 | 0.0618 | 0.1826 | 0.8015 | 0.9818 |
| 2019 | ndmi | 5653836 | 5660933 | nan | 78.0000 | 5651639.0000 | 2197.0000 | 9294.0000 | 0.0079 | 0.0243 | 0.5786 | 0.9910 |
| 2019 | ndvi | 5653836 | 5660933 | nan | 78.0000 | 5651639.0000 | 2197.0000 | 9294.0000 | 0.0113 | 0.0436 | 0.6148 | 0.9955 |
| 2020 | ndmi | 5648245 | 5648900 | nan | 73.3000 | 5648235.0000 | 10.0000 | 665.0000 | 0.0054 | 0.0162 | 0.4455 | 0.9978 |
| 2020 | ndvi | 5648245 | 5648900 | nan | 73.3000 | 5648235.0000 | 10.0000 | 665.0000 | 0.0075 | 0.0251 | 0.4252 | 0.9989 |
| 2021 | ndmi | 5558257 | 5623337 | nan | 64.8000 | 5557447.0000 | 810.0000 | 65890.0000 | 0.0209 | 0.0801 | 0.9636 | 0.9556 |
| 2021 | ndvi | 5558220 | 5623337 | nan | 64.8000 | 5557413.0000 | 807.0000 | 65924.0000 | 0.0414 | 0.1640 | 1.1817 | 0.9651 |
| 2022 | ndmi | 5405398 | 5407989 | nan | 55.4000 | 5404439.0000 | 959.0000 | 3550.0000 | 0.0065 | 0.0186 | 0.7877 | 0.9937 |
| 2022 | ndvi | 5405141 | 5407716 | nan | 55.4000 | 5404153.0000 | 988.0000 | 3563.0000 | 0.0110 | 0.0421 | 0.5944 | 0.9972 |
| 2023 | ndmi | 5659943 | 5660353 | nan | 71.9000 | 5658868.0000 | 1075.0000 | 1485.0000 | 0.0077 | 0.0234 | 1.4182 | 0.9921 |
| 2023 | ndvi | 5659943 | 5660353 | nan | 71.9000 | 5658868.0000 | 1075.0000 | 1485.0000 | 0.0122 | 0.0476 | 0.7938 | 0.9961 |

## Per sub-KHG (29 units of the lidar variant), baseline mean NDMI (u_ndmi)

- Largest difference of a unit mean: 0.0202; mean 0.0084
- Spearman correlation of the unit order: 0.9700
- Units on the other side of the dryness threshold (0.3): 0
- Earth Engine time per year: 60 s (median)

Table per unit: `$REPEAT_KHG/15.05-07-10.01_mendahara_batanghari/cache/gee_parity/ndmi_parity_units.csv`.

## Conclusion (2026-10-27)

Earth Engine does not reproduce the Planetary Computer composites for the whole period, so the prioritisation keeps its Python
route (`sources.ndmi_cloud`, Planetary Computer):

- 2015: no Sentinel-2 L2A scenes in Earth Engine (`COPERNICUS/S2_SR_HARMONIZED`) over the KHG; 2016-2017: few, pixel correlation 0.56;
  2018: 0.88. Planetary Computer serves the reprocessed L2A archive for those years.
- 2019-2023: close but not identical (correlation 0.96-0.998, mean absolute NDMI difference 0.005-0.02; 2021 the weakest), from
  differences in the scene sets of the two archives.
- Per sub-KHG, the baseline NDMI (u_ndmi, 2016-2023 here) keeps every unit on the same side of the dryness threshold (0.30), but the
  order of the units correlates at 0.97, not 1: the ranking, and so the sub-KHG selected for sekat, could change.
- Time: about 35-80 s per year in Earth Engine against about 2.5 min locally; a hybrid (Planetary Computer before 2019, Earth Engine
  after) would save about 8 min per KHG at the cost of small differences in the ranking.

An earlier version of `gee_s2.composite` resampled after the daily mosaic; a mosaic has no native projection in Earth Engine, so
values were interpolated over about a degree (pixel correlation 0.24). Resampling each scene before the mosaic fixed it.
