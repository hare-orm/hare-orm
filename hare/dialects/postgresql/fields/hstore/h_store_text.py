from __future__ import annotations

from collections.abc import Mapping


class HStoreText:
    """The text form of an ``hstore`` value - ``"key"=>"value", "other"=>NULL``."""

    @staticmethod
    def quote(text: str) -> str:
        """``text`` as a double-quoted hstore token."""
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @classmethod
    def encode(cls, mapping: Mapping[str, str | None]) -> str:
        """The hstore text of a mapping of strings to strings or None."""
        return ", ".join(
            f"{cls.quote(key)}=>{'NULL' if value is None else cls.quote(value)}" for key, value in mapping.items()
        )

    @staticmethod
    def read_token(text: str, position: int) -> tuple[str | None, int]:
        """Reads one key or value at ``position``, after any whitespace.

        Args:
            text: The hstore text.
            position: Where to start.

        Returns:
            The token (None for an unquoted ``NULL``) and the position after it.

        Raises:
            ValueError: The text ends inside a quoted token, or there's no token at ``position``.
        """
        while position < len(text) and text[position].isspace():
            position += 1
        if position < len(text) and text[position] == '"':
            characters: list[str] = []
            position += 1
            while position < len(text) and text[position] != '"':
                if text[position] == "\\":
                    position += 1
                if position < len(text):
                    characters.append(text[position])
                position += 1
            if position >= len(text):
                raise ValueError(f"unterminated quoted token in hstore text {text!r}")
            return "".join(characters), position + 1
        start = position
        while position < len(text) and not text[position].isspace() and text[position] not in ",=":
            position += 1
        token = text[start:position]
        if not token:
            raise ValueError(f"missing token at {start} in hstore text {text!r}")
        return (None if token.upper() == "NULL" else token), position

    @classmethod
    def parse(cls, text: str) -> dict[str, str | None]:
        """The mapping of an hstore text.

        Raises:
            ValueError: The text isn't a valid hstore value.
        """
        mapping: dict[str, str | None] = {}
        position = 0
        while True:
            while position < len(text) and (text[position].isspace() or text[position] == ","):
                position += 1
            if position >= len(text):
                return mapping
            key, position = cls.read_token(text, position)
            while position < len(text) and text[position].isspace():
                position += 1
            if key is None or not text.startswith("=>", position):
                raise ValueError(f"invalid hstore text {text!r}")
            value, position = cls.read_token(text, position + 2)
            mapping[key] = value
