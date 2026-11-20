from typing import Any

from tests.contracts.single_number_field import SingleNumberField


class SinglePassNumberField(SingleNumberField):
    """Keep a single-pass domain value intact until the write converts it."""

    def to_python(self, value: Any) -> Any:
        return value
