from __future__ import annotations

from typing import Any

from hare.query.expressions import Aggregate, F


class ArgExtremum(Aggregate, abstract=True):
    """Base of ``ArgMin`` and ``ArgMax`` - the value of one expression in the row where another is the
    least or the greatest of its group.

    Args:
        field: The field or expression whose value is returned.
        by: The field or expression the row is chosen by.
    """

    populate_field_object = True
    computed_over_window = True
    #: A repeated row is the same row - the result is of one of them.
    ignores_repeated_rows = True

    def __init__(self, field: Any, by: Any, **kwargs: Any) -> None:
        super().__init__(field, F(by) if isinstance(by, str) else by, **kwargs)
