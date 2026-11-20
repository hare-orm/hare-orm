from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


class GeneratedKeys:
    """The generated keys of rows a database hands out before they are written - its series of
    numbers: each instance written without its key gets one, and is
    written with it."""

    @staticmethod
    async def assign(binding: ModelBinding, connection: DatabaseClient, objs: Sequence[Model]) -> None:
        """Gives each instance without its generated key the next key of the model's series - nothing
        for a model of no generated key, or a database generating keys as it writes the rows.

        Args:
            binding: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be written.

        Raises:
            UnSupportedError: The database generates no keys.
            ValidationError: A key is beyond the key field's range - the series outgrew the field.
        """
        key_field_name = binding.generated_pk_field_name
        if key_field_name is None:
            return
        features = connection.features
        if not features.takes_keys_before_insert:
            if not features.supports_generated_keys:
                raise UnSupportedError(
                    f'"{binding.full_name}.{key_field_name}" is a primary key the database generates, but the '
                    f"{connection.dialect.name} server of connection {connection.connection_alias!r} generates no keys"
                )
            return
        keyless_instances = [instance for instance in objs if not instance._custom_generated_pk]
        if not keyless_instances:
            return
        keys = await GeneratedKeys.take(binding, connection, len(keyless_instances))
        for instance, key in zip(keyless_instances, keys, strict=True):
            setattr(instance, key_field_name, key)
            object.__setattr__(instance, "_custom_generated_pk", True)

    @staticmethod
    async def take(binding: ModelBinding, connection: DatabaseClient, count: int) -> list[Any]:
        """The next keys of the model's series, each checked by the key field's validators - a column of the
        database may take a key beyond the field's range.

        Args:
            binding: The model, its generated key taken from a series (``Features.takes_keys_before_insert``).
            connection: The connection the rows are written on.
            count: How many keys.

        Returns:
            The keys.

        Raises:
            ValidationError: A key is beyond the key field's range - the series outgrew the field.
        """
        key_field_name: str = binding.generated_pk_field_name  # type: ignore[assignment]
        keys = await connection.take_generated_keys(binding, count)
        key_field = binding.fields_map[key_field_name]
        for key in keys:
            key_field.validate(key)
        return list(keys)
