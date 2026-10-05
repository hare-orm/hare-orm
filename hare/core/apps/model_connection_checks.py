from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.exceptions import ConfigurationError
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ModelConnectionChecks:
    """The refusal of a model whose writes its connection's database can't make - checked when the
    model is bound to the connection, before any of its rows is written."""

    @staticmethod
    def check_model_writes(model: type[Model]) -> None:
        """Refuses a model whose primary key the database generates on a connection without
        generated keys, a uniqueness hare can't check on one keeping none, a ``UniqueConstraint`` option
        the database has no clause for, and a soft-deleted model on one that doesn't update stored rows.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: The model's connection can't make one of its writes.
        """
        meta = model._meta
        features = meta.connection.features
        if meta.generated_pk_field_name is not None and not features.supports_generated_keys:
            raise ConfigurationError(
                f'"{meta.full_name}.{meta.generated_pk_field_name}" is a primary key the database generates, '
                f'but the {meta.connection.dialect.name} database of connection "{meta.default_connection}" generates '
                "no keys - give the field generated=False and a default (e.g. default=uuid.uuid7 on a UUIDField), "
                "or set the key on every row written"
            )
        if features.checks_constraints_before_write and (
            unchecked := DeclaredUniqueness.get_unchecked_declarations(model)
        ):
            raise ConfigurationError(
                f'"{meta.full_name}" declares {", ".join(unchecked)}, but the {meta.connection.dialect.name} '
                f'database of connection "{meta.default_connection}" keeps nothing unique, and hare checks only a '
                "uniqueness of fields, with no condition or a Q one"
            )
        if features.supports_unique_constraints:
            ModelConnectionChecks.raise_if_unique_constraint_options_unsupported(model)
        if meta.soft_delete_field is not None and not features.supports_row_updates:
            raise ConfigurationError(
                f'"{meta.full_name}" is soft-deleted (Meta.soft_delete_field), but the {meta.connection.dialect.name} '
                f'database of connection "{meta.default_connection}" doesn\'t update stored rows'
            )

    @staticmethod
    def raise_if_unique_constraint_options_unsupported(model: type[Model]) -> None:
        """Refuses a ``UniqueConstraint`` of ``nulls_distinct`` or ``without_overlaps`` on a database
        without the clause - before a migration of it is written, which no migrate could apply.

        Args:
            model: The model, bound to its connection.

        Raises:
            ConfigurationError: A constraint sets such an option.
        """
        meta = model._meta
        features = meta.connection.features
        for constraint in meta.constraints:
            if not isinstance(constraint, UniqueConstraint):
                continue
            for option, is_set, is_supported in (
                ("nulls_distinct", constraint.nulls_distinct is not None, features.supports_nulls_distinct),
                ("without_overlaps", bool(constraint.without_overlaps), features.supports_without_overlaps),
            ):
                if is_set and not is_supported:
                    constraint_name = constraint.name or ", ".join(constraint.fields)
                    raise ConfigurationError(
                        f'"{meta.full_name}" declares UniqueConstraint({constraint_name}) with {option}, which the '
                        f'{meta.connection.dialect.name} database of connection "{meta.default_connection}" doesn\'t '
                        "support"
                    )
