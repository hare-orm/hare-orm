from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.classes.class_path import ClassPath
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.declarations.environment_value import EnvironmentValue
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD,
    CLICKHOUSE_DICTIONARY_SECRET_OPTIONS,
)
from hare.dialects.clickhouse.schema.messages import DICTIONARY_SOURCE_DUPLICATE_OPTION, DICTIONARY_SOURCE_IDENTIFIER
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.literals.sql_literals import SqlLiterals


@dataclass(frozen=True, init=False)
class DictionarySource:
    """A source type and explicit options, with environment values bound at execution.

    Strings and other literal values are rendered by the dialect. RawSQLTerm is an
    explicit SQL argument, such as a named collection identifier. Preview redacts
    environment references and password options without reading the environment.
    """

    source_type: str
    options: tuple[tuple[str, Any], ...]

    def __init__(self, source_type: str, **options: Any) -> None:
        for identifier in (source_type, *options):
            if not identifier.isascii() or not identifier.isidentifier():
                raise ConfigurationError(DICTIONARY_SOURCE_IDENTIFIER.format(identifier=identifier))
        normalized = tuple((name.upper(), deepcopy(value)) for name, value in options.items())
        if len({name for name, _ in normalized}) != len(normalized):
            raise ConfigurationError(DICTIONARY_SOURCE_DUPLICATE_OPTION)
        object.__setattr__(self, "source_type", source_type.upper())
        object.__setattr__(self, "options", normalized)

    def get_sql(self, literals: SqlLiterals, *, preview: bool = False) -> str:
        """Render SOURCE's contents, redacting explicit environment and password options in previews."""
        arguments = []
        for name, declared in self.options:
            if preview and (isinstance(declared, EnvironmentValue) or name in CLICKHOUSE_DICTIONARY_SECRET_OPTIONS):
                value = CLICKHOUSE_DICTIONARY_HIDDEN_PASSWORD
            else:
                value = declared.get_value() if isinstance(declared, EnvironmentValue) else declared
            sql = value.sql if isinstance(value, RawSQLTerm) else literals.get_literal_sql(value)
            arguments.append(f"{name} {sql}")
        return f"{self.source_type}({' '.join(arguments)})"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        return ClassPath.get(type(self)), [self.source_type], deepcopy(dict(self.options))
