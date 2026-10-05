"""The native GREATEST/LEAST pick the argument the Python ones pick - the first of equals, NULLs
skipped, numbers and decimal text compared as numbers - and raise what they raise."""

import itertools

import pytest

from hare.dialects.sqlite.functions.comparison.sqlite_greatest_least import SqliteGreatestLeast
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

ARGUMENTS = [None, 0, 1, -1, 2**70, 0.5, 1.0, -2.5, "1", "1.0", "0.50", "-3", "1e2", "abc", "b", "", b"1"]


def test_greatest_and_least() -> None:
    functions = SqliteNativeFunctions.module.GreatestLeast(SqliteGreatestLeast.pick)
    for comparison_type in ("number", "text"):
        for size in (1, 2, 3):
            for values in itertools.product(ARGUMENTS, repeat=size):
                for is_greatest, native in ((True, functions.greatest), (False, functions.least)):
                    try:
                        expected = SqliteGreatestLeast.pick(is_greatest, comparison_type, *values)
                    except Exception as error:  # noqa: BLE001 - the error is what is compared.
                        with pytest.raises(type(error)):
                            native(comparison_type, *values)
                        continue
                    result = native(comparison_type, *values)
                    assert result is expected or (result == expected and type(result) is type(expected)), (
                        comparison_type,
                        values,
                    )
