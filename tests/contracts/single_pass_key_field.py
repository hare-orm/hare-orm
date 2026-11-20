from typing import Any

from tests.contracts.single_number_field import SingleNumberField
from tests.contracts.single_pass_key import SinglePassKey


class SinglePassKeyField(SingleNumberField):
    """Read a fresh domain key while retaining supplied keys until binding."""

    field_type = SinglePassKey

    def to_python(self, value: Any) -> SinglePassKey | None:
        return value if value is None or isinstance(value, SinglePassKey) else SinglePassKey(value)
