from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field


@dataclass(frozen=True, slots=True)
class ModelGenerationOptions:
    """How a table is turned into a model.

    Attributes:
        dialect: The dialect the table was read from.
        app_label: The app label relations are qualified with.
        foreign_key_target_overrides: Per target table, the ``"<app>.<Class>"`` a relation points at.
        skip_many_to_many_through_tables: Whether a ManyToManyField through table gets a note instead
            of a class.
        class_name: The class name - ``ModelNaming.get_class_name()`` by default.
        connection_dialect: The dialect of the connection the table was read on - a field's column
            type may depend on its driver; the dialect's own by default.
        many_to_many_fields: The many-to-many fields of through tables of the run the model gets -
            preferred attribute name and field each.
    """

    dialect: str
    app_label: str = "models"
    foreign_key_target_overrides: dict[str, str] | None = None
    skip_many_to_many_through_tables: bool = True
    class_name: str | None = None
    connection_dialect: Dialect | None = None
    many_to_many_fields: tuple[tuple[str, Field[Any]], ...] = ()
