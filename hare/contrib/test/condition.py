from typing import Any


class Condition:
    """Base class for a value-matcher usable on the right side of an ``==`` comparison in test
    assertions (e.g. inside a dict compared against test data)."""

    def __init__(self, value: Any) -> None:
        self.value = value
