from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.base.operators import FilterOperators
from hare.dialects.enums import DialectName
from hare.dialects.postgresql.lookups.in_list.postgresql_large_in_list import PostgresqlLargeInList
from hare.dialects.postgresql.lookups.json.postgresql_json_lookups import PostgresqlJsonLookups
from hare.dialects.postgresql.lookups.regex import PostgresqlRegexLookups
from hare.dialects.postgresql.search import SearchCriterion
from hare.dialects.postgresql.search.functions.plain_to_ts_query import PlainToTsQuery
from hare.dialects.registry import DialectRegistry
from hare.query.filters import JsonLookups, Lookups
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field
    from hare.query.filters.field_lookup import FieldLookup


class PostgresqlFilterOperators(FilterOperators):
    """PostgreSQL's lookup operators - some take the lookup entry's own settings (a search
    configuration, the SQL type of a bound array)."""

    @classmethod
    def build(cls) -> PostgresqlFilterOperators:
        """The PostgreSQL lookup operators.

        Returns:
            The operators.
        """
        return cls(
            {
                JsonLookups.contains: PostgresqlJsonLookups.contains,
                JsonLookups.contained_by: PostgresqlJsonLookups.contained_by,
                JsonLookups.filter: PostgresqlJsonLookups.filter,
                JsonLookups.has_key: PostgresqlJsonLookups.has_key,
                JsonLookups.has_keys: PostgresqlJsonLookups.has_keys,
                JsonLookups.has_any_keys: PostgresqlJsonLookups.has_any_keys,
                Lookups.posix_regex: PostgresqlRegexLookups.posix_regex,
                Lookups.insensitive_posix_regex: PostgresqlRegexLookups.insensitive_posix_regex,
            }
        )

    @staticmethod
    def get_search_criterion(
        field: Term, value: Term | str, field_is_vector: bool = False, config: str | None = None
    ) -> SearchCriterion:
        """A full-text ``__search`` of ``field``."""
        query = value if isinstance(value, Term) else PlainToTsQuery(ValueWrapper(value), config=config)
        return SearchCriterion(field, expr=query, vectorize=not field_is_vector, config=config)

    @staticmethod
    def get_column_type(field: Field[Any] | None) -> str | None:
        """The PostgreSQL type of a list's elements, None to type them by their values."""
        return None if field is None else field.get_column_type(DialectRegistry.get_dialect(DialectName.POSTGRESQL))

    def get_overridden_operator(
        self, operator: Callable[..., Any], field_lookup: FieldLookup | None
    ) -> Callable[..., Any] | None:
        if operator is Lookups.search:
            field_is_vector = field_lookup is not None and field_lookup.is_tsvector
            search_config = field_lookup.search_config if field_lookup is not None else None
            return partial(self.get_search_criterion, field_is_vector=field_is_vector, config=search_config)
        if operator in (Lookups.is_in, Lookups.not_in, JsonLookups.is_in, JsonLookups.not_in):
            array_element_type = self.get_column_type(
                field_lookup.array_element_field if field_lookup is not None else None  # type: ignore[call-overload]
            )
            target = (
                PostgresqlLargeInList.is_in
                if operator in (Lookups.is_in, JsonLookups.is_in)
                else PostgresqlLargeInList.not_in
            )
            return partial(target, element_type=array_element_type)
        if operator in (Lookups.row_is_in, Lookups.row_not_in):
            element_fields = field_lookup.array_element_fields if field_lookup is not None else None
            array_element_types = (
                None if element_fields is None else [self.get_column_type(field) for field in element_fields]
            )
            row_target = (
                PostgresqlLargeInList.row_is_in if operator is Lookups.row_is_in else PostgresqlLargeInList.row_not_in
            )
            return partial(row_target, element_types=array_element_types)
        return super().get_overridden_operator(operator, field_lookup)
