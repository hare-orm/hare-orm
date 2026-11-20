from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from operator import itemgetter
from typing import Any

from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.dialects.sqlite.literals.constants import SQLITE_IDENTIFIER_CASE_FOLDING
from hare.inspectdb.constants import FOREIGN_KEY_DEFERRAL_TEMPLATE, FOREIGN_KEY_NO_ACTION
from hare.inspectdb.introspection.catalog_sql import CatalogSql


class SqliteForeignKeyClauses:
    """Read constraint timing, which foreign_key_list omits, from catalog clauses.

    Matching uses local and referenced columns and both actions, rather than pragma
    ordinal IDs. Repeated equivalent constraints retain a multiset of timings.
    Unknown clause shapes remain unknown instead of being declared immediate.
    """

    @staticmethod
    def get_ordered_groups(rows: Iterable[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        """Group PRAGMA rows by foreign key and order each key's columns by its sequence."""
        groups: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["id"], []).append(row)
        for group in groups.values():
            group.sort(key=itemgetter("seq"))
        return groups

    @classmethod
    def get_deferrals(
        cls,
        raw: dict[str, Any],
        target_columns: dict[str, list[dict[str, Any]]],
        groups: dict[int, list[dict[str, Any]]],
    ) -> dict[int, str | None]:
        timings: dict[tuple[tuple[str, ...], str, tuple[str, ...], str, str], list[str]] = defaultdict(list)
        target_columns = {
            name.translate(SQLITE_IDENTIFIER_CASE_FOLDING): columns for name, columns in target_columns.items()
        }
        names = {row["name"] for row in raw["columns"]}
        for clause in CatalogSql.get_parenthesized_terms(raw["table_rows"][0]["sql"]):
            lexemes = list(SqliteColumnDeclarations.get_top_level_tokens(clause))
            if not lexemes:
                continue
            first = SqliteColumnDeclarations.get_identifier(lexemes[0][1])
            if first in names:
                local_columns: tuple[str, ...] = (first,)
            else:
                foreign = next((position for position, value in lexemes if value.upper() == "FOREIGN"), None)
                if foreign is None:
                    continue
                local_columns = SqliteColumnDeclarations.get_parenthesized_identifiers(clause[foreign:])
            references = [position for position, (_, value) in enumerate(lexemes) if value.upper() == "REFERENCES"]
            for ordinal, position in enumerate(references):
                if position + 1 == len(lexemes):
                    continue
                target_position, target_token = lexemes[position + 1]
                target = SqliteColumnDeclarations.get_identifier(target_token).translate(
                    SQLITE_IDENTIFIER_CASE_FOLDING
                )
                tail = clause[target_position + len(target_token) :]
                opening = CatalogSql.get_first_unquoted_nonspace_offset(tail)
                referenced_columns = (
                    SqliteColumnDeclarations.get_parenthesized_identifiers(tail)
                    if opening is not None and tail[opening] == "("
                    else cls.get_primary_columns(target_columns.get(target, []))
                )
                end = references[ordinal + 1] if ordinal + 1 < len(references) else len(lexemes)
                words = [value.upper() for _, value in lexemes[position + 2 : end]]
                deferrable = "DEFERRABLE" in words and (
                    words.index("DEFERRABLE") == 0 or words[words.index("DEFERRABLE") - 1] != "NOT"
                )
                deferred = any(words[index : index + 2] == ["INITIALLY", "DEFERRED"] for index in range(len(words)))
                timing = (
                    FOREIGN_KEY_DEFERRAL_TEMPLATE.format(initially="DEFERRED" if deferred else "IMMEDIATE")
                    if deferrable
                    else ""
                )
                key = cls.get_match_key(
                    local_columns,
                    target,
                    referenced_columns,
                    cls.get_action(words, "DELETE"),
                    cls.get_action(words, "UPDATE"),
                )
                timings[key].append(timing)
        result = {}
        for identity, rows in groups.items():
            target = rows[0]["table"].translate(SQLITE_IDENTIFIER_CASE_FOLDING)
            referenced_columns = (
                tuple(row["to"] for row in rows)
                if all(row["to"] is not None for row in rows)
                else cls.get_primary_columns(target_columns.get(target, []))
            )
            key = cls.get_match_key(
                tuple(row["from"] for row in rows),
                target,
                referenced_columns,
                rows[0]["on_delete"],
                rows[0]["on_update"],
            )
            values = timings.get(key)
            result[identity] = values.pop() if values else None
        return result

    @staticmethod
    def get_match_key(
        local_columns: tuple[str, ...],
        target: str,
        referenced_columns: tuple[str, ...],
        on_delete: str,
        on_update: str,
    ) -> tuple[tuple[str, ...], str, tuple[str, ...], str, str]:
        """Match pragma columns and clause spellings using SQLite's ASCII identifier equality."""
        return (
            tuple(name.translate(SQLITE_IDENTIFIER_CASE_FOLDING) for name in local_columns),
            target,
            tuple(name.translate(SQLITE_IDENTIFIER_CASE_FOLDING) for name in referenced_columns),
            on_delete,
            on_update,
        )

    @staticmethod
    def get_primary_columns(columns: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(row["name"] for row in sorted(columns, key=itemgetter("pk")) if row["pk"])

    @staticmethod
    def get_action(words: list[str], operation: str) -> str:
        """Read the fixed ON DELETE/UPDATE grammar, preserving unknown action text."""
        for index in range(len(words) - 2):
            if words[index : index + 2] != ["ON", operation]:
                continue
            action = words[index + 2]
            if action in ("NO", "SET") and index + 3 < len(words):
                action += " " + words[index + 3]
            return action
        return FOREIGN_KEY_NO_ACTION
