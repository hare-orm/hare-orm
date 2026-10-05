from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import IntegrityError
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.write.constraints.constants import CONSTRAINT_CHECK_CHUNK_SIZE
from hare.query.enums import Connector
from hare.query.expressions import Q
from hare.query.expressions.conditions.hidden_values_q import HiddenValuesQ
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.constants import HIDDEN_PARAMETER_TEXT

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
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
            if changed_attribute_names is None and field.related_model is model:
                # New rows of a relation to their own model: a row written beside the one it names
                # is there once the rows are written, as in one INSERT of them.
                to_field_names = field.to_field_names
                values -= {tuple(getattr(instance, name) for name in to_field_names) for instance in objs}
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
        targets_primary_key = tuple(to_field_names) == tuple(related_model._meta.primary_key_attribute_names)
        # The related key fields aren't sensitive themselves - the values are the relation's.
        condition_class = HiddenValuesQ if field_name in model._meta.sensitive_fields else Q
        for start in range(0, len(value_rows), chunk_size):
            chunk = value_rows[start : start + chunk_size]
            if len(to_field_names) == 1:
                matching = queryset.filter(condition_class(**{f"{to_field_names[0]}__in": [row[0] for row in chunk]}))
                found.update((value,) for value in await matching.values_list(to_field_names[0], flat=True))
                continue
            # A key of several columns - the rows of a chunk in one query too.
            matching = queryset.filter(
                condition_class(pk__in=chunk)
                if targets_primary_key
                else condition_class.with_connector(
                    Connector.OR, *(Q(**dict(zip(to_field_names, row, strict=True))) for row in chunk)
                )
            )
            found.update(tuple(row) for row in await matching.values_list(*to_field_names))
        missing = [row for row in value_rows if row not in found]
        if missing:
            values_text = (
                HIDDEN_PARAMETER_TEXT
                if field_name in model._meta.sensitive_fields
                else ", ".join(repr(row[0] if len(row) == 1 else row) for row in missing[:5])
            )
            raise IntegrityError(
                f"{model.__name__}.{field_name}: no {related_model.__name__} row of {values_text} - the relation "
                f"is declared to the database (db_constraint=True), and the {connection.dialect.name} database keeps "
                "no foreign keys: hare checked it"
            )

    @staticmethod
    def get_referencing_relations(
        model: type[Model], changed_attribute_names: set[str]
    ) -> list[ForeignKeyFieldInstance[Any]]:
        """The relations of other rows to a model's rows declared to the database (``db_constraint=True``)
        whose key an update changes - a row naming the old key would name no row.

        Args:
            model: The model updated.
            changed_attribute_names: The attributes the update changes.

        Returns:
            The relations, each a foreign key of the referencing model.
        """
        return [
            foreign_key_field
            for __, foreign_key_field in DeletionGraph.get_backward_relations(model)
            if foreign_key_field.db_constraint
            and foreign_key_field.source_fields
            and not changed_attribute_names.isdisjoint(foreign_key_field.to_field_names)
        ]

    @classmethod
    async def check_released_keys(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        foreign_key_field: ForeignKeyFieldInstance[Any],
        released_keys: set[tuple[Any, ...]],
    ) -> None:
        """Refuses an update taking keys away from rows that other rows still name - the database's
        ``ON UPDATE NO ACTION``.

        Args:
            model: The model updated.
            connection: The connection the rows are written on.
            foreign_key_field: The relation naming the keys.
            released_keys: The keys the update leaves no row with.

        Raises:
            IntegrityError: A row of the relation names a released key.
        """
        if not released_keys:
            return
        referencing_model = foreign_key_field.model
        source_fields = foreign_key_field.source_fields
        queryset = RowScopes.get_base_queryset(
            referencing_model, RowVisibility(all_tenants=True, include_deleted=True)
        )
        if referencing_model._meta.default_connection == model._meta.default_connection:
            queryset = queryset.using(connection)
        key_rows = sorted(released_keys, key=repr)
        for start in range(0, len(key_rows), CONSTRAINT_CHECK_CHUNK_SIZE):
            chunk = key_rows[start : start + CONSTRAINT_CHECK_CHUNK_SIZE]
            if len(source_fields) == 1:
                matching = queryset.filter(**{f"{source_fields[0]}__in": [row[0] for row in chunk]})
            else:
                matching = queryset.filter(
                    Q.with_connector(Connector.OR, *(Q(**dict(zip(source_fields, row, strict=True))) for row in chunk))
                )
            if await matching.exists():
                raise IntegrityError(
                    f"{model.__name__}: rows of {referencing_model.__name__}.{foreign_key_field.model_field_name} "
                    "still name the key the update changes - the relation is declared to the database "
                    f"(db_constraint=True), and the {connection.dialect.name} database keeps no foreign keys: "
                    "hare checked it"
                )

    @classmethod
    async def check_changed_keys(
        cls, model: type[Model], connection: DatabaseClient, objs: Sequence[Model], changed_attribute_names: set[str]
    ) -> None:
        """Refuses an update of objs that changes a key other rows still name.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be updated.
            changed_attribute_names: The attributes the update changes.

        Raises:
            IntegrityError: A row of another model names a key the update changes.
        """
        relations = cls.get_referencing_relations(model, changed_attribute_names)
        if not relations:
            return
        primary_key_names = model._meta.primary_key_attribute_names
        stored = RowScopes.get_base_queryset(model, RowVisibility(all_tenants=True, include_deleted=True)).using(
            connection
        )
        get_key = (lambda obj: obj.pk) if len(primary_key_names) == 1 else (lambda obj: tuple(obj.pk))
        for foreign_key_field in relations:
            to_field_names = foreign_key_field.to_field_names
            new_keys_by_pk = {get_key(obj): tuple(getattr(obj, name) for name in to_field_names) for obj in objs}
            stored_rows = await stored.filter(pk__in=list(new_keys_by_pk)).values_list(
                *primary_key_names, *to_field_names
            )
            primary_key_count = len(primary_key_names)
            new_keys = set(new_keys_by_pk.values())
            released_keys = set()
            for row in stored_rows:
                pk = row[0] if primary_key_count == 1 else tuple(row[:primary_key_count])
                old_key = tuple(row[primary_key_count:])
                if None not in old_key and old_key != new_keys_by_pk.get(pk) and old_key not in new_keys:
                    released_keys.add(old_key)
            await cls.check_released_keys(model, connection, foreign_key_field, released_keys)
