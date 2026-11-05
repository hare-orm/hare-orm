from __future__ import annotations

from collections.abc import Collection, Iterator

from sqlparse import tokens
from sqlparse.lexer import tokenize

from hare.inspectdb.introspection.catalog_sql import CatalogSql


class SqliteColumnDeclarations:
    """Read column clauses from catalog CREATE text without scanning inside expressions.

    SQLite exposes neither a column's collation nor its generated expression in
    table_xinfo. Only these known clauses are interpreted; other source text is
    retained as returned by SQLite. Quoted names, comments and literal contents
    cannot introduce a clause or a table delimiter.
    """

    @staticmethod
    def get_columns(sql: str, names: Collection[str]) -> dict[str, str]:
        """Locate the declared columns by their exact catalog identities."""
        columns = {}
        for clause in CatalogSql.get_parenthesized_terms(sql):
            first = next(SqliteColumnDeclarations.get_top_level_tokens(clause), None)
            if first is None:
                continue
            position, token = first
            name = SqliteColumnDeclarations.get_identifier(token)
            if name in names:
                columns[name] = clause[position + len(token) :].strip()
        return columns

    @staticmethod
    def get_top_level_tokens(sql: str) -> Iterator[tuple[int, str]]:
        """Yield significant outer tokens with their source offsets."""
        position = depth = 0
        for token_type, value in tokenize(sql):
            if token_type in tokens.Punctuation:
                if value == "(":
                    depth += 1
                elif value == ")":
                    depth -= 1
            if depth == 0 and token_type not in tokens.Whitespace and token_type not in tokens.Comment:
                yield position, value
            position += len(value)

    @staticmethod
    def get_identifier(value: str) -> str:
        """Decode SQLite identifier quoting, including accepted single-quoted names."""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"', "`"):
            return value[1:-1].replace(value[0] * 2, value[0])
        if value.startswith("[") and value.endswith("]"):
            return value[1:-1]
        return value

    @staticmethod
    def get_collation(declaration: str) -> str:
        """Read the column COLLATE clause; expressions' collations belong to expressions."""
        lexemes = iter(SqliteColumnDeclarations.get_top_level_tokens(declaration))
        collation = "binary"
        for _, value in lexemes:
            if value.upper() == "COLLATE":
                following = next(lexemes, None)
                if following is not None:
                    collation = SqliteColumnDeclarations.get_identifier(following[1]).casefold()
        return collation

    @staticmethod
    def get_generated_expression(declaration: str) -> str | None:
        """Read AS (expression) without treating quoted AS or a nested default as a clause."""
        for position, value in SqliteColumnDeclarations.get_top_level_tokens(declaration):
            if value.upper() != "AS":
                continue
            tail = declaration[position + len(value) :]
            opening = next(
                (
                    offset
                    for offset, character, depth in CatalogSql.get_unquoted_characters(tail)
                    if not character.isspace()
                ),
                None,
            )
            if opening is not None:
                expression = CatalogSql.extract_parentheses(tail, opening)
                return expression.strip() if expression is not None else None
        return None
