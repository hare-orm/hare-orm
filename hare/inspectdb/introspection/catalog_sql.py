from __future__ import annotations

from collections.abc import Iterator

from sqlparse import tokens
from sqlparse.lexer import tokenize

from hare.inspectdb.constants import CATALOG_IDENTIFIER_QUOTES


class CatalogSql:
    """Read delimiters in catalog SQL without interpreting or rewriting expressions.

    The lexer handles quoted identifiers, escaped and dollar-quoted literals, and
    comments. Hare only tracks parenthesis depth and source offsets. Unknown
    expressions remain their original text, including whitespace and comments.
    This is a catalog reader, not a parser or normalizer of user-supplied SQL.
    """

    @staticmethod
    def get_identifier(sql: str) -> str | None:
        """Decode one catalog identifier, refusing expressions and qualified names."""
        lexemes = [(token_type, value) for token_type, value in tokenize(sql) if token_type not in tokens.Whitespace]
        if len(lexemes) != 1:
            return None
        token_type, value = lexemes[0]
        if token_type is tokens.Literal.String.Symbol and value.startswith('"') and value.endswith('"'):
            return value[1:-1].replace('""', '"')
        if token_type in tokens.Name or token_type in tokens.Keyword:
            return value
        return None

    @staticmethod
    def get_unquoted_characters(sql: str) -> Iterator[tuple[int, str, int]]:
        """Yield source positions and depth outside literals, identifiers and comments."""
        position = depth = 0
        for token_type, value in tokenize(sql):
            if not (
                token_type in tokens.Literal
                or token_type in tokens.Comment
                or value.startswith(CATALOG_IDENTIFIER_QUOTES)
            ):
                for offset, character in enumerate(value):
                    yield position + offset, character, depth
                    if character == "(":
                        depth += 1
                    elif character == ")":
                        depth -= 1
            position += len(value)

    @classmethod
    def get_first_unquoted_nonspace_offset(cls, sql: str) -> int | None:
        """The first non-whitespace offset outside quoted text and comments."""
        return next(
            (offset for offset, character, _ in cls.get_unquoted_characters(sql) if not character.isspace()), None
        )

    @classmethod
    def split_terms(cls, sql: str, separator: str = ",") -> list[str]:
        """Split at top-level delimiters or operators, preserving each term as reported."""
        terms = []
        start = 0
        for position, _, depth in cls.get_unquoted_characters(sql):
            if position >= start and depth == 0 and sql.startswith(separator, position):
                terms.append(sql[start:position])
                start = position + len(separator)
        terms.append(sql[start:])
        return terms

    @classmethod
    def get_parenthesized_terms(cls, sql: str) -> list[str]:
        """Read terms in the first outer parenthesis pair of a catalog declaration."""
        _, body, _ = cls.get_parenthesized_parts(sql)
        return cls.split_terms(body) if body is not None else []

    @classmethod
    def get_parenthesized_parts(cls, sql: str) -> tuple[str, str | None, str]:
        """Read the stripped prefix, the first outer pair's body and any verbatim suffix.

        The body is None when no complete pair exists.
        """
        opening, closing = cls._get_parenthesis_bounds(sql)
        prefix = sql[:opening].strip()
        if opening is None or closing is None:
            return prefix, None, ""
        return prefix, sql[opening + 1 : closing], sql[closing + 1 :]

    @classmethod
    def extract_parentheses(cls, sql: str, opening: int) -> str | None:
        """Read one balanced pair; delimiters inside strings and comments do not count."""
        if sql[opening : opening + 1] != "(":
            return None
        _, closing = cls._get_parenthesis_bounds(sql[opening:])
        return sql[opening + 1 : opening + closing] if closing is not None else None

    @classmethod
    def _get_parenthesis_bounds(cls, sql: str) -> tuple[int | None, int | None]:
        opening = None
        for position, character, depth in cls.get_unquoted_characters(sql):
            if character == "(" and depth == 0 and opening is None:
                opening = position
            elif character == ")" and depth == 1 and opening is not None:
                return opening, position
        return opening, None
