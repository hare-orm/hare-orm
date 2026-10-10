from __future__ import annotations

import collections.abc
import itertools
from collections.abc import Iterable
from typing import Any

from hare.contrib.factories.field_declarations.declaration import Declaration


class Iterator(Declaration):
    """The values of an iterable, one per object - from its start again once it ends::

        status = Iterator(["draft", "published"])

    Args:
        values: The values.
        cycle: False raises ``StopIteration`` once the values end.
    """

    def __init__(self, values: Iterable[Any], *, cycle: bool = True) -> None:
        self.values: collections.abc.Iterator[Any]
        if cycle:
            self.values = itertools.cycle(values)
        else:
            self.values = iter(values)

    def evaluate(self, sequence_number: int) -> Any:
        return next(self.values)
