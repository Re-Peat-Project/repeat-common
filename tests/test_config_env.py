"""${NAME} in a config is replaced by the environment variable; an unset one stops with a clear message.

    python -m tests.test_config_env
"""
import os

from repeat_common.config import expand_env


def test_expands_nested_values():
    os.environ["REPEAT_TEST_ROOT"] = "D:/data"
    out = expand_env({"rasters": {"dtm": "${REPEAT_TEST_ROOT}/dtm.tif"}, "list": ["${REPEAT_TEST_ROOT}/a", 3], "n": 5})
    assert out == {"rasters": {"dtm": "D:/data/dtm.tif"}, "list": ["D:/data/a", 3], "n": 5}


def test_plain_strings_untouched():
    assert expand_env({"a": "data_local/x.gpkg", "b": None}) == {"a": "data_local/x.gpkg", "b": None}


def test_unset_variable_stops():
    os.environ.pop("REPEAT_TEST_MISSING", None)
    try:
        expand_env({"p": "${REPEAT_TEST_MISSING}/x"})
    except SystemExit as e:
        assert "REPEAT_TEST_MISSING" in str(e)
        return
    raise AssertionError("an unset variable was accepted")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
