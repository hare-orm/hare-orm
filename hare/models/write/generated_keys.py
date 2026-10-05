from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError, ValidationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class GeneratedKeys:
    """The generated keys of rows a database hands out before they are written - its series of
    numbers: each instance written without its key gets one, and is
    written with it."""

    @staticmethod
    async def assign(model: type[Model], connection: DatabaseClient, objs: Sequence[Model]) -> list[Model]:
        """Gives each instance without its generated key the next key of the model's series - nothing
        for a model of no generated key, or a database generating keys as it writes the rows.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be written.

        Returns:
            The objs given a key.

        Raises:
            UnSupportedError: The database generates no keys.
            ValidationError: A key is beyond the key field's range - the series outgrew the field.
        """
        meta = model._meta
        key_field_name = meta.generated_pk_field_name
        if key_field_name is None:
            return []
        features = connection.features
        if not features.takes_keys_before_insert:
            if not features.supports_generated_keys:
                raise UnSupportedError(
                    f'"{meta.full_name}.{key_field_name}" is a primary key the database generates, but the '
                    f"{connection.dialect.name} server of connection {connection.connection_alias!r} generates no keys"
                )
            return []
        keyless_instances = [instance for instance in objs if not instance._custom_generated_pk]
        if not keyless_instances:
            return []
        keys = await connection.take_generated_keys(model, len(keyless_instances))
        # Every check of the field - a column of the database may take a key beyond its range.
        key_field = meta.fields_map[key_field_name]
        for key in keys:
            for validator in key_field.validators:
                try:
                    validator(key)
                except ValidationError as error:
                    raise ValidationError(f"{key_field_name}: the key {key} of the series - {error}") from error
        for instance, key in zip(keyless_instances, keys, strict=True):
            setattr(instance, key_field_name, key)
            object.__setattr__(instance, "_custom_generated_pk", True)
        return keyless_instances
