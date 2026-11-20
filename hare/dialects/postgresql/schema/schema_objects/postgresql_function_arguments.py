from __future__ import annotations

from collections.abc import Iterable

from sqlparse import tokens
from sqlparse.lexer import tokenize


class PostgresqlFunctionArguments:
    """Address a declared function without copying CREATE-only argument defaults.

    PostgreSQL accepts argument modes and names in DROP/ALTER/GRANT signatures. Only
    the default suffix is omitted; quoted names, types, comments and expressions are
    tokenized by sqlparse, and the default expression is never interpreted.
    """

    @staticmethod
    def without_default(argument: str) -> str:
        position = depth = 0
        for token_type, value in tokenize(argument):
            if depth == 0 and (
                token_type in tokens.Keyword
                and value.upper() == "DEFAULT"
                or token_type in tokens.Operator
                and value == "="
            ):
                return argument[:position].rstrip(" \t")
            if token_type in tokens.Punctuation:
                depth += value.count("(") + value.count("[") - value.count(")") - value.count("]")
            position += len(value)
        return argument.strip(" \t")

    @classmethod
    def get_identity(cls, arguments: Iterable[str]) -> tuple[str, ...]:
        return tuple(cls.without_default(argument) for argument in arguments)

    @classmethod
    def get_identity_sql(cls, arguments: Iterable[str]) -> str:
        return ", ".join(cls.get_identity(arguments))
