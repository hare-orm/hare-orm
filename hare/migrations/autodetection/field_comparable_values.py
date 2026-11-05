from __future__ import annotations

from typing import Any

from hare.declarations.declaration_values import DeclarationValues
from hare.fields.field import Field


class FieldComparableValues(DeclarationValues):
    """Comparable values in which a field is compared by what tells whether it changed between two model
    states (``StateSignatures.get_field_signature()``), not by every argument it was declared with."""

    @classmethod
    def get_object_value(cls, value: Any) -> object:
        if isinstance(value, Field):
            # Local import: the signatures module imports this one.
            from hare.migrations.autodetection.state_signatures import StateSignatures

            return StateSignatures.get_field_signature(value)
        return super().get_object_value(value)
