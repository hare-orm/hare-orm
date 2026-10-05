from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any, ClassVar

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    TSVECTOR_LETTERS_BY_WEIGHT,
    TSVECTOR_LEXEME_BYTES_LIMIT,
    TSVECTOR_MAX_BYTES,
    TSVECTOR_MAX_POSITION,
    TSVECTOR_MAX_POSITIONS,
    TSVECTOR_WEIGHT_SHIFT,
    TSVECTOR_WEIGHTS_BY_LETTER,
    TSVECTOR_WHITESPACE,
)
from hare.native.native_modules import NativeModules


class AsyncpgTsvectorCodec:
    """The binary ``tsvector`` codec of an asyncpg connection, taking and giving the text PostgreSQL
    reads and writes. asyncpg has none of its own, and its text array parser crashes on an empty
    ``tsvector`` in an array; the text is read as ``tsvectorin`` reads it."""

    #: The compiled decoder - None without the compiled extension.
    native_decoder: ClassVar[Any] = getattr(NativeModules.rows, "read_tsvector_text", None)

    @classmethod
    def encode(cls, value: str) -> bytes:
        """Encodes a ``tsvector`` text as PostgreSQL's binary ``tsvector``.

        Args:
            value: The text.

        Returns:
            The binary value.

        Raises:
            TypeError: The value isn't a string.
            ValueError: The text isn't a ``tsvector``, with PostgreSQL's message.
        """
        if not isinstance(value, str):
            raise TypeError(f"expected str, got {type(value).__name__}")
        if "\x00" in value:
            raise ValueError('invalid byte sequence for encoding "UTF8": 0x00')
        positions_by_lexeme: dict[bytes, list[tuple[int, int]] | None] = {}
        total_bytes = 0
        for lexeme, positions in cls.read_lexemes(value):
            lexeme_bytes = lexeme.encode()
            if len(lexeme_bytes) >= TSVECTOR_LEXEME_BYTES_LIMIT:
                raise ValueError(
                    f"word is too long ({len(lexeme_bytes)} bytes, max {TSVECTOR_LEXEME_BYTES_LIMIT - 1} bytes)"
                )
            if total_bytes > TSVECTOR_MAX_BYTES:
                raise ValueError(
                    f"string is too long for tsvector ({total_bytes} bytes, max {TSVECTOR_MAX_BYTES} bytes)"
                )
            total_bytes += len(lexeme_bytes)
            known_positions = positions_by_lexeme.get(lexeme_bytes)
            if positions is None:
                positions_by_lexeme.setdefault(lexeme_bytes, None)
            elif known_positions is None:
                positions_by_lexeme[lexeme_bytes] = positions
            else:
                known_positions.extend(positions)
        parts = [struct.pack("!i", len(positions_by_lexeme))]
        value_bytes = 0
        for lexeme_bytes in sorted(positions_by_lexeme):
            packed_positions = cls.get_unique_positions(positions_by_lexeme[lexeme_bytes] or [])
            value_bytes += len(lexeme_bytes)
            if packed_positions:
                value_bytes = value_bytes + value_bytes % 2 + 2 + 2 * len(packed_positions)
            parts.append(
                lexeme_bytes
                + b"\x00"
                + struct.pack(f"!H{len(packed_positions)}H", len(packed_positions), *packed_positions)
            )
        if value_bytes > TSVECTOR_MAX_BYTES:
            raise ValueError(f"string is too long for tsvector ({value_bytes} bytes, max {TSVECTOR_MAX_BYTES} bytes)")
        return b"".join(parts)

    @staticmethod
    def get_unique_positions(positions: list[tuple[int, int]]) -> list[int]:
        """A lexeme's positions as ``tsvectorin`` keeps them: sorted, one per position with its highest
        weight, at most TSVECTOR_MAX_POSITIONS and none past TSVECTOR_MAX_POSITION.

        Args:
            positions: The positions read, each with its weight.

        Returns:
            Each position with its weight in its upper bits.
        """
        if len(positions) <= 1:
            return [position | weight << TSVECTOR_WEIGHT_SHIFT for position, weight in positions]
        ordered = sorted(positions, key=lambda position_and_weight: position_and_weight[0])
        unique = [list(ordered[0])]
        for position, weight in ordered[1:]:
            if position != unique[-1][0]:
                unique.append([position, weight])
                if len(unique) >= TSVECTOR_MAX_POSITIONS or position == TSVECTOR_MAX_POSITION:
                    break
            elif weight > unique[-1][1]:
                unique[-1][1] = weight
        return [position | weight << TSVECTOR_WEIGHT_SHIFT for position, weight in unique]

    @staticmethod
    def raise_syntax_error(text: str) -> None:
        """Raises PostgreSQL's error of a text that isn't a ``tsvector``.

        Raises:
            ValueError: Always.
        """
        raise ValueError(f'syntax error in tsvector: "{text}"')

    @classmethod
    def read_lexemes(cls, text: str) -> list[tuple[str, list[tuple[int, int]] | None]]:
        """The lexemes of a ``tsvector`` text, in the order written, as ``tsvectorin`` reads them.

        Args:
            text: The text.

        Returns:
            Each lexeme with its positions and weights - None for a lexeme written without positions.

        Raises:
            ValueError: The text isn't a ``tsvector``.
        """
        lexemes: list[tuple[str, list[tuple[int, int]] | None]] = []
        length = len(text)
        index = 0
        while True:
            while index < length and text[index] in TSVECTOR_WHITESPACE:
                index += 1
            if index >= length:
                return lexemes
            characters: list[str] = []
            has_positions = False
            if text[index] == "'":
                index += 1
                while True:
                    if index >= length:
                        cls.raise_syntax_error(text)
                    character = text[index]
                    if character == "'":
                        if index + 1 < length and text[index + 1] == "'":
                            characters.append("'")
                            index += 2
                            continue
                        index += 1
                        break
                    if character == "\\":
                        index += 1
                        if index >= length:
                            raise ValueError(f'there is no escaped character: "{text}"')
                    characters.append(text[index])
                    index += 1
                if not characters:
                    cls.raise_syntax_error(text)
                if index < length and text[index] == ":":
                    has_positions = True
                    index += 1
            else:
                # The first character starts the word - a ":" too.
                while index < length and text[index] not in TSVECTOR_WHITESPACE:
                    character = text[index]
                    if character == "\\":
                        index += 1
                        if index >= length:
                            raise ValueError(f'there is no escaped character: "{text}"')
                    elif character == ":" and characters:
                        has_positions = True
                        index += 1
                        break
                    characters.append(text[index])
                    index += 1
            positions = None
            if has_positions:
                positions, index = cls.read_positions(text, index)
            lexemes.append(("".join(characters), positions))

    @classmethod
    def read_positions(cls, text: str, index: int) -> tuple[list[tuple[int, int]], int]:
        """Reads the positions after a lexeme's ``:``.

        Args:
            text: The text.
            index: Where the first position starts.

        Returns:
            The positions with their weights, and where the text goes on.

        Raises:
            ValueError: The positions aren't written right, or one is 0.
        """
        length = len(text)
        positions: list[tuple[int, int]] = []
        while True:
            start = index
            while index < length and "0" <= text[index] <= "9":
                index += 1
            if index == start:
                cls.raise_syntax_error(text)
            position = min(int(text[start:index]), TSVECTOR_MAX_POSITION)
            if position == 0:
                raise ValueError(f'wrong position info in tsvector: "{text}"')
            weight = 0
            while True:
                if index >= length or text[index] in TSVECTOR_WHITESPACE:
                    positions.append((position, weight))
                    return positions, index
                character = text[index]
                if character == ",":
                    index += 1
                    break
                if character in TSVECTOR_WEIGHTS_BY_LETTER:
                    if weight:
                        cls.raise_syntax_error(text)
                    weight = TSVECTOR_WEIGHTS_BY_LETTER[character]
                elif not "0" <= character <= "9":
                    # A digit after the weight is skipped.
                    cls.raise_syntax_error(text)
                index += 1
            positions.append((position, weight))

    @classmethod
    def get_decoder(cls) -> Callable[[bytes], str]:
        """The decoder of the binary value - the compiled one, ``decode()`` without it."""
        return cls.native_decoder or cls.decode

    @staticmethod
    def decode(value: bytes) -> str:
        """Decodes PostgreSQL's binary ``tsvector`` into its text, as ``tsvectorout`` writes it.

        Args:
            value: The binary value.

        Returns:
            The text.
        """
        letters = TSVECTOR_LETTERS_BY_WEIGHT
        count = int.from_bytes(value[:4], "big")
        offset = 4
        parts = []
        for __ in range(count):
            end = value.index(b"\x00", offset)
            lexeme = value[offset:end].decode()
            if "'" in lexeme or "\\" in lexeme:
                lexeme = lexeme.replace("\\", "\\\\").replace("'", "''")
            position_count = value[end + 1] << 8 | value[end + 2]
            offset = end + 3
            if not position_count:
                parts.append(f"'{lexeme}'")
                continue
            stop = offset + 2 * position_count
            # A position's two bytes: the weight in the upper two bits, the position below.
            written_positions = ",".join(
                [
                    f"{(value[index] & 63) << 8 | value[index + 1]}{letters[value[index] >> 6]}"
                    for index in range(offset, stop, 2)
                ]
            )
            offset = stop
            parts.append(f"'{lexeme}':{written_positions}")
        return " ".join(parts)
