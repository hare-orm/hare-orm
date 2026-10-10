from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import IntegrityError
from hare.models.write.constraints.constants import CONSTRAINT_CHECK_CHUNK_SIZE
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class ForeignKeyChecks:
    """The relations a model declares to the database (``db_constraint=True``), checked by hare before
    its rows are written - on a database keeping none of them (``Features.checks_constraints_before_write``):
    a row naming a related row that doesn't exist is refused as the database would refuse it. One query per
    relation per batch; a related row deleted in between the check and the write isn't seen."""

    @classmethod
    async def check_rows(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        objs: Sequence[Model],
        changed_attribute_names: set[str] | None = None,
    ) -> None:
        """Refuses rows naming related rows that don't exist.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be written.
            changed_attribute_names: The attributes an update changes - the other relations hold
                already; None for new rows.

        Raises:
            IntegrityError: A row names a related row that doesn't exist.
        """
        meta = model._meta
        for field_name in sorted(meta.foreign_key_fields | meta.one_to_one_fields):
            field: RelationalField[Any] = meta.fields_map[field_name]  # type: ignore[assignment]
            if not field.db_constraint or not field.source_fields:
                continue
            attribute_names = field.source_fields
            if changed_attribute_names is not None and changed_attribute_names.isdisjoint(attribute_names):
                continue
            values = {
                key
                for instance in objs
                if None not in (key := tuple(getattr(instance, name) for name in attribute_names))
            }
            if values:
                await cls.check_values(model, connection, field_name, values)

    @classmethod
    async def check_values(
        cls, model: type[Model], connection: DatabaseClient, field_name: str, values: set[tuple[Any, ...]]
    ) -> None:
        """Refuses values of a relation naming no related row.

        Args:
            model: The model.
            connection: The connection.
            field_name: The relation.
            values: The values of its key columns.

        Raises:
            IntegrityError: A value names no related row.
        """
        field: RelationalField[Any] = model._meta.fields_map[field_name]  # type: ignore[assignment]
        related_model = field.related_model
        to_field_names = field.to_field_names
        related_connection = (
            connection if related_model._meta.default_connection == model._meta.default_connection else None
        )
        queryset = RowScopes.get_base_queryset(related_model, RowVisibility(all_tenants=True, include_deleted=True))
        if related_connection is not None:
            queryset = queryset.using(related_connection)
        value_rows = sorted(values, key=repr)
        found: set[tuple[Any, ...]] = set()
        chunk_size = (
            (related_connection or related_model.get_connection()).features.max_bind_parameters
            if len(to_field_names) == 1
            else CONSTRAINT_CHECK_CHUNK_SIZE
        )
        for start in range(0, len(value_rows), chunk_size):
            chunk = value_rows[start : start + chunk_size]
            if len(to_field_names) == 1:
                matching = queryset.filter(**{f"{to_field_names[0]}__in": [row[0] for row in chunk]})
                found.update((value,) for value in await matching.values_list(to_field_names[0], flat=True))
            else:
                for row in chunk:
                    if await queryset.filter(**dict(zip(to_field_names, row, strict=True))).exists():
                        found.add(row)
        missing = [row for row in value_rows if row not in found]
        if missing:
            values_text = ", ".join(repr(row[0] if len(row) == 1 else row) for row in missing[:5])
            raise IntegrityError(
                f"{model.__name__}.{field_name}: no {related_model.__name__} row of {values_text} - the relation "
                f"is declared to the database (db_constraint=True), and the {connection.dialect.name} database keeps "
                "no foreign keys: hare checked it"
            )
