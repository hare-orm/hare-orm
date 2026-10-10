from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.lookups.filter_operators import FilterOperators
from hare.dialects.sqlite.functions.json.sqlite_json_equality import SqliteJsonEquality
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.sqlite_posix_regex import SqlitePosixRegex
from hare.dialects.sqlite.lookups.constants import SQLITE_FULL_TEXT_SEARCH_UNSUPPORTED_REASON
from hare.dialects.sqlite.lookups.in_list.sqlite_large_in_list import SqliteLargeInList
from hare.dialects.sqlite.lookups.json.sqlite_json_lookups import SqliteJsonLookups
from hare.query.filters import JsonLookups, Lookups
from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.sqlite.search.sqlite_text_search import SqliteTextSearch
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.query.lookup_info.lookup_info import LookupInfo


class SqliteFilterOperators(FilterOperators):
    """SQLite's lookup operators - ``__search`` takes the searched field, whose full-text index it
    matches through."""

    @classmethod
    def build(cls, dialect: Dialect) -> SqliteFilterOperators:
        """The SQLite lookup operators.

        Args:
            dialect: The dialect the operators run lookups for.

        Returns:
            The operators.
        """
        return cls(
            dialect,
            {
                Lookups.posix_regex: SqlitePosixRegex.posix_regex,
                Lookups.insensitive_posix_regex: SqlitePosixRegex.insensitive_posix_regex,
                JsonLookups.contains: SqliteJsonLookups.contains,
                JsonLookups.equal: SqliteJsonEquality.equal,
                JsonLookups.not_equal: SqliteJsonEquality.not_equal,
                JsonLookups.is_in: SqliteJsonEquality.is_in,
                JsonLookups.not_in: SqliteJsonEquality.not_in,
                JsonLookups.contained_by: SqliteJsonLookups.contained_by,
                JsonLookups.filter: SqliteJsonLookups.filter,
                JsonLookups.has_key: SqliteJsonLookups.has_key,
                JsonLookups.has_keys: SqliteJsonLookups.has_keys,
                JsonLookups.has_any_keys: SqliteJsonLookups.has_any_keys,
                JsonPathLookups.greater_than: SqliteJsonOrdering.greater_than,
                JsonPathLookups.greater_equal: SqliteJsonOrdering.greater_equal,
                JsonPathLookups.less_than: SqliteJsonOrdering.less_than,
                JsonPathLookups.less_equal: SqliteJsonOrdering.less_equal,
                JsonPathLookups.between: SqliteJsonOrdering.between,
                Lookups.is_in: SqliteLargeInList.is_in,
                Lookups.not_in: SqliteLargeInList.not_in,
                Lookups.row_is_in: SqliteLargeInList.row_is_in,
                Lookups.row_not_in: SqliteLargeInList.row_not_in,
            },
        )

    def supports_operator(self, field_lookup: FieldLookup) -> bool:
        """Whether SQLite runs a lookup - ``__search`` only on a field a ``FullTextIndex`` covers."""
        if field_lookup.operator is Lookups.search:
            # Local import: the search package imports hare's indexes and expressions, whose
            # modules import this one.
            from hare.dialects.sqlite.indexes.full_text_index import FullTextIndex

            searched_field: Any = field_lookup.searched_field  # type: ignore[call-overload]
            model = getattr(searched_field, "model", None)
            return model is not None and (
                FullTextIndex.get_covering(model, (searched_field.model_field_name,)) is not None
            )
        return super().supports_operator(field_lookup)

    def get_unsupported_reason(self, lookup_info: LookupInfo) -> str | None:
        field_lookup = lookup_info.field_lookup
        if field_lookup is not None and field_lookup.operator is Lookups.search:
            return SQLITE_FULL_TEXT_SEARCH_UNSUPPORTED_REASON
        return None

    def get_overridden_operator(
        self, operator: Callable[..., Any], field_lookup: FieldLookup | None
    ) -> Callable[..., Any] | None:
        if operator is Lookups.search:
            searched_field = None
            if field_lookup is not None:
                searched_field = field_lookup.searched_field  # type: ignore[call-overload]
            text_search = cast("SqliteTextSearch", self.dialect.text_search)
            return partial(text_search.get_search_criterion, searched_field=searched_field)
        return super().get_overridden_operator(operator, field_lookup)
