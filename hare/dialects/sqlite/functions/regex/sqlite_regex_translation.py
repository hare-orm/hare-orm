from __future__ import annotations

import re

from hare.dialects.sqlite.enums import SqliteRegexSyntax
from hare.dialects.sqlite.functions.constants import (
    POSIX_CHARACTER_CLASS_TRANSLATIONS,
    REGEX_ADVANCED_DIRECTOR,
    REGEX_BOUND_PATTERN,
    REGEX_CHARACTER_ESCAPES,
    REGEX_COLLATING_ELEMENT_NAMES,
    REGEX_DIGITS_PATTERN,
    REGEX_ERROR_PREFIX,
    REGEX_ESCAPE_ARGUMENT_LENGTHS,
    REGEX_EXPANDED_WHITESPACE,
    REGEX_HEX_DIGITS_PATTERN,
    REGEX_INVALID_BACKREFERENCE_MESSAGE,
    REGEX_INVALID_COLLATING_ELEMENT_MESSAGE,
    REGEX_INVALID_ESCAPE_MESSAGE,
    REGEX_INVALID_OPTION_MESSAGE,
    REGEX_INVALID_RANGE_MESSAGE,
    REGEX_INVALID_REPETITION_MESSAGE,
    REGEX_LITERAL_DIRECTOR,
    REGEX_MAX_CODE_POINT,
    REGEX_MAX_REPETITION,
    REGEX_OCTAL_DIGITS_PATTERN,
    REGEX_OPTION_LETTERS,
    REGEX_QUANTIFIER_OPERAND_MESSAGE,
    REGEX_UNBALANCED_BRACES_MESSAGE,
    REGEX_UNBALANCED_BRACKETS_MESSAGE,
    REGEX_UNBALANCED_PARENTHESES_MESSAGE,
    REGEX_WORD_END_BRACKET,
    REGEX_WORD_START_BRACKET,
)
from hare.dialects.sqlite.functions.regex.sqlite_posix_classes import SqlitePosixClasses
from hare.dialects.sqlite.functions.text.sqlite_case_mapping import SqliteCaseMapping


