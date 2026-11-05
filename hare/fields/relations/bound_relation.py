from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models import Model


@dataclass(slots=True)
class BoundRelation:
    """Target and key identity prepared for one relation binding."""

    related_model: type[Model] | None = None
    to_field_instance: Field[Any] | None = None
    to_field_names: tuple[str, ...] = ()
    to_field_instances: tuple[Field[Any], ...] = ()
    source_fields: tuple[str, ...] = ()
    db_column_names: tuple[str, ...] = ()
    forward_key: str = ""
    forward_keys: tuple[str, ...] = ()
    backward_key: str = ""
    backward_keys: tuple[str, ...] = ()
    through: str = ""
    through_model_class: type[Model] | None = None
    through_schema: str | None = None
