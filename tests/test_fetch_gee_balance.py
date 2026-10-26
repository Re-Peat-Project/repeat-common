from datetime import date

import pytest

from repeat_common.fetch_gee_balance import image_end, khg_unit, periods, weights


def test_dekads_cover_the_month_without_gaps():
    p = periods(date(2024, 2, 1), date(2024, 3, 1), "dekad")
    assert p == [(date(2024, 2, 1), date(2024, 2, 11)), (date(2024, 2, 11), date(2024, 2, 21)), (date(2024, 2, 21), date(2024, 3, 1))]
    assert sum((b - a).days for a, b in p) == 29          # leap year


def test_periods_cross_the_year_and_stop_at_end():
    assert periods(date(2023, 12, 21), date(2024, 1, 15), "dekad")[-1] == (date(2024, 1, 11), date(2024, 1, 15))
    assert periods(date(2023, 11, 1), date(2024, 2, 1), "month")[1] == (date(2023, 12, 1), date(2024, 1, 1))
    with pytest.raises(ValueError):
        periods(date(2024, 1, 5), date(2024, 2, 1), "dekad")


def test_image_lengths():
    assert (image_end(date(2021, 12, 21), "dekad") - date(2021, 12, 21)).days == 11
    assert (image_end(date(2021, 12, 27), "mod16") - date(2021, 12, 27)).days == 5     # last composite of 2021
    assert (image_end(date(2024, 12, 26), "mod16") - date(2024, 12, 26)).days == 6     # leap year: doy 361 of 366
    assert image_end(date(2024, 2, 1), "month") == date(2024, 3, 1)


def test_a_period_equal_to_one_image_takes_its_value_once():
    w, cov = weights({"d1": date(2024, 2, 1)}, date(2024, 2, 1), date(2024, 2, 11), "dekad", "rate")
    assert w == {"d1": 10.0} and cov == 10                 # mean mm/day x 10 days
    w, cov = weights({"m": date(2024, 2, 1)}, date(2024, 2, 1), date(2024, 3, 1), "month", "total")
    assert w == {"m": 1.0} and cov == 29


def test_an_8_day_total_is_split_by_overlap():
    starts = {"a": date(2024, 1, 9), "b": date(2024, 1, 17)}       # 9-16 and 17-24 January
    w, cov = weights(starts, date(2024, 1, 11), date(2024, 1, 21), "mod16", "total")
    assert w == {"a": 6 / 8, "b": 4 / 8} and cov == 10
    w, cov = weights({"x": date(2024, 1, 1)}, date(2024, 1, 11), date(2024, 1, 21), "day", "total")
    assert w == {} and cov == 0


def test_whole_khg_unit_matches_names_with_different_spacing():
    import geopandas as gpd
    from shapely.geometry import box
    b = gpd.GeoDataFrame({"NAMA_KHG": ["KHG Sungai Pungurbesar -  Sungai Kapuas", "KHG Sungai Pungurbesar -  Sungai Kapuas", "KHG Other"]},
                         geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(5, 5, 6, 6)], crs=4326)
    u = khg_unit(b, "NAMA_KHG", {"name": "KHG Sungai Pungurbesar - Sungai Kapuas", "khg_code": "KHG.61.12-71.01"})
    assert list(u["unit_id"]) == ["KHG.61.12-71.01"] and u.geometry.iloc[0].area == pytest.approx(2.0)
    assert khg_unit(b, "NAMA_KHG", {"name": "KHG Missing", "khg_code": "x"}).empty
