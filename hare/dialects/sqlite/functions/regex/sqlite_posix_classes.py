from __future__ import annotations

import re
import unicodedata
from typing import ClassVar

from hare.core.caching.cache import Cache
from hare.dialects.sqlite.functions.constants import (
    POSIX_ALPHA_PATTERN_TEMPLATE,
    POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES,
    POSIX_CASED_CHARACTER_CLASS_NAMES,
    POSIX_CHARACTER_CLASS_PATTERNS,
    POSIX_CHARACTER_CLASS_TRANSLATIONS,
    POSIX_CLASS_MEMBER_CODE_POINT_LIMIT,
    POSIX_COLLECTED_CHARACTER_CLASS_NAMES,
    POSIX_LOWER_CLASS_EXTRA_CHARACTERS,
    POSIX_OTHER_NUMBER_CLASS_NAME,
    POSIX_PUNCT_CATEGORIES,
    REGEX_ERROR_PREFIX,
    REGEX_INVALID_CLASS_MESSAGE,
)
from hare.dialects.sqlite.functions.text.sqlite_case_mapping import SqliteCaseMapping


class SqlitePosixClasses:
    """The POSIX named classes of a Postgres (UTF8) regular expression over the whole Unicode range,
    as Python ``re`` fragments: a fixed member list, a pattern of one member character, or members
    collected from the code points on first use."""

    #: Every character below ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` except the surrogates - made on
    #: first use.
    candidate_characters: ClassVar[str | None] = None
    #: (class name,) -> the members of a collected POSIX class; ("fragment", class name) -> its
    #: bracket-expression fragment.
    CLASSES: ClassVar[Cache[str]] = Cache(2 * len(POSIX_COLLECTED_CHARACTER_CLASS_NAMES))

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
        one-character lowercase/uppercase of their own - ``punct``, or the other numbers.

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
        if class_name == POSIX_OTHER_NUMBER_CLASS_NAME:
            return "".join(character for character in characters if unicodedata.category(character) == "No")
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
    def get_alpha_pattern(cls) -> str:
        """The ``re`` pattern of one ``[[:alpha:]]`` character."""
        return POSIX_ALPHA_PATTERN_TEMPLATE.format(cls.get_class_fragment(POSIX_OTHER_NUMBER_CLASS_NAME))

    @classmethod
    def get_word_pattern(cls) -> str:
        """The ``re`` pattern of one word character - ``[[:alnum:]_]``, what ``\\w`` and the word
        constraints read."""
        return f"(?:{cls.get_alpha_pattern()}|[0-9_])"

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

    @classmethod
    def add_class_items(
        cls, class_name: str, case_insensitive: bool, set_items: list[str], class_patterns: list[str]
    ) -> None:
        """Adds a POSIX named class to a bracket expression being translated.

        Args:
            class_name: The class's name.
            case_insensitive: Whether the match ignores case - ``upper`` and ``lower`` widen to ``alpha``.
            set_items: The bracket expression's ``re`` set items.
            class_patterns: Its alternatives, each a pattern of one character.

        Raises:
            ValueError: ``class_name`` isn't a POSIX class.
        """
        if case_insensitive and class_name in POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES:
            class_name = "alpha"
        if class_name in POSIX_CHARACTER_CLASS_TRANSLATIONS:
            set_items.append(POSIX_CHARACTER_CLASS_TRANSLATIONS[class_name])
        elif class_name == "alpha":
            class_patterns.append(cls.get_alpha_pattern())
        elif class_name == "alnum":
            class_patterns.append(cls.get_alpha_pattern())
            set_items.append("0-9")
        elif class_name in POSIX_CHARACTER_CLASS_PATTERNS:
            class_patterns.append(POSIX_CHARACTER_CLASS_PATTERNS[class_name])
        elif class_name in POSIX_COLLECTED_CHARACTER_CLASS_NAMES and class_name != POSIX_OTHER_NUMBER_CLASS_NAME:
            set_items.append(cls.get_class_fragment(class_name))
        else:
            raise ValueError(REGEX_ERROR_PREFIX + REGEX_INVALID_CLASS_MESSAGE)
