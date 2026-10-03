"""The native Cast() gives what the Python one gives - and raises what it raises - for every value,
target and source of a grid."""

import pytest

from hare.dialects.sqlite.functions.comparison.sqlite_cast import SqliteCast
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.sql.enums import CastType

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

VALUES = [
    None, 0, 1, -1, 2**31 - 1, 2**31, -(2**31), 2**63 - 1, 2**70, 0.0, -0.0, 0.5, 1.5, 2.5, -2.5, 1e20, 1e300,
    float("inf"), "0", "12", " 12 ", "+12", "-12", "1.5", "2.5", "-2.5", "0.004", "-0.004", "0.005", "999.995",
    "-0", "-0.00", ".5", "5.", "1e3", "1E-3", "12e40", "abc", "", " ", "1_000", "١٢", "\x0b12", "true", "t", b"12",
    "2024-03-01", "2024-03-01 10:00:00+00:00", "10:00",
]  # fmt: skip
PARAMETERS = [
    (CastType.INTEGER, 16, None), (CastType.INTEGER, 32, None), (CastType.INTEGER, 64, None),
    (CastType.FLOAT, None, None), (CastType.DECIMAL, 10, 2), (CastType.DECIMAL, 5, 0), (CastType.DECIMAL, 4, 3),
    (CastType.DECIMAL, 20, 8), (CastType.TEXT, None, None), (CastType.TEXT, 3, None), (CastType.TEXT, 0, None),
    (CastType.BOOLEAN, None, None), (CastType.DATE, None, None),
]  # fmt: skip


def test_cast() -> None:
    native_cast = SqliteNativeFunctions.module.SqliteCast(SqliteCast.cast).cast
    for value in VALUES:
        for target, first_parameter, second_parameter in PARAMETERS:
            for source in CastType:
                arguments = (value, str(target), str(source), first_parameter, second_parameter, 1)
                try:
                    expected = SqliteCast.cast(*arguments)
                except Exception as error:  # noqa: BLE001 - the error is what is compared.
                    with pytest.raises(type(error)):
                        native_cast(*arguments)
                    continue
                result = native_cast(*arguments)
                assert result == expected, arguments
                assert type(result) is type(expected), arguments
