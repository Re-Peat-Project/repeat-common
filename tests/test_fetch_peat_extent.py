"""Tile arithmetic of repeat_common.fetch_peat_extent (no network): python -m tests.test_fetch_peat_extent"""
import math

import shapely

from repeat_common import fetch_peat_extent as F


def test_tile_xy():
    assert F.tile_xy(0, 0, 1) == (1.0, 1.0)                 # the origin is the corner of the four zoom-1 tiles
    x, y = F.tile_xy(-180, 85.0511287798, 3)
    assert abs(x) < 1e-9 and abs(y) < 1e-6                   # top-left of the Web Mercator world


def test_tile_box_and_needed():
    b = F.tile_box(1, 1, 1)                                  # south-east quadrant
    assert b.bounds[0] == 0 and b.bounds[2] == 180 and abs(b.bounds[1] + 85.0511) < 1e-3 and abs(b.bounds[3]) < 1e-9
    z = 6
    g = shapely.box(102.0, 0.5, 102.2, 0.7)                  # small box: one or two tiles
    tiles = F.needed_tiles([g], z)
    assert 1 <= len(tiles) <= 4
    for t in tiles:
        assert g.intersects(F.tile_box(*t, z))
    x, y = F.tile_xy(102.1, 0.6, z)
    assert (int(x), int(y)) in tiles


if __name__ == "__main__":
    test_tile_xy()
    test_tile_box_and_needed()
    print("ok")
