from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.dialects.postgresql.constants import (
    POSTGRESQL_FLOAT_ELEMENT_TYPES,
    POSTGRESQL_INTEGER_ELEMENT_TYPES,
    POSTGRESQL_NUMERIC_CAST_TYPES,
    POSTGRESQL_TRUE_TEXTS,
)


class PostgresqlArrayLiterals:
    """PostgreSQL's array literals (``{1,2,"a b"}``) - a column default or an option PostgreSQL prints as
    one - read as lists of Python values."""

    @classmethod
    def read_array_literal(cls, text: str, position: int, element_type: str) -> tuple[list[Any], int]:
        """Reads the array literal at ``position``.

        Returns:
            The list and the position after its closing brace.

        Raises:
            ValueError: The text isn't an array literal.
        """
        if not text.startswith("{", position):
            raise ValueError(text)
        position += 1
        elements: list[Any] = []
        while position < len(text):
            if text.startswith("}", position) and not elements:
                return elements, position + 1
            element: Any
            if text.startswith("{", position):
                element, position = PostgresqlArrayLiterals.read_array_literal(text, position, element_type)
            elif text.startswith('"', position):
                element_text, position = PostgresqlArrayLiterals.read_quoted_array_element(text, position)
                element = PostgresqlArrayLiterals.get_array_element_value(element_text, element_type)
            else:
                end = min(
                    (index for index in (text.find(",", position), text.find("}", position)) if index != -1),
                    default=-1,
                )
                if end == -1:
                    raise ValueError(text)
                raw_element = text[position:end].strip()
                element = (
                    None
                    if raw_element.upper() == "NULL"
                    else PostgresqlArrayLiterals.get_array_element_value(raw_element, element_type)
                )
                position = end
            elements.append(element)
            if text.startswith(",", position):
                position += 1
            elif text.startswith("}", position):
                return elements, position + 1
            else:
                raise ValueError(text)
        raise ValueError(text)

    @staticmethod
    def read_quoted_array_element(text: str, position: int) -> tuple[str, int]:
        """Reads a double-quoted array element - a backslash escapes the next character.

        Returns:
            The element's text and the position after its closing quote.

        Raises:
            ValueError: The quote isn't closed.
        """
        characters = []
        position += 1
        while position < len(text):
            character = text[position]
            if character == "\\" and position + 1 < len(text):
                characters.append(text[position + 1])
                position += 2
            elif character == '"':
                return "".join(characters), position + 1
            else:
                characters.append(character)
                position += 1
        raise ValueError(text)

    @staticmethod
    def get_array_element_value(text: str, element_type: str) -> Any:
        """An array element's text as the value of its type.

        Raises:
            ValueError: The text isn't a value of the type.
        """
        if element_type in POSTGRESQL_INTEGER_ELEMENT_TYPES:
            return int(text)
        if element_type in POSTGRESQL_NUMERIC_CAST_TYPES:
            return float(text) if element_type in POSTGRESQL_FLOAT_ELEMENT_TYPES else Decimal(text)
        if element_type == "boolean":
            return text.lower() in POSTGRESQL_TRUE_TEXTS
        return text
