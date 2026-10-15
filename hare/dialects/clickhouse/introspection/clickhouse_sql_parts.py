from __future__ import annotations

from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN,
    CLICKHOUSE_SQL_QUOTES,
    CLICKHOUSE_SQL_WORD_PATTERN,
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

    @staticmethod
    def get_leading_identifier(sql: str) -> tuple[str, str]:
        """The identifier a text begins with.

        Args:
            sql: The text.

        Returns:
            The identifier - without its quotes - and the text after it; an empty identifier when the
            text begins with none.
        """
        if sql.startswith("`"):
            index = 1
            while index < len(sql) and sql[index] != "`":
                index += 2 if sql[index] == "\\" else 1
            return sql[1:index].replace("\\`", "`").replace("\\\\", "\\"), sql[index + 1 :]
        match = CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN.match(sql)
        return ("", sql) if match is None else (match.group(0), sql[match.end() :])

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
