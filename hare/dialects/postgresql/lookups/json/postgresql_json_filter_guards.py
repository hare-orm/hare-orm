from __future__ import annotations

import json
from typing import Any

from hare.dialects.postgresql.functions.array.any_value import AnyValue
from hare.dialects.postgresql.lookups.in_list.postgresql_large_in_list import PostgresqlLargeInList
from hare.dialects.postgresql.lookups.postgresql_regex_lookups import PostgresqlRegexLookups
from hare.query.filters import JsonLookups
from hare.query.filters.constants import JSON_ISO_DATETIME_PATTERN
from hare.query.filters.lookups.json.json_filter_guards import JsonFilterGuards
from hare.query.filters.lookups.json.json_filter_parser import JsonFilterParser
from hare.sql.enums import Equality
from hare.sql.functions.cast import Cast
from hare.sql.terms.case.case import Case
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class PostgresqlJsonFilterGuards(JsonFilterGuards):
    """The Postgres terms of the JSON ``__filter`` lookup, over ``jsonb``."""

    @staticmethod
    def get_json_term(field_term: Term, key_parts: list[str | int]) -> Term:
        """The jsonb value at the path (``->``), or the column itself for an empty path."""
        return JsonFilterParser.get_field_path(field_term, key_parts, as_text=False) if key_parts else field_term

    @classmethod
    def get_text_term(cls, field_term: Term, key_parts: list[str | int]) -> Term:
        return JsonFilterParser.get_field_path(field_term, key_parts) if key_parts else field_term

    @classmethod
    def get_container_term(cls, field_term: Term, key_parts: list[str | int]) -> Term:
        return cls.get_json_term(field_term, key_parts)

    @classmethod
    def get_container_value(cls, value: Any) -> Any:
        return Cast(ValueWrapper(json.dumps(value)), "JSONB")

    @classmethod
    def get_container_in_list(cls, container_term: Term, values: list[Any]) -> Criterion:
        # Bound as ONE jsonb[] parameter, however long the list.
        json_texts = [json.dumps(item) for item in values]
        return BasicCriterion(
            Equality.EQ, container_term, AnyValue(PostgresqlLargeInList.get_array_term(json_texts, "jsonb"))
        )

    @classmethod
    def get_scalar_in_list(cls, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        return PostgresqlLargeInList.is_in(guarded_term, values, element_type=sql_type)

    @classmethod
    def get_scalar_not_in_list(cls, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        return PostgresqlLargeInList.not_in(guarded_term, values, element_type=sql_type)

    @classmethod
    def key_existence_criterion(
        cls, field_term: Term, key_parts: list[str | int], operator_: Any, value: Any
    ) -> Criterion:
        # has_key/has_keys/has_any_keys test the object AT the path, so they need the JSON-typed
        # extraction (`->`), not the text one (`->>`).
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.lookups.json.postgresql_json_lookups import PostgresqlJsonLookups

        path_term = cls.get_json_term(field_term, key_parts)
        if operator_ is JsonLookups.has_key:
            return PostgresqlJsonLookups.has_key(path_term, value)
        if operator_ is JsonLookups.has_keys:
            return PostgresqlJsonLookups.has_keys(path_term, value)
        return PostgresqlJsonLookups.has_any_keys(path_term, value)

    @staticmethod
    def calendar_checked_cast(text_term: Term, sql_cast_type: str) -> Term:
        """Casts an ISO-shaped date/datetime text, or NULL when its day doesn't exist in its
        month (e.g. ``2024-02-30``), which the regex guard alone can't tell."""
        year = Cast(Function("SUBSTR", text_term, 1, 4), "INT")
        month = Cast(Function("SUBSTR", text_term, 6, 2), "INT")
        day = Cast(Function("SUBSTR", text_term, 9, 2), "INT")
        normalized_date = Function("MAKE_DATE", year, month, 1) + Cast(day - 1, "INT")
        return Case().when(Function("DATE_PART", "month", normalized_date).eq(month), Cast(text_term, sql_cast_type))

    @classmethod
    def type_guarded_term(
        cls, field_term: Term, key_parts: list[str | int], json_type: str, sql_cast_type: str | None
    ) -> Term:
        """Wraps the JSON path's value in ``CASE WHEN jsonb_typeof(...) = json_type THEN ... END``: a
        stored value of another type gives NULL instead of a failed cast, and the number ``3``
        doesn't match the string ``"3"``.

        Args:
            field_term: The JSON column.
            key_parts: The path as keys and indices.
            json_type: The ``jsonb_typeof()`` value the stored value must have.
            sql_cast_type: The SQL type the extracted text is cast to, None to compare the text.

        Returns:
            A term giving the extracted value or NULL.
        """
        text_term = JsonFilterParser.get_field_path(field_term, key_parts)
        jsonb_term = JsonFilterParser.get_field_path(field_term, key_parts, as_text=False)
        type_guard = Function("JSONB_TYPEOF", jsonb_term).eq(json_type)
        if sql_cast_type in {"timestamp", "timestamptz"}:
            return Case().when(
                type_guard & PostgresqlRegexLookups.posix_regex(text_term, JSON_ISO_DATETIME_PATTERN),
                cls.calendar_checked_cast(text_term, sql_cast_type),
            )
        branch_term = Cast(text_term, sql_cast_type) if sql_cast_type else text_term
        return Case().when(type_guard, branch_term)
