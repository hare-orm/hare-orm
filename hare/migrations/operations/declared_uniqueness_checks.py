from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models.model_binding import ModelBinding


class DeclaredUniquenessChecks:
    """The refusal of a migration declaring a uniqueness on a database that keeps nothing unique
    (``Features.supports_unique_constraints``) - before any statement of it runs, as a model declaring one
    is refused when it is bound."""

    @staticmethod
    def raise_if_unsupported(editor: BaseSchemaEditor, binding: ModelBinding, declarations: Iterable[str]) -> None:
        """Refuses the uniquenesses a migration declares for a model, on a database keeping none.

        Args:
            editor: The schema editor the migration runs with.
            binding: The model.
            declarations: What declares a uniqueness - ``code (unique=True)``, ``UniqueConstraint(code)``.

        Raises:
            UnSupportedError: A uniqueness is declared and the database keeps none.
        """
        model = binding._model
        declarations = list(declarations)
        if not declarations or editor.client.features.supports_unique_constraints:
            return
        raise UnSupportedError(
            f"{model.__name__} declares {', '.join(declarations)}, but the {editor.client.dialect.name} database "
            "keeps nothing unique"
        )

    @staticmethod
    def describe(schema_object: Any) -> str:
        """An index or constraint declaring a uniqueness, as the refusal names it.

        Args:
            schema_object: The index or constraint.

        Returns:
            Its class with its name, or its fields - ``UniqueConstraint(code)``.
        """
        name = getattr(schema_object, "name", None)
        fields = getattr(schema_object, "fields", ())
        return f"{type(schema_object).__name__}({name or ', '.join(str(field) for field in fields)})"

    @staticmethod
    def get_unique_field_declarations(binding: ModelBinding, field_names: Iterable[str]) -> list[str]:
        """The fields of ``field_names`` declaring ``unique=True`` on a column of the model's table - a
        primary key is unique by itself, and a many-to-many relation's ``unique`` is the pair of its
        automatic through table, kept where the database keeps uniqueness.

        Args:
            binding: The model.
            field_names: The fields looked at.

        Returns:
            ``<name> (unique=True)`` of each.
        """
        relation_names = binding.forward_key_relation_names
        return [
            f"{field_name} (unique=True)"
            for field_name in field_names
            if (field := binding.fields_map.get(field_name)) is not None
            and getattr(field, "unique", False)
            and not getattr(field, "pk", False)
            and (field_name in binding.fields_db_projection or field_name in relation_names)
        ]
