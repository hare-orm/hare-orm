from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import Any, cast

from hare.dialects.sqlite.constants import SQLITE_JSON_DATETIME_FUNCTION_NAME
from hare.dialects.sqlite.functions.json.sqlite_json_datetime import SqliteJsonDatetime
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath
from hare.dialects.sqlite.functions.sqlite_connection_function import SqliteConnectionFunction
from hare.dialects.sqlite.lookups.constants import SQLITE_JSON_KEY_PATH_FORMAT, SQLITE_JSON_TYPE_NAMES
from hare.dialects.sqlite.lookups.in_list.sqlite_large_in_list import SqliteLargeInList
from hare.dialects.sqlite.lookups.json.sqlite_json_type_criterion import SqliteJsonTypeCriterion
from hare.dialects.sqlite.sqlite_dialect import SqliteDialect
from hare.exceptions import UnSupportedError
from hare.query.expressions.constants import DECIMAL_WITHOUT_TYPE_MESSAGE
from hare.query.filters import JsonLookups
from hare.query.filters.lookups.date_part_lookups import DatePartLookups
from hare.query.filters.lookups.json.json_filter_guards import JsonFilterGuards
from hare.query.filters.lookups.json.json_filter_parser import JsonFilterParser
from hare.sql.terms.case.case import Case
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper


class SqliteJsonFilterGuards(JsonFilterGuards):
    """The SQLite terms of the JSON ``__filter`` lookup, over ``json_extract()``/``json_type()``."""

    @staticmethod
    def get_type_guard(field_term: Term, key_parts: list[str | int], type_names: tuple[str, ...]) -> Criterion:
        """The value at the path is of one of the ``json_type()`` names."""
        type_term = SqliteJsonTypeCriterion(field_term, key_parts)
        guard: Criterion | None = None
        for type_name in type_names:
            type_matches = type_term == ValueWrapper(type_name, allow_parametrize=False)
            guard = type_matches if guard is None else guard | type_matches
        assert guard is not None  # nosec B101
        return guard

    @classmethod
    def type_guarded_term(
        cls, field_term: Term, key_parts: list[str | int], json_type: str, sql_cast_type: str | None
    ) -> Term:
        text_term = JsonFilterParser.get_field_path(field_term, key_parts)
        guard = cls.get_type_guard(field_term, key_parts, SQLITE_JSON_TYPE_NAMES[json_type])
        if sql_cast_type in {"timestamp", "timestamptz"}:
            mode = "instant" if sql_cast_type == "timestamptz" else "wall"
            datetime_term = SqliteConnectionFunction(
                SQLITE_JSON_DATETIME_FUNCTION_NAME, text_term, ValueWrapper(mode, allow_parametrize=False)
            )
            return Case().when(guard, datetime_term)
        return Case().when(guard, text_term)

    @classmethod
    def create_criterion(
        cls, key_parts: list[str | int], field_term: Term, operator_: Callable[..., Any], value: Any
    ) -> Criterion:
        if operator_ in cls.DATE_PART_OPERATORS and not cls.is_container_value(operator_, value):
            date_part = cast("partial[Criterion]", operator_).args[0]
            guard = cls.get_type_guard(field_term, key_parts, SQLITE_JSON_TYPE_NAMES["string"])
            part_term = SqliteConnectionFunction(
                SQLITE_JSON_DATETIME_FUNCTION_NAME,
                JsonFilterParser.get_field_path(field_term, key_parts),
                ValueWrapper(date_part.value, allow_parametrize=False),
            )
            return Case().when(guard, part_term) == DatePartLookups.get_part_value(date_part, value)
        return super().create_criterion(key_parts, field_term, operator_, value)

    @classmethod
    def prepare_value(cls, value: Any, sql_type: str) -> Any:
        if isinstance(value, (list, tuple, set)):
            return [cls.prepare_value(item, sql_type) for item in value]
        if isinstance(value, datetime.date) and sql_type in {"timestamp", "timestamptz"}:
            return SqliteJsonDatetime.normalize_value(value, "instant" if sql_type == "timestamptz" else "wall")
        if isinstance(value, Decimal):
            raise UnSupportedError(DECIMAL_WITHOUT_TYPE_MESSAGE.format(value=value, dialect=SqliteDialect.name))
        if isinstance(value, int) and not isinstance(value, bool):
            return SqliteJsonPath.get_sqlite_number(value)
        return value

    @classmethod
    def get_container_term(cls, field_term: Term, key_parts: list[str | int]) -> Term:
        guard = cls.get_type_guard(field_term, key_parts, SQLITE_JSON_TYPE_NAMES["container"])
        # json_extract() gives an object or an array as its minified JSON text.
        return Case().when(guard, JsonFilterParser.get_field_path(field_term, key_parts))

    @classmethod
    def get_container_value(cls, value: Any) -> Any:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def get_container_in_list(cls, container_term: Term, values: list[Any]) -> Criterion:
        return SqliteLargeInList.is_in(container_term, [cls.get_container_value(item) for item in values])

    @classmethod
    def get_scalar_in_list(cls, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        return SqliteLargeInList.is_in(guarded_term, values)

    @classmethod
    def get_scalar_not_in_list(cls, guarded_term: Term, values: list[Any], sql_type: str) -> Criterion:
        return SqliteLargeInList.not_in(guarded_term, values)

    @classmethod
    def key_existence_criterion(
        cls, field_term: Term, key_parts: list[str | int], operator_: Any, value: Any
    ) -> Criterion:
        # A key of an object at the path - json_type() of the key's path is NULL where there's none. The
        # path is built from the bound key in SQL (the key as a JSON string), so a plan holds no key.
        document = JsonFilterParser.get_field_path(field_term, key_parts, as_text=False) if key_parts else field_term
        if operator_ is JsonLookups.has_key:
            keys = [cls.get_key(value)]
        else:
            keys = cls.get_key_list("has_keys" if operator_ is JsonLookups.has_keys else "has_any_keys", value)
        key_path_format = ValueWrapper(SQLITE_JSON_KEY_PATH_FORMAT, allow_parametrize=False)
        tests = [
            Function(
                "json_type", document, Function("printf", key_path_format, Function("json_quote", ValueWrapper(key)))
            ).notnull()
            for key in keys
        ]
        if not tests:
            # No key to have: a document has every key of none, and any of none it hasn't.
            if operator_ is JsonLookups.has_keys:
                return field_term.notnull()
            return SqliteLargeInList.is_in(field_term, [])
        return Criterion.all(tests) if operator_ is JsonLookups.has_keys else Criterion.any(tests)
