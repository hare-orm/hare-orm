from __future__ import annotations

from hare.dialects.base.operators import FilterOperators
from hare.dialects.sqlite.functions.json.sqlite_json_equality import SqliteJsonEquality
from hare.dialects.sqlite.functions.json.sqlite_json_lookups import SqliteJsonLookups
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.regex import SqlitePosixRegex
from hare.dialects.sqlite.lookups.in_list.sqlite_large_in_list import SqliteLargeInList
from hare.query.filters import JsonLookups, Lookups
from hare.query.filters.json_path_lookups import JsonPathLookups


class SqliteFilterOperators(FilterOperators):
    """SQLite's lookup operators."""

    @classmethod
    def build(cls) -> SqliteFilterOperators:
        """The SQLite lookup operators.

        Returns:
            The operators.
        """
        return cls(
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
            }
        )
