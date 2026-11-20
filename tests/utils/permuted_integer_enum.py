from enum import IntEnum


class PermutedIntegerEnum(IntEnum):
    """Numeric conversion is independent of an enum's declared stored codes."""

    FIRST = 1
    SECOND = 2

    def __int__(self) -> int:
        return 3 - self.value
