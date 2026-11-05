from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlparse import tokens
from sqlparse.lexer import tokenize

from hare.dialects.base.constants import SQL_DIALECT
from hare.inspectdb.constants import (
    CATALOG_SCHEMA_MEMBERS,
    CATALOG_SQL_MEMBERS,
    CATALOG_SQL_POSITIONAL_DECLARATIONS,
    CATALOG_SQL_TEXT_PARTS,
)
from hare.inspectdb.introspection.enums import SchemaPartType

if TYPE_CHECKING:
    from hare.inspectdb.introspection.schema_part import SchemaPart


class CatalogIdentities:
    """Map known catalog references without substituting arbitrary text fragments.

    Schema names are mapped as complete identifiers. SQL fields are tokenized;
    only the schema component of a qualified reference changes. Strings, comments
    and unknown scalar metadata remain literal. Dialects can declare catalog casts
    that contain object references and provide structured engine identity handling.
    """

    def __init__(self, schemas: Mapping[str, str], reference_casts: frozenset[str] = frozenset()) -> None:
        self.schemas = {**{name: name for name in schemas.values()}, **schemas}
        self.reference_casts = reference_casts

    def map_part(self, part: SchemaPart) -> SchemaPart:
        """Map definitions; table and object names already have their logical namespace."""
        if part.part_type is SchemaPartType.COMMENT:
            return part
        definition = (
            self.map_sql(part.definition)
            if isinstance(part.definition, str) and part.part_type in CATALOG_SQL_TEXT_PARTS
            else self.map_definition(part.definition)
        )
        name = part.name
        if part.part_type is SchemaPartType.GRANT:
            # A grant is identified by its normalized content, not a database object name.
            previous_identity = repr(part.definition)
            if part.name.endswith(previous_identity):
                prefix = part.name[: -len(previous_identity)]
                name = prefix + repr(definition)
        signatures = (
            tuple(self.map_sql(signature) for signature in part.operation_signatures)
            if part.operation_signatures is not None
            else None
        )
        return replace(
            part,
            name=name,
            definition=definition,
            operation_signatures=signatures,
            unrepresented_definition=self.map_definition(part.unrepresented_definition),
        )

    def map_definition(self, value: Any, member: str = "") -> Any:
        """Traverse catalog structure, preserving literal values and dictionary keys."""
        if isinstance(value, dict):
            return {key: self.map_definition(item, key) for key, item in value.items()}
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in CATALOG_SQL_POSITIONAL_DECLARATIONS:
                return [
                    value[0],
                    self.map_definition(value[1], "sql"),
                    *[self.map_definition(item) for item in value[2:]],
                ]
            return [self.map_definition(item, member) for item in value]
        if isinstance(value, str):
            if member in CATALOG_SCHEMA_MEMBERS:
                return self.schemas.get(value, value)
            if member in CATALOG_SQL_MEMBERS:
                return self.map_sql(value)
            if member == "engine":
                return self.map_engine(value)
        return value

    def map_engine(self, sql: str) -> str:
        """Map qualified references; engine-specific literal arguments need a dialect mapper."""
        return self.map_sql(sql)

    def map_sql(self, sql: str) -> str:
        """Canonicalize mapped schema identifiers, retaining all other tokens verbatim.

        Apply to both compared catalogs. Quoting mapped identifiers consistently
        avoids guessing whether a real schema name is a dialect keyword.
        """
        lexemes = list(tokenize(sql))
        significant = [
            position
            for position, (token_type, _) in enumerate(lexemes)
            if token_type not in tokens.Whitespace and token_type not in tokens.Comment
        ]
        replacements = {}
        for offset, position in enumerate(significant):
            token_type, value = lexemes[position]
            following = significant[offset + 1 : offset + 3]
            if (
                token_type is tokens.Literal.String.Single
                and len(following) == 2
                and lexemes[following[0]][1] == "::"
                and lexemes[following[1]][1].lower() in self.reference_casts
            ):
                reference = value[1:-1].replace("''", "'")
                mapped = self.map_sql(reference)
                if mapped != reference:
                    replacements[position] = SQL_DIALECT.literals.get_string_literal_sql(mapped)
                continue
            if not following or lexemes[following[0]][1] != ".":
                continue
            if token_type in tokens.Literal and token_type is not tokens.Literal.String.Symbol:
                continue
            quote = value[:1] if value.startswith(('"', "`")) else ""
            identifier = value[1:-1].replace(quote * 2, quote) if quote else value
            target = self.schemas.get(identifier)
            if target is None:
                continue
            replacements[position] = SQL_DIALECT.literals.quote_identifier(target)
        return "".join(replacements.get(position, value) for position, (_, value) in enumerate(lexemes))
