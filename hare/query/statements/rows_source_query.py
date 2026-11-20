from __future__ import annotations

from typing import Any

from hare.query.queryset.options.query_options import QueryOptions
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

    def _configure_rows_source(
        self, source: QuerySpecification[Any], *, keeps_ordering: bool, keeps_grouping: bool
    ) -> None:
        """Keep only settings selecting source rows, after copying or sharing its declaration.

        Group names are checked before being removed. Cursor boundaries still need the
        source ordering even when the caller does not otherwise retain it. Containers
        describing returned relations are replaced, never cleared on the source.
        """
        self._single = False
        self._raise_does_not_exist = False
        self._prefetch_map = {}
        self._prefetch_queries = {}
        self._select_related = set()
        self._selection = None
        self._combination = None
        options = source._options
        if options is not QueryOptions.DEFAULT:
            if not keeps_grouping and options.group_bys:
                source._raise_if_group_names_invalid()
            removed_options = QueryOptions.ROW_RETURN_SETTINGS
            if not keeps_grouping:
                removed_options += ("group_bys",)
            self._options = options.without(*removed_options)
        if not keeps_ordering and not (options.cursor_values or options.before_cursor_values):
            self._orderings = []
