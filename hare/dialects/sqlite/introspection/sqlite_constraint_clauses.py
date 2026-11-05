from __future__ import annotations

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.inspectdb.introspection.catalog_sql import CatalogSql


class SqliteConstraintClauses:
    """Read constraint clauses through the shared lexer, preserving expression text.

    Pragmas omit UNIQUE names and CHECK expressions. Quoted text and nested expressions
    cannot introduce declarations; incomplete CHECK clauses remain available as diagnostics.
    """

    @staticmethod
    def get_clauses(sql: str) -> list[str]:
        """Split a table body, retaining an incomplete final clause for diagnostics."""
        opening = next(
            (
                position
                for position, character, depth in CatalogSql.get_unquoted_characters(sql)
                if character == "(" and depth == 0
            ),
            None,
        )
        if opening is None:
            return []
        body = CatalogSql.extract_parentheses(sql, opening)
        return CatalogSql.split_terms(body if body is not None else sql[opening + 1 :])

    @classmethod
    def get_unique_names(cls, sql: str) -> dict[tuple[str, ...], list[str]]:
        """Map table-level UNIQUE column tuples to declared names, in declaration order."""
        names_by_columns: dict[tuple[str, ...], list[str]] = {}
        for clause in cls.get_clauses(sql or ""):
            lexemes = list(SqliteColumnDeclarations.get_top_level_tokens(clause))
            if len(lexemes) < 3 or lexemes[0][1].upper() != "CONSTRAINT" or lexemes[2][1].upper() != "UNIQUE":
                continue
            position, keyword = lexemes[2]
            columns = tuple(
                SqliteColumnDeclarations.get_identifier(column.strip())
                for column in CatalogSql.get_parenthesized_terms(clause[position + len(keyword) :])
            )
            if columns:
                name = SqliteColumnDeclarations.get_identifier(lexemes[1][1])
                names_by_columns.setdefault(columns, []).append(name)
        return names_by_columns

    @classmethod
    def get_checks(cls, table: str, sql: str) -> tuple[list[CheckConstraint], list[tuple[str, str]]]:
        """Read column/table CHECKs, assigning ordinal names to unnamed declarations."""
        constraints: list[CheckConstraint] = []
        unparsed: list[tuple[str, str]] = []
        ordinal = 0
        for clause in cls.get_clauses(sql):
            lexemes = list(SqliteColumnDeclarations.get_top_level_tokens(clause))
            for index, (position, keyword) in enumerate(lexemes):
                if keyword.upper() != "CHECK":
                    continue
                tail = clause[position + len(keyword) :]
                opening = next(
                    (
                        offset
                        for offset, character, _ in CatalogSql.get_unquoted_characters(tail)
                        if not character.isspace()
                    ),
                    None,
                )
                if opening is None or tail[opening] != "(":
                    continue
                ordinal += 1
                named = index >= 2 and lexemes[index - 2][1].upper() == "CONSTRAINT"
                name = (
                    SqliteColumnDeclarations.get_identifier(lexemes[index - 1][1])
                    if named
                    else f"{table}_check_{ordinal}"
                )
                expression = CatalogSql.extract_parentheses(tail, opening)
                if expression is None:
                    unparsed.append((name, clause[position:].strip()))
                else:
                    constraints.append(CheckConstraint(check=RawSQLTerm(expression.strip()), name=name))
        return constraints, unparsed
