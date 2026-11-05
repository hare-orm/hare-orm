from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.postgresql.connection.constants import SEARCH_PATH_CASE_TRANSLATION, SEARCH_PATH_WHITESPACE
from hare.dialects.postgresql.connection.messages import INVALID_SEARCH_PATH, INVALID_SEARCH_PATH_ELEMENT
from hare.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True, init=False)
class SearchPath:
    """An ordered PostgreSQL search path, independent of its setting representation.

    Each argument is one exact schema name. PostgreSQL's special entries ``$user`` and
    ``pg_temp`` remain entries, rather than being expanded against a particular connection.
    ``parse()`` consumes setting syntax; the constructor never splits a schema name on commas.
    """

    schemas: tuple[str, ...]

    def __init__(self, *schemas: str) -> None:
        if any(not schema or "\0" in schema for schema in schemas):
            raise ConfigurationError(INVALID_SEARCH_PATH_ELEMENT)
        object.__setattr__(self, "schemas", tuple(schemas))

    @classmethod
    def parse(cls, setting: str) -> SearchPath:
        """Read setting syntax, preserving quoted commas, escaped quotes and letter case.

        Unquoted ASCII letters are folded to lower case, matching a UTF-8 database.
        PostgreSQL setting syntax allows punctuation in unquoted names; only commas and
        scanner whitespace delimit them. An empty setting is an empty path; empty elements
        and unterminated identifiers are errors. Identifier length is left to the server.
        This parser never reads SQL.
        """
        position = 0
        schemas: list[str] = []
        length = len(setting)
        while position < length:
            while position < length and setting[position] in SEARCH_PATH_WHITESPACE:
                position += 1
            if position == length:
                if schemas:
                    raise ConfigurationError(INVALID_SEARCH_PATH)
                break
            if setting[position] == '"':
                position += 1
                characters = []
                while position < length:
                    character = setting[position]
                    position += 1
                    if character == '"':
                        if position < length and setting[position] == '"':
                            characters.append('"')
                            position += 1
                            continue
                        break
                    characters.append(character)
                else:
                    raise ConfigurationError(INVALID_SEARCH_PATH)
                name = "".join(characters)
            else:
                start = position
                while (
                    position < length and setting[position] != "," and setting[position] not in SEARCH_PATH_WHITESPACE
                ):
                    position += 1
                name = setting[start:position]
                if not name:
                    raise ConfigurationError(INVALID_SEARCH_PATH)
                name = name.translate(SEARCH_PATH_CASE_TRANSLATION)
            schemas.append(name)
            while position < length and setting[position] in SEARCH_PATH_WHITESPACE:
                position += 1
            if position == length:
                break
            if setting[position] != ",":
                raise ConfigurationError(INVALID_SEARCH_PATH)
            position += 1
            if position == length:
                raise ConfigurationError(INVALID_SEARCH_PATH)
        return cls(*schemas)

    def to_setting(self) -> str:
        """Encode all names as quoted identifiers, including PostgreSQL's special entries."""
        return ", ".join('"' + schema.replace('"', '""') + '"' for schema in self.schemas)

    def prepended(self, schema: str, *, replacing: str | None = None) -> SearchPath:
        """Put a schema first, removing a previous tenant only when it is the first entry."""
        schemas = self.schemas[1:] if replacing is not None and self.schemas[:1] == (replacing,) else self.schemas
        return SearchPath(schema, *schemas)

    def to_shell_option(self) -> str:
        """Encode the setting as one libpq PGOPTIONS argument, escaping whitespace and backslashes."""
        value = self.to_setting()
        escaped = "".join(
            "\\" + character if character.isspace() or character == "\\" else character for character in value
        )
        return f"-c search_path={escaped}"