class SqliteRegexTranslation:
    """One ``__posix_regex``/``__iposix_regex`` pattern translated into a Python ``re`` pattern
    (compiled with DOTALL) matching as a Postgres (UTF8) regular expression: its escapes (``\\b`` a
    backspace, ``\\y`` a word boundary, ``\\x`` of any length, octal escapes and back references), its
    POSIX classes, collating elements and word constraints, its embedded options (``(?n)``,
    ``(?x)``, ``(?q)``...) and ``***`` directors, its extended and basic syntaxes, and its errors, with
    Postgres's messages. A case-insensitive match accepts exactly a character's simple uppercase and
    lowercase forms.

    Args:
        pattern: The pattern.
        case_insensitive: Whether the match ignores case - ``(?i)``/``(?c)`` in the pattern decide
            over it.
    """

    __slots__ = (
        "pattern",
        "position",
        "case_insensitive",
        "newline_sensitive",
        "newline_anchors",
        "expanded",
        "syntax",
        "group_count",
        "open_groups",
        "parts",
        "can_repeat",
    )

    def __init__(self, pattern: str, case_insensitive: bool) -> None:
        self.pattern = pattern
        self.position = 0
        self.case_insensitive = case_insensitive
        #: Whether ``.`` and a negated bracket expression leave a newline out.
        self.newline_sensitive = False
        #: Whether ``^`` and ``$`` match at each line.
        self.newline_anchors = False
        #: Whether whitespace and ``#`` comments outside a bracket expression are skipped.
        self.expanded = False
        self.syntax = SqliteRegexSyntax.ADVANCED
        #: The capturing groups opened so far - a back reference names one of them.
        self.group_count = 0
        #: Whether each open group takes a quantifier once closed - a lookaround doesn't.
        self.open_groups: list[bool] = []
        self.parts: list[str] = []
        #: Whether the last translated part takes a quantifier.
        self.can_repeat = False

    @staticmethod
    def get_error(message: str) -> ValueError:
        """The error of an invalid pattern, with Postgres's message.

        Args:
            message: The message after ``REGEX_ERROR_PREFIX``.

        Returns:
            The error.
        """
        return ValueError(REGEX_ERROR_PREFIX + message)

    def translate(self) -> str:
        """The ``re`` pattern.

        Raises:
            ValueError: The pattern isn't a valid Postgres regular expression.
        """
        self.read_director()
        self.read_options()
        if self.syntax is SqliteRegexSyntax.LITERAL:
            return "".join(self.get_literal_sql(character) for character in self.pattern[self.position :])
        while self.position < len(self.pattern):
            if self.syntax is SqliteRegexSyntax.BASIC:
                self.translate_basic_next()
            else:
                self.translate_next()
        if self.open_groups:
            raise self.get_error(REGEX_UNBALANCED_PARENTHESES_MESSAGE)
        return "".join(self.parts)

    def read_director(self) -> None:
        """Reads a ``***=`` (literal text) or ``***:`` (advanced syntax) director."""
        if self.pattern.startswith(REGEX_LITERAL_DIRECTOR):
            self.syntax = SqliteRegexSyntax.LITERAL
            self.position = len(REGEX_LITERAL_DIRECTOR)
        elif self.pattern.startswith(REGEX_ADVANCED_DIRECTOR):
            self.position = len(REGEX_ADVANCED_DIRECTOR)

    def read_options(self) -> None:
        """Reads the embedded options at the pattern's start - ``(?in)``.

        Raises:
            ValueError: A letter isn't an option.
        """
        start = self.position
        if self.syntax is SqliteRegexSyntax.LITERAL or not self.pattern.startswith("(?", start):
            return
        if start + 2 >= len(self.pattern) or not self.pattern[start + 2].isalpha():
            return
        end = self.pattern.find(")", start + 2)
        letters = self.pattern[start + 2 : end] if end != -1 else self.pattern[start + 2 :]
        if end == -1 or any(letter not in REGEX_OPTION_LETTERS for letter in letters):
            raise self.get_error(REGEX_INVALID_OPTION_MESSAGE)
        for letter in letters:
            self.apply_option(letter)
        self.position = end + 1

    def apply_option(self, letter: str) -> None:
        """Applies one embedded option letter."""
        if letter == "b":
            self.syntax = SqliteRegexSyntax.BASIC
        elif letter == "e":
            self.syntax = SqliteRegexSyntax.EXTENDED
        elif letter == "q":
            self.syntax = SqliteRegexSyntax.LITERAL
        elif letter in "ic":
            self.case_insensitive = letter == "i"
        elif letter in "mn":
            self.newline_sensitive = self.newline_anchors = True
        elif letter in "pw":
            self.newline_sensitive, self.newline_anchors = letter == "p", letter == "w"
        elif letter == "s":
            self.newline_sensitive = self.newline_anchors = False
        else:
            self.expanded = letter == "x"

    def add_part(self, part: str, can_repeat: bool) -> None:
        """Appends a translated part.

        Args:
            part: Its ``re`` pattern.
            can_repeat: Whether it takes a quantifier.
        """
        self.parts.append(part)
        self.can_repeat = can_repeat

    def get_literal_sql(self, character: str) -> str:
        """The ``re`` pattern of one literal character - with its simple case variants in a
        case-insensitive match."""
        set_items = self.get_literal_set_items(character)
        return set_items[0] if len(set_items) == 1 else f"[{''.join(set_items)}]"

    def get_literal_set_items(self, character: str) -> list[str]:
        """The bracket-expression items matching one literal character."""
        characters = SqliteCaseMapping.get_case_variants(character) if self.case_insensitive else {character}
        return [re.escape(variant) for variant in sorted(characters)]

    def translate_next(self) -> None:
        """Translates the next part of an advanced or extended pattern."""
        character = self.pattern[self.position]
        if self.expanded and character in REGEX_EXPANDED_WHITESPACE:
            self.position += 1
        elif self.expanded and character == "#":
            newline = self.pattern.find("\n", self.position)
            self.position = len(self.pattern) if newline == -1 else newline + 1
        elif character == "\\":
            self.translate_escape()
        elif character == "[":
            self.translate_bracket()
        elif character == "(":
            self.open_group()
        elif character == ")":
            self.position += 1
            self.close_group()
        elif character == "|":
            self.position += 1
            self.add_part("|", can_repeat=False)
        elif character in "*+?":
            self.position += 1
            self.add_quantifier(character)
        elif character == "{" and self.pattern[self.position + 1 : self.position + 2].isdigit():
            self.position += 1
            self.add_quantifier(self.read_bound("}"))
        else:
            self.position += 1
            self.translate_plain_character(character)

    def translate_plain_character(self, character: str) -> None:
        """Translates an anchor, a dot or a literal character."""
        if character == "^":
            self.add_part("(?m:^)" if self.newline_anchors else "^", can_repeat=False)
        elif character == "$":
            self.add_part("(?m:$)" if self.newline_anchors else r"\Z", can_repeat=False)
        elif character == ".":
            self.add_part("[^\\n]" if self.newline_sensitive else ".", can_repeat=True)
        else:
            self.add_part(self.get_literal_sql(character), can_repeat=True)

    def add_quantifier(self, quantifier: str) -> None:
        """Appends a quantifier to the last part, lazy when ``?`` follows.

        Raises:
            ValueError: The last part takes no quantifier.
        """
        if not self.can_repeat:
            raise self.get_error(REGEX_QUANTIFIER_OPERAND_MESSAGE)
        if self.syntax is not SqliteRegexSyntax.BASIC and self.pattern.startswith("?", self.position):
            quantifier += "?"
            self.position += 1
        self.add_part(quantifier, can_repeat=False)

    def read_bound(self, closing: str) -> str:
        """Reads a bound after its ``{`` - ``m``, ``m,`` or ``m,n``, up to ``closing``.

        Returns:
            The bound as ``re`` writes it.

        Raises:
            ValueError: The bound isn't closed, or a count is above ``REGEX_MAX_REPETITION`` or the
                least above the most.
        """
        match = REGEX_BOUND_PATTERN.match(self.pattern, self.position)
        end = match.end() if match is not None else self.position
        if match is None or not self.pattern.startswith(closing, end):
            raise self.get_error(REGEX_UNBALANCED_BRACES_MESSAGE)
        self.position = end + len(closing)
        least = int(match.group(1))
        most = None if match.group(2) is None else (int(match.group(3)) if match.group(3) else -1)
        counts = [least] if most is None or most == -1 else [least, most]
        if any(count > REGEX_MAX_REPETITION for count in counts) or (len(counts) == 2 and least > counts[1]):
            raise self.get_error(REGEX_INVALID_REPETITION_MESSAGE)
        if most is None:
            return f"{{{least}}}"
        return f"{{{least},}}" if most == -1 else f"{{{least},{most}}}"

    def open_group(self) -> None:
        """Translates a ``(`` - a capturing group, ``(?:``, a lookaround or a ``(?#...)`` comment.

        Raises:
            ValueError: ``(?`` is followed by none of them.
        """
        self.position += 1
        if not self.pattern.startswith("?", self.position) or self.syntax is SqliteRegexSyntax.EXTENDED:
            self.group_count += 1
            self.open_groups.append(True)
            self.add_part("(", can_repeat=False)
            return
        for prefix, can_repeat in ((":", True), ("=", False), ("!", False), ("<=", False), ("<!", False)):
            if self.pattern.startswith(prefix, self.position + 1):
                self.position += 1 + len(prefix)
                self.open_groups.append(can_repeat)
                self.add_part(f"(?{prefix}", can_repeat=False)
                return
        if self.pattern.startswith("#", self.position + 1):
            comment_end = self.pattern.find(")", self.position)
            if comment_end == -1:
                raise self.get_error(REGEX_UNBALANCED_PARENTHESES_MESSAGE)
            self.position = comment_end + 1
            return
        # A ( followed by a quantifier with nothing to repeat, as Postgres reads it.
        raise self.get_error(REGEX_QUANTIFIER_OPERAND_MESSAGE)

    def close_group(self) -> None:
        """Translates a ``)``.

        Raises:
            ValueError: No group is open.
        """
        if not self.open_groups:
            raise self.get_error(REGEX_UNBALANCED_PARENTHESES_MESSAGE)
        self.add_part(")", can_repeat=self.open_groups.pop())

    def translate_escape(self) -> None:
        """Translates a backslash escape of an advanced or extended pattern.

        Raises:
            ValueError: The escape isn't one of Postgres's, or names a group not opened.
        """
        if self.position + 1 >= len(self.pattern):
            raise self.get_error(REGEX_INVALID_ESCAPE_MESSAGE)
        letter = self.pattern[self.position + 1]
        if self.syntax is SqliteRegexSyntax.EXTENDED or not letter.isalnum():
            self.position += 2
            self.add_part(self.get_literal_sql(letter), can_repeat=True)
            return
        if letter.isdigit():
            self.translate_number_escape()
            return
        constraint_pattern = self.get_constraint_pattern(letter)
        if constraint_pattern is not None:
            self.position += 2
            self.add_part(constraint_pattern, can_repeat=False)
            return
        class_pattern = self.get_class_escape_pattern(letter)
        if class_pattern is not None:
            self.position += 2
            self.add_part(class_pattern, can_repeat=True)
            return
        self.add_part(self.get_literal_sql(self.read_character_escape()), can_repeat=True)

    @staticmethod
    def get_constraint_pattern(letter: str) -> str | None:
        """The ``re`` pattern of a constraint escape - ``\\A``, ``\\Z``, and ``\\m``, ``\\M``, ``\\y``,
        ``\\Y`` of the start, the end, a boundary and no boundary of a word.

        Returns:
            The pattern, None for another letter.
        """
        word = SqlitePosixClasses.get_word_pattern()
        return {
            "A": r"\A",
            "Z": r"\Z",
            "m": f"(?<!{word})(?={word})",
            "M": f"(?<={word})(?!{word})",
            "y": f"(?:(?<={word})(?!{word})|(?<!{word})(?={word}))",
            "Y": f"(?:(?<={word})(?={word})|(?<!{word})(?!{word}))",
        }.get(letter)

    def get_class_escape_pattern(self, letter: str) -> str | None:
        """The ``re`` pattern of a class escape - ``\\d``, ``\\s``, ``\\w`` and their negations.

        Returns:
            The pattern, None for another letter.
        """
        set_items: list[str] = []
        class_patterns: list[str] = []
        if not self.add_class_escape_items(letter.lower(), set_items, class_patterns):
            return None
        return self.get_set_sql(set_items, class_patterns, negated=letter.isupper())

    @staticmethod
    def add_class_escape_items(letter: str, set_items: list[str], class_patterns: list[str]) -> bool:
        """Adds the members of ``\\d``, ``\\s`` or ``\\w`` to a set being translated.

        Returns:
            Whether the letter is one of them.
        """
        if letter == "d":
            set_items.append("0-9")
        elif letter == "s":
            set_items.append(POSIX_CHARACTER_CLASS_TRANSLATIONS["space"])
        elif letter == "w":
            class_patterns.append(SqlitePosixClasses.get_alpha_pattern())
            set_items.append("0-9_")
        else:
            return False
        return True

    def read_character_escape(self) -> str:
        """Reads an escape standing for one character - ``\\n``, ``\\cX``, ``\\x41``, ``\\u0041``.

        Returns:
            The character.

        Raises:
            ValueError: The escape stands for no character.
        """
        letter = self.pattern[self.position + 1]
        self.position += 2
        if letter in REGEX_CHARACTER_ESCAPES:
            return REGEX_CHARACTER_ESCAPES[letter]
        if letter == "c" and self.position < len(self.pattern):
            self.position += 1
            return chr(ord(self.pattern[self.position - 1]) & 0x1F)
        if letter == "x":
            match = REGEX_HEX_DIGITS_PATTERN.match(self.pattern, self.position)
            digits = match.group() if match is not None else ""
        elif letter in REGEX_ESCAPE_ARGUMENT_LENGTHS:
            digits = self.pattern[self.position : self.position + REGEX_ESCAPE_ARGUMENT_LENGTHS[letter]]
            if len(digits) != REGEX_ESCAPE_ARGUMENT_LENGTHS[letter] or not REGEX_HEX_DIGITS_PATTERN.fullmatch(digits):
                digits = ""
        else:
            digits = ""
        if not digits or int(digits, 16) > REGEX_MAX_CODE_POINT or 0xD800 <= int(digits, 16) <= 0xDFFF:
            raise self.get_error(REGEX_INVALID_ESCAPE_MESSAGE)
        self.position += len(digits)
        return chr(int(digits, 16))

    def translate_number_escape(self) -> None:
        """Translates a backslash followed by digits - a back reference by one digit or by the number
        of an opened group, an octal escape otherwise, as Postgres reads it.

        Raises:
            ValueError: One digit names a group not opened, or the digits aren't octal.
        """
        digits = REGEX_DIGITS_PATTERN.match(self.pattern, self.position + 1).group()
        if digits[0] != "0" and (len(digits) == 1 or int(digits) <= self.group_count):
            if int(digits) > self.group_count:
                raise self.get_error(REGEX_INVALID_BACKREFERENCE_MESSAGE)
            self.position += 1 + len(digits)
            self.add_part(self.get_backreference_sql(int(digits)), can_repeat=True)
            return
        self.add_part(self.get_literal_sql(self.read_octal_escape()), can_repeat=True)

    def get_backreference_sql(self, number: int | str) -> str:
        """The ``re`` pattern of a back reference - matched ignoring case in a case-insensitive match.

        Args:
            number: The group's number.

        Returns:
            The pattern.
        """
        return f"(?{'i' if self.case_insensitive else ''}:\\{number})"

    def read_octal_escape(self) -> str:
        """Reads an octal escape - one to three octal digits, the last left out where the value
        would pass 255.

        Returns:
            The character.

        Raises:
            ValueError: The first digit isn't octal.
        """
        match = REGEX_OCTAL_DIGITS_PATTERN.match(self.pattern, self.position + 1)
        if match is None:
            raise self.get_error(REGEX_INVALID_ESCAPE_MESSAGE)
        digits = match.group()
        if int(digits, 8) > 0xFF:
            digits = digits[:-1]
        self.position += 1 + len(digits)
        return chr(int(digits, 8))

    def translate_bracket(self) -> None:
        """Translates the bracket expression at the position - ``[[:<:]]`` and ``[[:>:]]`` the start
        and the end of a word.

        Raises:
            ValueError: The expression isn't closed, or holds an invalid item.
        """
        for bracket, letter in ((REGEX_WORD_START_BRACKET, "m"), (REGEX_WORD_END_BRACKET, "M")):
            if self.pattern.startswith(bracket, self.position):
                self.position += len(bracket)
                self.add_part(self.get_constraint_pattern(letter) or "", can_repeat=False)
                return
        self.position += 1
        negated = self.pattern.startswith("^", self.position)
        if negated:
            self.position += 1
        set_items: list[str] = []
        class_patterns: list[str] = []
        negated_patterns: list[str] = []
        is_first_item = True
        while True:
            if self.position >= len(self.pattern):
                raise self.get_error(REGEX_UNBALANCED_BRACKETS_MESSAGE)
            if self.pattern[self.position] == "]" and not is_first_item:
                self.position += 1
                break
            is_first_item = False
            self.read_bracket_item(set_items, class_patterns, negated_patterns)
        if negated_patterns:
            class_patterns.append(f"(?:(?!{'|'.join(negated_patterns)}).)")
        self.add_part(self.get_set_sql(set_items, class_patterns, negated), can_repeat=True)

    def read_bracket_item(self, set_items: list[str], class_patterns: list[str], negated_patterns: list[str]) -> None:
        """Reads one item of a bracket expression - a class, a character or a range.

        Raises:
            ValueError: The item isn't valid.
        """
        if self.pattern.startswith("[:", self.position):
            class_end = self.pattern.find(":]", self.position + 2)
            if class_end == -1:
                raise self.get_error(REGEX_UNBALANCED_BRACKETS_MESSAGE)
            SqlitePosixClasses.add_class_items(
                self.pattern[self.position + 2 : class_end], self.case_insensitive, set_items, class_patterns
            )
            self.position = class_end + 2
            return
        if (
            self.syntax is SqliteRegexSyntax.ADVANCED
            and self.pattern.startswith("\\", self.position)
            and self.position + 1 < len(self.pattern)
        ):
            letter = self.pattern[self.position + 1]
            if letter.lower() in "dsw" and letter.isalpha():
                self.position += 2
                escape_items: list[str] = []
                escape_patterns: list[str] = []
                self.add_class_escape_items(letter.lower(), escape_items, escape_patterns)
                if letter.isupper():
                    negated_patterns.append(self.get_set_sql(escape_items, escape_patterns, negated=False))
                else:
                    set_items.extend(escape_items)
                    class_patterns.extend(escape_patterns)
                return
        first_character = self.read_bracket_character()
        if (
            self.pattern.startswith("-", self.position)
            and self.position + 1 < len(self.pattern)
            and self.pattern[self.position + 1] != "]"
        ):
            self.position += 1
            last_character = self.read_bracket_character()
            if first_character > last_character:
                raise self.get_error(REGEX_INVALID_RANGE_MESSAGE)
            set_items.append(f"{re.escape(first_character)}-{re.escape(last_character)}")
            if self.case_insensitive:
                set_items.extend(
                    re.escape(variant)
                    for variant in SqlitePosixClasses.get_range_case_variants(first_character, last_character)
                )
            return
        set_items.extend(self.get_literal_set_items(first_character))

    def read_bracket_character(self) -> str:
        """Reads one character of a bracket expression - itself, a collating element (``[.hyphen.]``),
        an equivalence class (``[=a=]``) or, in an advanced pattern, a backslash escape.

        Returns:
            The character.

        Raises:
            ValueError: An element or an escape stands for no character.
        """
        for opening, closing in (("[.", ".]"), ("[=", "=]")):
            if self.pattern.startswith(opening, self.position):
                end = self.pattern.find(closing, self.position + 2)
                if end == -1:
                    raise self.get_error(REGEX_UNBALANCED_BRACKETS_MESSAGE)
                name = self.pattern[self.position + 2 : end]
                self.position = end + 2
                if len(name) == 1:
                    return name
                if name not in REGEX_COLLATING_ELEMENT_NAMES:
                    raise self.get_error(REGEX_INVALID_COLLATING_ELEMENT_MESSAGE)
                return REGEX_COLLATING_ELEMENT_NAMES[name]
        character = self.pattern[self.position]
        if character != "\\" or self.syntax is not SqliteRegexSyntax.ADVANCED:
            self.position += 1
            return character
        if self.position + 1 >= len(self.pattern):
            raise self.get_error(REGEX_UNBALANCED_BRACKETS_MESSAGE)
        letter = self.pattern[self.position + 1]
        if not letter.isalnum():
            self.position += 2
            return letter
        if letter.isdigit():
            return self.read_octal_escape()
        return self.read_character_escape()

    def get_set_sql(self, set_items: list[str], class_patterns: list[str], negated: bool) -> str:
        """The ``re`` pattern of one character of a set - a newline left out of a negated one in a
        newline-sensitive match.

        Args:
            set_items: Its ``re`` set items.
            class_patterns: Its alternatives, each a pattern of one character.
            negated: Whether the set matches the characters outside it.

        Returns:
            The pattern.
        """
        if negated and self.newline_sensitive:
            set_items = [*set_items, "\\n"]
        set_text = "".join(set_items)
        if not class_patterns:
            return f"[{'^' if negated else ''}{set_text}]" if set_text or negated else "(?!)"
        if not negated:
            alternatives = [f"[{set_text}]", *class_patterns] if set_text else class_patterns
            return f"(?:{'|'.join(alternatives)})"
        remaining_character = f"[^{set_text}]" if set_text else "."
        return f"(?:(?!{'|'.join(class_patterns)}){remaining_character})"

    def translate_basic_next(self) -> None:
        """Translates the next part of a basic pattern - ``\\(``, ``\\)`` and ``\\{`` group and repeat,
        ``*`` at its start is literal, ``^`` an anchor at its start and ``$`` at its end only.

        Raises:
            ValueError: An escape or a bound isn't valid.
        """
        character = self.pattern[self.position]
        at_start = not self.parts or self.parts[-1] in ("(", "^")
        if character == "\\":
            if self.position + 1 >= len(self.pattern):
                raise self.get_error(REGEX_INVALID_ESCAPE_MESSAGE)
            letter = self.pattern[self.position + 1]
            self.position += 2
            if letter == "(":
                self.group_count += 1
                self.open_groups.append(True)
                self.add_part("(", can_repeat=False)
            elif letter == ")":
                self.close_group()
            elif letter == "{":
                self.add_quantifier(self.read_bound("\\}"))
            elif letter in "123456789":
                if int(letter) > self.group_count:
                    raise self.get_error(REGEX_INVALID_BACKREFERENCE_MESSAGE)
                self.add_part(self.get_backreference_sql(letter), can_repeat=True)
            elif letter in "<>":
                self.add_part(self.get_constraint_pattern("m" if letter == "<" else "M") or "", can_repeat=False)
            else:
                self.add_part(self.get_literal_sql(letter), can_repeat=True)
        elif character == "[":
            self.translate_bracket()
        elif character == "*" and not at_start:
            self.position += 1
            self.add_quantifier("*")
        elif character == "^" and at_start:
            self.position += 1
            self.add_part("(?m:^)" if self.newline_anchors else "^", can_repeat=False)
        elif character == "$" and (
            self.position + 1 == len(self.pattern) or self.pattern.startswith("\\)", self.position + 1)
        ):
            self.position += 1
            self.add_part("(?m:$)" if self.newline_anchors else r"\Z", can_repeat=False)
        elif character == ".":
            self.position += 1
            self.add_part("[^\\n]" if self.newline_sensitive else ".", can_repeat=True)
        else:
            self.position += 1
            self.add_part(self.get_literal_sql(character), can_repeat=True)
