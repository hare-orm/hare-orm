from collections.abc import Callable
from typing import Any

import pytest


class FunctionComparison:
    """Value and error comparisons shared by SQLite native function tests."""

    @staticmethod
    def assert_equal(native_call: Callable[..., Any], python_call: Callable[..., Any], *arguments: Any) -> None:
        try:
            expected = python_call(*arguments)
        except Exception as error:  # noqa: BLE001 - the error is what is compared.
            with pytest.raises(type(error)):
                native_call(*arguments)
            return
        actual = native_call(*arguments)
        assert actual == expected, (arguments, actual, expected)

    @staticmethod
    def get_outcome(function: Callable[..., Any], *arguments: Any) -> tuple[str, Any]:
        try:
            result = function(*arguments)
        except Exception as error:
            return "error", type(error)
        return "value", (type(result), result)
