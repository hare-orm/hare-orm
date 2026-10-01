"""The native text functions give what the Python ones give - and raise what they raise - for every
argument of a grid: texts with characters past the BMP, negative and oversized lengths, non-text
values."""

import itertools

import pytest

from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.text import SqliteTextFunctions

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

TEXTS = [None, "", "a", "abc", "héllo wörld", "😀x😀", 12, 1.5, b"bytes"]
COUNTS = [None, -10, -2, -1, 0, 1, 2, 3, 10, "2", 1.9, 0x1F600, 0xD800, 0x110000]


def call_both(native_call, python_call, arguments):
    try:
        expected = python_call(*arguments)
    except Exception as error:  # noqa: BLE001 - the error is what is compared.
        with pytest.raises(type(error)):
            native_call(*arguments)
        return
    assert native_call(*arguments) == expected, arguments


def test_text_functions() -> None:
    functions = SqliteTextFunctions.get_functions()
    native = SqliteNativeFunctions.module.TextFunctions(functions)
    grids = {
        "left": itertools.product(TEXTS, COUNTS),
        "right": itertools.product(TEXTS, COUNTS),
        "substr": [*itertools.product(TEXTS, COUNTS), *itertools.product(TEXTS, COUNTS, COUNTS)],
        "lpad": itertools.product(TEXTS, COUNTS, [None, "", "xy", "😀"]),
        "rpad": itertools.product(TEXTS, COUNTS, [None, "", "xy", "😀"]),
        "repeat": itertools.product(TEXTS, COUNTS),
        "reverse": [(text,) for text in TEXTS],
        "chr": [(count,) for count in COUNTS],
        "ascii": [(text,) for text in TEXTS],
    }
    for name in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512"):
        grids[name] = [(text,) for text in TEXTS]
    for name, arguments_grid in grids.items():
        native_function, python_function = getattr(native, name), functions[name][1]
        for arguments in arguments_grid:
            call_both(native_function, python_function, arguments)
