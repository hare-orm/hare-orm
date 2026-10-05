from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ModelConnectionChecks:
    """The refusal of a model whose writes its connection's database can't make - checked when the
    model is bound to the connection, before any of its rows is written."""

    @staticmethod
    def check_model_writes(model: type[Model]) -> None:
        """Refuses a model whose primary key the database generates on a connection without
        generated keys, and a soft-deleted model on one that doesn't update stored rows.

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
        if meta.soft_delete_field is not None and not features.supports_row_updates:
            raise ConfigurationError(
                f'"{meta.full_name}" is soft-deleted (Meta.soft_delete_field), but the {meta.connection.dialect.name} '
                f'database of connection "{meta.default_connection}" doesn\'t update stored rows'
            )
