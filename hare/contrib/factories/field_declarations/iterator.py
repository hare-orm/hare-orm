from __future__ import annotations

import collections.abc
import itertools
from collections.abc import Iterable
from typing import Any

from hare.contrib.factories.field_declarations.declaration import Declaration
from hare.exceptions import ConfigurationError


class Iterator(Declaration):
    """The values of an iterable, one per object - from its start again once it ends::

        status = Iterator(["draft", "published"])

    Args:
        values: The values.
        cycle: False raises ``ConfigurationError`` for an object made once the values ended.
    """

    def __init__(self, values: Iterable[Any], *, cycle: bool = True) -> None:
        self.values: collections.abc.Iterator[Any]
        if cycle:
            self.values = itertools.cycle(values)
        else:
            self.values = iter(values)

    def evaluate(self, sequence_number: int) -> Any:
        try:
            return next(self.values)
        except StopIteration:
            # A StopIteration leaving the coroutine making the object is a RuntimeError of its own.
            raise ConfigurationError(
                "Iterator(cycle=False) has no value left - every one of its values is taken by an object already"
            ) from None
