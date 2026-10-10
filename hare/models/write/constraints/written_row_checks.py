from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.models.write.constraints.foreign_key_checks import ForeignKeyChecks
from hare.models.write.constraints.unique_checks import UniqueChecks
from hare.query.expressions import Expression
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


class WrittenRowChecks:
    """The uniqueness and the relations a model declares, checked by hare before its rows are written
    on a database keeping neither (``Features.checks_constraints_before_write``) - nothing elsewhere."""

    @staticmethod
    async def check_new_rows(
        model: type[Model],
        connection: DatabaseClient,
        objs: Sequence[Model],
        series_keyed: Collection[Model] = (),
    ) -> None:
        """Refuses new rows breaking a declared uniqueness or naming missing related rows.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be inserted.
            series_keyed: The objs keyed by a series - their keys are unique by themselves.

        Raises:
            IntegrityError: A row breaks a uniqueness or names a missing related row.
        """
        if not connection.features.checks_constraints_before_write or not objs:
            return
        series_keyed_ids = {id(instance) for instance in series_keyed}
        await UniqueChecks.check_rows(model, connection, objs, series_keyed_ids=series_keyed_ids)
        await ForeignKeyChecks.check_rows(model, connection, objs)

    @staticmethod
    async def check_changed_rows(
        model: type[Model], connection: DatabaseClient, objs: Sequence[Model], field_names: Collection[str] | None
    ) -> None:
        """Refuses stored rows an update makes break a declared uniqueness or name missing related rows.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be updated.
            field_names: The fields written; None for every field.

        Raises:
            IntegrityError: A row breaks a uniqueness or names a missing related row.
        """
        if not connection.features.checks_constraints_before_write or not objs:
            return
        changed_attribute_names = set(
            DeclaredUniqueness.get_attribute_names(
                model, field_names if field_names is not None else model._meta.fields_db_projection
            )
        )
        await UniqueChecks.check_rows(model, connection, objs, changed_attribute_names=changed_attribute_names)
        await ForeignKeyChecks.check_rows(model, connection, objs, changed_attribute_names=changed_attribute_names)

    @staticmethod
    async def check_query_update(
        queryset: QuerySet[Any, Any], connection: DatabaseClient, values: dict[str, Any]
    ) -> None:
        """Refuses an update of the rows of a queryset breaking a declared uniqueness or naming missing
        related rows - the values written as they are, not computed by the database.

        Args:
            queryset: The rows updated.
            connection: The connection the rows are written on.
            values: The values written, by field name.

        Raises:
            IntegrityError: The values break a uniqueness or name a missing related row.
        """
        if not connection.features.checks_constraints_before_write:
            return
        model = queryset.model
        meta = model._meta
        literal_values = {name: value for name, value in values.items() if not isinstance(value, Expression | Term)}
        if not literal_values:
            return
        # The values by the attribute holding them - a relation's by its key attributes.
        values_by_attribute: dict[str, Any] = {}
        # A relation set by its key attribute (``team_id=...``) - by its single key column.
        relation_by_key_attribute = {
            meta.fields_map[relation_name].source_fields[0]: relation_name
            for relation_name in meta.foreign_key_fields | meta.one_to_one_fields
            if len(meta.fields_map[relation_name].source_fields) == 1
        }
        for written_name, value in literal_values.items():
            field_name = relation_by_key_attribute.get(written_name, written_name)
            field = meta.fields_map[field_name]
            if field_name in meta.foreign_key_fields | meta.one_to_one_fields:
                key = getattr(value, "pk", value)
                keys = key if isinstance(key, tuple) else (key,)
                values_by_attribute.update(zip(field.source_fields, keys, strict=True))
                if getattr(field, "db_constraint", False) and value is not None:
                    await ForeignKeyChecks.check_values(model, connection, field_name, {keys})
            else:
                values_by_attribute[field_name] = value
        matched_keys: list[Any] | None = None
        for uniqueness in DeclaredUniqueness.get_declared(model):
            if not set(uniqueness.attribute_names) <= values_by_attribute.keys():
                continue
            written = tuple(values_by_attribute[name] for name in uniqueness.attribute_names)
            if uniqueness.nulls_distinct and None in written:
                continue
            if matched_keys is None:
                matched_keys = list(await queryset.using(connection).values_list(*meta.primary_key_attribute_names))
            # One value written into several rows - each holds it.
            if len(matched_keys) > 1:
                raise UniqueChecks.get_error(model, connection, uniqueness, written)
            existing = await UniqueChecks.fetch_existing(model, connection, uniqueness, [written])
            own_keys = {key[0] if len(meta.primary_key_attribute_names) == 1 else tuple(key) for key in matched_keys}
            if any(key not in own_keys for key in existing.get(written, [])):
                raise UniqueChecks.get_error(model, connection, uniqueness, written)
