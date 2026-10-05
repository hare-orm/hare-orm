from __future__ import annotations

import re

from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN,
    CLICKHOUSE_SQL_QUOTES,
    CLICKHOUSE_SQL_WORD_PATTERN,
    CLICKHOUSE_STRING_UNESCAPES,
)


class ClickhouseSqlParts:
    """The parts of SQL text the server reports - found outside its strings, quoted identifiers and
    parentheses."""

    @staticmethod
    def get_unquoted_characters(sql: str) -> list[tuple[int, str, int]]:
        """The characters outside every string and quoted identifier.

        Args:
            sql: The text.

        Returns:
            The position of each, the character and how many parentheses are open before it.
        """
        characters = []
        depth = 0
        quote = ""
        index = 0
        while index < len(sql):
            character = sql[index]
            if quote:
                if character == "\\":
                    index += 1
                elif character == quote:
                    quote = ""
            elif character in CLICKHOUSE_SQL_QUOTES:
                quote = character
            else:
                characters.append((index, character, depth))
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
            index += 1
        return characters

    @classmethod
    def get_top_level_indexes(cls, sql: str) -> list[int]:
        """The positions of the characters outside every string, quoted identifier and pair of
        parentheses - an opening parenthesis of an outermost pair among them.

        Args:
            sql: The text.

        Returns:
            The positions, in order.
        """
        return [index for index, _, depth in cls.get_unquoted_characters(sql) if depth == 0]

    @classmethod
    def search_outside_strings(cls, pattern: re.Pattern[str], sql: str) -> re.Match[str] | None:
        """The first match of a pattern beginning outside every string and quoted identifier.

        Args:
            pattern: The pattern.
            sql: The text.

        Returns:
            The match, None for none.
        """
        unquoted_indexes = {index for index, _, _ in cls.get_unquoted_characters(sql)}
        return next((match for match in pattern.finditer(sql) if match.start() in unquoted_indexes), None)

    @classmethod
    def split(cls, sql: str, separator: str = ",") -> list[str]:
        """The parts of a list.

        Args:
            sql: The list.
            separator: The character between its parts.

        Returns:
            The parts, without the spaces around them; none of an empty text.
        """
        if not sql.strip():
            return []
        parts = []
        start = 0
        for index in cls.get_top_level_indexes(sql):
            if sql[index] == separator:
                parts.append(sql[start:index].strip())
                start = index + 1
        parts.append(sql[start:].strip())
        return parts

    @classmethod
    def find_words(cls, sql: str, words: str, start: int = 0) -> int:
        """Where words of the language stand as words of their own.

        Args:
            sql: The text.
            words: The words - ``ORDER BY``.
            start: Where the search begins.

        Returns:
            The position of the first place, -1 for none.
        """
        for index in cls.get_top_level_indexes(sql):
            if index < start or not sql.startswith(words, index):
                continue
            end = index + len(words)
            if (index == 0 or not CLICKHOUSE_SQL_WORD_PATTERN.match(sql[index - 1])) and (
                end == len(sql) or not CLICKHOUSE_SQL_WORD_PATTERN.match(sql[end])
            ):
                return index
        return -1

    @classmethod
    def get_clauses(cls, sql: str, clause_words: tuple[str, ...]) -> dict[str, str]:
        """The clauses of a definition the server writes in a fixed order.

        Args:
            sql: ``ReplacingMergeTree(version) ORDER BY id TTL ... SETTINGS ...``.
            clause_words: The words each clause begins with, in the server's order.

        Returns:
            The text of each clause found by its words, the text before the first one under an empty
            name.
        """
        clause_positions = []
        search_start = 0
        for words in clause_words:
            index = cls.find_words(sql, words, search_start)
            if index >= 0:
                clause_positions.append((index, words))
                search_start = index + len(words)
        clauses = {"": sql[: clause_positions[0][0] if clause_positions else len(sql)].strip()}
        for position, (index, words) in enumerate(clause_positions):
            end = clause_positions[position + 1][0] if position + 1 < len(clause_positions) else len(sql)
            clauses[words] = sql[index + len(words) : end].strip()
        return clauses

    @classmethod
    def get_before_parentheses(cls, sql: str) -> str:
        """The text before the first pair of parentheses.

        Args:
            sql: The text.

        Returns:
            The text, without the spaces around it - whole when it has no parentheses.
        """
        open_index = next(
            (index for index, character, depth in cls.get_unquoted_characters(sql) if character == "(" and depth == 0),
            len(sql),
        )
        return sql[:open_index].strip()

    @classmethod
    def get_parenthesised(cls, sql: str) -> tuple[str, str]:
        """What the first pair of parentheses holds.

        Args:
            sql: The text.

        Returns:
            The text inside the pair and the text after it; two empty texts without a pair.
        """
        open_index = -1
        for index, character, depth in cls.get_unquoted_characters(sql):
            if character == "(" and depth == 0 and open_index < 0:
                open_index = index
            elif character == ")" and depth == 1 and open_index >= 0:
                return sql[open_index + 1 : index], sql[index + 1 :]
        return "", ""

    @classmethod
    def get_unwrapped(cls, sql: str) -> str:
        """The text without the pair of parentheses around the whole of it - ClickHouse 26.7+ reports
        an index's expression in one (``(score)``, ``(a, b)``), an older server bare.

        Args:
            sql: The text.

        Returns:
            The text inside the pair, the text itself when no pair holds all of it.
        """
        stripped = sql.strip()
        if not stripped.startswith("("):
            return stripped
        inside, after = cls.get_parenthesised(stripped)
        return inside.strip() if not after.strip() else stripped

    @classmethod
    def get_tuple_elements(cls, sql: str) -> list[str]:
        """The elements of a tuple in parentheses - ClickHouse keeps a key of ``(a, b)`` as the keys ``a, b``.

        Args:
            sql: The text.

        Returns:
            The elements of such a tuple, the text alone otherwise.
        """
        stripped = sql.strip()
        inside = cls.get_unwrapped(stripped)
        elements = cls.split(inside) if inside != stripped else []
        return elements if len(elements) >= 2 else [stripped]

    @staticmethod
    def get_leading_identifier(sql: str) -> tuple[str, str]:
        """The identifier a text begins with.

        Args:
            sql: The text.

        Returns:
            The identifier - without its quotes - and the text after it; an empty identifier when the
            text begins with none.
        """
        quote = sql[:1]
        if quote in ("`", '"'):
            # A backslash escapes a quote or itself, a doubled quote is one; any other escape stays as written.
            characters: list[str] = []
            index = 1
            while index < len(sql):
                character = sql[index]
                following = sql[index + 1 : index + 2]
                if character == "\\" and following in (quote, "\\"):
                    characters.append(following)
                    index += 2
                elif character == "\\" and following:
                    characters.append(character + following)
                    index += 2
                elif character == quote and following == quote:
                    characters.append(quote)
                    index += 2
                elif character == quote:
                    break
                else:
                    characters.append(character)
                    index += 1
            return "".join(characters), sql[index + 1 :]
        match = CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN.match(sql)
        return ("", sql) if match is None else (match.group(0), sql[match.end() :])

    @staticmethod
    def get_string_text(literal_sql: str) -> str:
        """The text a quoted string literal holds.

        Args:
            literal_sql: The literal with its quotes.

        Returns:
            The text - a backslash escapes the next character, a doubled quote is one.
        """
        text = literal_sql[1:-1]
        characters: list[str] = []
        index = 0
        while index < len(text):
            character = text[index]
            following = text[index + 1 : index + 2]
            if character == "\\" and following:
                characters.append(CLICKHOUSE_STRING_UNESCAPES.get(following, following))
                index += 2
            elif character == "'" and following == "'":
                characters.append("'")
                index += 2
            else:
                characters.append(character)
                index += 1
        return "".join(characters)

    @classmethod
    def get_name_argument(cls, argument_sql: str) -> str | None:
        """The name an engine's argument gives - in a string or as an identifier
        (``Distributed(cluster, database, 'table')``).

        Args:
            argument_sql: The argument.

        Returns:
            The name; None for an argument of any other sort.
        """
        stripped = argument_sql.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
            return cls.get_string_text(stripped)
        return cls.get_identifier(stripped)

    @classmethod
    def get_identifier(cls, sql: str) -> str | None:
        """The identifier a text is.

        Args:
            sql: The text.

        Returns:
            The identifier without its quotes; None for any other text.
        """
        identifier, rest = cls.get_leading_identifier(sql.strip())
        return identifier if identifier and not rest.strip() else None
