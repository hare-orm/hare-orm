from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import ClassVar

import aiosqlite

from hare.core.caching.cache import Cache
from hare.dialects.sqlite.enums import SqliteRegexMatching
from hare.dialects.sqlite.functions.constants import (
    MAX_REGEX_PATTERN_LENGTH,
    POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES,
    POSIX_CASED_CHARACTER_CLASS_NAMES,
    POSIX_CHARACTER_CLASS_PATTERNS,
    POSIX_CHARACTER_CLASS_TRANSLATIONS,
    POSIX_CLASS_MEMBER_CODE_POINT_LIMIT,
    POSIX_COLLECTED_CHARACTER_CLASS_NAMES,
    POSIX_LOWER_CLASS_EXTRA_CHARACTERS,
    POSIX_PUNCT_CATEGORIES,
    REGEX_CHARACTER_ESCAPES,
    REGEX_ESCAPE_ARGUMENT_LENGTHS,
    REGEX_GROUP_PREFIX_TERMINATORS,
    REGEX_PATTERN_CACHE_SIZE,
)
from hare.dialects.sqlite.functions.text.sqlite_case_mapping import SqliteCaseMapping
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.term import Term


class SqlitePosixRegex:
    """Translates a ``__posix_regex``/``__iposix_regex`` pattern into a Python ``re`` pattern that
    matches like a Postgres (UTF8) advanced regular expression: ``.`` matches a newline, ``$`` only
    the end of the text, POSIX named classes (``[[:upper:]]``) cover the whole Unicode range, and a
    case-insensitive match accepts exactly a character's simple uppercase and lowercase forms."""

    #: Every character below ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` except the surrogates - made on
    #: first use.
    candidate_characters: ClassVar[str | None] = None
    #: (class name,) -> the members of a collected POSIX class; ("fragment", class name) -> its
    #: bracket-expression fragment.
    CLASSES: ClassVar[Cache[str]] = Cache(2 * len(POSIX_COLLECTED_CHARACTER_CLASS_NAMES))
    #: (pattern, case-insensitive) -> the compiled pattern.
    PATTERNS: ClassVar[Cache[re.Pattern[str]]] = Cache(REGEX_PATTERN_CACHE_SIZE)

    @classmethod
    def get_candidate_characters(cls) -> str:
        """Every character below ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` except the surrogates."""
        if cls.candidate_characters is None:
            cls.candidate_characters = "".join(map(chr, range(0xD800))) + "".join(
                map(chr, range(0xE000, POSIX_CLASS_MEMBER_CODE_POINT_LIMIT))
            )
        return cls.candidate_characters

    @classmethod
    def get_class_members(cls, class_name: str) -> str:
        """The members of a collected POSIX class: ``upper``/``lower`` - characters with a
        one-character lowercase/uppercase of their own - or ``punct``.

        Args:
            class_name: One of ``POSIX_COLLECTED_CHARACTER_CLASS_NAMES``.

        Returns:
            The member characters, in code point order.
        """
        members = cls.CLASSES.get((class_name,))
        if members is None:
            members = cls.CLASSES[(class_name,)] = cls.collect_class_members(class_name)
        return members

    @classmethod
    def collect_class_members(cls, class_name: str) -> str:
        """The members ``get_class_members()`` keeps for a class."""
        characters = cls.get_candidate_characters()
        if class_name == "upper":
            return "".join(
                character
                for character in characters
                if character.lower() != character and SqliteCaseMapping.lower_character(character) != character
            )
        if class_name == "lower":
            return "".join(
                character
                for character in characters
                if (character.upper() != character and SqliteCaseMapping.upper_character(character) != character)
                or character in POSIX_LOWER_CLASS_EXTRA_CHARACTERS
            )
        space_pattern = re.compile(f"[{POSIX_CHARACTER_CLASS_TRANSLATIONS['space']}]")
        return "".join(
            character
            for character in characters
            if unicodedata.category(character) in POSIX_PUNCT_CATEGORIES and not space_pattern.match(character)
        )

    @classmethod
    def get_class_fragment(cls, class_name: str) -> str:
        """The ``re`` bracket-expression fragment of a collected POSIX class."""
        fragment = cls.CLASSES.get(("fragment", class_name))
        if fragment is None:
            fragment = cls.CLASSES[("fragment", class_name)] = cls.make_class_fragment(class_name)
        return fragment

    @classmethod
    def make_class_fragment(cls, class_name: str) -> str:
        """The fragment ``get_class_fragment()`` keeps for a class."""
        ranges: list[list[int]] = []
        for character in cls.get_class_members(class_name):
            code_point = ord(character)
            if ranges and ranges[-1][1] == code_point - 1:
                ranges[-1][1] = code_point
            else:
                ranges.append([code_point, code_point])
        return "".join(
            f"\\U{first:08x}" if first == last else f"\\U{first:08x}-\\U{last:08x}" for first, last in ranges
        )

    @classmethod
    def get_range_case_variants(cls, first_character: str, last_character: str) -> list[str]:
        """The simple case variants of the characters of a bracket range lying outside it."""
        first, last = ord(first_character), ord(last_character)
        variants: set[str] = set()
        for class_name in POSIX_CASED_CHARACTER_CLASS_NAMES:
            for character in cls.get_class_members(class_name):
                if first <= ord(character) <= last:
                    variants.update(SqliteCaseMapping.get_case_variants(character))
        return sorted(variant for variant in variants if not first <= ord(variant) <= last)

    @staticmethod
    def read_escape(pattern: str, position: int) -> tuple[str, int]:
        """Reads the backslash escape starting at ``position``.

        Returns:
            The escape's text, copied verbatim, and the position after it.
        """
        if position + 1 >= len(pattern):
            return pattern[position:], len(pattern)
        letter = pattern[position + 1]
        end = position + 2 + REGEX_ESCAPE_ARGUMENT_LENGTHS.get(letter, 0)
        if letter == "N" and pattern.startswith("{", end):
            closing_brace = pattern.find("}", end)
            end = len(pattern) if closing_brace == -1 else closing_brace + 1
        return pattern[position:end], min(end, len(pattern))

    @staticmethod
    def get_escaped_character(escape: str) -> str | None:
        """The one character a backslash escape stands for.

        Returns:
            The character, or ``None`` for a class escape (``\\d``), anchor or back reference.
        """
        letter = escape[1:2]
        if len(escape) == 2 and not letter.isalnum():
            return letter
        if letter in REGEX_CHARACTER_ESCAPES and len(escape) == 2:
            return REGEX_CHARACTER_ESCAPES[letter]
        hex_digits = escape[2:]
        if letter in REGEX_ESCAPE_ARGUMENT_LENGTHS and len(hex_digits) == REGEX_ESCAPE_ARGUMENT_LENGTHS[letter]:
            try:
                code_point = int(hex_digits, 16)
            except ValueError:
                return None
            return chr(code_point) if code_point <= 0x10FFFF else None
        return None

    @staticmethod
    def read_group_prefix(pattern: str, position: int) -> tuple[str, int]:
        """Reads the ``(?...`` prefix of an extension group - ``(?:``, ``(?=``, ``(?<!``,
        ``(?P<name>``, ``(?i)``, ``(?#...)`` - so its letters are never read as pattern characters.

        Returns:
            The prefix, copied verbatim, and the position after it.
        """
        end = position + 2
        if pattern.startswith(("P<", "P=", "#"), end):
            closing = ">" if pattern.startswith("P<", end) else ")"
            closing_position = pattern.find(closing, end)
            end = len(pattern) if closing_position == -1 else closing_position + 1
            return pattern[position:end], end
        if pattern.startswith(("<=", "<!"), end):
            return pattern[position : end + 2], end + 2
        while end < len(pattern) and (pattern[end].isalpha() or pattern[end] == "-"):
            end += 1
        if end < len(pattern) and pattern[end] in REGEX_GROUP_PREFIX_TERMINATORS:
            end += 1
        return pattern[position:end], end

    @staticmethod
    def get_literal_set_items(character: str, case_insensitive: bool) -> list[str]:
        """The bracket-expression items matching one literal character."""
        characters = SqliteCaseMapping.get_case_variants(character) if case_insensitive else {character}
        return [re.escape(variant) for variant in sorted(characters)]

    @classmethod
    def translate_literal(cls, character: str, case_insensitive: bool) -> str:
        """Translates one pattern character outside a bracket expression."""
        if character == "$":
            return r"\Z"
        if not case_insensitive:
            return character
        set_items = cls.get_literal_set_items(character, case_insensitive)
        return character if len(set_items) == 1 else f"[{''.join(set_items)}]"

    @classmethod
    def translate_bracket(cls, pattern: str, position: int, case_insensitive: bool) -> tuple[str, int]:
        """Translates the bracket expression starting at ``position``.

        Returns:
            The ``re`` pattern matching one character of the expression, and the position after it.

        Raises:
            re.error: The expression isn't closed or names an unknown POSIX class.
        """
        index = position + 1
        negated = pattern.startswith("^", index)
        if negated:
            index += 1
        set_items: list[str] = []
        class_patterns: list[str] = []
        is_first_item = True
        while True:
            if index >= len(pattern):
                raise re.error("unterminated character set", pattern, position)
            character = pattern[index]
            if character == "]" and not is_first_item:
                index += 1
                break
            is_first_item = False
            class_end = pattern.find(":]", index + 2) if pattern.startswith("[:", index) else -1
            if class_end != -1 and pattern[index + 2 : class_end].isalpha():
                cls.add_class_items(pattern[index + 2 : class_end], case_insensitive, set_items, class_patterns)
                index = class_end + 2
                continue
            if character == "\\":
                escape, index = cls.read_escape(pattern, index)
                escaped_character = cls.get_escaped_character(escape)
                if escaped_character is None:
                    set_items.append(escape)
                    continue
                first_character = escaped_character
            else:
                first_character = character
                index += 1
            if pattern.startswith("-", index) and index + 1 < len(pattern) and pattern[index + 1] != "]":
                last_character = pattern[index + 1]
                index += 2
                if last_character == "\\":
                    escape, index = cls.read_escape(pattern, index - 1)
                    escaped_character = cls.get_escaped_character(escape)
                    if escaped_character is None:
                        set_items.append(f"{re.escape(first_character)}-{escape}")
                        continue
                    last_character = escaped_character
                set_items.append(f"{re.escape(first_character)}-{re.escape(last_character)}")
                if case_insensitive and first_character <= last_character:
                    set_items.extend(
                        re.escape(variant) for variant in cls.get_range_case_variants(first_character, last_character)
                    )
                continue
            set_items.extend(cls.get_literal_set_items(first_character, case_insensitive))
        set_text = "".join(set_items)
        if not class_patterns:
            return f"[{'^' if negated else ''}{set_text}]", index
        if not negated:
            alternatives = [f"[{set_text}]", *class_patterns] if set_text else class_patterns
            return f"(?:{'|'.join(alternatives)})", index
        remaining_character = f"[^{set_text}]" if set_text else "."
        return f"(?:(?!{'|'.join(class_patterns)}){remaining_character})", index

    @classmethod
    def add_class_items(
        cls, class_name: str, case_insensitive: bool, set_items: list[str], class_patterns: list[str]
    ) -> None:
        """Adds a POSIX named class to a bracket expression being translated.

        Raises:
            re.error: ``class_name`` isn't a POSIX class.
        """
        if case_insensitive and class_name in POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES:
            class_name = "alpha"
        if class_name in POSIX_CHARACTER_CLASS_TRANSLATIONS:
            set_items.append(POSIX_CHARACTER_CLASS_TRANSLATIONS[class_name])
        elif class_name in POSIX_CHARACTER_CLASS_PATTERNS:
            class_patterns.append(POSIX_CHARACTER_CLASS_PATTERNS[class_name])
        elif class_name in POSIX_COLLECTED_CHARACTER_CLASS_NAMES:
            set_items.append(cls.get_class_fragment(class_name))
        else:
            raise re.error(f"invalid character class {class_name!r}")

    @classmethod
    def translate(cls, pattern: str, case_insensitive: bool) -> str:
        """Translates a Postgres-style pattern into a Python ``re`` pattern (compiled with DOTALL).

        Raises:
            re.error: A bracket expression isn't closed or names an unknown POSIX class.
        """
        translated_parts: list[str] = []
        position = 0
        while position < len(pattern):
            character = pattern[position]
            if character == "\\":
                escape, position = cls.read_escape(pattern, position)
                escaped_character = cls.get_escaped_character(escape) if case_insensitive else None
                set_items = cls.get_literal_set_items(escaped_character, True) if escaped_character else []
                translated_parts.append(f"[{''.join(set_items)}]" if len(set_items) > 1 else escape)
            elif character == "[":
                bracket, position = cls.translate_bracket(pattern, position, case_insensitive)
                translated_parts.append(bracket)
            elif pattern.startswith("(?", position):
                group_prefix, position = cls.read_group_prefix(pattern, position)
                translated_parts.append(group_prefix)
            else:
                translated_parts.append(cls.translate_literal(character, case_insensitive))
                position += 1
        return "".join(translated_parts)

    @classmethod
    def compile(cls, pattern: str, case_insensitive: bool) -> re.Pattern[str]:
        """Compiles a Postgres-style pattern.

        Args:
            pattern: The ``__posix_regex``/``__iposix_regex`` pattern.
            case_insensitive: Whether the match ignores case.

        Returns:
            The compiled pattern.

        Raises:
            ValueError: The pattern exceeds ``MAX_REGEX_PATTERN_LENGTH``.
            re.error: The pattern isn't valid.
        """
        key = (pattern, case_insensitive)
        compiled = cls.PATTERNS.get(key)
        if compiled is None:
            if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
                raise ValueError(f"regex pattern exceeds the maximum length of {MAX_REGEX_PATTERN_LENGTH} characters")
            compiled = cls.PATTERNS[key] = re.compile(cls.translate(pattern, case_insensitive), re.DOTALL)
        return compiled

    @classmethod
    def search(cls, pattern: str | None, text: str | None, case_insensitive: bool) -> bool | None:
        """Backs the SQLite ``REGEXP``/``MATCH`` functions.

        Returns:
            Whether ``pattern`` matches somewhere in ``text``, or ``None`` when either is NULL.
        """
        # A NULL column value or pattern can't match - SQL NULL (not False) keeps both the filter
        # and its negation from matching, like every other lookup on a NULL column.
        if pattern is None or text is None:
            return None
        return cls.compile(pattern, case_insensitive).search(text) is not None

    @staticmethod
    def posix_regex(term: Term, value: str, text_function: Callable[[Term], Term] | None = None) -> BasicCriterion:
        """``field__posix_regex=value`` on SQLite - matched by Python's ``re`` with the pattern
        translated to Postgres semantics. ``re`` isn't resistant to catastrophic backtracking: a
        pattern as short as ``"(a+)+$"`` can hang the connection, so never pass unvalidated user
        input as ``value``.
        """
        return Lookups.regex_criterion(SqliteRegexMatching.POSIX_REGEX, term, value, text_function)

    @staticmethod
    def insensitive_posix_regex(
        term: Term, value: str, text_function: Callable[[Term], Term] | None = None
    ) -> BasicCriterion:
        """``field__iposix_regex=value`` on SQLite - see ``posix_regex()``'s own docstring for
        the same ReDoS caveat. A character of the pattern matches itself and its simple uppercase and
        lowercase forms, like Postgres's ``~*`` (``i`` matches ``I`` but not ``İ``)."""
        return Lookups.regex_criterion(SqliteRegexMatching.IPOSIX_REGEX, term, value, text_function)

    @staticmethod
    async def install(connection: aiosqlite.Connection):
        """Registers the matching functions of ``__posix_regex``/``__iposix_regex`` on ``connection`` -
        Python's ``re``. Not resistant to catastrophic backtracking; ``MAX_REGEX_PATTERN_LENGTH`` is
        only a sanity ceiling. Never pass a pattern from an untrusted source without a complexity
        limit of your own.
        """

        def regexp(pattern: str | None, text: str | None) -> bool | None:
            return SqlitePosixRegex.search(pattern, text, case_insensitive=False)

        def iregexp(pattern: str | None, text: str | None) -> bool | None:
            return SqlitePosixRegex.search(pattern, text, case_insensitive=True)

        await connection.create_function("regexp", 2, regexp)
        await connection.create_function("match", 2, iregexp)
