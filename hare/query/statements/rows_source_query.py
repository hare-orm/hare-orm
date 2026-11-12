from __future__ import annotations

from typing import Any

from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.awaitable_query import AwaitableQuery


class RowsSourceQuery(AwaitableQuery[Any], abstract=True):
    """A query of the rows another declaration selects - a summary or a write of them. It shares that
    declaration's settings by reference instead of copying them when it is made: a run on a plan
    only reads them, and a build works on a copy (``copy()``), which takes independent containers."""

    __slots__ = ()

    def __init__(
        self, source: QuerySpecification[Any], *, keeps_ordering: bool = False, keeps_grouping: bool = False
    ) -> None:
        """
        Args:
            source: The declaration whose rows the query reads.
            keeps_ordering: Keep the ordering - a slice is taken in it. Without it the ordering is
                kept only for the keyset boundaries.
            keeps_grouping: Keep the ``.group_by()`` fields.
        """
        SpecificationCopying.share_specification(source, self)
        self._configure_rows_source(source, keeps_ordering=keeps_ordering, keeps_grouping=keeps_grouping)
        self._init_build_state()
