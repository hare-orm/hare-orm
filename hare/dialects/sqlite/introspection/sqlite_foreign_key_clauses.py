from __future__ import annotations

from collections import defaultdict
from operator import itemgetter
from typing import Any

from hare.dialects.sqlite.introspection.sqlite_column_declarations import SqliteColumnDeclarations
from hare.inspectdb.constants import FOREIGN_KEY_DEFERRAL_TEMPLATE, FOREIGN_KEY_NO_ACTION
from hare.inspectdb.introspection.catalog_sql import CatalogSql


class SqliteForeignKeyClauses:
    """Read constraint timing, which foreign_key_list omits, from catalog clauses.

    Matching uses local and referenced columns and both actions, rather than pragma
    ordinal IDs. Repeated equivalent constraints retain a multiset of timings.
    Unknown clause shapes remain unknown instead of being declared immediate.
    """

    @classmethod
    def get_deferrals(
        cls, raw: dict[str, Any], target_columns: dict[str, list[dict[str, Any]]]
    ) -> dict[int, str | None]:
        timings: dict[tuple[tuple[str, ...], str, tuple[str, ...], str, str], list[str]] = defaultdict(list)
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
                local_columns = tuple(
                    SqliteColumnDeclarations.get_identifier(value.strip())
                    for value in CatalogSql.get_parenthesized_terms(clause[foreign:])
                )
            references = [position for position, (_, value) in enumerate(lexemes) if value.upper() == "REFERENCES"]
            for ordinal, position in enumerate(references):
                if position + 1 == len(lexemes):
                    continue
                target_position, target_token = lexemes[position + 1]
                target = SqliteColumnDeclarations.get_identifier(target_token)
                tail = clause[target_position + len(target_token) :]
                leading = next(
                    (
                        character
                        for _, character, _ in CatalogSql.get_unquoted_characters(tail)
                        if not character.isspace()
                    ),
                    None,
                )
                referenced_columns = (
                    tuple(
                        SqliteColumnDeclarations.get_identifier(value.strip())
                        for value in CatalogSql.get_parenthesized_terms(tail)
                    )
                    if leading == "("
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
                key = (
                    local_columns,
                    target,
                    referenced_columns,
                    cls.get_action(words, "DELETE"),
                    cls.get_action(words, "UPDATE"),
                )
                timings[key].append(timing)
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in raw["foreign_keys"]:
            grouped[row["id"]].append(row)
        result = {}
        for identity, rows in grouped.items():
            rows.sort(key=itemgetter("seq"))
            target = rows[0]["table"]
            referenced_columns = (
                tuple(row["to"] for row in rows)
                if all(row["to"] is not None for row in rows)
                else cls.get_primary_columns(target_columns.get(target, []))
            )
            key = (
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
