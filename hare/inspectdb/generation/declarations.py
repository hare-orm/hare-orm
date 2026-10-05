from __future__ import annotations

from dataclasses import dataclass


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
    """

    dialect: str
    app_label: str = "models"
    foreign_key_target_overrides: dict[str, str] | None = None
    skip_many_to_many_through_tables: bool = True
    class_name: str | None = None
