from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from hare.declarations.declaration_values import DeclarationValues
from hare.inspectdb.introspection.enums import SchemaPartType


@dataclass(frozen=True)
class SchemaPart:
    """One part of a schema as the database prints it - a column, an index, a constraint, a view - by its
    type, its table and its name; two schemas are compared part by part.

    Args:
        part_type: What the part is.
        table: The table it belongs to - ``""`` for a part of the schema itself (a view, an ``ENUM`` type).
        name: Its name, unique among the parts of its type and table.
        definition: What it is, in a form compared as is - plain values, lists and dicts.
        value: What the introspector read it as - kept for building the operation, not compared.
    """

    part_type: SchemaPartType
    table: str
    name: str
    definition: Any
    value: Any = field(default=None, compare=False)
    #: Canonical callable signatures used to decide whether a name-based operation is unambiguous.
    operation_signatures: tuple[str, ...] | None = field(default=None, compare=False, kw_only=True)
    #: Physical properties for which the reconstructed declaration provides no operation.
    unrepresented_definition: Any = field(default=None, compare=False, kw_only=True)

    @property
    def key(self) -> tuple[SchemaPartType, str, str]:
        """What a part of the other schema is matched by."""
        return self.part_type, self.table, self.name

    @classmethod
    def of(cls, part_type: SchemaPartType, table: str, name: str, value: Any) -> SchemaPart:
        """A part read as ``value``, its definition made from it.

        Args:
            part_type: What the part is.
            table: Its table.
            name: Its name.
            value: What the introspector read.

        Returns:
            The part.
        """
        return cls(part_type, table, name, cls.get_definition(value), value)

    @classmethod
    def get_definition(cls, value: Any) -> Any:
        """A value read back in the form parts are compared in: a read-back record by its fields, a
        declaration by the call a migration file writes it as.

        Args:
            value: The value.

        Returns:
            Plain values, lists and dicts.
        """
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if getattr(value, "migration_import_path", None):
            return DeclarationValues.get(value)
        deconstruct = getattr(value, "deconstruct", None)
        if callable(deconstruct):
            path, args, kwargs = deconstruct()
            return [path, cls.get_definition(args), cls.get_definition(kwargs)]
        if dataclasses.is_dataclass(value) and not isinstance(value, type) and not hasattr(value, "deconstruct"):
            return {
                item.name: cls.get_definition(getattr(value, item.name))
                for item in dataclasses.fields(value)
                if item.compare
            }
        if isinstance(value, dict):
            return {str(key): cls.get_definition(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls.get_definition(item) for item in value]
        return DeclarationValues.get(value)
