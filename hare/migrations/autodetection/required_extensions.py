from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.migrations.state.model_state import ModelState
    from hare.models import Model


class RequiredExtensions:
    """The database extensions a model's table needs on some dialects - ``Meta.extensions``, and what
    each object of the model names for itself through the dialect: a field's column type (the
    dialect's types), an index (its class), a constraint (the dialect's schema editor). Each answers
    None where it needs none; no extension is named here."""

    @classmethod
    def get_for_model_state(cls, model_state: ModelState, dialects: Iterable[Dialect]) -> set[str]:
        """The extensions a model of a migration state needs on any of the dialects.

        Args:
            model_state: The model's state.
            dialects: The dialects - every registered one for a migration file, run on any database.

        Returns:
            The extension names.
        """
        options = model_state.options
        primary_key_attribute = options.get(ModelOption.PRIMARY_KEY_ATTRIBUTE)
        return cls.get_for_declarations(
            dialects,
            declared_extensions=options.get(ModelOption.EXTENSIONS, ()),
            fields_by_name=model_state.fields,
            indexes=options.get(ModelOption.INDEXES, ()),
            constraints=options.get(ModelOption.CONSTRAINTS, ()),
            primary_key_without_overlaps=(
                primary_key_attribute
                if options.get(ModelOption.PK_WITHOUT_OVERLAPS) and isinstance(primary_key_attribute, tuple)
                else None
            ),
        )

    @classmethod
    def get_for_model(cls, model: type[Model], dialects: Iterable[Dialect]) -> set[str]:
        """The extensions a model needs on any of the dialects.

        Args:
            model: The model.
            dialects: The dialects.

        Returns:
            The extension names.
        """
        meta = model._meta
        return cls.get_for_declarations(
            dialects,
            declared_extensions=meta.extensions,
            fields_by_name=meta.fields_map,
            indexes=meta.indexes,
            constraints=meta.constraints,
            primary_key_without_overlaps=meta.primary_key_attribute if meta.pk_without_overlaps else None,
        )

    @staticmethod
    def get_for_declarations(
        dialects: Iterable[Dialect],
        *,
        declared_extensions: Iterable[str],
        fields_by_name: Mapping[str, Field[Any]],
        indexes: Iterable[Any],
        constraints: Iterable[Any],
        primary_key_without_overlaps: Sequence[str] | None,
    ) -> set[str]:
        """The extensions a model's declarations need on any of the dialects.

        Args:
            dialects: The dialects.
            declared_extensions: ``Meta.extensions``.
            fields_by_name: The model's fields, by name.
            indexes: ``Meta.indexes``.
            constraints: ``Meta.constraints``.
            primary_key_without_overlaps: The fields of a primary key compared ``WITHOUT OVERLAPS``,
                None for another key.

        Returns:
            The extension names.
        """
        extensions = set(declared_extensions)
        for dialect in dialects:
            constraint_statements_class = dialect.schema_editor_class.constraint_statements_class
            answers: list[str | None] = [field.get_required_extension(dialect) for field in fields_by_name.values()]
            answers.extend(
                index.get_required_extension(dialect, fields_by_name) for index in indexes if isinstance(index, Index)
            )
            for constraint in constraints:
                if isinstance(constraint, ExclusionConstraint):
                    answers.append(
                        constraint_statements_class.get_exclusion_constraint_extension(constraint, fields_by_name)
                    )
                elif isinstance(constraint, UniqueConstraint) and constraint.without_overlaps:
                    answers.append(
                        constraint_statements_class.get_without_overlaps_extension(constraint.fields, fields_by_name)
                    )
            if primary_key_without_overlaps is not None:
                answers.append(
                    constraint_statements_class.get_without_overlaps_extension(
                        primary_key_without_overlaps, fields_by_name
                    )
                )
            extensions.update(answer for answer in answers if answer is not None)
        return extensions
