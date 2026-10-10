from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

from hare.exceptions import QueryError
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.json_attribute_criterion import JSONAttributeCriterion
from hare.sql.terms.term import Term


class JsonFilterParser:
    """Parses a JSON-path filter dict (e.g. `{"a__b__gte": 5}`) and resolves it to a
    hare.sql term over a JSON column."""

    @staticmethod
    def get_operator(
        value: dict[str, Any], operator_keywords: dict[str, Callable[..., Criterion]]
    ) -> tuple[list[str | int], Any, Callable[..., Criterion]]:
        """
        Extracts the key parts, filter value and operator from a JSON filter dictionary.

        Raises:
            QueryError: ``value`` isn't a dict with exactly one key.
        """
        if not isinstance(value, dict):
            raise QueryError(f"__filter expects a dict with exactly one key, got {value!r}")
        if len(value) != 1:
            raise QueryError(f"__filter expects a dict with exactly one key, got {len(value)}: {value!r}")
        ((key, filter_value),) = value.items()
        key_parts = JsonFilterParser.get_key_parts(key)
        operator_ = operator_keywords[str(key_parts.pop(-1))] if key_parts[-1] in operator_keywords else operator.eq
        # {"path": {"not": value}} is the nested spelling of {"path__not": value} - only a
        # single-key dict whose key is an operator keyword; any other dict is a JSON object value.
        if operator_ is operator.eq and isinstance(filter_value, dict) and len(filter_value) == 1:
            ((nested_key, nested_value),) = filter_value.items()
            if nested_key in operator_keywords:
                operator_ = operator_keywords[nested_key]
                filter_value = nested_value
        return key_parts, filter_value, operator_

    @staticmethod
    def get_key_parts(path: str) -> list[str | int]:
        """Splits a ``__``-separated JSON path into object keys and array indices.

        Args:
            path: The path, e.g. ``"tags__0"`` or ``"values__-1"``.

        Returns:
            The path parts, every ASCII integer (``"0"``, ``"-1"``) turned into an ``int``.
        """
        key_parts: list[str | int] = []
        for key_part in path.split("__"):
            digits = key_part.removeprefix("-")
            key_parts.append(int(key_part) if digits.isascii() and digits.isdigit() else key_part)
        return key_parts

    @staticmethod
    def get_field_path(field_term: Term, key_parts: list[str | int], *, as_text: bool = True) -> Term:
        """
        Resolves a JSON path from a list of key parts, e.g. converting
        (field, ['a', 'b', 'c']) to field->'a'->'b'->>'c'. Returns a hare.sql Term.

        Args:
            field_term: The JSON column/field to access.
            key_parts: The path to the attribute as a list of keys/indices.
            as_text: Whether the final path segment extracts its value as text rather than JSON.
        """
        return JSONAttributeCriterion(field_term, key_parts, as_text=as_text)
