from __future__ import annotations

from collections.abc import Collection, Hashable, Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError
from hare.fields.database_default import DatabaseDefault
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.models.write.constraints.foreign_key_checks import ForeignKeyChecks
from hare.models.write.constraints.unique_checks import UniqueChecks
from hare.models.write.constraints.unique_key_locks import UniqueKeyLocks
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
    ) -> list[Hashable]:
        """Refuses new rows breaking a declared uniqueness or naming missing related rows - their
        unique values held until the caller releases them (``UniqueKeyLocks.release()``) once the
        rows are written.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be inserted.
            series_keyed: The objs keyed by a series - their keys are unique by themselves.

        Returns:
            The unique values held.

        Raises:
            IntegrityError: A row breaks a uniqueness or names a missing related row.
            UnSupportedError: A field of a row's primary key is left to its ``db_default``.
        """
        if not connection.features.checks_constraints_before_write or not objs:
            return []
        WrittenRowChecks.raise_if_key_left_to_database(model, connection, objs)
        held_keys = await UniqueKeyLocks.acquire(model, connection, objs)
        try:
            series_keyed_ids = {id(instance) for instance in series_keyed}
            await UniqueChecks.check_rows(model, connection, objs, series_keyed_ids=series_keyed_ids)
            await ForeignKeyChecks.check_rows(model, connection, objs)
        except BaseException:
            UniqueKeyLocks.release(held_keys)
            raise
        return held_keys

    @staticmethod
    def raise_if_key_left_to_database(model: type[Model], connection: DatabaseClient, objs: Sequence[Model]) -> None:
        """Refuses a new row whose primary key takes a field's ``db_default`` - on a database checking
        no uniqueness and returning no row of a write, the key could neither be checked unique nor
        read back.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be inserted.

        Raises:
            UnSupportedError: A field of an obj's primary key holds ``DatabaseDefault``.
        """
        for name in model._meta.primary_key_attribute_names:
            if any(isinstance(getattr(obj, name, None), DatabaseDefault) for obj in objs):
                raise UnSupportedError(
                    f"{model.__name__}.{name} is part of the primary key and left to its db_default - the "
                    f"{connection.dialect.name} database returns no row of a write, so the key could be neither "
                    "checked unique nor read back; give it a value (default= computes one in Python)"
                )

    @staticmethod
    async def check_changed_rows(
        model: type[Model], connection: DatabaseClient, objs: Sequence[Model], field_names: Collection[str] | None
    ) -> list[Hashable]:
        """Refuses stored rows an update makes break a declared uniqueness or name missing related rows
        - the unique values written held until the caller releases them (``UniqueKeyLocks.release()``)
        once the rows are written.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs about to be updated.
            field_names: The fields written; None for every field.

        Returns:
            The unique values held.

        Raises:
            IntegrityError: A row breaks a uniqueness or names a missing related row.
        """
        if not connection.features.checks_constraints_before_write or not objs:
            return []
        changed_attribute_names = set(
            DeclaredUniqueness.get_attribute_names(
                model, field_names if field_names is not None else model._meta.fields_db_projection
            )
        )
        held_keys = await UniqueKeyLocks.acquire(model, connection, objs, changed_attribute_names)
        try:
            await UniqueChecks.check_rows(model, connection, objs, changed_attribute_names=changed_attribute_names)
            await ForeignKeyChecks.check_rows(model, connection, objs, changed_attribute_names=changed_attribute_names)
            await ForeignKeyChecks.check_changed_keys(model, connection, objs, changed_attribute_names)
        except BaseException:
            UniqueKeyLocks.release(held_keys)
            raise
        return held_keys

    @staticmethod
    async def check_query_key_changes(
        queryset: QuerySet[Any, Any],
        connection: DatabaseClient,
        changed_attribute_names: set[str],
        literal_values: dict[str, Any],
        expression_values: dict[str, Any],
    ) -> None:
        """Refuses an update of the rows of a queryset changing a key other rows still name - the rows
        are read with the keys written (an expression's computed by the database).

        Args:
            queryset: The rows updated.
            connection: The connection the rows are written on.
            changed_attribute_names: The attributes the update changes.
            literal_values: The literal values, by attribute.
            expression_values: The expressions, by attribute.

        Raises:
            IntegrityError: A row of another model names a key the update changes.
        """
        model = queryset.model
        relations = ForeignKeyChecks.get_referencing_relations(model, changed_attribute_names)
        if not relations:
            return
        rows = queryset.using(connection)
        aliases = {name: f"hare_written_{name}" for name in expression_values}
        if aliases:
            rows = rows.annotate(**{aliases[name]: expression for name, expression in expression_values.items()})
        objs = list(await rows)
        for obj in objs:
            for attribute_name, value in literal_values.items():
                setattr(obj, attribute_name, value)
            for attribute_name, alias in aliases.items():
                setattr(obj, attribute_name, getattr(obj, alias))
        await ForeignKeyChecks.check_changed_keys(model, connection, objs, changed_attribute_names)

    @staticmethod
    async def check_query_update(
        queryset: QuerySet[Any, Any], connection: DatabaseClient, values: dict[str, Any]
    ) -> list[Hashable]:
        """Refuses an update of the rows of a queryset breaking a declared uniqueness or naming missing
        related rows. The rows a uniqueness the update touches - a field of it, or of its condition -
        are read and given the values written (an expression's computed by the database), then
        checked as rows a save writes: against each other and against the other stored rows.

        Args:
            queryset: The rows updated.
            connection: The connection the rows are written on.
            values: The values written, by field name.

        Returns:
            The unique values written, held until the caller releases them
            (``UniqueKeyLocks.release()``) once the update ran.

        Raises:
            IntegrityError: The values break a uniqueness or name a missing related row.
        """
        if not connection.features.checks_constraints_before_write or not values:
            return []
        model = queryset.model
        meta = model._meta
        relation_names = meta.foreign_key_fields | meta.one_to_one_fields
        # A relation set by its key attribute (``team_id=...``) - by its single key column.
        relation_by_key_attribute = {
            meta.fields_map[relation_name].source_fields[0]: relation_name
            for relation_name in relation_names
            if len(meta.fields_map[relation_name].source_fields) == 1
        }
        # The literal values by the attribute holding them - a relation's by its key attributes - and
        # the expressions by the attribute they are written into.
        literal_values: dict[str, Any] = {}
        expression_values: dict[str, Any] = {}
        for written_name, value in values.items():
            if isinstance(value, Expression | Term):
                expression_values[written_name] = value
                continue
            field_name = relation_by_key_attribute.get(written_name, written_name)
            field = meta.fields_map[field_name]
            if field_name not in relation_names:
                literal_values[field_name] = value
                continue
            key = getattr(value, "pk", value)
            keys = key if isinstance(key, tuple) else (key,)
            if value is None:
                keys = (None,) * len(field.source_fields)
            literal_values.update(zip(field.source_fields, keys, strict=True))
            if getattr(field, "db_constraint", False) and value is not None:
                await ForeignKeyChecks.check_values(model, connection, field_name, {keys})
        changed_attribute_names = {*literal_values, *expression_values}
        await WrittenRowChecks.check_query_key_changes(
            queryset, connection, changed_attribute_names, literal_values, expression_values
        )
        if not any(
            uniqueness.reads_any(changed_attribute_names) for uniqueness in DeclaredUniqueness.get_declared(model)
        ):
            return []
        rows = queryset.using(connection)
        aliases = {name: f"hare_written_{name}" for name in expression_values}
        if aliases:
            rows = rows.annotate(**{aliases[name]: expression for name, expression in expression_values.items()})
        objs = list(await rows)
        for obj in objs:
            for attribute_name, value in literal_values.items():
                setattr(obj, attribute_name, value)
            for attribute_name, alias in aliases.items():
                setattr(obj, attribute_name, getattr(obj, alias))
        held_keys = await UniqueKeyLocks.acquire(model, connection, objs, changed_attribute_names)
        try:
            await UniqueChecks.check_rows(model, connection, objs, changed_attribute_names=changed_attribute_names)
        except BaseException:
            UniqueKeyLocks.release(held_keys)
            raise
        return held_keys
